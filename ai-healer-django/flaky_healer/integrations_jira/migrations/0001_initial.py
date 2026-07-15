from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("clients", "0004_clients_slug"),
    ]

    operations = [
        migrations.CreateModel(
            name="JiraConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("last_modified", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("a", "Active"), ("i", "Inactive")], db_index=True, default="a", max_length=1)),
                ("is_deleted", models.BooleanField(default=False)),
                ("deleted_on", models.DateTimeField(blank=True, null=True)),
                ("base_url", models.URLField(help_text="e.g. https://xebiaww.atlassian.net")),
                ("email", models.EmailField(max_length=254)),
                ("api_token_encrypted", models.TextField()),
                ("display_name", models.CharField(blank=True, default="", max_length=100)),
                (
                    "client",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="jira_connection",
                        to="clients.clients",
                    ),
                ),
            ],
            options={
                "verbose_name": "Jira Connection",
                "verbose_name_plural": "Jira Connections",
            },
        ),
    ]
