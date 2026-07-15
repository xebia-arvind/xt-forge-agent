# Upgrade Plan

**Status:** draft — to be refined and executed in phases later.
**Owner:** Arvind.
**Created:** 2026-06-29.

This plan covers four upgrades, grouped by area. Each item lists current state (cited to file:line), the proposed change, blast radius, and open questions.

For background on the current architecture, see [ARCHITECTURE.md](ARCHITECTURE.md), [API_REFERENCE.md](API_REFERENCE.md), [DATA_FLOW.md](DATA_FLOW.md).

---

## Phase 1 — Multi-tenancy: every module scoped by `client_id`  ✅ done (2026-06-30)

Implemented (1.2 through 1.5):
- **`Clients.slug`** added (auto-derived from `clientname`); used for per-client materialization dirs.
- **Client FK added** to `TestRun`, `TestCaseResult` (`test_analytics`), `GenerationJob` (`test_generation`), `UIPage` (`ui_knowledge`). All nullable to permit backfill.
- **`UIPage` uniqueness** changed from `(route,)` to `(client, route)` — two tenants can now both own `/login`.
- **`TestRun` uniqueness** changed from `run_id` unique to `(client, run_id)` unique — tenants reuse run IDs freely.
- **Backfill data migration**: `legacy` `Clients` row created automatically; every pre-Phase-1 row assigned to it. See `clients/migrations/0004_clients_slug.py` and per-app `0002_client_scope.py`.
- **`ClientResolutionMiddleware`** + **`ClientScopedQuerysetMixin`** added in `clients/` and registered after DRF auth runs. `request.client` is the resolved tenant on every authenticated request.
- **Views scoped**:
  - `test_analytics`: ingest endpoint now requires JWT (`PlaywrightResultAPIView`) and stamps `client` on `TestRun` / `TestCaseResult`. Dashboard summary/detail scope by the logged-in user's `UserClient` memberships.
  - `test_generation`: all 7 endpoints inherit `_ClientScopedAPIView` (JWT or session). `GenerationJob` rows are created with `client`; queries filter by `client`.
  - `ui_knowledge`: snapshot create + change-status views scope by `client`. `detect_ui_change_for_healing` gained an optional `client=` kwarg.
  - `curertestai`: `_process_heal_request` and `_detect_ui_change_level` thread the resolved `client` into `detect_ui_change_for_healing`.
- **Per-client directories — Option A.** Materialization writes to `tests/generated/<slug>/...` and `tests/pages/generated/<slug>/...`. `materialize_job(client_slug=...)` keeps the LLM's canonical artifact paths but adds the slug at disk-write time. The materialized manifest records both `path` (on-disk) and `logical_path`.
- **UI-change pre-flight (1.5)** — `_ui_change_preflight(job)` runs at the top of `generate_job_draft`, checks each seed URL for the job's tenant, and appends `MAJOR_CHANGE` / `ELEMENT_REMOVED` warnings to `job.llm_notes` (visible to the operator on the Review page).
- **TS client (`wraper-healer/`)**:
  - `sendToDjango.ts` now uses `getAccessToken()` → adds `Authorization: Bearer …`. One-shot retry on 401.
  - `baseTest.ts` adds a `beforeAll` soft pre-flight that calls `/ui-knowledge/change-status/?route=<BASE_URL>` and warns on severe drift. Off by default for runs without a baseline (set `UI_CHANGE_PREFLIGHT=true`); set `UI_CHANGE_PREFLIGHT_BLOCK=true` to fail the run instead of warn.

### Phase 1 verification (to run on your end after `migrate`)

```bash
cd ai-healer-django/flaky_healer
python manage.py migrate clients
python manage.py migrate test_analytics
python manage.py migrate test_generation
python manage.py migrate ui_knowledge
```

Then in the Django admin, confirm:
- Every pre-existing `TestRun`, `TestCaseResult`, `GenerationJob`, `UIPage` row now has `client=Legacy`.
- The `Legacy` client exists with `slug="legacy"`.

Multi-tenant smoke test:
1. Create two `Clients` rows (e.g. `Acme`, `Bluco`) via admin.
2. Assign two distinct users to them via `UserClient`.
3. Login as user A → POST a `UIPage` snapshot for `/login`. Login as user B → same POST. Both succeed (used to collide on the unique `route` constraint).
4. Login as user A in the dashboard → confirm `runs` list only shows runs whose `client=Acme`.

### 1.1 Current state (audited)

### 1.1 Current state (audited)

Only `curertestai` has client scoping today. Everything else is global.

| App | Model | Has `client` FK today? |
|---|---|---|
| `curertestai` | `HealerRequestBatch` | ✅ `client_id → Clients` |
| `curertestai` | `HealerRequest` | ✅ `client_id → Clients` |
| `curertestai` | `SuggestedSelector` | ❌ (FK to `HealerRequest`, inherits transitively) |
| `curertestai` | `DomSnapshot` | ❌ |
| `test_analytics` | `TestRun` | ❌ |
| `test_analytics` | `TestCaseResult` | ❌ |
| `test_generation` | `GenerationJob` | ❌ |
| `test_generation` | `GenerationScenario`, `GeneratedArtifact`, `GenerationExecutionLink` | ❌ (transitive via job) |
| `ui_knowledge` | `UIPage`, `UIRouteSnapshot`, `UIElement`, `UIScreenshot`, `UIChangeLog` | ❌ |

