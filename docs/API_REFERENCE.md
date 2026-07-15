# Django API Reference

Base URL: `http://127.0.0.1:8000` (Django dev server, MySQL backend `ai_healer_service`).

Auth notes:
- Only `/api/heal/` and `/api/heal/batch/` enforce JWT (`IsAuthenticated`).
- `/test-analytics/test-result/`, `/test-generation/*`, `/ui-knowledge/*` are open (no auth).
- `/test-analytics/dashboard/` uses Django session auth (`LoginRequiredMixin`).

URL mounts (`flaky_healer/urls.py`):

| Prefix | Module |
|---|---|
| `/admin/` | Django admin |
| `/auth/` | `auth.urls` |
| `/api/` | `curertestai.urls` (healing) |
| `/test-analytics/` | `test_analytics.urls` |
| `/test-generation/` | `test_generation.urls` |
| `/ui-knowledge/` | `ui_knowledge.urls` |
| `/uploads/` | media files (DEBUG only) |

---

## Auth

### `POST /auth/login/`

Login as a (User, Client) pair, get JWT tokens.

Request:

```json
{ "email": "...", "password": "...", "client_secret": "<uuid>" }
```

Response:

```json
{
  "tokens": { "access": "<jwt>", "refresh": "<jwt>" },
  "user":   { "id": 1, "email": "..." },
  "client": { "secret_key": "<uuid>", "clientname": "..." }
}
```

JWT claims carry `client_id` (UUID) and `email`. Both `HealAPIView` and `BatchHealAPIView` resolve the current `User` and `Clients` row from these claims.

---

## Healing — `curertestai`

### `POST /api/heal/`

Heal one broken selector. **Requires JWT.**

Request (`HealRequestSerializer`):

```jsonc
{
  "failed_selector": "#loginButton",
  "html": "<html>...</html>",           // full DOM at failure
  "semantic_dom": "<...>",              // optional pre-extracted form
  "use_of_selector": "click sign in",   // human-readable intent
  "page_url": "https://www.carnival.com/",
  "intent_key": "homepage_signin_cta",  // optional, drives intent_policies
  "selector_type": "css",               // default "css"
  "screenshot": "<base64>",             // optional
  "test_name": "...",                   // optional, for provenance
  "skip_cache": false                   // bypass HealingCache on retry
}
```

Response (`HealResponseSerializer`):

```jsonc
{
  "chosen": "button[aria-label='Sign In']",     // CSS selector
  "candidates": [
    { "selector": "...", "xpath": "...", "score": 0.81,
      "base_score": 0.72, "attribute_score": 0.30,
      "tag": "button", "text": "Sign In" }
  ],
  "validation_status": "VALID",                  // or "NO_SAFE_MATCH"
  "validation_reason": "...",
  "ui_change_level": "MINOR_CHANGE",
  "debug": {
    "engine": "sbert+faiss",                     // or "history_cache"
    "cache_hit": false,
    "cache_source_id": null,
    "dom_fingerprint": "<sha256>",
    "history_assisted": true,
    "history_hits": 4,
    "llm_used": true
  }
}
```

Scoring composition (`validation_engine.select_validated_candidate`) — Phase 2 weights:

```
final_score = 0.75·semantic + 0.15·history_norm + 0.10·llm
```

(The 0.08·retrieval term backed by `DomSnapshot` was removed in Phase 2 — weights re-normalized.)

`best_score < 0.10 → validation_status = "NO_SAFE_MATCH"`.

DB writes (per request):
- `HealerRequest` (the entire request + outcome)
- up to 5 × `SuggestedSelector` (top-k candidates with scores)

`HealResponse` still includes `batch_id` and `retrieval_assisted`/`retrieval_hits`/`retrieved_versions` for client back-compat. They are always `0` / `false` / `[]` now.

### `POST /api/heal/batch/`

Batch version of `/heal/`. Body is `{requests: [<HealRequest>, ...]}`. Persists one `HealerRequest` row per item; the batch aggregate (totals, processing time) is computed in-memory and **not** persisted — `HealerRequestBatch` was removed in Phase 2. Response shape (`id`, `results`, `total_processed`, `total_succeeded`, `total_failed`, `processing_time_ms`) is unchanged, but `id` is always `0`.

---

## Test analytics — `test_analytics`

### `POST /test-analytics/test-result/`

Ingest a single Playwright test result (`PlaywrightResultAPIView`). **No auth.** Called from `wraper-healer/sendToDjango.ts` inside every `afterEach`.

Request (`TestCaseResultSerializer`) — see `docs/DATA_FLOW.md` for the full shape Playwright actually sends. Key fields:

