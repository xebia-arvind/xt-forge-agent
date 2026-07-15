"""
Dashboard-only view: switch the browser user's active tenant.

POST /test-analytics/pick-client/
    client_id=<uuid>   — must be in the caller's UserClient.clients
    next=<path>        — optional; validated to be a local path

Writes `request.session["active_client_id"]` and redirects to `next` (default
`/test-analytics/worklist/`). If the user submits a client they don't belong to,
returns 403 without touching the session.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views import View

from .middleware import _user_client_secret_keys


@method_decorator(login_required(login_url="test_analytics_login"), name="dispatch")
@method_decorator(require_POST, name="dispatch")
class PickClientView(View):
    def post(self, request):
        client_id = (request.POST.get("client_id") or "").strip()
        allowed = {str(k) for k in _user_client_secret_keys(request.user)}
        if client_id not in allowed:
            return HttpResponseForbidden("You are not a member of this client.")

        request.session["active_client_id"] = client_id

        raw_next = request.POST.get("next") or ""
        if raw_next and url_has_allowed_host_and_scheme(raw_next, allowed_hosts={request.get_host()}):
            return redirect(raw_next)
        return redirect(reverse("panel_worklist"))
