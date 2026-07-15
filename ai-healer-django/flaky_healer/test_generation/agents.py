"""
Phase 6 — six-agent generation pipeline.

Each agent function:
    - Reads state off a `GenerationJob` row (its inputs are the outputs of
      the previous stage, plus optional `Clients` context).
    - Builds one prompt and calls the shared `_call_ollama_json` helper from
      `generation_service`.
    - Writes its output back onto the job (`stage_*_output` field).
    - Records an audit entry on `stage_history`.
    - Sets `job.stage` to the corresponding STAGE_ constant.
    - Returns the raw agent output dict.

Every agent is idempotent — re-running the same agent regenerates its output
and appends a new entry to `stage_history`, but does NOT advance the stage
until the operator explicitly approves via the endpoint (see views.py).

The functions here are pure business logic; they don't touch HTTP. They can be
invoked from the Django shell for smoke-testing:

    from test_generation.models import GenerationJob
    from test_generation import agents
    job = GenerationJob.objects.get(job_id="…")
    agents.run_feature_agent(job)
    print(job.stage_feature_output)

For the Executor + Root-Cause Fixer, see `executor.py` (adds subprocess
orchestration on top of the LLM call).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from django.db import transaction

from .models import GenerationJob
from .generation_service import (
    _default_test_gen_model,
    _llm_timeout,
    _slug,
)
from .llm_backends import (
    LLMBackendError,
    LLMResult,
    pick_backend,
    tenant_config_for_job,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _record_stage(job: GenerationJob, *, agent: str, notes: str = "", decision: str = "generated") -> None:
    """Append an audit entry onto `stage_history` and persist."""
    entry = {
        "stage": job.stage,
        "agent": agent,
        "recorded_on": _now_iso(),
        "decision": decision,
        "notes": notes,
    }
    history = list(job.stage_history or [])
    history.append(entry)
    job.stage_history = history


def _run_llm(*, prompt: str, num_predict: int, temperature: float = 0.0,
             job: Optional[GenerationJob] = None,
             agent: str = "feature_author",
             system: str = "") -> Dict[str, Any]:
    """
    Route the LLM call through the configured backend for this agent.

    `agent` is the routing key — one of `feature_author`, `manual_test_author`,
    `plan_architect`, `artifact_generator`, `root_cause_fixer`. Model choice
    per agent lives in `llm_backends.DEFAULT_MODELS`, overridable per-tenant.

    Backwards-compat: existing call sites that don't yet pass `agent` default to
    `feature_author` (cheapest model). Migrate them to pass the real agent name
    so cost + quality routing works correctly.

    `system` may carry the stable schema/rules block for prompt-cache friendliness;
    if omitted the entire prompt goes as the user message.
    """
    temp = temperature if job is None else float(job.llm_temperature or temperature)

    backend, model = pick_backend(agent, tenant_config_for_job(job) if job else None)
    try:
        result: LLMResult = backend.call(
            system=system or _DEFAULT_SYSTEM_MESSAGE,
            user=prompt,
            model=model,
            temperature=temp,
            max_output_tokens=num_predict,
            timeout_seconds=_llm_timeout(),
        )
    except LLMBackendError as exc:
        # Surface OpenAI failures as a stage_history entry so the operator can
        # see WHY the pipeline halted, then re-raise so run_and_repair / the
        # panel treat it as a normal stage-run failure.
        if job is not None:
            _record_stage(
                job, agent=agent, decision="llm_error",
                notes=f"OpenAI call failed: {exc}",
            )
            job.save(update_fields=["stage_history", "last_modified"])
        raise

    # Stash usage on the job's stage_history so we can bill/audit later.
    if job is not None:
        _record_stage(
            job, agent=agent, decision="llm_call",
            notes=(
                f"model={result.model} "
                f"input={result.usage.get('input_tokens', 0)} "
                f"cached={result.usage.get('cached_input_tokens', 0)} "
                f"output={result.usage.get('output_tokens', 0)} "
                f"ms={result.usage.get('wall_clock_ms', 0)}"
            ),
        )
        # Also stash the raw usage dict on the LAST history entry so the panel
        # can render it without regex-parsing the notes string.
        try:
            history = list(job.stage_history or [])
            if history:
                history[-1]["llm_usage"] = result.usage
                history[-1]["model"] = result.model
                job.stage_history = history
        except Exception:  # noqa: BLE001
            pass
        job.save(update_fields=["stage_history", "last_modified"])

    return result.payload


_DEFAULT_SYSTEM_MESSAGE = (
    "You are a QA automation agent. Follow the schema and rules exactly. "
    "Return STRICT JSON only — no prose, no markdown fences."
)


# ---------------------------------------------------------------------------
# Agent 1 — Feature Author
# ---------------------------------------------------------------------------
def _credential_hints(job: GenerationJob) -> Dict[str, str]:
    """
    Resolve the literal login username + password from a job's preconditions.

    Jira stories express test credentials in several shapes. This helper walks
    the well-known ones in priority order and returns the first non-empty
    literal it finds. Empty strings when nothing is set — callers should treat
    those as "let the runtime env-var fallback resolve at Cucumber time".
    """
    pre: Dict[str, Any] = job.preconditions or {}
    if not isinstance(pre, dict):
        return {"username": "", "password": ""}

    def _first_str(*paths: str) -> str:
        for path in paths:
            cursor: Any = pre
            for key in path.split("."):
                if not isinstance(cursor, dict):
                    cursor = None
                    break
                cursor = cursor.get(key)
            if isinstance(cursor, str) and cursor.strip():
                return cursor.strip()
        return ""

    return {
        "username": _first_str(
            "email",
            "username",
            "login.email",
            "login.username",
            "credentials.username",
            "credentials.email",
            "http_basic.username",
        ),
        "password": _first_str(
            "password",
            "login.password",
            "credentials.password",
            "http_basic.password",
        ),
    }


def _build_feature_author_prompt(jira_summary: str, jira_description: str,
                                 base_url: str, jira_key: str) -> str:
    return (
        "You are a QA Feature Author. Read the Jira ticket below and produce a\n"
        "concise, testable feature specification. Return STRICT JSON only.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "title":                "<one line feature title>",\n'
        '  "description":          "<2-4 sentences describing the feature>",\n'
        '  "acceptance_criteria":  ["<criterion 1>", "<criterion 2>", ...],\n'
        '  "seed_urls":            ["<url or path 1>", "<url or path 2>", ...],\n'
        '  "preconditions":        {\n'
        '     "http_basic": {"username": "…", "password": "…"},\n'
        '     "<other_key>": "<string value the tests need at runtime>"\n'
        "  },\n"
        '  "notes":                ["<any short remark for reviewers>"]\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- acceptance_criteria: 3-7 items. Each MUST be a checkable behavior\n"
        "  (not implementation detail). Prefer 'Given/When/Then' phrasing but\n"
        "  keep them one-liners here — full Gherkin comes in later agents.\n"
        "- seed_urls: the FIRST entry MUST be the FULL app-under-test URL\n"
        "  extracted verbatim from the ticket (e.g. the URL after phrases like\n"
        "  'Base URL:', 'Environment:', 'Test on:', or the first https:// link\n"
        "  clearly referring to the AUT). If the ticket contains no such URL,\n"
        "  emit no full URL — leave that decision to the reviewer.\n"
        "  Every SUBSEQUENT entry is a relative path (`/`, `/login`, `/cart`)\n"
        "  the tests will need to navigate — one per acceptance criterion that\n"
        "  requires navigation.\n"
        "- preconditions: extract runtime setup values from any 'Preconditions'\n"
        "  or 'Environment' block in the ticket. Recognised patterns:\n"
        "    * 'HTTP Basic Auth ... username: X, password: Y'\n"
        "      or 'Basic Auth: X / Y'\n"
        "      → put in `preconditions.http_basic = {username, password}`.\n"
        "    * 'Email: X' or 'Username: X' or 'Login: X'\n"
        "      → put in `preconditions.email = \"X\"` (top-level string).\n"
        "    * 'Password: Y' or 'Passwd: Y' or 'Pwd: Y'\n"
        "      → put in `preconditions.password = \"Y\"` (top-level string).\n"
        "      These are the LOGIN credentials the generated .feature will\n"
        "      embed. Never emit placeholders like 'valid_username' — copy the\n"
        "      literal value from the ticket.\n"
        "    * 'Age-gate year to select: 1990' → `preconditions.seed_year: \"1990\"`.\n"
        "    * Any other 'X: value' pair the tests will need — put as a top-level\n"
        "      string key. Never invent values; only extract what's literally in\n"
        "      the ticket. Return `preconditions: {}` if the ticket has none.\n"
        "- Base your output ONLY on the Jira content below. Do not invent\n"
        "  requirements that aren't implied by the ticket.\n"
        "\n"
        f"Jira issue key: {jira_key}\n"
        f"Ticket summary: {jira_summary}\n"
        f"Ticket description:\n{jira_description}\n"
    )


@transaction.atomic
def run_feature_agent(job: GenerationJob, *,
                      jira_summary: str = "",
                      jira_description: str = "") -> Dict[str, Any]:
    """Agent 1 — turn a Jira ticket into a feature spec.

    Reads `job.feature_name`, `job.feature_description`, `job.jira_issue_key`,
    `job.base_url` by default. When invoked from an HTTP endpoint the caller
    may pass fresh `jira_summary` / `jira_description` fetched from Jira live.
    """
    summary = jira_summary or job.feature_name or ""
    description = jira_description or job.feature_description or ""
    prompt = _build_feature_author_prompt(
        jira_summary=summary,
        jira_description=description,
        base_url=job.base_url or "",
        jira_key=job.jira_issue_key or "",
    )
    output = _run_llm(prompt=prompt, num_predict=900, job=job, agent="feature_author")

    # We deliberately DO NOT persist output.base_url or output.seed_urls to
    # the job here — the operator gets to review + edit those on the Feature
    # Review panel first. The Approve endpoint splits the "Seed URLs" field
    # into `job.base_url` (first absolute URL) + `job.seed_urls` (relative
    # paths). See StageFeatureApproveView.post() in views.py.

    job.stage_feature_output = output
    job.stage = GenerationJob.STAGE_FEATURE
    _record_stage(
        job, agent="feature_author",
        notes=f"Produced {len(output.get('acceptance_criteria') or [])} acceptance criteria.",
    )
    job.save(update_fields=["stage_feature_output", "stage", "stage_history", "last_modified"])
    return output


# ---------------------------------------------------------------------------
# Agent 2 — Manual Test Author
# ---------------------------------------------------------------------------
def _build_manual_tests_prompt(feature: Dict[str, Any],
                               seed_urls: Optional[List[str]] = None,
                               jira_description: str = "") -> str:
    seeds_line = ""
    if seed_urls:
        # The first entry is the reviewer-approved AUT URL; the rest are
        # relative paths. Give both to the LLM as reference — the first
        # `given` should navigate to the first entry.
        seeds_line = (
            f"Reviewer-approved navigation targets (in order):\n"
            + "\n".join(f"  {i + 1}. {u}" for i, u in enumerate(seed_urls))
            + "\nThe first `given` clause MUST anchor the test to entry #1.\n\n"
        )
    # The raw Jira description is the source of truth for UI-element names.
    # The Feature Author's paraphrase can drift ("user icon" → "logo"), so we
    # give the Manual Test Author both signals and forbid paraphrasing.
    description_block = ""
    if (jira_description or "").strip():
        description_block = (
            "Original Jira description (source of truth for UI-element wording):\n"
            "```\n"
            + jira_description.strip()
            + "\n```\n\n"
        )
    return (
        "You are a QA Manual Test Author. Given the feature spec below,\n"
        "produce EXACTLY ONE end-to-end manual test case that walks the full\n"
        "happy path from start to finish. Every acceptance criterion MUST be\n"
        "covered inside this single test.\n"
        f"{seeds_line}"
        "Return STRICT JSON only.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "manual_tests": [\n'
        "    {\n"
        '      "id":     "MT-1",\n'
        '      "title":  "<one line title summarising the full user journey>",\n'
        '      "type":   "SMOKE",\n'
        '      "given":  ["<precondition 1>", "<precondition 2>", ...],\n'
        '      "when":   ["<action 1>", "<action 2>", "<action 3>", ...],\n'
        '      "then":   ["<expectation 1>", "<expectation 2>", ...]\n'
        "    }\n"
        "  ],\n"
        '  "notes": ["short reviewer note"]\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- manual_tests MUST contain EXACTLY ONE entry. Do NOT split the\n"
        "  feature into multiple test cases — the whole story is one journey.\n"
        "- id: always \"MT-1\".\n"
        "- type: always \"SMOKE\" (the single test is the happy-path walk).\n"
        "- when: enumerate the ordered actions covering every acceptance\n"
        "  criterion (accept cookies → age gate year → confirm age → click\n"
        "  login, etc). More entries in `when` are BETTER than more tests.\n"
        "- given/then: one clause per array entry. Plain English, no code,\n"
        "  no selectors. A tester without dev tools should be able to follow.\n"
        "- Do NOT emit a NEGATIVE or REGRESSION variant here. Negative paths\n"
        "  belong in a separate story / job.\n"
        "- PRESERVE UI-ELEMENT WORDING VERBATIM. If the Jira description\n"
        "  says 'user icon in the top-right header', the manual test MUST say\n"
        "  'user icon' — never paraphrase to 'logo', 'avatar', 'profile\n"
        "  picture', 'button', etc. Copy the exact noun phrase the ticket\n"
        "  uses. If the phrase is ambiguous, copy it literally rather than\n"
        "  resolving. Same for control names ('birth-year dropdown',\n"
        "  'accept-cookies button', etc.).\n"
        "\n"
        f"{description_block}"
        f"Feature spec:\n{json.dumps(feature, indent=2)}\n"
    )


@transaction.atomic
def run_manual_tests_agent(job: GenerationJob) -> Dict[str, Any]:
    """Agent 2 — derive manual test cases from the (approved) feature spec.

    Enforces a single end-to-end test per story: if the LLM emits multiple
    entries (older prompt cadence, retry, or partial JSON) we collapse them
    into one so downstream stages see the single-scenario shape they expect.
    """
    if not job.stage_feature_output:
        raise ValueError("Feature output missing — run agent 1 (feature_author) first.")

    # Reviewer-approved Seed URLs are the single source of truth for what the
    # tests navigate. The Feature Approve endpoint already split the first
    # absolute entry into job.base_url + the relative paths into job.seed_urls
    # — reconstruct the ordered list for the prompt.
    reviewer_seed_urls: List[str] = []
    if job.base_url:
        reviewer_seed_urls.append(job.base_url)
    reviewer_seed_urls.extend(job.seed_urls or [])
    prompt = _build_manual_tests_prompt(
        job.stage_feature_output,
        seed_urls=reviewer_seed_urls,
        jira_description=job.feature_description or "",
    )
    output = _run_llm(prompt=prompt, num_predict=1400, job=job, agent="manual_test_author")
    output = _coerce_single_manual_test(output)

    job.stage_manual_tests_output = output
    job.stage = GenerationJob.STAGE_MANUAL_TESTS
    _record_stage(job, agent="manual_test_author",
                  notes=f"Produced {len(output.get('manual_tests') or [])} manual test cases.")
    job.save(update_fields=["stage_manual_tests_output", "stage", "stage_history", "last_modified"])
    return output


def _coerce_single_manual_test(output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce: exactly one MT-1 test per feature. If the LLM emitted several
    entries, merge their steps into one long end-to-end walk so we never lose
    coverage — the operator can still edit in Review.
    """
    if not isinstance(output, dict):
        return output
    tests = output.get("manual_tests") or []
    if not isinstance(tests, list) or len(tests) <= 1:
        return output

    merged_given, merged_when, merged_then = [], [], []
    merged_title_parts = []
    for t in tests:
        if not isinstance(t, dict):
            continue
        if t.get("title"):
            merged_title_parts.append(str(t["title"]))
        for clause in t.get("given") or []:
            if clause and str(clause) not in merged_given:
                merged_given.append(str(clause))
        for clause in t.get("when") or []:
            if clause:
                merged_when.append(str(clause))
        for clause in t.get("then") or []:
            if clause and str(clause) not in merged_then:
                merged_then.append(str(clause))

    single = {
        "id": "MT-1",
        "title": " → ".join(merged_title_parts[:3]) or "End-to-end journey",
        "type": "SMOKE",
        "given": merged_given,
        "when": merged_when,
        "then": merged_then,
    }
    notes = list(output.get("notes") or [])
    notes.append(
        f"Collapsed {len(tests)} LLM-emitted tests into one end-to-end SMOKE walk."
    )
    return {"manual_tests": [single], "notes": notes}


