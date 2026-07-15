"""Phase 6 — pipeline stages on GenerationJob + Cucumber artifact types.

Non-destructive: adds new fields with defaults, extends a choice list.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("test_generation", "0002_client_scope"),
    ]

    operations = [
        # ---- GenerationJob: new stage fields ----------------------------------
        migrations.AddField(
            model_name="generationjob",
            name="stage",
            field=models.CharField(
                choices=[
                    ("INTAKE", "Intake (Jira ticket linked)"),
                    ("FEATURE", "Feature Author output ready"),
                    ("MANUAL_TESTS", "Manual test cases ready"),
                    ("PLAN", "Plan ready"),
                    ("ARTIFACTS", "Artifacts generated"),
                    ("EXECUTE", "Executor running"),
                    ("REPORT", "Ready to push to Jira"),
                    ("DONE", "Pushed to Jira"),
                    ("HUMAN_REVIEW_NEEDED", "Human review needed"),
                ],
                db_index=True,
                default="INTAKE",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="stage_feature_output",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="stage_manual_tests_output",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="stage_plan_output",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="stage_execute_output",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="stage_history",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="execute_iteration",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="jira_issue_key",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        # ---- GeneratedArtifact: new artifact_type choices ---------------------
        # Only alters the choice list (no schema change in the DB — CharField
        # column stays the same width). Django still records it as an AlterField.
        migrations.AlterField(
            model_name="generatedartifact",
            name="artifact_type",
            field=models.CharField(
                choices=[
                    ("PAGE_OBJECT", "Page Object"),
                    ("SPEC", "Spec (legacy)"),
                    ("FEATURE", "Gherkin .feature"),
                    ("STEP_DEFINITIONS", "Cucumber step definitions"),
                ],
                max_length=32,
            ),
        ),
    ]
