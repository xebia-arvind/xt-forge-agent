# Generated manually to remove generation models from test_analytics state only.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("test_analytics", "0007_generation_models"),
        ("test_generation", "0001_initial_state"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name="GenerationExecutionLink"),
                migrations.DeleteModel(name="GeneratedArtifact"),
                migrations.DeleteModel(name="GenerationScenario"),
                migrations.DeleteModel(name="GenerationJob"),
            ],
        )
    ]
