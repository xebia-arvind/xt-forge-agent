"""
Deterministic locator fill (Phase 5.3).

After the artifact-generator LLM returns its draft, walk every locator
string in every generated page-object file. For each locator that is NOT
present in the ui_knowledge ground-truth inventory for that page, ask
`curertestai.matching_engine.MatchingEngine` to find the closest real
element based on the step's semantic text — and rewrite the file with
that selector. No LLM involved.

Why here (not later)
--------------------
Two upstream defenses already exist in Phase 5:
  5.2 tells the LLM "use only these selectors"
  5.3 (this file) enforces it deterministically when the LLM ignores 5.2
Selector Verifier + Root-Cause Fixer come later at Execute time; catching
the miss here saves an entire Cucumber iteration.

Fail-safe by design
-------------------
Any error in this module (matching engine unavailable, no candidates,
ui_knowledge empty) collapses into "leave the artifact alone and add a
warning to the report". The pipeline still runs.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .selector_verifier import _extract_locators

logger = logging.getLogger(__name__)


# Minimum matching-engine score to accept a swap. Below this we don't swap
# because the risk of picking a WRONG element (e.g. matching "user icon"
# to a menu item that happens to share text) outweighs the benefit.
_MIN_MATCH_SCORE = 0.35


def apply_deterministic_fill(job, artifacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    For every PAGE_OBJECT artifact in `artifacts`, replace any locator that
    doesn't exist in ui_knowledge with the matching engine's top pick.

    Returns a report dict:
        {
          "enabled": bool,
          "files_scanned": int,
          "locators_scanned": int,
          "locators_swapped": int,
          "swaps": [{path, kind, from, to, score, source}, ...],
          "misses": [{path, kind, from, reason}, ...],
        }

    Mutates `artifacts` in place (each dict's `content` is rewritten).
    """
    report: Dict[str, Any] = {
        "enabled": True,
        "files_scanned":    0,
        "locators_scanned": 0,
        "locators_swapped": 0,
        "swaps":  [],
        "misses": [],
    }

    inventory = _load_inventory(job)
    if not inventory["allowed_selectors"]:
        # No ground truth to enforce against — silently no-op.
        report["enabled"] = False
        return report

    # Match engine only initialized per-URL when we hit a miss (avoids the
    # sbert / faiss import overhead on green runs).
    engines_by_url: Dict[str, Any] = {}

    from ui_knowledge.models import UIPage

    for artifact in artifacts:
        if artifact.get("artifact_type") != "PAGE_OBJECT":
            continue
        rel_path = artifact.get("relative_path") or ""
        content = artifact.get("content") or ""
        if not content:
            continue
        report["files_scanned"] += 1

        # Match the artifact to a seed URL by class name.
        matched_url, matched_ui_page = _match_artifact_to_ui_page(job, rel_path)

        # Build the list of pages to probe against. Order matters:
        #   1. The class-name-mapped route (most specific).
        #   2. Any other route on the same client that HAS elements —
        #      handles the cross-origin-redirect case (Pulze `/login` and
        #      `/dashboard` both land on Azure B2C, and one may have 0
        #      elements while the other captured the form).
        probe_pages = _fallback_probe_pages(job, matched_url, matched_ui_page)
        if not probe_pages:
            report["misses"].append({
                "path":   rel_path,
                "kind":   "-",
                "from":   "-",
                "reason": "no ui_knowledge routes for this client",
            })
            continue

        # Union of allowed selectors across every probe page — a locator
        # that exists on a related route counts as ground truth for this
        # file (redirect-collapse tolerance).
        allowed_for_scope: set = set()
        for pg in probe_pages:
            allowed_for_scope |= inventory["per_url_selectors"].get(pg[0], set())

        locators = _extract_locators(content)
        if not locators:
            continue

        # Build one merged matching engine covering all probe pages. This
        # lets the intent-boosted score compare candidates across every
        # page at once, so a submit button on the B2C form outranks a
        # cookie-preferences button on the homepage when the intent is
        # "click login submit".
        scope_key = "||".join(u for u, _ in probe_pages)
        engine = engines_by_url.get(scope_key)
        if engine is None:
            engine = _build_merged_matching_engine([p for _u, p in probe_pages])
            engines_by_url[scope_key] = engine

        # Any selector that's already in ui_knowledge AND already appears
        # in the file is off-limits as a swap target — same class shouldn't
        # have two fields pointing at the same element.
        already_used_targets: set = set()
        for loc in locators:
            if loc["selector"] in allowed_for_scope:
                already_used_targets.add(loc["selector"])

        # Pre-score every locator so we can process them highest-confidence
        # first. This matters because reserving `#password` for `passwordInput`
        # (obvious match) BEFORE trying `usernameInput` (ambiguous) lets
        # the confidence gate pass for the harder case — `#password` is no
        # longer competing with `#signInName` for top slot.
        prescored: List[Tuple[Dict[str, str], Optional[str], float]] = []
        for loc in locators:
            report["locators_scanned"] += 1
            if loc["selector"] in allowed_for_scope:
                prescored.append((loc, None, 0.0))
                continue
            hint = _semantic_hint_for_locator(loc["selector"], content)
            swap, score = _pick_swap(engine, loc["selector"], hint,
                                     exclude_selectors=already_used_targets)
            prescored.append((loc, swap, score))
        # Sort by score DESCENDING so easy wins fire first, reserving
        # their targets and freeing the field for harder cases below.
        prescored.sort(key=lambda t: -(t[2] or 0.0))

        rewrite_map: Dict[str, str] = {}
        for loc, initial_swap, initial_score in prescored:
            sel = loc["selector"]
            if sel in allowed_for_scope:
                continue
            # Re-pick because our exclusion set may have grown since prescoring.
            swap, score = _pick_swap(
                engine, sel,
                semantic_hint=_semantic_hint_for_locator(sel, content),
                exclude_selectors=already_used_targets,
            )
            if swap is None or score < _MIN_MATCH_SCORE:
                report["misses"].append({
                    "path":   rel_path,
                    "kind":   loc["kind"],
                    "from":   sel,
                    "reason": f"no candidate (score={score:.2f})",
                })
                continue

            # Only queue the swap if the target is actually in ui_knowledge.
            if swap not in allowed_for_scope:
                report["misses"].append({
                    "path":   rel_path,
                    "kind":   loc["kind"],
                    "from":   sel,
                    "reason": "engine picked non-ground-truth candidate",
                })
                continue

            # Reserve this selector so subsequent locators in the SAME file
            # can't pick it too (avoids two fields → same element).
            already_used_targets.add(swap)
            rewrite_map[sel] = swap
            report["swaps"].append({
                "path":   rel_path,
                "kind":   loc["kind"],
                "from":   sel,
                "to":     swap,
                "score":  round(score, 3),
                "source": "curertestai_matching_engine",
            })
            report["locators_swapped"] += 1

        if rewrite_map:
            artifact["content"] = _rewrite_selectors(content, rewrite_map)

    return report


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def _load_inventory(job) -> Dict[str, Any]:
    """
    Build `{per_url_selectors: {route: set(selector)}, allowed_selectors: set(all)}`
    from every UIPage on the client. We walk ALL pages (not just seed
    URLs) so fallback-scope probing (see `_fallback_probe_pages`) can
    accept selectors captured under a sibling route — necessary when a
    seed URL cross-origin-redirects and the ground truth lands elsewhere.
    """
    from ui_knowledge.models import UIPage

    per_url: Dict[str, set] = {}
    total: set = set()

    for page in UIPage.objects.filter(client=job.client, is_active=True):
        snap = page.snapshots.filter(is_current=True).order_by("-version").first()
        if not snap:
            continue
        selectors = set()
        for el in snap.elements.all().values_list("selector", flat=True):
            s = str(el or "").strip()
            if s:
                selectors.add(s)
                total.add(s)
        if selectors:
            per_url[page.route] = selectors

    return {"per_url_selectors": per_url, "allowed_selectors": total}


