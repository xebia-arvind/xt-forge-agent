# XT-Forge TypeScript normalizer sidecar

Deterministic AST-based fixer for the two artifact types the LLM most often
malforms: **step-defs** and **page-objects**. Replaces the growing regex stack
in `views.py::_normalize_step_definitions` with a proper TypeScript parse via
[`ts-morph`](https://ts-morph.com).

## Contract

Called by Django as a one-shot subprocess. Input on **stdin**, output on **stdout**:

```json
// stdin
{
  "artifact_type": "STEP_DEFINITIONS",   // or "PAGE_OBJECT"
  "relative_path": "features/steps/foo-steps.ts",
  "content":       "import { … } from '@cucumber/cucumber'; …",
  "context": {
    "slug":              "blu-b2c",
    "known_page_objects": ["HomePage", "LoginPage"]
  }
}
```

```json
// stdout
{
  "ok": true,
  "content": "<rewritten source>",
  "errors":  [],
  "transformations": [
    { "name": "unescape-literal-newlines",    "detail": "converted 3 sequences" },
    { "name": "strip-decorator-class-wrapper", "detail": "moved 5 methods to module scope" },
    { "name": "dedupe-imports",                "detail": "removed 1 duplicate" }
  ]
}
```

`ok:false` means the file couldn't even be parsed after all rewrites. In that
case Django falls back to the legacy regex normalizer (env `TS_NORMALIZER_MODE=regex`).

## Install

```bash
cd ai-healer-django/flaky_healer/test_generation/ts_normalizer
npm install
```

## Smoke test

```bash
echo '{"artifact_type":"STEP_DEFINITIONS","relative_path":"foo.ts","content":"import { Given } from \"@cucumber/cucumber\";\nexport default class S {\n  @Given(\"x\") m() {}\n}"}' | node index.mjs
```

Expected: `ok: true`, and the emitted content contains `Given('x', ...)` at
module scope with no class wrapper.

## Transform list

See [`transforms/README.md`](transforms/README.md) for the full catalog.
Every transform is idempotent — running the sidecar N times converges after 1.
