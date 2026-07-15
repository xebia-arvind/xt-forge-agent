import re
from typing import Any, Dict


def _contains(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def classify_failure(data: Dict[str, Any]) -> Dict[str, Any]:
    status = str(data.get("status") or "").upper()
    if status != "FAILED":
        return {
            "failure_category": "NOT_FAILED",
            "root_cause": data.get("root_cause") or "",
        }

    error_message = str(data.get("error_message") or "")
    failure_reason = str(data.get("failure_reason") or "")
    failed_selector = str(data.get("failed_selector") or "")
    healed_selector = str(data.get("healed_selector") or "")
    html = str(data.get("html") or "")

    healing_attempted = bool(data.get("healing_attempted") or False)
    healing_outcome = str(data.get("healing_outcome") or "NOT_ATTEMPTED")

    category = "UNCLASSIFIED_FAILURE"
    root_cause = data.get("root_cause") or "Unable to infer failure root cause"

    if _contains(error_message, r"timeout|timed out"):
        category = "TIMEOUT"
        root_cause = "Action/assertion timed out"
    elif _contains(error_message, r"element\(s\) not found|locator\("):
        category = "ELEMENT_NOT_FOUND"
        root_cause = "Expected element was not found in current DOM"
    elif _contains(error_message, r"navigation|net::|ECONN|ENOTFOUND|502|503|504"):
        category = "ENV_OR_NETWORK"
        root_cause = "Environment or network issue during test execution"

    # Domain-specific check for removed add-to-cart style actions
    if _contains(failure_reason, r"add to cart") and not _contains(html, r"add to cart"):
        category = "ELEMENT_REMOVED_OR_TEXT_CHANGED"
        root_cause = "Add-to-cart intent present in test but matching text not found in DOM"

    # Healer quality checks
    if healing_attempted and healing_outcome == "SUCCESS":
        if (
            _contains(failure_reason, r"add to cart")
            and _contains(healed_selector, r"/cart|cart-icon|cart")
            and not _contains(healed_selector, r"add")
        ):
            category = "HEALING_FALSE_POSITIVE"
            root_cause = "Healer resolved to navigation/cart link instead of add-to-cart action"
        elif category in {"ELEMENT_NOT_FOUND", "TIMEOUT", "UNCLASSIFIED_FAILURE"}:
            category = "POST_HEAL_ASSERTION_FAILURE"
            root_cause = "Healed click succeeded, but downstream assertion/flow still failed"

    if healing_attempted and healing_outcome == "FAILED":
        category = "HEALING_FAILED"
        root_cause = "Healer attempted fallback but could not recover the action"

    if not failed_selector and _contains(error_message, r"expect\("):
        category = "ASSERTION_FAILURE"
        root_cause = "Assertion failed without a tracked failed selector"

    return {
        "failure_category": category,
        "root_cause": root_cause,
    }
