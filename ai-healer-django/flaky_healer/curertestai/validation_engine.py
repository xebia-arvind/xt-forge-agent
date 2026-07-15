import json
import os
import re
from typing import Any, Dict, List, Tuple
from urllib import request as urllib_request
from urllib.error import URLError
from functools import lru_cache
from pathlib import Path

from curertestai.models import HealerRequest


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _intent(use_of_selector: str) -> str:
    text = _normalize(use_of_selector)
    if "add to cart" in text or ("add" in text and "cart" in text):
        return "add_to_cart"
    if "checkout" in text:
        return "checkout"
    if "cart" in text:
        return "cart"
    if "pay now" in text or "payment" in text:
        return "payment"
    if "view details" in text:
        return "view_details"
    return "generic"


DEFAULT_INTENT_POLICIES: Dict[str, Dict[str, Any]] = {
    "add_to_cart": {
        "blocked_selector_patterns": [
            r'(/cart|cart-icon|data-testid=["\']cart-icon|bi-bag)',
            r'^a\[',
        ],
        "allowed_hint_patterns": [
            r'button',
            r'add',
            r'cart-plus',
        ],
    },
    "checkout": {
        "blocked_selector_patterns": [],
        "allowed_hint_patterns": [r'checkout', r'proceed', r'pay', r'button', r'a\['],
    },
    "payment": {
        "blocked_selector_patterns": [r'cart-icon'],
        "allowed_hint_patterns": [r'pay', r'payment', r'button'],
    },
    "view_details": {
        "blocked_selector_patterns": [],
        "allowed_hint_patterns": [r'view', r'details', r'a\['],
    },
    "generic": {
        "blocked_selector_patterns": [],
        "allowed_hint_patterns": [],
    },
}

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "intent_policies.json"


@lru_cache(maxsize=1)
def _load_intent_policies() -> Dict[str, Dict[str, Any]]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict) and loaded:
                return loaded
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return DEFAULT_INTENT_POLICIES


def _resolve_intent(intent_key: str, use_of_selector: str) -> str:
    policies = _load_intent_policies()
    explicit = _normalize(intent_key)
    if explicit and explicit in policies:
        return explicit
    return _intent(use_of_selector)


def _rule_validate_candidate(intent: str, candidate_selector: str) -> Tuple[bool, str]:
    sel = _normalize(candidate_selector)

    policies = _load_intent_policies()
    policy = policies.get(intent, policies.get("generic", {}))

    for pattern in policy.get("blocked_selector_patterns", []):
        if re.search(pattern, sel):
            if intent == "add_to_cart" and pattern == r'^a\[' and "add" in sel:
                continue
            return False, f"INTENT_POLICY_BLOCKED:{pattern}"

    hints = policy.get("allowed_hint_patterns", [])
    if hints:
        if not any(re.search(pattern, sel) for pattern in hints):
            return False, "INTENT_POLICY_NO_ALLOWED_HINT_MATCH"

    return True, "VALID"


def _history_boost(page_url: str, use_of_selector: str, candidate_selector: str) -> Tuple[float, int]:
    recent = HealerRequest.objects.filter(
        url=page_url or "",
        use_of_selector=use_of_selector or "",
    ).order_by("-created_on")[:50]

    if not recent:
        return 0.0, 0

    success_hits = 0
    false_hits = 0

    for item in recent:
        if _normalize(item.healed_selector) == _normalize(candidate_selector):
            if item.success:
                success_hits += 1
            else:
                false_hits += 1

    total_hits = success_hits + false_hits
    if total_hits == 0:
        return 0.0, 0

    # Cap boost/penalty tightly for safety.
    boost = max(-0.2, min(0.2, (success_hits * 0.05) - (false_hits * 0.08)))
    return boost, total_hits


def _llm_enabled() -> bool:
    return os.getenv("USE_LLM_VALIDATION", "false").lower() == "true"


