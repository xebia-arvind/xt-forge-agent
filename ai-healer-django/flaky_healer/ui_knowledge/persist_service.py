"""
In-process persistence for one UI-knowledge snapshot payload.

Extracted from `UISnapshotCreateAPI.post` so both the HTTP endpoint and
Django-side callers (see `test_generation/ui_knowledge_capture.py`) share
the same write path — no HTTP loopback, no JWT dance, one source of truth
for how a snapshot lands in the DB.

The payload shape matches what `UISnapshotSerializer` produces (see
`ui_knowledge/serializers.py`), so callers who already have validated data
can hand it in directly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .change_detection_service import compare_snapshots
from .models import (
    UIChangeLog,
    UIElement,
    UIPage,
    UIRouteSnapshot,
    UIScreenshot,
)


def persist_snapshot(client, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persist one route snapshot for `client`, returning `{status, snapshot_id, elements}`.
    Mirrors the response shape of `POST /ui-knowledge/sync/`.
    """
    # ------------------------------------------------
    # PAGE (scoped per-tenant)
    # ------------------------------------------------
    page, _ = UIPage.objects.get_or_create(
        client=client,
        route=payload["route"],
        defaults={
            "title": payload.get("title", ""),
            "feature_name": payload.get("feature_name", ""),
        },
    )
    updated_fields = []
    incoming_title = payload.get("title")
    incoming_feature_name = payload.get("feature_name")
    if incoming_title is not None and incoming_title != page.title:
        page.title = incoming_title
        updated_fields.append("title")
    if incoming_feature_name is not None and incoming_feature_name != page.feature_name:
        page.feature_name = incoming_feature_name
        updated_fields.append("feature_name")
    if updated_fields:
        page.save(update_fields=updated_fields + ["updated_on"])

    # Mark previous "current" snapshot for this page as non-current.
    page.snapshots.filter(is_current=True).update(is_current=False)

    snapshot = UIRouteSnapshot.objects.create(
        page=page,
        snapshot_type=payload["snapshot_type"],
        dom_hash=payload.get("dom_hash", ""),
        snapshot_json=payload.get("snapshot_json", {}),
        is_current=True,
        version=page.snapshots.count() + 1,
    )

    # Change detection: only meaningful for non-BASELINE writes.
    if payload["snapshot_type"] != "BASELINE":
        baseline = (
            page.snapshots.filter(snapshot_type="BASELINE")
            .exclude(id=snapshot.id)
            .first()
        )
        if baseline:
            diff = compare_snapshots(baseline, snapshot)
            UIChangeLog.objects.create(
                page=page,
                baseline_snapshot=baseline,
                new_snapshot=snapshot,
                change_type=diff.get("change_type") or "MINOR",
                added_selectors=diff.get("added_selectors") or [],
                removed_selectors=diff.get("removed_selectors") or [],
            )

    if payload.get("screenshot_path"):
        UIScreenshot.objects.create(
            snapshot=snapshot,
            image_path=payload["screenshot_path"],
        )

    rows = [
        UIElement(
            snapshot=snapshot,
            selector=el["selector"],
            tag=el.get("tag", ""),
            role=el.get("role", ""),
            text=el.get("text", ""),
            test_id=el.get("test_id", ""),
            intent_key=el.get("intent_key", "generic"),
        )
        for el in payload.get("elements", [])
    ]
    UIElement.objects.bulk_create(rows)

    return {
        "status": "stored",
        "snapshot_id": snapshot.id,
        "elements": len(rows),
    }
