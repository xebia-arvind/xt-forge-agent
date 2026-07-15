from django.db import models
from abstract.models import Common
from clients.models import Clients
import uuid


class GenerationJob(Common):
    COVERAGE_SMOKE_NEGATIVE = "SMOKE_NEGATIVE"
    COVERAGE_CHOICES = [
        (COVERAGE_SMOKE_NEGATIVE, "Smoke + Negative"),
    ]

    STATE_DRAFTING = "DRAFTING"
    STATE_DRAFT_READY = "DRAFT_READY"
    STATE_APPROVED = "APPROVED"
    STATE_MATERIALIZED = "MATERIALIZED"
    STATE_REJECTED = "REJECTED"
    STATE_FAILED = "FAILED"
    STATE_CHOICES = [
        (STATE_DRAFTING, "Drafting"),
        (STATE_DRAFT_READY, "Draft Ready"),
        (STATE_APPROVED, "Approved"),
        (STATE_MATERIALIZED, "Materialized"),
        (STATE_REJECTED, "Rejected"),
        (STATE_FAILED, "Failed"),
    ]

    # --- Phase 6 pipeline stages -------------------------------------------
    # `stage` tracks progression through the 6-agent pipeline. `job_status`
    # (above) stays for back-compat with legacy single-shot jobs; Phase 6 jobs
    # move through `stage` while `job_status` typically stays DRAFTING until
    # the pipeline completes (then MATERIALIZED / FAILED / etc).
    STAGE_INTAKE = "INTAKE"
    STAGE_FEATURE = "FEATURE"
    STAGE_MANUAL_TESTS = "MANUAL_TESTS"
    STAGE_PLAN = "PLAN"
    STAGE_ARTIFACTS = "ARTIFACTS"
    STAGE_EXECUTE = "EXECUTE"
    STAGE_REPORT = "REPORT"
    STAGE_DONE = "DONE"
    STAGE_HUMAN_REVIEW_NEEDED = "HUMAN_REVIEW_NEEDED"
    STAGE_CHOICES = [
        (STAGE_INTAKE, "Intake (Jira ticket linked)"),
        (STAGE_FEATURE, "Feature Author output ready"),
        (STAGE_MANUAL_TESTS, "Manual test cases ready"),
        (STAGE_PLAN, "Plan ready"),
        (STAGE_ARTIFACTS, "Artifacts generated"),
        (STAGE_EXECUTE, "Executor running"),
        (STAGE_REPORT, "Ready to push to Jira"),
        (STAGE_DONE, "Pushed to Jira"),
        (STAGE_HUMAN_REVIEW_NEEDED, "Human review needed"),
    ]

    # Tenant scope; nullable to support backfill of pre-Phase-1 rows.
    client = models.ForeignKey(
        Clients,
        on_delete=models.PROTECT,
        null=True,
        related_name="generation_jobs",
        db_index=True,
    )
    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    feature_name = models.CharField(max_length=255)
    feature_description = models.TextField()
    seed_urls = models.JSONField(default=list, blank=True)
    # Free-form JSON blob for test preconditions extracted from the Jira story
    # (HTTP Basic Auth, API-flow user creation, seeded data, etc.). Executor
    # reads well-known keys and injects them into the Cucumber env:
    #   {"http_basic": {"username": "…", "password": "…"},
    #    "seed_year":  "1990",
    #    "notes":      ["free-form reviewer remark"]}
    preconditions = models.JSONField(default=dict, blank=True)
    intent_hints = models.JSONField(default=list, blank=True)
    coverage_mode = models.CharField(
        max_length=32,
        choices=COVERAGE_CHOICES,
        default=COVERAGE_SMOKE_NEGATIVE,
    )
    max_scenarios = models.PositiveIntegerField(default=8)
    max_routes = models.PositiveIntegerField(default=20)
    base_url = models.URLField(default="http://localhost:3000")
    job_status = models.CharField(
        max_length=32,
        choices=STATE_CHOICES,
        default=STATE_DRAFTING,
        db_index=True,
    )
    llm_model = models.CharField(max_length=128, default="qwen2.5-coder:7b")
    llm_temperature = models.FloatField(default=0.0)
    crawl_summary = models.JSONField(default=dict, blank=True)
    feature_summary = models.TextField(blank=True, default="")
    llm_notes = models.JSONField(default=list, blank=True)
    validation_summary = models.JSONField(default=dict, blank=True)
    materialized_manifest = models.JSONField(default=list, blank=True)
    approved_by = models.CharField(max_length=255, blank=True, default="")
    approved_notes = models.TextField(blank=True, default="")
    rejected_reason = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_by = models.CharField(max_length=255, blank=True, default="")
    drafting_started_on = models.DateTimeField(null=True, blank=True)
    drafting_finished_on = models.DateTimeField(null=True, blank=True)
    materialized_on = models.DateTimeField(null=True, blank=True)

    # --- Phase 6 pipeline persistence --------------------------------------
    # Current stage in the 6-agent pipeline. Defaults to INTAKE for new Phase-6
    # jobs; legacy single-shot jobs simply never leave INTAKE.
    stage = models.CharField(
        max_length=32,
        choices=STAGE_CHOICES,
        default=STAGE_INTAKE,
        db_index=True,
    )
    # Raw output of each stage's agent — kept for audit and re-runs. Nullable
    # because stages populate them in order.
    stage_feature_output = models.JSONField(null=True, blank=True)
    stage_manual_tests_output = models.JSONField(null=True, blank=True)
    stage_plan_output = models.JSONField(null=True, blank=True)
    stage_execute_output = models.JSONField(null=True, blank=True)
    # Append-only audit log of stage transitions & reviewer decisions.
    # Each entry: {stage, agent, started_on, finished_on, decision, reviewer, notes}
    stage_history = models.JSONField(default=list, blank=True)
    # Bumped by the Executor on each auto-retry; hard-capped at 3.
    execute_iteration = models.PositiveIntegerField(default=0)
    # Link back to the Jira issue that seeded this job (from the Worklist panel).
    jira_issue_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    class Meta:
        db_table = "test_generation_generationjob"

    def __str__(self):
        return f"{self.feature_name} | {self.job_id}"


