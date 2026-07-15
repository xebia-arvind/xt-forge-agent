# Architecture

## 30-second summary

This repo is a **five-process system** for AI-assisted, self-healing E2E testing of carnival.com:

| # | Process | Lang | Port | What it does |
|---|---------|------|------|--------------|
| 1 | **Playwright tests** | TypeScript | — | Drive a real browser through the booking flow. |
| 2 | **`ai-healer-django/`** | Python / Django + DRF | 8000 | Heals broken selectors (SBERT + FAISS + LLM), generates tests, stores UI baselines, ingests test results. Also serves the operator dashboard (Phase 3). |
| 3 | **django-q2 worker** | Python | — | Background task runner; executes `npm run gen:testcases` and `npx playwright test` on behalf of the dashboard. |
| 4 | **Redis** | external | 6379 | Broker for django-q2. Fall back to the ORM broker with `Q_ORM_BROKER=true` for local dev. |
| 5 | **Ollama** | external | 11434 | Local LLM (`qwen2.5:7b`) called by Django for validation and test-generation. |
| ~~6~~ | ~~`streamlet-ui/`~~ | ~~Python / Streamlit~~ | ~~8501~~ | *Superseded by Phase 3 Django panels. Left in place for parallel operation during cut-over; removed once every panel is validated.* |

`wraper-healer/` is **TypeScript glue** that lives inside the Playwright project: it is what Playwright tests `import` to talk to Django (healing API, analytics ingestion, test-generation CLI, UI-knowledge crawler).

## System diagram

```
                ┌─────────────────────────────────────────────────┐
                │ Streamlit UI  (:8501)                           │
                │  Page 0 Jira  →  1 Config  →  2 Generate        │
                │       →  3 Review  →  4 Execute & Push          │
                └───────┬───────────────────────┬─────────────────┘
                        │ HTTP (REST)           │ subprocess
                        ▼                       ▼
                ┌─────────────────┐   ┌────────────────────────────┐
                │ Jira Cloud      │   │ npm scripts (Node, in cwd) │
                │ /rest/api/3/... │   │ gen:testcases / playwright │
                └─────────────────┘   └────────┬───────────────────┘
                                               │
                                               │ axios (HTTP)
                                               ▼
                ┌──────────────────────────────────────────────────┐
                │ Django ai-healer  (:8000, MySQL: ai_healer_service) │
                │   /auth/login/              JWT (SimpleJWT)      │
                │   /api/heal/                selector healing     │
                │   /api/heal/batch/                               │
                │   /test-generation/jobs/    LLM-driven gen       │
                │   /test-analytics/test-result/  result ingest    │
                │   /ui-knowledge/sync/       baseline crawls      │
                │   /ui-knowledge/change-status/                   │
                └────────────┬─────────────────────────────────────┘
                             │ HTTP
                             ▼
                ┌─────────────────────────────────────────────────┐
                │ Ollama  (:11434, qwen2.5:7b)                    │
                │   /api/generate  — validation + scenario gen    │
                └─────────────────────────────────────────────────┘
```

## Component responsibilities

### 1. Playwright tests (repo root)

- **Specs live in `tests/`.** The hand-written flow is `tests/carnivalBooking.spec______.ts` (the trailing underscores are intentional). Generated specs land under `tests/generated/<client_slug>/` and generated page objects under `tests/pages/generated/<client_slug>/` (Phase 1 multi-tenant layout — Option A). The materialized manifest records both the logical path (`tests/generated/foo.spec.ts`) and the on-disk path (`tests/generated/<slug>/foo.spec.ts`).
- **Page objects in `pages/`** — `HomePage`, `LoginPage`, `SearchPage`, `CruiseDetailsPage`, `CabinPage`, `CartPage`. They take a `Page` in their constructor and expose async action methods.
- **Custom fixtures in [fixtures/baseFixture.ts](../fixtures/baseFixture.ts)** — extend Playwright `test` with one fixture per page object plus a `healingReport` fixture (`auto: true`) that consumes any healing logs after each test and attaches them as a `healing-log` text attachment.
- **Self-healing entry points** — `HomePage.clickSignIn()` calls `selfHealingClick()` for the consent banner and sign-in button. Other page objects use raw Playwright locators. `CartPage` defines a `resilientClick()` wrapper but does not currently invoke it in the live flow.
- **Multi-user retry** — the booking spec catches the exported `CABIN_OFFER_CONTINUE_FAILED` constant from `CabinPage.ts`, calls `clearSession()`, and retries the whole flow with the second user from `test-data/user.ts`.

