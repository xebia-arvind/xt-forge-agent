from rest_framework import serializers

from .models import RunnerJob


class RunnerJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = RunnerJob
        fields = (
            "id", "kind", "state", "argv", "cwd", "env_overrides",
            "log_path", "return_code", "started_on", "finished_on",
            "error_message", "created_on", "last_modified",
        )
        read_only_fields = fields


class GenerateRequestSerializer(serializers.Serializer):
    # No args today — `npm run gen:testcases` reads feature_requests.json from disk.
    # Reserved for future filtering (e.g. only specific job).
    pass


class ExecuteRequestSerializer(serializers.Serializer):
    spec = serializers.CharField(required=False, allow_blank=True, default="")
    grep = serializers.CharField(required=False, allow_blank=True, default="")
    workers = serializers.IntegerField(required=False, default=1, min_value=1, max_value=8)
    headed = serializers.BooleanField(required=False, default=False)
    # Optional per-run override of the BASE_URL env var Playwright reads. When
    # blank the subprocess inherits the value from the repo's .env file.
    base_url = serializers.URLField(required=False, allow_blank=True, default="")
