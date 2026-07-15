# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An AI-assisted Playwright E2E framework targeting carnival.com booking flows. It is **three cooperating processes**, not a single test project:

1. **Playwright tests** (TypeScript, repo root) — the actual browser automation.
2. **`ai-healer-django/`** — Django + DRF backend (port 8000). Stores UI-knowledge, runs ML-based selector healing, and generates test code via LLM.
3. **`streamlet-ui/`** — Streamlit dashboard (port 8501) that drives the end-to-end workflow: pull Jira tickets → configure features → generate tests → review → execute.

The `wraper-healer/` directory is the bridge: it contains the self-healing runtime that Playwright tests import, plus the Node CLIs (`runGenerationFromFile.mjs`, `syncUiKnowledge.mjs`) the Streamlit UI shells out to.

## Common commands

Playwright (run from repo root):

```bash
npm test                 # all tests, headless
npm run test:headed      # show the browser
npm run test:login       # only the login-tagged spec ("Carnival Login Feature")
npm run report           # open last HTML report

npm run gen:testcases    # node wraper-healer/runGenerationFromFile.mjs (POSTs to Django)
npm run sync:ui          # node wraper-healer/syncUiKnowledge.mjs
```

Run a single test by title:

```bash
npx playwright test -g "Validate Carnival"
```

Django backend (separate venv):

```bash
cd ai-healer-django/flaky_healer
python manage.py runserver   # http://127.0.0.1:8000
```

Streamlit UI + Django startup helper:

```bash
bash start_apps.command       # kills :8501, picks a venv, installs streamlet-ui/requirements.txt, launches Streamlit
```

Environment is read from `.env` at the repo root: `BASE_URL`, `HEADLESS`, `TIMEOUT`, `RUN_ID`. `playwright.config.ts` consumes these directly — there is no separate config layer.

## Architecture and conventions

### Test layout (Playwright side)

- `tests/` — specs. The main flow lives in `tests/carnivalBooking.spec______.ts` (the trailing underscores are intentional, do not rename).
- `pages/` — page objects. Six classes (`HomePage`, `LoginPage`, `SearchPage`, `CruiseDetailsPage`, `CabinPage`, `CartPage`). Each takes a `Page` in its constructor, defines locators as fields, and exposes async action methods. Reuse these — do not re-locate elements inside specs.
- `fixtures/baseFixture.ts` — extends Playwright `test` with one fixture per page object plus a `healingReport` fixture that auto-attaches healing logs to the test result. **Specs must import `test` from this file, not from `@playwright/test` directly.**
- `test-data/user.ts` — credentials. The `email`/`password` arrays hold multiple users for the fallback-retry pattern (see below).

### Self-healing runtime

`wraper-healer/` plugs into tests via:

- `selfHealing.ts` — wraps locator interactions; on failure it captures context (selector, DOM, screenshot) and asks Django for a repaired selector.
- `baseTest.ts` — `afterEach` hook that ships failure payloads to Django via `sendToDjango.ts` / `apiClient.ts`.
- `failureContext.ts`, `healingReportLogger.ts` — accumulate per-test healing events that the `healingReport` fixture flushes as attachments.

When a page-object action needs to be resilient, use the existing `selfHealingClick` wrapper (see `HomePage.ts`, `CartPage.ts`) rather than introducing new try/catch blocks.

### Multi-user fallback pattern

The booking spec catches the exported `CABIN_OFFER_CONTINUE_FAILED` constant from `CabinPage.ts`, calls `clearSession()`, and retries the flow with the next entry in `test-data/user.ts`. When adding flows that can hit transient site-side rejections, follow this pattern instead of marking the test flaky.

### Generation / sync flow (Streamlit → Django → Playwright)

1. Streamlit page `2_⚡_Generate.py` shells out to `npm run gen:testcases`.
2. `wraper-healer/runGenerationFromFile.mjs` reads `wraper-healer/generation/feature_requests.json` and POSTs to Django `/test-generation/jobs/`.
3. After review (Streamlit page 3), Django materializes generated specs/page-objects under `tests/generated/` and `pages/generated/`.
4. Streamlit page `4_▶️_Execute.py` runs `npm test` and pushes results back to Jira.

`streamlet-ui/utils/api_client.py` hardcodes `http://127.0.0.1:8000` — if the Django host changes, update it there.

## Deep documentation

For anything beyond the one-pager below, read [docs/](docs/):

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the four processes, the system diagram, all env vars, security caveats.
- [docs/DATA_FLOW.md](docs/DATA_FLOW.md) — sequence diagrams for healing / generation / UI sync / Jira push.
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) — every Django endpoint and the data model.
- [docs/WORKFLOWS.md](docs/WORKFLOWS.md) — how to bring everything up, run, heal, generate, push.

## Things to know before editing

- TypeScript is `strict` with `module: commonjs` (`tsconfig.json`); use CommonJS-friendly imports.
- Reports are written under `playwright-report/` keyed by `RUN_ID`; do not commit these or `test-results/`.
- `pages_bkp/` and `pages.zip` are stale snapshots — ignore them; the live page objects are in `pages/`.
- There is a `.env` at the repo root with real-looking URLs; treat it as the source of truth for environment defaults but never put secrets in it.