# --------------------------------------------------------------------------
# Artifact ↔ seed URL mapping
# --------------------------------------------------------------------------
def _fallback_probe_pages(job, primary_url: Optional[str], primary_page):
    """
    Build an ordered list of `(route, UIPage)` pairs to probe against for
    an artifact.
      1. The primary (class-name-mapped) page comes first — most specific.
      2. Sibling pages on the same client are appended ONLY when the
         primary is missing or has zero elements. This prevents the
         `homepage's cookie-preferences-button` from getting picked as
         a candidate for the login page's submit button.
    """
    from ui_knowledge.models import UIPage

    pages: list = []
    primary_has_elements = False

    if primary_url and primary_page:
        snap = primary_page.snapshots.filter(is_current=True).order_by("-version").first()
        count = snap.elements.count() if snap else 0
        pages.append((primary_url, primary_page))
        primary_has_elements = count > 0

    # If the primary route captured real ground truth, DO NOT dilute with
    # siblings — cross-page selectors are usually noise for a page object.
    if primary_has_elements:
        return pages

    # Primary is missing / empty → fall back to sibling pages. Sorted by
    # element count so the richest ground truth is tried first.
    seen_ids: set = set()
    if primary_page:
        seen_ids.add(primary_page.id)

    siblings = UIPage.objects.filter(client=job.client, is_active=True)
    if seen_ids:
        siblings = siblings.exclude(id__in=seen_ids)

    scored: list = []
    for pg in siblings:
        snap = pg.snapshots.filter(is_current=True).order_by("-version").first()
        if not snap:
            continue
        count = snap.elements.count()
        if count == 0:
            continue
        scored.append((count, pg.route, pg))
    scored.sort(key=lambda t: -t[0])
    for _count, route, pg in scored:
        pages.append((route, pg))
    return pages