### 2. `wraper-healer/` — the bridge layer

| File | Lines | Role |
|---|---|---|
| `selfHealing.ts` | ~357 | `selfHealingClick(page, locator, failedSelector, testInfo, options)`. Fallback chain: original (3s timeout) → original retry (5s) → `POST /api/heal/` → click healed CSS → fallback to candidate XPath → if the chosen selector came from cache and fails, re-`POST /api/heal/` with `skip_cache: true`. |
| `baseTest.ts` | 113 | Extends Playwright `test`. `afterEach` hook bundles attachments + tracked failure context + step events into one payload and `POST`s to `/test-analytics/test-result/`. Honours `SAVE_ONLY_FAILED` env. |
| `failureContext.ts` | 111 | Per-test `Map<testId, FailureContext>`. `setFailureContext` keeps the **highest-severity** UI change level seen. `addStepEvent` appends an action/assertion/navigation event with status `PASSED | FAILED | HEALED`. |
| `healingReportLogger.ts` | 26 | Per-test string buffer. `appendHealingReportLog` writes; the `healingReport` fixture calls `consumeHealingReportLogs` and attaches the text to the HTML report. |
| `apiClient.ts` | 76 | Axios instance, `baseURL = HEALER_API_BASE_URL || http://127.0.0.1:8000/api`. `authenticatedPost<T>` injects `Authorization: Bearer …`, re-auths on 401, retries once on transient errors / 5xx. |
| `auth.ts` | 51 | `getAccessToken()` posts to `http://127.0.0.1:8000/auth/login/` (URL hardcoded). **Email, password, and `client_secret` are hardcoded literals** — see Security caveats. Token cached in-process. |
| `sendToDjango.ts` | 39 | Hardcoded `POST http://127.0.0.1:8000/test-analytics/test-result/` (no auth header). Called from `baseTest.ts`. |
| `healer.ts` | 61 | Type-only definitions of `HealResponse`, `HealerCandidate`, `HealerDebug`. |
| `runGenerationFromFile.mjs` | ~170 | CLI (`npm run gen:testcases`). Reads `wraper-healer/generation/feature_requests.json`, `POST`s each job to `/test-generation/jobs/`, prints job IDs. Approval + materialization are then manual via Django admin. |
| `syncUiKnowledge.mjs` | ~355 | CLI (`npm run sync:ui`). Spawns `crawlContext.mjs`, optionally classifies intents through Ollama (`INTENT_LLM_URL`, default `http://127.0.0.1:11434/api/generate`), then `POST`s each crawled route to `/ui-knowledge/sync/`. |
| `crawlContext.mjs` | ~316 | Playwright BFS crawler. Extracts interactables (selector hints ranked by stability: testid > id > aria-label > name > class > tag+text), forms, links, DOM hash, full-page screenshot. Respects `MAX_ROUTES`, `MAX_DEPTH`, `MAX_INTERACTABLES`. |
| `validateSelectors.mjs` | 112 | Standalone harness — launches a browser and checks each `--selector` against each `--url`. Not wired into the main flow. |

### 3. `ai-healer-django/` — backend

