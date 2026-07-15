from django.shortcuts import render

# Create your views here.
# ui_knowledge/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.permissions import IsAuthenticated
from .models import *
from .serializers import UISnapshotSerializer
from .change_detection_service import compare_snapshots, detect_ui_change_for_healing
from .persist_service import persist_snapshot
from clients.mixins import require_client
from urllib.parse import urlparse


class _ClientScopedAPIView(APIView):
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


def _normalize_route(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return parsed.path or "/"
    except Exception:
        pass
    if raw.startswith("/"):
        return raw
    return f"/{raw}"


class UISnapshotCreateAPI(_ClientScopedAPIView):

    def post(self, request):
        client = require_client(request)

        serializer = UISnapshotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Persist via shared service so Django-side callers (Artifact-stage
        # auto-capture) go through the exact same write path as the HTTP API.
        result = persist_snapshot(client, dict(serializer.validated_data))
        return Response(result)


class UIChangeStatusAPIView(_ClientScopedAPIView):
    """
    GET /ui-knowledge/change-status/?route=/cart
    Optional:
      - failed_selector
      - use_of_selector
    """

    def get(self, request):
        client = require_client(request)
        route = str(request.query_params.get("route") or "").strip()
        if not route:
            return Response({"error": "Missing required query param: route"}, status=400)

        failed_selector = str(request.query_params.get("failed_selector") or "")
        use_of_selector = str(request.query_params.get("use_of_selector") or "")

        route_path = _normalize_route(route)
        # All page lookups are tenant-scoped.
        scoped = UIPage.objects.filter(client=client, is_active=True)
        page = scoped.filter(route=route).first()
        if not page:
            page = scoped.filter(route=route_path).first()
        if not page:
            page = scoped.filter(route__endswith=route_path).order_by("-updated_on").first()

        if not page:
            detection = detect_ui_change_for_healing(
                page_url=route,
                failed_selector=failed_selector,
                use_of_selector=use_of_selector,
                client=client,
            )
            return Response(
                {
                    "status": "not_found",
                    "route": route,
                    "detection": detection,
                },
                status=404,
            )

        baseline = page.snapshots.filter(snapshot_type="BASELINE").order_by("-version", "-created_on").first()
        current = page.snapshots.filter(is_current=True).order_by("-version", "-created_on").first()
        if not current:
            current = page.snapshots.order_by("-version", "-created_on").first()

        detection = detect_ui_change_for_healing(
            page_url=page.route,
            failed_selector=failed_selector,
            use_of_selector=use_of_selector,
            client=client,
        )

        diff = None
        if baseline and current and baseline.id != current.id:
            diff = compare_snapshots(baseline, current)

        latest_log = (
            UIChangeLog.objects.filter(page=page)
            .order_by("-created_on")
            .first()
        )

        return Response(
            {
                "status": "ok",
                "route": page.route,
                "page_id": page.id,
                "baseline_snapshot_id": baseline.id if baseline else None,
                "current_snapshot_id": current.id if current else None,
                "latest_change_log": {
                    "id": latest_log.id,
                    "change_type": latest_log.change_type,
                    "created_on": latest_log.created_on,
                    "added_selectors_count": len(latest_log.added_selectors or []),
                    "removed_selectors_count": len(latest_log.removed_selectors or []),
                } if latest_log else None,
                "detection": detection,
                "computed_diff": diff,
            }
        )
