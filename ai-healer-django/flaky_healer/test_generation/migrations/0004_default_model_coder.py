"""Change GenerationJob.llm_model default to qwen2.5-coder:7b.

Additive only — the default applies to NEW rows; existing rows keep whatever
model string they were created with.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("test_generation", "0003_pipeline_stages"),
    ]

    operations = [
        migrations.AlterField(
            model_name="generationjob",
            name="llm_model",
            field=models.CharField(default="qwen2.5-coder:7b", max_length=128),
        ),
    ]
