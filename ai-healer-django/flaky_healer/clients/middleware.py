"""
Multi-tenant client resolution.

`ClientResolutionMiddleware` attaches `request.client` (a Clients instance or
None) after DRF authentication has run. Resolution order:

    1. JWT — `request.auth["client_id"]` (used by wraper-healer & other
       machine callers).
    2. Session — `request.session["active_client_id"]` (used by browser users
       hitting the dashboard). The session value is validated against the
       logged-in user's `UserClient.clients` on every request; a stale/invalid
       value is silently dropped.
    3. Auto-pick — if the browser user is authenticated, has no session choice,
       and is assigned to exactly one client, the middleware selects it and
       caches the choice in the session. Single-tenant users never see the picker.

The middleware never raises. Views enforce policy: DRF views use
`clients.mixins.require_client()`; dashboard views (via `_PanelView`) render a
picker screen when `request.client is None`.
"""
from typing import Optional

from django.utils.deprecation import MiddlewareMixin


class ClientResolutionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.client = None
        return None

    def process_view(self, request, view_func, view_args, view_kwargs):
        client = _resolve_client(request)
        if client is not None:
            request.client = client
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _user_client_secret_keys(user):
    """Set of Clients.secret_key values the logged-in user may access."""
    if not user or not user.is_authenticated:
        return set()
    try:
        user_client = getattr(user, "user_client", None)
        if user_client is None:
            return set()
        return set(user_client.clients.values_list("secret_key", flat=True))
    except Exception:
        return set()


def _resolve_client(request) -> Optional[object]:
    from clients.models import Clients

    # (1) JWT path.
    # Django middleware's process_view fires BEFORE DRF's view.dispatch(), so
    # `request.auth` isn't populated by DRF yet. If the caller sent an
    # Authorization: Bearer header we authenticate here directly so we can
    # read the `client_id` claim before the view runs. Session-authenticated
    # browser requests fall through to path (2).
    auth = getattr(request, "auth", None)
    if auth is None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if header.lower().startswith("bearer "):
            try:
                from rest_framework_simplejwt.authentication import JWTAuthentication
                jwt_auth = JWTAuthentication()
                result = jwt_auth.authenticate(request)
                if result is not None:
                    user, token = result
                    # Stash on the request so DRF's downstream auth pass sees it
                    # and doesn't repeat work / doesn't re-raise on invalid tokens.
                    request.user = user
                    request.auth = token
                    auth = token
            except Exception:
                # Invalid/expired token — let DRF surface the proper 401 later.
                auth = None

    if auth is not None:
        try:
            claim = auth.get("client_id") if hasattr(auth, "get") else None
        except Exception:
            claim = None
        if claim:
            try:
                return Clients.objects.filter(secret_key=claim).first()
            except Exception:
                return None

    # (2)/(3) Session path — browser users.
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None
    session = getattr(request, "session", None)
    if session is None:
        return None

    allowed = _user_client_secret_keys(user)
    if not allowed:
        return None

    session_key = session.get("active_client_id")
    if session_key:
        try:
            # Validate against the current membership set every request; an admin
            # can revoke a client at any time and we must react immediately.
            if str(session_key) not in {str(k) for k in allowed}:
                del session["active_client_id"]
            else:
                return Clients.objects.filter(secret_key=session_key).first()
        except Exception:
            pass

    # (3) Auto-pick when the user has exactly one assigned client.
    if len(allowed) == 1:
        only_key = next(iter(allowed))
        try:
            session["active_client_id"] = str(only_key)
        except Exception:
            pass
        return Clients.objects.filter(secret_key=only_key).first()

    return None


def resolve_client_from_request(request):
    """External helper for views that need to (re-)resolve mid-request."""
    return _resolve_client(request)
