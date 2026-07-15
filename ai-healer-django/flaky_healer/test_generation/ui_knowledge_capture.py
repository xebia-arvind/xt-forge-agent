"""
Auto-capture ui_knowledge snapshots before the Artifact stage runs.

Why
---
The artifact generator hallucinates selectors when its ground truth
(`_run_ui_knowledge_context`) is empty. This module makes sure the
generator always has fresh, real DOM to reason from — for the current
job's `base_url + seed_urls`, if we don't have a snapshot yet OR the
existing one is older than `TEST_GEN_UI_KNOWLEDGE_MAX_AGE_DAYS`, we
crawl the URLs (via the existing `wraper-healer/crawlContext.mjs`) and
persist the result through `ui_knowledge.persist_service.persist_snapshot`.

No HTTP loopback, no JWT dance — this is called from within the same
Django process that owns the DB, so we persist directly through the
service layer that the `/ui-knowledge/sync/` endpoint also uses.

Kill-switch: `TEST_GEN_UI_KNOWLEDGE_AUTO_CAPTURE` env var. Default "on".
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Env knobs (with defaults). Explicit gettattrs so tests can override.
# -----------------------------------------------------------------------------
def _auto_capture_enabled() -> bool:
    raw = str(getattr(settings, "TEST_GEN_UI_KNOWLEDGE_AUTO_CAPTURE", "on")).lower()
    return raw in ("on", "1", "true", "yes")


def _max_age_days() -> int:
    try:
        return int(getattr(settings, "TEST_GEN_UI_KNOWLEDGE_MAX_AGE_DAYS", 7))
    except (TypeError, ValueError):
        return 7


def _crawl_timeout_seconds() -> int:
    try:
        return int(getattr(settings, "TEST_GEN_UI_KNOWLEDGE_CRAWL_TIMEOUT", 240))
    except (TypeError, ValueError):
        return 240


# -----------------------------------------------------------------------------
# URL helpers
# -----------------------------------------------------------------------------
def _normalize_route(base_url: str, seed_url: str) -> Optional[str]:
    """
    Convert a seed URL (which may be absolute or relative) into the `route`
    string we store on `UIPage.route`. We store the path only — same
    convention `UISnapshotCreateAPI` uses today.
    """
    raw = (seed_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    return raw if raw.startswith("/") else f"/{raw}"


def _absolute_url(base_url: str, seed_url: str) -> str:
    """
    Resolve `seed_url` against `base_url` — used to actually open the page.

    An absolute-path seed (starts with `/`) is resolved against the base's
    ORIGIN (scheme + host), not its full path. This avoids the classic
    `urljoin` doubling when the base already contains the same locale
    prefix as the seed:

        base = "https://staging.pulze.com/it-IT"
        seed = "/it-IT/account/login"
        urljoin(base + "/", seed.lstrip("/")) → ".../it-IT/it-IT/account/login"  ← WRONG
        this function                          → ".../it-IT/account/login"        ← right

    A relative seed (no leading slash) is resolved against the full base
    path — the traditional urljoin behavior — because that's what "relative
    to the current page" means.
    """
    raw = (seed_url or "").strip()
    if not raw:
        return base_url or ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return raw
    if not base_url:
        return raw
    base_parsed = urlparse(base_url)
    if raw.startswith("/") and base_parsed.scheme and base_parsed.netloc:
        origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
        return origin + raw
    return urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))


# -----------------------------------------------------------------------------
# Freshness check
# -----------------------------------------------------------------------------
def _needs_capture(client, route: str) -> bool:
    """
    Returns True when there's no BASELINE snapshot for (client, route) yet,
    OR the newest BASELINE is older than TEST_GEN_UI_KNOWLEDGE_MAX_AGE_DAYS.
    """
    from ui_knowledge.models import UIPage, UIRouteSnapshot

    page = UIPage.objects.filter(client=client, route=route, is_active=True).first()
    if page is None:
        return True

    latest = (
        UIRouteSnapshot.objects.filter(page=page, snapshot_type="BASELINE")
        .order_by("-created_on")
        .first()
    )
    if latest is None:
        return True

    cutoff = timezone.now() - timedelta(days=_max_age_days())
    return latest.created_on < cutoff


# -----------------------------------------------------------------------------
# Subprocess crawler
# -----------------------------------------------------------------------------
def _run_crawler(
    *,
    base_url: str,
    absolute_seed_urls: List[str],
    http_basic: Optional[Dict[str, str]] = None,
    max_routes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Invoke `wraper-healer/crawlContext.mjs` for the given seeds. Returns
    the parsed JSON `{routes: [...], warnings: [...]}` payload.

    HTTP Basic Auth (if provided) is forwarded via env — `crawlContext.mjs`
    reads `HTTP_BASIC_USERNAME` / `HTTP_BASIC_PASSWORD` and passes them to
    Playwright's `newContext({httpCredentials})`.
    """
    import os

    script = os.path.join(settings.REPO_ROOT, "wraper-healer", "crawlContext.mjs")
    if not os.path.exists(script):
        raise FileNotFoundError(f"crawl script not found: {script}")

    max_routes_val = str(max_routes if max_routes is not None else len(absolute_seed_urls) or 1)

    argv = [
        "node", script,
        "--base-url",         base_url,
        "--seed-urls",        json.dumps(absolute_seed_urls),
        "--max-routes",       max_routes_val,
        "--max-depth",        "0",   # seed URLs only — the artifact stage tells us what to cover
        "--max-interactables", "200",
    ]

    env = os.environ.copy()
    if http_basic and http_basic.get("username") and http_basic.get("password"):
        env["HTTP_BASIC_USERNAME"] = http_basic["username"]
        env["HTTP_BASIC_PASSWORD"] = http_basic["password"]

    logger.info("ui_knowledge_capture: running crawler for %d seed(s) under %s", len(absolute_seed_urls), base_url)
    proc = subprocess.run(
        argv,
        cwd=settings.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_crawl_timeout_seconds(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"crawlContext.mjs exit {proc.returncode}: "
            f"{(proc.stderr or '')[:800]}"
        )

    stdout = (proc.stdout or "").strip()
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError("crawlContext.mjs produced no JSON on stdout")
    try:
        return json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"crawlContext.mjs stdout not JSON: {exc}") from exc