def _match_artifact_to_ui_page(job, rel_path: str) -> Tuple[Optional[str], Any]:
    """
    Given a page-object file path, resolve which seed URL (route) it maps to,
    and return (route, UIPage). Reuses the same class-name convention the
    artifact prompt uses (`_seed_url_to_class_name`).
    """
    from urllib.parse import urlparse
    from ui_knowledge.models import UIPage
    from .agents import _seed_url_to_class_name
    from .selector_verifier import _class_name_from_artifact_path

    target_class = _class_name_from_artifact_path(rel_path)
    if not target_class:
        return None, None

    seed_urls: List[str] = []
    if job.base_url:
        seed_urls.append(job.base_url)
    seed_urls.extend(job.seed_urls or [])

    for raw in seed_urls:
        raw = (raw or "").strip()
        if not raw:
            continue
        if _seed_url_to_class_name(raw) != target_class:
            continue
        parsed = urlparse(raw)
        route = parsed.path if (parsed.scheme and parsed.netloc) else (
            raw if raw.startswith("/") else f"/{raw}"
        )
        route = route or "/"
        page = UIPage.objects.filter(
            client=job.client, route=route, is_active=True
        ).first()
        if page:
            return route, page

    return None, None


# --------------------------------------------------------------------------
# Matching engine glue
# --------------------------------------------------------------------------
def _build_matching_engine(ui_page):
    """
    Instantiate curertestai.MatchingEngine over the UIPage's current
    snapshot elements. Returns None on any import/init failure.

    Each element dict carries an extra `_ui_knowledge_selector` field with
    the exact selector string from `UIElement.selector`. When we pick a
    candidate we return that string directly instead of the engine's
    re-derived CSS — this guarantees the swap is in the ground-truth
    allow-set (see `apply_deterministic_fill`).
    """
    try:
        from curertestai.matching_engine import MatchingEngine
        snap = ui_page.snapshots.filter(is_current=True).order_by("-version").first()
        if not snap:
            return None
        elements: List[Dict[str, Any]] = []
        for el in snap.elements.all():
            selector = str(el.selector or "")
            elements.append({
                "tag":              str(el.tag or ""),
                "text":             str(el.text or ""),
                "accessible_name":  str(el.text or ""),
                "role":             str(el.role or ""),
                "selector":         selector,
                "xpath":            "",
                "attributes": {
                    "id":          str(el.element_id or ""),
                    "data-testid": str(el.test_id or ""),
                    "class":       "",
                },
                "context": {"parent": "", "parent_class": ""},
                # Non-standard key — our post-processing reads this to
                # override the engine's re-derived `suggested`.
                "_ui_knowledge_selector": selector,
            })
        return MatchingEngine(elements)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deterministic_fill: matching-engine init failed: %s", exc)
        return None


def _build_merged_matching_engine(ui_pages):
    """
    Instantiate one `curertestai.MatchingEngine` seeded with UIElement
    rows from every page in `ui_pages`. Lets the intent-boosted ranker
    compare candidates ACROSS pages so a real submit button on the login
    form beats a lookalike button on the homepage.
    """
    try:
        from curertestai.matching_engine import MatchingEngine
    except Exception as exc:  # noqa: BLE001
        logger.warning("deterministic_fill: matching-engine import failed: %s", exc)
        return None

    elements: List[Dict[str, Any]] = []
    for pg in ui_pages:
        snap = pg.snapshots.filter(is_current=True).order_by("-version").first()
        if not snap:
            continue
        for el in snap.elements.all():
            selector = str(el.selector or "")
            if not selector:
                continue
            elements.append({
                "tag":              str(el.tag or ""),
                "text":             str(el.text or ""),
                "accessible_name":  str(el.text or ""),
                "role":             str(el.role or ""),
                "selector":         selector,
                "xpath":            "",
                "attributes": {
                    "id":          str(el.element_id or ""),
                    "data-testid": str(el.test_id or ""),
                    "class":       "",
                },
                "context": {"parent": "", "parent_class": ""},
                "_ui_knowledge_selector": selector,
            })
    if not elements:
        return None
    try:
        return MatchingEngine(elements)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deterministic_fill: matching-engine init failed: %s", exc)
        return None


