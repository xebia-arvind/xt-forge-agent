# Data Flow

Three primary flows. Each has a sequence diagram and a per-step list of HTTP calls / DB writes / disk writes.

---

## Flow A — Self-healing click during a test

Triggered any time `selfHealingClick()` is invoked from a page object (today: only `HomePage.clickSignIn()`).

```
Test (Node)        wraper-healer            Django (:8000)              MySQL          Ollama
   │                   │                         │                        │              │
   │ clickSignIn(ti)   │                         │                        │              │
   │──────────────────▶│                         │                        │              │
   │                   │ setFailureContext()     │                        │              │
   │                   │ locator.click(3s) ❌   │                        │              │
   │                   │ locator.click(5s) ❌   │                        │              │
   │                   │                         │                        │              │
   │                   │ getAccessToken()        │                        │              │
   │                   │ ─── POST /auth/login ──▶│ verify user+client     │              │
   │                   │ ◀────── {access} ───────│                        │              │
   │                   │                         │                        │              │
   │                   │ ─── POST /api/heal/ ───▶│ HealAPIView            │              │
   │                   │     Bearer …            │ ├ cache lookup ───────▶ HealerRequest │
   │                   │     {failed_selector,   │ ├ DOMExtractor         │              │
   │                   │      html, screenshot,  │ ├ MatchingEngine       │              │
   │                   │      page_url, intent}  │ │   SBERT + FAISS      │              │
   │                   │                         │ ├ validation_engine    │              │
   │                   │                         │ │   intent_policies    │              │
   │                   │                         │ │   history boost ─────▶ HealerRequest│
   │                   │                         │ │   LLM score ─── POST /api/generate ─▶│
   │                   │                         │ │                     ◀─ {score} ─────│
   │                   │                         │ ├ detect_ui_change                    │
   │                   │                         │ └ persist ───────────▶ HealerRequest   │
   │                   │                         │                        SuggestedSel(x5)│
   │                   │ ◀──── HealResponse ─────│                                       │
   │                   │      {chosen, candidates[], validation_status,                  │
   │                   │       debug{cache_hit, engine, ...}, ui_change_level}           │
   │                   │                                                                 │
   │                   │ clickUsingResolvedSelector(chosen)                              │
   │                   │   if page.locator(chosen).count()===1 → click CSS               │
   │                   │   else → click XPath fallback                                   │
   │                   │ addStepEvent({status:"HEALED"})                                 │
   │                   │ appendHealingReportLog(...)                                     │
   │ ◀── return ───────│                                                                 │
   │                                                                                     │
   │ (test proceeds)                                                                     │
```

### Cache-fallback branch

If `HealResponse.debug.cache_hit === true` (or `engine === "history_cache"`) **and** the click with the cached selector then fails, `selfHealing.ts` issues a second `POST /api/heal/` with `skip_cache: true` and retries with the fresh result. `cacheFallbackToFresh = true` is recorded in the failure context.

### Final aggregation (every test, not just failures)

```
   │ test ends                                                                            │
   │ afterEach (baseTest.ts):                                                             │
   │   - merge tracked failureContext + parseFailureFromError                             │
   │   - bundle attachments (screenshot/video/trace from testInfo.attachments)            │
   │   ─── POST /test-analytics/test-result/ ─────────▶ PlaywrightResultAPIView           │
   │       (no auth header)                            classifier.classify_failure()     │
   │       full payload (see below)                    persist ─────▶ TestRun           │
   │                                                                  TestCaseResult     │
   │ healingReport fixture:                                                              │
   │   consumeHealingReportLogs(testInfo)                                                │
   │   testInfo.attach('healing-log', body=…)                                            │
```

Payload sent to `/test-analytics/test-result/` (every test):

```jsonc
{
  "run_id": "1234",
  "build_id": "<sha or generated>",
  "environment": "staging",
  "run_execution_time": 23456,

  "test_name": "Validate Carnival cruise booking flow until checkout",
  "status": "FAILED",
  "error_message": "…",
  "stack_trace": "…",

  "page_url": "https://www.carnival.com/...",
  "failed_selector": "#loginButton",
  "failure_reason": "Timeout 5000ms exceeded",

  "healing_attempted": true,
  "healing_outcome": "SUCCESS",
  "healed_selector": "button[aria-label='Sign In']",
  "healing_confidence": 0.81,
  "validation_status": "VALID",
  "ui_change_level": "MINOR_CHANGE",
  "history_assisted": true,
  "history_hits": 4,
  "cache_hit": true,
  "cache_fallback_to_fresh": false,
  "root_cause": "Original locator failed within 5000ms",

  "step_events": [
    {"step_name":"…","step_type":"action","status":"HEALED","timestamp":"…"}
  ],

  "html": "<full page html at failure>",
  "screenshot_path": "test-results/.../trace.png",
  "video_path": "test-results/.../video.webm",
  "trace_path":  "test-results/.../trace.zip"
}
```