- **Apps installed** (`flaky_healer/settings.py:56–72`): `clients`, `curertestai` (healing engine), `test_analytics`, `test_generation`, `ui_knowledge`, `abstract` (common audit-fields base model), plus `rest_framework`, `import_export`, `admin_interface`, `colorfield`, and an `auth` app.
- **Database is MySQL**, not SQLite: `ENGINE=django.db.backends.mysql, NAME=ai_healer_service, USER=root, HOST=localhost, PORT=3306` (settings.py:117–125). The SQLite block above it is commented out, and `ai_healer_service.sql` in the project root is a 27 MB seed dump.
- **Healing pipeline** (`curertestai/views.py`, `matching_engine.py`, `validation_engine.py`):
  1. Cache lookup (history of past successful heals, gated by `USE_HEALING_CACHE`, `MAX_AGE_DAYS=14`, `MIN_CONFIDENCE=0.30`).
  2. DOM extraction → fingerprint (signature tokens; SHA-256 of sorted tokens).
  3. Semantic match: `sentence-transformers` (`all-MiniLM-L6-v2`) → FAISS `IndexFlatIP`. Fallback to scikit-learn TF-IDF if SBERT fails.
  4. Multi-stage scoring: `0.75·semantic + 0.15·history + 0.10·LLM` *(retrieval boost removed in Phase 2; was 0.08·retrieval_jaccard backed by `DomSnapshot`)*.
  5. Intent policy gate from `config/intent_policies.json` (blocked-pattern / allow-hint pairs per intent_key, e.g. `add_to_cart` blocks `cart-icon`-style selectors).
  6. Optional LLM validation (Ollama, `LLM_VALIDATION_URL=http://127.0.0.1:11434/api/generate`, `qwen2.5:7b`, 10s timeout). Gated by `USE_LLM_VALIDATION`.
  7. UI-change classification (`UNCHANGED | MINOR_CHANGE | MAJOR_CHANGE | ELEMENT_REMOVED`).
- **Test generation pipeline** (`test_generation/generation_service.py`): same Ollama instance, env vars `TEST_GEN_LLM_URL` / `TEST_GEN_LLM_MODEL`. Creates `GenerationJob` (status flow `DRAFTING → DRAFT_READY → APPROVED → MATERIALIZED`), spawns `GenerationScenario` rows, then `GeneratedArtifact` rows for each `.spec`/`.po` file.
- **Auth**: SimpleJWT. `POST /auth/login/` expects `{email, password, client_secret}`, returns `{access, refresh}` with `client_id` + `email` claims. **Phase 1 (multi-tenant) changes** — all of these now require JWT auth and stamp the caller's tenant on every row: `/api/heal/`, `/api/heal/batch/`, `/test-analytics/test-result/`, `/test-generation/*`, `/ui-knowledge/sync/`, `/ui-knowledge/change-status/`. The dashboard summary/detail endpoints still accept session auth from a logged-in browser user. `ClientResolutionMiddleware` (clients/middleware.py) reads `request.auth["client_id"]` after DRF auth runs and sets `request.client`; views use `clients.mixins.require_client(request)` to enforce.
- **Security caveats from settings.py**: `DEBUG = True`, `ALLOWED_HOSTS = []`, `SECRET_KEY` hardcoded. Treat the Django instance as local-dev only.

### 4. `streamlet-ui/` — the operator UI

- **Entry point** `app.py` renders the splash + 5-card workflow and a sidebar with a live `Backend Online/Offline` badge (1-line healthcheck against `/test-generation/jobs/`).
- **Multi-page nav** uses Streamlit's `pages/` convention. `utils/ui_components.py:render_sidebar_navigation` gates pages on `st.session_state["completed_steps"]`.
- **Pages**:
  - `0_📋_Jira_Worklist.py` — Jira search via `POST {jira_url}/rest/api/3/search/jql` (HTTPBasicAuth), pick a ticket, optionally fire `start_autonomous_flow`.
  - `1_🧪_Feature_Config.py` — read/edit `wraper-healer/generation/feature_requests.json` in-place.
  - `2_⚡_Generate.py` — shells out to `npm run gen:testcases` and streams stdout.
  - `3_👁️_Review.py` — `GET /test-generation/jobs/`, `POST .../approve/`, `POST .../materialize/{allow_overwrite:true}`, preview generated files from `tests/generated/` and `tests/pages/generated/`.
  - `4_▶️_Execute.py` — shells out to `npx playwright test [file] [-g <pattern>] --workers=N`, parses results, then `POST`s a Jira comment (ADF) and attachments to `/rest/api/3/issue/{key}/comment` and `/rest/api/3/issue/{key}/attachments`.
- **Autonomous flow** (`utils/autonomous_flow.py`): a 5-state linear machine `validate → generate → review_materialize → execute → push`, each stage marked `pending / running / success / failed`. Pages check `is_autonomous_active() && get_current_stage() == "<expected>"` and gate auto-advance with short holds (8–15 s) for human observation.

## End-to-end flow (autonomous mode)

