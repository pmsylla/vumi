# -*- test-case-name: vumi.application.tests.test_rapidsms_relay -*-
import json
from base64 import b64encode

from zope.interface import implements
from twisted.internet.defer import (
    inlineCallbacks, returnValue, DeferredList, fail)
from twisted.web import http
from twisted.web.resource import Resource, IResource
from twisted.web.server import NOT_DONE_YET
from twisted.cred import portal, checkers, credentials, error
from twisted.web.guard import HTTPAuthSessionWrapper, BasicCredentialFactory

from vumi.application.base import ApplicationWorker
from vumi.persist.txredis_manager import TxRedisManager
from vumi.config import (
    ConfigUrl, ConfigText, ConfigInt, ConfigDict, ConfigBool, ConfigContext)
from vumi.message import to_json
from vumi.utils import http_request_full
from vumi.errors import ConfigError
from vumi import log


class HealthResource(Resource):
    isLeaf = True

    def render_GET(self, request):
        request.setResponseCode(http.OK)
        request.do_not_log = True
        return 'OK'


class SendResource(Resource):
    isLeaf = True

    def __init__(self, application):
        self.application = application
        Resource.__init__(self)

    def finish_request(self, request, msgs):
        request.setResponseCode(http.OK)
        request.write(to_json([msg.payload for msg in msgs]))
        request.finish()

    def render_(self, request):
        log.msg("Send request: %s" % (request,))
        request.setHeader("content-type", "application/json")
        d = self.application.handle_raw_outbound_message(request)
        d.addCallback(lambda msgs: self.finish_request(request, msgs))
        return NOT_DONE_YET

    def render_PUT(self, request):
        return self.render_(request)

    def render_GET(self, request):
        return self.render_(request)

    def render_POST(self, request):
        return self.render_(request)


class RapidSMSRelayRealm(object):
    implements(portal.IRealm)

    def __init__(self, resource):
        self.resource = resource

    def requestAvatar(self, user, mind, *interfaces):
        if IResource in interfaces:
            return (IResource, self.resource, lambda: None)
        raise NotImplementedError()


class RapidSMSRelayAccessChecker(object):
    implements(checkers.ICredentialsChecker)
    credentialInterfaces = (credentials.IUsernamePassword,
                            credentials.IAnonymous)

    def __init__(self, get_avatar_id):
        self._get_avatar_id = get_avatar_id

    def requestAvatarId(self, credentials):
        return self._get_avatar_id(credentials)


class RapidSMSRelayConfig(ApplicationWorker.CONFIG_CLASS):
    """RapidSMS relay configuration.

    A RapidSMS relay requires a `send_to` configuration section for the
    `default` send_to tag.
    """

    web_path = ConfigText(
        "Path to listen for outbound messages from RapidSMS on.",
        static=True)
    web_port = ConfigInt(
        "Port to listen for outbound messages from RapidSMS on.",
        static=True)
    redis_manager = ConfigDict(
        "Redis manager configuration (only required if"
        " `allow_replies` is true)",
        default={}, static=True)
    allow_replies = ConfigBool(
        "Whether to support replies via the `in_reply_to` argument"
        " from RapidSMS.", default=True, static=True)

    vumi_username = ConfigText(
        "Username required when calling `web_path` (default: no"
        " authentication)",
        default=None)
    vumi_password = ConfigText(
        "Password required when calling `web_path`", default=None)
    vumi_auth_method = ConfigText(
        "Authentication method required when calling `web_path`."
        "The 'basic' method is currently the only available method",
        default='basic')
    vumi_reply_timeout = ConfigInt(
        "Number of seconds to keep original messages in redis so that"
        " replies may be sent via `in_reply_to`.", default=10 * 60)

    rapidsms_url = ConfigUrl("URL of the rapidsms http backend.")
    rapidsms_username = ConfigText(
        "Username to use for the `rapidsms_url` (default: no authentication)",
        default=None)
    rapidsms_password = ConfigText(
        "Password to use for the `rapidsms_url`", default=None)
    rapidsms_auth_method = ConfigText(
        "Authentication method to use with `rapidsms_url`."
        "The 'basic' method is currently the only available method.",
        default='basic')
    rapidsms_http_method = ConfigText(
        "HTTP request method to use for the `rapidsms_url`",
        default='POST')