JWT *already* carries `client_id`. It is set in [`auth/tokens.py`](../ai-healer-django/flaky_healer/auth/tokens.py):

```python
refresh["client_id"] = str(client.secret_key)
```

and read in [`curertestai/views.py:51`](../ai-healer-django/flaky_healer/curertestai/views.py) — `client_secret = request.auth.get('client_id')`. The mechanism works; it is just not used outside `curertestai`.

`UIPage.route` is currently globally unique (`unique_together = ("route",)`, [`ui_knowledge/models.py`](../ai-healer-django/flaky_healer/ui_knowledge/models.py)). Two clients with `/login` collide today.

### 1.2 Changes

1. **Add `client = ForeignKey(Clients, on_delete=PROTECT, db_index=True)` to**:
   - `test_analytics`: `TestRun`, `TestCaseResult` (also reachable via `test_run.client`, but store directly to keep filters cheap).
   - `test_generation`: `GenerationJob` (children inherit via `job.client`).
   - `ui_knowledge`: `UIPage` (children inherit via `page.client`).
   - `curertestai`: `SuggestedSelector` — denormalize from parent so admin queries can filter; can be skipped if perf is not a concern.

2. **Change `UIPage.Meta.unique_together` to `("client", "route")`.** Otherwise the second client to sync `/login` gets an IntegrityError.

3. **Centralize client resolution in middleware.** Today every view re-implements the JWT → `Clients` lookup. Introduce `clients/middleware.py::ClientResolutionMiddleware` that:
   - Reads `client_id` from `request.auth` (DRF SimpleJWT) on authenticated views.
   - Sets `request.client = Clients.objects.get(...)`.
   - Returns 401/403 if the JWT is missing the claim (gated by a view-mixin or decorator so unauth endpoints like `/test-analytics/test-result/` can opt out).
   - Add a `ClientScopedQuerysetMixin` for ViewSets / API views that auto-applies `.filter(client=request.client)` on `get_queryset()`.

4. **Authenticate the analytics ingest.** `/test-analytics/test-result/` is currently open (no auth). Decide:
   - **Option A (recommended):** require JWT here too, so the client is implicit. Update `wraper-healer/sendToDjango.ts` to use `authenticatedPost` like `/api/heal/`.
   - **Option B:** keep it open but require an explicit `client_secret` in the body (less safe — token leaks live in logs).

5. **Backfill migration.**
   - New rows after deploy must have a `client`. Older rows do not.
   - Plan: introduce a *default tenant* (`Clients.objects.get_or_create(clientname="legacy")`) and run a data migration that assigns it to every existing row. Surface this in the admin as "Legacy" so it's obvious.
   - For UIPage, the route uniqueness change is safe as long as the backfill puts every existing row under the legacy client first.

### 1.3 Files to edit (per app)

| File | Change |
|---|---|
| `test_analytics/models.py` | Add `client = ForeignKey(...)` to `TestRun`, `TestCaseResult`. |
| `test_analytics/views.py` | All views: filter by `request.client`. `PlaywrightResultAPIView` must resolve client from JWT (see auth decision above). |
| `test_analytics/serializers.py` | Drop `client` from writable fields — server-set only. |
| `test_generation/models.py` | Add `client = ForeignKey(...)` to `GenerationJob`. |
| `test_generation/views.py` | All views: filter by `request.client`. |
| `test_generation/generation_service.py` | Pass `client` into job creation; ensure crawls target the client's `base_url`. |
| `ui_knowledge/models.py` | Add `client` to `UIPage`; change `unique_together = ("client", "route")`. |
| `ui_knowledge/views.py` | Resolve and filter by `request.client`. Snapshot writes scoped per client. |
| `ui_knowledge/change_detection_service.py` | All lookups scoped by client. |
| `curertestai/serializers.py` | Add `client` denormalization on `SuggestedSelector` if we keep it. |
| `clients/middleware.py` | **new** — `ClientResolutionMiddleware`. |
| `clients/mixins.py` | **new** — `ClientScopedQuerysetMixin`. |
| `flaky_healer/settings.py` | Register middleware. |
| `migrations/*` | One per app: schema + backfill data migration. |

### 1.4 Per-client directory for test code

> *Open question — see § Open questions.*

User intent: "Each client should have separate directory where all the test cases / playwright framework new and existing execute."

Two viable interpretations:

| Option | Layout | Pros | Cons |
|---|---|---|---|
| **A. Single repo, per-client subdirs** | `tests/{client_slug}/...`, `pages/{client_slug}/...` | Minimal infra change. One `playwright.config.ts`, one `node_modules`. Easy to share fixtures. | Single npm install. No code isolation between clients. |
| **B. Separate Playwright project per client** | `clients/{slug}/playwright/` with its own `package.json`, `playwright.config.ts`, `tests/`, `pages/` | Strong isolation. Different clients can pin different versions. | Materialization gets harder (Django has to know where each client lives). Multiple Node trees. |

Either way:
- `GenerationJob.materialize` ([`test_generation/views.py`](../ai-healer-django/flaky_healer/test_generation/views.py)) needs to compute the destination path from `job.client.slug`. Add a `slug` field on `Clients` (auto-derived from `clientname`).
- Streamlit (and the future dashboard execute panel) needs to invoke `npx playwright test` with the right `cwd`.
- `wraper-healer/runGenerationFromFile.mjs` currently writes to `tests/generated/`; needs to route by client.

Action item: pick A or B before starting; I'd lean Option A unless versioning isolation is required.

