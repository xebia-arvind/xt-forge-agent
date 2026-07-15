from django.contrib import admin

from .models import RunnerJob


@admin.register(RunnerJob)
class RunnerJobAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "kind", "state", "return_code", "started_on", "finished_on")
    list_filter = ("kind", "state")
    readonly_fields = ("argv", "cwd", "env_overrides", "log_path", "return_code",
                       "started_on", "finished_on", "error_message", "created_on", "last_modified")
