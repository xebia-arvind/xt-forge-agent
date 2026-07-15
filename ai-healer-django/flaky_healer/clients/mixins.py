"""
DRF mixins / helpers for multi-tenant query scoping.

`ClientScopedAPIView` and `ClientScopedQuerysetMixin` filter results to the
caller's client (resolved by `ClientResolutionMiddleware`) and ensure new rows
are stamped with the client on save.

Both helpers fail-closed: if `request.client` is None, GETs return 401 and writes
raise PermissionDenied. Anonymous endpoints (e.g. /auth/login/) must NOT inherit
these.
"""
from typing import Optional

from rest_framework.exceptions import PermissionDenied, NotAuthenticated


def require_client(request):
    """
    Returns `request.client` or raises 401. Use at the top of view methods that
    do not subclass ClientScopedAPIView (e.g. function-based views, custom mixins).
    """
    client = getattr(request, "client", None)
    if client is None:
        raise NotAuthenticated("Client could not be resolved from authentication token.")
    return client


class ClientScopedQuerysetMixin:
    """
    Mixin for DRF generic views / viewsets.

    The mixin auto-filters `get_queryset()` to `client=request.client` using the
    field declared in `client_field` (default: "client").

    For nested resources (e.g. records FK'd to a parent that owns the client),
    set `client_field = "<parent>__client"`.
    """

    client_field: str = "client"

    def get_queryset(self):
        qs = super().get_queryset()
        client = require_client(self.request)
        return qs.filter(**{self.client_field: client})

    def perform_create(self, serializer):
        client = require_client(self.request)
        serializer.save(client=client)
