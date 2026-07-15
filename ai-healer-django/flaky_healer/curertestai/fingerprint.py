import hashlib
from typing import Any, Dict, List, Set


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _truncate(text: str, max_len: int = 80) -> str:
    return _norm(text)[:max_len]


def build_dom_signature_tokens(elements: List[Dict[str, Any]]) -> Set[str]:
    tokens: Set[str] = set()

    for el in elements[:800]:
        tag = _norm(str(el.get("tag") or ""))
        if tag:
            tokens.add(f"tag:{tag}")

        attrs = el.get("attributes") or {}
        for key in ("id", "class", "role", "aria-label", "data-testid", "type", "name"):
            value = _norm(str(attrs.get(key) or ""))
            if value:
                tokens.add(f"{key}:{value}")

        text = _truncate(str(el.get("text") or el.get("accessible_name") or ""))
        if text:
            tokens.add(f"text:{text}")

    return tokens


def generate_dom_fingerprint(elements: List[Dict[str, Any]]) -> str:
    tokens = sorted(build_dom_signature_tokens(elements))
    payload = "||".join(tokens)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def jaccard_similarity(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)
