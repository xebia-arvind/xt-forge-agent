import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from django.utils import timezone

from .models import GeneratedArtifact, GenerationJob, GenerationScenario

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    # .../ecommerce-app/ai-healer-django/flaky_healer/test_generation/generation_service.py
    return Path(__file__).resolve().parents[3]


def _default_test_gen_model() -> str:
    return os.getenv("TEST_GEN_LLM_MODEL", os.getenv("LLM_VALIDATION_MODEL", "qwen2.5-coder:7b"))


def _llm_url() -> str:
    return os.getenv("TEST_GEN_LLM_URL", "http://127.0.0.1:11434/api/generate").strip()


def _llm_timeout() -> int:
    try:
        return int(os.getenv("TEST_GEN_TIMEOUT_SECONDS", "120"))
    except ValueError:
        return 120


def _effective_llm_timeout(base_timeout: int, num_predict: int) -> int:
    # Local Ollama on laptop/CPU can be slow on first load; keep generous floor.
    if num_predict >= 2400:
        return max(base_timeout, 240)
    if num_predict >= 1200:
        return max(base_timeout, 180)
    return max(base_timeout, 120)


def _max_scenarios_default() -> int:
    return int(os.getenv("TEST_GEN_MAX_SCENARIOS", "8"))


def _max_routes_default() -> int:
    return int(os.getenv("TEST_GEN_MAX_ROUTES", "20"))


def _test_gen_enabled() -> bool:
    return os.getenv("USE_TEST_GEN", "true").lower() == "true"


def _runtime_selector_validation_enabled() -> bool:
    return os.getenv("TEST_GEN_RUNTIME_SELECTOR_VALIDATION", "true").lower() == "true"


def _selector_validation_source() -> str:
    return os.getenv("TEST_GEN_SELECTOR_VALIDATION_SOURCE", "ui_knowledge").strip().lower()


def _feature_presence_required() -> bool:
    return os.getenv("TEST_GEN_REQUIRE_FEATURE_PRESENCE", "true").lower() == "true"


def _feature_presence_min_score() -> float:
    try:
        return float(os.getenv("TEST_GEN_FEATURE_PRESENCE_MIN_SCORE", "0.40"))
    except ValueError:
        return 0.40


def _use_ui_knowledge_source() -> bool:
    return os.getenv("TEST_GEN_USE_UI_KNOWLEDGE", "true").lower() == "true"


def _allow_live_crawl_fallback() -> bool:
    return os.getenv("TEST_GEN_ALLOW_LIVE_CRAWL_FALLBACK", "false").lower() == "true"


def _require_all_artifacts_valid() -> bool:
    return os.getenv("TEST_GEN_REQUIRE_ALL_ARTIFACTS_VALID", "true").lower() == "true"


def _planning_verify_enabled() -> bool:
    return os.getenv("TEST_GEN_ENABLE_PLANNING_VERIFY", "true").lower() == "true"


def _planning_verify_num_predict() -> int:
    try:
        return int(os.getenv("TEST_GEN_PLANNING_VERIFY_NUM_PREDICT", "900"))
    except ValueError:
        return 900


def _respect_manual_scenarios_exactly() -> bool:
    return os.getenv("TEST_GEN_RESPECT_MANUAL_SCENARIOS", "true").lower() == "true"


def _codegen_enabled() -> bool:
    return os.getenv("TEST_GEN_ENABLE_CODEGEN", "false").lower() == "true"


def _codegen_retry_enabled() -> bool:
    """
    Gate for the *validation* retry (Phase 4). When true (default), if the first
    codegen pass produces at least one artifact that fails `_validate_artifacts`,
    we call the LLM once more with the validator's error messages embedded in
    the retry prompt.
    """
    return os.getenv("TEST_GEN_CODEGEN_RETRY_ENABLED", "true").lower() == "true"


def _enrich_llm_context(
    planning: Dict[str, Any],
    selector_map: Dict[str, str],
    allowed_intent_keys: List[str],
    notes: List[str],
) -> Tuple[Dict[str, str], List[str]]:
    """
    Merge manual-scenario context into what the LLM will see.

    Two things get injected:
      1. Any `step.selector` from `planning["scenarios"][*].steps[*]` that isn't
         already a value in `selector_map`. Key is derived from `step.action`
         via `_slug()` so identical actions collapse deterministically.
      2. Any `step.intent_key` (or `scenario.intent_key`) not already in
         `allowed_intent_keys` is appended. The LLM will then preserve custom
         intent keys instead of downgrading them to "generic".

    Idempotent — safe to call before the initial prompt and again before the
    retry prompt.
    """
    existing_selector_values = {str(v).strip() for v in selector_map.values() if v}
    normalized_intents = {str(k).strip().lower() for k in allowed_intent_keys if k}

    added_selectors = 0
    added_intents = 0

    scenarios = planning.get("scenarios") if isinstance(planning, dict) else None
    for scenario in scenarios or []:
        # Scenario-level intent key (rare, but present in some manual specs).
        scen_intent = str(scenario.get("intent_key") or "").strip()
        if scen_intent and scen_intent.lower() not in normalized_intents:
            allowed_intent_keys.append(scen_intent)
            normalized_intents.add(scen_intent.lower())
            added_intents += 1

        for step in scenario.get("steps") or []:
            selector = str(step.get("selector") or "").strip()
            if selector and selector not in existing_selector_values:
                base = _slug(str(step.get("action") or "manual"))[:40] or "manual"
                # Deduplicate the slug key too.
                candidate = base
                i = 2
                while candidate in selector_map:
                    candidate = f"{base}_{i}"
                    i += 1
                selector_map[candidate] = selector
                existing_selector_values.add(selector)
                added_selectors += 1

            step_intent = str(step.get("intent_key") or "").strip()
            if step_intent and step_intent.lower() not in normalized_intents:
                allowed_intent_keys.append(step_intent)
                normalized_intents.add(step_intent.lower())
                added_intents += 1

    if added_selectors or added_intents:
        notes.append(
            f"Enriched codegen context: +{added_selectors} manual selector(s), "
            f"+{added_intents} custom intent key(s)."
        )
    return selector_map, allowed_intent_keys


def _safe_json(value: Any, fallback: Any):
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return fallback


def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _available_intent_keys() -> List[str]:
    keys: List[str] = []
    try:
        from ui_knowledge.models import UIElement  # lazy import to avoid hard coupling at module import time

        keys = [
            str(v or "").strip().lower()
            for v in UIElement.objects.exclude(intent_key__isnull=True).values_list("intent_key", flat=True).distinct()
            if str(v or "").strip()
        ]
    except Exception:
        keys = []
    if "generic" not in keys:
        keys.append("generic")
    return sorted(set(keys))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-") or "feature"


