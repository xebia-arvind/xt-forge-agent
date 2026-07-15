"""
Phase 6 — Executor + Root-Cause Fixer retry loop.

`run_and_repair(job_id)` is a django-q2 task. It runs Cucumber against a
materialized job's per-tenant feature directory, parses the JSON report,
and — if any scenario failed — invokes the Root-Cause Fixer LLM agent to
patch the affected artifacts before re-materializing and re-running.

Hard cap: 3 iterations. If the run is still red at iteration 3 the job flips
to STAGE_HUMAN_REVIEW_NEEDED with every iteration's raw log preserved on
`stage_execute_output.iterations[]`.

    stage_execute_output = {
        "iterations": [
            {
                "iteration": 1,
                "runner_job_id": 42,
                "started_on": "...",
                "finished_on": "...",
                "return_code": 1,
                "scenarios": [
                    {"name": "...", "status": "passed"|"failed", "step_failure": {...}},
                    ...
                ],
                "diagnosis": "…",             # from root-cause agent (present on failed iterations)
                "patches_applied": ["features/steps/xx-99-steps.ts", ...]
            },
            ...
        ],
        "final_state": "GREEN"|"HUMAN_REVIEW_NEEDED",
        "green_iteration": 2,                 # only present on green
    }
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.utils import timezone as dj_timezone

from clients.models import Clients  # noqa: F401 (documentation of scope)
from runners.models import RunnerJob
from runners import tasks as runner_tasks
from .agents import run_root_cause_fixer
from .generation_service import materialize_job
from .models import GeneratedArtifact, GenerationJob

logger = logging.getLogger(__name__)

# Iteration cap for the Cucumber → Root-Cause-Fixer loop. Env-overridable
# so the operator can tune per environment (CI, local dev, prod release
# validation) without a code change. Legacy value was 3.
MAX_ITERATIONS = int(os.environ.get("TEST_GEN_MAX_ITERATIONS", "15"))
CUCUMBER_REPORT_RELPATH = "test-results/cucumber-report.json"


# ---------------------------------------------------------------------------
# Cucumber report parsing
# ---------------------------------------------------------------------------
def _iso_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_cucumber_report(report_path: Path) -> List[Dict[str, Any]]:
    """
    Convert the Cucumber-JS JSON report into a flat list of scenarios with
    per-scenario pass/fail info. Missing report file → empty list (executor
    treats that as "all failed / unknown").

    Cucumber's report is an array of features, each with `elements[]` which
    are scenarios; each scenario has `steps[]` with `result.status`.
    """
    if not report_path.exists():
        return []
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse Cucumber report %s: %s", report_path, exc)
        return []

    # Statuses that Cucumber uses to indicate a step DID NOT COMPLETE
    # SUCCESSFULLY. "undefined" and "pending" mean the runner found a
    # step in the .feature that has no matching step-def signature —
    # historically we treated those as passes, which caused smoke-mode
    # to declare GREEN even though Cucumber exited rc=1.
    # "ambiguous" means multiple step-defs matched. All three are
    # scenario-fail conditions per Cucumber's own semantics.
    _FAIL_STATUSES = {"failed", "undefined", "pending", "ambiguous"}

    scenarios: List[Dict[str, Any]] = []
    for feature in payload or []:
        feature_name = str(feature.get("name") or feature.get("uri") or "")
        for element in feature.get("elements") or []:
            if element.get("type") not in (None, "scenario", "background"):
                continue
            steps = element.get("steps") or []
            failed_step = None
            for step in steps:
                result = step.get("result") or {}
                status = str(result.get("status") or "").lower()
                if status in _FAIL_STATUSES:
                    raw_err = str(result.get("error_message") or "")
                    if not raw_err:
                        # Undefined / pending / ambiguous steps have no
                        # error_message, so synthesize one so downstream
                        # rendering (panel + Root-Cause Fixer prompt) has
                        # something meaningful to display.
                        raw_err = f"Step is {status} — no matching step-definition."
                    err_head, _, stack = raw_err.partition("\n")
                    failed_step = {
                        "keyword": (step.get("keyword") or "").strip(),
                        "name": step.get("name") or "",
                        "status": status,
                        "error_message": raw_err[:4000],
                        "error_head": err_head[:400],
                        "stack_trace": stack[:4000],
                        "duration_ns": result.get("duration") or 0,
                    }
                    break
            status_ = "failed" if failed_step else "passed"
            scenarios.append({
                "feature": feature_name,
                "name": element.get("name") or "",
                "status": status_,
                "step_failure": failed_step,
            })
    return scenarios


# ---------------------------------------------------------------------------
# BASE_URL normalization
# ---------------------------------------------------------------------------
def _resolve_effective_base_url(base_url: str, seed_urls: List[str]) -> str:
    """
    Return the BASE_URL to expose to Cucumber, avoiding path duplication.

    Playwright's `page.goto('/foo')` resolves against `baseURL`. If the
    stored `job.base_url` contains a PATH prefix that every seed URL also
    starts with (e.g. `base_url = "https://staging.pulze.com/it-IT"` and
    seeds = `["/it-IT/", "/it-IT/account/login"]`), then a naive
    `goto("/it-IT/")` resolves to `"https://staging.pulze.com/it-IT/it-IT/"`
    — a 404 due to path duplication.

    Rule: if every seed's path starts with `base_url.path`, strip that
    path from base_url so the resolution stays clean. Otherwise return
    `base_url` unchanged — the operator wrote it intentionally.
    """
    if not base_url:
        return base_url

    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base_url)
    if not (parsed.scheme and parsed.netloc):
        return base_url

    base_path = (parsed.path or "").rstrip("/")
    if not base_path:
        return base_url                     # already just an origin, nothing to strip

    # Every seed must have a path AND start with base_path for stripping
    # to be safe. If seeds are absolute URLs, compare their paths too.
    seeds_relative_to_base = []
    for raw in seed_urls or []:
        raw = str(raw or "").strip()
        if not raw:
            continue
        seed_parsed = urlparse(raw)
        seed_path = seed_parsed.path if (seed_parsed.scheme and seed_parsed.netloc) else raw
        if not seed_path.startswith("/"):
            seed_path = f"/{seed_path}"
        seeds_relative_to_base.append(seed_path)

    if not seeds_relative_to_base:
        return base_url

    # Only strip when EVERY seed already contains base_path — otherwise
    # the operator meant a nested base and we'd break relative seeds.
    if all(sp == base_path or sp.startswith(base_path + "/") for sp in seeds_relative_to_base):
        stripped = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        logger.info(
            "BASE_URL: stripped duplicate path prefix %r → %r "
            "(seeds already carry it: %s)",
            base_url, stripped, seeds_relative_to_base[:3],
        )
        return stripped
    return base_url


# ---------------------------------------------------------------------------
# Synchronous runner spawn (bypass django-q since we're inside a worker task)
# ---------------------------------------------------------------------------
def _make_log_path(job_id: int) -> str:
    return os.path.join(settings.RUNNER_LOG_DIR, f"job_{job_id}.log")


def _spawn_cucumber_runner_sync(job: GenerationJob) -> RunnerJob:
    """
    Create a RunnerJob(kind=CUCUMBER) and run the subprocess *inline* using
    the same `runners.tasks._run` primitive the qcluster worker uses. Blocks
    until Cucumber exits.
    """
    client = job.client
    slug = (getattr(client, "slug", "") or "").strip()
    # Invoke the LOCAL @cucumber/cucumber binary — not `npx cucumber-js`.
    # Two reasons:
    #   1. `npx cucumber-js` (or `npx --package=@cucumber/cucumber cucumber-js`)
    #      resolves the binary from an isolated npx cache that does NOT include
    #      the repo's `ts-node` — so Cucumber crashes on `Cannot find module
    #      'ts-node/register'` because our cucumber.js declares it.
    #   2. The local install shares node_modules with everything the config +
    #      step defs import, so ts-node, chalk, page-object files all resolve.
    argv = [
        "node", "node_modules/.bin/cucumber-js", "--profile", "tenant",
    ]
    env_overrides = {
        "TENANT_SLUG": slug,
        "HEADLESS": "true",
        # CI-style flags so any interactive tool downstream picks the sane path.
        "CI": "1",
    }
    if job.base_url:
        env_overrides["BASE_URL"] = _resolve_effective_base_url(
            job.base_url, list(job.seed_urls or [])
        )

    # `preconditions` is a free-form dict extracted from the Jira story.
    # Well-known keys are lifted to env vars the CustomWorld / step defs read.
    # Everything else is passed through as `PRECOND_<KEY>=<value>` so step
    # defs can grab arbitrary values (year selectors, API endpoints, etc.).
    pre = dict(job.preconditions or {})
    http_basic = pre.pop("http_basic", None) or {}
    if isinstance(http_basic, dict):
        user = str(http_basic.get("username") or "").strip()
        password = str(http_basic.get("password") or "").strip()
        if user and password:
            env_overrides["HTTP_BASIC_USERNAME"] = user
            env_overrides["HTTP_BASIC_PASSWORD"] = password
    # Phase 9.5 — canonical login-credential aliases. The step-def env
    # fallback (Phase 9.4) looks for PRECOND_EMAIL / PRECOND_USERNAME /
    # PRECOND_PASSWORD regardless of which key the Jira story used. Lift
    # `email` / `username` and `password` explicitly BEFORE the generic
    # `PRECOND_<KEY>` loop below so the alias names are stable and the loop
    # doesn't overwrite them with a different-cased duplicate.
    email_literal = str(pre.get("email") or pre.get("username") or "").strip()
    if email_literal:
        env_overrides.setdefault("PRECOND_EMAIL", email_literal)
        env_overrides.setdefault("PRECOND_USERNAME", email_literal)
    password_literal = str(pre.get("password") or "").strip()
    if password_literal:
        env_overrides.setdefault("PRECOND_PASSWORD", password_literal)
    for key, value in pre.items():
        # Simple string values only — objects/arrays are ignored to keep the
        # env clean. Reviewers can read the raw JSON in the Feature panel.
        if isinstance(value, (str, int, float, bool)):
            env_overrides.setdefault(f"PRECOND_{str(key).upper()}", str(value))

    runner_job = RunnerJob.objects.create(
        client=client,
        kind=RunnerJob.KIND_CUCUMBER,
        state=RunnerJob.STATE_QUEUED,
        argv=argv,
        cwd=settings.REPO_ROOT,
        env_overrides=env_overrides,
        log_path="",
    )
    runner_job.log_path = _make_log_path(runner_job.id)
    open(runner_job.log_path, "a", encoding="utf-8").close()
    runner_job.save(update_fields=["log_path", "last_modified"])

    # Run the subprocess inline. `_run` reads state off the row and updates
    # start/finish/return_code/state on it.
    runner_tasks._run(runner_job)
    runner_job.refresh_from_db()
    return runner_job


# ---------------------------------------------------------------------------
# Root-cause application
# ---------------------------------------------------------------------------
def _artifact_snapshot(job: GenerationJob) -> List[Dict[str, str]]:
    """Small snapshot of current artifacts to feed the root-cause fixer."""
    snapshot = []
    for a in job.artifacts.all():
        snapshot.append({
            "artifact_type": a.artifact_type,
            "relative_path": a.relative_path,
            "content_preview": (a.content_final or a.content_draft or "")[:400],
        })
    return snapshot


def _apply_patches(job: GenerationJob, patches: List[Dict[str, Any]]) -> List[str]:
    """
    Overwrite `GeneratedArtifact.content_final` for each patched file.
    Returns the list of relative paths that were actually touched.

    Files that appear in the patch list but not in the DB are ignored (the
    agent can only patch what already exists — creating new files here would
    bypass all the validation).
    """
    touched: List[str] = []
    for patch in patches or []:
        rp = str(patch.get("relative_path") or "").strip()
        content = str(patch.get("content") or "")
        if not rp or not content:
            continue
        try:
            artifact = job.artifacts.get(relative_path=rp)
        except GeneratedArtifact.DoesNotExist:
            logger.info("Root-cause patch skipped: %s not in artifact table.", rp)
            continue
        artifact.content_final = content
        artifact.content_draft = content
        artifact.save(update_fields=["content_final", "content_draft", "last_modified"])
        touched.append(rp)
    return touched


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------
def _run_single_iteration(job: GenerationJob, iteration: int) -> Dict[str, Any]:
    """Run Cucumber once, parse the report, decide green/red."""
    started_on = _iso_utc()

    # Delete any stale cucumber-report.json from a previous run BEFORE spawning
    # Cucumber. Without this, if Cucumber crashes on TypeScript module-load
    # (e.g. undefined class reference in step defs), it won't touch the file
    # and our parser reads yesterday's report — producing a bogus "step timed
    # out" diagnosis instead of the real "TS2304 undefined class" one. See
    # ai-healer-django/... executor.py comment history for prior bug.
    report_path = Path(settings.REPO_ROOT) / CUCUMBER_REPORT_RELPATH
    try:
        if report_path.exists():
            report_path.unlink()
    except OSError as exc:
        logger.warning("Could not remove stale Cucumber report %s: %s", report_path, exc)

    runner_job = _spawn_cucumber_runner_sync(job)
    finished_on = _iso_utc()

    scenarios = _parse_cucumber_report(report_path)
    # Belt-and-suspenders: the iteration passes only when BOTH the JSON
    # report says every scenario passed AND the runner exited with rc=0.
    # Cucumber writes rc=1 for undefined / pending / ambiguous / failed
    # steps, but individual steps of the same run can still be marked
    # `passed` in the JSON — checking rc catches parser edge cases.
    parser_says_green = bool(scenarios) and all(s["status"] == "passed" for s in scenarios)
    runner_rc_green = runner_job.return_code in (None, 0)
    all_passed = parser_says_green and runner_rc_green

    # If Cucumber crashed BEFORE writing a report (import error, syntax error,
    # module-scope crash), `scenarios` is empty even though the runner exited
    # non-zero. Capture the tail of the log so the Root-Cause Fixer still has
    # something to patch against — otherwise the executor loop spins its
    # wheels for MAX_ITERATIONS with "no failure found; retrying".
    crash_log_tail = ""
    if not scenarios and runner_job.return_code not in (None, 0):
        crash_log_tail = _read_log_tail(runner_job.log_path, max_bytes=4000)

    # Phase 5.5 — Attach a UI-regression report to each failed scenario.
    # For every failure we diff the current DOM (via ui_knowledge's
    # baseline/current snapshots) against what the test expects, so the
    # operator can immediately see "this test broke because 3 elements
    # changed since capture" instead of triaging blindly.
    _attach_regression_reports(job, scenarios)

    return {
        "iteration": iteration,
        "runner_job_id": runner_job.id,
        "runner_state": runner_job.state,
        "return_code": runner_job.return_code,
        "started_on": started_on,
        "finished_on": finished_on,
        "scenarios": scenarios,
        "all_passed": all_passed,
        "crash_log_tail": crash_log_tail,
    }


def _read_log_tail(log_path: str, max_bytes: int = 4000) -> str:
    """Return the last `max_bytes` of a runner log file. Empty on any error."""
    if not log_path:
        return ""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _attach_regression_reports(job: GenerationJob, scenarios: List[Dict[str, Any]]) -> None:
    """
    For each failed scenario, use ui_knowledge's change-detection service
    to diff current DOM vs. the stored baseline. Mutates `scenarios` in
    place — each failed scenario gets a new `regression_report` field.

    Never raises. On any failure (ui_knowledge unavailable, no snapshots,
    etc.) the field is set to `{ui_change_level: "UNKNOWN", reason: ...}`
    so the review panel can render "no regression signal available"
    instead of erroring.
    """
    try:
        from ui_knowledge.change_detection_service import detect_ui_change_for_healing
    except Exception as exc:  # noqa: BLE001
        logger.warning("regression_report: import failed: %s", exc)
        for s in scenarios:
            if s.get("status") == "failed":
                s["regression_report"] = {
                    "ui_change_level": "UNKNOWN",
                    "reason": "ui_knowledge unavailable",
                }
        return

    # Use each seed URL as a candidate reference URL. Cucumber's JSON
    # report doesn't expose the page URL at the time of failure, so we
    # fall back to trying every seed and pick the first one that resolves
    # to a UIPage in ui_knowledge. Same fallback shape the existing
    # `_capture_failure_context` uses, just extended to walk seeds.
    from urllib.parse import urlparse

    candidate_routes: List[str] = []
    for u in [job.base_url] + list(job.seed_urls or []):
        raw = (u or "").strip()
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            candidate_routes.append(parsed.path or "/")
        else:
            candidate_routes.append(raw if raw.startswith("/") else f"/{raw}")

    for s in scenarios:
        if s.get("status") != "failed":
            continue
        step_failure = s.get("step_failure") or {}
        failed_selector = ""
        step_name = str(step_failure.get("name") or "")
        report = None
        for route in candidate_routes or ["/"]:
            try:
                candidate_report = detect_ui_change_for_healing(
                    page_url=route,
                    failed_selector=failed_selector,
                    use_of_selector=step_name,
                    client=job.client,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("regression_report: detect failed for %s: %s", route, exc)
                continue
            # Prefer routes with a real page over UNKNOWN.
            if candidate_report and candidate_report.get("ui_change_level") != "UNKNOWN":
                report = candidate_report
                break
            if report is None:
                report = candidate_report
        s["regression_report"] = report or {"ui_change_level": "UNKNOWN", "reason": "no ui_knowledge page"}


def _pick_representative_failure(scenarios: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the first failed scenario (with its failing step) or None."""
    for s in scenarios:
        if s.get("status") == "failed":
            return s
    return None


