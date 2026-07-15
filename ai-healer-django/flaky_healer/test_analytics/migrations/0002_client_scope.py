from django.db import migrations, models
import django.db.models.deletion


def assign_legacy_client(apps, schema_editor):
    Clients = apps.get_model("clients", "Clients")
    TestRun = apps.get_model("test_analytics", "TestRun")
    TestCaseResult = apps.get_model("test_analytics", "TestCaseResult")

    legacy = Clients.objects.filter(slug="legacy").first()
    if not legacy:
        legacy = Clients.objects.create(slug="legacy", clientname="Legacy")

    TestRun.objects.filter(client__isnull=True).update(client=legacy)
    TestCaseResult.objects.filter(client__isnull=True).update(client=legacy)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("test_analytics", "0001_initial"),
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        # Drop the global uniqueness on run_id so we can re-add it per-client.
        migrations.AlterField(
            model_name="testrun",
            name="run_id",
            field=models.CharField(max_length=100),
        ),
        migrations.AddField(
            model_name="testrun",
            name="client",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="test_runs",
                to="clients.clients",
            ),
        ),
        migrations.AddField(
            model_name="testcaseresult",
            name="client",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="test_case_results",
                to="clients.clients",
            ),
        ),
        migrations.RunPython(assign_legacy_client, noop_reverse),
        migrations.AlterUniqueTogether(
            name="testrun",
            unique_together={("client", "run_id")},
        ),
    ]