### 1.5 UI-change-check on generate and execute

User intent: "While execution of existing test case or generating new test case, UI change log should be compared with baseline UI."

Today, change detection runs **only** during `/api/heal/` ([`curertestai/views.py:484`](../ai-healer-django/flaky_healer/curertestai/views.py)) — i.e. after a click already failed. We need to invoke it proactively:

- **At generation time.** Inside `test_generation/generation_service.py`, before calling Ollama, run `detect_ui_change_for_healing()` for each seed_url. If `ui_change_level >= MAJOR_CHANGE` or `ELEMENT_REMOVED`, surface a warning on the `GenerationJob` (new field `ui_change_warnings: JSONField`) so the operator can re-sync UI knowledge first.
- **At execute time.** Two options:
  1. Run a pre-flight `npm run sync:ui --check` that crawls the seeds and compares against baseline; if MAJOR_CHANGE detected, fail fast.
  2. Inside `wraper-healer/baseTest.ts`, add a `beforeAll` that fires `GET /ui-knowledge/change-status/?route=…` for the entry URL and aborts the run if the change is severe.

Recommended: do #1 for generation (visible in UI), and add a soft-warning version of #2 for execute (logs only, does not abort by default).

---

## Phase 2 — `curertestai` cleanup  ✅ done (2026-06-29)

Implemented:
- Models removed: `HealerRequestBatch`, `DomSnapshot`.
- `HealerRequest.batch_id`: FK → `IntegerField(default=0)`.
- Admin: `HealerRequestBatchAdmin` and `DomSnapshotAdmin` deleted; `HealerRequest` admin no longer references `batch_id`.
- Views: `_save_dom_snapshot` deleted; `BatchHealAPIView` no longer persists a batch row (totals are computed in-memory; response `id` is `0`).
- Scoring re-weighted to `0.75·semantic + 0.15·history + 0.10·LLM` (was `0.70 / 0.12 / 0.10 / 0.08·retrieval`). Retrieval-related response fields kept for client back-compat, always `false`/`0`/`[]`.
- Migration `0008_remove_domsnapshot_and_batch.py` authored — run `python manage.py migrate curertestai` against the MySQL DB to apply. Destructive: drops `dom_snapshot` and `healer_request_batch` tables.
- TS client (`wraper-healer/`): no change required — `batch_id` and `cache_source_id` still arrive (now always `0` / `undefined`).

### 2.1 Remove `HealerRequestBatch` and `DomSnapshot`

**Scope** (already audited end-to-end):

| File | What changes |
|---|---|
| [`curertestai/models.py`](../ai-healer-django/flaky_healer/curertestai/models.py) | Delete `HealerRequestBatch` (l.7–17) and `DomSnapshot` (l.63–88). Update `HealerRequest.batch_id`: drop the FK, keep an `IntegerField(default=0)` for response compatibility. |
| [`curertestai/admin.py`](../ai-healer-django/flaky_healer/curertestai/admin.py) | Remove `HealerRequestBatchAdmin` (l.45–49) and `DomSnapshotAdmin` (l.52–69). Remove `batch_id` from `HealerRequestAdmin.list_display` / `list_filter`. |
| [`curertestai/views.py`](../ai-healer-django/flaky_healer/curertestai/views.py) | Drop imports (l.20). Drop `batch_instance` param from `_save_heal_request` (l.393). Delete `_save_dom_snapshot()` (l.446–483) and its call. In `BatchHealAPIView` (l.628–747): compute totals in-memory, don't persist a batch row. |
| [`curertestai/validation_engine.py`](../ai-healer-django/flaky_healer/curertestai/validation_engine.py) | Drop `DomSnapshot` import (l.10). In `_retrieval_boost()` (l.189–256): either delete the function and remove its weight, or replace its data source (see § 2.2). |
| [`curertestai/serializers.py`](../ai-healer-django/flaky_healer/curertestai/serializers.py) | Keep `batch_id` in `HealResponseSerializer` (l.60) — TS client reads it. Always set 0. |
| Migrations | New migration: `RemoveField('HealerRequest','batch_id')` (FK) → `AddField('HealerRequest','batch_id', IntegerField(default=0))`, then `DeleteModel('HealerRequestBatch')` and `DeleteModel('DomSnapshot')`. Run on a backup first — data is destroyed. |

**TS client (`wraper-healer/`) impact:**

| File:line | Field | Action |
|---|---|---|
| [`healer.ts:59`](../wraper-healer/healer.ts) | `batch_id: number` | No change — server keeps returning 0. |
| [`healer.ts:33`](../wraper-healer/healer.ts) | `cache_source_id?: number` | Optional — keep field; it will just be `undefined`. |
| [`selfHealing.ts:214,228–233`](../wraper-healer/selfHealing.ts) | `cacheSourceId` for log message | No change — log line gracefully degrades. |

### 2.2 Retrieval boost (0.08 weight) — what replaces it?

`DomSnapshot` provided the *retrieval boost* in the scoring formula:

```
final = 0.70·semantic + 0.12·history + 0.10·llm + 0.08·retrieval
```

Removing it drops the ceiling to 0.92. The history boost already covers "this selector worked before for this intent on this URL" — the unique value of `DomSnapshot` was *DOM-shape similarity* via signature tokens, which lets the scorer say "yes, the page looks like it did last time we successfully healed, so reuse that healed selector with more confidence."

Three options:

