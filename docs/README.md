# Playwright Carnival Framework — Deep Documentation

This folder contains a deep, code-derived walkthrough of the entire system. Read [../CLAUDE.md](../CLAUDE.md) first for the 30-second orientation; come here for the details.

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The four cooperating processes, what each one owns, the system diagram, every env var that does something, security caveats. |
| [DATA_FLOW.md](DATA_FLOW.md) | Sequence diagrams + step-by-step traces for the four primary flows: self-healing click, test generation, UI knowledge sync, Jira push. |
| [API_REFERENCE.md](API_REFERENCE.md) | Every Django endpoint with request/response shapes, plus the data model cheat-sheet. |
| [WORKFLOWS.md](WORKFLOWS.md) | How to bring the stack up, run/heal/generate/push, debug each component. |
| [UPGRADE_PLAN.md](UPGRADE_PLAN.md) | Draft plan: multi-tenancy, `curertestai` cleanup, dashboard absorbs Streamlit. |

## At a glance

```
Playwright (TS)  ──┐
                   │  wraper-healer/  ──── HTTP ───▶  Django (port 8000) ─── HTTP ───▶ Ollama (11434)
Streamlit (Py)  ───┤                                  ├─ /auth/login/             qwen2.5:7b
                   │                                  ├─ /api/heal/               · validation
Jira Cloud  ◀─── HTTP ─── Streamlit                   ├─ /test-generation/jobs/   · generation
                                                      ├─ /test-analytics/...
                                                      └─ /ui-knowledge/...
                                                            │
                                                            ▼
                                                        MySQL (ai_healer_service)
```

## Quick links by intent

**"I want to add a new page object and have it self-heal"** → [ARCHITECTURE.md § 2 wraper-healer](ARCHITECTURE.md#2-wraper-healer--the-bridge-layer) + the `HomePage.clickSignIn` reference implementation in `pages/HomePage.ts`.

**"I want to debug why a heal returned NO_SAFE_MATCH"** → [DATA_FLOW.md § Flow A](DATA_FLOW.md#flow-a--self-healing-click-during-a-test), [API_REFERENCE.md § /api/heal/](API_REFERENCE.md#post-apiheal), [WORKFLOWS.md § 5 Watch healing](WORKFLOWS.md#5-watch-healing-in-action).

**"I want to generate tests for a new feature"** → [WORKFLOWS.md § 3](WORKFLOWS.md#3-generate-new-tests-from-a-feature-request).

**"I want to refresh the UI knowledge after the site changed"** → [WORKFLOWS.md § 4](WORKFLOWS.md#4-refresh-the-ui-baseline-selectors-the-healer-learns-from).

**"Someone asked me where the credentials are hardcoded"** → [ARCHITECTURE.md § Security caveats](ARCHITECTURE.md#security-caveats-call-these-out-before-any-non-local-use).