def _llm_score(intent: str, use_of_selector: str, candidate: Dict[str, Any], failed_selector: str) -> float:
    if not _llm_enabled():
        return 0.0

    url = os.getenv("LLM_VALIDATION_URL", "http://127.0.0.1:11434/api/generate")
    model = os.getenv("LLM_VALIDATION_MODEL", "qwen2.5-coder:7b")
    timeout = int(os.getenv("LLM_VALIDATION_TIMEOUT_SECONDS", "10"))

    prompt = (
        "You are a strict UI automation validator. "
        "Return JSON only: {\"score\": <0..1>, \"reason\": \"...\"}. "
        "Score means intent-match safety for this selector.\n"
        f"intent={intent}\n"
        f"use_of_selector={use_of_selector}\n"
        f"failed_selector={failed_selector}\n"
        f"candidate_selector={candidate.get('selector')}\n"
        f"candidate_tag={candidate.get('tag')}\n"
        f"candidate_text={candidate.get('text')}\n"
    )

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
            "num_predict": 120,
        },
    }

    req = urllib_request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw)
            model_output = parsed.get("response", "{}")

            score_payload = json.loads(model_output) if isinstance(model_output, str) else model_output
            score = float(score_payload.get("score", 0.0))
            return max(0.0, min(1.0, score))
    except (URLError, ValueError, TimeoutError, json.JSONDecodeError):
        return 0.0


def select_validated_candidate(
    *,
    candidates: List[Dict[str, Any]],
    failed_selector: str,
    use_of_selector: str,
    page_url: str,
    intent_key: str = "",
    current_signature_tokens: List[str] | None = None,
    ui_change_level: str = "UNKNOWN",
) -> Dict[str, Any]:
    if not candidates:
        no_candidate_reason = "No candidates available"
        if ui_change_level == "ELEMENT_REMOVED":
            no_candidate_reason = (
                "No candidates available. Historical comparison indicates target element was removed from UI."
            )
        elif ui_change_level == "MAJOR_CHANGE":
            no_candidate_reason = (
                "No candidates available. Historical comparison indicates major UI change."
            )
        return {
            "chosen": None,
            "validation_status": "NO_SAFE_MATCH",
            "validation_reason": no_candidate_reason,
            "llm_used": _llm_enabled(),
            "history_assisted": False,
            "history_hits": 0,
            "retrieval_assisted": False,
            "retrieval_hits": 0,
            "retrieved_versions": [],
        }

    intent = _resolve_intent(intent_key, use_of_selector)
    scored: List[Tuple[float, Dict[str, Any], str]] = []
    invalid_reasons: List[str] = []
    total_history_hits = 0

    for candidate in candidates:
        selector = candidate.get("selector", "")
        is_valid, reason = _rule_validate_candidate(intent, selector)
        if not is_valid:
            invalid_reasons.append(f"{selector}: {reason}")
            continue

        base = float(candidate.get("score", 0.0))
        history, history_hits = _history_boost(page_url, use_of_selector, selector)
        total_history_hits += history_hits
        llm = _llm_score(intent, use_of_selector, candidate, failed_selector)
        history_norm = max(0.0, history + 0.2) / 0.4
        # Retrieval boost was removed with DomSnapshot; weights re-normalized.
        final_score = (0.75 * base) + (0.15 * history_norm) + (0.10 * llm)
        scored.append((final_score, candidate, "VALID"))

    if not scored:
        reason = "No candidate passed validation rules"
        if invalid_reasons:
            reason = f"{reason}. Rejections: {' | '.join(invalid_reasons[:3])}"
        if ui_change_level == "ELEMENT_REMOVED":
            reason = (
                "No safe match. Historical comparison indicates target element was removed from UI."
            )
        elif ui_change_level == "MAJOR_CHANGE":
            reason = (
                "No safe match. Historical comparison indicates major UI change; suggestions blocked for safety."
            )
        return {
            "chosen": None,
            "validation_status": "NO_SAFE_MATCH",
            "validation_reason": reason,
            "llm_used": _llm_enabled(),
            "history_assisted": total_history_hits > 0,
            "history_hits": total_history_hits,
            "retrieval_assisted": False,
            "retrieval_hits": 0,
            "retrieved_versions": [],
        }

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_candidate, _ = scored[0]

    # Conservative threshold to avoid unsafe healing.
    if best_score < 0.10:
        return {
            "chosen": None,
            "validation_status": "NO_SAFE_MATCH",
            "validation_reason": f"Best candidate score too low ({best_score:.3f})",
            "llm_used": _llm_enabled(),
            "history_assisted": total_history_hits > 0,
            "history_hits": total_history_hits,
            "retrieval_assisted": False,
            "retrieval_hits": 0,
            "retrieved_versions": [],
        }

    chosen_selector = best_candidate.get("selector") or ""

    return {
        "chosen": chosen_selector,
        "validation_status": "VALID",
        "validation_reason": "Selector passed rule/history/LLM validation",
        "llm_used": _llm_enabled(),
        "history_assisted": total_history_hits > 0,
        "history_hits": total_history_hits,
        "retrieval_assisted": False,
        "retrieval_hits": 0,
        "retrieved_versions": [],
    }
