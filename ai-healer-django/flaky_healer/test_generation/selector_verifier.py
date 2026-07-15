"""
Selector verifier — runs after `_persist_agent_artifacts` on the Artifact stage.

Goal
----
Reduce the number of red-Execute-iteration selector-repair rounds by
verifying every locator in every generated page-object file against the
LIVE DOM (via Playwright MCP) BEFORE handing the artifacts off to Execute.

Flow (verify only, no auto-repair on failures the LLM can't fix in one round):

  1. For every PAGE_OBJECT artifact on the job:
       - Match the file to a seed URL (via `_seed_url_to_class_name`).
       - Open that URL in Playwright MCP.
       - Extract every locator string from the file (regex, no AST).
       - For each locator, run `browser_evaluate` to count matches.
       - Record hits / misses per file.

  2. If ANY misses, run a GPT-4o tool-using loop scoped to page-objects:
     tools = { list_page_objects, read_page_object, browser_snapshot,
               search_page_source, write_page_object, run_ts_check, end_verify }
     The LLM sees the miss report + live DOM and rewrites only the affected
     page-object files. Same MAX_TURNS/wall-clock discipline as the Fixer.

  3. Return a `{files_checked, files_rewritten, misses, tool_trace}` report
     that the Artifact stage attaches to `diagnostic.selector_verify_report`.

Kill switch: `SELECTOR_VERIFY_ENABLED` env var (default "on").

Never raises. On any error, returns `{ok: False, error: "..."}` so the
Artifact stage response still goes out — verification is a bonus pass, not
a gate.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

from .llm_backends import LLMBackendError, _backend, _model_for
from .models import GeneratedArtifact, GenerationJob

logger = logging.getLogger(__name__)


# Same-shape caps as tool_using_fixer — one budget across the whole verify pass.
MAX_TURNS = 15
WALL_CLOCK_SECONDS = 300         # 5 minutes; verify is faster than repair


# ---------------------------------------------------------------------------
# Locator extraction (regex-only; the AST normalizer already ran)
# ---------------------------------------------------------------------------
# Match the common Playwright locator shapes on a page-object line:
#   this.page.locator('.foo')
#   this.page.locator("[data-testid='bar']")
#   this.page.getByRole('button', { name: 'Log in' })
#   this.page.getByTestId('login-btn')
#   this.page.getByText('Welcome')
#   this.page.getByLabel('Email')
#   this.page.getByPlaceholder('email')
# Plus the class-field pattern LLMs often emit for reusable selectors:
#   readonly usernameField: string = '#username';
#   readonly loginBtn: string = "[data-testid=login]";
#   private readonly cta = '.cta';
# Each entry is (kind, compiled_pattern). Order matters because we scan for
# the more specific `getByX` first — an unqualified `.locator(` regex would
# also match `page.getByTestId('x').locator('y')` for the trailing call.
_LOCATOR_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("getByTestId",      re.compile(r"""\.getByTestId\(\s*(['"`])(?P<sel>.+?)\1""",     re.DOTALL)),
    ("getByText",        re.compile(r"""\.getByText\(\s*(['"`])(?P<sel>.+?)\1""",       re.DOTALL)),
    ("getByLabel",       re.compile(r"""\.getByLabel\(\s*(['"`])(?P<sel>.+?)\1""",      re.DOTALL)),
    ("getByPlaceholder", re.compile(r"""\.getByPlaceholder\(\s*(['"`])(?P<sel>.+?)\1""", re.DOTALL)),
    ("getByRole",        re.compile(r"""\.getByRole\(\s*(['"`])(?P<sel>.+?)\1""",       re.DOTALL)),
    ("locator",          re.compile(r"""\.locator\(\s*(['"`])(?P<sel>.+?)\1""",         re.DOTALL)),
    # Class-field selector declarations. Matches:
    #   readonly foo: string = '#bar'
    #   readonly foo = '#bar'
    #   private foo: string = '#bar'
    # Deliberately narrow — the string must look selector-shaped (starts with
    # #, ., [, /, or a Playwright helper prefix). Anything else (URL strings,
    # error messages) is ignored.
    ("field_declaration", re.compile(
        r"""(?:readonly|private|public|protected)?\s*"""
        r"""(?:readonly\s+)?[a-zA-Z_$][\w$]*"""
        r"""(?:\s*:\s*string)?"""
        r"""\s*=\s*(['"`])(?P<sel>[#\.\[/][^'"`\n]{1,300})\1""",
        re.DOTALL,
    )),
]


def _extract_locators(content: str) -> List[Dict[str, str]]:
    """
    Return [{kind, selector}, ...] for every locator call in `content`.

    Skips template literals with `${…}` interpolation — those can't be probed
    without evaluating the surrounding code.
    """
    if not content:
        return []
    hits: List[Dict[str, str]] = []
    for kind, pat in _LOCATOR_PATTERNS:
        for m in pat.finditer(content):
            sel = m.group("sel")
            if "${" in sel:
                continue
            hits.append({"kind": kind, "selector": sel})
    # De-dupe by (kind, selector) preserving order.
    seen = set()
    unique = []
    for h in hits:
        key = (h["kind"], h["selector"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique


# ---------------------------------------------------------------------------
# Seed URL resolution
# ---------------------------------------------------------------------------
def _seed_url_for_page_object(
    job: GenerationJob, artifact: GeneratedArtifact
) -> Optional[str]:
    """
    Match a PAGE_OBJECT artifact to the seed URL whose derived class name
    matches the file's class name. Falls back to the job's base_url.
    """
    from .agents import _seed_url_to_class_name  # local import — circular safety

    class_name = _class_name_from_artifact_path(artifact.relative_path)
    if not class_name:
        return job.base_url or None

    urls: List[str] = []
    if job.base_url:
        urls.append(job.base_url)
    urls.extend(job.seed_urls or [])
    if not urls:
        return None
    for u in urls:
        if _seed_url_to_class_name(u) == class_name:
            # If the URL is relative, prepend base_url.
            if u.startswith("/") and job.base_url:
                return job.base_url.rstrip("/") + u
            return u
    return job.base_url or urls[0]


def _class_name_from_artifact_path(rel_path: str) -> str:
    if not rel_path:
        return ""
    tail = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    if tail.endswith(".ts"):
        tail = tail[:-3]
    return "".join(ch for ch in tail if ch.isalnum() or ch == "_")


# ---------------------------------------------------------------------------
# MCP-driven locator probe
# ---------------------------------------------------------------------------
def _probe_selectors(
    mcp,
    url: str,
    locators: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """
    For each locator, ask the browser how many elements resolve. Returns
    `[{kind, selector, count, ok, error?}, ...]` in input order.

    Uses `browser_evaluate` MCP tool — we build a small JS snippet per locator
    that maps kind→page.locator invocation and returns its `.count()`.
    """
    if mcp is None:
        return [{**loc, "count": -1, "ok": False, "error": "MCP unavailable"} for loc in locators]

    try:
        mcp.open(url)
    except Exception as exc:  # noqa: BLE001
        return [{**loc, "count": -1, "ok": False, "error": f"navigate failed: {exc}"} for loc in locators]

    results: List[Dict[str, Any]] = []
    for loc in locators:
        js = _build_probe_snippet(loc["kind"], loc["selector"])
        try:
            resp = mcp._tool_call("browser_evaluate", {"function": js})
        except Exception as exc:  # noqa: BLE001
            results.append({**loc, "count": -1, "ok": False, "error": f"eval failed: {exc}"})
            continue
        count = _parse_eval_count(resp)
        results.append({
            **loc,
            "count": count,
            "ok": count >= 1,
            "error": "" if count >= 1 else "no matches",
        })
    return results


def _build_probe_snippet(kind: str, selector: str) -> str:
    """
    Build a `async ({ page }) => …` snippet that returns the locator's count.
    Playwright MCP's `browser_evaluate` expects a function body it can call.
    """
    # Escape the selector for embedding in a JS string literal.
    esc = selector.replace("\\", "\\\\").replace("`", "\\`")
    if kind == "getByTestId":
        expr = f"page.getByTestId(`{esc}`)"
    elif kind == "getByText":
        expr = f"page.getByText(`{esc}`)"
    elif kind == "getByLabel":
        expr = f"page.getByLabel(`{esc}`)"
    elif kind == "getByPlaceholder":
        expr = f"page.getByPlaceholder(`{esc}`)"
    elif kind == "getByRole":
        expr = f"page.getByRole(`{esc}`)"
    else:
        expr = f"page.locator(`{esc}`)"
    return f"async ({{ page }}) => {{ try {{ return await {expr}.count(); }} catch (e) {{ return -1; }} }}"


def _parse_eval_count(resp: Any) -> int:
    """
    Playwright MCP wraps evaluate output in content[0].text as JSON-ish
    text. Real payloads look like:

        - Ran Playwright code:
          await page.locator('#signInName').count()
        - Result: 0

    Historical bug: `re.search(r"-?\\d+")` grabbed the FIRST integer,
    which for the MCP-2026 session preamble is `-2026` (a session id),
    not the actual count. Fix: prefer the `Result: <number>` marker;
    fall back to the LAST integer in the payload if the marker is
    missing (older MCP versions).
    """
    if not isinstance(resp, dict):
        return -1
    for item in resp.get("content") or []:
        text = str((item or {}).get("text") or "").strip()
        if not text:
            continue
        # Preferred: the explicit `- Result: <n>` line.
        m = re.search(r"Result:\s*(-?\d+)", text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        # Fallback: the LAST integer in the payload (whatever the
        # evaluate returned lands at the tail).
        nums = re.findall(r"-?\d+", text)
        if nums:
            try:
                return int(nums[-1])
            except ValueError:
                return -1
    return -1


# ---------------------------------------------------------------------------
# Tool-using LLM loop (only invoked if there are misses)
# ---------------------------------------------------------------------------
_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_page_objects",
            "description": (
                "List every PAGE_OBJECT artifact on the job with its miss "
                "report — the selectors that did NOT resolve on the live page."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page_object",
            "description": "Return the current content of a page-object file.",
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
            "name": "browser_snapshot",
            "description": (
                "Fetch the live DOM (accessibility-tree text) for the seed URL "
                "mapped to the page-object you're currently repairing."
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
            "name": "search_page_source",
            "description": (
                "Grep for `query` in the most-recent browser_snapshot output. "
                "Use to confirm text or role names exist before writing a locator."
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
            "name": "write_page_object",
            "description": (
                "Overwrite a page-object file. `content` MUST be the full "
                "new body — no diffs, no ellipses. Only writes to files under "
                "tests/pages/generated/…"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content":       {"type": "string"},
                    "reason":        {"type": "string"},
                },
                "required": ["relative_path", "content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_ts_check",
            "description": "Ask the AST normalizer to parse a page-object and report any TS errors.",
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
            "name": "end_verify",
            "description": (
                "Signal that verification is complete. `summary` is a one-line "
                "recap. Call this when every miss is either fixed or you're "
                "confident no further edit will help."
            ),
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


_SYSTEM_PROMPT = (
    "You are a QA Selector Verifier. A page-object file was generated but "
    "one or more locators DID NOT resolve to any element on the live page. "
    "Your job is to fix the locators so they match the actual DOM.\n\n"
    "You have tools: list_page_objects, read_page_object, browser_snapshot, "
    "search_page_source, write_page_object, run_ts_check, end_verify.\n\n"
    "Workflow:\n"
    "  1. Call `list_page_objects` — you'll see each PAGE_OBJECT with its "
    "     miss report (kind, selector, count, error).\n"
    "  2. For each file with misses: `read_page_object` to see the current "
    "     code, then `browser_snapshot` to see the live DOM for its seed URL.\n"
    "  3. `search_page_source` to confirm the text/role/attribute you plan "
    "     to target actually exists on the page.\n"
    "  4. Call `write_page_object` with the FULL new file body. Only rewrite "
    "     the locators that missed — leave every hitting locator untouched.\n"
    "  5. Call `run_ts_check` to verify your patch compiles.\n"
    "  6. When every file's misses are resolved (or you have no better "
    "     idea), call `end_verify`.\n\n"
    "Rules:\n"
    "  * Only edit files under `tests/pages/generated/…`. Never touch "
    "    step-defs, features, or specs.\n"
    "  * Prefer `getByRole` / `getByLabel` / `getByTestId` over raw CSS "
    "    when the live DOM exposes stable text or ARIA attributes.\n"
    "  * Don't invent selectors — every one you write MUST correspond to "
    "    something you saw in `browser_snapshot` or `search_page_source`.\n"
    "  * A locator with `${...}` interpolation is skipped by probe — don't "
    "    rewrite it unless you're confident.\n"
)


class _Ctx:
    def __init__(self, job: GenerationJob, mcp,
                 file_reports: Dict[str, Dict[str, Any]]):
        self.job = job
        self.mcp = mcp
        self.file_reports = file_reports    # rel_path → { url, locators, misses }
        self.snapshots: Dict[str, str] = {}  # rel_path → last snapshot text
        self.last_snapshot_key: Optional[str] = None
        self.patches: List[Dict[str, str]] = []
        self.tool_trace: List[Dict[str, Any]] = []
        self.summary: str = ""
        self.done: bool = False


def _h_list_page_objects(ctx: _Ctx, _args) -> Dict[str, Any]:
    rows = []
    for rp, r in ctx.file_reports.items():
        rows.append({
            "relative_path": rp,
            "seed_url":      r.get("url"),
            "locator_count": len(r.get("locators") or []),
            "misses":        [
                {"kind": m["kind"], "selector": m["selector"], "count": m["count"]}
                for m in (r.get("misses") or [])
            ],
        })
    return {"page_objects": rows}


def _h_read_page_object(ctx: _Ctx, args) -> Dict[str, Any]:
    rp = str(args.get("relative_path") or "").strip()
    a = ctx.job.artifacts.filter(
        relative_path=rp,
        artifact_type=GeneratedArtifact.TYPE_PAGE_OBJECT,
    ).first()
    if a is None:
        return {"error": f"page-object not found: {rp!r}"}
    return {"relative_path": rp, "content": a.content_final or a.content_draft or ""}


def _h_browser_snapshot(ctx: _Ctx, args) -> Dict[str, Any]:
    rp = str(args.get("relative_path") or "").strip()
    report = ctx.file_reports.get(rp) or {}
    url = report.get("url")
    if not url:
        return {"error": f"no seed URL mapped for {rp!r}"}
    if ctx.mcp is None:
        return {"error": "Playwright MCP unavailable"}
    try:
        ctx.mcp.open(url)
        snap = ctx.mcp.snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MCP error: {exc}"}
    text = str(snap.get("html") or "")
    ctx.snapshots[rp] = text
    ctx.last_snapshot_key = rp
    return {"relative_path": rp, "url": url, "snapshot_bytes": len(text), "snapshot_preview": text[:4000]}


def _h_search_page_source(ctx: _Ctx, args) -> Dict[str, Any]:
    query = str(args.get("query") or "")
    if not query:
        return {"error": "query is required"}
    key = ctx.last_snapshot_key
    if key is None or key not in ctx.snapshots:
        return {"error": "no snapshot yet — call browser_snapshot first"}
    text = ctx.snapshots[key]
    matches: List[str] = []
    for line in text.splitlines():
        if query.lower() in line.lower():
            matches.append(line.strip()[:200])
            if len(matches) >= 20:
                break
    return {"query": query, "matches": matches, "match_count": len(matches)}


def _h_write_page_object(ctx: _Ctx, args) -> Dict[str, Any]:
    rp = str(args.get("relative_path") or "").strip()
    content = str(args.get("content") or "")
    reason = str(args.get("reason") or "")
    if not rp or not content:
        return {"error": "relative_path and content are required"}
    if "tests/pages/generated/" not in rp.replace("\\", "/"):
        return {"error": "may only write files under tests/pages/generated/"}
    ctx.patches.append({"relative_path": rp, "content": content, "reason": reason})
    return {"ok": True, "queued_for_apply": True, "reason": reason[:200]}


def _h_run_ts_check(ctx: _Ctx, args) -> Dict[str, Any]:
    from .artifact_validation import validate_artifact
    rp = str(args.get("relative_path") or "").strip()
    content = ""
    for p in reversed(ctx.patches):
        if p["relative_path"] == rp:
            content = p["content"]
            break
    if not content:
        a = ctx.job.artifacts.filter(relative_path=rp).first()
        if a is None:
            return {"error": f"not found: {rp!r}"}
        content = a.content_final or a.content_draft or ""
    a = ctx.job.artifacts.filter(relative_path=rp).first()
    result = validate_artifact(
        (a.artifact_type if a else GeneratedArtifact.TYPE_PAGE_OBJECT),
        rp,
        content,
        ctx={"seed_urls": list(ctx.job.seed_urls or [])},
    )
    return {
        "relative_path": rp,
        "is_valid": result.is_valid,
        "errors":  [e.dict() for e in result.errors],
        "warnings": [w.dict() for w in result.warnings],
    }


def _h_end_verify(ctx: _Ctx, args) -> Dict[str, Any]:
    ctx.summary = str(args.get("summary") or "").strip()
    ctx.done = True
    return {"ok": True, "patches_queued": len(ctx.patches)}


_HANDLERS = {
    "list_page_objects":  _h_list_page_objects,
    "read_page_object":   _h_read_page_object,
    "browser_snapshot":   _h_browser_snapshot,
    "search_page_source": _h_search_page_source,
    "write_page_object":  _h_write_page_object,
    "run_ts_check":       _h_run_ts_check,
    "end_verify":         _h_end_verify,
}


def _run_llm_repair(ctx: _Ctx) -> None:
    """Drive the tool-using loop. Fills ctx.patches / ctx.summary in place."""
    model = _model_for("selector_verifier")
    client = _backend().client

    initial_user = (
        f"Job: {ctx.job.feature_name!r}\n"
        f"Base URL: {ctx.job.base_url or '(not set)'}\n"
        "The verifier found selectors that don't resolve on the live page. "
        "Start by calling `list_page_objects`."
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": initial_user},
    ]

    started = time.time()
    for turn in range(1, MAX_TURNS + 1):
        if time.time() - started > WALL_CLOCK_SECONDS:
            logger.warning("selector_verifier: wall-clock cap hit after turn %d", turn)
            break
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                temperature=0.0,
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMBackendError(f"OpenAI verifier call failed: {exc}") from exc

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
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
            break

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            handler = _HANDLERS.get(name)
            if handler is None:
                result = {"error": f"unknown tool {name!r}"}
            else:
                try:
                    result = handler(ctx, args)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"tool {name!r} crashed: {exc}"}
            ctx.tool_trace.append({
                "turn": turn,
                "tool": name,
                "args": {k: (v if len(str(v)) < 300 else str(v)[:300] + "…") for k, v in args.items()},
                "result_bytes": len(json.dumps(result, default=str)),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": json.dumps(result, default=str),
            })

        if ctx.done:
            break


def _apply_patches(job: GenerationJob, patches: List[Dict[str, str]]) -> int:
    """Persist patched page-object content back onto GeneratedArtifact rows."""
    from .generation_service import _sha256
    applied = 0
    for p in patches:
        a = job.artifacts.filter(
            relative_path=p["relative_path"],
            artifact_type=GeneratedArtifact.TYPE_PAGE_OBJECT,
        ).first()
        if a is None:
            continue
        a.content_final = p["content"]
        a.checksum = _sha256(p["content"])
        a.save(update_fields=["content_final", "checksum"])
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def verify(job: GenerationJob) -> Dict[str, Any]:
    """
    Run the verifier over every PAGE_OBJECT artifact on `job`.

    Return shape:
        {
          "enabled":            bool,
          "files_checked":      int,
          "files_with_misses":  int,
          "files_rewritten":    int,
          "per_file": [
            {"relative_path": ..., "seed_url": ..., "locator_count": ...,
             "hits": [...], "misses": [...]}
          ],
          "tool_trace":         [...],
          "summary":            str,
          "error":              str  (only present on failure),
        }
    """
    enabled_raw = str(getattr(settings, "SELECTOR_VERIFY_ENABLED", "on")).lower()
    if enabled_raw not in ("on", "1", "true", "yes"):
        return {"enabled": False, "files_checked": 0}

    from .mcp_playwright import playwright_mcp_client

    po_artifacts: List[GeneratedArtifact] = list(
        job.artifacts.filter(artifact_type=GeneratedArtifact.TYPE_PAGE_OBJECT)
    )
    if not po_artifacts:
        return {"enabled": True, "files_checked": 0, "files_rewritten": 0}

    file_reports: Dict[str, Dict[str, Any]] = {}
    with playwright_mcp_client() as mcp:
        for a in po_artifacts:
            url = _seed_url_for_page_object(job, a)
            content = a.content_final or a.content_draft or ""
            locators = _extract_locators(content)
            if not url or not locators:
                file_reports[a.relative_path] = {
                    "url": url,
                    "locators": locators,
                    "probed": [],
                    "misses": [],
                }
                continue
            probed = _probe_selectors(mcp, url, locators)
            misses = [p for p in probed if not p.get("ok")]
            file_reports[a.relative_path] = {
                "url": url,
                "locators": locators,
                "probed": probed,
                "misses": misses,
            }

        files_with_misses = [rp for rp, r in file_reports.items() if r["misses"]]
        rewritten = 0
        tool_trace: List[Dict[str, Any]] = []
        summary = ""

        if files_with_misses:
            ctx = _Ctx(job, mcp, file_reports)
            try:
                _run_llm_repair(ctx)
            except LLMBackendError as exc:
                logger.warning("selector_verifier: LLM repair failed: %s", exc)
                return {
                    "enabled": True,
                    "files_checked": len(po_artifacts),
                    "files_with_misses": len(files_with_misses),
                    "files_rewritten": 0,
                    "per_file": _per_file_summary(file_reports),
                    "tool_trace": [],
                    "summary": "",
                    "error": str(exc),
                }
            rewritten = _apply_patches(job, ctx.patches)
            tool_trace = ctx.tool_trace
            summary = ctx.summary

    return {
        "enabled":           True,
        "files_checked":     len(po_artifacts),
        "files_with_misses": len(files_with_misses),
        "files_rewritten":   rewritten,
        "per_file":          _per_file_summary(file_reports),
        "tool_trace":        tool_trace,
        "summary":           summary,
    }


def _per_file_summary(file_reports: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for rp, r in file_reports.items():
        probed = r.get("probed") or []
        hits   = [p for p in probed if p.get("ok")]
        misses = [p for p in probed if not p.get("ok")]
        out.append({
            "relative_path": rp,
            "seed_url":      r.get("url"),
            "locator_count": len(r.get("locators") or []),
            "hit_count":     len(hits),
            "miss_count":    len(misses),
            "misses":        [
                {"kind": m["kind"], "selector": m["selector"], "count": m.get("count")}
                for m in misses
            ],
        })
    return out