DB writes per failure: 1 × `HealerRequest`, up to 5 × `SuggestedSelector`, 1 × `TestRun` (upsert by `run_id`), 1 × `TestCaseResult`. *(Phase 2 dropped the `DomSnapshot` write.)*

---

## Flow B — Test generation (Streamlit → Django → disk)

```
Streamlit Page 0─Page 4                Node CLI                   Django                        Ollama
        │                                 │                          │                            │
        │ user clicks "Run autonomous"    │                          │                            │
        │ → set auto_flow.current_stage="validate"                   │                            │
        │                                                            │                            │
Page 1  │ load feature_requests.json (fs read)                       │                            │
        │ user edits → save_feature_requests() (fs write)            │                            │
        │ auto_flow.current_stage="generate"                         │                            │
        │                                                            │                            │
Page 2  │ subprocess.run(["npm","run","gen:testcases"], cwd=root)   │                            │
        │   ───────────────────────────▶│ runGenerationFromFile.mjs                              │
        │                                │ for each job in feature_requests.json:                │
        │                                │   ─── POST /test-generation/jobs/ ────▶│              │
        │                                │      {feature_name, feature_description,             │
        │                                │       seed_urls, coverage_mode,                       │
        │                                │       max_scenarios, max_routes,                      │
        │                                │       base_url, intent_hints,                         │
        │                                │       manual_scenarios?}                              │
        │                                │                                       │              │
        │                                │                                       │ generation_service.generate_job_draft()│
        │                                │                                       │  ─── POST /api/generate (qwen2.5:7b) ─▶│
        │                                │                                       │                                      │ scenarios, page-objects, specs
        │                                │                                       │ ◀─── {scenarios[], artifacts[]} ─────│
        │                                │                                       │ persist:                              │
        │                                │                                       │   GenerationJob (status DRAFT_READY)  │
        │                                │                                       │   GenerationScenario (×N)             │
        │                                │                                       │   GeneratedArtifact   (×M, content_draft)│
        │                                │ ◀───── {job_id, status} ──────────────│                                      │
        │                                │ ─── GET /test-generation/jobs/{id}/ ─▶│                                      │
        │                                │ ◀───── job detail ────────────────────│                                      │
        │   ◀── stdout streamed ─────────│                                                                              │
        │ auto_flow.current_stage="review_materialize"                                                                  │
        │                                                                                                                │
Page 3  │ get_jobs() ── GET /test-generation/jobs/ ───────────────▶│                                                    │
        │ approve_job(id) ── POST .../approve/ ─────────────────────▶│ GenerationJob.job_status=APPROVED                │
        │                                                            │                                                  │
        │ materialize_job(id) ── POST .../materialize/{allow_overwrite:true}─▶│                                          │
        │                                                            │ for each GeneratedArtifact:                      │
        │                                                            │   write tests/generated/<file>.spec.ts            │
        │                                                            │   write tests/pages/generated/<file>.ts           │
        │                                                            │   content_final = content_draft                   │
        │                                                            │   checksum, validation_status                     │
        │                                                            │ GenerationJob.job_status=MATERIALIZED             │
        │ ◀──── {paths[]} ───────────────────────────────────────────│                                                  │
        │ list_generated_tests(project_root) (fs scan)                                                                  │
        │ preview .spec.ts / .ts files                                                                                   │
```

DB writes per generation job: 1 × `GenerationJob`, N × `GenerationScenario`, M × `GeneratedArtifact`. On materialize the `content_final` column is filled and files appear on disk.

Disk writes during materialize:

- `tests/generated/<feature>.spec.ts`
- `tests/pages/generated/<PageName>.ts` *(note: agent referred to "pages/generated/" but the actual layout used by the Streamlit scanner is `tests/pages/generated/`)*

---

## Flow C — UI knowledge sync (`npm run sync:ui`)

Used to capture or refresh baselines that the healing pipeline uses as retrieval evidence.