# ---------------------------------------------------------------------------
# Agent 3 — Plan Architect
# ---------------------------------------------------------------------------
def _build_plan_prompt(feature: Dict[str, Any], manual_tests: Dict[str, Any],
                       selector_map: Dict[str, str], intent_keys: List[str],
                       seed_urls: Optional[List[str]] = None,
                       ground_truth_block: str = "",
                       has_ground_truth: bool = False,
                       has_base_url: bool = False,
                       credential_hints: Optional[Dict[str, str]] = None) -> str:
    # When ui_knowledge is empty we used to tell the LLM to invent selectors
    # so the plan wasn't dropped. That produced hallucinated locators like
    # `[data-testid="logo-image"]` for tenants where a real snapshot is just
    # missing. Now the fallback is narrower:
    #   * has_ground_truth  → force ground-truth table (strictest).
    #   * selector_map only → force selector_map values.
    #   * no base_url yet   → onboarding path, keep the old "invent" fallback.
    #   * base_url set but empty selector_map + no ground truth → NEW: emit
    #     empty selectors + a loud note. The Artifact stage will 409 before
    #     the LLM ever emits page-objects (Phase 9.7), so this branch acts
    #     as an "operator, run ui_knowledge sync" signal, not a silent guess.
    if has_ground_truth:
        selector_rule = (
            "- Every selector in `step.selector` MUST be one of the strings\n"
            "  from the GROUND-TRUTH LOCATORS table below (copy verbatim).\n"
            "  Do NOT invent selectors like '#login-button' or '#usernameInput'\n"
            "  — the pipeline verifies every selector against the live DOM and\n"
            "  rejects hallucinated ones.\n"
        )
    elif bool(selector_map):
        selector_rule = (
            "- Every selector in `step.selector` MUST appear as a value in the\n"
            "  provided selector_map. Do NOT invent selectors.\n"
        )
    elif has_base_url:
        selector_rule = (
            "- No selector data is available for this tenant's seed URLs.\n"
            "  Emit `selector: \"\"` on every step and push a note into\n"
            "  `notes` demanding the operator re-run ui_knowledge sync.\n"
            "  Do NOT invent selectors — the Artifact stage will refuse to\n"
            "  generate code from invented plans.\n"
        )
    else:
        selector_rule = (
            "- The selector_map is empty and no base URL is set (brand-new\n"
            "  tenant onboarding). Invent best-guess CSS or accessible\n"
            "  selectors (e.g. `#login-button`, `text=Sign in`,\n"
            "  `[data-testid=\"cta\"]`). Add a note in `notes` warning the\n"
            "  operator that selectors need review.\n"
        )
    credential_hints = credential_hints or {"username": "", "password": ""}
    has_creds = bool(credential_hints.get("username") or credential_hints.get("password"))
    credential_rule = (
        "- When a step is 'enter username', 'type email', 'fill password',\n"
        "  or a similar login-credential action, the step's `value` field\n"
        f"  MUST be the literal from credential_hints (username=\"{credential_hints.get('username','')}\","
        f" password=\"{credential_hints.get('password','')}\") — copy it verbatim.\n"
        "  NEVER emit placeholders like 'valid_username' or 'valid_password'.\n"
        "  If a credential_hints value is the empty string, still emit \"\" —\n"
        "  the step-def resolves from env vars at Cucumber time.\n"
    ) if has_creds else (
        "- credential_hints is empty. For login steps, emit `value: \"\"` —\n"
        "  the step-def resolves credentials from env vars at runtime.\n"
    )
    return (
        "You are a QA Plan Architect. Convert the manual tests into a\n"
        "concrete automation plan. Each scenario carries the exact selectors\n"
        "and intent keys the code-generator will use next.\n"
        "Return STRICT JSON only.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "scenarios": [\n'
        "    {\n"
        '      "id":                "SC-1",\n'
        '      "title":             "<mirrors manual test title>",\n'
        '      "type":              "SMOKE" | "NEGATIVE",\n'
        '      "preconditions":     ["nav to /login"],\n'
        '      "steps":             [\n'
        '        {\n'
        '          "action":     "click sign-in",\n'
        '          "selector":   "#loginButton",\n'
        '          "intent_key": "homepage_signin_cta",\n'
        '          "value":      ""\n'
        "        }, ...\n"
        "      ],\n"
        '      "assertions":         ["expect user greeting visible"],\n'
        '      "selectors_used":     ["#loginButton", "#username"],\n'
        '      "intent_keys_used":   ["homepage_signin_cta", "login_username"]\n'
        "    }, ...\n"
        "  ],\n"
        '  "notes": ["short remark"]\n'
        "}\n"
        "\n"
        "Rules:\n"
        f"{selector_rule}"
        f"{credential_rule}"
        "- Every intent_key MUST appear in the allowed intent_keys list.\n"
        "  If none fits, use \"generic\".\n"
        "- The `manual_tests` input carries ONE end-to-end test. Output\n"
        "  EXACTLY ONE scenario (id=SC-1, type=SMOKE) whose `steps` walk\n"
        "  through every entry in the manual test's `when` clauses in order.\n"
        "  Do NOT split the story into multiple scenarios.\n"
        "- selectors_used / intent_keys_used are dedup summaries used by\n"
        "  the code-generator; keep them tight and correct.\n"
        + (
            "- The scenario's first `preconditions` entry MUST reference seed_url[1]\n"
            "  (the first, fully-qualified navigation target). Every `step.action`\n"
            "  that navigates MUST use one of the reviewer-approved seed URLs\n"
            "  below verbatim — never invent a URL, never emit `/` or `/home` alone.\n"
            if seed_urls else ""
        ) +
        "\n"
        + (
            "Reviewer-approved navigation targets (use these verbatim):\n"
            + "\n".join(f"  {i + 1}. {u}" for i, u in enumerate(seed_urls or []))
            + "\n"
            if seed_urls else ""
        ) +
        (ground_truth_block + "\n" if ground_truth_block else "") +
        f"Feature: {json.dumps(feature, indent=2)}\n"
        f"Manual tests: {json.dumps(manual_tests, indent=2)}\n"
        f"Selector map: {json.dumps(selector_map, indent=2)}\n"
        f"Allowed intent keys: {json.dumps(intent_keys)}\n"
        f"Credential hints: {json.dumps(credential_hints)}\n"
    )