def _camel(text: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", text or "")
    merged = "".join(p.capitalize() for p in parts if p)
    if not merged:
        return "Generated"
    if merged[0].isdigit():
        return f"F{merged}"
    return merged


def _sha256(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _normalize_scenario_type(value: str) -> str:
    norm = (value or "").strip().upper()
    if "NEG" in norm:
        return GenerationScenario.TYPE_NEGATIVE
    return GenerationScenario.TYPE_SMOKE


def _render_step_name(step: Dict[str, Any], fallback: str) -> str:
    name = (step.get("action") or step.get("name") or fallback or "").strip()
    return name or "perform action"


def _render_failed_selector(step: Dict[str, Any]) -> str:
    """
    Return a CSS/XPath selector suitable for `selfHealingClick(page, loc, SEL, …)`.

    Guards against a common planning quirk where a `navigate` step lands here
    with a URL in the `selector` field. If we let that through we'd emit
    `selfHealingClick(..., '/', ...)` — invalid at validation time. The action-
    kind branch upstream already routes `navigate` to `page.goto()`, so this
    fallback only runs when none of the branches match; treat URL-shaped values
    as "no usable selector" and drop to the generic fallback.
    """
    preferred = step.get("failed_selector") or step.get("selector") or step.get("locator") or ""
    preferred_str = str(preferred or "").strip()
    if preferred_str and not _looks_like_url(preferred_str):
        return preferred_str
    return 'button:has-text("Action")'


def _render_intent_key(step: Dict[str, Any]) -> str:
    allowed = _available_intent_keys()
    key = (step.get("intent_key") or "").strip().lower()
    if key in allowed:
        return key
    text_blob = " ".join(
        [
            str(step.get("action") or ""),
            str(step.get("name") or ""),
            str(step.get("selector") or ""),
            str(step.get("locator") or ""),
            str(step.get("hint") or ""),
        ]
    ).lower()
    # Intent mapping is config-driven: pick best token-overlap with known keys.
    tokenized_blob = set(re.split(r"[^a-z0-9]+", text_blob))
    best_key = "generic"
    best_score = 0
    for intent in allowed:
        parts = set(p for p in intent.split("_") if p)
        if not parts:
            continue
        overlap = len(parts & tokenized_blob)
        if overlap > best_score:
            best_score = overlap
            best_key = intent
    if best_score > 0:
        return best_key
    return "generic"


def _interactable_selector_candidates(node: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    test_id = str(node.get("test_id") or "").strip()
    role = str(node.get("role") or "").strip()
    text = str(node.get("text") or "").strip()
    aria = str(node.get("aria_label") or "").strip()
    element_id = str(node.get("id") or "").strip()
    tag = str(node.get("tag") or "").strip() or "button"

    if test_id:
        candidates.append(f'[data-testid="{test_id}"]')
    if role and text:
        short_text = text.replace('"', '\\"')[:40]
        candidates.append(f'{tag}[role="{role}"]:has-text("{short_text}")')
    if aria:
        aria_escaped = aria.replace('"', '\\"')
        candidates.append(f'{tag}[aria-label="{aria_escaped}"]')
    if element_id:
        candidates.append(f'#{element_id}')
    if text:
        short_text = text.replace('"', '\\"')[:40]
        candidates.append(f'{tag}:has-text("{short_text}")')
    for hint in node.get("selector_hints") or []:
        h = str(hint).strip()
        if h:
            candidates.append(h)
    # Deduplicate while preserving order.
    out: List[str] = []
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out[:8]


def _collect_interactables(crawl_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for route in crawl_summary.get("routes") or []:
        route_url = route.get("url") or ""
        for node in (route.get("interactables") or [])[:200]:
            rows.append(
                {
                    "route_url": route_url,
                    "node": node,
                }
            )
    return rows


def _validate_selectors_from_ui_knowledge(
    *,
    selectors: List[str],
    crawl_summary: Dict[str, Any],
) -> Dict[str, str]:
    known: set[str] = set()
    for route in crawl_summary.get("routes") or []:
        for node in route.get("interactables") or []:
            for candidate in _interactable_selector_candidates(node):
                if candidate:
                    known.add(str(candidate).strip())
            raw_hints = node.get("selector_hints") or []
            for hint in raw_hints:
                h = str(hint or "").strip()
                if h:
                    known.add(h)

    missing_map: Dict[str, str] = {}
    generic_auth_selectors = {
        'button[type="submit"]',
        "button[type='submit']",
        'input[type="email"]',
        "input[type='email']",
        'input[type="password"]',
        "input[type='password']",
    }
    for selector in selectors:
        s = str(selector or "").strip()
        if not s:
            continue
        if s.lower() in generic_auth_selectors:
            continue
        if s in known:
            continue
        missing_map[s] = "selector not found in ui_knowledge baseline routes"
    return missing_map

def _build_selector_map(crawl_summary: Dict[str, Any]) -> Dict[str, str]:
    """
    Compress crawl output into deterministic selector map.
    This massively improves local LLM stability.
    """
    selector_map: Dict[str, str] = {}

    for route in crawl_summary.get("routes") or []:
        for node in route.get("interactables") or []:

            key_parts = []

            if node.get("test_id"):
                key_parts.append(str(node["test_id"]).lower())

            if node.get("text"):
                key_parts.append(
                    str(node["text"]).lower().replace(" ", "_")[:30]
                )

            if node.get("id"):
                key_parts.append(str(node["id"]).lower())

            if not key_parts:
                continue

            key = "_".join(key_parts[:2])

            candidates = _interactable_selector_candidates(node)
            if candidates and key not in selector_map:
                selector_map[key] = candidates[0]

    return selector_map



def _pick_best_selector(
    crawl_summary: Dict[str, Any],
    hints: List[str],
    default_selector: str,
    *,
    action_kind: str = "click",
    action_text: str = "",
    fill_target_tokens: List[str] | None = None,
) -> str:
    rows = _collect_interactables(crawl_summary)[:300]
    if not rows:
        return default_selector
    hint_tokens = set()
    for h in hints:
        hint_tokens.update(_tokenize(str(h)))
    if not hint_tokens:
        hint_tokens.update(_tokenize(default_selector))

    best_score = -1
    best_selector = default_selector
    action_text_l = (action_text or "").lower()
    target_tokens = set(t for t in (fill_target_tokens or []) if t)
    for row in rows:
        route_url = str(row.get("route_url") or "").lower()
        node = row["node"]
        selector_hints_blob = " ".join(str(h or "") for h in (node.get("selector_hints") or [])[:8])
        blob_parts = [
            str(node.get("text") or ""),
            str(node.get("aria_label") or ""),
            str(node.get("test_id") or ""),
            str(node.get("id") or ""),
            str(node.get("role") or ""),
            str(node.get("href") or ""),
            selector_hints_blob,
        ]
        tokens = set(_tokenize(" ".join(blob_parts)))
        overlap = len(tokens & hint_tokens)
        candidates = _interactable_selector_candidates(node)
        if not candidates:
            continue
        tag = str(node.get("tag") or "").strip().lower()
        is_fillable = tag in {"input", "textarea", "select"} or any(
            re.search(r"\b(input|textarea|select)\b", c) for c in candidates
        )
        role = str(node.get("role") or "").strip().lower()
        node_text = " ".join(
            [
                str(node.get("text") or "").lower(),
                str(node.get("aria_label") or "").lower(),
                str(node.get("test_id") or "").lower(),
                str(node.get("href") or "").lower(),
            ]
        )
        is_clickable = (
            tag in {"button", "a", "summary"}
            or role in {"button", "link", "menuitem"}
            or any(re.search(r"\b(button|a\[|href=|role=\"button\"|role=\"link\")", c) for c in candidates)
        )

        # Route-aware journey signals: improve selector relevance across multi-page flows.
        if "view details" in action_text_l:
            if route_url.endswith("/") or route_url.rstrip("/").endswith("localhost:3000"):
                overlap += 3
            if "/cart" in route_url:
                overlap -= 2
        if "add to cart" in action_text_l:
            if "/product" in route_url:
                overlap += 3
            if "/cart" in route_url:
                overlap -= 1
        if ("go to cart" in action_text_l) or ("open cart" in action_text_l):
            if "/cart" in route_url:
                overlap += 4
        if any(k in action_text_l for k in ("coupon", "apply", "enter code")) and "/cart" in route_url:
            overlap += 3

        if action_kind == "fill":
            if is_fillable:
                overlap += 4
            else:
                overlap -= 3
            # Prefer fields whose id/name/label/aria tokens match target field hints (generic across apps).
            if target_tokens:
                target_overlap = len(tokens & target_tokens)
                overlap += target_overlap * 5
                if target_overlap == 0:
                    overlap -= 2
            node_type = str(node.get("type") or "").strip().lower()
            if "email" in target_tokens and node_type == "email":
                overlap += 6
            if any(t in target_tokens for t in {"message", "comment", "description", "details", "note"}):
                if tag == "textarea":
                    overlap += 6
                elif tag == "input":
                    overlap -= 1
            if any(k in action_text_l for k in ("code", "coupon", "promo", "discount", "voucher", "enter")):
                if any(k in tokens for k in ("code", "coupon", "promo", "discount", "voucher", "enter")):
                    overlap += 2
        elif action_kind == "click":
            if is_clickable:
                overlap += 2
            else:
                overlap -= 2
            if "view details" in action_text_l:
                if "view details" in node_text or "view" in tokens:
                    overlap += 4
                if "apply" in tokens or "apply" in node_text:
                    overlap -= 4
            if "apply" in action_text_l and "apply" in tokens:
                overlap += 3
                if "apply" in node_text:
                    overlap += 2
                # Avoid broad section/container selectors when user explicitly asks to click a button.
                if tag in {"div", "section", "article", "main"}:
                    overlap -= 4
                if "section" in str(node.get("test_id") or "").lower():
                    overlap -= 3
            if "button" in action_text_l and tag != "button":
                overlap -= 2
            if "add" in action_text_l and "cart" in action_text_l and ("add" in tokens or "cart" in tokens):
                overlap += 2
            if "go to cart" in action_text_l or ("go" in action_text_l and "cart" in action_text_l):
                # Reject container selectors for navigation intent.
                if "coupon-section" in str(node.get("test_id") or "").lower():
                    overlap -= 6
                if "cart" in node_text:
                    overlap += 3
                if tag in {"a", "button"}:
                    overlap += 1
                if any("/cart" in c.lower() for c in candidates):
                    overlap += 5
            if "coupon section" in action_text_l and "apply" in action_text_l:
                if "apply" in node_text or any("apply" in c.lower() for c in candidates):
                    overlap += 6
        if overlap > best_score:
            best_score = overlap
            best_selector = candidates[0]
    return best_selector


def _render_single_assertion_plan(
    assertion_item: Any,
    crawl_summary: Dict[str, Any],
    fallback_selector: str,
) -> Dict[str, List[str]]:
    strict_lines: List[str] = []
    fallback_lines: List[str] = []

    if isinstance(assertion_item, dict):
        a_type = str(assertion_item.get("type") or "").strip().lower()
        target = assertion_item.get("target") or {}
        if a_type == "url_contains":
            value = str(assertion_item.get("value") or target.get("value") or "").strip().replace("/", "\\/")
            if value:
                return {"strict": [f"  await expect(page).toHaveURL(/{value}/);"], "fallback": []}
        if a_type == "visible":
            strategy = str(target.get("strategy") or "").strip().lower()
            value = str(target.get("value") or "").strip()
            if strategy == "testid" and value:
                return {"strict": [f"  await expect(page.locator('[data-testid=\"{value}\"]')).toBeVisible();"], "fallback": []}
            if strategy == "selector" and value:
                escaped = value.replace("'", "\\'")
                return {"strict": [f"  await expect(page.locator('{escaped}')).toBeVisible();"], "fallback": []}

    text = str(assertion_item).strip()
    if not text:
        return {"strict": ["  await expect(page.locator('body')).toBeVisible();"], "fallback": []}

    lower = text.lower()
    quoted = re.search(r"'([^']+)'", text)
    if any(k in lower for k in ("logged in", "login successful", "logged-in user greeting", "account menu", "no login error")):
        return {
            "strict": [
                "  const greeting = page.getByText(/Ahoy,/i).first();",
                "  const account = page.getByText(/My Account|My Profile|Manage/i).first();",
                "  const greetingVisible = await greeting.isVisible().catch(() => false);",
                "  const accountVisible = await account.isVisible().catch(() => false);",
                "  await expect(greetingVisible || accountVisible).toBeTruthy();",
            ],
            "fallback": [],
        }
    if "url" in lower and "/" in text:
        path = "/" + text.split("/")[-1].strip()
        path_regex = path.replace("/", "\\/")
        return {"strict": [f"  await expect(page).toHaveURL(/{path_regex}/);"], "fallback": []}

    if any(k in lower for k in ("popup", "modal", "dialog")):
        strict_lines.append("  await expect(page.locator('[role=\"dialog\"], .modal, [aria-modal=\"true\"]').first()).toBeVisible();")
        fallback_lines.append("  await expect(page.locator('body')).toBeVisible();")
        return {"strict": strict_lines, "fallback": fallback_lines}

    if any(k in lower for k in ("iframe", "frame")):
        strict_lines.append("  await expect(page.frameLocator('iframe').first().locator('body')).toBeVisible();")
        fallback_lines.append("  await expect(page.locator('iframe').first()).toBeVisible();")
        return {"strict": strict_lines, "fallback": fallback_lines}

    is_error = any(k in lower for k in ("error", "invalid", "failed", "failure", "unable", "blocked", "denied"))
    is_success = any(k in lower for k in ("success", "applied", "completed", "persist", "saved", "confirmed"))

    if is_success:
        if quoted:
            expected = quoted.group(1).replace("'", "\\'")
            strict_lines.append(f"  await expect(page.getByText('{expected}', {{ exact: false }})).toBeVisible();")
        else:
            strict_lines.append("  await expect(page.getByText(/success|applied|completed|confirmed/i)).toBeVisible();")
        fallback_lines.append("  await expect(page.getByText(/thanks|submitted|saved|done|success/i).first()).toBeVisible();")
        return {"strict": strict_lines, "fallback": fallback_lines}

    if is_error:
        if quoted:
            expected = quoted.group(1).replace("'", "\\'")
            strict_lines.append(f"  await expect(page.getByText('{expected}', {{ exact: false }})).toBeVisible();")
        else:
            strict_lines.append("  await expect(page.locator('.text-danger, .alert-danger, [role=\"alert\"]').first()).toBeVisible();")
        fallback_lines.append("  await expect(page.getByText(/invalid|error|failed|unable/i)).toBeVisible();")
        return {"strict": strict_lines, "fallback": fallback_lines}

    if "discount" in lower:
        return {
            "strict": ["  await expect(page.getByText(/discount/i)).toBeVisible();"],
            "fallback": ["  await expect(page.getByText(/total|summary/i).first()).toBeVisible();"],
        }

    if "total" in lower:
        return {
            "strict": ["  await expect(page.getByText(/total/i)).toBeVisible();"],
            "fallback": ["  await expect(page.locator('body')).toBeVisible();"],
        }

    if "change" in lower or "updated" in lower or "refresh" in lower or "dynamic" in lower:
        strict_lines.append("  await expect(page.locator('body')).toBeVisible();")
        fallback_lines.append("  await expect(page.getByText(/total|updated|last|summary/i)).toBeVisible();")
        return {"strict": strict_lines, "fallback": fallback_lines}

    if quoted:
        expected = quoted.group(1).replace("'", "\\'")
        return {"strict": [f"  await expect(page.getByText('{expected}', {{ exact: false }})).toBeVisible();"], "fallback": []}

    selector = _pick_best_selector(crawl_summary, [text], fallback_selector)
    escaped = selector.replace("'", "\\'")
    strict_lines.append(f"  await expect(page.locator('{escaped}')).toBeVisible();")
    fallback_lines.append("  await expect(page.locator('body')).toBeVisible();")
    return {"strict": strict_lines, "fallback": fallback_lines}


def _count_lines(text: str) -> Dict[str, int]:
    """Count line occurrences (trimmed of leading/trailing whitespace).

    Used by `_validate_artifact_content` to detect the "LLM stuck in a loop"
    degeneracy where hundreds of identical whitespace-only or single-token
    lines dominate the output.
    """
    counts: Dict[str, int] = {}
    for line in (text or "").splitlines():
        key = line.strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


_URL_SCHEMES = {
    "http", "https", "mailto", "about", "javascript",
    "file", "ftp", "ws", "wss", "data", "blob", "tel",
}


def _looks_like_url(value: str) -> bool:
    """
    Return True for anything that clearly is not a CSS/XPath selector — URL paths,
    fully-qualified URLs, mailto:, about:, javascript:, etc.

    Careful: CSS selectors like `a:has-text(...)`, `button:hover`, or
    `li:nth-child(3)` also match the naive `^\\w+:` pattern, so we allow-list
    only known URL schemes. `//` (protocol-relative URLs) also counts.
    """
    s = str(value or "").strip()
    if not s:
        return False
    # Bare `/` or path-only strings starting with a single `/` and containing
    # no CSS-selector characters. `//foo/bar` is a protocol-relative URL, not
    # a CSS selector.
    if s == "/" or s.startswith("//"):
        return True
    if s.startswith("/") and not re.search(r"[\[\]\.\#>:*+,()~=\s]", s):
        return True
    # Scheme URL: only if the prefix before `:` is a known URL scheme. Anything
    # else (`a:`, `button:`, `li:` — all valid CSS) is left alone.
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", s)
    if match and match.group(1).lower() in _URL_SCHEMES:
        return True
    return False


def _extract_selector_literals_from_text(text: str) -> List[str]:
    values: List[str] = []
    # view.primaryAction('selector')
    for match in re.finditer(r"primaryAction\('([^']+)'\)", text):
        values.append(match.group(1))
    # page.locator('selector')
    for match in re.finditer(r"page\.locator\('([^']+)'\)", text):
        values.append(match.group(1))
    # selfHealingClick failed selector literal (3rd arg)
    for match in re.finditer(r"selfHealingClick\(\s*[\s\S]*?,\s*[\s\S]*?,\s*'([^']+)'", text):
        values.append(match.group(1))
    out: List[str] = []
    seen = set()
    for v in values:
        # Drop URL-shaped literals early — they can't be verified as selectors
        # and produce noisy false positives ("selector `/` not found on route").
        if not v or v in seen or _looks_like_url(v):
            continue
        seen.add(v)
        out.append(v)
    return out


def _is_universal_selector(selector: str) -> bool:
    s = str(selector or "").strip().lower()
    if not s:
        return True
    return s in {"body", "html", ":root", "*", "document", "page", "body *"}


def _extract_feature_keywords(job: GenerationJob) -> List[str]:
    blob = f"{job.feature_name} {job.feature_description}".lower()
    tokens = [t.strip() for t in re.split(r"[^a-z0-9]+", blob) if len(t.strip()) >= 4]
    # Keep meaningful unique words for feature-presence checks.
    ignored = {"user", "with", "from", "page", "flow", "item", "feature", "see", "validation"}
    out = []
    for token in tokens:
        if token in ignored:
            continue
        if token not in out:
            out.append(token)
    return out[:8]


def _feature_presence_report(job: GenerationJob, crawl_summary: Dict[str, Any]) -> Dict[str, Any]:
    keywords = _extract_feature_keywords(job)
    primary_feature = (job.feature_name or "").strip().lower()
    primary_tokens = [t for t in re.split(r"[^a-z0-9]+", primary_feature) if len(t) >= 4]
    min_score = _feature_presence_min_score()
    routes = crawl_summary.get("routes") or []
    if not keywords:
        return {
            "keywords": [],
            "matched_keywords": [],
            "coverage_score": 0.0,
            "required_min_score": min_score,
            "primary_tokens": primary_tokens,
            "primary_feature_matched": False,
            "feature_likely_present": False,
        }
    corpus_parts = []
    for route in routes:
        corpus_parts.append(str(route.get("url") or ""))
        corpus_parts.append(str(route.get("title") or ""))
        for node in (route.get("interactables") or [])[:200]:
            corpus_parts.append(str(node.get("text") or ""))
            corpus_parts.append(str(node.get("aria_label") or ""))
            corpus_parts.append(str(node.get("test_id") or ""))
            corpus_parts.append(str(node.get("id") or ""))
    corpus = " ".join(corpus_parts).lower()
    matched = [kw for kw in keywords if kw in corpus]
    score = round((len(matched) / max(len(keywords), 1)), 3)
    primary_match = any(token in corpus for token in primary_tokens) if primary_tokens else False
    likely_present = (score >= min_score) or primary_match
    return {
        "keywords": keywords,
        "matched_keywords": matched,
        "coverage_score": score,
        "required_min_score": min_score,
        "primary_tokens": primary_tokens,
        "primary_feature_matched": primary_match,
        "feature_likely_present": likely_present,
    }


def _call_ollama_json(
    *,
    prompt: str,
    model: str,
    temperature: float,
    timeout_seconds: int,
    num_predict: int,
) -> Dict[str, Any]:
    effective_timeout = _effective_llm_timeout(timeout_seconds, num_predict)

    def _post_json(url: str) -> str:
        req = urllib_request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        logger.info("TEST_GEN_LLM request started url=%s model=%s timeout=%s", url, model, effective_timeout)
        with urllib_request.urlopen(req, timeout=effective_timeout) as response:
            raw_bytes = response.read()
            logger.info(
                "TEST_GEN_LLM response received url=%s status=%s bytes=%s",
                url,
                getattr(response, "status", "NA"),
                len(raw_bytes or b""),
            )
            return raw_bytes.decode("utf-8")

    def _extract_json_fragment(text: str) -> str:
        """
        Return the first balanced `{ … }` JSON object found in `text`, or a
        best-effort *repaired* fragment when the LLM truncated its output
        mid-way (Ollama capped `num_predict`).

        Truncation-recovery strategy:
          - Walk brace/quote depth exactly like the strict pass.
          - When we hit end-of-text still open (in_string or depth > 0), close
            the open string (if any), then discard any trailing unterminated
            array element by trimming back to the last comma outside strings,
            then append `]` for every unclosed array and `}` for every
            unclosed object. Try to parse. If it still doesn't parse, return
            the strict pass's empty string.
        """
        raw_text = (text or "").strip()
        if not raw_text:
            return ""
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r"\s*```$", "", raw_text)

        start = raw_text.find("{")
        if start < 0:
            return ""

        # ---- Strict pass: return first balanced object ----------------------
        depth_obj = 0
        depth_arr = 0
        in_string = False
        escaped = False
        for idx in range(start, len(raw_text)):
            ch = raw_text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth_obj += 1
                continue
            if ch == "}":
                depth_obj -= 1
                if depth_obj == 0 and depth_arr == 0:
                    return raw_text[start : idx + 1]
                continue
            if ch == "[":
                depth_arr += 1
                continue
            if ch == "]":
                depth_arr -= 1
                continue

        # ---- Truncation-recovery pass --------------------------------------
        # Ran off the end of the buffer with unclosed structure. Try to repair.
        body = raw_text[start:]
        # If we ended inside a string, force-close it. Escapes at the tail can
        # produce invalid JSON — best effort only.
        if in_string:
            body += '"'
        # Trim back to the last comma outside strings to drop a half-emitted
        # array element. Cheap heuristic: walk from end, stop at first `,` that
        # isn't inside a string.
        in_s = False
        esc = False
        trim_at = None
        for i in range(len(body) - 1, -1, -1):
            c = body[i]
            if in_s:
                # We're walking backwards, so treat backslash-before-quote as
                # escape indicator (imperfect but good enough).
                if c == '"' and not (i > 0 and body[i - 1] == "\\"):
                    in_s = False
                continue
            if c == '"':
                in_s = True
                continue
            if c == "," and not in_s:
                trim_at = i
                break
        if trim_at is not None:
            body = body[:trim_at]

        # Now close any still-open arrays / objects. Recount from the trimmed body.
        depth_obj = 0
        depth_arr = 0
        in_string = False
        escaped = False
        for ch in body:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth_obj += 1
            elif ch == "}":
                depth_obj -= 1
            elif ch == "[":
                depth_arr += 1
            elif ch == "]":
                depth_arr -= 1
        if in_string:
            body += '"'
        body += "]" * max(0, depth_arr)
        body += "}" * max(0, depth_obj)

        # Final sanity: only return if it parses. The caller will try
        # json.loads on it and give up cleanly if not.
        try:
            json.loads(body)
        except Exception:
            return ""
        logger.warning(
            "TEST_GEN_LLM output was truncated; recovered a partial JSON fragment "
            "(orig_len=%d, recovered_len=%d).", len(raw_text), len(body),
        )
        return body

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            # --- Anti-repetition sampling -----------------------------------
            # qwen2.5:7b (base) has been observed to degenerate into
            # `\n \t\t\t\n \t\t\t\n` loops inside long escaped-string values
            # (e.g. multi-line .feature file content). Ollama's default
            # `repeat_penalty` (1.1) is too weak here. Nudge these knobs so
            # the sampler penalizes recent tokens harder without harming
            # normal code output. Env vars let you tune per-deployment.
            "repeat_penalty": float(os.getenv("TEST_GEN_REPEAT_PENALTY", "1.3")),
            "repeat_last_n": int(os.getenv("TEST_GEN_REPEAT_LAST_N", "256")),
            "top_p": float(os.getenv("TEST_GEN_TOP_P", "0.9")),
            "top_k": int(os.getenv("TEST_GEN_TOP_K", "40")),
        },
    }
    
    llm_url = _llm_url()
    alt_url = llm_url.rstrip("/") if llm_url.endswith("/") else f"{llm_url}/"
    raw = ""
    last_exc: Exception | None = None
    attempt_errors: List[str] = []
    for candidate_url in [llm_url, alt_url]:
        try:
            raw = _post_json(candidate_url)
            last_exc = None
            break
        except HTTPError as exc:
            body = ""
            try:
                body = (exc.read() or b"").decode("utf-8", errors="replace")[:500]
            except Exception:
                body = ""
            message = f"url={candidate_url} http={exc.code} reason={exc.reason} body={body}"
            attempt_errors.append(message)
            logger.exception("TEST_GEN_LLM HTTP error: %s", message)
            last_exc = exc
            continue
        except (URLError, TimeoutError, socket.timeout) as exc:
            message = f"url={candidate_url} error={str(exc)}"
            attempt_errors.append(message)
            logger.exception("TEST_GEN_LLM network/timeout error: %s", message)
            last_exc = exc
            continue
        except Exception as exc:
            message = f"url={candidate_url} unexpected={type(exc).__name__}:{str(exc)}"
            attempt_errors.append(message)
            logger.exception("TEST_GEN_LLM unexpected error: %s", message)
            last_exc = exc
            continue
    if last_exc:
        joined = " | ".join(attempt_errors)[:1200]
        raise ValueError(f"LLM request failed for all URL attempts. {joined}")

    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        raise ValueError("LLM response envelope is not a JSON object")
    if parsed.get("error"):
        raise ValueError(f"LLM returned error: {str(parsed.get('error'))}")

    if isinstance(parsed.get("response"), dict):
        return parsed["response"]

    # Ollama /api/generate commonly returns a JSON string in `response`.
    candidates: List[str] = []
    if isinstance(parsed.get("response"), str):
        candidates.append(parsed["response"])
    message = parsed.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        candidates.append(message["content"])
    for key in ("output", "text", "content"):
        value = parsed.get(key)
        if isinstance(value, str):
            candidates.append(value)

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            fragment = _extract_json_fragment(candidate)
            if fragment:
                try:
                    decoded = json.loads(fragment)
                    if isinstance(decoded, dict):
                        return decoded
                except json.JSONDecodeError:
                    pass

    # As a final fallback, if envelope itself looks like expected object, return it.
    if "scenarios" in parsed or "page_objects" in parsed or "specs" in parsed:
        return parsed

    snippet = (raw or "")[:600]
    raise ValueError(f"LLM response does not contain valid JSON payload. raw={snippet}")


def _normalize_planning_payload(planning: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(planning, dict):
        return {}
    normalized = dict(planning)
    scenarios = normalized.get("scenarios")
    if not isinstance(scenarios, list):
        for alias in ("test_scenarios", "cases", "test_cases", "scenario_list", "flows"):
            candidate = normalized.get(alias)
            if isinstance(candidate, list):
                scenarios = candidate
                break
    if isinstance(scenarios, list):
        normalized["scenarios"] = scenarios
    else:
        normalized["scenarios"] = []
    if not isinstance(normalized.get("notes"), list):
        normalized["notes"] = []
    if not isinstance(normalized.get("feature_summary"), str):
        normalized["feature_summary"] = str(normalized.get("feature_summary") or "")
    return normalized


def _normalize_codegen_payload(codegen: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(codegen, dict):
        return {"page_objects": [], "specs": [], "notes": []}
    normalized: Dict[str, Any] = dict(codegen)
    page_objects = normalized.get("page_objects")
    specs = normalized.get("specs")
    notes = normalized.get("notes")

    if not isinstance(page_objects, list):
        page_objects = []
    if not isinstance(specs, list):
        specs = []
    if not isinstance(notes, list):
        notes = []

    # Common alternative keys returned by LLMs.
    if not page_objects:
        alt_po = normalized.get("pageObjects") or normalized.get("pages")
        if isinstance(alt_po, list):
            page_objects = alt_po
    if not specs:
        alt_specs = normalized.get("tests") or normalized.get("test_specs")
        if isinstance(alt_specs, list):
            specs = alt_specs

    # Generic artifact list support.
    artifacts = normalized.get("artifacts")
    if isinstance(artifacts, list):
        for art in artifacts:
            if not isinstance(art, dict):
                continue
            kind = str(art.get("type") or art.get("artifact_type") or "").strip().lower()
            path = str(art.get("path") or art.get("relative_path") or "").strip()
            content = str(art.get("content") or art.get("code") or "").strip()
            row = {"path": path, "content": content}
            if kind in {"spec", "test", "test_spec"}:
                specs.append(row)
            elif kind in {"page_object", "page", "po"}:
                page_objects.append(row)
            elif path.endswith(".spec.ts"):
                specs.append(row)
            elif path.endswith(".ts"):
                page_objects.append(row)

    # files map support: {"tests/generated/a.spec.ts":"...","tests/pages/generated/A.ts":"..."}
    files = normalized.get("files")
    if isinstance(files, dict):
        for path, content in files.items():
            path_str = str(path or "").strip()
            content_str = str(content or "")
            if not path_str:
                continue
            row = {"path": path_str, "content": content_str}
            if path_str.endswith(".spec.ts"):
                specs.append(row)
            elif path_str.endswith(".ts"):
                page_objects.append(row)

    # Single-file fallback shapes.
    single_path = str(normalized.get("path") or normalized.get("relative_path") or "").strip()
    single_content = str(normalized.get("content") or normalized.get("code") or "").strip()
    if single_path and single_content:
        row = {"path": single_path, "content": single_content}
        if single_path.endswith(".spec.ts"):
            specs.append(row)
        elif single_path.endswith(".ts"):
            page_objects.append(row)

    def _unique_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen = set()
        for r in rows:
            path = str(r.get("path") or "").strip()
            content = str(r.get("content") or "")
            if not path or not content:
                continue
            key = (path, content[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": path, "content": content})
        return out

    return {
        "page_objects": _unique_rows(page_objects),
        "specs": _unique_rows(specs),
        "notes": [str(n) for n in notes[:30]],
    }


def _run_node_crawl_context(
    *,
    base_url: str,
    seed_urls: List[str],
    max_routes: int,
) -> Dict[str, Any]:
    repo_root = _repo_root()
    script_path = repo_root / "wraper-healer" / "crawlContext.mjs"
    if not script_path.exists():
        return {
            "base_url": base_url,
            "seed_urls": seed_urls,
            "routes": [],
            "warnings": [f"Crawl script missing at {script_path}"],
        }

    cmd = [
        "node",
        str(script_path),
        "--base-url",
        base_url,
        "--seed-urls",
        json.dumps(seed_urls),
        "--max-routes",
        str(max_routes),
        "--max-depth",
        "2",
        "--max-interactables",
        "200",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception as exc:
        return {
            "base_url": base_url,
            "seed_urls": seed_urls,
            "routes": [],
            "warnings": [f"crawl subprocess failed: {str(exc)}"],
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "base_url": base_url,
            "seed_urls": seed_urls,
            "routes": [],
            "warnings": [f"crawl failed rc={proc.returncode}", stderr[:1000]],
        }
    try:
        parsed = json.loads(stdout) if stdout else {}
        if isinstance(parsed, dict):
            if stderr:
                warnings = parsed.get("warnings") or []
                warnings.append(stderr[:1000])
                parsed["warnings"] = warnings
            return parsed
    except json.JSONDecodeError:
        pass
    return {
        "base_url": base_url,
        "seed_urls": seed_urls,
        "routes": [],
        "warnings": [f"crawl returned non-json output: {stdout[:1000]}"],
    }


def _run_ui_knowledge_context(
    *,
    base_url: str,
    seed_urls: List[str],
    max_routes: int,
) -> Dict[str, Any]:
    try:
        from ui_knowledge.models import UIPage, UIRouteSnapshot
    except Exception as exc:
        return {
            "base_url": base_url,
            "seed_urls": seed_urls,
            "routes": [],
            "warnings": [f"ui_knowledge import failed: {str(exc)}"],
        }

    routes: List[Dict[str, Any]] = []
    warnings: List[str] = []

    try:
        pages = list(UIPage.objects.filter(is_active=True).order_by("route")[: max(max_routes, 1) * 3])
    except Exception as exc:
        return {
            "base_url": base_url,
            "seed_urls": seed_urls,
            "routes": [],
            "warnings": [f"ui_knowledge query failed: {str(exc)}"],
        }

    for page in pages:
        if len(routes) >= max_routes:
            break
        snapshot = (
            UIRouteSnapshot.objects.filter(page=page, snapshot_type="BASELINE", is_current=True)
            .order_by("-version")
            .first()
        )
        if not snapshot:
            snapshot = (
                UIRouteSnapshot.objects.filter(page=page, snapshot_type="BASELINE")
                .order_by("-version")
                .first()
            )
        if not snapshot:
            snapshot = UIRouteSnapshot.objects.filter(page=page, is_current=True).order_by("-version").first()
        if not snapshot:
            continue

        page_route = str(page.route or "").strip()
        route_url = urljoin(base_url.rstrip("/") + "/", page_route.lstrip("/")) if page_route else base_url

        interactables: List[Dict[str, Any]] = []
        for el in snapshot.elements.all()[:200]:
            selector_hints = [str(el.selector or "").strip()] if str(el.selector or "").strip() else []
            if el.test_id:
                selector_hints.append(f'[data-testid="{el.test_id}"]')
            if el.text:
                short_text = str(el.text).replace('"', '\\"')[:40]
                selector_hints.append(f'{(el.tag or "button")}:has-text("{short_text}")')
            selector_hints = list(dict.fromkeys([s for s in selector_hints if s]))[:8]

            interactables.append(
                {
                    "tag": str(el.tag or ""),
                    "role": str(el.role or ""),
                    "test_id": str(el.test_id or ""),
                    "aria_label": "",
                    "id": str(el.element_id or ""),
                    "name": "",
                    "type": "",
                    "text": str(el.text or ""),
                    "href": "",
                    "selector_hints": selector_hints,
                    "selector_score": max(1.0, float(el.stability_score or 1.0) * 100.0),
                    "intent_key": str(el.intent_key or "generic").strip().lower() or "generic",
                }
            )

        snapshot_json = snapshot.snapshot_json if isinstance(snapshot.snapshot_json, dict) else {}
        if not interactables and isinstance(snapshot_json.get("interactables"), list):
            interactables = list(snapshot_json.get("interactables") or [])[:200]

        route_row = {
            "url": route_url,
            "title": str(page.title or snapshot_json.get("title") or ""),
            "depth": int(snapshot_json.get("depth") or 0),
            "interactables": interactables,
            "forms": list(snapshot_json.get("forms") or [])[:20] if isinstance(snapshot_json.get("forms"), list) else [],
            "dom_hash": str(snapshot.dom_hash or snapshot_json.get("dom_hash") or ""),
            "feature_name": str(page.feature_name or ""),
            "source": "ui_knowledge",
        }
        routes.append(route_row)

    if not routes:
        warnings.append("No baseline/current UI knowledge routes found.")

    return {
        "base_url": base_url,
        "seed_urls": seed_urls,
        "max_routes": max_routes,
        "routes_visited": len(routes),
        "routes": routes,
        "warnings": warnings,
        "source": "ui_knowledge",
    }


def _run_crawl_context(
    *,
    base_url: str,
    seed_urls: List[str],
    max_routes: int,
) -> Dict[str, Any]:
    if _use_ui_knowledge_source():
        ui_summary = _run_ui_knowledge_context(
            base_url=base_url,
            seed_urls=seed_urls,
            max_routes=max_routes,
        )
        
        if ui_summary.get("routes"):
            return ui_summary
        if not _allow_live_crawl_fallback():
            return ui_summary
        fallback = _run_node_crawl_context(base_url=base_url, seed_urls=seed_urls, max_routes=max_routes)
        warnings = ui_summary.get("warnings") or []
        warnings.append("Falling back to live crawl because ui_knowledge had no usable routes.")
        fallback["warnings"] = (fallback.get("warnings") or []) + warnings
        fallback["source"] = "live_crawl_fallback"
        return fallback
    return _run_node_crawl_context(base_url=base_url, seed_urls=seed_urls, max_routes=max_routes)


def _fallback_scenarios(job: GenerationJob, crawl_summary: Dict[str, Any]) -> Dict[str, Any]:
    feature = job.feature_name or "Generated Feature"
    routes = crawl_summary.get("routes") or []
    first_route = routes[0] if routes else {}
    first_nodes = (first_route.get("interactables") or [])[:20]
    primary = first_nodes[0] if first_nodes else {}
    primary_selector_hints = primary.get("selector_hints") or []
    primary_selector = primary_selector_hints[0] if primary_selector_hints else 'button:has-text("Continue")'
    primary_label = (primary.get("text") or primary.get("aria_label") or "primary action").strip() or "primary action"
    feature_presence = _feature_presence_report(job, crawl_summary)
    notes = ["Fallback scenarios used because LLM planning was unavailable or invalid."]
    if not feature_presence.get("feature_likely_present"):
        notes.append(
            "Requested feature keywords were not found strongly in crawled UI. "
            "Generated draft may be low-confidence until feature is implemented."
        )
    return {
        "feature_summary": f"Auto-generated baseline scenarios for {feature} (generic mode)",
        "scenarios": [
            {
                "id": "smoke_1",
                "title": f"{feature} smoke flow",
                "type": "SMOKE",
                "preconditions": [f"Open {job.base_url}"],
                "steps": [
                    {"action": "navigate to seed page", "selector": (first_route.get("url") or "/")},
                    {"action": f"perform {primary_label}", "selector": primary_selector, "intent_key": "generic"},
                ],
                "assertions": ["Primary flow action is reachable without runtime error"],
            },
            {
                "id": "negative_1",
                "title": f"{feature} negative validation flow",
                "type": "NEGATIVE",
                "preconditions": [f"Open {job.base_url}"],
                "steps": [
                    {"action": "trigger negative path for same action context", "selector": primary_selector, "intent_key": "generic"},
                ],
                "assertions": ["Validation, guard message, or safe failure signal appears"],
            },
        ],
        "notes": notes,
        "crawl_routes_seen": len(crawl_summary.get("routes") or []),
        "feature_presence": feature_presence,
    }


# def _build_planning_prompt(job: GenerationJob, crawl_summary: Dict[str, Any]) -> str:
#     intent_catalog = _available_intent_keys()
#     feature_presence = _feature_presence_report(job, crawl_summary)
#     return (
#         "You are a senior QA automation architect.\n"
#         "Return STRICT JSON only. No markdown, no prose.\n"
#         "Schema:\n"
#         "{\n"
#         '  "feature_summary": "string",\n'
#         '  "scenarios": [\n'
#         "    {\n"
#         '      "id": "string_short_unique",\n'
#         '      "title": "string",\n'
#         '      "type": "SMOKE|NEGATIVE",\n'
#         '      "preconditions": ["string"],\n'
#         '      "steps": [{"action":"string","selector":"string","intent_key":"string"}],\n'
#         '      "assertions": ["string"]\n'
#         "    }\n"
#         "  ],\n"
#         '  "notes": ["string"]\n'
#         "}\n"
#         f"Feature name: {job.feature_name}\n"
#         f"Feature description: {job.feature_description}\n"
#         f"Coverage mode: {job.coverage_mode}\n"
#         f"Intent hints: {json.dumps(job.intent_hints or [])}\n"
#         f"Allowed intent keys: {json.dumps(intent_catalog)}\n"
#         f"Max scenarios: {job.max_scenarios}\n"
#         f"Crawl summary: {json.dumps(crawl_summary)}\n"
#         f"Feature presence report: {json.dumps(feature_presence)}\n"
#         "Constraints:\n"
#         "- Include at least one SMOKE and one NEGATIVE scenario.\n"
#         "- Keep scenarios practical for Playwright UI tests.\n"
#         "- Keep selectors semantic and stable where possible.\n"
#         "- intent_key should come from allowed intent keys. Use 'generic' if unsure.\n"
#         "- If feature presence is weak, add a clear note and avoid inventing non-existent UI.\n"
#     )

def _build_planning_prompt(job: GenerationJob, crawl_summary: Dict[str, Any]) -> str:
    intent_catalog = _available_intent_keys()
    feature_presence = _feature_presence_report(job, crawl_summary)

    selector_map = _build_selector_map(crawl_summary)
    return (
        "You are a senior QA automation architect.\n"
        "Return STRICT JSON only.\n"
        "Schema:\n"
        "{"
        '"feature_summary":"string",'
        '"scenarios":[{'
        '"id":"string",'
        '"title":"string",'
        '"type":"SMOKE|NEGATIVE",'
        '"preconditions":["string"],'
        '"steps":[{"action":"string","selector":"string","intent_key":"string"}],'
        '"assertions":["string"]'
        "}],"
        '"notes":["string"]'
        "}\n"
        f"Feature name: {job.feature_name}\n"
        f"Feature description: {job.feature_description}\n"
        f"Allowed intent keys: {json.dumps(intent_catalog)}\n"
        f"Selector map: {json.dumps(selector_map)}\n"
        f"Feature presence: {json.dumps(feature_presence)}\n"
        "Rules:\n"
        "- DO NOT invent selectors.\n"
        "- Use selectors from selector map.\n"
        "- Include ALL scenarios.\n"
        
    )


def _scenario_to_comment_lines(scenario: Dict[str, Any]) -> str:
    lines = []
    for idx, step in enumerate(scenario.get("steps") or [], start=1):
        lines.append(f"  console.log('Generated step {idx}: {_render_step_name(step, 'action')}');")
    return "\n".join(lines) if lines else "  console.log('Generated scenario execution');"


def _build_template_artifacts(
    job: GenerationJob,
    planning: Dict[str, Any],
    crawl_summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    def _ident(text: str, prefix: str) -> str:
        parts = [p for p in re.split(r"[^a-zA-Z0-9]+", (text or "").strip()) if p]
        if not parts:
            return prefix
        first = parts[0].lower()
        rest = "".join(p[:1].upper() + p[1:] for p in parts[1:])
        out = f"{first}{rest}"
        if out[0].isdigit():
            return f"{prefix}{out.capitalize()}"
        return out

    def _method_name(text: str, prefix: str) -> str:
        base = _ident(text, prefix)
        if not base.startswith(prefix):
            return f"{prefix}{base[:1].upper()}{base[1:]}"
        return base

    def _infer_action_kind(step: Dict[str, Any]) -> str:
        action_text = " ".join(
            [
                str(step.get("action") or ""),
                str(step.get("name") or ""),
            ]
        ).lower()
        if any(k in action_text for k in ("click", "tap", "press", "submit")):
            return "click"
        if "refresh" in action_text or "reload" in action_text:
            return "reload"
        if any(k in action_text for k in ("navigate", "go to", "open")):
            return "navigate"
        if any(k in action_text for k in ("type", "enter", "input", "fill", "write")):
            return "fill"
        return "click"

    def _extract_step_value(step: Dict[str, Any]) -> str:
        direct = str(step.get("value") or "").strip()
        if direct:
            return direct
        action_text = str(step.get("action") or "")
        match = re.search(r"'([^']+)'", action_text)
        if match:
            return match.group(1).strip()
        # Support unquoted credentials in manual scenario text.
        email_match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", action_text)
        if email_match:
            return email_match.group(1).strip()
        password_match = re.search(r"password\s*(?:is|=|:)?\s*([^\s]+)", action_text, flags=re.IGNORECASE)
        if password_match:
            return password_match.group(1).strip()
        return ""

    def _extract_fill_target_tokens(step: Dict[str, Any], action_value: str) -> List[str]:
        action_text = " ".join(
            [
                str(step.get("action") or ""),
                str(step.get("name") or ""),
                str(step.get("selector") or ""),
                str(step.get("locator") or ""),
            ]
        ).lower()

        # Remove literal value from action text to avoid polluting target hints.
        if action_value:
            action_text = action_text.replace(str(action_value).lower(), " ")
        action_text = re.sub(r"'[^']*'", " ", action_text)

        segments: List[str] = [action_text]
        into_match = re.search(r"\b(?:into|in|to|on)\s+(.+)$", action_text)
        if into_match:
            segments.append(into_match.group(1))

        known_form_tokens = {
            "name",
            "email",
            "subject",
            "message",
            "phone",
            "mobile",
            "password",
            "username",
            "search",
            "address",
            "city",
            "state",
            "zip",
            "postal",
            "country",
            "code",
            "otp",
            "comment",
            "description",
            "details",
            "note",
            "first",
            "last",
        }

        out: List[str] = []
        for seg in segments:
            for token in _tokenize(seg):
                if token in known_form_tokens and token not in out:
                    out.append(token)
        return out[:8]

    feature_slug = _slug(job.feature_name)
    page_class = f"{_camel(job.feature_name)}Page"
    page_path = f"tests/pages/generated/{page_class}.ts"
    spec_path = f"tests/generated/{feature_slug}.spec.ts"
    scenarios = (planning.get("scenarios") or [])[: job.max_scenarios]

    selector_to_field: Dict[str, str] = {}
    field_to_selector: Dict[str, str] = {}
    action_methods: Dict[Tuple[int, int], Dict[str, Any]] = {}
    assertion_methods: Dict[Tuple[int, int], str] = {}
    used_names: set[str] = set()

    for s_idx, scenario in enumerate(scenarios):
        for st_idx, step in enumerate(scenario.get("steps") or []):
            action_kind = _infer_action_kind(step)
            action_value = _extract_step_value(step)
            fill_target_tokens = _extract_fill_target_tokens(step, action_value) if action_kind == "fill" else []
            raw_selector = _render_failed_selector(step)
            explicit_selector = str(step.get("selector") or step.get("locator") or "").strip()
            hints = [
                str(step.get("action") or ""),
                str(step.get("name") or ""),
                str(step.get("selector") or ""),
                str(step.get("locator") or ""),
            ]
            if action_kind == "fill":
                hints.extend(["input", "textbox", "textarea", "field", *fill_target_tokens])
            if action_kind == "click":
                hints.extend(["button", "link", "cta"])
            selector = explicit_selector or _pick_best_selector(
                crawl_summary,
                hints,
                raw_selector,
                action_kind=action_kind,
                action_text=str(step.get("action") or step.get("name") or ""),
                fill_target_tokens=fill_target_tokens,
            )
            if selector not in selector_to_field:
                field_name = _ident(step.get("action") or f"action {len(selector_to_field)+1}", "actionSelector")
                if not field_name.endswith("Selector"):
                    field_name = f"{field_name}Selector"
                base_name = field_name
                counter = 2
                while field_name in used_names:
                    field_name = f"{base_name}{counter}"
                    counter += 1
                used_names.add(field_name)
                selector_to_field[selector] = field_name
                field_to_selector[field_name] = selector
            action_methods[(s_idx, st_idx)] = {
                "field_name": selector_to_field[selector],
                "selector": selector,
                "intent_key": _render_intent_key(step),
                "use_of_selector": _render_step_name(step, "click on generated action"),
                "action_kind": action_kind,
                "action_value": action_value,
            }

        for a_idx, assertion in enumerate(scenario.get("assertions") or []):
            assertion_text = str(assertion if not isinstance(assertion, dict) else assertion.get("type") or "assertion")
            method_name = _method_name(assertion_text or f"assertion {a_idx + 1}", "verify")
            base_method = method_name
            counter = 2
            while method_name in used_names:
                method_name = f"{base_method}{counter}"
                counter += 1
            used_names.add(method_name)
            assertion_methods[(s_idx, a_idx)] = method_name

    locator_lines: List[str] = []
    for field_name, selector in field_to_selector.items():
        selector_escaped = selector.replace("'", "\\'")
        locator_lines.append(f"  {field_name} = '{selector_escaped}';")
    if not locator_lines:
        locator_lines = ["  primaryActionSelector = 'button:has-text(\"Continue\")';"]
        field_to_selector["primaryActionSelector"] = 'button:has-text("Continue")'

    action_method_lines = [
        "  async openHomePage() {",
        f"    await this.page.goto('{job.base_url.rstrip('/')}/');",
        "  }",
        "",
        "  actionLocator(selector: string): Locator {",
        "    return this.page.locator(selector).first();",
        "  }",
        "",
    ]

    assertion_method_lines = []
    for s_idx, scenario in enumerate(scenarios):
        for a_idx, assertion in enumerate(scenario.get("assertions") or []):
            method_name = assertion_methods.get((s_idx, a_idx))
            if not method_name:
                continue
            first_selector = next(iter(field_to_selector.values()))
            plan = _render_single_assertion_plan(assertion, crawl_summary, first_selector)
            strict_body: List[str] = []
            fallback_body: List[str] = []
            for line in plan.get("strict") or []:
                strict_body.append(line.replace("page.", "this.page.").replace("  await", "      await"))
            for line in plan.get("fallback") or []:
                fallback_body.append(line.replace("page.", "this.page.").replace("  await", "      await"))
            if not strict_body:
                strict_body = ["      await expect(this.page.locator('body')).toBeVisible();"]
            assertion_method_lines.extend(
                [
                    f"  async {method_name}() {{",
                    "    let strictPassed = true;",
                    "    try {",
                    *strict_body,
                    "    } catch (_assertErr) {",
                    "      strictPassed = false;",
                    "    }",
                    "    if (!strictPassed) {",
                    *(fallback_body or ["      throw new Error('Strict assertion failed and no fallback passed.');"]),
                    "    }",
                    "  }",
                    "",
                ]
            )

    if not assertion_method_lines:
        assertion_method_lines = [
            "  async verifyPageLoaded() {",
            "    await expect(this.page.locator('body')).toBeVisible();",
            "  }",
            "",
        ]

    page_content = f"""import {{ Page, Locator, expect }} from '@playwright/test';

export class {page_class} {{
  readonly page: Page;

  constructor(page: Page) {{
    this.page = page;
  }}

  // ===== Locators =====
{os.linesep.join(locator_lines)}

  // ===== Actions =====
{os.linesep.join(action_method_lines)}
  // ===== Assertions =====
{os.linesep.join(assertion_method_lines)}
}}
"""

    test_blocks: List[str] = []
    for s_idx, scenario in enumerate(scenarios):
        scenario_type = str(scenario.get("type") or "SMOKE").upper()
        title = str(scenario.get("title") or "Generated scenario")
        full_title = f"{scenario_type} - {title}"
        title_log = full_title.replace("\\", "\\\\").replace("'", "\\'")
        lines = [
            f"  test('{full_title}', async ({{ page }}, testInfo) => {{",
            "    console.log(chalk.hex('#00FFFF')('\\n========================================'));",
            f"    console.log(chalk.hex('#00FFFF')('TEST: {title_log}'));",
            "    console.log(chalk.hex('#00FFFF')('========================================'));",
            "",
            f"    const flow = new {page_class}(page);",
            "    await flow.openHomePage();",
            "",
        ]
        for st_idx, step in enumerate(scenario.get("steps") or [], start=1):
            meta = action_methods.get((s_idx, st_idx - 1))
            if not meta:
                continue
            failed_selector = meta["selector"].replace("'", "\\'")
            use_of_selector = meta["use_of_selector"].replace("'", "\\'")
            action_kind = str(meta.get("action_kind") or "click")
            action_value = str(meta.get("action_value") or "").replace("\\", "\\\\").replace("'", "\\'")
            step_log = _render_step_name(step, "perform action").replace("\\", "\\\\").replace("'", "\\'")
            lines.extend(
                [
                    f"    // Step {st_idx}: {_render_step_name(step, 'perform action')}",
                    f"    console.log('Step {st_idx}: {step_log}');",
                ]
            )
            step_action_lower = str(step.get("action") or step.get("name") or "").lower()
            is_optional_consent_step = (
                action_kind == "click"
                and any(k in step_action_lower for k in ("consent", "cookie", "policy", "agree"))
            )
            if is_optional_consent_step:
                lines.extend(
                    [
                        f"    const optionalConsent = page.locator(flow.{meta['field_name']}).first();",
                        "    const consentVisible = await optionalConsent.waitFor({ state: 'visible', timeout: 3000 }).then(() => true).catch(() => false);",
                        "    if (consentVisible) {",
                        "      await optionalConsent.click();",
                        "    }",
                        "",
                    ]
                )
                continue
            if action_kind == "reload":
                lines.extend(
                    [
                        "    await page.reload({ waitUntil: 'domcontentloaded' });",
                        "",
                    ]
                )
                continue
            if action_kind == "fill":
                if action_value:
                    lines.extend(
                        [
                            f"    await page.locator(flow.{meta['field_name']}).first().fill('{action_value}');",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            f"    throw new Error('Missing fill value for step: {step_log}');",
                            "",
                        ]
                    )
                continue
            if action_kind == "navigate":
                # Navigation is a URL operation, not a click. Reserve
                # selfHealingClick for CSS/XPath selectors. Extract the URL
                # from whichever field the planning put it in (value > selector
                # > action-text-inside-quotes). Fall back to `/` when the
                # planning is ambiguous.
                nav_target = (
                    action_value
                    or (step.get("selector") if _looks_like_url(str(step.get("selector") or "")) else "")
                    or (step.get("locator")  if _looks_like_url(str(step.get("locator") or ""))  else "")
                    or "/"
                )
                nav_target = str(nav_target).replace("'", "\\'")
                lines.extend(
                    [
                        f"    await page.goto('{nav_target}');",
                        "",
                    ]
                )
                continue
            lines.extend(
                [
                    "    await selfHealingClick(",
                    "      page,",
                    f"      flow.actionLocator(flow.{meta['field_name']}),",
                    f"      '{failed_selector}',",
                    "      testInfo,",
                    "      {",
                    f"        use_of_selector: '{use_of_selector}',",
                    "        selector_type: 'generated',",
                    f"        intent_key: '{meta['intent_key']}',",
                    "      }",
                    "    );",
                    "",
                ]
            )
        scenario_assertions = scenario.get("assertions") or []
        for a_idx, assertion in enumerate(scenario_assertions):
            method_name = assertion_methods.get((s_idx, a_idx))
            if method_name:
                lines.append(f"    await flow.{method_name}();")

        # 🔥 safety assertion for validator + runtime stability
        lines.append("    await expect(page.locator('body')).toBeVisible();")
        lines.extend(["  });", ""])
        test_blocks.append(os.linesep.join(lines))

    spec_content = f"""import {{ test, expect }} from '../../wraper-healer/baseTest';
import chalk from 'chalk';
import {{ selfHealingClick }} from '../../wraper-healer/selfHealing';
import {{ {page_class} }} from '../pages/generated/{page_class}';

test.describe('{job.feature_name} Feature', () => {{
{os.linesep.join(test_blocks)}
}});
"""

    return [
        {"artifact_type": GeneratedArtifact.TYPE_PAGE_OBJECT, "relative_path": page_path, "content": page_content},
        {"artifact_type": GeneratedArtifact.TYPE_SPEC, "relative_path": spec_path, "content": spec_content},
    ]


# def _build_codegen_prompt(job: GenerationJob, planning: Dict[str, Any], crawl_summary: Dict[str, Any]) -> str:
#     intent_catalog = _available_intent_keys()
#     return (
#         "You generate Playwright TypeScript test files.\n"
#         "Return STRICT JSON only.\n"
#         "Schema:\n"
#         "{\n"
#         '  "page_objects": [{"path":"tests/pages/generated/Name.ts","content":"..."}],\n'
#         '  "specs": [{"path":"tests/generated/name.spec.ts","content":"..."}],\n'
#         '  "notes": ["string"]\n'
#         "}\n"
#         f"Feature: {job.feature_name}\n"
#         f"Feature Description: {job.feature_description}\n"
#         f"Planning: {json.dumps(planning)}\n"
#         f"Crawl summary: {json.dumps(crawl_summary)}\n"
#         f"Allowed intent keys: {json.dumps(intent_catalog)}\n"
#         "Mandatory constraints:\n"
#         "- spec files must import: test, expect from '../baseTest'\n"
#         "- spec files must import selfHealingClick from '../utils/selfHealing'\n"
#         "- use intent_key in selfHealingClick options from allowed intent keys or generic\n"
#         "- avoid waitForTimeout/setTimeout/test.only\n"
#         "- output paths strictly under tests/generated or tests/pages/generated\n"
#         "- keep output application-agnostic; do not assume ecommerce-only entities unless crawl supports it\n"
#     )

def _build_codegen_prompt(
    job: GenerationJob,
    planning: Dict[str, Any],
    crawl_summary: Dict[str, Any],
    *,
    selector_map: Dict[str, str] | None = None,
    allowed_intent_keys: List[str] | None = None,
) -> str:
    """
    Build the first-pass codegen prompt.

    `selector_map` and `allowed_intent_keys` are pre-enriched by
    `_enrich_llm_context` at the caller — passed in explicitly here so the
    prompt sees the exact same context the retry prompt will see. If they are
    not provided we fall back to the defaults from the crawl / catalog.

    The rules block mirrors `_validate_artifact_content` one-for-one so the LLM
    is told about every hard constraint the validator will check.
    """
    if selector_map is None:
        selector_map = _build_selector_map(crawl_summary)
    if allowed_intent_keys is None:
        allowed_intent_keys = list(_available_intent_keys())

    return (
        "You generate Playwright TypeScript test files.\n"
        "Return STRICT JSON ONLY. No prose, no markdown fences.\n"
        "\n"
        "Schema (exact):\n"
        "{"
        '"page_objects":[{"path":"tests/pages/generated/X.ts","content":"..."}],'
        '"specs":[{"path":"tests/generated/X.spec.ts","content":"..."}],'
        '"notes":["string"]'
        "}\n"
        "\n"
        f"Feature: {job.feature_name}\n"
        f"Feature description: {job.feature_description}\n"
        f"Planning: {json.dumps(planning)}\n"
        f"Selector map (canonical → CSS selector): {json.dumps(selector_map)}\n"
        f"Allowed intent keys: {json.dumps(allowed_intent_keys)}\n"
        "\n"
        "REQUIRED SPEC STRUCTURE (each generated .spec.ts file must):\n"
        "- Start with: import { test, expect } from '../../wraper-healer/baseTest';\n"
        "- Import: import { selfHealingClick } from '../../wraper-healer/selfHealing';\n"
        "- Wrap tests in test.describe('<feature>', () => { ... }); or bare test(...) calls.\n"
        "- Each test signature: test('<title>', async ({ page }, testInfo) => { ... });\n"
        "- Call selfHealingClick(page, page.locator(SELECTOR), SELECTOR, testInfo,\n"
        "    { use_of_selector: '<action text>', selector_type: 'css', intent_key: '<intent>' })\n"
        "  for every click; include the intent_key from planning verbatim.\n"
        "- Include at least one `expect(...)` assertion per test.\n"
        "- Path must live under tests/generated/ and end in .spec.ts.\n"
        "\n"
        "REQUIRED PAGE OBJECT STRUCTURE (each generated .ts file must):\n"
        "- Define `export class <ClassName> { ... }`.\n"
        "- Provide a `constructor(page: Page) { this.page = page; }`.\n"
        "- Expose readonly selector fields plus async action methods.\n"
        "- If any method calls selfHealingClick(...) it MUST import it from '../utils/selfHealing'.\n"
        "- Path must live under tests/pages/generated/ and end in .ts.\n"
        "\n"
        "FORBIDDEN (validator will reject the artifact):\n"
        "- suite(...), suiteSetup(...)   — use test.describe / test.beforeAll instead.\n"
        "- test.page                      — always destructure `{ page }` from the test callback.\n"
        "- Constructing page objects with test.page (e.g. `const p = new Foo(test.page)`).\n"
        "- Default-importing test from ../../wraper-healer/baseTest — only named imports.\n"
        "- waitForTimeout(...), setTimeout(...), test.only(...), process.exit(...).\n"
        "- .nth(<n>) selectors — use text/role/testid based locators from the map.\n"
        "\n"
        "SELECTOR RULES:\n"
        "- If a step's `selector` value appears anywhere in the selector map, use that exact string.\n"
        "- Do NOT invent selectors. Do NOT rewrite selectors provided by the planning.\n"
        "- Intent keys from planning must be passed to selfHealingClick verbatim.\n"
        "- selfHealingClick(page, locator, SELECTOR, testInfo, options) — SELECTOR MUST be a CSS or XPath\n"
        "  selector (e.g. '#login', 'button[aria-label=\"Sign in\"]'). NEVER pass a URL path like '/' or\n"
        "  'https://…' as the selector argument. For navigation use `await page.goto(url)` instead.\n"
        "\n"
        "COMPLETENESS:\n"
        "- Emit code for every step and every assertion in the planning.\n"
        "- Do not skip scenarios. Do not abbreviate steps.\n"
    )


def _build_codegen_retry_prompt(
    job: GenerationJob,
    planning: Dict[str, Any],
    crawl_summary: Dict[str, Any],
    *,
    selector_map: Dict[str, str] | None = None,
    allowed_intent_keys: List[str] | None = None,
    invalid_artifacts_report: List[Dict[str, Any]] | None = None,
) -> str:
    """
    Retry prompt used in two situations:

    (a) First-pass output was empty (`_is_codegen_empty`) — call with
        `invalid_artifacts_report=None`. The prompt tells the LLM to try again
        because the previous output had no page_objects/specs.
    (b) First-pass output validated but some artifacts failed — call with the
        list of `{relative_path, artifact_type, validation_errors}` records.
        The prompt shows the LLM its own bad output plus the validator's
        error strings verbatim, so it can fix the exact issues.
    """
    if selector_map is None:
        selector_map = _build_selector_map(crawl_summary)
    if allowed_intent_keys is None:
        allowed_intent_keys = list(_available_intent_keys())

    parts: List[str] = [
        "Return STRICT JSON only. No prose, no markdown fences.\n",
        "Do not return empty arrays. At least one page object and one spec are mandatory.\n",
        "\n",
        "Schema (exact):\n",
        "{\n",
        '  "page_objects": [{"path":"tests/pages/generated/Name.ts","content":"typescript code"}],\n',
        '  "specs":        [{"path":"tests/generated/name.spec.ts","content":"typescript code"}],\n',
        '  "notes":        ["short note"]\n',
        "}\n",
        "\n",
        f"Feature name: {job.feature_name}\n",
        f"Feature description: {job.feature_description}\n",
        f"Planning: {json.dumps(planning)}\n",
        f"Selector map (use exactly these values, keyed by canonical name): {json.dumps(selector_map)}\n",
        f"Allowed intent keys: {json.dumps(allowed_intent_keys)}\n",
        "\n",
        "RULES (same as first attempt — the validator will reject anything violating them):\n",
        "- Spec: import { test, expect } from '../../wraper-healer/baseTest';\n",
        "- Spec: import { selfHealingClick } from '../../wraper-healer/selfHealing';\n",
        "- Every click uses selfHealingClick(page, locator, SELECTOR, testInfo, { intent_key, ... }).\n",
        "- Include at least one expect(...) assertion per test.\n",
        "- Page object: `export class ... { constructor(page: Page) { ... } }`.\n",
        "- Paths under tests/generated/ (.spec.ts) and tests/pages/generated/ (.ts).\n",
        "- Forbidden: suite(), suiteSetup(), test.page, test.only(), waitForTimeout(), setTimeout(),\n",
        "  process.exit(), .nth(n) selectors, default-importing test.\n",
        "- Never invent selectors. Every selector must be a value from the selector map above,\n",
        "  or a selector explicitly given in a planning step.\n",
        "- selfHealingClick's 3rd argument MUST be a CSS/XPath selector. NEVER pass a URL there —\n",
        "  use `await page.goto(url)` for navigation. Passing '/' or an http(s) URL will be rejected.\n",
    ]

    if invalid_artifacts_report:
        parts.append(
            "\n"
            "PREVIOUS ATTEMPT WAS REJECTED. Fix EVERY error listed below.\n"
            "You produced these artifacts and each has one or more validator errors —\n"
            "regenerate the full JSON so the errors are gone. Keep valid artifacts intact.\n"
            "\n"
        )
        for report in invalid_artifacts_report:
            parts.append(
                f"- {report.get('artifact_type', '?')} @ {report.get('relative_path', '?')}\n"
            )
            for err in report.get("validation_errors") or []:
                parts.append(f"    · {err}\n")
            preview = str(report.get("content_preview") or "").strip()
            if preview:
                parts.append(f"    previous content (first 400 chars):\n    {preview[:400]}\n")

    return "".join(parts)


def _is_codegen_empty(payload: Dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return True
    return not (payload.get("page_objects") or payload.get("specs"))


def _ensure_spec_required_imports(content: str) -> str:
    text = str(content or "")
    lines = text.splitlines()
    has_base_test_import = bool(re.search(r"from\s+['\"]\.\./\.\./wraper-healer/baseTest['\"]", text))
    has_self_healing_import = bool(
        re.search(r"from\s+['\"]\.\./\.\./wraper-healer/selfHealing['\"]", text)
    )

    # Normalize any existing baseTest import to the required named import form.
    if has_base_test_import:
        lines = [
            ln for ln in lines
            if "from '../../wraper-healer/baseTest'" not in ln and 'from "../../wraper-healer/baseTest"' not in ln
        ]
    prefix: List[str] = ["import { test, expect } from '../../wraper-healer/baseTest';"]
    if not has_self_healing_import:
        prefix.append("import { selfHealingClick } from '../../wraper-healer/selfHealing';")

    # Keep imports grouped at the top for deterministic output.
    while lines and not lines[0].strip():
        lines.pop(0)
    rebuilt = "\n".join(prefix + lines)
    return rebuilt.strip() + "\n"


def _extract_codegen_artifacts(
    job: GenerationJob,
    codegen_json: Dict[str, Any],
    planning: Dict[str, Any],
    crawl_summary: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes = [str(n) for n in (codegen_json.get("notes") or [])[:20]]
    artifacts: List[Dict[str, Any]] = []
    for po in codegen_json.get("page_objects") or []:
        artifacts.append(
            {
                "artifact_type": GeneratedArtifact.TYPE_PAGE_OBJECT,
                "relative_path": str(po.get("path") or ""),
                "content": str(po.get("content") or ""),
            }
        )
    for spec in codegen_json.get("specs") or []:
        spec_content = _ensure_spec_required_imports(str(spec.get("content") or ""))
        artifacts.append(
            {
                "artifact_type": GeneratedArtifact.TYPE_SPEC,
                "relative_path": str(spec.get("path") or ""),
                "content": spec_content,
            }
        )

    valid_artifacts = [a for a in artifacts if a["relative_path"] and a["content"]]

    if not valid_artifacts:
        # Tag every fallback artifact with `_from_template: True` so the caller's
        # Phase-5 retry loop can distinguish templates from LLM output and skip
        # the LLM re-ask (which would just hand the template back to the LLM as
        # if it were the LLM's own previous work — wasteful and confusing).
        valid_artifacts = _build_template_artifacts(job, planning, crawl_summary)
        for a in valid_artifacts:
            a["_from_template"] = True
        notes.append("Fallback code templates used because LLM codegen output was empty/invalid.")
    return valid_artifacts, notes


def _runtime_validate_selectors(
    validated_artifacts: List[Dict[str, Any]],
    *,
    base_url: str,
    crawl_summary: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not _runtime_selector_validation_enabled():
        return validated_artifacts, {
            "enabled": False,
            "checked_selectors": 0,
            "missing_selectors": 0,
            "warnings": [],
        }

    all_selectors: List[str] = []
    for artifact in validated_artifacts:
        if artifact.get("artifact_type") != GeneratedArtifact.TYPE_SPEC:
            continue
        for selector in _extract_selector_literals_from_text(str(artifact.get("content") or "")):
            if _is_universal_selector(selector):
                continue
            if selector and selector not in all_selectors:
                all_selectors.append(selector)

    if not all_selectors:
        return validated_artifacts, {
            "enabled": True,
            "checked_selectors": 0,
            "missing_selectors": 0,
            "warnings": [],
        }

    missing_map: Dict[str, str] = {}
    source = str(crawl_summary.get("source") or "").strip().lower()
    validation_source = _selector_validation_source()

    use_ui_knowledge_validation = validation_source == "ui_knowledge" or source == "ui_knowledge"
    if use_ui_knowledge_validation:
        missing_map = _validate_selectors_from_ui_knowledge(
            selectors=all_selectors,
            crawl_summary=crawl_summary,
        )
    else:
        repo_root = _repo_root()
        validator_script = repo_root / "tests" / "utils" / "validateSelectors.mjs"
        if not validator_script.exists():
            return validated_artifacts, {
                "enabled": True,
                "checked_selectors": 0,
                "missing_selectors": 0,
                "warnings": [f"Selector validator script missing at {validator_script}"],
            }

        route_urls = [str(r.get("url") or "").strip() for r in (crawl_summary.get("routes") or []) if r.get("url")]
        if not route_urls:
            route_urls = [base_url]
        route_urls = route_urls[:30]

        cmd = [
            "node",
            str(validator_script),
            "--base-url",
            base_url,
            "--urls",
            json.dumps(route_urls),
            "--selectors",
            json.dumps(all_selectors),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            warning = f"Runtime selector validation failed to run: {str(exc)}"
            return validated_artifacts, {
                "enabled": True,
                "checked_selectors": len(all_selectors),
                "missing_selectors": 0,
                "warnings": [warning],
            }

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            warning = f"Runtime selector validation rc={proc.returncode}: {(stderr or stdout)[:500]}"
            return validated_artifacts, {
                "enabled": True,
                "checked_selectors": len(all_selectors),
                "missing_selectors": 0,
                "warnings": [warning],
            }

        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            return validated_artifacts, {
                "enabled": True,
                "checked_selectors": len(all_selectors),
                "missing_selectors": 0,
                "warnings": [f"Runtime selector validation returned non-JSON: {stdout[:500]}"],
            }

        result_rows = parsed.get("results") or []
        for row in result_rows:
            selector = str(row.get("selector") or "").strip()
            if not selector:
                continue
            if not bool(row.get("matched")):
                error_text = str(row.get("error") or "selector not found on crawled routes").strip()
                missing_map[selector] = error_text

    if not missing_map:
        return validated_artifacts, {
            "enabled": True,
            "checked_selectors": len(all_selectors),
            "missing_selectors": 0,
            "warnings": [],
        }

    updated: List[Dict[str, Any]] = []
    for artifact in validated_artifacts:
        content = str(artifact.get("content") or "")
        selectors_here = _extract_selector_literals_from_text(content)
        missing_here = [s for s in selectors_here if s in missing_map]
        if not missing_here:
            updated.append(artifact)
            continue

        existing_errors = list(artifact.get("validation_errors") or [])
        existing_warnings = list(artifact.get("warnings") or [])
        failure_line = (
            "Runtime selector validation failed for selector(s): "
            + ", ".join(missing_here[:10])
        )
        existing_errors.append(failure_line)
        for selector in missing_here[:10]:
            existing_warnings.append(f"{selector}: {missing_map.get(selector, 'not found')}")

        artifact["validation_errors"] = existing_errors
        artifact["warnings"] = existing_warnings
        artifact["validation_status"] = GeneratedArtifact.INVALID
        updated.append(artifact)

    return updated, {
        "enabled": True,
        "checked_selectors": len(all_selectors),
        "missing_selectors": len(missing_map),
        "warnings": [],
    }


# Phase 6 introduces two more path prefixes for Cucumber output. The order in
# `_ALLOWED_PATH_PREFIXES` matters because both `_validate_relative_path` and
# `_scoped_relative_path` iterate the tuple; longer prefixes must come first so
# a path like `features/steps/foo-steps.ts` isn't matched by the shorter
# `features/` prefix and left un-scoped.
_ALLOWED_PATH_PREFIXES = (
    "tests/pages/generated/",   # PAGE_OBJECT (both legacy and Phase 6)
    "tests/generated/",         # SPEC (legacy Playwright .spec.ts)
    "features/steps/",          # STEP_DEFINITIONS (Phase 6 Cucumber)
    "features/",                # FEATURE (Phase 6 Cucumber .feature)
)


def _validate_relative_path(relative_path: str) -> List[str]:
    errors: List[str] = []
    rp = (relative_path or "").replace("\\", "/").strip()
    if not rp:
        return ["Missing relative_path"]
    if rp.startswith("/") or ".." in Path(rp).parts:
        errors.append("Path traversal or absolute path is not allowed")
    if not rp.startswith(_ALLOWED_PATH_PREFIXES):
        errors.append(
            "Path must be under tests/generated, tests/pages/generated, "
            "features/, or features/steps/"
        )
    # Extension check — Phase 6 adds .feature; legacy still allows .ts / .spec.ts.
    if not (rp.endswith(".ts") or rp.endswith(".spec.ts") or rp.endswith(".feature")):
        errors.append("Generated files must be .ts, .spec.ts, or .feature")
    return errors


def _scoped_relative_path(relative_path: str, client_slug: Optional[str]) -> str:
    """
    Inject the client slug into the materialization path so each tenant gets
    its own directory. Handles both the legacy layout (tests/generated/,
    tests/pages/generated/) and the Phase 6 Cucumber layout (features/,
    features/steps/).

        tests/generated/login.spec.ts        -> tests/generated/<slug>/login.spec.ts
        tests/pages/generated/LoginPage.ts   -> tests/pages/generated/<slug>/LoginPage.ts
        features/login.feature               -> features/<slug>/login.feature
        features/steps/login-steps.ts        -> features/steps/<slug>/login-steps.ts

    No-op if client_slug is falsy (single-tenant fallback).
    """
    rp = (relative_path or "").replace("\\", "/").strip()
    if not client_slug or not rp:
        return rp
    slug = str(client_slug).strip("/")
    for prefix in _ALLOWED_PATH_PREFIXES:
        if rp.startswith(prefix):
            tail = rp[len(prefix):]
            if tail.startswith(f"{slug}/"):
                return rp
            return f"{prefix}{slug}/{tail}"
    return rp


def _inject_slug_into_pageobject_imports(content: str, slug: str) -> str:
    """
    Rewrite page-object import paths so they resolve against the slug-scoped
    on-disk layout, regardless of what shape the LLM emitted them in.

    Canonical target for a step-defs file that lives at
    `features/steps/<slug>/foo-steps.ts` is:

        ../../../tests/pages/generated/<slug>/<Name>

    (three `../` hops: out of `<slug>/`, out of `steps/`, out of `features/`.)

    IDEMPOTENT: unlike the earlier version, this function fully rebuilds each
    matching import from the raw class name — so running it N times converges
    to the same result. The earlier version amplified `../` hops and prepended
    `<slug>/` on every re-run, producing paths like
    `../../../../../../tests/pages/generated/<slug>/<slug>/<slug>/<Name>`.
    """
    if not slug:
        return content

    def _fix(m: re.Match) -> str:
        quote = m.group("quote")
        path = m.group("path")
        # Split on `/`, drop any `..`, `.`, empty, or slug segments; take the
        # last remaining component as the class-name-that-is-the-file-stem.
        segments = [s for s in path.split("/") if s and s != slug]
        # Everything after `generated` is directory noise (`<slug>/<slug>/…`)
        # plus the final class-name segment. Keep only the last segment as the
        # target file.
        try:
            gen_idx = segments.index("generated")
        except ValueError:
            return m.group(0)
        after = segments[gen_idx + 1:]
        if not after:
            return m.group(0)
        class_name = after[-1]  # last non-slug segment = file stem
        return f"{quote}../../../tests/pages/generated/{slug}/{class_name}"

    # Match any import path that ends at `tests/pages/generated/…` regardless
    # of hop count or interleaved slug repetitions.
    return re.sub(
        r"(?P<quote>['\"])(?P<path>(?:\.\./)+tests/pages/generated/[^'\"]+)",
        _fix,
        content,
    )


def _validate_artifact_content(artifact_type: str, content: str,
                               path: str = "",
                               ctx: Optional[Dict[str, Any]] = None) -> Tuple[List[str], List[str]]:
    """
    Legacy adapter — delegates to the unified `ArtifactValidator` and returns
    the old `(errors, warnings)` shape so existing callers don't need to
    change. New code should call `validate_artifact(...)` directly to get the
    richer `ValidationResult` with per-rule metadata.
    """
    from test_generation.artifact_validation import validate_artifact

    result = validate_artifact(artifact_type, path, content or "", ctx=ctx or {})
    return result.error_messages(), result.warning_messages()


def _typescript_parse_check(relative_path: str, content: str) -> List[str]:
    # Skip Gherkin — TypeScript compiler would reject `.feature` syntax on sight.
    if (relative_path or "").lower().endswith(".feature"):
        return []
    repo_root = _repo_root()
    script = (
        "const fs=require('fs');"
        "let ts;"
        "try{ts=require('typescript');}catch(e){console.log('__TS_MISSING__');process.exit(0)}"
        "const file=process.argv[1];"
        "const src=fs.readFileSync(file,'utf8');"
        "const out=ts.transpileModule(src,{compilerOptions:{target:'ES2020',module:'CommonJS'}});"
        "const diags=out.diagnostics||[];"
        "if(diags.length){"
        "console.log(JSON.stringify(diags.slice(0,5).map(d=>ts.flattenDiagnosticMessageText(d.messageText,' '))))"
        "}"
    )
    tmp_dir = repo_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / f"gen_validate_{_slug(relative_path)}"
    tmp_file = tmp_file.with_suffix(".ts")
    tmp_file.write_text(content, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", "-e", script, str(tmp_file)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except Exception as exc:
        return [f"TypeScript parse check failed to run: {str(exc)}"]
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

    stdout = (proc.stdout or "").strip()
    if stdout == "__TS_MISSING__":
        return []
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        return [stdout[:500]]
    return []


def _validate_artifacts(artifacts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    invalid_count = 0
    warnings_count = 0

    for artifact in artifacts:
        artifact_type = artifact.get("artifact_type") or GeneratedArtifact.TYPE_SPEC
        relative_path = str(artifact.get("relative_path") or "")
        content = str(artifact.get("content") or "")

        errors = _validate_relative_path(relative_path)
        content_errors, content_warnings = _validate_artifact_content(artifact_type, content)
        errors.extend(content_errors)
        warnings = content_warnings
        ts_errors = _typescript_parse_check(relative_path, content)
        errors.extend(ts_errors)

        is_valid = len(errors) == 0
        if not is_valid:
            invalid_count += 1
        warnings_count += len(warnings)

        validated.append(
            {
                "artifact_type": artifact_type,
                "relative_path": relative_path,
                "content": content,
                "checksum": _sha256(content),
                "validation_status": GeneratedArtifact.VALID if is_valid else GeneratedArtifact.INVALID,
                "validation_errors": errors,
                "warnings": warnings,
            }
        )

    summary = {
        "total_artifacts": len(validated),
        "invalid_artifacts": invalid_count,
        "warnings": warnings_count,
        "valid_artifacts": len(validated) - invalid_count,
    }
    return validated, summary


def _planning_from_existing_job_scenarios(job: GenerationJob) -> Dict[str, Any] | None:
    rows: List[Dict[str, Any]] = []
    for s in job.scenarios.order_by("priority"):
        rows.append(
            {
                "id": str(s.scenario_id or ""),
                "title": str(s.title or ""),
                "type": str(s.scenario_type or "SMOKE"),
                "preconditions": _safe_json(s.preconditions or [], []),
                "steps": _safe_json(s.steps or [], []),
                "assertions": _safe_json(s.expected_assertions or [], []),
            }
        )
    if not rows:
        return None
    return {
        "feature_summary": str(job.feature_summary or f"Existing scenarios for {job.feature_name}"),
        "scenarios": rows,
        "notes": ["Planning sourced from existing saved scenarios."],
    }


def _normalize_step_item(step: Any) -> Dict[str, Any] | None:
    if isinstance(step, dict):
        action = str(step.get("action") or step.get("name") or "").strip()
        if not action:
            return None
        row: Dict[str, Any] = {
            "action": action,
            "selector": str(step.get("selector") or step.get("locator") or "").strip(),
            "intent_key": _render_intent_key(step),
        }
        value = step.get("value")
        if value is not None:
            row["value"] = str(value)
        return row

    action = str(step or "").strip()
    if not action:
        return None
    return {
        "action": action,
        "selector": "",
        "intent_key": "generic",
    }


def _sanitize_scenarios(
    raw_scenarios: List[Dict[str, Any]],
    max_scenarios: int,
    *,
    enforce_balance: bool = True,
) -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    seen_titles = set()
    for idx, item in enumerate(raw_scenarios[:max_scenarios], start=1):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or f"Generated Scenario {idx}").strip()
        if title.lower() in seen_titles:
            title = f"{title} #{idx}"
        seen_titles.add(title.lower())
        raw_steps = item.get("steps") or []
        normalized_steps: List[Dict[str, Any]] = []
        for step in raw_steps[:20]:
            row = _normalize_step_item(step)
            if row:
                normalized_steps.append(row)
        if not normalized_steps:
            normalized_steps = [{"action": "open feature page", "selector": "/", "intent_key": "generic"}]

        preconditions = [str(p).strip() for p in (item.get("preconditions") or []) if str(p).strip()][:10]
        assertions = [str(a).strip() for a in (item.get("assertions") or []) if str(a).strip()][:10]
        if not assertions:
            assertions = ["Page renders without runtime errors"]

        scenarios.append(
            {
                "id": (item.get("id") or f"scenario_{idx}").strip(),
                "title": title,
                "type": _normalize_scenario_type(item.get("type") or "SMOKE"),
                "preconditions": preconditions,
                "steps": normalized_steps,
                "assertions": assertions,
            }
        )
    if enforce_balance:
        # Ensure at least one smoke and one negative.
        types = {s["type"] for s in scenarios}
        if GenerationScenario.TYPE_SMOKE not in types:
            scenarios.insert(
                0,
                {
                    "id": "smoke_auto",
                    "title": "Auto-added smoke scenario",
                    "type": GenerationScenario.TYPE_SMOKE,
                    "preconditions": [],
                    "steps": [{"action": "open feature page", "selector": "/", "intent_key": "generic"}],
                    "assertions": ["Page renders without errors"],
                },
            )
        if GenerationScenario.TYPE_NEGATIVE not in types:
            scenarios.append(
                {
                    "id": "negative_auto",
                    "title": "Auto-added negative scenario",
                    "type": GenerationScenario.TYPE_NEGATIVE,
                    "preconditions": [],
                    "steps": [{"action": "trigger invalid action", "selector": "text=Submit", "intent_key": "generic"}],
                    "assertions": ["Validation message appears"],
                }
            )
    return scenarios[:max_scenarios]


def _scenario_quality_score(scenarios: List[Dict[str, Any]]) -> int:
    score = 0
    if not scenarios:
        return score

    types = {str(s.get("type") or "").upper() for s in scenarios}
    if GenerationScenario.TYPE_SMOKE in types:
        score += 3
    if GenerationScenario.TYPE_NEGATIVE in types:
        score += 3

    for scenario in scenarios:
        steps = scenario.get("steps") or []
        assertions = scenario.get("assertions") or []
        score += min(len(steps), 8)
        score += min(len(assertions), 8)
        for step in steps:
            selector = str(step.get("selector") or "").strip()
            if selector:
                score += 1
            intent_key = str(step.get("intent_key") or "").strip().lower()
            if intent_key and intent_key != "generic":
                score += 1
    return score


def _merge_verified_scenarios(
    base_scenarios: List[Dict[str, Any]],
    candidate_scenarios: List[Dict[str, Any]],
    *,
    max_scenarios: int,
    enforce_balance: bool,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    candidate_by_title = {
        str(c.get("title") or "").strip().lower(): c
        for c in candidate_scenarios
        if isinstance(c, dict) and str(c.get("title") or "").strip()
    }

    used_titles = set()
    for base in base_scenarios:
        title_key = str(base.get("title") or "").strip().lower()
        candidate = candidate_by_title.get(title_key)
        merged = dict(base)
        if candidate:
            used_titles.add(title_key)
            base_steps = [dict(s) for s in (base.get("steps") or [])]
            cand_steps = [dict(s) for s in (candidate.get("steps") or [])]
            merged_steps: List[Dict[str, Any]] = []
            for idx, step in enumerate(base_steps):
                row = dict(step)
                if idx < len(cand_steps):
                    cstep = cand_steps[idx]
                    if not str(row.get("selector") or "").strip() and str(cstep.get("selector") or "").strip():
                        row["selector"] = str(cstep.get("selector") or "").strip()
                    if str(row.get("intent_key") or "generic").strip().lower() == "generic":
                        c_intent = str(cstep.get("intent_key") or "").strip().lower()
                        if c_intent and c_intent != "generic":
                            row["intent_key"] = c_intent
                    if row.get("value") is None and cstep.get("value") is not None:
                        row["value"] = str(cstep.get("value"))
                merged_steps.append(row)
            if len(cand_steps) > len(merged_steps):
                merged_steps.extend(cand_steps[len(merged_steps) :])
            merged["steps"] = merged_steps[:20]

            merged_assertions: List[str] = []
            for item in (base.get("assertions") or []) + (candidate.get("assertions") or []):
                text = str(item).strip()
                if text and text not in merged_assertions:
                    merged_assertions.append(text)
            merged["assertions"] = merged_assertions[:10]

            merged_preconditions: List[str] = []
            for item in (base.get("preconditions") or []) + (candidate.get("preconditions") or []):
                text = str(item).strip()
                if text and text not in merged_preconditions:
                    merged_preconditions.append(text)
            merged["preconditions"] = merged_preconditions[:10]
        out.append(merged)

    for candidate in candidate_scenarios:
        title_key = str(candidate.get("title") or "").strip().lower()
        if not title_key or title_key in used_titles:
            continue
        out.append(candidate)
        used_titles.add(title_key)
        if len(out) >= max_scenarios:
            break

    return _sanitize_scenarios(out, max_scenarios, enforce_balance=enforce_balance)


def _verify_and_patch_planning_with_llm(
    *,
    job: GenerationJob,
    crawl_summary: Dict[str, Any],
    planning: Dict[str, Any],
    enforce_balance: bool,
) -> Tuple[Dict[str, Any], List[str]]:
    notes: List[str] = []
    effective_max_scenarios = int(job.max_scenarios or _max_scenarios_default())
    if not _planning_verify_enabled():
        return planning, notes
    if not _test_gen_enabled():
        return planning, notes

    base_scenarios = _sanitize_scenarios(
        planning.get("scenarios") or [],
        effective_max_scenarios,
        enforce_balance=enforce_balance,
    )
    if not base_scenarios:
        return planning, notes

    try:
        verify_prompt = (
            "You are validating and improving an existing QA test-scenario plan.\n"
            "Improve only if meaningful. Keep same feature scope. Do not invent unrelated flows.\n"
            "Return strict JSON object with keys: feature_summary (string), scenarios (array), notes (array).\n"
            f"Constraints: max {effective_max_scenarios} scenarios; "
            "each scenario needs id,title,type,preconditions,steps,assertions; "
            "steps use action,selector,intent_key,value(optional).\n"
            "Prefer preserving original steps and patching missing selectors/assertions.\n\n"
            f"Feature: {job.feature_name}\n"
            f"Description: {job.feature_description}\n"
            "Crawl summary (short):\n"
            f"{json.dumps({'routes': (crawl_summary.get('routes') or [])[:5]}, ensure_ascii=False)}\n\n"
            "Current planning JSON:\n"
            f"{json.dumps({'feature_summary': planning.get('feature_summary'), 'scenarios': base_scenarios}, ensure_ascii=False)}\n"
        )
        raw = _call_ollama_json(
            prompt=verify_prompt,
            model=job.llm_model or _default_test_gen_model(),
            temperature=float(job.llm_temperature or 0.0),
            timeout_seconds=_llm_timeout(),
            num_predict=_planning_verify_num_predict(),
        )
        
        normalized = _normalize_planning_payload(raw)
        candidate_raw = normalized.get("scenarios") or []
        candidate_scenarios = _sanitize_scenarios(
            candidate_raw,
            effective_max_scenarios,
            enforce_balance=enforce_balance,
        )
        if not candidate_scenarios:
            notes.append("Planning verify skipped: LLM returned no scenarios.")
            return planning, notes

        merged_scenarios = _merge_verified_scenarios(
            base_scenarios,
            candidate_scenarios,
            max_scenarios=effective_max_scenarios,
            enforce_balance=enforce_balance,
        )
        base_score = _scenario_quality_score(base_scenarios)
        merged_score = _scenario_quality_score(merged_scenarios)
        changed = json.dumps(base_scenarios, sort_keys=True) != json.dumps(merged_scenarios, sort_keys=True)
        if changed and merged_score >= base_score:
            patched = dict(planning)
            patched["scenarios"] = merged_scenarios
            summary = str(normalized.get("feature_summary") or "").strip()
            if summary:
                patched["feature_summary"] = summary
            notes.append(f"Planning verified with LLM: patch applied (score {base_score} -> {merged_score}).")
            return patched, notes
        notes.append(f"Planning verified with LLM: no beneficial patch (score {base_score} -> {merged_score}).")
        return planning, notes
    except (URLError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        notes.append(f"Planning verify LLM skipped: {str(exc)}")
        return planning, notes


def _plan_scenarios(
    job: GenerationJob,
    crawl_summary: Dict[str, Any],
    *,
    manual_scenarios: List[Dict[str, Any]] | None = None,
    use_existing_scenarios: bool = False,
    llm_first: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    planning: Dict[str, Any] | None = _manual_scenarios_to_planning_payload(job, manual_scenarios or [])
    notes: List[str] = []
    planning_source = "manual" if planning else ""
    effective_max_scenarios = int(job.max_scenarios or _max_scenarios_default())
    manual_input = bool(manual_scenarios)
    enforce_balance = not (manual_input and _respect_manual_scenarios_exactly())

    def _try_llm_planning() -> Dict[str, Any] | None:
        if not _test_gen_enabled():
            return None
        try:
            planning_prompt = _build_planning_prompt(job, crawl_summary)
            raw = _call_ollama_json(
                prompt=planning_prompt,
                model=job.llm_model or _default_test_gen_model(),
                temperature=float(job.llm_temperature or 0.0),
                timeout_seconds=_llm_timeout(),
                num_predict=900,
            )
            normalized = _normalize_planning_payload(raw)
            if not isinstance(normalized.get("scenarios"), list) or not normalized.get("scenarios"):
                return None
            return normalized
        except (URLError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
            notes.append(f"Planning LLM fallback: {str(exc)}")
            return None

    if not planning and llm_first:
        planning = _try_llm_planning()
        if planning:
            planning_source = "llm"

    if not planning and use_existing_scenarios:
        planning = _planning_from_existing_job_scenarios(job)
        if planning:
            planning_source = "existing"

    if not planning and not llm_first:
        planning = _try_llm_planning()
        if planning:
            planning_source = "llm"

    if not planning:
        planning = _fallback_scenarios(job, crawl_summary)
        planning_source = "fallback"

    if planning_source in {"manual", "existing", "fallback"}:
        planning, verify_notes = _verify_and_patch_planning_with_llm(
            job=job,
            crawl_summary=crawl_summary,
            planning=planning,
            enforce_balance=enforce_balance,
        )
        notes.extend(verify_notes)

    scenarios = _sanitize_scenarios(
        planning.get("scenarios") or [],
        effective_max_scenarios,
        enforce_balance=enforce_balance,
    )
    planning["scenarios"] = scenarios
    if not isinstance(planning.get("notes"), list):
        planning["notes"] = []
    if not isinstance(planning.get("feature_summary"), str) or not str(planning.get("feature_summary")).strip():
        planning["feature_summary"] = f"Auto-generated scenarios for {job.feature_name}"
    return planning, scenarios, notes


def _invalid_artifact_summary(validated_artifacts: List[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for artifact in validated_artifacts:
        if artifact.get("validation_status") == GeneratedArtifact.VALID:
            continue
        path = str(artifact.get("relative_path") or "(unknown)")
        errors = artifact.get("validation_errors") or []
        first_error = str(errors[0]) if errors else "unknown validation error"
        rows.append(f"{path}: {first_error}")
    return "; ".join(rows[:3])


def _manual_scenarios_to_planning_payload(
    job: GenerationJob,
    manual_scenarios: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    if not isinstance(manual_scenarios, list) or not manual_scenarios:
        return None

    rows: List[Dict[str, Any]] = []
    for idx, raw in enumerate(manual_scenarios, start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or f"Manual Scenario {idx}").strip() or f"Manual Scenario {idx}"
        raw_steps = raw.get("steps") or []
        steps: List[Dict[str, Any]] = []
        for s_idx, step in enumerate(raw_steps, start=1):
            if isinstance(step, dict):
                action_text = str(step.get("action") or step.get("name") or f"step {s_idx}")
                step_row = {
                    "action": action_text,
                    "selector": str(step.get("selector") or step.get("locator") or ""),
                    "intent_key": str(step.get("intent_key") or "generic"),
                }
                if step.get("value") is not None:
                    step_row["value"] = str(step.get("value"))
                steps.append(step_row)
                continue
            action_text = str(step or "").strip()
            if not action_text:
                continue
            step_row = {
                "action": action_text,
                "selector": "",
                "intent_key": "generic",
            }
            m = re.search(r"'([^']+)'", action_text)
            if m:
                step_row["value"] = m.group(1)
            steps.append(step_row)
        assertions = [str(a).strip() for a in (raw.get("assertions") or []) if str(a).strip()]
        rows.append(
            {
                "id": str(raw.get("id") or f"manual_{idx}"),
                "title": title,
                "type": str(raw.get("type") or ("NEGATIVE" if ("invalid" in title.lower() or "negative" in title.lower()) else "SMOKE")),
                "preconditions": [str(p).strip() for p in (raw.get("preconditions") or []) if str(p).strip()],
                "steps": steps,
                "assertions": assertions,
            }
        )

    if not rows:
        return None

    return {
        "feature_summary": f"Manual scenarios from request file for {job.feature_name}",
        "scenarios": rows,
        "notes": ["Planning sourced from manual_scenarios payload."],
    }


def _ui_change_preflight(job: GenerationJob) -> List[str]:
    """
    Phase 1.5 — Before generation, walk each seed_url and ask the ui_knowledge
    service whether the baseline still matches the current snapshot for the job's
    tenant. Returns a list of warning strings; never raises and never blocks the
    pipeline (logged on the job as notes for the operator to act on).
    """
    notes: List[str] = []
    try:
        # Imported lazily to keep this module importable when ui_knowledge
        # service deps are not on the path (e.g. management commands).
        from ui_knowledge.change_detection_service import detect_ui_change_for_healing
    except Exception as exc:  # pragma: no cover - defensive
        return [f"UI-change pre-flight skipped (ui_knowledge import failed): {exc}"]

    client = getattr(job, "client", None)
    severe_levels = {"MAJOR_CHANGE", "ELEMENT_REMOVED"}

    for seed in (job.seed_urls or [])[:10]:  # cap to keep startup latency bounded
        try:
            detection = detect_ui_change_for_healing(
                page_url=str(seed or ""),
                client=client,
            )
        except Exception as exc:
            notes.append(f"UI-change pre-flight error on {seed!r}: {exc}")
            continue

        level = str(detection.get("ui_change_level") or "UNKNOWN").upper()
        reason = detection.get("reason") or detection.get("change_type") or ""
        if level in severe_levels:
            notes.append(
                f"⚠ UI baseline diverged on {seed!r}: ui_change_level={level} "
                f"({reason}). Run `npm run sync:ui` before relying on generated tests."
            )
        elif level == "UNKNOWN" and reason == "snapshot_missing":
            notes.append(
                f"ℹ No baseline snapshot for {seed!r}. Run `npm run sync:ui` to seed one."
            )
    return notes


def generate_job_draft(job: GenerationJob, *, manual_scenarios: List[Dict[str, Any]] | None = None) -> GenerationJob:
    job.job_status = GenerationJob.STATE_DRAFTING
    job.error_message = ""
    job.drafting_started_on = timezone.now()
    job.max_scenarios = job.max_scenarios or _max_scenarios_default()
    job.max_routes = job.max_routes or _max_routes_default()
    if not job.llm_model:
        job.llm_model = _default_test_gen_model()
    job.save(
        update_fields=[
            "job_status",
            "error_message",
            "drafting_started_on",
            "max_scenarios",
            "max_routes",
            "llm_model",
            "last_modified",
        ]
    )

    # Phase 1.5: surface UI-change warnings BEFORE we spend LLM time.
    preflight_notes = _ui_change_preflight(job)

    try:
        crawl_summary = _run_crawl_context(
            base_url=job.base_url,
            seed_urls=job.seed_urls or [],
            max_routes=job.max_routes,
        )
        feature_presence = _feature_presence_report(job, crawl_summary)
        crawl_summary["feature_presence"] = feature_presence
        if not feature_presence.get("feature_likely_present"):
            warnings = crawl_summary.get("warnings") or []
            warnings.append(
                "Requested feature appears weakly represented in current UI crawl. "
                "Implement feature first or provide correct seed URLs before generation."
            )
            crawl_summary["warnings"] = warnings
            if _feature_presence_required():
                job.crawl_summary = crawl_summary
                job.feature_summary = (
                    "Draft blocked: requested feature not detected in current application crawl."
                )
                job.llm_notes = [
                    "Strict feature presence gate enabled.",
                    "Generation aborted to avoid irrelevant artifacts.",
                ]
                job.validation_summary = {
                    "total_artifacts": 0,
                    "valid_artifacts": 0,
                    "invalid_artifacts": 0,
                    "warnings": len(warnings),
                    "blocked_by_feature_presence": True,
                }
                job.job_status = GenerationJob.STATE_FAILED
                job.error_message = (
                    "Feature presence gate failed. Add/enable requested feature in app "
                    "or provide accurate seed_urls, then retry generation."
                )
                job.drafting_finished_on = timezone.now()
                job.save(
                    update_fields=[
                        "crawl_summary",
                        "feature_summary",
                        "llm_notes",
                        "validation_summary",
                        "job_status",
                        "error_message",
                        "drafting_finished_on",
                        "last_modified",
                    ]
                )
                return job
        
        planning, scenarios, planning_notes = _plan_scenarios(
            job,
            crawl_summary,
            manual_scenarios=manual_scenarios,
            use_existing_scenarios=False,
            llm_first=False,
        )
        
        llm_notes: List[str] = [str(n) for n in planning.get("notes") or []]
        llm_notes.extend(planning_notes)
        # Surface Phase 1.5 UI-change pre-flight warnings on the job for the operator.
        if preflight_notes:
            llm_notes.extend(preflight_notes)
        if _codegen_enabled() and _test_gen_enabled():
            try:
                # Enrich context BEFORE building the prompt so manual selectors
                # (from feature_requests.json) and custom intent_keys reach the
                # LLM. Without this the model is forced to either invent
                # selectors or skip steps — both are validator failures.
                planning_for_codegen = {"scenarios": scenarios}
                enrichment_notes: List[str] = []
                selector_map = _build_selector_map(crawl_summary)
                allowed_intent_keys = list(_available_intent_keys())
                selector_map, allowed_intent_keys = _enrich_llm_context(
                    planning_for_codegen, selector_map, allowed_intent_keys, enrichment_notes,
                )
                llm_notes.extend(enrichment_notes)

                prompt = _build_codegen_prompt(
                    job, planning_for_codegen, crawl_summary,
                    selector_map=selector_map, allowed_intent_keys=allowed_intent_keys,
                )
                raw_codegen = _call_ollama_json(
                    prompt=prompt,
                    model=job.llm_model or _default_test_gen_model(),
                    temperature=float(job.llm_temperature or 0.0),
                    timeout_seconds=_llm_timeout(),
                    num_predict=2600,
                )

                normalized_codegen = _normalize_codegen_payload(raw_codegen)
                # (a) Empty-response retry — unchanged pre-existing behavior.
                if _is_codegen_empty(normalized_codegen):
                    retry_prompt = _build_codegen_retry_prompt(
                        job, planning_for_codegen, crawl_summary,
                        selector_map=selector_map,
                        allowed_intent_keys=allowed_intent_keys,
                    )
                    retry_codegen = _call_ollama_json(
                        prompt=retry_prompt,
                        model=job.llm_model or _default_test_gen_model(),
                        temperature=float(job.llm_temperature or 0.0),
                        timeout_seconds=_llm_timeout(),
                        num_predict=2600,
                    )
                    normalized_codegen = _normalize_codegen_payload(retry_codegen)

                artifacts, codegen_notes = _extract_codegen_artifacts(
                    job,
                    normalized_codegen,
                    planning_for_codegen,
                    crawl_summary,
                )
                llm_notes.append("LLM codegen enabled for artifact generation.")
                llm_notes.extend(codegen_notes)

                # (b) Validation retry — Phase 4. Run validation up-front; if
                # any artifact fails, feed the exact validator errors back and
                # ask the LLM to fix them. One retry maximum.
                # Skip when the current artifacts are template fallbacks (the
                # LLM already produced nothing usable — asking it to "fix" the
                # template is a waste of a call and confuses the retry prompt).
                from_template = any(a.get("_from_template") for a in artifacts)
                if from_template:
                    llm_notes.append(
                        "Codegen fell back to templates; skipping LLM validation retry. "
                        "Refine feature description / seed URLs and re-Generate for a real LLM attempt."
                    )
                if _codegen_retry_enabled() and not from_template:
                    initial_validated, initial_summary = _validate_artifacts(artifacts)
                    if initial_summary.get("invalid_artifacts", 0) > 0:
                        invalid_report = [
                            {
                                "artifact_type": a.get("artifact_type"),
                                "relative_path": a.get("relative_path"),
                                "validation_errors": a.get("validation_errors") or [],
                                "content_preview": (a.get("content") or "")[:400],
                            }
                            for a in initial_validated
                            if a.get("validation_status") != GeneratedArtifact.VALID
                        ]
                        retry_prompt = _build_codegen_retry_prompt(
                            job, planning_for_codegen, crawl_summary,
                            selector_map=selector_map,
                            allowed_intent_keys=allowed_intent_keys,
                            invalid_artifacts_report=invalid_report,
                        )
                        try:
                            retry_codegen = _call_ollama_json(
                                prompt=retry_prompt,
                                model=job.llm_model or _default_test_gen_model(),
                                temperature=float(job.llm_temperature or 0.0),
                                timeout_seconds=_llm_timeout(),
                                num_predict=2600,
                            )
                            retry_normalized = _normalize_codegen_payload(retry_codegen)
                            retry_artifacts, retry_notes = _extract_codegen_artifacts(
                                job, retry_normalized, planning_for_codegen, crawl_summary,
                            )
                            _, retry_summary = _validate_artifacts(retry_artifacts)
                            # Only accept the retry if it strictly improves validity.
                            if retry_summary.get("invalid_artifacts", 999) < initial_summary.get("invalid_artifacts", 0):
                                artifacts = retry_artifacts
                                llm_notes.extend(retry_notes)
                                llm_notes.append(
                                    f"Codegen retry improved validation: "
                                    f"{initial_summary.get('invalid_artifacts')} → "
                                    f"{retry_summary.get('invalid_artifacts')} invalid."
                                )
                            else:
                                llm_notes.append(
                                    "Codegen retry did not reduce validation errors; keeping first attempt."
                                )
                        except Exception as retry_exc:
                            llm_notes.append(f"Codegen retry raised: {retry_exc}. Keeping first attempt.")
            except Exception as exc:
                llm_notes.append(f"LLM codegen failed; using templates. Reason: {str(exc)}")
                artifacts = _build_template_artifacts(
                    job,
                    {"scenarios": scenarios},
                    crawl_summary,
                )
        else:
            llm_notes.append("Template-only artifact generation enabled; LLM codegen is disabled.")
            artifacts = _build_template_artifacts(
                job,
                {"scenarios": scenarios},
                crawl_summary,
            )
        notes: List[str] = []
        llm_notes.extend(notes)
        validated_artifacts, validation_summary = _validate_artifacts(artifacts)
        validated_artifacts, runtime_selector_summary = _runtime_validate_selectors(
            validated_artifacts,
            base_url=job.base_url,
            crawl_summary=crawl_summary,
        )
        invalid_artifacts = sum(
            1 for artifact in validated_artifacts if artifact.get("validation_status") != GeneratedArtifact.VALID
        )
        validation_summary = {
            **validation_summary,
            "invalid_artifacts": invalid_artifacts,
            "valid_artifacts": len(validated_artifacts) - invalid_artifacts,
            "runtime_selector_validation": runtime_selector_summary,
            "runtime_selector_checked_count": runtime_selector_summary.get("checked_selectors", 0),
            "runtime_selector_missing_count": runtime_selector_summary.get("missing_selectors", 0),
            "strict_artifact_gate": _require_all_artifacts_valid(),
        }

        job.scenarios.all().delete()
        job.artifacts.all().delete()

        scenario_rows = []
        for index, sc in enumerate(scenarios, start=1):
            scenario_rows.append(
                GenerationScenario(
                    job=job,
                    scenario_id=sc["id"],
                    title=sc["title"],
                    scenario_type=sc["type"],
                    priority=index,
                    preconditions=sc.get("preconditions") or [],
                    steps=sc.get("steps") or [],
                    expected_assertions=sc.get("assertions") or [],
                    selected_for_materialization=True,
                )
            )
        GenerationScenario.objects.bulk_create(scenario_rows)

        artifact_rows = []
        for art in validated_artifacts:
            artifact_rows.append(
                GeneratedArtifact(
                    job=job,
                    artifact_type=art["artifact_type"],
                    relative_path=art["relative_path"],
                    content_draft=art["content"],
                    content_final=art["content"],
                    checksum=art["checksum"],
                    validation_status=art["validation_status"],
                    validation_errors=art["validation_errors"],
                    warnings=art["warnings"],
                )
            )
        GeneratedArtifact.objects.bulk_create(artifact_rows)

        job.crawl_summary = crawl_summary
        job.feature_summary = str(planning.get("feature_summary") or "")
        job.llm_notes = llm_notes[:100]
        job.validation_summary = validation_summary
        job.drafting_finished_on = timezone.now()
        strict_gate = _require_all_artifacts_valid()
        has_valid = validation_summary.get("valid_artifacts", 0) > 0
        if strict_gate and invalid_artifacts > 0:
            job.job_status = GenerationJob.STATE_FAILED
            job.error_message = f"Artifact validation failed: {_invalid_artifact_summary(validated_artifacts)}"
        else:
            job.job_status = GenerationJob.STATE_DRAFT_READY if has_valid else GenerationJob.STATE_FAILED
            if job.job_status == GenerationJob.STATE_FAILED:
                job.error_message = "No valid artifacts were generated."
            else:
                job.error_message = ""
        job.save(
            update_fields=[
                "crawl_summary",
                "feature_summary",
                "llm_notes",
                "validation_summary",
                "drafting_finished_on",
                "job_status",
                "error_message",
                "last_modified",
            ]
        )
        return job
    except Exception as exc:
        job.job_status = GenerationJob.STATE_FAILED
        job.error_message = str(exc)
        job.drafting_finished_on = timezone.now()
        job.save(update_fields=["job_status", "error_message", "drafting_finished_on", "last_modified"])
        return job


def apply_approval_selection(
    *,
    job: GenerationJob,
    include_scenario_ids: List[str] | None,
    exclude_scenario_ids: List[str] | None,
) -> None:
    include_set = set(include_scenario_ids or [])
    exclude_set = set(exclude_scenario_ids or [])
    if not include_set and not exclude_set:
        return
    for scenario in job.scenarios.all():
        selected = True
        if include_set:
            selected = scenario.scenario_id in include_set
        if scenario.scenario_id in exclude_set:
            selected = False
        scenario.selected_for_materialization = selected
        scenario.save(update_fields=["selected_for_materialization", "last_modified"])


@dataclass
class MaterializationResult:
    written_files: List[str]
    conflicts: List[str]
    errors: List[str]

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors


def materialize_job(
    job: GenerationJob,
    *,
    allow_overwrite: bool = False,
    client_slug: Optional[str] = None,
) -> MaterializationResult:
    """
    Write each VALID artifact to disk.

    When `client_slug` is set (Phase 1 multi-tenant path), files land under
    `tests/generated/<slug>/...` and `tests/pages/generated/<slug>/...` so the
    test runner can find them per-client without cross-tenant collisions.
    """
    # Fall back to the job's own client.slug if the caller did not pass one.
    effective_slug = client_slug
    if effective_slug is None and getattr(job, "client_id", None):
        try:
            effective_slug = job.client.slug if job.client else None
        except Exception:
            effective_slug = None

    repo_root = _repo_root()
    artifacts = job.artifacts.filter(validation_status=GeneratedArtifact.VALID).order_by("relative_path")
    written_files: List[str] = []
    conflicts: List[str] = []
    errors: List[str] = []
    manifest: List[Dict[str, Any]] = []

    for artifact in artifacts:
        rp_logical = (artifact.relative_path or "").replace("\\", "/").strip()
        path_errors = _validate_relative_path(rp_logical)
        if path_errors:
            errors.append(f"{rp_logical}: {'; '.join(path_errors)}")
            continue

        # Inject the client slug AFTER validation so the LLM prompt/spec still uses
        # the canonical layout (tests/generated/...) and only disk writes diverge.
        rp_on_disk = _scoped_relative_path(rp_logical, effective_slug)

        target = (repo_root / rp_on_disk).resolve()
        try:
            target.relative_to(repo_root.resolve())
        except ValueError:
            errors.append(f"{rp_on_disk}: resolved outside repository root")
            continue

        if target.exists() and not allow_overwrite:
            conflicts.append(rp_on_disk)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        content = artifact.content_final or artifact.content_draft or ""
        # When materializing under a tenant slug, the on-disk paths are
        # e.g. `tests/pages/generated/blu-b2c/HomePage.ts`, but the artifact
        # sources use the canonical unscoped paths. Rewrite step-defs +
        # spec-file imports of `tests/pages/generated/<Name>` to
        # `tests/pages/generated/<slug>/<Name>` so Node can find them.
        if effective_slug and artifact.artifact_type in (
            GeneratedArtifact.TYPE_STEP_DEFINITIONS,
            GeneratedArtifact.TYPE_SPEC,
        ):
            content = _inject_slug_into_pageobject_imports(content, effective_slug)
        target.write_text(content, encoding="utf-8")
        checksum = _sha256(content)
        artifact.checksum = checksum
        artifact.content_final = content
        artifact.save(update_fields=["checksum", "content_final", "last_modified"])
        written_files.append(rp_on_disk)
        manifest.append(
            {
                "path": rp_on_disk,
                "logical_path": rp_logical,
                "client_slug": effective_slug,
                "checksum": checksum,
                "artifact_type": artifact.artifact_type,
            }
        )

    if not conflicts and not errors:
        job.job_status = GenerationJob.STATE_MATERIALIZED
        job.materialized_on = timezone.now()
        job.materialized_manifest = manifest
        job.save(update_fields=["job_status", "materialized_on", "materialized_manifest", "last_modified"])

    return MaterializationResult(
        written_files=written_files,
        conflicts=conflicts,
        errors=errors,
    )


def regenerate_job_artifacts_with_llm(job: GenerationJob) -> Dict[str, Any]:
    crawl_summary = _run_crawl_context(
        base_url=job.base_url,
        seed_urls=job.seed_urls or [],
        max_routes=job.max_routes or _max_routes_default(),
    )
    if not (crawl_summary.get("routes") or []):
        # Fallback to stored summary only if refresh could not load any route.
        crawl_summary = job.crawl_summary or crawl_summary or {}
    if not (crawl_summary.get("routes") or []):
        warnings = crawl_summary.get("warnings") or []
        warnings.append("No crawl routes available during LLM regeneration; selector validation will be low-confidence.")
        crawl_summary["warnings"] = warnings

    planning, scenarios, planning_notes = _plan_scenarios(
        job,
        crawl_summary,
        use_existing_scenarios=True,
        llm_first=True,
    )
    artifacts = _build_template_artifacts(
        job,
        {"scenarios": scenarios},
        crawl_summary,
    )
    notes = ["Template-only artifact regeneration completed (LLM used for planning only)."]
    validated_artifacts, validation_summary = _validate_artifacts(artifacts)
    validated_artifacts, runtime_selector_summary = _runtime_validate_selectors(
        validated_artifacts,
        base_url=job.base_url,
        crawl_summary=crawl_summary,
    )

    invalid_artifacts = sum(
        1 for artifact in validated_artifacts if artifact.get("validation_status") != GeneratedArtifact.VALID
    )
    validation_summary = {
        **validation_summary,
        "invalid_artifacts": invalid_artifacts,
        "valid_artifacts": len(validated_artifacts) - invalid_artifacts,
        "runtime_selector_validation": runtime_selector_summary,
        "runtime_selector_checked_count": runtime_selector_summary.get("checked_selectors", 0),
        "runtime_selector_missing_count": runtime_selector_summary.get("missing_selectors", 0),
        "strict_artifact_gate": _require_all_artifacts_valid(),
        "regenerated_via_llm": True,
        "regenerated_at": timezone.now().isoformat(),
    }

    job.scenarios.all().delete()
    scenario_rows = []
    for index, sc in enumerate(scenarios, start=1):
        scenario_rows.append(
            GenerationScenario(
                job=job,
                scenario_id=sc["id"],
                title=sc["title"],
                scenario_type=sc["type"],
                priority=index,
                preconditions=sc.get("preconditions") or [],
                steps=sc.get("steps") or [],
                expected_assertions=sc.get("assertions") or [],
                selected_for_materialization=True,
            )
        )
    GenerationScenario.objects.bulk_create(scenario_rows)

    job.artifacts.all().delete()
    artifact_rows = []
    for art in validated_artifacts:
        artifact_rows.append(
            GeneratedArtifact(
                job=job,
                artifact_type=art["artifact_type"],
                relative_path=art["relative_path"],
                content_draft=art["content"],
                content_final=art["content"],
                checksum=art["checksum"],
                validation_status=art["validation_status"],
                validation_errors=art["validation_errors"],
                warnings=art["warnings"],
            )
        )
    GeneratedArtifact.objects.bulk_create(artifact_rows)

    llm_notes = [str(n) for n in (job.llm_notes or [])]
    llm_notes.append("Artifacts regenerated via admin LLM action.")
    llm_notes.extend(planning_notes)
    llm_notes.extend([str(n) for n in notes[:10]])
    job.llm_notes = llm_notes[:100]
    job.crawl_summary = crawl_summary
    job.feature_summary = str(planning.get("feature_summary") or job.feature_summary or "")
    job.validation_summary = validation_summary
    strict_gate = _require_all_artifacts_valid()
    has_valid = validation_summary.get("valid_artifacts", 0) > 0
    if strict_gate and invalid_artifacts > 0:
        job.job_status = GenerationJob.STATE_FAILED
        job.error_message = f"Artifact validation failed: {_invalid_artifact_summary(validated_artifacts)}"
    else:
        job.job_status = GenerationJob.STATE_DRAFT_READY if has_valid else GenerationJob.STATE_FAILED
        if job.job_status == GenerationJob.STATE_FAILED:
            job.error_message = "No valid artifacts were generated via LLM regeneration."
        else:
            job.error_message = ""
    job.save(
        update_fields=[
            "llm_notes",
            "crawl_summary",
            "feature_summary",
            "validation_summary",
            "job_status",
            "error_message",
            "last_modified",
        ]
    )

    return {
        "total_artifacts": len(validated_artifacts),
        "valid_artifacts": validation_summary.get("valid_artifacts", 0),
        "invalid_artifacts": validation_summary.get("invalid_artifacts", 0),
        "runtime_selector_missing_count": validation_summary.get("runtime_selector_missing_count", 0),
        "status": job.job_status,
    }
