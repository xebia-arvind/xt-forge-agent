# Transform catalog

Every transform:

- Takes `(source, ctx)` for AST-level ops or `(content: string)` for pre-parse
- Returns `{ changed: bool, report?: { name, detail } }` — or `{ changed: false }` if it was a no-op
- Is **idempotent**: running twice produces the same output as running once
- Never throws; falls back to a no-op on any internal error

## Pre-parse (string-level)

- **`unescape-newlines`** — Convert literal `\n` / `\t` / `\r\n` character sequences (as opposed to real newlines) into real whitespace. LLMs sometimes emit `"…\n }); \n When(…"` as one long line with backslash-n characters; TypeScript rejects those as `TS1127 Invalid character`.
- **`flip-quotes`** — Fix `'[data-testid='foo']'` → `"[data-testid='foo']"`. The outer `'…'` is closed by the first inner `'`, TypeScript sees the rest as parse errors.

## Step-defs (AST)

- **`decorator-to-registration`** — Every `@Given('text', ...) async method(params) { body }` inside a class → module-level `Given('text', async function(params) { body });`
- **`strip-class-wrapper`** — Drop `export default class Steps { … }` — Cucumber-JS registers steps at module scope, not class methods.
- **`named-imports`** — `import HomePage from '.../HomePage'` → `import { HomePage } from '.../HomePage'` (page objects use `export class`, so default imports resolve to undefined at runtime).
- **`module-scope-new`** — Delete `const homePage = new HomePage(this.page);` at module scope (`this` is undefined there — file crashes on load).
- **`inject-instantiation`** — For every step body that references a variable stripped by the above rule, insert `const homePage = new HomePage(this.page);` at the top of the body.
- **`slug-injection`** — Rewrite `'../../tests/pages/generated/HomePage'` → `'../../../tests/pages/generated/<slug>/HomePage'` because slug-scoped materialize puts files one level deeper.
- **`and-but-remap`** — `And('text', ...)` / `But('text', ...)` → the previous Given/When/Then verb. `@cucumber/cucumber` v11 doesn't export And/But.
- **`cucumber-import`** — Rebuild the `import { … } from '@cucumber/cucumber'` line so it matches what's actually used (drops stale And/But, adds missing Given/When/Then).
- **`expect-import`** — Prepend `import { expect } from '@playwright/test';` if the body calls `expect(` without importing it.

## Page-objects (AST)

- **`page-object-exports`** — `class HomePage { … }` → `export class HomePage { … }`; drop `export default new HomePage()` (step defs need the class, not an instance).

## Shared (runs on both)

- **`dedupe-imports`** — Collapse identical `import { X } from '…'` lines.