def _pick_swap(engine, failed_selector: str, semantic_hint: str,
               exclude_selectors: Optional[set] = None) -> Tuple[Optional[str], float]:
    """
    Ask MatchingEngine for the top candidate, then re-score by
    element-role affinity. Returns (canonical_selector, score).

    `exclude_selectors` — canonical UI-knowledge selectors that cannot
    be picked (already used elsewhere in the same file or already
    correct in-place). Prevents two fields in the same page-object
    from being auto-mapped to the same DOM element.

    The base matching engine ranks by string similarity, which prefers
    candidates whose test-id or class name lexically resembles the
    failed selector's name. That's a problem when the LLM invented a
    descriptive name (`loginSubmitButton`) but the real element has a
    generic id (`#next`) — the base ranker prefers a lookalike link
    over the actual submit button. This re-scoring pass boosts
    candidates whose TAG/ROLE matches the intent implied by the
    semantic hint (`submit`/`click` → button, `enter`/`fill` → input,
    etc.), keeping the base similarity as a tiebreak.
    """
    if engine is None or not getattr(engine, "ready", False):
        return None, 0.0
    try:
        results = engine.rank(failed_selector, semantic_hint, top_k=25)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deterministic_fill: rank() failed: %s", exc)
        return None, 0.0
    if not results:
        return None, 0.0

    # Filter out selectors we're not allowed to pick (already reserved
    # for another locator in the same file, etc.).
    if exclude_selectors:
        results = [
            r for r in results
            if str((r.get("element") or {}).get("_ui_knowledge_selector") or "").strip()
            not in exclude_selectors
        ]
        if not results:
            return None, 0.0

    intent = _classify_intent(failed_selector, semantic_hint)
    # Build "off-topic" penalty tokens: the general conflict set for this
    # intent, MINUS any token that already appears in the failed selector
    # or hint (which would mean the LLM is deliberately targeting that
    # topic — e.g. `acceptCookiesButton` targets the cookie banner).
    hint_blob = (failed_selector + " " + semantic_hint).lower()
    conflict_tokens = tuple(
        t for t in _conflict_tokens_for_intent(intent) if t not in hint_blob
    )

    def _adjusted_score(r: Dict[str, Any]) -> float:
        base = float(r.get("score") or 0.0)
        element = r.get("element") or {}
        tag = str(element.get("tag") or "").lower()
        role = str(element.get("role") or "").lower()
        blob = " ".join([
            str(element.get("text") or ""),
            str((element.get("attributes") or {}).get("data-testid") or ""),
            str((element.get("attributes") or {}).get("id") or ""),
            str(element.get("selector") or ""),
        ]).lower()
        bonus = 0.0

        # Tag / role affinity bump.
        if intent == "submit_click":
            if tag == "button":
                bonus += 0.20
                # A button with visible text is almost always the target
                # of a "click" intent — the LLM's field name doesn't
                # translate across locales, but a real button HAS a
                # label that a human maps to the action. This lifts
                # `#next` (text="Accedi") over `#GoogleExchange`
                # (text="Google") for a login-submit intent.
                text = str(element.get("text") or "").strip()
                if text:
                    bonus += 0.10
            elif tag == "input" and role in ("button", "submit"):
                bonus += 0.15
            elif tag == "a":
                bonus -= 0.10
        elif intent == "text_input":
            if tag == "input" and role not in ("button", "submit", "checkbox", "radio"):
                bonus += 0.20
            elif tag == "textarea":
                bonus += 0.15
            elif tag == "button" or tag == "a":
                bonus -= 0.10
        elif intent == "select":
            if tag == "select":
                bonus += 0.25

        # Off-topic candidate penalty — larger than the tag bump so a
        # topic-mismatched button loses to a topic-matched one.
        for token in conflict_tokens:
            if token in blob:
                bonus -= 0.30
                break

        return base + bonus

    # Re-rank in place.
    ranked = sorted(results, key=_adjusted_score, reverse=True)
    top = ranked[0]
    top_score = _adjusted_score(top)

    # Confidence gate: only reject when the top candidate has a
    # near-tie with a candidate whose ELEMENT would be a plausible
    # confusion (same tag, same tag-role). In practice for a login
    # form's inputs, the top-2 will BOTH be inputs; refusing every such
    # case defeats the purpose. Trust the intent-boosted top pick
    # unless there's essentially no daylight (< 0.01) — a truly-tied
    # ranking means the engine can't distinguish and we shouldn't
    # blindly commit. The intent-aware bonuses, off-topic penalties,
    # and cross-file dedup handle the "obviously wrong" cases upstream.
    if len(ranked) >= 2:
        second = ranked[1]
        margin = top_score - _adjusted_score(second)
        if margin < 0.01:
            return None, top_score

    element = top.get("element") or {}
    canonical = str(element.get("_ui_knowledge_selector") or "").strip()
    if not canonical:
        canonical = str(top.get("suggested") or "")
    return canonical, top_score