```
Operator                                  Node CLI                          Django                 MySQL
   │                                         │                                 │                     │
   │ BASE_URL=… SEED_URLS=… npm run sync:ui  │                                 │                     │
   │ ───────────────────────────────────────▶│ syncUiKnowledge.mjs            │                     │
   │                                         │  spawn crawlContext.mjs (Playwright)                 │
   │                                         │   BFS from SEED_URLS, depth ≤ MAX_DEPTH              │
   │                                         │   for each route:                                    │
   │                                         │     extract interactables (selector_hints[] ranked)  │
   │                                         │     extract forms[], links[]                         │
   │                                         │     compute dom_hash                                 │
   │                                         │     fullPage screenshot → UI_SCREENSHOT_DIR          │
   │                                         │  ◀── routes[] over stdout (JSON) ──                 │
   │                                         │                                                       │
   │                                         │  optional intent classification:                      │
   │                                         │   for each route:                                     │
   │                                         │     ─── POST {INTENT_LLM_URL} ──▶ Ollama qwen2.5:7b   │
   │                                         │     ◀── {feature_name, intents[{idx,intent_key}]} ──  │
   │                                         │                                                       │
   │                                         │  for each route:                                      │
   │                                         │    ─── POST /ui-knowledge/sync/ ─▶│ UISnapshotCreateAPI │
   │                                         │       {route, title, feature_name,                    │
   │                                         │        snapshot_type:"BASELINE",                      │
   │                                         │        dom_hash, snapshot_json,                       │
   │                                         │        elements:[{selector, tag, role,                │
   │                                         │                   text, test_id, intent_key}],        │
   │                                         │        screenshot_path}                               │
   │                                         │                                  │ upsert UIPage     │
   │                                         │                                  │ new UIRouteSnapshot│
   │                                         │                                  │ ×N UIElement      │
   │                                         │                                  │ UIScreenshot      │
   │                                         │                                  │ if baseline+new exist:│
   │                                         │                                  │   compare_snapshots()│
   │                                         │                                  │   → UIChangeLog    │
   │                                         │ ◀── {ok, elements_count} ────────│                   │
   │ ◀── stdout summary (success=…, failed=…)                                                       │
```

The crawler ranks selector hints inside `crawlContext.mjs`: `data-testid (100) > id (90) > aria-label (80) > name (…) > class (…) > tag+text`. The first hint is the canonical selector stored on `UIElement.selector`.

---

## Flow D — Result push to Jira

Only happens after Page 4 finishes running Playwright.

```
Streamlit Page 4                                       Jira Cloud (REST v3)
        │                                                    │
        │ parse_playwright_results(project_root):             │
        │   read playwright-report/results.json               │
        │   compute pass/fail counts                          │
        │                                                     │
        │ validate_jira_issue_access (1–3 attempts, backoff)  │
        │ ─── GET /rest/api/3/issue/{key}?fields=key,status ─▶│
        │ ◀── 200 ────────────────────────────────────────────│
        │                                                     │
        │ push_comment_to_jira:                               │
        │ ─── POST /rest/api/3/issue/{key}/comment ──────────▶│
        │     {body: {type:"doc", version:1, content:[ADF]}}  │
        │     auth=HTTPBasicAuth(email, api_token)            │
        │                                                     │
        │ for each attachment (spec, page-object, exec log):  │
        │ ─── POST /rest/api/3/issue/{key}/attachments ──────▶│
        │     X-Atlassian-Token: no-check                     │
        │     multipart file                                  │
        │                                                     │
        │ cleanup: rm /tmp/{key}_execution_*.log              │
```

ADF (Atlassian Document Format) is constructed in `streamlet-ui/utils/process_runner.py::generate_jira_rich_report_content()`.

---

## Cross-flow guarantees & invariants

- **JWT cache lifetime** = Playwright process lifetime (`wraper-healer/auth.ts:cachedToken`). On HTTP 401, `apiClient.ts` clears it and re-authenticates once.
- **Failure context lifetime** = single test (cleared in `baseTest.ts` afterEach). It is a `Map<testId, FailureContext>` keyed by Playwright's internal test id.
- **UI-change severity** is monotonic per test: `setFailureContext` keeps `ELEMENT_REMOVED > MAJOR_CHANGE > MINOR_CHANGE > UNCHANGED`.
- **Generation job state machine** is strictly forward: `DRAFTING → DRAFT_READY → APPROVED → MATERIALIZED`. `REJECTED` and `FAILED` are terminal alternates.
- **Healing cache key** = `(page_url, use_of_selector, failed_selector, intent_key)`; only successful past heals within `MAX_AGE_DAYS` and above `MIN_CONFIDENCE` qualify.
- **Test results are POSTed for every test by default**, not only failures. Set `SAVE_ONLY_FAILED=true` to opt into a slimmer ingest stream.
