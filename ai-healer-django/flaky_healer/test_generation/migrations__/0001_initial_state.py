# Generated manually for safe app split from test_analytics

import django.db.models.deletion
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("test_analytics", "0007_generation_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="GenerationJob",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_on", models.DateTimeField(auto_now_add=True)),
                        ("last_modified", models.DateTimeField(auto_now=True)),
                        ("status", models.CharField(choices=[("a", "Active"), ("i", "Inactive")], db_index=True, default="a", max_length=1)),
                        ("is_deleted", models.BooleanField(default=False)),
                        ("deleted_on", models.DateTimeField(blank=True, null=True)),
                        ("job_id", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                        ("feature_name", models.CharField(max_length=255)),
                        ("feature_description", models.TextField()),
                        ("seed_urls", models.JSONField(blank=True, default=list)),
                        ("intent_hints", models.JSONField(blank=True, default=list)),
                        ("coverage_mode", models.CharField(choices=[("SMOKE_NEGATIVE", "Smoke + Negative")], default="SMOKE_NEGATIVE", max_length=32)),
                        ("max_scenarios", models.PositiveIntegerField(default=8)),
                        ("max_routes", models.PositiveIntegerField(default=20)),
                        ("base_url", models.URLField(default="http://localhost:3000")),
                        ("job_status", models.CharField(choices=[("DRAFTING", "Drafting"), ("DRAFT_READY", "Draft Ready"), ("APPROVED", "Approved"), ("MATERIALIZED", "Materialized"), ("REJECTED", "Rejected"), ("FAILED", "Failed")], db_index=True, default="DRAFTING", max_length=32)),
                        ("llm_model", models.CharField(default="qwen2.5:7b", max_length=128)),
                        ("llm_temperature", models.FloatField(default=0.0)),
                        ("crawl_summary", models.JSONField(blank=True, default=dict)),
                        ("feature_summary", models.TextField(blank=True, default="")),
                        ("llm_notes", models.JSONField(blank=True, default=list)),
                        ("validation_summary", models.JSONField(blank=True, default=dict)),
                        ("materialized_manifest", models.JSONField(blank=True, default=list)),
                        ("approved_by", models.CharField(blank=True, default="", max_length=255)),
                        ("approved_notes", models.TextField(blank=True, default="")),
                        ("rejected_reason", models.TextField(blank=True, default="")),
                        ("error_message", models.TextField(blank=True, default="")),
                        ("created_by", models.CharField(blank=True, default="", max_length=255)),
                        ("drafting_started_on", models.DateTimeField(blank=True, null=True)),
                        ("drafting_finished_on", models.DateTimeField(blank=True, null=True)),
                        ("materialized_on", models.DateTimeField(blank=True, null=True)),
                    ],
                    options={
                        "ordering": ["-created_on"],
                        "db_table": "test_analytics_generationjob",
                    },
                ),
                migrations.CreateModel(
                    name="GenerationExecutionLink",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_on", models.DateTimeField(auto_now_add=True)),
                        ("last_modified", models.DateTimeField(auto_now=True)),
                        ("status", models.CharField(choices=[("a", "Active"), ("i", "Inactive")], db_index=True, default="a", max_length=1)),
                        ("is_deleted", models.BooleanField(default=False)),
                        ("deleted_on", models.DateTimeField(blank=True, null=True)),
                        ("notes", models.TextField(blank=True, default="")),
                        ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="execution_links", to="test_generation.generationjob")),
                        ("test_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generation_links", to="test_analytics.testrun")),
                    ],
                    options={
                        "ordering": ["-created_on"],
                        "db_table": "test_analytics_generationexecutionlink",
                    },
                ),
                migrations.CreateModel(
                    name="GeneratedArtifact",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_on", models.DateTimeField(auto_now_add=True)),
                        ("last_modified", models.DateTimeField(auto_now=True)),
                        ("status", models.CharField(choices=[("a", "Active"), ("i", "Inactive")], db_index=True, default="a", max_length=1)),
                        ("is_deleted", models.BooleanField(default=False)),
                        ("deleted_on", models.DateTimeField(blank=True, null=True)),
                        ("artifact_type", models.CharField(choices=[("PAGE_OBJECT", "Page Object"), ("SPEC", "Spec")], max_length=32)),
                        ("relative_path", models.CharField(max_length=512)),
                        ("content_draft", models.TextField(blank=True, default="")),
                        ("content_final", models.TextField(blank=True, default="")),
                        ("checksum", models.CharField(blank=True, default="", max_length=64)),
                        ("validation_status", models.CharField(choices=[("VALID", "Valid"), ("INVALID", "Invalid")], default="VALID", max_length=16)),
                        ("validation_errors", models.JSONField(blank=True, default=list)),
                        ("warnings", models.JSONField(blank=True, default=list)),
                        ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="artifacts", to="test_generation.generationjob")),
                    ],
                    options={
                        "ordering": ["-created_on"],
                        "db_table": "test_analytics_generatedartifact",
                        "unique_together": {("job", "relative_path")},
                    },
                ),
                migrations.CreateModel(
                    name="GenerationScenario",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_on", models.DateTimeField(auto_now_add=True)),
                        ("last_modified", models.DateTimeField(auto_now=True)),
                        ("status", models.CharField(choices=[("a", "Active"), ("i", "Inactive")], db_index=True, default="a", max_length=1)),
                        ("is_deleted", models.BooleanField(default=False)),
                        ("deleted_on", models.DateTimeField(blank=True, null=True)),
                        ("scenario_id", models.CharField(db_index=True, max_length=64)),
                        ("title", models.CharField(max_length=255)),
                        ("scenario_type", models.CharField(choices=[("SMOKE", "Smoke"), ("NEGATIVE", "Negative")], default="SMOKE", max_length=32)),
                        ("priority", models.PositiveIntegerField(default=1)),
                        ("preconditions", models.JSONField(blank=True, default=list)),
                        ("steps", models.JSONField(blank=True, default=list)),
                        ("expected_assertions", models.JSONField(blank=True, default=list)),
                        ("selected_for_materialization", models.BooleanField(default=True)),
                        ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scenarios", to="test_generation.generationjob")),
                    ],
                    options={
                        "ordering": ["-created_on"],
                        "db_table": "test_analytics_generationscenario",
                        "unique_together": {("job", "scenario_id")},
                    },
                ),
            ],
        )
    ]
