from django.db import migrations, models
import django.db.models.deletion


def assign_legacy_client(apps, schema_editor):
    Clients = apps.get_model("clients", "Clients")
    UIPage = apps.get_model("ui_knowledge", "UIPage")

    legacy = Clients.objects.filter(slug="legacy").first()
    if not legacy:
        legacy = Clients.objects.create(slug="legacy", clientname="Legacy")

    UIPage.objects.filter(client__isnull=True).update(client=legacy)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ui_knowledge", "0001_initial"),
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        # Drop the global uniqueness on route so we can re-add it per-client.
        migrations.AlterUniqueTogether(
            name="uipage",
            unique_together=set(),
        ),
        migrations.AddField(
            model_name="uipage",
            name="client",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ui_pages",
                to="clients.clients",
            ),
        ),
        migrations.RunPython(assign_legacy_client, noop_reverse),
        migrations.AlterUniqueTogether(
            name="uipage",
            unique_together={("client", "route")},
        ),
    ]
