from django.db import migrations, models
from django.utils.text import slugify


LEGACY_CLIENT_NAME = "Legacy"
LEGACY_CLIENT_SLUG = "legacy"


def backfill_slugs(apps, schema_editor):
    """
    Populate slug for existing Clients rows (derived from clientname),
    and ensure a 'legacy' tenant exists so other migrations can assign it
    to pre-Phase-1 rows.
    """
    Clients = apps.get_model("clients", "Clients")

    seen = set()
    for client in Clients.objects.all():
        base = slugify(client.clientname or "") or "client"
        candidate = base[:64]
        i = 2
        while candidate in seen or Clients.objects.filter(slug=candidate).exclude(pk=client.pk).exists():
            suffix = f"-{i}"
            candidate = base[: 64 - len(suffix)] + suffix
            i += 1
        client.slug = candidate
        client.save(update_fields=["slug"])
        seen.add(candidate)

    # Ensure the legacy tenant exists for orphan-row backfill.
    Clients.objects.get_or_create(
        slug=LEGACY_CLIENT_SLUG,
        defaults={"clientname": LEGACY_CLIENT_NAME},
    )


def noop_reverse(apps, schema_editor):
    # Slug is being added; reverse migration simply drops the field.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("clients", "0003_remove_clients_user_userclient"),
    ]

    operations = [
        migrations.AddField(
            model_name="clients",
            name="slug",
            field=models.SlugField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.RunPython(backfill_slugs, noop_reverse),
    ]
