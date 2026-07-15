"""
Unified artifact validation.

One `ArtifactValidator.validate(artifact_type, path, content, ctx)` call is the
"standard template" the operator asked for. Every rule lives in the registry
below with a name + description + check function. Endpoints, panels, and the
review UI all go through the same call so validation stays consistent.

Backward-compat: `_validate_artifact_content` in `generation_service.py` keeps
its `(errors: List[str], warnings: List[str])` signature by adapting our
`ValidationResult` output — nothing outside this module needs to know about
the refactor. No new external dependencies (uses stdlib `dataclasses`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------
@dataclass
class ValidationError:
    """One failing rule, machine-readable."""
    rule: str
    message: str
    severity: str = "error"      # error | warning

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """
    Returned by `ArtifactValidator.validate(...)`.

    Callers that need the legacy `List[str]` shape can call
    `result.error_messages()` / `result.warning_messages()`.
    """
    artifact_type: str
    is_valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    def error_messages(self) -> List[str]:
        return [e.message for e in self.errors]

    def warning_messages(self) -> List[str]:
        return [w.message for w in self.warnings]

    def dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "is_valid": self.is_valid,
            "errors": [e.dict() for e in self.errors],
            "warnings": [w.dict() for w in self.warnings],
        }


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------
# A rule takes (text, ctx) → Optional[error message]. Returning None means the
# rule passed. `ctx` is a dict carrying artifact-type-specific inputs like
# `seed_urls`, `feature_slug`, and the artifact path.
CheckFn = Callable[[str, Dict[str, Any]], Optional[str]]


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    check: CheckFn
    severity: str = "error"      # error | warning


# Per-artifact-type registries, populated by decorators below.
_REGISTRY: Dict[str, List[Rule]] = {
    "FEATURE": [],
    "STEP_DEFINITIONS": [],
    "SPEC": [],
    "PAGE_OBJECT": [],
    "*": [],                     # shared rules run against every type
}


def _register(artifact_type: str, name: str, description: str,
              severity: str = "error"):
    def wrap(fn: CheckFn) -> CheckFn:
        _REGISTRY.setdefault(artifact_type, []).append(
            Rule(name=name, description=description, check=fn, severity=severity)
        )
        return fn
    return wrap


# ---------------------------------------------------------------------------
# Shared rules (apply to every artifact type)
# ---------------------------------------------------------------------------
@_register("*", "shared/non-empty",
           "Artifact content must not be empty.")
def _rule_non_empty(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    return None if (text or "").strip() else "Artifact content is empty."


@_register("*", "shared/balanced-braces",
           "TypeScript files must not be truncated — braces/parens/brackets/quotes must balance.")
def _rule_balanced_braces(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    """
    Cheap truncation detector. Skips Gherkin (`.feature`) since it has no
    balanced brackets. For .ts artifacts we scan character-by-character,
    ignoring braces inside `"…"` / `'…'` / `` `…` `` strings and `//`, `/* */`
    comments, then compare open-vs-close counts for `{}`, `()`, `[]`. Any
    unbalanced pair means the LLM cut off mid-file (see e.g. a HomePage.ts
    ending on `'[data-testid=`).
    """
    if ctx.get("_is_gherkin"):
        return None
    if not text:
        return None

    braces = {"{": 0, "(": 0, "[": 0}
    closers = {"}": "{", ")": "(", "]": "["}
    in_str: Optional[str] = None
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Comment / string state machine.
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1; continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2; continue
            i += 1; continue
        if in_str is not None:
            if c == "\\":
                i += 2; continue         # skip escaped char
            if c == in_str:
                in_str = None
            i += 1; continue
        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2; continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2; continue
        if c in ("'", '"', "`"):
            in_str = c
            i += 1; continue
        if c in braces:
            braces[c] += 1
        elif c in closers:
            braces[closers[c]] -= 1
        i += 1

    if in_str is not None:
        return f"File ends inside an unterminated {in_str!r} string — content is truncated."
    if in_block_comment:
        return "File ends inside an unterminated /* … */ comment — content is truncated."

    if any(v != 0 for v in braces.values()):
        pretty = ", ".join(f"{{={braces['{']}, (={braces['(']}, [={braces['[']}".split(","))
        return (
            f"Unbalanced brackets (open minus close: {pretty}) — the file is "
            "likely truncated or the LLM emitted invalid TypeScript."
        )
    return None


@_register("*", "shared/no-repetition-loop",
           "LLM must not degenerate into a repetition loop (>40 identical lines).")
def _rule_no_repetition_loop(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not text or len(text) <= 400:
        return None
    lines = [ln for ln in text.split("\n") if ln.strip()]
    counts: Dict[str, int] = {}
    for ln in lines:
        counts[ln] = counts.get(ln, 0) + 1
    if not counts:
        return None
    top_line, top_count = max(counts.items(), key=lambda kv: kv[1])
    if top_count > 40:
        preview = top_line.strip()[:60]
        return (
            f"LLM output appears to have degenerated into a repetition loop "
            f"({top_count} identical lines of {preview!r}). Rerun with a "
            f"different model or bump TEST_GEN_REPEAT_PENALTY."
        )
    return None


# ---------------------------------------------------------------------------
# Rules for non-Gherkin (TypeScript-ish) artifacts — apply to STEP_DEFINITIONS,
# SPEC, PAGE_OBJECT but NOT FEATURE (Gherkin ≠ JS).
# ---------------------------------------------------------------------------
_FORBIDDEN_JS_PATTERNS = [
    ("shared/no-waitForTimeout", "Do not use `waitForTimeout` — use `expect(...).toBeVisible()`.",
     r"\bwaitForTimeout\s*\("),
    ("shared/no-setTimeout", "Do not use `setTimeout` in tests.",
     r"\bsetTimeout\s*\("),
    ("shared/no-test-only", "Do not commit `test.only(...)`.",
     r"\btest\.only\s*\("),
    ("shared/no-process-exit", "Do not use `process.exit(...)` in tests.",
     r"\bprocess\.exit\s*\("),
]
_JS_TYPES = {"STEP_DEFINITIONS", "SPEC", "PAGE_OBJECT"}


def _forbidden_check_factory(pattern: str, message: str) -> CheckFn:
    compiled = re.compile(pattern)
    def _inner(text: str, ctx: Dict[str, Any]) -> Optional[str]:
        if ctx.get("_is_gherkin"):
            return None
        return message if compiled.search(text) else None
    return _inner


for name, description, pattern in _FORBIDDEN_JS_PATTERNS:
    # Register against every type; the check itself no-ops on Gherkin.
    for _t in _JS_TYPES:
        _REGISTRY[_t].append(Rule(
            name=name,
            description=description,
            check=_forbidden_check_factory(pattern, description),
        ))


# Brittle-nth selectors → warning, not error (matches previous behaviour).
_NTH_RE = re.compile(r"\.nth\(\d+\)")
for _t in _JS_TYPES:
    _REGISTRY[_t].append(Rule(
        name="shared/avoid-nth-selectors",
        description="Prefer semantic selectors over `.nth(<n>)`.",
        check=lambda t, c, _re=_NTH_RE: (
            "Avoid brittle nth(index) selectors" if _re.search(t) else None
        ),
        severity="warning",
    ))


# ---------------------------------------------------------------------------
# FEATURE (Gherkin)
# ---------------------------------------------------------------------------
@_register("FEATURE", "feature/has-header",
           "The file must start with a `Feature:` header.")
def _feature_has_header(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not re.search(r"^\s*Feature:\s+\S", text, flags=re.MULTILINE):
        return "Feature file must contain a `Feature:` header."
    return None


@_register("FEATURE", "feature/has-scenario",
           "The file must contain at least one `Scenario:` (or `Scenario Outline:`) block.")
def _feature_has_scenario(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not re.search(r"\bScenario(?:\s+Outline)?:\s+\S", text):
        return "Feature file must contain at least one `Scenario:` block."
    return None


@_register("FEATURE", "feature/has-given-step",
           "The scenario must include at least one `Given` step.")
def _feature_has_given(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not re.search(r"(?<![A-Za-z])Given\s+\S", text):
        return "Feature file must contain at least one `Given` step."
    return None


@_register("FEATURE", "feature/has-when-step",
           "The scenario must include at least one `When` step.")
def _feature_has_when(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not re.search(r"(?<![A-Za-z])When\s+\S", text):
        return "Feature file must contain at least one `When` step."
    return None


@_register("FEATURE", "feature/has-then-step",
           "The scenario must include at least one `Then` step.")
def _feature_has_then(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not re.search(r"(?<![A-Za-z])Then\s+\S", text):
        return "Feature file must contain at least one `Then` step."
    return None


# ---------------------------------------------------------------------------
# STEP_DEFINITIONS
# ---------------------------------------------------------------------------
@_register("STEP_DEFINITIONS", "stepdefs/imports-cucumber",
           "Step defs must import Given/When/Then from '@cucumber/cucumber'.")
def _stepdefs_imports_cucumber(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "from '@cucumber/cucumber'" not in text:
        return "Step defs must import from '@cucumber/cucumber'."
    return None


@_register("STEP_DEFINITIONS", "stepdefs/no-selfHealingClick",
           "Do not use selfHealingClick — clicks are ordinary Playwright locator interactions.")
def _stepdefs_no_healing(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "selfHealingClick" in text:
        return (
            "Step defs reference `selfHealingClick` — that helper is no longer "
            "part of this codebase. Use plain Playwright instead: "
            "`await this.page.locator(SELECTOR).click();`"
        )
    return None


# `\b` matches at `@|Given`, so we exclude `@` explicitly. Only accept
# function-call style `Given(...)`, which is the Cucumber-JS API.
_STEPDEFS_REGISTRATION_RE = re.compile(r"(?<![A-Za-z@])(Given|When|Then)\s*\(")


@_register("STEP_DEFINITIONS", "stepdefs/registers-step",
           "At least one function-style Given/When/Then registration must be present.")
def _stepdefs_registers(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not _STEPDEFS_REGISTRATION_RE.search(text):
        return (
            "Step defs must register at least one Given/When/Then step via the "
            "function-call form, e.g. `Given('I open the homepage', async function () {…})`."
        )
    return None


@_register("STEP_DEFINITIONS", "stepdefs/no-decorator-registrations",
           "Cucumber-JS uses function-call registration, not TypeScript decorators.")
def _stepdefs_no_decorators(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if re.search(r"@\s*(Given|When|Then|And|But)\s*\(", text):
        return (
            "TypeScript decorator syntax (`@Given(...)`, `@When(...)`, `@Then(...)`) "
            "is a TestCafe/other-framework idiom — Cucumber-JS does not support it. "
            "Use the function-call form instead: `Given('step text', async function () {…})`."
        )
    return None


@_register("STEP_DEFINITIONS", "stepdefs/no-default-class-export",
           "Step defs must not wrap step registrations in a `class` or `export default class`.")
def _stepdefs_no_class_wrapper(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if re.search(r"export\s+default\s+class\b", text):
        return (
            "Step-defs file uses `export default class` — Cucumber-JS discovers steps "
            "via module-level Given/When/Then calls, not class methods. Move each "
            "registration to module scope and delete the class wrapper."
        )
    return None


@_register("STEP_DEFINITIONS", "stepdefs/expect-import-when-used",
           "If step defs use `expect(...)`, they must import it from '@playwright/test'.")
def _stepdefs_expect_import(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "expect(" not in text:
        return None
    if re.search(
        r"import\s*\{[^}]*\bexpect\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]",
        text,
    ):
        return None
    return (
        "Step defs call `expect(...)` but never import it. Add: "
        "`import { expect } from '@playwright/test';`"
    )


@_register("STEP_DEFINITIONS", "stepdefs/uppercase-registrations",
           "Cucumber-JS exports only `Given` / `When` / `Then` — lowercase names crash at runtime.")
def _stepdefs_uppercase(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    m = re.search(r"(?<![A-Za-z0-9_.])(given|when|then|and|but)\s*\(", text)
    if m:
        cap = m.group(1).capitalize()
        target = cap if cap in ("Given", "When", "Then") else "Given/When/Then"
        return (
            f"Lowercase step call `{m.group(1)}(...)` — @cucumber/cucumber "
            f"only exports `Given` / `When` / `Then`. Use `{target}(...)` "
            f"instead (Gherkin And/But inherit the previous verb's context)."
        )
    return None


@_register("STEP_DEFINITIONS", "stepdefs/no-and-but-registrations",
           "`And` and `But` are NOT exported by @cucumber/cucumber — register with the previous Given/When/Then verb.")
def _stepdefs_no_and_but(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    m = re.search(r"(?<![A-Za-z0-9_@])(And|But)\s*\(", text)
    if m:
        return (
            f"`{m.group(1)}(...)` is not a Cucumber-JS registration function. "
            f"@cucumber/cucumber only exports `Given` / `When` / `Then`. In "
            f"step defs, register And/But lines with the SAME verb the "
            f"previous step used (Gherkin And/But inherit the previous "
            f"keyword's context)."
        )
    return None


@_register("STEP_DEFINITIONS", "stepdefs/named-page-object-imports",
           "Page objects must be imported by name — page objects export `export class Foo`, not default.")
def _stepdefs_named_page_object_imports(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    """
    Catch `import HomePage from '../../tests/pages/generated/HomePage'` — the
    page-object files use `export class HomePage`, so a default import
    resolves to `undefined` at runtime and every `new HomePage(...)` crashes.
    Named import is required: `import { HomePage } from '.../HomePage'`.
    """
    for m in re.finditer(
        r"import\s+([^{\s][^\s,;]*)\s+from\s+['\"]([^'\"]+)['\"]",
        text,
    ):
        symbol = m.group(1).strip()
        module = m.group(2)
        if "tests/pages/generated/" not in module:
            continue
        # Skip if it's actually the destructured `{X}` form (shouldn't match
        # this pattern, but belt-and-braces).
        if symbol.startswith("{"):
            continue
        return (
            f"Default import `{symbol}` from {module!r} — page-object files use "
            "`export class`, so a default import evaluates to undefined. Use a "
            f"named import: `import {{ {symbol} }} from '{module}';`"
        )
    return None


_TESTCAFE_PATTERNS = [
    ("stepdefs/no-testcafe-selector",
     "TestCafe `Selector(...)` API is not supported — use `page.locator(...)`.",
     r"\bSelector\s*\("),
    ("stepdefs/no-waitForVisible",
     "TestCafe `.waitForVisible()` is not supported — use `expect(locator).toBeVisible()`.",
     r"\.waitForVisible\s*\("),
    ("stepdefs/no-selfSelectOption",
     "TestCafe `.selfSelectOption()` is not supported — use `locator.selectOption(...)`.",
     r"\.selfSelectOption\s*\("),
]
for _n, _d, _p in _TESTCAFE_PATTERNS:
    _REGISTRY["STEP_DEFINITIONS"].append(Rule(
        name=_n, description=_d,
        check=_forbidden_check_factory(_p, _d),
    ))


@_register("STEP_DEFINITIONS", "stepdefs/no-barrel-import",
           "Each page-object import must reference the file explicitly (no folder imports).")
def _stepdefs_no_barrel(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    for m in re.finditer(r"from\s+['\"]([^'\"]+)['\"]", text):
        src = m.group(1)
        if src.endswith("/") and "tests/pages/generated" in src:
            return (
                f"Barrel/folder import {src!r} — import each page object by "
                "its exact file path (e.g. `.../generated/LoginPage`)."
            )
    return None


# ---------------------------------------------------------------------------
# SPEC (legacy Playwright specs)
# ---------------------------------------------------------------------------
_SPEC_PLAYWRIGHT_IMPORT_RE = re.compile(
    r"import\s*\{[^}]*\btest\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]"
)


@_register("SPEC", "spec/imports-playwright",
           "Spec must import { test, expect } from '@playwright/test'.")
def _spec_imports_playwright(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if not _SPEC_PLAYWRIGHT_IMPORT_RE.search(text):
        return "Spec must import { test, expect } from '@playwright/test'."
    return None


@_register("SPEC", "spec/no-selfHealingClick",
           "Spec must not use selfHealingClick — call locator.click() directly.")
def _spec_no_healing(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "selfHealingClick" in text:
        return (
            "Spec references `selfHealingClick` — that helper is no longer part "
            "of this codebase. Use `locator.click()` instead."
        )
    return None


@_register("SPEC", "spec/has-assertion",
           "Spec must include at least one expect(...) assertion.")
def _spec_has_assertion(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "expect(" not in text:
        return "Spec must include assertion."
    return None


_SPEC_INCOMPAT = [
    ("spec/no-suite", "Use Playwright test.describe(), not suite().", r"\bsuite\s*\("),
    ("spec/no-suiteSetup", "Use Playwright hooks, not suiteSetup().", r"\bsuiteSetup\s*\("),
    ("spec/no-test-page", "Use injected `{ page }`, not test.page.", r"\btest\.page\b"),
    ("spec/no-test-page-constructor",
     "Do not construct page objects with test.page.",
     r"\bconst\s+\w+\s*=\s*new\s+\w+\s*\(\s*test\.page\s*\)"),
]
for _n, _d, _p in _SPEC_INCOMPAT:
    _REGISTRY["SPEC"].append(Rule(
        name=_n, description=_d,
        check=_forbidden_check_factory(_p, _d),
    ))


_URL_RE = re.compile(r"^\s*(https?://\S+|/[\w\-\./?=&#]*)\s*$")


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value or ""))


# Note: legacy `spec/no-url-as-selector` was removed together with
# `selfHealingClick`. Ordinary `locator(...)` never accepts URLs, so this
# specific misuse is no longer possible in generated specs.


# ---------------------------------------------------------------------------
# PAGE_OBJECT
# ---------------------------------------------------------------------------
@_register("PAGE_OBJECT", "pageobject/defines-class",
           "Page object file must define a class.")
def _po_defines_class(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "class " not in text:
        return "Page object file must define a class."
    return None


@_register("PAGE_OBJECT", "pageobject/defines-constructor",
           "Page object class must define a constructor.")
def _po_defines_constructor(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "constructor(" not in text:
        return "Page object class must define constructor."
    return None


@_register("PAGE_OBJECT", "pageobject/no-selfHealingClick",
           "Page objects must use plain Playwright locators — selfHealingClick is removed.")
def _po_no_healing(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    if "selfHealingClick" in text:
        return (
            "Page object references `selfHealingClick` — that helper is no "
            "longer part of this codebase. Use `this.page.locator(SEL).click()` "
            "directly."
        )
    return None


@_register("PAGE_OBJECT", "pageobject/no-default-instance-export",
           "Page objects must export the class itself, not an instantiated singleton.")
def _po_no_default_instance(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    # Matches `export default new HomePage()` (with or without whitespace).
    if re.search(r"export\s+default\s+new\s+[A-Z]\w*\s*\(", text):
        return (
            "Page object uses `export default new <Class>()` — this exports an "
            "instance without a Page, so the step defs' `new HomePage(this.page)` "
            "would fail. Use a NAMED class export: "
            "`export class <Class> { constructor(private page: Page) {} … }` "
            "and drop the default export."
        )
    return None


@_register("PAGE_OBJECT", "pageobject/export-class",
           "Page-object class MUST be exported with `export class <Name>`.")
def _po_export_class(text: str, ctx: Dict[str, Any]) -> Optional[str]:
    # Only fire when a class exists but isn't exported. Skip if the file has
    # no `class` at all — the `pageobject/defines-class` rule handles that.
    if "class " not in text:
        return None
    if re.search(r"export\s+class\s+[A-Z]\w*\b", text):
        return None
    return (
        "Page-object class must be a NAMED export: change `class <Name> {…}` "
        "to `export class <Name> {…}` so step defs can `import { <Name> }` it."
    )


# ---------------------------------------------------------------------------
# Standard-template conformance
# ---------------------------------------------------------------------------
# Each artifact type has a canonical skeleton — the list of textual markers
# that MUST appear. Missing any is a template deviation. This is a summary
# check on top of the atomic rules above so the operator gets a clear
# "matches / diverges from standard template" signal instead of just a
# scattering of rule failures.
STANDARD_TEMPLATE = {
    "FEATURE": [
        ("template/feature-header",   r"^\s*Feature:\s+\S",   True,  "Feature: <title>"),
        ("template/feature-scenario", r"\bScenario(?:\s+Outline)?:\s+\S", True, "Scenario: <name>"),
        ("template/feature-given",    r"(?<![A-Za-z])Given\s+\S", True, "Given step"),
        ("template/feature-when",     r"(?<![A-Za-z])When\s+\S",  True, "When step"),
        ("template/feature-then",     r"(?<![A-Za-z])Then\s+\S",  True, "Then step"),
    ],
    "STEP_DEFINITIONS": [
        ("template/stepdefs-cucumber-import",
         r"import\s*\{[^}]*(Given|When|Then)[^}]*\}\s*from\s*['\"]@cucumber/cucumber['\"]",
         True, "import { Given, When, Then } from '@cucumber/cucumber'"),
        ("template/stepdefs-function-registration",
         r"(?<![A-Za-z@])(Given|When|Then)\s*\(\s*['\"`]",
         True, "Given('step text', async function () { … })"),
    ],
    "PAGE_OBJECT": [
        ("template/pageobject-page-import",
         r"import\s*\{[^}]*\bPage\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]",
         True, "import { Page } from '@playwright/test'"),
        ("template/pageobject-export-class",
         r"export\s+class\s+[A-Z]\w*\b",
         True, "export class <Name>Page { … }"),
        ("template/pageobject-constructor",
         r"constructor\s*\(",
         True, "constructor(private page: Page) {}"),
    ],
    "SPEC": [
        ("template/spec-playwright-import",
         r"import\s*\{[^}]*\btest\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]",
         True, "import { test, expect } from '@playwright/test'"),
        ("template/spec-has-expect",
         r"\bexpect\s*\(",
         True, "expect(...)"),
    ],
}


def _register_template_rules() -> None:
    """
    Turn each `STANDARD_TEMPLATE` entry into an actual `Rule`. Kept as a
    function so the module-level import order doesn't matter — this runs
    after every atomic rule above is defined.
    """
    for artifact_type, entries in STANDARD_TEMPLATE.items():
        for rule_name, pattern, required, human_shape in entries:
            compiled = re.compile(pattern, flags=re.MULTILINE)
            description = f"Standard template requires: {human_shape}"
            def _make(_compiled=compiled, _shape=human_shape, _rule=rule_name) -> CheckFn:
                def _check(text: str, ctx: Dict[str, Any]) -> Optional[str]:
                    if _compiled.search(text):
                        return None
                    return (
                        f"Diverges from standard template — expected {_shape!r}. "
                        f"See rule {_rule!r} for the exact pattern."
                    )
                return _check
            _REGISTRY[artifact_type].append(Rule(
                name=rule_name,
                description=description,
                check=_make(),
            ))


_register_template_rules()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
class ArtifactValidator:
    """Single entry point for validating any artifact."""

    def validate(self, artifact_type: str, path: str, content: str,
                 ctx: Optional[Dict[str, Any]] = None) -> ValidationResult:
        ctx = dict(ctx or {})
        ctx["_path"] = path
        ctx["_is_gherkin"] = (artifact_type == "FEATURE")

        errors: List[ValidationError] = []
        warnings: List[ValidationError] = []

        # Shared rules first, then per-type.
        rulesets = (_REGISTRY.get("*", []), _REGISTRY.get(artifact_type, []))
        for rules in rulesets:
            for rule in rules:
                try:
                    msg = rule.check(content or "", ctx)
                except Exception as exc:  # noqa: BLE001
                    # A rule crash is itself a validation problem — surface it.
                    errors.append(ValidationError(
                        rule=f"internal/{rule.name}",
                        message=f"Rule {rule.name!r} crashed: {exc}",
                    ))
                    continue
                if not msg:
                    continue
                entry = ValidationError(rule=rule.name, message=msg, severity=rule.severity)
                if rule.severity == "warning":
                    warnings.append(entry)
                else:
                    errors.append(entry)

        return ValidationResult(
            artifact_type=artifact_type,
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )


# Module-level convenience — most callers just want a one-liner.
DEFAULT_VALIDATOR = ArtifactValidator()


def validate_artifact(artifact_type: str, path: str, content: str,
                      ctx: Optional[Dict[str, Any]] = None) -> ValidationResult:
    return DEFAULT_VALIDATOR.validate(artifact_type, path, content, ctx)