```jsonc
{
  "run_id": "1234", "build_id": "...", "environment": "staging",
  "run_execution_time": 23456,
  "test_name": "...", "status": "FAILED|PASSED|SKIPPED",
  "error_message": "...", "stack_trace": "...",
  "page_url": "...", "failed_selector": "...", "failure_reason": "...",
  "healing_attempted": true, "healing_outcome": "SUCCESS|FAILED|NOT_ATTEMPTED",
  "healed_selector": "...", "healing_confidence": 0.81,
  "validation_status": "VALID", "ui_change_level": "MINOR_CHANGE",
  "history_assisted": true, "history_hits": 4,
  "cache_hit": true, "cache_fallback_to_fresh": false,
  "root_cause": "...",
  "step_events": [{...}],
  "html": "...", "screenshot_path": "...", "video_path": "...", "trace_path": "..."
}
```

Server-side: `classifier.classify_failure()` tags `failure_category` (FLAKY / UNSTABLE / ENV_ISSUE / HEALING_FALSE_POSITIVE / …). DB writes: 1 × `TestRun` (upsert on `run_id`), 1 × `TestCaseResult`.

### `GET /test-analytics/test-result/<int:id>/`

Fetch a single result.

### `GET /test-analytics/summary/`

Aggregated dashboard data. Optional query params: `run_id`, `build_id`, `environment`. Returns totals, failure breakdown, healing effectiveness, cache metrics, generation job stats.

### `GET|POST /test-analytics/dashboard/`

HTML dashboard. Requires Django session login (`/test-analytics/login/` & `/test-analytics/logout/`).

---

## Test generation — `test_generation`

### `GET /test-generation/jobs/`

List `GenerationJob` records.

### `POST /test-generation/jobs/`

Create a new generation job and kick off LLM drafting (`generate_job_draft()`, Ollama `qwen2.5:7b` via `TEST_GEN_LLM_URL`). Returns the job immediately with status `DRAFTING`; LLM work runs synchronously in the request handler until status becomes `DRAFT_READY` (or `FAILED`).

Request:

```jsonc
{
  "feature_name": "XX-94: Carnival User Login via Homepage Popup",
  "feature_description": "...",
  "seed_urls": ["/", "/login"],
  "coverage_mode": "SMOKE_NEGATIVE",
  "max_scenarios": 2,
  "max_routes": 5,
  "base_url": "https://www.carnival.com/",
  "intent_hints": ["...", "..."],
  "created_by": "manual-file-runner",
  "manual_scenarios": [
    {
      "title": "...",
      "type": "SMOKE",
      "steps": [{"action": "...", "selector": "...", "intent_key": "..."}],
      "assertions": ["..."]
    }
  ]
}
```

Response: full `GenerationJob` with `scenarios[]` and `artifacts[]` (each artifact has `content_draft`).

### `GET /test-generation/jobs/<uuid:job_id>/`

Fetch a single job (scenarios + artifacts).

### `POST /test-generation/jobs/<uuid:job_id>/approve/`

Body: `{ "approved_by": "Streamlit UI", "notes": "Approved via Streamlit", "selected_scenarios": ["<scenario_id>", ...] }` (selected scenarios optional; defaults to all). Moves `job_status → APPROVED`.

### `POST /test-generation/jobs/<uuid:job_id>/reject/`

Body: `{ "rejected_reason": "..." }`. Moves `job_status → REJECTED`.

### `POST /test-generation/jobs/<uuid:job_id>/materialize/`

Body: `{ "allow_overwrite": true }`. For each selected `GeneratedArtifact`, writes `content_final` to disk relative to the repo root and seals the row with checksum + validation. Moves `job_status → MATERIALIZED`. Returns the manifest:

```jsonc
{
  "manifest": [
    {"relative_path": "tests/generated/login.spec.ts", "artifact_type": "SPEC", "checksum": "..."},
    {"relative_path": "tests/pages/generated/LoginPage.ts", "artifact_type": "PAGE_OBJECT", "checksum": "..."}
  ]
}
```

### `POST /test-generation/jobs/<uuid:job_id>/artifacts/update/`

Body: `{ "relative_path": "...", "content": "..." }`. Lets the operator edit a draft before materialization.

### `POST /test-generation/jobs/<uuid:job_id>/link-run/`

Body: `{ "test_run_id": <id>, "notes": "..." }`. Creates a `GenerationExecutionLink` joining the job to a `TestRun` so analytics can attribute regressions to the generating job.

---

## UI knowledge — `ui_knowledge`

### `POST /ui-knowledge/sync/`

Upsert a UI snapshot for one route. **No auth.** Called per route by `wraper-healer/syncUiKnowledge.mjs`.

Request:

```jsonc
{
  "route": "/checkout",
  "title": "Checkout",
  "feature_name": "Checkout Flow",
  "snapshot_type": "BASELINE",         // or "NEW_STRUCTURE"
  "dom_hash": "sha256:...",
  "snapshot_json": { /* full crawl payload */ },
  "screenshot_path": "test-results/ui-crawl-screenshots/003_checkout.png",
  "elements": [
    {
      "selector": "button[aria-label='Add to cart']",
      "tag": "button", "role": "button", "text": "Add to cart",
      "test_id": "", "element_id": "", "intent_key": "add_to_cart"
    }
  ]
}
```

Behaviour: upserts `UIPage` by `route`, creates a new `UIRouteSnapshot` (auto-incrementing `version`), persists `UIElement[]` and `UIScreenshot`. If both BASELINE and NEW_STRUCTURE exist for the page, runs `compare_snapshots()` and writes a `UIChangeLog` with `added_selectors[]` / `removed_selectors[]` and a classification of `NO_CHANGE | MINOR | STRUCTURAL`.

### `GET /ui-knowledge/change-status/`

Query params: `route` (required), `failed_selector`, `use_of_selector`. Returns the baseline + current snapshots, the most recent `UIChangeLog`, and a `detect_ui_change_for_healing()` verdict (`ui_change_level`). The healing pipeline calls the same service inline (not via HTTP) when it processes `/api/heal/`.

---

## Models cheat-sheet

Every business model inherits `abstract.Common`: `created_on`, `last_modified`, `status` (a/i), `is_deleted`, `deleted_on`.

### `curertestai`
- **HealerRequest** — `failed_selector`, `html`, `use_of_selector`, `url`, `healed_selector`, `confidence`, `validation_status`, `dom_fingerprint`, `intent_key`, `candidate_snapshot` (JSON), `ui_change_level`, history flags, cache flags. `batch_id` is a plain integer kept for client back-compat (always `0` after Phase 2).
- **SuggestedSelector** — top-5 candidates per request: `selector`, `xpath`, `score`, `base_score`, `attribute_score`, `tag`, `text`.
- *(Phase 2 removed `DomSnapshot` and `HealerRequestBatch`. The healing cache and history boost remain on `HealerRequest` itself.)*

### `test_analytics`
- **TestRun** — `run_id` (unique), `environment`, `build_id`, `execution_time`.
- **TestCaseResult** — one row per test invocation, fields exactly matching the analytics payload above plus `failure_category`, optional `embedding` (reserved).

### `test_generation`
- **GenerationJob** — `job_id` (UUID), feature inputs (`feature_name`, `feature_description`, `seed_urls`, `coverage_mode`, `max_scenarios`, `max_routes`, `intent_hints`), LLM config (`llm_model`, `llm_temperature`), state (`job_status`), outputs (`feature_summary`, `crawl_summary`, `validation_summary`, `materialized_manifest`).
- **GenerationScenario** — `(job, scenario_id)` unique, `title`, `scenario_type`, `priority`, `preconditions` / `steps` / `expected_assertions` (JSON), `selected_for_materialization`.
- **GeneratedArtifact** — `(job, relative_path)` unique, `artifact_type` (`PAGE_OBJECT|SPEC`), `content_draft`, `content_final`, `checksum`, `validation_status`, `validation_errors`, `warnings`.
- **GenerationExecutionLink** — FK to `GenerationJob` + FK to `TestRun`.

### `ui_knowledge`
- **UIPage** — `route` unique, `title`, `feature_name`.
- **UIRouteSnapshot** — versioned per page; `snapshot_type` (BASELINE|NEW_STRUCTURE), `is_current`, `dom_hash`, `snapshot_json`.
- **UIElement** — per snapshot, `selector`, `tag`, `role`, `text`, `test_id`, `element_id`, `intent_key`, `stability_score`.
- **UIScreenshot** — `image_path`, `viewport`, `device`.
- **UIChangeLog** — comparison between two snapshots: `change_type` (NO_CHANGE|MINOR|STRUCTURAL), `added_selectors`, `removed_selectors`.

### `clients` / `auth`
- **Clients** — `secret_key` (UUID PK), `clientname`, `client_logo` (`uploads/clientlogo/…`).
- **UserClient** — OneToOne(`User`) ↔ ManyToMany(`Clients`). The JWT login validates this bridge.

---

## Headers / quirks worth remembering

- The healer client (`wraper-healer/apiClient.ts`) sets `baseURL` to `…/api`, so `POST /api/heal/` is reached as `POST /heal/` from code.
- `sendToDjango.ts` hardcodes the full URL `http://127.0.0.1:8000/test-analytics/test-result/` and does **not** send an `Authorization` header — the endpoint must remain auth-free or analytics ingest breaks.
- Jira attachment upload requires `X-Atlassian-Token: no-check` plus multipart body (`streamlet-ui/utils/api_client.py`).
- Streamlit `BACKEND_URL` defaults to `http://127.0.0.1:8000` and can be overridden via env. Page 0's Jira creds are read from `st.session_state` (defaults are baked into `app.py`).