```
User picks a Jira ticket on Page 0
  │  start_autonomous_flow(jira_id, summary)
  ▼
Page 1  ── save feature_requests.json (in-place edit)
  ▼
Page 2  ── subprocess: `npm run gen:testcases`
            └── node wraper-healer/runGenerationFromFile.mjs
                  └── POST /test-generation/jobs/  (×N jobs)
                        └── Ollama qwen2.5:7b
                              └── GenerationJob status: DRAFT_READY
  ▼
Page 3  ── POST .../approve/   →  POST .../materialize/
                                    └── Django writes tests/generated/*.spec.ts
                                        and tests/pages/generated/*.ts
  ▼
Page 4  ── subprocess: `npx playwright test ...`
            └── selfHealingClick on failure:
                  POST /api/heal/   (axios, JWT)
                    └── SBERT+FAISS + intent policies + LLM validation
                  HealResponse → click healed selector
            └── afterEach: POST /test-analytics/test-result/  (full payload)
  ▼
Page 4 (push) ── POST {jira}/rest/api/3/issue/{key}/comment    (ADF body)
                  POST .../attachments                          (spec + logs)
```

## Data lifecycle of a single failed click (deep cut)

```
1. tests/...spec.ts runs a step inside test.step()
2. HomePage.clickSignIn() → selfHealingClick(page, locator, ".signIn",
                                              testInfo,
                                              {use_of_selector, selector_type, intent_key})
3. selfHealing.ts → setFailureContext({failedSelector, pageUrl, healingAttempted:false})
4. locator.click({timeout:3000})  ❌  → retry({timeout:5000})  ❌
5. setFailureContext({healingAttempted:true, healingOutcome:"FAILED", rootCause})
6. authenticatedPost("/heal/", {
       test_name, failed_selector, html, screenshot (base64),
       page_url, use_of_selector, selector_type, intent_key
   })
7. Django curertestai/views.HealAPIView:
     a. HealingCache lookup (page_url + use_of_selector + intent_key + failed_selector)
     b. DOMExtractor → MatchingEngine.embed(query) → faiss.search(top-k)
     c. select_validated_candidate():
          base_semantic + history boost + retrieval boost + LLM score
        intent_policies gate (rule-based)
     d. detect_ui_change_for_healing()  → UI change level
     e. Persists HealerRequest + SuggestedSelector[] + DomSnapshot
     f. Returns HealResponse { chosen, candidates[], validation_status,
                               debug { cache_hit, cache_source_id, engine, ... },
                               ui_change_level }
8. selfHealing.ts → clickUsingResolvedSelector(chosen) (CSS only if count===1, else XPath)
9. If success: addStepEvent({status:"HEALED"}); setFailureContext(success metadata).
   If cached selector failed: POST /heal/ again with skip_cache:true; retry.
10. afterEach (baseTest.ts) → sendToDjango(payload) → /test-analytics/test-result/
    Django classifier.classify_failure() tags failure_category and persists TestCaseResult.
11. healingReport fixture flushes the per-test log buffer as a 'healing-log' attachment.
```

## Where state lives

| Stored | Where | Notes |
|---|---|---|
| Generation requests | `wraper-healer/generation/feature_requests.json` | Hand-edited or written by Streamlit Page 1. |
| Generated specs / page objects | `tests/generated/`, `tests/pages/generated/` | Currently empty; populated by Django's `/materialize/`. `pages/generated/` does not exist yet. |
| Crawled UI baselines | Django `UIPage`, `UIRouteSnapshot`, `UIElement`, `UIScreenshot`, `UIChangeLog` | MySQL. Populated by `npm run sync:ui`. |
| Healing history | Django `HealerRequest`, `SuggestedSelector`, `DomSnapshot` | Used by future heals as cache + retrieval boost. |
| Test runs / per-case results | Django `TestRun`, `TestCaseResult` | Populated by every `afterEach`. |
| Test reports (HTML) | `playwright-report/${RUN_ID}/` | Local files; `RUN_ID` defaults to `run_${Date.now()}` if env unset. |
| Trace / video / screenshot | `test-results/` | Created by Playwright on failure only. |
| Streamlit ↔ user session | `st.session_state` | Includes hardcoded Jira creds (see Security caveats). |
| JWT (TS-side) | In-memory in `wraper-healer/auth.ts` | One token per Playwright process. |

