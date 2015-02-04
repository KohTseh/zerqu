# coding: utf-8

from functools import wraps
from flask import Blueprint
from flask import jsonify
from flask import request, json, session
from werkzeug.exceptions import HTTPException
from werkzeug._compat import text_type
from oauthlib.common import to_unicode
from flask_oauthlib.utils import decode_base64
from ..models import AuthSession, OAuthClient
from ..models.auth import oauth
from ..libs.ratelimit import ratelimit
from ..versions import VERSION, API_VERSION

bp = Blueprint('api_base', __name__)


class APIException(HTTPException):
    error_code = 'invalid_request'

    def __init__(self, code=None, error=None, description=None, response=None):
        if code is not None:
            self.code = code
        if error is not None:
            self.error_code = error
        super(APIException, self).__init__(description, response)

    def get_body(self, environ=None):
        return text_type(json.dumps(dict(
            status='error',
            error_code=self.error_code,
            error_description=self.description,
        )))

    def get_headers(self, environ=None):
        return [('Content-Type', 'application/json')]


def generate_limit_params(login, scopes):
    user = AuthSession.get_current_user()
    if user:
        request._current_user = user
        return 'limit:sid:%s' % session.get('id'), 600, 300

    valid, req = oauth.verify_request(scopes)
    if login and not valid:
        raise APIException(
            401,
            'authorization_required',
            'Authorization is required'
        )

    if valid:
        request._current_user = req.user
        # TODO
        key = 'limit:%s-%d' % (req.client_id, req.user.id)
        return key, 600, 600

    client_id = request.values.get('client_id')
    if client_id:
        c = OAuthClient.query.filter_by(
            client_id=client_id
        ).first()
        if not c:
            raise APIException(
                400,
                'invalid_client',
                'Client not found on client_id',
            )
        return 'limit:%d' % c.id
    return 'limit:ip:%s' % request.remote_addr, 3600, 3600


def require_oauth(login=True, *scopes):
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            prefix, count, duration = generate_limit_params(login, scopes)
            remaining, expires = ratelimit(prefix, count, duration)
            if remaining <= 0 and expires:
                raise APIException(
                    code=429,
                    error='limit_exceeded',
                    description=(
                        'Rate limit exceeded, retry in %is'
                    ) % expires
                )
            request._rate_remaining = remaining
            request._rate_expires = expires
            return f(*args, **kwargs)
        return decorated
    return wrapper


def require_confidential(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', None)
        error = APIException(
            code=403,
            error='confidential_only',
            description='Only confidential clients are allowed'
        )
        if not auth:
            raise error
        try:
            _, s = auth.split(' ')
            client_id, client_secret = decode_base64(s).split(':')
            client_id = to_unicode(client_id, 'utf-8')
            client_secret = to_unicode(client_secret, 'utf-8')
        except:
            raise error
        client = oauth._clientgetter(client_id)
        if not client or client.client_secret != client_secret:
            raise error
        if not client.is_confidential:
            raise error
        return f(*args, **kwargs)
    return decorated


def ratelimit_hook(response):
    if hasattr(request, '_rate_remaining'):
        response.headers['X-Rate-Limit'] = str(request._rate_remaining)
    if hasattr(request, '_rate_expires'):
        response.headers['X-Rate-Expires'] = str(request._rate_expires)

    # javascript can request API
    response.headers['Access-Control-Allow-Origin'] = '*'
    # api not available in iframe
    response.headers['X-Frame-Options'] = 'deny'
    # security protection
    response.headers['Content-Security-Policy'] = "default-src 'none'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.route('/')
def index():
    return jsonify(status='ok', data=dict(
        system='zerqu',
        version=VERSION,
        api_version=API_VERSION,
    ))