1. **Drop the boost.** Reweight to `final = 0.75·semantic + 0.15·history + 0.10·llm`. Simplest. Acceptable because the explicit healing cache already short-circuits to known-good selectors when the page matches recently.
2. **Move retrieval into `HealerRequest`.** It already stores `html`, `dom_fingerprint`, `candidate_snapshot`, `signature_tokens` (already exists). The boost becomes "find past successful `HealerRequest`s with similar fingerprint". One fewer model.
3. **Move retrieval into `UIRouteSnapshot`.** Healing time, compare the live DOM fingerprint against the latest baseline snapshot for that `(client, route)`. Tighter coupling between healing and the UI-knowledge layer — fits well with Phase 1.4.

Recommendation: **(2) collapse into HealerRequest** if you want minimal churn, **(3) collapse into ui_knowledge** if you also commit to per-execute UI-change checks (Phase 1.5).

---

## Phase 3 — Absorb Streamlit into the Django dashboard  ✅ done (2026-07-01)

Implemented:

**Infrastructure**
- New requirements: `django-q2==1.7.6`, `cryptography==43.0.1`, `redis==5.0.8`, `requests==2.32.3`.
- `settings.py` gained a Phase-3 block: `REPO_ROOT`, `FERNET_KEY` (derived from `SECRET_KEY` in dev, `FERNET_KEY` env var in prod), `Q_CLUSTER` config (Redis by default, ORM fallback via `Q_ORM_BROKER=true`), and `RUNNER_LOG_DIR`.
- New apps registered: `django_q`, `integrations_jira`, `runners`.

**`integrations_jira` app**
- Model: `JiraConnection` (OneToOne per `Clients`, `api_token_encrypted` stored as Fernet ciphertext; helpers `set_api_token()`, `get_api_token()`, `auth_tuple()`).
- `services.py::JiraClient` — thin REST wrapper for search, issue detail, add-comment, attach-file. `build_adf_paragraph()` builds a minimal ADF body.
- Endpoints (all JWT-or-session, all tenant-scoped):
  - `GET  /integrations/jira/connection/` · `PUT /integrations/jira/connection/`
  - `POST /integrations/jira/search/`
  - `GET  /integrations/jira/issue/<key>/`
  - `POST /integrations/jira/comment/`

**`runners` app**
- Model: `RunnerJob` (client-scoped, `kind ∈ {GEN, EXECUTE}`, `state ∈ {QUEUED, RUNNING, SUCCESS, FAILED, CANCELLED}`, argv/cwd/env-overrides/log-path/return-code/timings). `state` is deliberately separate from `abstract.Common.status` (the lifecycle marker).
- `tasks.py` — django-q2 task entry points `run_generate` and `run_execute`. Both stream `stdout+stderr` line-buffered into `logs/runners/job_<id>.log`.
- Endpoints:
  - `POST /runners/generate/` — enqueue `npm run gen:testcases`.
  - `POST /runners/execute/` — enqueue `npx playwright test [spec] [-g grep] --workers=N`, honours `headed`.
  - `GET  /runners/jobs/` · `GET /runners/jobs/<id>/` — list and detail.
  - `GET  /runners/jobs/<id>/stream/` — **SSE** tail of the log file. Emits `event: log` frames while running, `event: done` on exit; keep-alive comment every 15 s.

**Dashboard layout**
- New shared template [_layout.html](ai-healer-django/flaky_healer/test_analytics/templates/test_analytics/_layout.html) — left side-nav + topbar. Sidebar highlights based on the view's `active_panel` context var.
- Five new templates in [test_analytics/templates/test_analytics/panels/](ai-healer-django/flaky_healer/test_analytics/templates/test_analytics/panels/): `worklist.html`, `config.html`, `generate.html`, `review.html`, `execute.html`.
- Five new view classes in `test_analytics/views.py`: `WorklistPanelView`, `ConfigPanelView`, `GeneratePanelView`, `ReviewPanelView`, `ExecutePanelView` — all `LoginRequiredMixin` + `TemplateView`, share `_PanelView` base.
- Route additions: `/test-analytics/{worklist,config,generate,review,execute}/`. Root `/test-analytics/` now redirects to Worklist.
- Existing Healer & Jobs tabs are still served by `TestAnalyticsDashboardView` at `/test-analytics/dashboard/`; the view now sets `active_panel` from `?tab=` so the sidebar highlights correctly.

**Streamlit sunset**
- Streamlit is left in place for parallel operation (soft cut-over). The dashboard hits the exact same Django endpoints the Streamlit UI used, so both work simultaneously.
- Once every panel is validated, drop the `start_apps.command` step that launches Streamlit and remove the `streamlet-ui/` directory.

### Phase 3 verification (to run on your end)

```bash
# 1. Install new deps
cd ai-healer-django
pip install -r requirements.txt

# 2. Run migrations (creates integrations_jira, runners, django_q tables)
cd flaky_healer
python manage.py migrate

# 3. Start Redis (or export Q_ORM_BROKER=true for dev)
redis-server &

# 4. Start the django-q2 worker in one terminal
python manage.py qcluster

# 5. Start Django in another terminal
python manage.py runserver

# 6. Open http://127.0.0.1:8000/test-analytics/  (redirects to /worklist/)
#    - save a Jira connection (token is encrypted at rest)
#    - search for issues, pick one, jump to Config
#    - fill the form, jump to Generate
#    - click "POST /test-generation/jobs/" OR "Enqueue runner"
#    - open Review, load the job, approve + materialize
#    - open Execute, enter a spec path, watch SSE logs
#    - push a comment to Jira from the same panel
```

