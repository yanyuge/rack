# Copyright (c) 2014 ITOCHU Techno-Solutions Corporation.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
"""
Common Auth Middleware.

"""

from oslo.config import cfg
from rack.api import wsgi
from rack import context
from rack.openstack.common.gettextutils import _
from rack.openstack.common import jsonutils
from rack.openstack.common import log as logging
from rack import wsgi as base_wsgi
import webob.dec
import webob.exc


auth_opts = [
    cfg.BoolOpt('api_rate_limit',
                default=False,
                help=('Whether to use per-user rate limiting for the api. ')),
    cfg.StrOpt('auth_strategy',
               default='noauth',
               help='The strategy to use for auth: noauth or keystone.'),
    cfg.BoolOpt('use_forwarded_for',
                default=False,
                help='Treat X-Forwarded-For as the canonical remote address. '
                     'Only enable this if you have a sanitizing proxy.'),
]

CONF = cfg.CONF
CONF.register_opts(auth_opts)

LOG = logging.getLogger(__name__)


def _load_pipeline(loader, pipeline):
    filters = [loader.get_filter(n) for n in pipeline[:-1]]
    app = loader.get_app(pipeline[-1])
    filters.reverse()
    for filter in filters:
        app = filter(app)
    return app


def pipeline_factory(loader, global_conf, **local_conf):
    """A paste pipeline replica that keys off of auth_strategy."""
    pipeline = local_conf[CONF.auth_strategy]
    if not CONF.api_rate_limit:
        limit_name = CONF.auth_strategy + '_nolimit'
        pipeline = local_conf.get(limit_name, pipeline)
    pipeline = pipeline.split()
    return _load_pipeline(loader, pipeline)


class InjectContext(base_wsgi.Middleware):

    """Add a 'rack.context' to WSGI environ."""

    def __init__(self, context, *args, **kwargs):
        self.context = context
        super(InjectContext, self).__init__(*args, **kwargs)

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        req.environ['rack.context'] = self.context
        return self.application


class RackKeystoneContext(base_wsgi.Middleware):

    """Make a request context from keystone headers."""

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        user_id = req.headers.get('X_USER')
        user_id = req.headers.get('X_USER_ID', user_id)
        if user_id is None:
            LOG.debug("Neither X_USER_ID nor X_USER found in request")
            return webob.exc.HTTPUnauthorized()

        roles = self._get_roles(req)

        if 'X_TENANT_ID' in req.headers:
            # This is the new header since Keystone went to ID/Name
            project_id = req.headers['X_TENANT_ID']
        else:
            # This is for legacy compatibility
            project_id = req.headers['X_TENANT']
        project_name = req.headers.get('X_TENANT_NAME')
        user_name = req.headers.get('X_USER_NAME')

        # Get the auth token
        auth_token = req.headers.get('X_AUTH_TOKEN',
                                     req.headers.get('X_STORAGE_TOKEN'))

        # Build a context, including the auth_token...
        remote_address = req.remote_addr
        if CONF.use_forwarded_for:
            remote_address = req.headers.get('X-Forwarded-For', remote_address)

        service_catalog = None
        if req.headers.get('X_SERVICE_CATALOG') is not None:
            try:
                catalog_header = req.headers.get('X_SERVICE_CATALOG')
                service_catalog = jsonutils.loads(catalog_header)
            except ValueError:
                raise webob.exc.HTTPInternalServerError(
                    _('Invalid service catalog json.'))

        ctx = context.RequestContext(user_id,
                                     project_id,
                                     user_name=user_name,
                                     project_name=project_name,
                                     roles=roles,
                                     auth_token=auth_token,
                                     remote_address=remote_address,
                                     service_catalog=service_catalog)

        req.environ['rack.context'] = ctx
        return self.application

    def _get_roles(self, req):
        """Get the list of roles."""

        if 'X_ROLES' in req.headers:
            roles = req.headers.get('X_ROLES', '')
        else:
            # Fallback to deprecated role header:
            roles = req.headers.get('X_ROLE', '')
            if roles:
                LOG.warn(_("Sourcing roles from deprecated X-Role HTTP "
                           "header"))
        return [r.strip() for r in roles.split(',')]


class NoAuthMiddlewareBase(base_wsgi.Middleware):

    def base_call(self, req, project_id_in_path):
        remote_address = getattr(req, 'remote_address', '127.0.0.1')
        ctx = context.RequestContext(user_id='noauth',
                                     project_id='noauth',
                                     is_admin=True,
                                     remote_address=remote_address)

        req.environ['rack.context'] = ctx
        return self.application


class NoAuthMiddleware(NoAuthMiddlewareBase):

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        return self.base_call(req, True)