class RapidSMSRelay(ApplicationWorker):
    """Application that relays messages to RapidSMS."""

    CONFIG_CLASS = RapidSMSRelayConfig
    SEND_TO_TAGS = frozenset(['default'])

    def validate_config(self):
        self.supported_auth_methods = {
            'basic': self.generate_basic_auth_headers,
        }

    def generate_basic_auth_headers(self, username, password):
        credentials = ':'.join([username, password])
        auth_string = b64encode(credentials.encode('utf-8'))
        return {
            'Authorization': ['Basic %s' % (auth_string,)]
        }

    def get_auth_headers(self, config):
        auth_method, username, password = (config.rapidsms_auth_method,
                                           config.rapidsms_username,
                                           config.rapidsms_password)
        if auth_method not in self.supported_auth_methods:
            raise ConfigError('HTTP Authentication method %s'
                              ' not supported' % (repr(auth_method,)))
        if username is not None:
            handler = self.supported_auth_methods.get(auth_method)
            return handler(username, password)
        return {}

    def get_protected_resource(self, resource):
        checker = RapidSMSRelayAccessChecker(self.get_avatar_id)
        realm = RapidSMSRelayRealm(resource)
        p = portal.Portal(realm, [checker])
        factory = BasicCredentialFactory("RapidSMS Relay")
        protected_resource = HTTPAuthSessionWrapper(p, [factory])
        return protected_resource

    @inlineCallbacks
    def get_avatar_id(self, creds):
        if credentials.IAnonymous.providedBy(creds):
            config = yield self.get_config(None, ConfigContext(username=None))
            # allow anonymous authentication if no username is configured
            if config.vumi_username is None:
                returnValue(None)
        elif credentials.IUsernamePassword.providedBy(creds):
            username, password = creds.username, creds.password
            config = yield self.get_config(None,
                                           ConfigContext(username=username))
            if (username == config.vumi_username and
                password == config.vumi_password):
                returnValue(username)
        raise error.UnauthorizedLogin()

    @inlineCallbacks
    def setup_application(self):
        config = self.get_static_config()
        self.redis = None
        if config.allow_replies:
            self.redis = yield TxRedisManager.from_config(config.redis_manager)
        send_resource = self.get_protected_resource(SendResource(self))
        self.web_resource = yield self.start_web_resources(
            [
                (send_resource, config.web_path),
                (HealthResource(), 'health'),
            ],
            config.web_port)

    @inlineCallbacks
    def teardown_application(self):
        yield self.web_resource.loseConnection()
        if self.redis is not None:
            yield self.redis.close_manager()

    def handle_raw_outbound_message(self, request):
        data = json.loads(request.content.read())
        content = data['content']
        to_addrs = data['to_addr']
        sends = []
        if 'in_reply_to' in data and False: # TODO: complete
            # TODO: Add redis store.
            [to_addr] = to_addrs
            if original_message['from_addr'] == to_addr:
                sends.append(self.reply_to(original_message, content))
            else:
                sends.append(fail("Invalid to_addr for reply"))
        else:
            for to_addr in to_addrs:
                sends.append(self.send_to(to_addr, content))
        d = DeferredList(sends, consumeErrors=True)
        d.addCallback(lambda msgs: [msg[1] for msg in msgs if msg[0]])
        return d

    @inlineCallbacks
    def _call_rapidsms(self, message):
        config = yield self.get_config(message)
        headers = self.get_auth_headers(config)
        response = http_request_full(config.rapidsms_url.geturl(),
                                     message.to_json(),
                                     headers, config.rapidsms_http_method)
        response.addCallback(lambda response: log.info(response.code))
        response.addErrback(lambda failure: log.err(failure))
        yield response

    def consume_user_message(self, message):
        return self._call_rapidsms(message)

    def close_session(self, message):
        return self._call_rapidsms(message)

    def consume_ack(self, event):
        log.info("Acknowledgement received for message %r"
                 % (event['user_message_id']))

    def consume_delivery_report(self, event):
        log.info("Delivery report received for message %r, status %r"
                 % (event['user_message_id'], event['delivery_status']))