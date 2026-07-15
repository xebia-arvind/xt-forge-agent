"""
Persisted record of a background subprocess kicked off by the dashboard.

Two "kinds" today:
    - GEN       — `npm run gen:testcases` (wraps runGenerationFromFile.mjs)
    - EXECUTE   — `npx playwright test <spec>` for a materialized job

The row is created immediately in state QUEUED; the django-q2 worker updates
`status`, `return_code`, `started_on`, and `finished_on` as the subprocess runs.
Live stdout is streamed by appending to `log_path` (a plain file on disk); the
SSE endpoint tails that file.
"""
from __future__ import annotations

from django.db import models

from abstract.models import Common
from clients.models import Clients


class RunnerJob(Common):
    KIND_GEN = "GEN"
    KIND_EXECUTE = "EXECUTE"        # Playwright — legacy .spec.ts flow.
    KIND_CUCUMBER = "CUCUMBER"      # Phase 6 — Cucumber-JS Gherkin runner.
    KIND_CHOICES = [
        (KIND_GEN, "Generate tests"),
        (KIND_EXECUTE, "Execute Playwright"),
        (KIND_CUCUMBER, "Execute Cucumber"),
    ]

    STATE_QUEUED = "QUEUED"
    STATE_RUNNING = "RUNNING"
    STATE_SUCCESS = "SUCCESS"
    STATE_FAILED = "FAILED"
    STATE_CANCELLED = "CANCELLED"
    STATE_CHOICES = [
        (STATE_QUEUED, "Queued"),
        (STATE_RUNNING, "Running"),
        (STATE_SUCCESS, "Success"),
        (STATE_FAILED, "Failed"),
        (STATE_CANCELLED, "Cancelled"),
    ]

    client = models.ForeignKey(
        Clients,
        on_delete=models.PROTECT,
        related_name="runner_jobs",
        db_index=True,
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    # Runtime execution state. Named `state` so it doesn't shadow `Common.status`
    # (which is the row's soft-lifecycle a/i marker inherited from abstract.Common).
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_QUEUED, db_index=True)
    argv = models.JSONField(default=list, help_text="Command + args actually executed.")
    cwd = models.CharField(max_length=1024, default="")
    env_overrides = models.JSONField(default=dict, blank=True)
    log_path = models.CharField(max_length=512, default="")
    return_code = models.IntegerField(null=True, blank=True)
    started_on = models.DateTimeField(null=True, blank=True)
    finished_on = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-created_on",)

    def __str__(self):
        return f"{self.kind} · {self.state} · id={self.id}"