# ---------------------------------------------------------------------------
# Failure-context capture (Playwright MCP) — feeds real DOM into the fixer
# ---------------------------------------------------------------------------
def _capture_failure_context(job: GenerationJob, failed_scenario: Dict[str, Any]) -> Dict[str, Any]:
    """
    Re-navigate to the failing page via Playwright MCP and snapshot its live
    DOM/accessibility tree. Returns `{page_url, page_dom, notes}`; every field
    is empty on any error (MCP absent, timeout, etc.) so the caller can just
    interpolate without None-checking.

    We use `job.base_url` as the target because Cucumber's JSON report doesn't
    expose the page URL at failure moment reliably. That means the DOM we
    fetch is the LANDING page, not the exact page the test crashed on. Still
    dramatically more useful than the empty string we passed in v1.
    """
    from .mcp_playwright import playwright_mcp_client

    url = (job.base_url or "").strip()
    if not url:
        return {"page_url": "", "page_dom": "", "notes": "no base_url on job"}

    dom = ""
    with playwright_mcp_client() as client:
        if client is None:
            return {"page_url": url, "page_dom": "", "notes": "playwright MCP unavailable"}
        try:
            client.open(url)
            snap = client.snapshot()
            dom = str(snap.get("html") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP failure-context capture failed: %s", exc)
            return {"page_url": url, "page_dom": "", "notes": f"mcp error: {exc}"}

    return {"page_url": url, "page_dom": dom, "notes": ""}


def _signature_of_patches(patches: List[Dict[str, Any]]) -> str:
    """
    Return a deterministic hash covering every (path, content) pair in the
    patch bundle. Two consecutive iterations with the same signature mean the
    LLM is stuck — we bail out of the loop.
    """
    import hashlib
    items = []
    for p in patches or []:
        rp = str((p or {}).get("relative_path") or "")
        body = str((p or {}).get("content") or "")
        items.append((rp, hashlib.sha256(body.encode("utf-8")).hexdigest()))
    items.sort()
    return hashlib.sha256(
        "\n".join(f"{rp}::{h}" for rp, h in items).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Public task entry point
# ---------------------------------------------------------------------------
def run_and_repair(job_id, smoke_mode: bool = False) -> None:
    """
    Django-q2 task. Drives up to `MAX_ITERATIONS` Cucumber runs, applying
    LLM-generated patches between failed runs. Updates
    `job.stage_execute_output` after every iteration so the panel's polling
    UI can render progress live.

    `smoke_mode` (Phase 6.5.3): when True, the task runs exactly ONE
    Cucumber iteration with NO Fixer / NO LLM involvement. If it passes,
    the job goes to REPORT with `final_state=GREEN`. If it fails, the
    job goes to HUMAN_REVIEW_NEEDED with `smoke_mode_failed=True` — the
    Execute panel surfaces a banner offering "Heal & retry" that
    triggers a fresh full-heal run. Smoke mode is used for daily
    "did anything drift?" runs on previously-green jobs.
    """
    try:
        job = GenerationJob.objects.get(job_id=job_id)
    except GenerationJob.DoesNotExist:
        logger.warning("run_and_repair called with unknown job_id=%s", job_id)
        return

    output = dict(job.stage_execute_output or {})
    iterations: List[Dict[str, Any]] = list(output.get("iterations") or [])
    output["iterations"] = iterations
    # Persist smoke_mode into stage_execute_output so the panel + reporter
    # can tell what the operator asked for.
    output["smoke_mode"] = bool(smoke_mode)
    # Convergence tracking — the LLM gets no more chances once it produces
    # identical patch content two iterations in a row.
    previous_diagnoses: List[str] = list(output.get("previous_diagnoses") or [])
    signatures: List[str] = list(output.get("patch_signatures") or [])

    for i in range(1, MAX_ITERATIONS + 1):
        job.execute_iteration = i
        job.save(update_fields=["execute_iteration", "last_modified"])

        # Re-materialize before every iteration (patches from the previous
        # iteration are already on the artifact rows, so this is what writes
        # them to disk).
        slug = getattr(getattr(job, "client", None), "slug", "") or ""
        try:
            mat_result = materialize_job(job, allow_overwrite=True, client_slug=slug)
        except Exception as exc:
            iterations.append({
                "iteration": i,
                "error": f"materialize failed: {exc}",
                "started_on": _iso_utc(),
                "finished_on": _iso_utc(),
                "all_passed": False,
            })
            output["final_state"] = "HUMAN_REVIEW_NEEDED"
            _persist_output(job, output, GenerationJob.STAGE_HUMAN_REVIEW_NEEDED)
            return

        iter_result = _run_single_iteration(job, i)
        iter_result["written_files"] = mat_result.written_files
        iterations.append(iter_result)
        _persist_output(job, output)   # incremental so UI can see progress

        if iter_result["all_passed"]:
            output["final_state"] = "GREEN"
            output["green_iteration"] = i
            _persist_output(job, output, GenerationJob.STAGE_REPORT)
            return

        # Phase 6.5.3 — Smoke-mode short-circuit. When the operator asked
        # for a smoke run (previously-GREEN job, daily-drift check), a
        # failing iteration 1 is a real regression signal — NOT an
        # invitation to burn LLM tokens patching around it. Stop here
        # and let the operator decide (via the Execute panel banner)
        # whether this is a genuine regression to report OR whether to
        # trigger a fresh full-heal run.
        if smoke_mode and i == 1:
            output["final_state"] = "RED"
            output["smoke_mode_failed"] = True
            _persist_output(job, output, GenerationJob.STAGE_HUMAN_REVIEW_NEEDED)
            logger.info(
                "run_and_repair: smoke mode failed for job %s on iteration 1 — no auto-heal",
                job.job_id,
            )
            return

        # Red. If we still have budget, invoke the Root-Cause Fixer and patch.
        if i < MAX_ITERATIONS:
            failure = _pick_representative_failure(iter_result["scenarios"])
            if not failure:
                # Runner exited non-zero but Cucumber never wrote a report —
                # usually an import/syntax crash in the step defs so the file
                # can't even load. Synthesize a pseudo-failure from the runner
                # log tail so the Root-Cause Fixer still gets a signal.
                tail = (iter_result.get("crash_log_tail") or "").strip()
                if not tail:
                    iter_result["diagnosis"] = "no failure found in Cucumber report and log is empty; giving up"
                    output["final_state"] = "HUMAN_REVIEW_NEEDED"
                    _persist_output(job, output, GenerationJob.STAGE_HUMAN_REVIEW_NEEDED)
                    return
                failure = {
                    "name": "(module-load failure — Cucumber crashed before writing a report)",
                    "feature": "",
                    "step_failure": {
                        "keyword": "",
                        "name": "load step-definitions file",
                        "error_message": tail,
                        "error_head": tail.splitlines()[0][:400] if tail else "",
                        "stack_trace": tail,
                    },
                }

            # Capture the live page DOM via Playwright MCP so the Root-Cause
            # Fixer has real ground truth (not the empty string legacy code
            # passed in). Falls back to empty when MCP is unavailable — logged
            # but non-fatal.
            failure_ctx = _capture_failure_context(job, failure)
            iter_result["failure_context"] = {
                "page_url": failure_ctx.get("page_url", ""),
                "dom_bytes": len(failure_ctx.get("page_dom", "")),
                "notes": failure_ctx.get("notes", ""),
            }

            try:
                patch_bundle = run_root_cause_fixer(
                    job,
                    failed_scenario={"name": failure.get("name"), "feature": failure.get("feature")},
                    failed_step=failure.get("step_failure") or {},
                    error_message=(failure.get("step_failure") or {}).get("error_message") or "",
                    page_html_excerpt="",              # legacy — DOM comes via page_dom_snapshot now
                    failed_selector="",
                    page_url=failure_ctx.get("page_url", ""),
                    current_artifacts=_artifact_snapshot(job),
                    previous_diagnoses=previous_diagnoses,
                    page_dom_snapshot=failure_ctx.get("page_dom", ""),
                )
            except Exception as exc:
                iter_result["diagnosis"] = f"root-cause fixer raised: {exc}"
                _persist_output(job, output)
                continue

            patches = patch_bundle.get("patches") or []
            diagnosis = str(patch_bundle.get("diagnosis") or "")
            touched = _apply_patches(job, patches)
            iter_result["diagnosis"] = diagnosis
            iter_result["patches_applied"] = touched
            # Phase 1: tool-using fixer emits a linear log of tool calls it made
            # to arrive at the patch. UI renders this as an expandable trace.
            tool_trace = patch_bundle.get("tool_trace")
            if tool_trace:
                iter_result["tool_trace"] = tool_trace

            # Convergence detection — track diagnosis + patch signature.
            previous_diagnoses.append(diagnosis)
            sig = _signature_of_patches(patches)
            signatures.append(sig)
            output["previous_diagnoses"] = previous_diagnoses
            output["patch_signatures"] = signatures

            # Two consecutive identical signatures → LLM is stuck; bail out
            # before we burn more Ollama tokens on a doomed loop.
            if len(signatures) >= 2 and signatures[-1] == signatures[-2]:
                iter_result["diagnosis"] = (
                    diagnosis + " [converged: identical patches two iterations in a row]"
                ).strip()
                output["final_state"] = "STUCK_CONVERGED"
                _persist_output(job, output, GenerationJob.STAGE_HUMAN_REVIEW_NEEDED)
                return

            _persist_output(job, output)

    # Fell out of the loop without a green run.
    output["final_state"] = "HUMAN_REVIEW_NEEDED"
    _persist_output(job, output, GenerationJob.STAGE_HUMAN_REVIEW_NEEDED)


def _persist_output(job: GenerationJob, output: Dict[str, Any], new_stage: Optional[str] = None) -> None:
    job.stage_execute_output = output
    if new_stage:
        job.stage = new_stage
    job.save(update_fields=[
        "stage_execute_output", "stage", "last_modified",
    ] if new_stage else ["stage_execute_output", "last_modified"])
