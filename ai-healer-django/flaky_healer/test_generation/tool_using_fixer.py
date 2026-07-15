"""
Tool-using Root-Cause Fixer — Claude-Code-style loop over OpenAI function-calling.

The legacy fixer is one-shot: prompt in, patch bundle out. This module runs a
multi-turn conversation where GPT-4o is given a toolbox (`browser_snapshot`,
`read_artifact`, `write_artifact`, `run_ts_check`, `list_artifacts`,
`search_page_source`) and iterates until it either:

  * calls `end_fix(diagnosis, notes)` to signal completion, OR
  * emits a final assistant message with a JSON patch bundle, OR
  * hits the turn/wall-clock cap (`HUMAN_REVIEW_NEEDED`).

The returned dict matches the legacy contract so the Executor doesn't care
which mode ran:

    {
        "diagnosis": "one-liner",
        "patches":   [{"relative_path": …, "content": …, "reason": …}, …],
        "notes":     [...],
        "tool_trace": [   # NEW: linear log of tool calls the LLM made
            {"turn": 1, "tool": "browser_snapshot", "args": {}, "result_bytes": 1204},
            {"turn": 2, "tool": "read_artifact",    "args": {"path": "features/…"}, "result_bytes": 850},
            ...
        ],
    }

Kill-switch: if `OPENAI_TOOL_USE=off` the caller falls back to the legacy
non-tool-using prompt path. This module simply won't be invoked in that case.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from django.conf import settings

from .llm_backends import (
    LLMBackendError,
    _backend,        # module-level OpenAI client accessor
    _model_for,
)
from .models import GeneratedArtifact, GenerationJob

logger = logging.getLogger(__name__)


# Hard caps — a runaway loop would burn tokens fast.
MAX_TURNS = 15
WALL_CLOCK_SECONDS = 600           # 10 minutes


# ---------------------------------------------------------------------------
# Tool schema (OpenAI function-calling format)
# ---------------------------------------------------------------------------
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_artifacts",
            "description": (
                "Return every artifact on this job (path, artifact_type, validation_status). "
                "Call this ONCE at the start to see what you can read/write."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": "Return the current content of an artifact by its `relative_path`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "e.g. features/steps/foo-steps.ts"},
                },
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_artifact",
            "description": (
                "Overwrite the `content_final` of an artifact. Provide the FULL new "
                "file body — no diffs, no ellipses. The old content is replaced whole."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                    "reason":  {"type": "string", "description": "One-line why-this-patch."},
                },
                "required": ["relative_path", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "history_lookup",
            "description": (
                "Deterministic-first (Phase 5.4). Check the healer's history cache "
                "for a previously VALID selector that resolved the same "
                "(page_url, use_of_selector, failed_selector) combo. Returns "
                "`{hit: bool, healed_selector, confidence, source_id}`. Prefer "
                "this BEFORE calling browser_snapshot — it's zero-cost and "
                "returns the answer directly when someone's fixed this before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_url":         {"type": "string"},
                    "failed_selector":  {"type": "string"},
                    "use_of_selector":  {"type": "string", "description": "One-line 'what is this locator supposed to do?' hint."},
                },
                "required": ["page_url", "failed_selector", "use_of_selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "matching_engine_probe",
            "description": (
                "Deterministic-first (Phase 5.4). Ask curertestai's matching "
                "engine to find the closest real element on the given page for "
                "the failed selector, using its semantic hint. Returns the top "
                "3 candidates with scores. Prefer this BEFORE browser_snapshot "
                "when the failure looks like 'wrong selector' — it runs against "
                "the stored ui_knowledge inventory, no browser needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_url":         {"type": "string"},
                    "failed_selector":  {"type": "string"},
                    "use_of_selector":  {"type": "string"},
                },
                "required": ["page_url", "failed_selector", "use_of_selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": (
                "Fetch the live page DOM (accessibility-tree text) from Playwright MCP. "
                "Uses the URL Playwright MCP was last navigated to; falls back to the "
                "job's base_url. LAST-RESORT tool — try history_lookup and "
                "matching_engine_probe first when the failure is a bad selector."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_page_source",
            "description": (
                "Grep for `query` in the most-recent browser_snapshot output. Returns "
                "matching lines with a bit of context. Cheap way to check whether a "
                "selector really exists on the page."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_ts_check",
            "description": (
                "Ask the AST normalizer to parse an artifact and report syntax errors. "
                "Use AFTER write_artifact to confirm your patch compiles."
            ),
            "parameters": {
                "type": "object",
                "properties": {"relative_path": {"type": "string"}},
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_fix",
            "description": (
                "Signal that the fix is complete. `diagnosis` is a one-sentence root "
                "cause. Optional `notes` for reviewers. Call this when you've written "
                "all needed patches and verified them with run_ts_check."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diagnosis": {"type": "string"},
                    "notes":     {"type": "array", "items": {"type": "string"}},
                },
                "required": ["diagnosis"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
class _ToolContext:
    """
    Per-fix scratchpad — carries the job, an MCP client (lazy-opened), the
    patches the LLM has written so far, and the tool_trace log.
    """

    def __init__(self, job: GenerationJob):
        self.job = job
        self.mcp = None                 # PlaywrightMCPClient, lazy
        self.mcp_ctx = None             # context manager
        self.last_snapshot: str = ""
        self.patches: List[Dict[str, str]] = []
        self.tool_trace: List[Dict[str, Any]] = []
        self.diagnosis: str = ""
        self.notes: List[str] = []
        self.done: bool = False
        # Phase 5.4 — deterministic-first bookkeeping. Set to True the
        # first time the LLM calls history_lookup, matching_engine_probe,
        # or browser_snapshot. end_fix guard uses these to refuse a
        # zero-patch exit on DOM-shaped failures where the LLM never
        # actually looked at the page.
        self.tried_history:  bool = False
        self.tried_matching: bool = False
        self.tried_snapshot: bool = False

    def close(self) -> None:
        if self.mcp_ctx is not None:
            try:
                self.mcp_ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.mcp_ctx = None
            self.mcp = None

    def ensure_mcp(self):
        if self.mcp is not None:
            return self.mcp
        from .mcp_playwright import playwright_mcp_client
        self.mcp_ctx = playwright_mcp_client()
        self.mcp = self.mcp_ctx.__enter__()
        return self.mcp


def _handle_list_artifacts(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for a in ctx.job.artifacts.all():
        rows.append({
            "relative_path": a.relative_path,
            "artifact_type": a.artifact_type,
            "validation_status": a.validation_status,
        })
    return {"artifacts": rows}


def _handle_read_artifact(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rp = str(args.get("relative_path") or "").strip()
    a = ctx.job.artifacts.filter(relative_path=rp).first()
    if a is None:
        return {"error": f"artifact not found: {rp!r}"}
    return {
        "relative_path": rp,
        "artifact_type": a.artifact_type,
        "content": a.content_final or a.content_draft or "",
    }


def _handle_write_artifact(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rp = str(args.get("relative_path") or "").strip()
    content = str(args.get("content") or "")
    reason = str(args.get("reason") or "")
    if not rp or not content:
        return {"error": "relative_path and content are required"}
    # Track for the returned bundle; don't touch the DB directly — the Executor
    # applies patches via `_apply_patches` after this loop returns.
    ctx.patches.append({"relative_path": rp, "content": content, "reason": reason})
    return {"ok": True, "queued_for_apply": True, "reason": reason[:200]}


def _handle_history_lookup(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic-first (Phase 5.4). Query curertestai's HealerRequest
    history cache for a previously VALID selector matching this
    (page_url, use_of_selector, failed_selector). No LLM, no browser.
    """
    ctx.tried_history = True
    page_url = str(args.get("page_url") or "").strip()
    failed_selector = str(args.get("failed_selector") or "").strip()
    use_of_selector = str(args.get("use_of_selector") or "").strip()
    if not (page_url and failed_selector and use_of_selector):
        return {"hit": False, "reason": "missing required arguments"}

    from datetime import timedelta
    import os
    from django.utils import timezone as tz
    from curertestai.models import HealerRequest

    try:
        max_age_days = int(os.getenv("HEALING_CACHE_MAX_AGE_DAYS", "14"))
        min_conf = float(os.getenv("HEALING_CACHE_MIN_CONFIDENCE", "0.30"))
    except (TypeError, ValueError):
        max_age_days, min_conf = 14, 0.30
    cutoff = tz.now() - timedelta(days=max_age_days)

    qs = (
        HealerRequest.objects.filter(
            url=page_url,
            use_of_selector=use_of_selector,
            failed_selector=failed_selector,
            success=True,
            validation_status="VALID",
            created_on__gte=cutoff,
            confidence__gte=min_conf,
        )
        .exclude(healed_selector__isnull=True)
        .exclude(healed_selector__exact="")
        .order_by("-created_on")
    )
    cached = qs.first()
    if cached is None:
        return {"hit": False, "reason": "no cached VALID selector for this combo"}
    return {
        "hit":              True,
        "healed_selector":  cached.healed_selector,
        "confidence":       round(float(cached.confidence or 0.0), 3),
        "source_id":        cached.id,
    }


