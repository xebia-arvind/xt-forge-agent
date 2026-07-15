# Generated manually for safe physical table rename

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("test_generation", "0001_initial_state"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE IF EXISTS "test_analytics_generationjob" '
                        'RENAME TO "test_generation_generationjob";'
                    ),
                    reverse_sql=(
                        'ALTER TABLE IF EXISTS "test_generation_generationjob" '
                        'RENAME TO "test_analytics_generationjob";'
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE IF EXISTS "test_analytics_generationscenario" '
                        'RENAME TO "test_generation_generationscenario";'
                    ),
                    reverse_sql=(
                        'ALTER TABLE IF EXISTS "test_generation_generationscenario" '
                        'RENAME TO "test_analytics_generationscenario";'
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE IF EXISTS "test_analytics_generatedartifact" '
                        'RENAME TO "test_generation_generatedartifact";'
                    ),
                    reverse_sql=(
                        'ALTER TABLE IF EXISTS "test_generation_generatedartifact" '
                        'RENAME TO "test_analytics_generatedartifact";'
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE IF EXISTS "test_analytics_generationexecutionlink" '
                        'RENAME TO "test_generation_generationexecutionlink";'
                    ),
                    reverse_sql=(
                        'ALTER TABLE IF EXISTS "test_generation_generationexecutionlink" '
                        'RENAME TO "test_analytics_generationexecutionlink";'
                    ),
                ),
            ],
            state_operations=[
                migrations.AlterModelTable(
                    name="generationjob",
                    table="test_generation_generationjob",
                ),
                migrations.AlterModelTable(
                    name="generationscenario",
                    table="test_generation_generationscenario",
                ),
                migrations.AlterModelTable(
                    name="generatedartifact",
                    table="test_generation_generatedartifact",
                ),
                migrations.AlterModelTable(
                    name="generationexecutionlink",
                    table="test_generation_generationexecutionlink",
                ),
            ],
        )
    ]
