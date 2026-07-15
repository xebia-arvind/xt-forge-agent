from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("curertestai", "0007_domsnapshot"),
    ]

    operations = [
        # 1. Drop DomSnapshot entirely (table + FK to HealerRequest).
        migrations.DeleteModel(
            name="DomSnapshot",
        ),
        # 2. Convert HealerRequest.batch_id from FK(HealerRequestBatch) to a plain IntegerField.
        #    Drop the FK first so the HealerRequestBatch table is no longer referenced.
        migrations.RemoveField(
            model_name="healerrequest",
            name="batch_id",
        ),
        migrations.AddField(
            model_name="healerrequest",
            name="batch_id",
            field=models.IntegerField(default=0),
        ),
        # 3. Drop the HealerRequestBatch table.
        migrations.DeleteModel(
            name="HealerRequestBatch",
        ),
    ]
