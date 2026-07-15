"""Phase 6 — allow RunnerJob.kind = CUCUMBER."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("runners", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="runnerjob",
            name="kind",
            field=models.CharField(
                choices=[
                    ("GEN", "Generate tests"),
                    ("EXECUTE", "Execute Playwright"),
                    ("CUCUMBER", "Execute Cucumber"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
    ]