### 3.1 Current state (audited)

### 3.1 Current state (audited)

Dashboard today: [`test_analytics/templates/test_analytics/dashboard.html`](../ai-healer-django/flaky_healer/test_analytics/templates/test_analytics/dashboard.html) — single template, 1599 lines, vanilla ES6 + custom CSS, two tabs:

- **Healer tab**: filter bar (run/build/env), 9 KPI cards, recent-failures table with step-event timeline, four chart cards (failure category, healing outcome, history assist, top failed selectors).
- **Jobs tab**: 3 KPI cards, recent jobs table, job-detail panel (scenarios + artifacts preview), generation jobs chart.

Streamlit today: 5 multi-pages ([`streamlet-ui/pages/0..4`](../streamlet-ui/pages/)), each owning one step of the wizard: Jira Worklist → Feature Config → Generate → Review → Execute. State machine in [`utils/autonomous_flow.py`](../streamlet-ui/utils/autonomous_flow.py).

### 3.2 Target dashboard layout

Reorganize into **a single dashboard with a left side-nav** so all four areas live in one place. Replaces the two-tab top nav.

```
┌──────────────────────────────────────────────────────────────────┐
│ XT-Forge Dashboard           [run] [build] [env]   user ▾        │
├──────────┬───────────────────────────────────────────────────────┤
│ 🗂 Work   │                                                       │
│ 🧪 Config │                                                       │
│ ⚡ Gen    │                  active panel renders here            │
│ ✅ Review │                                                       │
│ ▶ Execute │                                                       │
│ ─────     │                                                       │
│ 📊 Healer │                                                       │
│ 📈 Jobs   │                                                       │
└──────────┴───────────────────────────────────────────────────────┘
```

- **Top 5 = the workflow** (replacement for Streamlit).
- **Bottom 2 = today's analytics**, kept as-is for now.

Each top-5 panel maps 1:1 to a Streamlit page (see § 3.3).

### 3.3 Page-by-page migration map

| Streamlit page | New dashboard panel | New Django endpoints | Notes |
|---|---|---|---|
| `0_📋_Jira_Worklist.py` | **Worklist** | `GET /test-analytics/jira/issues/` (proxies Jira `POST /rest/api/3/search/jql`) | Move the Jira creds from `st.session_state` to a `JiraConnection` model scoped by client. Token must not be in JS — keep it server-side. |
| `1_🧪_Feature_Config.py` | **Config** | `GET/PUT /test-generation/feature-requests/{client_id}/` | Replace the JSON-on-disk (`wraper-healer/generation/feature_requests.json`) with a DB-backed `FeatureRequest` model OR a per-client file inside the client's directory (Phase 1.4). |
| `2_⚡_Generate.py` | **Generate** | `POST /test-generation/jobs/` (exists), `GET /test-generation/jobs/{id}/log/` for live stream | Today Streamlit shells out to `npm run gen:testcases`. In Django, call the generation pipeline directly via a Celery task (or sync if jobs are short). Stream stdout over SSE or WebSocket. |
| `3_👁️_Review.py` | **Review** | Existing `approve/`, `materialize/`, `artifacts/update/` | Already API-backed. Replace Streamlit UI with a Django page that hits the same endpoints. |
| `4_▶️_Execute.py` | **Execute** | `POST /executions/` (new) → returns `execution_id`; `GET /executions/{id}/log/` (SSE) | Spawns `npx playwright test ...` in the client's dir (Phase 1.4). Server-managed subprocess: process supervision, log streaming, cancel button, history. Jira push moves server-side too. |

### 3.4 Where new code goes

| Area | Location |
|---|---|
| New panels (HTML) | `test_analytics/templates/test_analytics/panels/{worklist,config,generate,review,execute}.html` |
| Shared layout (sidebar, topbar) | `test_analytics/templates/test_analytics/_layout.html` |
| Client-side JS | Start with vanilla (existing convention). If complexity warrants, add HTMX later — avoid pulling in React/Vue unless the team is committed. |
| Background tasks (generation, execution) | New app `runners/` with Celery (recommended) or `django-q2` if a lighter task queue is preferred. Requires Redis. |
| Live log streaming | SSE via `django.http.StreamingHttpResponse` is the lowest-friction path. WebSockets require Channels. |
| Jira proxy | New app `integrations/jira/` — owns `JiraConnection` model (per-client), validates connections, proxies issue search + comment + attachment. Tokens encrypted at rest (use `django-cryptography` or Fernet on a settings-managed key). |

### 3.5 Streamlit sunset

The user said *"I will remove streamlit UI later."* Plan it as a soft cut-over:

1. Build the Django panels alongside Streamlit. Keep both running.
2. Operator validates feature parity per panel.
3. Once all five panels are green, remove the `streamlit run` step from `start_apps.command` and stop documenting it. Keep `streamlet-ui/` in the repo for a release or two as fallback, then delete.

### 3.6 Healer & Jobs tabs (existing)

Both keep their content. Reskin to fit the side-nav layout. Filters move into the top bar so they apply across all panels (a generation job and its test results can be filtered together).

---

## Phase 4 — Cross-cutting items

