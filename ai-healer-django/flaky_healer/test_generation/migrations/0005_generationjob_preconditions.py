"""Add `preconditions` JSONField to GenerationJob.

Test preconditions extracted from the Jira story (HTTP Basic Auth, API-flow
user creation, seeded data, …). Read by the Executor at Cucumber-spawn time
and injected into the subprocess env. Additive only.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("test_generation", "0004_default_model_coder"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationjob",
            name="preconditions",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
