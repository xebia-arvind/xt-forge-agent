from django.db import migrations, models
import django.db.models.deletion


def assign_legacy_client(apps, schema_editor):
    Clients = apps.get_model("clients", "Clients")
    GenerationJob = apps.get_model("test_generation", "GenerationJob")

    legacy = Clients.objects.filter(slug="legacy").first()
    if not legacy:
        legacy = Clients.objects.create(slug="legacy", clientname="Legacy")

    GenerationJob.objects.filter(client__isnull=True).update(client=legacy)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("test_generation", "0001_initial"),
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationjob",
            name="client",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="generation_jobs",
                to="clients.clients",
            ),
        ),
        migrations.RunPython(assign_legacy_client, noop_reverse),
    ]
