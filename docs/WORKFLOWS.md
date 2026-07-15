# Workflows

How to actually do things. Every workflow lists prerequisites, the exact commands, what to expect, and where the failure points are.

---

## 0. One-time setup

| Step | Command | Notes |
|---|---|---|
| Install Node deps | `npm install` | run from repo root |
| Install Playwright browsers | `npx playwright install` | one-time |
| Python venv for Django | `python -m venv .venv && source .venv/bin/activate` | inside `ai-healer-django/` |
| Install Django deps | `pip install -r requirements.txt` | needs `mysqlclient` (system libs) |
| MySQL DB | start MySQL, `CREATE DATABASE ai_healer_service;`, import `ai-healer-django/ai_healer_service.sql` if present | matches `settings.py:117–125` |
| **Apply Phase 1 + 2 + 3 migrations** | `cd ai-healer-django/flaky_healer && python manage.py migrate` | Phase 2 drops `dom_snapshot` + `healer_request_batch`; Phase 1 adds `client` FKs and the `legacy` tenant backfill; Phase 3 adds `JiraConnection`, `RunnerJob`, and django-q2 tables. Back up first. |
| **Phase 3: install new Python deps** | `pip install -r ai-healer-django/requirements.txt` | Adds `django-q2`, `cryptography`, `redis`, `requests`. |
| **Phase 3: Redis** | `redis-server` (or set `Q_ORM_BROKER=true` for dev) | Broker for the background task runner. |
| **Phase 3: task worker** | `python manage.py qcluster` in a separate terminal | Runs `runners.tasks.run_generate` / `run_execute` from the queue. |
| Streamlit deps | handled by `start_apps.command` (auto-pips `streamlet-ui/requirements.txt`) | |
| Ollama (optional) | `ollama serve` + `ollama pull qwen2.5:7b` | only needed when `USE_LLM_VALIDATION=true` or for generation |

`.env` already exists at the repo root with `BASE_URL=https://www.carnival.com`, `HEADLESS=true`, `TIMEOUT=240000`, `RUN_ID=1234`. Override as needed per command.

---

## 1. Bring the system up

```bash
# Terminal 1 — Django (port 8000)
cd ai-healer-django/flaky_healer
source ../.venv/bin/activate
python manage.py runserver

# Terminal 2 — Streamlit (port 8501) + Chrome
bash start_apps.command
# (auto-detects a venv, kills :8501, pip-installs streamlet-ui/requirements.txt,
#  starts `streamlit run streamlet-ui/app.py`, opens http://localhost:8501)

# Terminal 3 — Ollama (only if using LLM features)
ollama serve
```

Sanity checks:

```bash
curl -s http://127.0.0.1:8000/test-generation/jobs/   # expect JSON
curl -s http://127.0.0.1:8501                          # expect HTML
curl -s http://127.0.0.1:11434/api/tags                # expect ollama models
```

In Streamlit's sidebar, the "● Backend" badge turns green when Django responds within 2s.

---

## 2. Run the existing booking test

Hand-written end-to-end booking test: `tests/carnivalBooking.spec______.ts`.

```bash
npm test                              # all tests, headless
npm run test:headed                   # show the browser
npx playwright test -g "Validate Carnival"   # single test by title
npm run test:login                    # only "Carnival Login Feature" (headed)
```

What runs end-to-end (each user gets the same 9-step sequence):

1. `clearSession()` — wipes cookies + localStorage + sessionStorage.
2. `homePage.open()` — `goto(baseURL)`.
3. `homePage.clickSignIn(testInfo)` — agreement consent + sign in (both via `selfHealingClick` → Django).
4. `loginPage.login(email, password)` — asserts greeting visible.
5. `searchPage.searchCruise()` — Alaska, 6–9 days.
6. `cruiseDetailsPage.selectCruise()` — pick first cruise.
7. `cabinPage.selectCabin()` — accessible room, radio, confirm. Throws `CABIN_OFFER_CONTINUE_FAILED` if the cabin-offer continue button never appears.
8. `cartPage.proceedCheckout()` — handles deck + room selectors, clicks checkout.
9. Assert URL matches `/checkout|payment/`.

If step 7 throws `CABIN_OFFER_CONTINUE_FAILED`, the spec catches it, calls `clearSession()`, and replays the whole sequence with the second user from `test-data/user.ts`.