# -----------------------------------------------------------------------------
# Payload shaping — mirror what wraper-healer/syncUiKnowledge.mjs does
# -----------------------------------------------------------------------------
def _shape_snapshot_payload(
    *,
    client_slug: str,
    route: str,
    crawl_route: Dict[str, Any],
    feature_name_hint: str,
) -> Dict[str, Any]:
    """
    Convert one crawler-route entry into the payload shape
    `persist_snapshot` expects (same shape as `UISnapshotSerializer` produces).
    """
    interactables = list(crawl_route.get("interactables") or [])[:200]
    elements: List[Dict[str, Any]] = []
    for el in interactables:
        # Pick the strongest selector hint the crawler produced. Order matches
        # syncUiKnowledge.mjs so historical rows line up.
        selector = ""
        hints = el.get("selector_hints") or []
        if hints:
            selector = str(hints[0])
        elif el.get("test_id"):
            selector = f'[data-testid="{el["test_id"]}"]'
        elif el.get("id"):
            selector = f'#{el["id"]}'
        elif el.get("aria_label"):
            tag = el.get("tag") or "button"
            selector = f'{tag}[aria-label="{el["aria_label"]}"]'
        elif el.get("text"):
            tag = el.get("tag") or "button"
            selector = f'{tag}:has-text("{str(el["text"])[:40]}")'

        if not selector:
            continue

        # UIElement column limits (see ui_knowledge/models.py). Truncate
        # here so a portal with unusually long test-ids or roles can't
        # blow up a bulk_create. We don't lose signal — the raw text is
        # still in `snapshot_json`, this is just what's queryable.
        elements.append({
            "selector":   selector,                                                    # TextField
            "tag":        str(el.get("tag") or "")[:30],                              # CharField(30)
            "role":       str(el.get("role") or "")[:50],                             # CharField(50)
            "text":       str(el.get("text") or ""),                                  # TextField
            "test_id":    str(el.get("test_id") or "")[:100],                         # CharField(100)
            "intent_key": "generic",                                                   # CharField(100)
        })

    return {
        "route":           route,
        "title":           str(crawl_route.get("title") or ""),
        "feature_name":    feature_name_hint,
        "snapshot_type":   "BASELINE",
        "dom_hash":        str(crawl_route.get("dom_hash") or ""),
        "screenshot_path": str(crawl_route.get("screenshot_path") or ""),
        "snapshot_json":   crawl_route,
        "elements":        elements,
    }


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def ensure_snapshots_fresh(job) -> Dict[str, Any]:
    """
    Before the Artifact stage runs, make sure a fresh BASELINE snapshot
    exists in ui_knowledge for every seed URL on `job`.

    Returns a small report the caller can attach to the stage diagnostic:
      {
        "enabled":       bool,
        "checked_routes": [...],           # per-route status
        "captured":      int,              # how many routes got a fresh snapshot
        "skipped":       int,              # already fresh
        "failed":        int,              # crawler/DB failure per route
      }
    Never raises — every failure mode collapses into a `failed` count so
    the Artifact stage still runs even if capture is broken.
    """
    from ui_knowledge.persist_service import persist_snapshot

    report: Dict[str, Any] = {
        "enabled":        _auto_capture_enabled(),
        "checked_routes": [],
        "captured":       0,
        "skipped":        0,
        "failed":         0,
    }
    if not report["enabled"]:
        return report

    client = getattr(job, "client", None)
    if client is None:
        report["failed"] += 1
        report["checked_routes"].append({"route": "-", "status": "no_client_on_job"})
        return report

    base_url = str(job.base_url or "").strip()
    seed_urls: List[str] = list(job.seed_urls or [])
    if base_url and not seed_urls:
        seed_urls = ["/"]
    if not seed_urls:
        return report

    # Compose the freshness plan.
    routes_needing_capture: List[Dict[str, str]] = []   # [{route, absolute_url}]
    for seed in seed_urls:
        route = _normalize_route(base_url, seed)
        if not route:
            continue
        absolute = _absolute_url(base_url, seed)
        if _needs_capture(client, route):
            routes_needing_capture.append({"route": route, "absolute": absolute})
            report["checked_routes"].append({"route": route, "status": "needs_capture"})
        else:
            report["skipped"] += 1
            report["checked_routes"].append({"route": route, "status": "fresh"})

    if not routes_needing_capture:
        return report

    # Pull HTTP Basic Auth from preconditions if the story provided it.
    pre = dict(getattr(job, "preconditions", None) or {})
    http_basic = pre.get("http_basic") if isinstance(pre.get("http_basic"), dict) else None

    absolute_seeds = [r["absolute"] for r in routes_needing_capture]
    try:
        crawl = _run_crawler(
            base_url=base_url or absolute_seeds[0],
            absolute_seed_urls=absolute_seeds,
            http_basic=http_basic,
            max_routes=len(absolute_seeds),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.exception("ui_knowledge_capture: crawler failed for job %s", getattr(job, "job_id", "?"))
        report["failed"] += len(routes_needing_capture)
        for entry in report["checked_routes"]:
            if entry["status"] == "needs_capture":
                entry["status"] = "crawl_failed"
                entry["error"] = str(exc)[:400]
        return report

    # Match crawler output to the routes we asked about.
    #
    # Portals like Pulze redirect authed routes to a different origin (Azure
    # B2C sign-in), so the crawler's `page.url()` after `goto()` may not
    # match the seed's path at all. Strategy:
    #   1. Try exact path match on the FINAL URL after redirects.
    #   2. Try trailing-slash-insensitive match.
    #   3. Fall back to positional match against the crawler's `routes` list
    #      — the crawler processes the queue in seed order, so `routes[i]`
    #      corresponds to `routes_needing_capture[i]` up to failed seeds.
    crawl_routes = list(crawl.get("routes") or [])
    by_path: Dict[str, Dict[str, Any]] = {}
    for cr in crawl_routes:
        url = str(cr.get("url") or "")
        try:
            key = urlparse(url).path or "/"
        except Exception:
            key = url
        by_path.setdefault(key, cr)

    # Positional fallback: when path-matching fails (cross-origin redirects,
    # single-page apps, portals that rewrite URLs post-auth), we assign
    # crawler routes to unmatched seeds. Prefer routes that actually
    # returned elements — a route with 0 interactables is useless as
    # ground truth and would leave the artifact generator guessing.
    unused_routes = list(crawl_routes)

    # Pre-consume path-matched crawler routes so they aren't reused positionally.
    path_matched: Dict[str, Dict[str, Any]] = {}
    for target in routes_needing_capture:
        route = target["route"]
        cr = by_path.get(route)
        if cr is None:
            for cand_path, cand_data in by_path.items():
                if cand_path.rstrip("/") == route.rstrip("/"):
                    cr = cand_data
                    break
        if cr is not None:
            path_matched[route] = cr
            unused_routes = [x for x in unused_routes if x is not cr]

    def _pick_positional() -> Optional[Dict[str, Any]]:
        # Prefer routes with actual interactables. Zero-element results
        # (e.g. mid-redirect snapshots) are near-useless as ground truth
        # so we push them to the end.
        if not unused_routes:
            return None
        best_idx = None
        best_size = -1
        for i, cr in enumerate(unused_routes):
            size = len(cr.get("interactables") or [])
            if size > best_size:
                best_size = size
                best_idx = i
        if best_idx is None:
            return None
        return unused_routes.pop(best_idx)

    for target in routes_needing_capture:
        route = target["route"]
        cr = path_matched.get(route)

        if cr is None:
            cr = _pick_positional()
            if cr is not None:
                final_url = str(cr.get("url") or "")
                _mark(report, route, "captured_after_redirect", extra={"final_url": final_url})

        if cr is None:
            report["failed"] += 1
            _mark(report, route, "crawler_returned_no_data")
            continue

        payload = _shape_snapshot_payload(
            client_slug=str(getattr(client, "slug", "") or ""),
            route=route,
            crawl_route=cr,
            feature_name_hint=str(getattr(job, "feature_name", "") or ""),
        )
        try:
            result = persist_snapshot(client, payload)
            report["captured"] += 1
            _mark(report, route, "captured", extra={"snapshot_id": result.get("snapshot_id"), "elements": result.get("elements")})
        except Exception as exc:  # noqa: BLE001
            logger.exception("ui_knowledge_capture: persist failed for route %s", route)
            report["failed"] += 1
            _mark(report, route, "persist_failed", extra={"error": str(exc)[:400]})

    return report


def _mark(report: Dict[str, Any], route: str, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    for entry in report["checked_routes"]:
        if entry.get("route") == route:
            entry["status"] = status
            if extra:
                entry.update(extra)
            return
    row = {"route": route, "status": status}
    if extra:
        row.update(extra)
    report["checked_routes"].append(row)
