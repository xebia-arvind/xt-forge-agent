import re
from ignore_rules.memory import change_frequency
def matches_text_patterns(text, rules):
    for pattern in rules.get("text_patterns", []):
        if re.search(pattern, text):
            return True
    return False


def heuristic_score(el):
    score = 0
    text = (el.get("text") or "").strip()

    if not text:
        return 0

    # numeric-like
    if re.fullmatch(r"[\d,.€$%-]+", text):
        score += 2

    # long token-like (IDs, hashes)
    if len(text) > 12 and re.search(r"[A-Za-z0-9]{8,}", text):
        score += 2

    return score


def classify_element(el, rules):
    text = (el.get("text") or "").strip()
    key = f"{el.get('tag')}::{el.get('class')}::{text}"

    # Stage 1: Config
    if text and matches_text_patterns(text, rules):
        return "DYNAMIC"

    # Stage 2: Heuristics
    if rules.get("heuristics_enabled"):
        if heuristic_score(el) >= 2:
            return "DYNAMIC"

    # Stage 3: Learning
    if rules.get("learning_enabled"):
        freq = change_frequency(key)

        min_runs = rules["learning"]["min_runs_before_learning"]
        threshold = rules["learning"]["change_frequency_threshold"]

        if freq >= threshold:
            return "DYNAMIC"

    return "STATIC"