Reports:

```bash
npm run report          # opens playwright-report/${RUN_ID}/index.html
# JSON also at playwright-report/results.json
# Failures: test-results/<test-id>/{trace.zip, video.webm, screenshot.png}
```

Every test (pass or fail) hits `POST /test-analytics/test-result/`. Confirm in Django:

```bash
curl -s "http://127.0.0.1:8000/test-analytics/summary/?run_id=1234"
```

---

## 3. Generate new tests from a feature request

There are two paths — CLI direct, or the Streamlit wizard. They produce the same Django state.

### 3a. CLI direct

1. Edit `wraper-healer/generation/feature_requests.json` — see `wraper-healer/generation/README.md` for the schema; the file currently holds an XX-94 login example.
2. Submit jobs:
   ```bash
   npm run gen:testcases
   ```
3. Output prints `job_id`s and the detail URL. Open Django admin at `http://127.0.0.1:8000/admin/test_generation/generationjob/` (you'll need a superuser; create one with `python manage.py createsuperuser`).
4. Approve a job and materialize:
   ```bash
   JOB=<uuid>
   curl -X POST "http://127.0.0.1:8000/test-generation/jobs/$JOB/approve/" \
        -H 'Content-Type: application/json' \
        -d '{"approved_by":"cli","notes":""}'
   curl -X POST "http://127.0.0.1:8000/test-generation/jobs/$JOB/materialize/" \
        -H 'Content-Type: application/json' \
        -d '{"allow_overwrite":true}'
   ```
5. New files appear under `tests/generated/` and `tests/pages/generated/`. Run them like any other:
   ```bash
   npx playwright test tests/generated/<file>.spec.ts
   ```

### 3b. Streamlit wizard (recommended for non-devs)

1. Open `http://localhost:8501`.
2. **Page 0 — Jira Worklist.** Connect Jira (URL / email / API token are pre-filled). Pick a ticket. Click **Run Autonomous Flow** to chain everything, or step through manually.
3. **Page 1 — Feature Config.** Edits map straight back to `wraper-healer/generation/feature_requests.json`. Click **Generate Test**.
4. **Page 2 — Generate.** Shells out to `npm run gen:testcases` and streams output. Wait for `returncode == 0`.
5. **Page 3 — Review.** Pick the job, **Approve Draft**, then **Materialize Job**. Inspect generated files in the in-page preview.
6. **Page 4 — Execute.** Pick a test file or "All Tests", configure browser/workers, **Run Tests**. After completion, configure the Jira push (attach code, attach logs) and **Push to Jira**.

Autonomous mode advances stages automatically with short holds (8–15 s) between them so you can see what's happening. Any stage marked `failed` halts the chain.

---

## 4. Refresh the UI baseline (selectors the healer learns from)

The healer leans on `DomSnapshot` and `UIElement` records to bias selector choices toward known-good patterns. Refresh them whenever the target app structurally changes.

```bash
# baseline crawl
BASE_URL=https://www.carnival.com \
SEED_URLS='["/", "/login", "/cruise-deals"]' \
MAX_ROUTES=20 MAX_DEPTH=2 \
SNAPSHOT_TYPE=BASELINE \
FEATURE_NAME="Carnival baseline" \
npm run sync:ui
```

Useful env knobs:

| Var | Default | Effect |
|---|---|---|
| `BASE_URL` | `http://localhost:3000` | site under test |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Django |
| `SEED_URLS` | `["/"]` | BFS roots (JSON list) |
| `MAX_ROUTES` | 20 | crawl breadth |
| `MAX_DEPTH` | 2 | crawl depth |
| `MAX_INTERACTABLES` | 200 | per-page extraction cap |
| `SNAPSHOT_TYPE` | `BASELINE` | use `NEW_STRUCTURE` after a known UI change |
| `UI_SCREENSHOT_DIR` | `test-results/ui-crawl-screenshots` | output dir |
| `INTENT_LLM_URL` / `INTENT_LLM_MODEL` | Ollama `qwen2.5:7b` | per-route intent classifier |

After the crawl, both `UIPage` rows and `UIRouteSnapshot` rows for each route exist; if a BASELINE + a NEW_STRUCTURE snapshot coexist, `UIChangeLog` rows record the diff.

Verify a route:

```bash
curl -s "http://127.0.0.1:8000/ui-knowledge/change-status/?route=/login&failed_selector=%23username&use_of_selector=login-username"
```

---

## 5. Watch healing in action

1. Break a selector deliberately: edit `pages/HomePage.ts` and change `'#loginButton'` to e.g. `'#loginButtonZZZ'`. Save.
2. Run the booking test in headed mode:
   ```bash
   HEADLESS=false RUN_ID=demo npx playwright test -g "Validate Carnival"
   ```
3. The test will fail the original locator twice (3 s, 5 s), then call `/api/heal/`. Watch Django console — you'll see one POST per failed selector. The Playwright report will carry a `healing-log` text attachment with the full reasoning.
4. Inspect what Django saw:
   ```bash
   open "http://127.0.0.1:8000/admin/curertestai/healerrequest/"
   ```
5. Restore the original selector when done.

To force a fresh heal (skip the cache) for an entire run, set `USE_HEALING_CACHE=false` on the Django process. To skip per-request, the TS client already does this on cache-failure retry; from `curl`, add `"skip_cache": true` to the body.

---

## 6. Push a run to Jira manually

Page 4 of the Streamlit UI handles this; from the CLI, replicate with `streamlet-ui/utils/api_client.py::push_comment_to_jira()`. The minimum is:

```bash
ISSUE=XX-94
JIRA_URL=https://xebiaww.atlassian.net
EMAIL=arvind.kumar1@xebia.com
TOKEN=...

# Pre-flight
curl -u "$EMAIL:$TOKEN" -s "$JIRA_URL/rest/api/3/issue/$ISSUE?fields=key,status"

# Comment (body must be Atlassian Document Format)
curl -u "$EMAIL:$TOKEN" -X POST "$JIRA_URL/rest/api/3/issue/$ISSUE/comment" \
  -H 'Content-Type: application/json' \
  -d @comment.json

# Attachment
curl -u "$EMAIL:$TOKEN" -X POST "$JIRA_URL/rest/api/3/issue/$ISSUE/attachments" \
  -H 'X-Atlassian-Token: no-check' \
  -F "file=@tests/generated/login.spec.ts"
```

---

## 7. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `axios ECONNREFUSED 127.0.0.1:8000` during tests | Django not running | start it in terminal 1 |
| `401` on `/api/heal/` | JWT expired or `auth.ts` creds wrong | `apiClient.ts` auto-retries once; if still failing, re-check user + `client_secret` |
| `validation_status: "NO_SAFE_MATCH"` | top score < 0.10 — no candidate is safe | check `intent_policies.json`, supply a better `intent_key`, or refresh UI baseline |
| `CABIN_OFFER_CONTINUE_FAILED` thrown twice | both users hit a transient site issue | retry the run; check if Carnival changed the offers flow |
| Streamlit Page 2 hangs on Generate | Ollama not reachable | start `ollama serve`, ensure `qwen2.5:7b` is pulled; or set `USE_TEST_GEN=false` and disable that page |
| Materialize fails with "file exists" | prior generated content blocks overwrite | pass `allow_overwrite:true` (Streamlit already does); manually delete `tests/generated/<file>` if stuck |
| Crawl screenshots missing | `UI_SCREENSHOT_DIR` not writable | create the dir or unset it (screenshots become optional) |
| Jira push 403 | API token doesn't have permission on the project | regenerate token, paste into Page 0 |
| Healing report attachment empty | `healingReport` fixture not auto-injected — page object did not call `appendHealingReportLog` | use `selfHealingClick` (logs are automatic) or call `appendHealingReportLog(testInfo, msg)` directly |

---

## 8. Tearing down

```bash
# Streamlit + Chrome were started by start_apps.command:
lsof -ti:8501 | xargs kill -9 2>/dev/null

# Django: Ctrl-C its terminal.
# Ollama: Ctrl-C its terminal (or `pkill ollama`).
# MySQL: leave running, or `brew services stop mysql`.

# Clean local artifacts:
rm -rf playwright-report test-results tmp
```

Database state (heal history, generation jobs, UI baselines) persists in MySQL — drop and re-import `ai_healer_service.sql` to reset.