# --------------------------------------------------------------------------
# Intent classification for re-ranking
# --------------------------------------------------------------------------
_INTENT_KEYWORDS = {
    "submit_click": (
        "submit", "click", "press", "confirm", "accept", "login", "signin",
        "sign_in", "signup", "sign_up", "register", "continue", "next",
        "accedi", "conferma", "invia", "avanti", "button", "btn",
    ),
    "text_input": (
        "input", "field", "type", "fill", "enter", "email", "password",
        "username", "user_name", "text", "search", "query", "value",
    ),
    "select": (
        "select", "dropdown", "combo", "combobox", "picker",
        "year", "month", "day",
    ),
}


# Topics that are almost never what a given intent is targeting. Applied
# as a negative bonus in _adjusted_score so a matching-tag candidate with
# a conflicting topic loses to a matching-tag candidate on-topic.
_INTENT_CONFLICT_TOKENS = {
    "submit_click": (
        "cookie", "cookies", "consent", "preferences", "gdpr", "onetrust",
        "signup", "sign-up", "register", "registrati",
        "forgot", "forgotten", "reset", "recover",
        "logout", "sign-out", "signout",
        "cancel", "annulla", "close", "chiudi",
    ),
    "text_input": (
        "cookie", "preferences",
    ),
    "select": (
        "cookie",
    ),
}


def _conflict_tokens_for_intent(intent: str):
    return _INTENT_CONFLICT_TOKENS.get(intent, ())


def _classify_intent(failed_selector: str, semantic_hint: str) -> str:
    """
    Best-effort infer whether the LLM was trying to grab a submit button,
    a text input, a select, etc. Uses tokens from both the original
    failed selector name AND the semantic hint (method / field name).
    """
    blob = (failed_selector + " " + semantic_hint).lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(kw in blob for kw in keywords):
            return intent
    return "generic"


# --------------------------------------------------------------------------
# Selector rewriting
# --------------------------------------------------------------------------
def _semantic_hint_for_locator(selector: str, content: str) -> str:
    """
    Best-effort "what is this selector for?" hint we can feed to the
    matching engine's `use_of_selector` parameter. Grabs surrounding
    context from the method name and any nearby comment.
    """
    # Method / function name that contains this selector call.
    lower_content = content
    idx = lower_content.find(selector)
    if idx < 0:
        return selector
    prefix = lower_content[max(0, idx - 400) : idx]
    method_match = re.search(
        r"(?:async\s+)?([a-z][a-zA-Z0-9_]*)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{[^{}]*$",
        prefix,
    )
    tokens: List[str] = []
    if method_match:
        # Split camelCase method name into space-separated words.
        name = method_match.group(1)
        tokens.extend(re.findall(r"[A-Za-z][a-z0-9]*|[A-Z0-9]+(?=[A-Z][a-z])|[A-Z0-9]+", name))
    if not tokens:
        # Fall back to the raw selector as the hint.
        return selector
    return " ".join(t.lower() for t in tokens)


def _rewrite_selectors(content: str, rewrite_map: Dict[str, str]) -> str:
    """
    Replace every occurrence of each `old` selector with `new` inside the
    file content. Preserves quoting because we only rewrite the selector
    substring itself, never the surrounding quote characters.
    """
    out = content
    for old, new in rewrite_map.items():
        # Escape for use inside a regex, but replacement is literal.
        out = out.replace(old, new)
    return out