class GenerationScenario(Common):
    TYPE_SMOKE = "SMOKE"
    TYPE_NEGATIVE = "NEGATIVE"
    TYPE_CHOICES = [
        (TYPE_SMOKE, "Smoke"),
        (TYPE_NEGATIVE, "Negative"),
    ]

    job = models.ForeignKey(
        GenerationJob,
        on_delete=models.CASCADE,
        related_name="scenarios",
    )
    scenario_id = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=255)
    scenario_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default=TYPE_SMOKE)
    priority = models.PositiveIntegerField(default=1)
    preconditions = models.JSONField(default=list, blank=True)
    steps = models.JSONField(default=list, blank=True)
    expected_assertions = models.JSONField(default=list, blank=True)
    selected_for_materialization = models.BooleanField(default=True)

    class Meta:
        db_table = "test_generation_generationscenario"
        unique_together = ("job", "scenario_id")

    def __str__(self):
        return f"{self.job_id} | {self.scenario_id}"


class GeneratedArtifact(Common):
    TYPE_PAGE_OBJECT = "PAGE_OBJECT"
    TYPE_SPEC = "SPEC"
    # Phase 6 additions — Cucumber output.
    TYPE_FEATURE = "FEATURE"
    TYPE_STEP_DEFINITIONS = "STEP_DEFINITIONS"
    TYPE_CHOICES = [
        (TYPE_PAGE_OBJECT, "Page Object"),
        (TYPE_SPEC, "Spec (legacy)"),
        (TYPE_FEATURE, "Gherkin .feature"),
        (TYPE_STEP_DEFINITIONS, "Cucumber step definitions"),
    ]

    VALID = "VALID"
    INVALID = "INVALID"
    VALIDATION_CHOICES = [
        (VALID, "Valid"),
        (INVALID, "Invalid"),
    ]

    job = models.ForeignKey(
        GenerationJob,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    artifact_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    relative_path = models.CharField(max_length=512)
    content_draft = models.TextField(default="", blank=True)
    content_final = models.TextField(default="", blank=True)
    checksum = models.CharField(max_length=64, blank=True, default="")
    validation_status = models.CharField(
        max_length=16,
        choices=VALIDATION_CHOICES,
        default=VALID,
    )
    validation_errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "test_generation_generatedartifact"
        unique_together = ("job", "relative_path")

    def __str__(self):
        return f"{self.artifact_type} | {self.relative_path}"


class GenerationExecutionLink(Common):
    job = models.ForeignKey(
        GenerationJob,
        on_delete=models.CASCADE,
        related_name="execution_links",
    )
    test_run = models.ForeignKey(
        "test_analytics.TestRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generation_links",
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "test_generation_generationexecutionlink"

    def __str__(self):
        return f"{self.job_id} -> {self.test_run_id or 'NA'}"