def _handle_matching_engine_probe(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic-first (Phase 5.4). Ask curertestai's MatchingEngine to
    rank real elements from ui_knowledge for the failed selector +
    semantic hint. Returns the top candidates with intent-adjusted scores.

    Uses the same merged-engine + intent-boost logic as `deterministic_fill`
    so the Fixer sees the same ranking the pre-persist pass would produce.
    Cross-route search is enabled: if the exact route has no elements
    (e.g. cross-origin redirect), we fall back to any UIPage on the
    client that has captured elements — necessary for portals whose
    login/dashboard redirect to a different origin.
    """
    ctx.tried_matching = True
    page_url = str(args.get("page_url") or "").strip()
    failed_selector = str(args.get("failed_selector") or "").strip()
    use_of_selector = str(args.get("use_of_selector") or "").strip()
    if not (page_url and failed_selector and use_of_selector):
        return {"candidates": [], "reason": "missing required arguments"}

    from urllib.parse import urlparse
    from ui_knowledge.models import UIPage
    from .deterministic_fill import (
        _build_merged_matching_engine,
        _fallback_probe_pages,
    )

    parsed = urlparse(page_url)
    route = parsed.path if (parsed.scheme and parsed.netloc) else page_url
    if not route.startswith("/"):
        route = f"/{route}"

    ui_page = UIPage.objects.filter(
        client=ctx.job.client, route=route, is_active=True
    ).first()
    if ui_page is None:
        for p in UIPage.objects.filter(client=ctx.job.client, is_active=True):
            if p.route.rstrip("/") == route.rstrip("/"):
                ui_page = p
                break

    # Whether the exact route matched or not, build a merged engine over
    # the primary + sibling pages so the login form (captured under a
    # different route due to redirect) is still in the corpus.
    probe_pages = _fallback_probe_pages(ctx.job, route, ui_page)
    if not probe_pages:
        return {"candidates": [], "reason": f"no ui_knowledge for client"}

    engine = _build_merged_matching_engine([p for _u, p in probe_pages])
    if engine is None or not getattr(engine, "ready", False):
        return {"candidates": [], "reason": "matching engine unavailable"}

    # Return the top-10 raw candidates with tag / text / test_id so the
    # LLM can pick the semantically correct one. We deliberately do NOT
    # apply intent-boost re-ranking here — the pre-persist deterministic
    # fill (see `apply_deterministic_fill`) tries automatic picks, but
    # the Fixer's job is to reason across candidates with the DOM in
    # front of it. Giving it a wider set of options works better than
    # forcing a single potentially-wrong pick.
    try:
        raw_results = engine.rank(failed_selector, use_of_selector, top_k=10)
    except Exception as exc:  # noqa: BLE001
        return {"candidates": [], "reason": f"rank() failed: {exc}"}

    candidates = []
    for r in raw_results:
        el = r.get("element") or {}
        canonical = str(el.get("_ui_knowledge_selector") or r.get("suggested") or "")
        candidates.append({
            "selector":  canonical,
            "score":     round(float(r.get("score") or 0.0), 3),
            "tag":       str(el.get("tag") or ""),
            "text":      str(el.get("text") or "")[:80],
            "test_id":   str((el.get("attributes") or {}).get("data-testid") or ""),
        })
    return {"candidates": candidates}


def _handle_browser_snapshot(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.tried_snapshot = True
    client = ctx.ensure_mcp()
    if client is None:
        return {"error": "Playwright MCP unavailable"}
    url = (ctx.job.base_url or "").strip()
    try:
        if url:
            client.open(url)
        snap = client.snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MCP error: {exc}"}
    ctx.last_snapshot = str(snap.get("html") or "")
    # Return a truncated preview + full length so the LLM can decide whether to search.
    trimmed = ctx.last_snapshot[:4000]
    return {
        "url": url,
        "snapshot_bytes": len(ctx.last_snapshot),
        "snapshot_preview": trimmed,
    }


def _handle_search_page_source(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "")
    if not query:
        return {"error": "query is required"}
    if not ctx.last_snapshot:
        return {"error": "no snapshot yet — call browser_snapshot first"}
    matches: List[str] = []
    for line in ctx.last_snapshot.splitlines():
        if query.lower() in line.lower():
            matches.append(line.strip()[:200])
            if len(matches) >= 20:
                break
    return {"query": query, "matches": matches, "match_count": len(matches)}


def _handle_run_ts_check(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ask the Phase-2 AST normalizer whether the artifact parses. If the sidecar
    isn't wired up yet, fall back to the current Python-side validator so the
    LLM still gets some signal.
    """
    rp = str(args.get("relative_path") or "").strip()

    # Look for an in-flight patch first (the LLM may have just called
    # write_artifact and be checking its own work before the Executor applies).
    content = ""
    for p in reversed(ctx.patches):
        if p["relative_path"] == rp:
            content = p["content"]
            break
    if not content:
        a = ctx.job.artifacts.filter(relative_path=rp).first()
        if a is None:
            return {"error": f"artifact not found: {rp!r}"}
        content = a.content_final or a.content_draft or ""

    from .artifact_validation import validate_artifact
    a = ctx.job.artifacts.filter(relative_path=rp).first()
    result = validate_artifact(
        (a.artifact_type if a else ""),
        rp,
        content,
        ctx={"seed_urls": list(ctx.job.seed_urls or [])},
    )
    return {
        "relative_path": rp,
        "is_valid": result.is_valid,
        "errors": [e.dict() for e in result.errors],
        "warnings": [w.dict() for w in result.warnings],
    }


# Phrases in the failure error message that indicate the root cause is
# likely a wrong selector / missing element (as opposed to a TS compile
# error or app crash). When we see one of these AND the LLM tries to
# `end_fix` with zero patches AND zero DOM investigation, we refuse the
# exit so the LLM has to actually look at the page.
_DOM_SHAPED_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "not visible",
    "no elements",
    "resolved to 0 elements",
    "strict mode violation",
    "waiting for locator",
    "waiting for selector",
    "expected to be visible",
)


