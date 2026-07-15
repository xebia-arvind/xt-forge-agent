import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .models import UIPage, UIRouteSnapshot

_AGENT_IMPORTED = False


def _agent_root() -> Path:
    return Path(__file__).resolve().parent / "UI_chnage_detaction-Agent"


def _ensure_agent_imports() -> None:
    global _AGENT_IMPORTED
    if _AGENT_IMPORTED:
        return
    root = _agent_root()
    if root.exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
    _AGENT_IMPORTED = True


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


def _selector_hints(failed_selector: str, use_of_selector: str) -> List[str]:
    hints: List[str] = []
    sel = (failed_selector or "").strip().lower()
    use_text = (use_of_selector or "").strip().lower()

    text_match = re.search(r'has-text\("([^"]+)"\)', sel)
    if text_match:
        hints.append(text_match.group(1).strip())
    text_match_single = re.search(r"has-text\('([^']+)'\)", sel)
    if text_match_single:
        hints.append(text_match_single.group(1).strip())

    id_match = re.search(r"#([a-zA-Z0-9_-]+)", sel)
    if id_match:
        hints.append(id_match.group(1).strip().lower())

    testid_match = re.search(r'data-testid\s*=\s*["\']([^"\']+)["\']', sel)
    if testid_match:
        hints.append(testid_match.group(1).strip().lower())

    attr_literal_matches = re.findall(
        r'(?:id|name|role|aria-label|data-testid)\s*=\s*["\']([^"\']+)["\']',
        sel,
    )
    for val in attr_literal_matches[:3]:
        hints.append(val.strip().lower())

    generic_words = [
        token
        for token in re.split(r"[^a-z0-9]+", use_text)
        if token and len(token) >= 4 and token not in {"click", "button", "first", "with", "self", "healing"}
    ]
    hints.extend(generic_words[:4])

    normalized: List[str] = []
    seen = set()
    for h in hints:
        v = " ".join(str(h).split()).strip().lower()
        if len(v) < 3 or v in seen:
            continue
        normalized.append(v)
        seen.add(v)
    return normalized[:6]


def _extract_selector_set(snapshot: UIRouteSnapshot) -> set[str]:
    selectors = set()
    for s in snapshot.elements.values_list("selector", flat=True):
        candidate = str(s or "").strip().lower()
        if candidate:
            selectors.add(candidate)
    return selectors