- **`Clients.slug` field.** Required for directory naming (Phase 1.4) and for keying everything in URLs cleanly. Auto-generated from `clientname` on save; unique.
- **Admin polish.** The clients/admin already has `logo_preview`. Add inline `UserClient` to `ClientsAdmin` so the user-↔-client mapping is editable in one place.
- **Documentation.** Update [ARCHITECTURE.md](ARCHITECTURE.md), [API_REFERENCE.md](API_REFERENCE.md), [DATA_FLOW.md](DATA_FLOW.md), and [WORKFLOWS.md](WORKFLOWS.md) per phase as it lands.

---

## Phase 6 — Multi-agent pipeline + Cucumber + auto-retry  🚧 Stage 1 (Foundations) done (2026-07-03)

Replaces the single-shot `generate_job_draft()` with a **6-agent pipeline** where each agent produces reviewable output and the operator gates every transition. Framework: Cucumber (`.feature` + step defs + page objects); the legacy `.spec.ts` output is retired for new jobs.

### Stage 1 — Foundations (this commit)

Model + migration + agents module + Cucumber runtime scaffolding. No UI wiring yet; each agent is hand-runnable from the Django shell for smoke tests.

- **`GenerationJob` gained**: `stage`, `stage_feature_output`, `stage_manual_tests_output`, `stage_plan_output`, `stage_execute_output`, `stage_history`, `execute_iteration`, `jira_issue_key`. Migration `test_generation/0003_pipeline_stages.py`.
- **`GeneratedArtifact.artifact_type`** now includes `FEATURE` and `STEP_DEFINITIONS`.
- **[test_generation/agents.py](../ai-healer-django/flaky_healer/test_generation/agents.py)** — six agent entry points, each calls `_call_ollama_json` with its own prompt + JSON schema:
  - `run_feature_agent(job, jira_summary, jira_description)` — Jira → feature spec.
  - `run_manual_tests_agent(job)` — feature → manual test cases (Given/When/Then).
  - `run_plan_agent(job, selector_map, intent_keys)` — manual tests → concrete plan.
  - `run_artifacts_agent(job, selector_map, intent_keys)` — plan → `.feature` + step-defs + page objects.
  - `run_root_cause_fixer(job, failed_scenario, failed_step, error_message, ...)` — patches artifacts after a failing run.
  - `run_reporter_agent(job)` — synthesizes an ADF-ready Jira comment.
- **`_validate_relative_path` + `_scoped_relative_path`** now accept `features/` and `features/steps/` prefixes. `_validate_artifact_content` gained `FEATURE` and `STEP_DEFINITIONS` branches. `_typescript_parse_check` skips `.feature` files (Gherkin isn't TS).
- **Cucumber runtime scaffolding at repo root**: `cucumber.js`, `features/support/world.ts`, `.gitignore`. `package.json` gained `@cucumber/cucumber` and `cucumber` / `cucumber:tenant` scripts. The `tenant` profile reads `TENANT_SLUG` env var so the Executor can target one client's directory.

### Stage 2 — Endpoints + panels  ✅ done (2026-07-03)

- **11 new endpoints in `test_generation/urls.py`**:
  - `POST /test-generation/pipeline-jobs/` — light intake (creates a row in `STAGE_INTAKE`, no LLM call).
  - `POST /test-generation/jobs/<uuid>/stage/{feature,manual-tests,plan,artifacts,execute}/run/` — invoke that stage's agent, persist raw output.
  - `POST /test-generation/jobs/<uuid>/stage/{...}/approve/` — advance the stage. Optional `edited_output` overrides the agent's raw output; optional `reviewer_notes` is recorded on `stage_history`.
- **`StageFeatureRunView`** pulls live Jira context via `integrations_jira` when `job.jira_issue_key` is set: fetches the issue, walks the ADF description into plain text, feeds both into the Feature Author agent.
- **`StageArtifactsRunView`** persists the agent's `features` + `step_definitions` + `page_objects` bundles into `GeneratedArtifact` rows and runs `_validate_artifacts` on the merged set. Rows land with `validation_status` + per-artifact errors so the Review panel can show them.
- **`StageArtifactsApproveView`** approves + materializes in one shot: writes files under `features/<slug>/`, `features/steps/<slug>/`, and `tests/pages/generated/<slug>/`, then flips `job.stage = EXECUTE`.
- **`_resolve_plan_context()`** builds the enriched selector map + intent keys the Plan Architect and Artifact Generator both consume — reuses the Phase 5 `_enrich_llm_context` helper. Crawl summary is cached on the job to avoid re-crawling on retries.
- **Three new dashboard panels** (all extend `_layout.html`):
  - `panels/feature_review.html` — form-editable feature spec, Run + Approve buttons.
  - `panels/manual_tests_review.html` — table of Given/When/Then blocks, inline-editable.
  - `panels/plan_review.html` — scenario tree with selectors + intent keys + step JSON preview.
- **Extended `panels/review.html`** — same panel now handles both flows. On `stage=PLAN` it shows a **Run Artifact Generator** button; on `stage=ARTIFACTS` its Approve calls the pipeline endpoint (which approves + materializes at once) instead of the legacy two-click flow.
- **Sidebar `Pipeline` group** (added between Workflow and Analytics) with Feature / Manual Tests / Plan / Review / Execute in stage order. `active_panel` highlights the current stage.
- **Worklist panel** — each Jira row now has two buttons: **🧩 Start pipeline →** creates a pipeline job via the intake endpoint and hops to Feature Review; **Legacy config →** keeps the pre-Phase-6 config flow alive for parity.
- **`test_generation/views.py`** gained `PipelineJobCreateView` and the 10 stage views. Route names: `pipeline_job_create`, `stage_{feature,manual_tests,plan,artifacts,execute}_{run,approve}`.

### Stage 3 — Executor + retry + Jira push  ✅ done (2026-07-03)

- **`runners` app** gained `KIND_CUCUMBER` (choice-only, additive migration `0002_kind_cucumber.py`) and a `run_cucumber` task entry point in `runners/tasks.py`. Both reuse the same generic `_run()` primitive that the existing `run_execute` uses — nothing Cucumber-specific in the task.
- **[test_generation/executor.py](../ai-healer-django/flaky_healer/test_generation/executor.py)** — new `run_and_repair(job_id)` django-q2 task:
  - Iterates up to 3 times.
  - Each iteration: re-materializes (so patches from the previous iteration land on disk), spawns a `RunnerJob(kind=CUCUMBER)` via the task queue's `_run` primitive *inline* (bypassing `async_task` since we're already inside a worker), reads `test-results/cucumber-report.json`, decides pass/fail per scenario.
  - On failure with budget left: picks the first failed scenario + step, calls `run_root_cause_fixer` with the error message + current artifact snapshot, applies the returned patches to `GeneratedArtifact.content_final`. Files rewrite on the next iteration's `materialize_job(allow_overwrite=True)`.
  - Persists `stage_execute_output = {iterations: [...], final_state: "GREEN"|"HUMAN_REVIEW_NEEDED", green_iteration?}` incrementally after every iteration so the panel's polling UI shows progress live.
  - On green: `job.stage = STAGE_REPORT`. After 3 red iterations: `job.stage = STAGE_HUMAN_REVIEW_NEEDED`.