def _is_dom_shaped_failure(error_message: str) -> bool:
    text = (error_message or "").lower()
    return any(marker in text for marker in _DOM_SHAPED_ERROR_MARKERS)


def _make_end_fix_handler(error_message: str):
    """
    Build an `end_fix` handler bound to the current iteration's error
    message. When the failure looks DOM-shaped AND the LLM is trying to
    exit with zero patches AND no deterministic-first probe was made AND
    no browser snapshot was taken — refuse. The LLM will re-attempt on
    the next turn with the guard reason as feedback.
    """
    dom_shaped = _is_dom_shaped_failure(error_message)

    def _handle(ctx: _ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
        if dom_shaped and not ctx.patches:
            if not (ctx.tried_history or ctx.tried_matching or ctx.tried_snapshot):
                return {
                    "error": (
                        "REFUSED: this failure looks selector-shaped and you're "
                        "ending without any evidence. Call history_lookup, "
                        "matching_engine_probe, or browser_snapshot at least "
                        "once before end_fix. Prefer the first two — they're "
                        "faster and don't need the browser."
                    ),
                    "hint": {
                        "tried_history":  ctx.tried_history,
                        "tried_matching": ctx.tried_matching,
                        "tried_snapshot": ctx.tried_snapshot,
                    },
                }
        ctx.diagnosis = str(args.get("diagnosis") or "").strip()
        ctx.notes = [str(n) for n in (args.get("notes") or [])]
        ctx.done = True
        return {"ok": True, "patches_queued": len(ctx.patches)}

    return _handle


_HANDLERS = {
    "list_artifacts":        _handle_list_artifacts,
    "read_artifact":         _handle_read_artifact,
    "write_artifact":        _handle_write_artifact,
    "history_lookup":        _handle_history_lookup,
    "matching_engine_probe": _handle_matching_engine_probe,
    "browser_snapshot":      _handle_browser_snapshot,
    "search_page_source":    _handle_search_page_source,
    "run_ts_check":          _handle_run_ts_check,
    # end_fix is patched in per-run by `run()` so it can see the current
    # iteration's error_message for the evidence guard.
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a QA Root-Cause Fixer. A Cucumber test just failed. Your job is "
    "to diagnose the failure and patch the artifact files so the next run "
    "passes.\n\n"
    "You have tools: list_artifacts, read_artifact, write_artifact, "
    "browser_snapshot, search_page_source, run_ts_check, end_fix.\n\n"
    "Workflow you MUST follow:\n"
    "  1. Call `list_artifacts` to see what files exist.\n"
    "  2. Call `read_artifact` on any file mentioned in the error message.\n"
    "  3. If the error is a TypeScript compile error (e.g. TS2304 'Cannot find "
    "     name X', TS2339 'Property does not exist on type'), FIX THAT FIRST. "
    "     A file that doesn't compile prevents every test from running — no "
    "     amount of DOM snapshots will help until the file loads.\n"
    "  4. When the error is a WRONG SELECTOR (timeout waiting for a locator, "
    "     `resolved to 0 elements`, `not visible`), FIRST try the fast, "
    "     deterministic tools:\n"
    "       a. `history_lookup(page_url, failed_selector, use_of_selector)` "
    "     — zero cost, returns any previously VALID heal for the same combo.\n"
    "       b. `matching_engine_probe(page_url, failed_selector, use_of_selector)` "
    "     — ranks real elements from ui_knowledge for the given URL. Its top "
    "     candidate is usually the right answer.\n"
    "     Only fall through to `browser_snapshot` when BOTH deterministic "
    "     tools return no useful candidate. Snapshot is slow and costs a "
    "     Playwright MCP round-trip; the deterministic tools are cheaper "
    "     AND read the SAME DOM ui_knowledge captured before this run.\n"
    "  5. Call `write_artifact` with the FULL new file body (no diffs).\n"
    "  6. Call `run_ts_check` on every file you wrote to verify it parses.\n"
    "  7. When every write is verified, call `end_fix` with a one-sentence "
    "     diagnosis.\n\n"
    "Rules:\n"
    "  * Patch ONLY the file(s) that actually caused the failure.\n"
    "  * If you have nothing new to try (same fix would just repeat), call "
    "    end_fix with a diagnosis explaining why and NO write_artifact calls.\n"
    "  * `content` in write_artifact MUST be the entire file body.\n"
    "  * Cucumber-JS exports only `Given` / `When` / `Then` — never `And` or `But`.\n"
    "  * Every locator string with inner quotes MUST use the other quote type "
    "    on the outside: `\"[data-testid='foo']\"`, never `'[data-testid='foo']'`.\n"
    "  * Undefined class names (TS2304) usually mean an IMPORT is missing OR a "
    "    class was never generated. Prefer using a class that IS listed by "
    "    `list_artifacts` over hallucinating a new class file.\n"
    "  * HTTP Basic Auth credentials (loaded via the browser context, invisible "
    "    to test code) are DIFFERENT from app-login credentials (typed into a "
    "    login form by the test). Never use HTTP-Basic-Auth values as the app "
    "    login email/password unless the story explicitly says so.\n"
)


def _build_initial_user_message(job: GenerationJob, *,
                                failed_scenario: Dict[str, Any],
                                failed_step: Dict[str, Any],
                                error_message: str,
                                previous_diagnoses: List[str]) -> str:
    prev_block = ""
    if previous_diagnoses:
        prev_block = (
            "\nPrevious diagnoses (already tried — do NOT repeat):\n"
            + "\n".join(f"  - {d}" for d in previous_diagnoses)
            + "\n"
        )
    return (
        f"Job feature name: {job.feature_name!r}\n"
        f"Jira issue key: {job.jira_issue_key!r}\n"
        f"App under test base URL: {job.base_url or '(not set)'}\n"
        f"Failed scenario:\n{json.dumps(failed_scenario, indent=2)}\n"
        f"Failed step:\n{json.dumps(failed_step, indent=2)}\n"
        f"Cucumber error message:\n{error_message}\n"
        f"{prev_block}\n"
        "Start by calling `list_artifacts`."
    )


def run(job: GenerationJob, *,
        failed_scenario: Dict[str, Any],
        failed_step: Dict[str, Any],
        error_message: str,
        previous_diagnoses: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Drive the tool-use loop. Returns the same shape the legacy fixer returned
    plus an extra `tool_trace` list for the Execute panel.
    """
    if not settings.OPENAI_TOOL_USE:
        raise RuntimeError(
            "tool_using_fixer.run called but OPENAI_TOOL_USE is off. "
            "Callers must check the setting first."
        )

    ctx = _ToolContext(job)
    started = time.time()
    model = _model_for("root_cause_fixer")
    client = _backend().client

    # Phase 5.4 — patch in an `end_fix` handler that knows this run's error
    # message so it can refuse zero-patch exits on DOM-shaped failures
    # where the LLM never actually looked at the page.
    handlers = dict(_HANDLERS)
    handlers["end_fix"] = _make_end_fix_handler(error_message)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_initial_user_message(
                job,
                failed_scenario=failed_scenario,
                failed_step=failed_step,
                error_message=error_message,
                previous_diagnoses=list(previous_diagnoses or []),
            ),
        },
    ]

    try:
        for turn in range(1, MAX_TURNS + 1):
            if time.time() - started > WALL_CLOCK_SECONDS:
                logger.warning("tool_using_fixer: wall-clock cap hit after turn %d", turn)
                break

            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.0,
                    timeout=90,
                )
            except Exception as exc:  # noqa: BLE001
                raise LLMBackendError(f"OpenAI tool-loop call failed: {exc}") from exc

            choice = resp.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []

            # Append assistant message so the model sees its own tool_calls
            # on the next turn.
            messages.append({
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ] or None,
            })

            if not tool_calls:
                # Assistant returned a final message. If it contains JSON with
                # patches[], accept it as a bundle for backward compat with
                # the legacy prompt path.
                text = (choice.content or "").strip()
                if text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict) and isinstance(parsed.get("patches"), list):
                            ctx.patches = parsed["patches"]
                            ctx.diagnosis = str(parsed.get("diagnosis") or "")
                            ctx.notes = list(parsed.get("notes") or [])
                    except json.JSONDecodeError:
                        pass
                break

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                handler = handlers.get(name)
                if handler is None:
                    result = {"error": f"unknown tool: {name}"}
                else:
                    try:
                        result = handler(ctx, args)
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": f"tool {name} raised: {exc}"}

                # Record trace entry
                ctx.tool_trace.append({
                    "turn": turn,
                    "tool": name,
                    "args": args,
                    "result_summary": _summarize_result(result),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result)[:6000],
                })

            if ctx.done:
                break
    finally:
        ctx.close()

    return {
        "diagnosis": ctx.diagnosis or "tool-using fixer ended without an explicit diagnosis",
        "patches": ctx.patches,
        "notes": ctx.notes,
        "tool_trace": ctx.tool_trace,
    }


def _summarize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim tool results to fit in the trace column without dumping full DOMs."""
    if not isinstance(result, dict):
        return {"repr": str(result)[:120]}
    summary: Dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = f"<{len(v)} chars>"
        elif isinstance(v, list):
            summary[k] = f"<{len(v)} items>"
        else:
            summary[k] = v
    return summary