def _extract_layout_from_snapshot(snapshot: UIRouteSnapshot) -> List[Dict[str, Any]]:
    payload = snapshot.snapshot_json if isinstance(snapshot.snapshot_json, dict) else {}
    interactables = payload.get("interactables") if isinstance(payload.get("interactables"), list) else []
    rows: List[Dict[str, Any]] = []
    for item in interactables[:1500]:
        if not isinstance(item, dict):
            continue
        layout = item.get("layout") if isinstance(item.get("layout"), dict) else {}
        width = int(layout.get("width") or 0)
        height = int(layout.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        rows.append(
            {
                "tag": str(item.get("tag") or "").upper() or "DIV",
                "id": str(item.get("id") or ""),
                "class": "",
                "text": str(item.get("text") or "")[:80],
                "x": int(layout.get("x") or 0),
                "y": int(layout.get("y") or 0),
                "width": width,
                "height": height,
            }
        )
    return rows


def _resolve_page(page_url: str, client=None) -> Optional[UIPage]:
    exact = str(page_url or "").strip()
    route_path = _normalize_route(exact)

    # Tenant scope: when `client` is provided (multi-tenant path) only that client's
    # routes are considered. The healer view always passes its `client`; legacy
    # callers that pass None still resolve globally (used during migration window).
    qs = UIPage.objects.filter(is_active=True)
    if client is not None:
        qs = qs.filter(client=client)

    if exact:
        page = qs.filter(route=exact).first()
        if page:
            return page
    if route_path:
        page = qs.filter(route=route_path).first()
        if page:
            return page
        page = qs.filter(route__endswith=route_path).order_by("-updated_on").first()
        if page:
            return page
    return None


def _resolve_baseline_and_current(page: UIPage) -> Tuple[Optional[UIRouteSnapshot], Optional[UIRouteSnapshot]]:
    baseline = (
        page.snapshots.filter(snapshot_type="BASELINE")
        .order_by("-version", "-created_on")
        .first()
    )
    current = (
        page.snapshots.filter(is_current=True)
        .order_by("-version", "-created_on")
        .first()
    )
    if not current:
        current = page.snapshots.order_by("-version", "-created_on").first()
    return baseline, current


def compare_snapshots(
    baseline_snapshot: UIRouteSnapshot,
    new_snapshot: UIRouteSnapshot,
) -> Dict[str, Any]:
    baseline_selectors = _extract_selector_set(baseline_snapshot)
    new_selectors = _extract_selector_set(new_snapshot)
    added = sorted(list(new_selectors - baseline_selectors))[:500]
    removed = sorted(list(baseline_selectors - new_selectors))[:500]

    layout_diff: Dict[str, Any] = {
        "position_shifts": [],
        "size_changes": [],
        "missing_elements": [],
        "new_elements": [],
        "table_issues": [],
    }
    severity = {"severity": "LOW", "reasons": []}

    try:
        _ensure_agent_imports()
        from ignore_rules.filter import load_rules  # type: ignore
        from layout_diff.diff_engine import diff_layout  # type: ignore
        from severity.severity_engine import calculate_severity  # type: ignore

        rules = load_rules()
        base_layout = _extract_layout_from_snapshot(baseline_snapshot)
        curr_layout = _extract_layout_from_snapshot(new_snapshot)
        if base_layout and curr_layout:
            layout_diff = diff_layout(base_layout, curr_layout, rules)
            # Keep visual signal optional here; service remains deterministic from stored snapshot data.
            severity = calculate_severity(layout_diff, {"visual_change": False, "pixel_change_ratio": 0, "ssim_score": 1})
        else:
            if baseline_snapshot.dom_hash == new_snapshot.dom_hash:
                severity = {"severity": "LOW", "reasons": ["DOM hash unchanged"]}
            else:
                severity = {"severity": "MEDIUM", "reasons": ["DOM hash changed; layout data unavailable"]}
    except Exception:
        if baseline_snapshot.dom_hash == new_snapshot.dom_hash:
            severity = {"severity": "LOW", "reasons": ["DOM hash unchanged"]}
        else:
            severity = {"severity": "MEDIUM", "reasons": ["DOM hash changed"]}

    if not added and not removed and baseline_snapshot.dom_hash == new_snapshot.dom_hash:
        change_type = "NO_CHANGE"
    elif severity.get("severity") == "HIGH" or removed:
        change_type = "STRUCTURAL"
    else:
        change_type = "MINOR"

    return {
        "change_type": change_type,
        "added_selectors": added,
        "removed_selectors": removed,
        "layout_diff": layout_diff,
        "severity": severity,
    }


def detect_ui_change_for_healing(
    *,
    page_url: str,
    failed_selector: str = "",
    use_of_selector: str = "",
    client=None,
) -> Dict[str, Any]:
    page = _resolve_page(page_url, client=client)
    if not page:
        return {"ui_change_level": "UNKNOWN", "source": "ui_knowledge", "reason": "page_not_found"}

    baseline, current = _resolve_baseline_and_current(page)
    if not baseline or not current:
        return {"ui_change_level": "UNKNOWN", "source": "ui_knowledge", "reason": "snapshot_missing"}

    if baseline.id == current.id:
        return {
            "ui_change_level": "UNCHANGED",
            "source": "ui_knowledge",
            "change_type": "NO_CHANGE",
            "added_selectors": [],
            "removed_selectors": [],
            "baseline_snapshot_id": baseline.id,
            "current_snapshot_id": current.id,
        }

    diff = compare_snapshots(baseline, current)

    hints = _selector_hints(failed_selector, use_of_selector)
    baseline_selectors = _extract_selector_set(baseline)
    current_selectors = _extract_selector_set(current)
    had_before = any(any(h in s for s in baseline_selectors) for h in hints) if hints else False
    exists_now = any(any(h in s for s in current_selectors) for h in hints) if hints else False

    if hints and had_before and not exists_now:
        level = "ELEMENT_REMOVED"
    else:
        sev = str((diff.get("severity") or {}).get("severity") or "LOW").upper()
        if sev == "HIGH":
            level = "MAJOR_CHANGE"
        elif sev == "MEDIUM":
            level = "MINOR_CHANGE"
        else:
            level = "UNCHANGED"

    return {
        "ui_change_level": level,
        "source": "ui_knowledge",
        "change_type": diff.get("change_type"),
        "added_selectors": diff.get("added_selectors") or [],
        "removed_selectors": diff.get("removed_selectors") or [],
        "baseline_snapshot_id": baseline.id,
        "current_snapshot_id": current.id,
        "severity": (diff.get("severity") or {}).get("severity"),
        "severity_reasons": (diff.get("severity") or {}).get("reasons") or [],
    }
