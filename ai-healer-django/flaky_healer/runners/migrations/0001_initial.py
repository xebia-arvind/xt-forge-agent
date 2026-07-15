from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        migrations.CreateModel(
            name="RunnerJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("last_modified", models.DateTimeField(auto_now=True)),
                # abstract.Common.status (a/i lifecycle marker) — kept separate from the runtime state below.
                ("status", models.CharField(
                    choices=[("a", "Active"), ("i", "Inactive")],
                    db_index=True,
                    default="a",
                    max_length=1,
                )),
                ("is_deleted", models.BooleanField(default=False)),
                ("deleted_on", models.DateTimeField(blank=True, null=True)),
                ("state", models.CharField(
                    choices=[
                        ("QUEUED", "Queued"),
                        ("RUNNING", "Running"),
                        ("SUCCESS", "Success"),
                        ("FAILED", "Failed"),
                        ("CANCELLED", "Cancelled"),
                    ],
                    db_index=True,
                    default="QUEUED",
                    max_length=16,
                )),
                ("kind", models.CharField(
                    choices=[("GEN", "Generate tests"), ("EXECUTE", "Execute Playwright")],
                    db_index=True,
                    max_length=16,
                )),
                ("argv", models.JSONField(default=list)),
                ("cwd", models.CharField(default="", max_length=1024)),
                ("env_overrides", models.JSONField(blank=True, default=dict)),
                ("log_path", models.CharField(default="", max_length=512)),
                ("return_code", models.IntegerField(blank=True, null=True)),
                ("started_on", models.DateTimeField(blank=True, null=True)),
                ("finished_on", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, default="")),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="runner_jobs",
                        to="clients.clients",
                    ),
                ),
            ],
            options={"ordering": ("-created_on",)},
        ),
    ]
