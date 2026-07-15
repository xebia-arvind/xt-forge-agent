"""
Diagnostic CLI for the "Plan Architect shows empty selectors" symptom.

Phase 13.4 — after Plan Architect renders `(missing — patch me)` for every
step, the operator has no signal about WHY the underlying ui_knowledge
capture failed. This command dumps every relevant field for one job so
the operator can see:

  * whether `job.preconditions.http_basic` is set (with the password
    redacted — never leaks credentials to the terminal),
  * whether a `UIPage` + `UIRouteSnapshot` exists for each seed URL,
  * how many `UIElement` rows the latest snapshot has,
  * the last `capture_report` recorded on `stage_history`.

Usage:
    python manage.py inspect_capture <job_id>
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from test_generation.models import GenerationJob


def _redact(value: str, keep: int = 2) -> str:
    """Mask everything but the first `keep` chars of a secret."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def _redact_preconditions(pre: dict) -> dict:
    """Return a deep-copy of pre with any 'password' field masked."""
    if not isinstance(pre, dict):
        return {}
    out: dict = {}
    for k, v in pre.items():
        if isinstance(v, dict):
            out[k] = {
                sk: (_redact(str(sv)) if "password" in sk.lower() else sv)
                for sk, sv in v.items()
            }
        elif "password" in k.lower():
            out[k] = _redact(str(v))
        else:
            out[k] = v
    return out


def _route_from_url(base_url: str, seed: str) -> str:
    """Mirror the derivation used by _build_ground_truth_inventory."""
    raw = (seed or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    if raw.startswith("/"):
        return raw
    return f"/{raw}"


class Command(BaseCommand):
    help = "Dump ui_knowledge capture status for one GenerationJob."

    def add_arguments(self, parser):
        parser.add_argument("job_id", help="GenerationJob.job_id (UUID string)")

    def handle(self, *args, **opts):
        job_id = opts["job_id"]
        try:
            job = GenerationJob.objects.select_related("client").get(job_id=job_id)
        except GenerationJob.DoesNotExist as exc:
            raise CommandError(f"Job {job_id} not found.") from exc

        w = self.stdout.write
        w(self.style.HTTP_INFO(f"\n=== inspect_capture · job {job.job_id} ==="))
        w(f"  client: {getattr(job.client, 'slug', '(none)')}")
        w(f"  stage:  {job.stage}")
        w(f"  base_url: {job.base_url or '(unset)'}")
        w(f"  seed_urls: {list(job.seed_urls or [])}")

        # Preconditions — mask any password-shaped field before printing.
        pre = _redact_preconditions(job.preconditions or {})
        w(f"\n  preconditions (passwords redacted):")
        if pre:
            for k, v in pre.items():
                w(f"    {k}: {v}")
        else:
            w("    (empty)")
        has_basic = bool(
            isinstance(job.preconditions, dict)
            and isinstance(job.preconditions.get("http_basic"), dict)
            and job.preconditions["http_basic"].get("username")
            and job.preconditions["http_basic"].get("password")
        )
        if not has_basic:
            w(self.style.WARNING(
                "    → No http_basic auth set. If the base_url requires "
                "auth, the crawler will fail silently."
            ))

        # UI knowledge state per seed URL.
        w(f"\n  ui_knowledge state per seed URL:")
        from ui_knowledge.models import UIPage
        routes = [_route_from_url(job.base_url or "", u) for u in (list(job.seed_urls or []) or [job.base_url or ""])]
        routes = [r for r in routes if r]
        if not routes:
            w("    (no seed URLs to check)")
        for route in routes:
            page = UIPage.objects.filter(
                client=job.client, route=route, is_active=True
            ).first()
            if not page:
                w(self.style.ERROR(f"    {route}: NO UIPage row"))
                continue
            snap = page.snapshots.filter(is_current=True).order_by("-version").first()
            if not snap:
                w(self.style.ERROR(
                    f"    {route}: UIPage exists but no is_current snapshot"
                ))
                continue
            n = snap.elements.count()
            marker = self.style.SUCCESS if n > 0 else self.style.ERROR
            w(marker(
                f"    {route}: snapshot v{snap.version}, {n} element(s)"
            ))

        # Most recent capture_report in stage_history.
        history = list(job.stage_history or [])
        latest_report = None
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            diag = entry.get("diagnostic")
            if isinstance(diag, dict) and diag.get("ui_knowledge_capture"):
                latest_report = diag["ui_knowledge_capture"]
                break
        w("\n  last capture_report from stage_history:")
        if not latest_report:
            w("    (none recorded)")
        else:
            w(f"    enabled: {latest_report.get('enabled')}")
            w(f"    captured: {latest_report.get('captured')}")
            w(f"    skipped:  {latest_report.get('skipped')}")
            w(f"    failed:   {latest_report.get('failed')}")
            for r in (latest_report.get("checked_routes") or []):
                marker = self.style.ERROR if "fail" in str(r.get("status") or "") else self.style.NOTICE
                w(marker(f"    - {r.get('route')}: {r.get('status')}"))
                if r.get("error"):
                    w(f"      error: {r['error'][:400]}")

        # Actionable footer.
        if not has_basic and job.base_url:
            w(self.style.WARNING(
                "\n  → Fix: add http_basic to preconditions via Django admin:\n"
                "      GenerationJob · " + str(job.job_id) + " → preconditions field\n"
                "      {\"http_basic\": {\"username\": \"...\", \"password\": \"...\"}}\n"
                "    then re-run the Plan Architect stage."
            ))
        w("")