## Configuration surface (every env var that actually does something)

### Playwright / Node side

| Var | Default | Read at | Effect |
|---|---|---|---|
| `BASE_URL` | — | `playwright.config.ts:19` | Playwright `use.baseURL`. |
| `HEADLESS` | (false) | `playwright.config.ts:20` | Truthy only when string === `"true"`. |
| `TIMEOUT` | 60000 | `playwright.config.ts:11` | Whole-test timeout (ms). |
| `RUN_ID` | `run_${Date.now()}` | `playwright.config.ts:7` | HTML report sub-folder + payload `run_id`. |
| `CI` | — | `playwright.config.ts:6` | Forces workers=1. |
| `BUILD_ID` / `GITHUB_SHA` / `CI_COMMIT_SHA` / `BUILD_COMMIT` | — | `wraper-healer/baseTest.ts` | Build identifier on every analytics payload. |
| `SAVE_ONLY_FAILED` | (false) | `wraper-healer/baseTest.ts` | If `"true"`, only failed tests POSTed to analytics. |
| `HEALER_API_BASE_URL` | `http://127.0.0.1:8000/api` | `wraper-healer/apiClient.ts` | Healer base URL. |
| `HEALER_API_TIMEOUT_MS` | 60000 | `wraper-healer/apiClient.ts` | Axios timeout. |
| `BASE_URL`, `BACKEND_URL`, `MAX_ROUTES`, `MAX_DEPTH`, `MAX_INTERACTABLES`, `FEATURE_NAME`, `SNAPSHOT_TYPE`, `UI_SCREENSHOT_DIR`, `SEED_URLS`, `INTENT_LLM_URL`, `INTENT_LLM_MODEL` | various | `wraper-healer/syncUiKnowledge.mjs` | UI knowledge crawl controls. |

### Streamlit side

| Var | Default | Notes |
|---|---|---|
| `BACKEND_URL` | `http://127.0.0.1:8000` | `streamlet-ui/utils/api_client.py:8`. |

### Django side

| Var | Default | Notes |
|---|---|---|
| `USE_HEALING_CACHE` | true | `curertestai/views.py`. |
| `MAX_AGE_DAYS` | 14 | Healing cache freshness window. |
| `MIN_CONFIDENCE` | 0.30 | Cache score floor. |
| `USE_LLM_VALIDATION` | — | Gates Ollama validation. |
| `LLM_VALIDATION_URL` | `http://127.0.0.1:11434/api/generate` | Ollama endpoint. |
| `LLM_VALIDATION_MODEL` | `qwen2.5:7b` | Model id. |
| `LLM_VALIDATION_TIMEOUT_SECONDS` | 10 | |
| `USE_TEST_GEN` | — | Gates LLM-driven scenario generation. |
| `TEST_GEN_LLM_URL` / `TEST_GEN_LLM_MODEL` / `TEST_GEN_TIMEOUT_SECONDS` | as above + 120s | Generation LLM. |

## Security caveats (call these out before any non-local use)

- `wraper-healer/auth.ts` ships `email`, `password`, and `client_secret` as **literals** in source. Any `npm test` run authenticates as that user against `http://127.0.0.1:8000/auth/login/`.
- `streamlet-ui/app.py` ships a **Jira API token** as a `st.session_state` default. Anyone running the Streamlit app inherits those credentials. The token is also displayed in a `text_input` on Page 0.
- `ai-healer-django/flaky_healer/flaky_healer/settings.py`: `DEBUG=True`, `ALLOWED_HOSTS=[]`, hardcoded `SECRET_KEY`. Not safe to expose beyond localhost.
- Most Django endpoints accept unauthenticated POST. The JWT gate only applies to `/api/heal/` and `/api/heal/batch/`.

## See also

- [WORKFLOWS.md](WORKFLOWS.md) — every operator-facing workflow with step-by-step instructions.
- [DATA_FLOW.md](DATA_FLOW.md) — sequence diagrams for the three primary flows.
- [API_REFERENCE.md](API_REFERENCE.md) — every Django endpoint, request/response shape, and DB writes.
- [../CLAUDE.md](../CLAUDE.md) — orientation for future Claude Code sessions.
