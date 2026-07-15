from rest_framework import serializers

from .models import (
    GenerationJob,
    GenerationScenario,
    GeneratedArtifact,
    GenerationExecutionLink,
)


class GenerationJobCreateSerializer(serializers.Serializer):
    feature_name = serializers.CharField(max_length=255)
    feature_description = serializers.CharField()
    seed_urls = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    coverage_mode = serializers.ChoiceField(
        choices=[GenerationJob.COVERAGE_SMOKE_NEGATIVE],
        default=GenerationJob.COVERAGE_SMOKE_NEGATIVE,
    )
    max_scenarios = serializers.IntegerField(required=False, min_value=1, max_value=30, default=8)
    max_routes = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)
    base_url = serializers.URLField(required=False, default="http://localhost:3000")
    intent_hints = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    created_by = serializers.CharField(required=False, allow_blank=True, default="")
    manual_scenarios = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )


class GenerationJobApproveSerializer(serializers.Serializer):
    approved_by = serializers.CharField(max_length=255)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    include_scenario_ids = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )
    exclude_scenario_ids = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )


class GenerationJobRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class GenerationJobMaterializeSerializer(serializers.Serializer):
    allow_overwrite = serializers.BooleanField(required=False, default=False)


class GenerationJobLinkRunSerializer(serializers.Serializer):
    run_id = serializers.CharField(max_length=100)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class GenerationJobArtifactUpdateSerializer(serializers.Serializer):
    relative_path = serializers.CharField(max_length=512)
    content = serializers.CharField()
    update_draft = serializers.BooleanField(required=False, default=True)


class GenerationScenarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = GenerationScenario
        fields = [
            "scenario_id",
            "title",
            "scenario_type",
            "priority",
            "preconditions",
            "steps",
            "expected_assertions",
            "selected_for_materialization",
        ]


class GeneratedArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneratedArtifact
        fields = [
            "artifact_type",
            "relative_path",
            "content_draft",
            "content_final",
            "checksum",
            "validation_status",
            "validation_errors",
            "warnings",
        ]


class GenerationExecutionLinkSerializer(serializers.ModelSerializer):
    run_id = serializers.CharField(source="test_run.run_id", read_only=True)

    class Meta:
        model = GenerationExecutionLink
        fields = ["id", "run_id", "notes", "created_on"]


class GenerationJobDetailSerializer(serializers.ModelSerializer):
    scenarios = GenerationScenarioSerializer(many=True, read_only=True)
    artifacts = GeneratedArtifactSerializer(many=True, read_only=True)
    execution_links = GenerationExecutionLinkSerializer(many=True, read_only=True)
    status = serializers.CharField(source="job_status", read_only=True)

    class Meta:
        model = GenerationJob
        fields = [
            "job_id",
            "feature_name",
            "feature_description",
            "seed_urls",
            "intent_hints",
            "coverage_mode",
            "max_scenarios",
            "max_routes",
            "base_url",
            "status",
            "llm_model",
            "llm_temperature",
            "crawl_summary",
            "feature_summary",
            "llm_notes",
            "validation_summary",
            "materialized_manifest",
            "approved_by",
            "approved_notes",
            "rejected_reason",
            "error_message",
            "created_by",
            "drafting_started_on",
            "drafting_finished_on",
            "materialized_on",
            "created_on",
            "last_modified",
            "scenarios",
            "artifacts",
            "execution_links",
            # --- Phase 6 pipeline state (needed by the review panels) ---
            "stage",
            "stage_feature_output",
            "stage_manual_tests_output",
            "stage_plan_output",
            "stage_execute_output",
            "stage_history",
            "execute_iteration",
            "jira_issue_key",
        ]