@transaction.atomic
def run_plan_agent(job: GenerationJob, *,
                   selector_map: Optional[Dict[str, str]] = None,
                   intent_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Agent 3 — build the concrete automation plan."""
    if not job.stage_manual_tests_output:
        raise ValueError("Manual tests missing — run agent 2 first.")

    # Callers (the endpoint) inject the enriched selector_map + intent_keys.
    # Falling back to empty is legal — the LLM will simply produce fewer usable
    # selectors, which the operator can then patch on the review screen.
    reviewer_seed_urls: List[str] = []
    if job.base_url:
        reviewer_seed_urls.append(job.base_url)
    reviewer_seed_urls.extend(job.seed_urls or [])

    # Phase 5.6 — same ground-truth inventory injection we do at Artifact
    # stage (Phase 5.2). When ui_knowledge has been captured for the seed
    # URLs, feed the LLM the real selectors so the Plan doesn't ship
    # hallucinated names that reviewers then have to mentally translate.
    inventory = _build_ground_truth_inventory(job)
    ground_truth_block = _format_ground_truth_block(inventory)
    has_ground_truth = bool(inventory.get("per_url"))

    output = _run_llm(
        prompt=_build_plan_prompt(
            feature=job.stage_feature_output or {},
            manual_tests=job.stage_manual_tests_output or {},
            selector_map=selector_map or {},
            intent_keys=intent_keys or [],
            seed_urls=reviewer_seed_urls,
            ground_truth_block=ground_truth_block,
            has_ground_truth=has_ground_truth,
            has_base_url=bool(job.base_url),
            credential_hints=_credential_hints(job),
        ),
        num_predict=2200,
        job=job,
        agent="plan_architect",
    )

    # Safety net: if the LLM returned no scenarios (happens when the crawl
    # was empty and the model plays it too safe), synthesize placeholder
    # scenarios from the manual tests so the operator has SOMETHING to
    # review + fix instead of a blank plan.
    if not (output.get("scenarios") or []):
        output = _synthesize_plan_from_manual_tests(
            output, job.stage_manual_tests_output or {}
        )

    # Enforce one-scenario-per-story: if the LLM ignored the rule and emitted
    # several, merge them so downstream Artifacts sees the single-scenario
    # shape the .feature file needs.
    output = _coerce_single_plan_scenario(output)

    job.stage_plan_output = output
    job.stage = GenerationJob.STAGE_PLAN
    _record_stage(job, agent="plan_architect",
                  notes=f"Produced {len(output.get('scenarios') or [])} scenarios.")
    job.save(update_fields=["stage_plan_output", "stage", "stage_history", "last_modified"])
    return output


def _coerce_single_plan_scenario(output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce: exactly one scenario per plan. If the LLM emitted multiple, merge
    their steps + assertions into one long SC-1 walk so the .feature file
    produced downstream has a single `Scenario:` block per story.
    """
    if not isinstance(output, dict):
        return output
    scenarios = output.get("scenarios") or []
    if not isinstance(scenarios, list) or len(scenarios) <= 1:
        return output

    merged_steps: List[Dict[str, Any]] = []
    merged_pre: List[str] = []
    merged_assert: List[str] = []
    merged_selectors: List[str] = []
    merged_intents: List[str] = []
    merged_title_parts: List[str] = []

    for sc in scenarios:
        if not isinstance(sc, dict):
            continue
        if sc.get("title"):
            merged_title_parts.append(str(sc["title"]))
        for pre in sc.get("preconditions") or []:
            if pre and str(pre) not in merged_pre:
                merged_pre.append(str(pre))
        for step in sc.get("steps") or []:
            if isinstance(step, dict):
                merged_steps.append(step)
        for a in sc.get("assertions") or []:
            if a and str(a) not in merged_assert:
                merged_assert.append(str(a))
        for s in sc.get("selectors_used") or []:
            if s and s not in merged_selectors:
                merged_selectors.append(str(s))
        for k in sc.get("intent_keys_used") or []:
            if k and k not in merged_intents:
                merged_intents.append(str(k))

    single = {
        "id": "SC-1",
        "title": " → ".join(merged_title_parts[:3]) or "End-to-end journey",
        "type": "SMOKE",
        "preconditions": merged_pre,
        "steps": merged_steps,
        "assertions": merged_assert,
        "selectors_used": merged_selectors,
        "intent_keys_used": merged_intents,
    }
    notes = list(output.get("notes") or [])
    notes.append(
        f"Collapsed {len(scenarios)} LLM-emitted scenarios into one end-to-end SC-1 walk."
    )
    return {"scenarios": [single], "notes": notes}


def _synthesize_plan_from_manual_tests(existing: Dict[str, Any],
                                       manual_tests: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic fallback for when the LLM emits `scenarios: []`.

    Walks the manual-tests output and produces one placeholder scenario per
    manual test with `selector: ""` on every step. The operator patches the
    selectors in Review — but at least the pipeline no longer silently drops
    every test.
    """
    tests = manual_tests.get("tests") if isinstance(manual_tests, dict) else None
    if not isinstance(tests, list):
        return existing

    scenarios = []
    for idx, t in enumerate(tests, start=1):
        if not isinstance(t, dict):
            continue
        steps_in = t.get("steps") or []
        steps_out = []
        for step in steps_in:
            action = step.get("action") if isinstance(step, dict) else str(step)
            steps_out.append({
                "action": str(action or "step"),
                "selector": "",
                "intent_key": "generic",
                "value": "",
            })
        scenarios.append({
            "id": f"SC-{idx}",
            "title": str(t.get("title") or f"Scenario {idx}"),
            "type": str(t.get("type") or "SMOKE"),
            "preconditions": t.get("preconditions") or [],
            "steps": steps_out,
            "assertions": t.get("assertions") or [],
            "selectors_used": [],
            "intent_keys_used": ["generic"],
        })

    notes = list(existing.get("notes") or [])
    notes.append(
        "Fallback plan: LLM returned empty scenarios (likely because the site "
        "crawl was empty). Placeholder selectors emitted — review each step "
        "and fill in a real selector before advancing to Artifacts."
    )
    return {"scenarios": scenarios, "notes": notes}


# ---------------------------------------------------------------------------
# Agent 4 — Artifact Generator (Cucumber)
# ---------------------------------------------------------------------------
def _seed_url_to_class_name(url: str) -> str:
    """
    Turn a seed URL PATH into a PascalCase page-object class name.

      /login              -> LoginPage
      /cart/checkout      -> CartCheckoutPage
      /                   -> HomePage
      https://x.com/foo   -> FooPage       (host stripped)
      https://x.com/      -> HomePage      (host stripped, root path)

    Historically this function PascalCased every segment of the raw URL,
    which turned `https://staging.pulze.com/it-IT/` into the phantom
    `HttpsStagingPulzeComItItPage` — colliding with `/it-IT/`'s
    `ItItPage`. The LLM then emitted two classes for the same page and
    the pipeline shipped one of them as a comment block. Root cause:
    the mapping must be PATH-based only, not URL-string-based.
    """
    from re import findall
    from urllib.parse import urlparse
    raw = (url or "").strip().split("?", 1)[0].split("#", 1)[0]
    # Strip scheme + host if present. urlparse handles both
    # "https://x.com/foo" and bare paths ("/foo", "foo") safely.
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or "/"
    else:
        path = raw
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return "HomePage"
    words: List[str] = []
    for seg in parts:
        for chunk in findall(r"[A-Za-z0-9]+", seg):
            words.append(chunk[:1].upper() + chunk[1:].lower())
    return ("".join(words) or "Home") + "Page"


# --------------------------------------------------------------------------
# Ground-truth locator inventory (Phase 5.2)
# --------------------------------------------------------------------------
# Query `ui_knowledge.UIElement` rows for every seed URL on the job and
# format them as a compact table the artifact-generator prompt can quote
# verbatim. This is the LLM's ONLY allowed selector source — everything
# else is a hallucination.
_INVENTORY_MAX_ROWS_PER_URL = 120

# Tags that carry actual user interaction — get top priority in the
# inventory so the prompt window shows the LLM what to CLICK / FILL /
# SELECT, not just the containers around them.
_INTERACTIVE_TAGS = frozenset({"input", "button", "select", "textarea", "a"})


def _build_ground_truth_inventory(job: GenerationJob) -> Dict[str, Any]:
    """
    Returns `{url_path: [{selector, tag, role, text, test_id, intent_key}, …]}`
    plus a flat set of every allowed selector across all seed URLs. Empty
    dict when ui_knowledge has nothing captured for this client (falls back
    to the pre-Phase-5 behavior).
    """
    from ui_knowledge.models import UIPage
    from urllib.parse import urlparse

    inventory: Dict[str, List[Dict[str, Any]]] = {}
    allowed_selectors: set = set()

    seed_urls: List[str] = []
    if job.base_url:
        seed_urls.append(job.base_url)
    seed_urls.extend(job.seed_urls or [])
    if not seed_urls:
        return {"per_url": inventory, "allowed_selectors": allowed_selectors}

    routes = set()
    for raw in seed_urls:
        raw = (raw or "").strip()
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            routes.add(parsed.path or "/")
        elif raw.startswith("/"):
            routes.add(raw)
        else:
            routes.add(f"/{raw}")

    for route in sorted(routes):
        page = UIPage.objects.filter(
            client=job.client, route=route, is_active=True
        ).first()
        if not page:
            continue
        snap = page.snapshots.filter(is_current=True).order_by("-version").first()
        if not snap:
            continue
        # Rank rows so the LLM's prompt window sees the most useful
        # selectors first: stable test-ids > accessible text > everything
        # else. `stability_score` in auto-captured snapshots is uniform (1.0)
        # so we can't rely on it — the ranking is a client-side priority
        # sort. Cap at MAX_ROWS_PER_URL to keep the prompt bounded.
        raw_rows = list(snap.elements.all())

        def _priority(el) -> int:
            score = 0
            tag = (el.tag or "").lower()
            # Interactive elements are what the tests actually target.
            if tag in _INTERACTIVE_TAGS:
                score += 200
            if el.test_id:
                score += 100
            if el.role in {"button", "link", "textbox", "checkbox", "combobox", "dialog"}:
                score += 40
            if (el.text or "").strip():
                score += 20
            return -score  # ascending sort = highest first

        raw_rows.sort(key=_priority)
        raw_rows = raw_rows[:_INVENTORY_MAX_ROWS_PER_URL]

        rows: List[Dict[str, Any]] = []
        for el in raw_rows:
            selector = str(el.selector or "").strip()
            if not selector:
                continue
            allowed_selectors.add(selector)
            rows.append({
                "selector":   selector,
                "tag":        str(el.tag or ""),
                "role":       str(el.role or ""),
                "text":       str(el.text or "")[:80],
                "test_id":    str(el.test_id or ""),
                "intent_key": str(el.intent_key or "generic"),
            })
        if rows:
            inventory[route] = rows

    return {"per_url": inventory, "allowed_selectors": allowed_selectors}


def _format_ground_truth_block(inventory: Dict[str, Any]) -> str:
    """
    Render the inventory as a Markdown-ish block for the prompt. Kept
    compact — each row is one line so 60 rows/URL fits inside the
    generator's context budget with room for the rest of the prompt.
    """
    per_url: Dict[str, List[Dict[str, Any]]] = inventory.get("per_url") or {}
    if not per_url:
        return ""
    lines: List[str] = [
        "GROUND-TRUTH LOCATORS (captured from the live DOM at Artifact-stage time):",
        "These are the ONLY selectors that exist on the listed pages. Every locator you",
        "write in a page-object file MUST be one of these strings, verbatim. Do NOT",
        "invent new selectors — the pipeline will reject them. If no listed row fits a",
        "step, add a short entry to `notes` explaining what's missing and pick the",
        "closest match (or omit the step from the page object).",
        "",
    ]
    for url_path, rows in per_url.items():
        lines.append(f"## {url_path}   ({len(rows)} elements)")
        lines.append("selector | tag | role | text | test_id | intent_key")
        for r in rows:
            # Escape pipes in text so the table shape stays parseable by
            # anyone eyeballing the prompt in a log.
            text = r["text"].replace("|", "\\|")
            lines.append(
                f'{r["selector"]} | {r["tag"]} | {r["role"]} | {text} | '
                f'{r["test_id"]} | {r["intent_key"]}'
            )
        lines.append("")
    return "\n".join(lines)


def _build_artifacts_prompt(job: GenerationJob, plan: Dict[str, Any],
                            selector_map: Dict[str, str], intent_keys: List[str],
                            client_slug: str,
                            credential_hints: Optional[Dict[str, str]] = None) -> str:
    feature_slug = _slug(job.feature_name or "feature") or "feature"

    # Reviewer-approved Seed URLs list — [0] is the fully-qualified target,
    # [1..] are relative paths. The LLM must use these verbatim in step defs
    # (`page.goto(...)`) and to name page-object classes.
    all_seed_urls: List[str] = []
    if job.base_url:
        all_seed_urls.append(job.base_url)
    all_seed_urls.extend(job.seed_urls or [])
    if not all_seed_urls:
        all_seed_urls = ["/"]

    page_object_names = [_seed_url_to_class_name(u) for u in all_seed_urls]
    seed_table_lines = [
        f"    {url!r:40} -> {name}"
        for url, name in zip(all_seed_urls, page_object_names)
    ]
    seed_table = "\n".join(seed_table_lines)
    # Legacy alias so the rest of the prompt (page-object generation) still
    # sees the same list it always did.
    seed_urls = all_seed_urls

    # Ground-truth locator inventory (Phase 5.2). When ui_knowledge has
    # snapshots for the seed URLs (auto-captured in Phase 5.1), inject them
    # as the LLM's ONLY allowed selector source. When empty, the prompt
    # falls back to the pre-Phase-5 behavior (selector_map + guessing).
    inventory = _build_ground_truth_inventory(job)
    ground_truth_block = _format_ground_truth_block(inventory)
    has_inventory = bool(inventory.get("per_url"))

    # Phase 9.4 — credential handling. Belt: LLM inlines literal Jira-provided
    # credentials into the .feature file. Suspenders: emitted step-defs also
    # resolve process.env.PRECOND_EMAIL / PRECOND_PASSWORD when the runtime
    # sees a known placeholder token (defensive fallback for tenants whose
    # story didn't ship literals — the executor still sets those env vars).
    credential_hints = credential_hints or {"username": "", "password": ""}
    hint_user = credential_hints.get("username") or ""
    hint_pass = credential_hints.get("password") or ""
    if hint_user or hint_pass:
        credential_feature_rule = (
            "- The Jira story provided real login credentials. Where the plan\n"
            "  carries a login step (enter username / type email / fill\n"
            "  password), the .feature file MUST use the LITERAL values\n"
            "  verbatim:\n"
            f"    And I enter my username as '{hint_user}'\n"
            f"    And I enter my password as '{hint_pass}'\n"
            "  NEVER emit the placeholders 'valid_username' or\n"
            "  'valid_password' — those are historical artefacts and cause\n"
            "  Cucumber to type the literal string 'valid_username' into the\n"
            "  form, which always fails.\n"
        )
    else:
        credential_feature_rule = (
            "- No login credentials are available for this job. If the plan\n"
            "  has a login step, emit the value as an empty single-quoted\n"
            "  string in Gherkin (e.g. `And I enter my username as ''`). The\n"
            "  step-def will fall back to process.env.PRECOND_EMAIL /\n"
            "  PRECOND_PASSWORD at runtime.\n"
        )
    credential_stepdef_rule = (
        "- Defensive credential fallback in the step-defs: if a `{string}`\n"
        "  parameter is empty OR equals the literal 'valid_username' /\n"
        "  'valid_password', resolve it from process.env before using it:\n"
        "    if (!username || username === 'valid_username')\n"
        "        username = process.env.PRECOND_EMAIL || process.env.PRECOND_USERNAME || username;\n"
        "    if (!password || password === 'valid_password')\n"
        "        password = process.env.PRECOND_PASSWORD || password;\n"
        "  Apply this pattern INSIDE every step-def whose parameter carries a\n"
        "  credential value. Guards nothing else — the empty/placeholder check\n"
        "  is exact.\n"
    )

    return (
        "You generate Cucumber test artifacts. Output THREE kinds of files:\n"
        "  1. ONE Gherkin feature file (.feature).\n"
        "  2. ONE step-definitions file (.ts, imports @cucumber/cucumber).\n"
        f"  3. EXACTLY {len(seed_urls)} page-object class(es) (.ts) — ONE per\n"
        f"     seed URL. See the seed-URL → class-name table below.\n"
        "\n"
        "Return STRICT JSON only. No prose, no markdown fences.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "features":         [{"path":"features/<feature>.feature","content":"..."}],\n'
        '  "step_definitions": [{"path":"features/steps/<feature>-steps.ts","content":"..."}],\n'
        '  "page_objects":     [{"path":"tests/pages/generated/<Name>.ts","content":"..."}],\n'
        '  "notes":            ["short remark"]\n'
        "}\n"
        "\n"
        "FILE ROUTING (strict — the validator rejects misgrouped files):\n"
        "- `features` array: ONLY `.feature` files. Path prefix `features/`.\n"
        "- `step_definitions` array: ONLY `.ts` files whose path is under\n"
        "  `features/steps/`. NEVER put a page object here.\n"
        "- `page_objects` array: ONLY `.ts` files whose path is under\n"
        "  `tests/pages/generated/`. NEVER put step definitions here.\n"
        "\n"
        "REQUIRED FEATURE FILE (Gherkin) — MANDATORY:\n"
        "- Start with `Feature: <title>`.\n"
        "- MUST contain EXACTLY ONE `Scenario: <name>` block. Do NOT split\n"
        "  the plan into multiple scenarios — one story = one scenario.\n"
        "- The scenario MUST have at least one `Given`, one `When`, and one\n"
        "  `Then` step. Use plain English; do NOT put CSS selectors here.\n"
        "- Walk EVERY step in the plan's single scenario in order. Use `And`\n"
        "  / `But` for additional clauses of the same type instead of adding\n"
        "  new `Scenario:` blocks.\n"
        f"{credential_feature_rule}"
        "\n"
        "NAVIGATION TARGETS (reviewer-approved seed URLs — use verbatim):\n"
        f"{seed_table}\n"
        "- Seed URL #1 is the fully-qualified app-under-test URL. Every other\n"
        "  seed entry is a path relative to that host.\n"
        "- Every `page.goto(...)` call in step defs MUST use one of these\n"
        "  entries verbatim (or concatenate seed #1 with a relative entry to\n"
        "  form the absolute URL). The Cucumber World does NOT set Playwright's\n"
        "  `baseURL` — bare paths like `/` or `/home` resolve to localhost.\n"
        "\n"
        + (
            ground_truth_block + "\n" if ground_truth_block else ""
        ) +
        (
            "SELECTOR SOURCE (STRICT):\n"
            "- Every `page.locator(...)`, `page.getByRole(...)`, etc. in the\n"
            "  page-object files MUST use a selector string that appears in the\n"
            "  ground-truth table above for that URL. Copy the `selector`\n"
            "  column verbatim.\n"
            "- Do NOT invent CSS shorthand like `#userIcon` or `.login-btn` if\n"
            "  the table doesn't list it — the pipeline verifies every locator\n"
            "  against the live DOM after this stage and rejects invented ones.\n"
            "- If a step in the plan has no matching row in the table, add a\n"
            "  short entry to `notes` explaining what's missing and use the\n"
            "  closest available selector (or skip the interaction).\n"
            "\n"
            if has_inventory
            else ""
        ) +
        "REQUIRED STEP DEFINITIONS FILE (.ts):\n"
        "- Every string literal MUST use CONSISTENT quotes. If the string\n"
        "  contains single quotes (e.g. an attribute selector like\n"
        "  `[data-testid='foo']`), WRAP the whole string in DOUBLE quotes:\n"
        "  `\"[data-testid='foo']\"`. Never emit `'[…='…']'` — the outer\n"
        "  quote closes on the first inner quote and TypeScript rejects it.\n"
        "- If you need a page-object instance, instantiate it INSIDE the step\n"
        "  body: `const homePage = new HomePage(this.page);`. NEVER declare it\n"
        "  at module scope — `this` is undefined there, the file crashes on\n"
        "  load, and every step registration is lost.\n"
        "- import { Given, When, Then } from '@cucumber/cucumber';\n"
        "- import { expect } from '@playwright/test';\n"
        "- import each page-object class BY NAME (one import per class), e.g.\n"
        "    import { LoginPage } from '../../tests/pages/generated/LoginPage';\n"
        "  NEVER import from a folder or an index (no `.../generated/'`).\n"
        "  NEVER use a default import — the page-object file exports a class,\n"
        "  so `import HomePage from …` resolves to `undefined` at runtime.\n"
        "- Register EVERY step with UPPERCASE `Given`, `When`, or `Then`:\n"
        "    Given('I open the homepage', async function () {\n"
        "      await this.page.goto('/');\n"
        "    });\n"
        "  NEVER use lowercase `given(...)`, `when(...)`, `then(...)` — those\n"
        "  are not exported by @cucumber/cucumber and crash at runtime with\n"
        "  `ReferenceError: given is not defined`.\n"
        "- @cucumber/cucumber v11 exports ONLY `Given` / `When` / `Then`.\n"
        "  For Gherkin `And` / `But` steps, register them with WHICHEVER of\n"
        "  `Given` / `When` / `Then` the previous step used (Gherkin And/But\n"
        "  inherit the previous keyword's context). Never call `And(...)`,\n"
        "  `But(...)`, `and(...)`, or `but(...)` — none of them exist.\n"
        "- Use plain Playwright APIs only: `page.locator(SELECTOR).click()`,\n"
        "  `page.goto(URL)`, `page.getByRole(...)`, `page.fill(...)`,\n"
        "  `expect(page.locator(SELECTOR)).toBeVisible()`.\n"
        "- Clicks are ordinary Playwright locator interactions:\n"
        "    await this.page.locator(SELECTOR).click();\n"
        "  Do NOT wrap them in helper functions. Do NOT import anything from\n"
        "  `wraper-healer` — this codebase now uses vanilla Playwright.\n"
        "- Each `Then` step must include at least one `expect(...)` assertion,\n"
        "  and `expect` MUST come from '@playwright/test'.\n"
        f"{credential_stepdef_rule}"
        "\n"
        "REQUIRED PAGE OBJECTS (.ts) — ONE PER SEED URL:\n"
        f"- Emit EXACTLY {len(seed_urls)} page-object file(s), one per seed URL.\n"
        "  Use the seed URL → class-name mapping below verbatim. NEVER invent\n"
        "  extra classes (`ConsentPopup`, `InvalidCredentialsSubmissionAttempt`,\n"
        "  etc). Fold all behaviour for a page into that page's single class.\n"
        "\n"
        "  Seed URL → class name (use these exact names + file paths):\n"
        f"{seed_table}\n"
        "\n"
        "  File paths:  `tests/pages/generated/<ClassName>.ts`\n"
        "\n"
        "- Full class definition, e.g.:\n"
        "    import { Page } from '@playwright/test';\n"
        "    export class LoginPage {\n"
        "      constructor(private page: Page) {}\n"
        "      readonly signInButton: string = '#signIn';\n"
        "      async open(): Promise<void> { await this.page.goto('/login'); }\n"
        "      async clickSignIn(): Promise<void> {\n"
        "        await this.page.locator(this.signInButton).click();\n"
        "      }\n"
        "    }\n"
        "- The class MUST be exported with `export class <Name>` — a NAMED\n"
        "  export. Do NOT emit `export default new LoginPage()` or\n"
        "  `export default class` — step defs import the class by name\n"
        "  (`import { LoginPage } from '.../LoginPage'`) and construct it\n"
        "  themselves.\n"
        "- Import ONLY from `@playwright/test`. Do NOT import from\n"
        "  `wraper-healer/selfHealing` — that helper is no longer used.\n"
        "- Every string-literal selector MUST use double quotes on the outside\n"
        "  when it already contains single quotes, and vice-versa. Do NOT emit\n"
        "  `'[data-testid='consent-banner']'` — that's three quotes in a row\n"
        "  and TypeScript rejects it.\n"
        "- Readonly selector fields sourced VERBATIM from the selector map.\n"
        "- Async action methods for each UI interaction the plan describes.\n"
        "\n"
        "FORBIDDEN (validator will reject the artifact):\n"
        "- waitForTimeout(...), setTimeout(...), test.only(...), process.exit(...).\n"
        "- .nth(<n>) selectors.\n"
        "- Inventing selectors: every selector MUST be a value from the map below.\n"
        "- ANY TestCafe API: `Selector(...)`, `.waitForVisible()`, `.selfSelectOption(...)`,\n"
        "  `.click()` on a Selector, `t.` handles. This is Playwright + Cucumber only.\n"
        "- Barrel imports like `from '../../tests/pages/generated/'` — always import\n"
        "  each class by its exact file path.\n"
        "- `selfHealingClick(...)` and any import from `wraper-healer/selfHealing` —\n"
        "  removed from this codebase. Use `locator.click()` instead.\n"
        "- TypeScript decorator syntax (`@Given(...)`, `@When(...)`, `@Then(...)`) —\n"
        "  Cucumber-JS does NOT support decorators. Use function-call form only.\n"
        "- Wrapping step registrations in `export default class` — put every\n"
        "  Given/When/Then call at module scope.\n"
        "- Default-importing a page object (`import HomePage from '.../HomePage'`).\n"
        "  Page objects use `export class`, so use `import { HomePage } from '.../HomePage'`.\n"
        "- Lowercase step registrations: `given(...)`, `when(...)`, `then(...)`,\n"
        "  `and(...)`. Only the capitalized `Given` / `When` / `Then` are exported\n"
        "  by @cucumber/cucumber. `And` / `But` have NO registration function.\n"
        "- Page-object `export default new <Class>()` — export the class itself,\n"
        "  not an instance. The step defs `new` the class with `this.page`.\n"
        "\n"
        f"Client slug: {client_slug}\n"
        f"Feature slug (use in path names): {feature_slug}\n"
        f"Plan: {json.dumps(plan, indent=2)}\n"
        f"Selector map: {json.dumps(selector_map, indent=2)}\n"
        f"Allowed intent keys: {json.dumps(intent_keys)}\n"
        f"Credential hints: {json.dumps(credential_hints)}\n"
    )


@transaction.atomic
def run_artifacts_agent(job: GenerationJob, *,
                        selector_map: Optional[Dict[str, str]] = None,
                        intent_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Agent 4 — generate Cucumber artifacts (.feature + step defs + page objects)."""
    if not job.stage_plan_output:
        raise ValueError("Plan missing — run agent 3 first.")

    client_slug = ""
    try:
        client_slug = getattr(job.client, "slug", "") or ""
    except Exception:
        pass

    # Artifact bundle contains one .feature (Gherkin), one step-defs .ts, and
    # 1-3 page-object .ts files — all JSON-escaped inside a single response.
    # 3200 tokens was empirically too tight and truncated the last artifact
    # mid-string; bump to 6400 and let _effective_llm_timeout() extend the
    # deadline in _call_ollama_json accordingly.
    credential_hints = _credential_hints(job)
    output = _run_llm(
        prompt=_build_artifacts_prompt(
            job=job,
            plan=job.stage_plan_output or {},
            selector_map=selector_map or {},
            intent_keys=intent_keys or [],
            client_slug=client_slug,
            credential_hints=credential_hints,
        ),
        num_predict=6400,
        job=job,
        agent="artifact_generator",
    )

    # Phase 9.9 — set-membership validator. When ground-truth inventory is
    # present, strip any selector the LLM invented (i.e. not in the allowed
    # set for this job's seed URLs). Rewrite them to "" in-place and push
    # notes into the output so reviewers see the swap. Runs BEFORE the
    # endpoint persists artifacts — never blocks the response.
    inventory = _build_ground_truth_inventory(job)
    membership_report: Dict[str, Any] = {"enabled": False}
    if inventory.get("per_url"):
        output, membership_report = _reject_hallucinated_selectors(
            output, inventory,
        )

    # Note: the endpoint layer (views.py — Stage 2 of the plan) is responsible
    # for turning `output.features / step_definitions / page_objects` into
    # `GeneratedArtifact` rows and running `_validate_artifacts` on them.
    # This function just persists the raw agent output for review.
    job.stage = GenerationJob.STAGE_ARTIFACTS
    _record_stage(
        job, agent="artifact_generator",
        notes=(f"Produced {len(output.get('features') or [])} feature(s), "
               f"{len(output.get('step_definitions') or [])} step-def file(s), "
               f"{len(output.get('page_objects') or [])} page object(s)."),
    )
    job.save(update_fields=["stage", "stage_history", "last_modified"])
    output["_selector_membership_report"] = membership_report
    return output


def _reject_hallucinated_selectors(
    output: Dict[str, Any],
    inventory: Dict[str, Any],
) -> "tuple[Dict[str, Any], Dict[str, Any]]":
    """
    Walk every page-object file's content and strip any selector that isn't
    in `inventory["allowed_selectors"]`. Rewrite the offending string to `""`
    inline so downstream syntax stays valid; push a note into `output["notes"]`.

    Returns (mutated_output, report). `report` shape:
      {
        "enabled": True,
        "allowed_count": N,
        "hallucinated": [{"file": "...", "kind": "locator", "selector": "..."}]
      }
    """
    import re  # local import to keep top-level minimal
    from .selector_verifier import _LOCATOR_PATTERNS  # DRY — same regexes

    # Supplementary pattern for the mixed-quote case the shared regexes miss:
    # a single-quoted string that CONTAINS a double quote (or vice-versa),
    # e.g. `readonly foo: string = '[data-testid="logo-image"]'`. Two
    # single-quote-outer + double-quote-outer variants; kept narrow with the
    # same "selector-shaped" first-character constraint as the shared regex.
    _MIXED_QUOTE_FIELD = [
        re.compile(
            r"""(?:readonly|private|public|protected)?\s*"""
            r"""(?:readonly\s+)?[a-zA-Z_$][\w$]*"""
            r"""(?:\s*:\s*string)?"""
            r"""\s*=\s*(?P<q>')(?P<sel>[#\.\[/][^'\n]{1,300})(?P=q)"""
        ),
        re.compile(
            r"""(?:readonly|private|public|protected)?\s*"""
            r"""(?:readonly\s+)?[a-zA-Z_$][\w$]*"""
            r"""(?:\s*:\s*string)?"""
            r"""\s*=\s*(?P<q>\")(?P<sel>[#\.\[/][^\"\n]{1,300})(?P=q)"""
        ),
    ]

    allowed: set = inventory.get("allowed_selectors") or set()
    hallucinated: List[Dict[str, str]] = []
    if not allowed:
        return output, {"enabled": False}

    for page_obj in output.get("page_objects") or []:
        content = str(page_obj.get("content") or "")
        if not content:
            continue
        rel_path = str(page_obj.get("path") or "")

        def _replace(match: "re.Match") -> str:  # noqa: F821
            sel = match.group("sel")
            if "${" in sel:
                # Template literal — never rewrite; we can't evaluate the value.
                return match.group(0)
            if sel in allowed:
                return match.group(0)
            hallucinated.append({
                "file": rel_path,
                "kind": "locator",
                "selector": sel,
            })
            # Replace the selector with the empty string, preserving the
            # opening/closing quote pair. This keeps the file syntactically
            # valid; the operator sees an obvious `""` and re-runs sync.
            quote = match.group("q") if "q" in match.groupdict() else match.group(1)
            return match.group(0).replace(f"{quote}{sel}{quote}", f'{quote}{quote}', 1)

        new_content = content
        for _kind, pat in _LOCATOR_PATTERNS:
            new_content = pat.sub(_replace, new_content)
        for pat in _MIXED_QUOTE_FIELD:
            new_content = pat.sub(_replace, new_content)
        page_obj["content"] = new_content

    if hallucinated:
        notes = list(output.get("notes") or [])
        notes.append(
            f"Selector membership check stripped {len(hallucinated)} "
            f"hallucinated selector(s) from page-object(s). Details: "
            + json.dumps(hallucinated[:10])
        )
        output["notes"] = notes

    return output, {
        "enabled": True,
        "allowed_count": len(allowed),
        "hallucinated": hallucinated,
    }


# ---------------------------------------------------------------------------
# Agent 5 — Executor (subprocess side lives in executor.py; only the
# root-cause-fixer LLM prompt is here so it's colocated with its siblings)
# ---------------------------------------------------------------------------
def _build_root_cause_prompt(job: GenerationJob, failed_scenario: Dict[str, Any],
                             failed_step: Dict[str, Any],
                             error_message: str, page_html_excerpt: str,
                             failed_selector: str, page_url: str,
                             current_artifacts: List[Dict[str, str]],
                             previous_diagnoses: Optional[List[str]] = None,
                             page_dom_snapshot: str = "") -> str:
    prev = list(previous_diagnoses or [])
    prev_block = ""
    if prev:
        prev_block = (
            "\nPrevious diagnoses (already tried — do NOT repeat):\n"
            + "\n".join(f"  - iteration {i + 1}: {d}" for i, d in enumerate(prev))
            + "\n"
        )
    dom_block = ""
    if page_dom_snapshot:
        # Trim large DOM captures — Ollama context is finite.
        trimmed = page_dom_snapshot[:8000]
        truncated = " (truncated)" if len(page_dom_snapshot) > 8000 else ""
        dom_block = (
            f"\nLive page DOM from Playwright MCP (first 8000 chars{truncated}):\n"
            f"{trimmed}\n"
        )

    return (
        "You are a QA Root-Cause Fixer. A Cucumber run failed on the step\n"
        "described below. Diagnose the failure and return a JSON patch that\n"
        "replaces the affected artifact(s) so the next run passes.\n"
        "Return STRICT JSON only.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "diagnosis": "<one-sentence root cause>",\n'
        '  "patches": [\n'
        "    {\n"
        '      "relative_path": "features/steps/<slug>/<feature>-steps.ts",\n'
        '      "content":       "<full new file content>",\n'
        '      "reason":        "<why this file was patched>"\n'
        "    }\n"
        "  ],\n"
        '  "notes": ["short remark"]\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- Patch ONLY the file(s) that actually caused the failure. Do not\n"
        "  rewrite unrelated artifacts.\n"
        "- Each `content` MUST be the full new file body — no diffs, no\n"
        "  ellipses, no partial snippets. The old content is replaced whole.\n"
        "- Do not change selectors that were already working in other scenarios.\n"
        "  If the root cause is a stale selector, prefer updating just the one\n"
        "  broken selector.\n"
        "- If the live page DOM below shows a different selector than the one\n"
        "  the plan uses, PREFER the DOM. The DOM is ground truth; the plan's\n"
        "  selector may have been guessed before the page was reachable.\n"
        "- Your `diagnosis` MUST be materially different from every entry\n"
        "  in 'Previous diagnoses' — if you have nothing new to try, say so\n"
        "  and return `patches: []` so the loop can stop.\n"
        "\n"
        f"Failed scenario:\n{json.dumps(failed_scenario, indent=2)}\n"
        f"Failed step:\n{json.dumps(failed_step, indent=2)}\n"
        f"Playwright/Cucumber error message:\n{error_message}\n"
        f"Failed selector: {failed_selector}\n"
        f"Page URL at failure: {page_url}\n"
        f"Page HTML excerpt (first 4000 chars):\n{(page_html_excerpt or '')[:4000]}\n"
        f"{dom_block}"
        f"{prev_block}"
        "\n"
        f"Current artifacts (relative_path + first 400 chars of content):\n"
        f"{json.dumps(current_artifacts, indent=2)}\n"
    )


def run_root_cause_fixer(job: GenerationJob, *,
                         failed_scenario: Dict[str, Any],
                         failed_step: Dict[str, Any],
                         error_message: str,
                         page_html_excerpt: str = "",
                         failed_selector: str = "",
                         page_url: str = "",
                         current_artifacts: Optional[List[Dict[str, str]]] = None,
                         previous_diagnoses: Optional[List[str]] = None,
                         page_dom_snapshot: str = "") -> Dict[str, Any]:
    """Agent 5b — analyze a failed Cucumber run and return a patch bundle.

    The Executor (see executor.py) calls this after each red iteration; it
    applies the returned `patches[]` to the corresponding GeneratedArtifact
    rows before re-materializing.

    `previous_diagnoses` is a running list of every prior iteration's
    `diagnosis` string — passed in so the LLM doesn't repeat itself.
    `page_dom_snapshot` is a Playwright-MCP-captured DOM (typically an
    accessibility-tree text) — empty when MCP is unavailable.

    Phase-1 behaviour: if `OPENAI_TOOL_USE` is on (default), route through
    `tool_using_fixer.run(...)` — Claude-Code-style multi-turn loop with
    tools. Otherwise fall back to the legacy single-shot prompt path.
    """
    from django.conf import settings
    if getattr(settings, "OPENAI_TOOL_USE", False):
        from .tool_using_fixer import run as run_tool_loop
        try:
            output = run_tool_loop(
                job,
                failed_scenario=failed_scenario,
                failed_step=failed_step,
                error_message=error_message,
                previous_diagnoses=previous_diagnoses,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool-using fixer crashed — falling back to single-shot")
            output = _run_root_cause_fixer_legacy(
                job,
                failed_scenario=failed_scenario,
                failed_step=failed_step,
                error_message=error_message,
                page_html_excerpt=page_html_excerpt,
                failed_selector=failed_selector,
                page_url=page_url,
                current_artifacts=current_artifacts,
                previous_diagnoses=previous_diagnoses,
                page_dom_snapshot=page_dom_snapshot,
            )
            output.setdefault("notes", []).append(
                f"tool-using fixer crashed ({type(exc).__name__}); "
                "fell back to single-shot prompt"
            )
    else:
        output = _run_root_cause_fixer_legacy(
            job,
            failed_scenario=failed_scenario,
            failed_step=failed_step,
            error_message=error_message,
            page_html_excerpt=page_html_excerpt,
            failed_selector=failed_selector,
            page_url=page_url,
            current_artifacts=current_artifacts,
            previous_diagnoses=previous_diagnoses,
            page_dom_snapshot=page_dom_snapshot,
        )

    _record_stage(
        job, agent="root_cause_fixer",
        decision="patched",
        notes=f"Iteration {job.execute_iteration}: {output.get('diagnosis', '')[:200]}",
    )
    job.save(update_fields=["stage_history", "last_modified"])
    return output


def _run_root_cause_fixer_legacy(job: GenerationJob, *,
                                 failed_scenario: Dict[str, Any],
                                 failed_step: Dict[str, Any],
                                 error_message: str,
                                 page_html_excerpt: str = "",
                                 failed_selector: str = "",
                                 page_url: str = "",
                                 current_artifacts: Optional[List[Dict[str, str]]] = None,
                                 previous_diagnoses: Optional[List[str]] = None,
                                 page_dom_snapshot: str = "") -> Dict[str, Any]:
    """Single-shot prompt path. Retained as fallback when OPENAI_TOOL_USE=off."""
    return _run_llm(
        prompt=_build_root_cause_prompt(
            job=job,
            failed_scenario=failed_scenario,
            failed_step=failed_step,
            error_message=error_message,
            page_html_excerpt=page_html_excerpt,
            failed_selector=failed_selector,
            page_url=page_url,
            current_artifacts=current_artifacts or [],
            previous_diagnoses=previous_diagnoses,
            page_dom_snapshot=page_dom_snapshot,
        ),
        num_predict=2400,
        job=job,
        agent="root_cause_fixer",
    )


# ---------------------------------------------------------------------------
# Agent 6 — Jira Reporter (composes the ADF comment; push is done by the
# integrations_jira app)
# ---------------------------------------------------------------------------
def _build_reporter_prompt(job: GenerationJob,
                           execute_output: Dict[str, Any]) -> str:
    return (
        "You are a QA Jira Reporter. Produce a single, human-readable summary\n"
        "of the automated test run for pasting into a Jira comment.\n"
        "Return STRICT JSON only.\n"
        "\n"
        "Schema (exact):\n"
        "{\n"
        '  "headline":       "<one line summary, e.g. \'All 5 scenarios passed on iteration 2\'>",\n'
        '  "body_markdown":  "<3-6 paragraphs in plain Markdown>",\n'
        '  "highlights":     ["<short bullet>", "<short bullet>"],\n'
        '  "notes":          ["reviewer remark"]\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- Cite scenario titles verbatim from the plan / execution output.\n"
        "- If retries were needed, mention how many and what the fix was.\n"
        "- Do NOT include secrets, tokens, or full stack traces (a truncated\n"
        "  error line is fine).\n"
        "\n"
        f"Job: feature_name={job.feature_name!r}, jira_issue_key={job.jira_issue_key!r}\n"
        f"Execute output:\n{json.dumps(execute_output, indent=2)}\n"
    )


@transaction.atomic
def run_reporter_agent(job: GenerationJob) -> Dict[str, Any]:
    """Agent 6 — synthesize a Jira-comment summary from the execute output."""
    output = _run_llm(
        prompt=_build_reporter_prompt(job=job, execute_output=job.stage_execute_output or {}),
        num_predict=1400,
        job=job,
        agent="feature_author",   # reporter is short prose — reuse the cheapest model tier
    )
    job.stage = GenerationJob.STAGE_REPORT
    _record_stage(job, agent="jira_reporter", notes=output.get("headline", "")[:200])
    job.save(update_fields=["stage", "stage_history", "last_modified"])
    return output