- **`StageExecuteRunView`** — real body: resets `execute_iteration` + `stage_execute_output`, enqueues `test_generation.executor.run_and_repair` via django-q2, returns 202. Accepts re-runs from `HUMAN_REVIEW_NEEDED` for operator-driven retries.
- **`StageExecuteApproveView`** — real body: gated on `final_state == "GREEN"` and a non-empty `jira_issue_key`. Runs the Reporter agent to produce a `{headline, body_markdown, highlights}` payload, converts that to ADF via `_adf_from_report()`, posts the comment via `integrations_jira`, then attaches every `GeneratedArtifact` (feature file, step defs, page objects) as a Jira attachment. Also attaches the last iteration's runner log tail (last 64 KB). Advances `job.stage` to `STAGE_DONE`.
- **Execute panel** ([execute.html](../ai-healer-django/flaky_healer/test_analytics/templates/test_analytics/panels/execute.html)) — rewritten with a pipeline-first layout. Top card shows a pipeline job picker, Run + Push buttons, stage badge. Middle card renders one collapsible card per iteration (banner: GREEN/RED/ERROR, scenario counts, per-scenario expandable details with failing step + error message, root-cause diagnosis, patches applied). Polls `GET /test-generation/jobs/<id>/` every 4 s once Run is clicked; auto-stops on `REPORT`/`DONE`/`HUMAN_REVIEW_NEEDED`. Legacy Playwright runner tucked into a `<details>` block at the bottom.

### Stage 3 verification (to run on your end)

Prereqs: Ollama running, Redis + `python manage.py qcluster` up, `npm install` at repo root, `python manage.py migrate` after pulling.

1. Materialize a Phase 6 job (walk through Feature → Manual Tests → Plan → Review → Approve). Job is now `stage=EXECUTE`, files are on disk under `features/<slug>/…` etc.
2. Open `/test-analytics/execute/`. The pipeline picker shows the job. Click **▶ Run Cucumber (up to 3 iterations)**.
3. Panel polls; within ~30-90 s the first iteration card appears. Expand to see per-scenario status.
4. If red: within another minute a second iteration card appears with the root-cause diagnosis and the `patches_applied` list. Watch either turn green or produce a third card.
5. On green: **🚀 Push to Jira** enables. Click it. Confirm in Jira: one new comment (ADF), plus attachments (`.feature`, step defs, page objects, `runner-<id>.log`).
6. On HUMAN_REVIEW_NEEDED: the panel keeps the iteration log, disables Push, and prompts the operator to intervene manually.

### Rollback

- Set `TEST_GEN_CODEGEN_RETRY_ENABLED=false` doesn't apply here (that's Phase 5). To disable the pipeline auto-retry loop, delete the `run_and_repair` task binding — but there's no reason to: the endpoint refuses to fire unless the operator clicks Run.
- Every Stage 3 change is additive. Legacy Playwright execute flow works unchanged.

### Stage 3 — Executor + retry + Jira push (after that)

`test_generation/executor.py`, Cucumber branch in `ExecuteEnqueueView`, Root-Cause Fixer wired into the retry loop (cap 3), Jira push endpoint. After Stage 3, the full green-path plus one forced-failure loop should work end-to-end.

### Rollback

Every Stage-1 change is additive. Legacy single-shot generation (`POST /jobs/`) continues to work exactly as it does today — the new `stage` field defaults to `INTAKE` on legacy jobs and never advances unless the new endpoints (Stage 2) are invoked. Migration is destructive to nothing (only adds fields + extends a choice list).

---

## Phase 5 — LLM prompt tuning + one-shot validator retry  ✅ done (2026-07-03)

Fixes generation jobs that failed because the LLM output violated `_validate_artifact_content` rules the prompt never told it about — most acutely when the feature carries manual-scenario selectors / custom `intent_key`s not present in the crawl-derived selector map.

