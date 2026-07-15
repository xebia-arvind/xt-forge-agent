"""Add `llm_config` JSONField to Clients (per-tenant LLM overrides).

Consumed by `test_generation.llm_backends.pick_backend` — empty dict = use
env vars / defaults. Additive only.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="clients",
            name="llm_config",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