Implemented in [test_generation/generation_service.py](../ai-healer-django/flaky_healer/test_generation/generation_service.py):

- **`_enrich_llm_context(planning, selector_map, allowed_intent_keys, notes)`** — walks every step in `planning["scenarios"]` and injects any missing selectors (keyed by `_slug(step.action)`) and any custom `intent_key` (scenario- or step-level) into the two lists the LLM sees. Idempotent. Reports what was added into `llm_notes`.
- **`_codegen_retry_enabled()`** — env gate `TEST_GEN_CODEGEN_RETRY_ENABLED` (default `true`).
- **`_build_codegen_prompt(job, planning, crawl_summary, *, selector_map=None, allowed_intent_keys=None)`** — rewritten. Rules block now mirrors `_validate_artifact_content` 1:1: exact import lines, spec/page-object structural requirements, full forbidden-pattern list, path constraint, completeness ("emit code for every step; do not skip scenarios"), and explicit "reuse selectors from the map verbatim".
- **`_build_codegen_retry_prompt(..., *, selector_map=None, allowed_intent_keys=None, invalid_artifacts_report=None)`** — two modes. Without `invalid_artifacts_report` it behaves like before (empty-response retry). With the report it shows the LLM its own previous invalid output and quotes each validator error verbatim, telling it to fix them and regenerate the full JSON.
- **`generate_job_draft` codegen block** — enrichment runs once, before the first prompt; enriched `selector_map`/`allowed_intent_keys` are passed to both prompts. After the empty-response retry (unchanged), the new validation retry runs: if `_validate_artifacts` reports any invalid artifact and the env flag is on, one more LLM call is made with the validator errors embedded. Retry output is only accepted if it strictly reduces `invalid_artifacts`.

Backward compat: no models, no migrations, no serializer changes. Function signatures are positional-compatible; new params are keyword-only with defaults. `TEST_GEN_REQUIRE_ALL_ARTIFACTS_VALID` still governs strict-gate behavior.

### Phase 5 verification

1. Restart the Django dev server so any env changes are picked up.
2. Re-run the XX-94 flow: Config → Generate.
3. Expected: job reaches `DRAFT_READY` on first attempt (no retry needed) because the enriched selector map now contains `#loginButton`, `#username`, `#password`, `button[type="submit"]`, and the custom `homepage_agree_cta` / `homepage_signin_cta` intent keys are in the allowed list.
4. If the first attempt still produces one invalid artifact, look at `llm_notes` on the job (Django admin → GenerationJob detail). One of these will be present:
   - `"Codegen retry improved validation: N → 0 invalid."`
   - `"Codegen retry did not reduce validation errors; keeping first attempt."`
5. Rollback: `export TEST_GEN_CODEGEN_RETRY_ENABLED=false` disables the retry pass. The enrichment helper is safe to leave — it only adds context, never removes.

---

## Suggested sequencing

The phases are mostly independent, but order matters for migrations.

1. **Phase 2 first** — removing `HealerRequestBatch` / `DomSnapshot` is a contained refactor and reduces what Phase 1 has to migrate.
2. **Phase 1 (multi-tenancy)** — add `client` FK everywhere + middleware + UIPage uniqueness. Backfill to a "legacy" client. *Defer the per-client directory split (1.4) until you commit to Option A or B.*
3. **Phase 1.4 + 1.5 (directories + UI-change pre-flight)** — these unlock the new Execute panel.
4. **Phase 3 (dashboard absorbs Streamlit)** — build panels one at a time, in Streamlit order: Worklist → Config → Generate → Review → Execute.
5. **Streamlit sunset.**

---

## Open questions to settle before execution

1. **Per-client directory layout — Option A (`tests/{slug}/...`) or Option B (`clients/{slug}/playwright/...`)?**
2. **Analytics ingest auth — require JWT (server change + TS client change) or keep open with explicit `client_secret` in payload?**
3. **Retrieval boost replacement — drop it (re-weight to 0.92 baseline), fold into `HealerRequest`, or fold into `UIRouteSnapshot`?**
4. **Existing data — assign to a single "legacy" client, or skip the backfill and let those rows fade out via TTL?**
5. **Background task queue — Celery (full-featured) or `django-q2` (lighter)?**
6. **Live log streaming — SSE (simpler) or Channels/WebSocket (full duplex, more setup)?**
7. **Jira token storage — encrypt with `django-cryptography`, or just rely on filesystem perms + `.env`?**

---

## Verification per phase

- **Phase 2.** Run the existing `npm test` against a fresh DB; assert `HealResponse.batch_id === 0`, `validation_status` still set, scoring still produces a `chosen`. Migrate; assert tables dropped; admin UI no longer lists the two models.
- **Phase 1.** Create two `Clients` via admin. Authenticate as user A (assigned to client 1), POST to `/ui-knowledge/sync/` with `route=/login`. Authenticate as user B (client 2), POST same route — no IntegrityError. Cross-client GETs return only the caller's rows.
- **Phase 1.5.** Mutate a baseline (`SNAPSHOT_TYPE=NEW_STRUCTURE`), kick off generation — confirm `ui_change_warnings` populated on the job; kick off execute — confirm log warning.
- **Phase 3.** Side-by-side parity test: same Jira ticket processed through both Streamlit and the new Django panels; outputs identical (job created, files materialized, test executed, Jira commented).
