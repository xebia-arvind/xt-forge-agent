from rest_framework import serializers

from .models import JiraConnection


class JiraConnectionSerializer(serializers.ModelSerializer):
    """
    Write-only for `api_token` (never returned). Read shape shows the connection
    without the secret.
    """
    api_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = JiraConnection
        fields = ("id", "base_url", "email", "display_name", "api_token", "last_modified")
        read_only_fields = ("id", "last_modified")

    def create(self, validated_data):
        plaintext = validated_data.pop("api_token", "")
        instance = JiraConnection(**validated_data)
        instance.set_api_token(plaintext)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        plaintext = validated_data.pop("api_token", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        if plaintext is not None and plaintext != "":
            instance.set_api_token(plaintext)
        instance.save()
        return instance


class JiraSearchSerializer(serializers.Serializer):
    jql = serializers.CharField(required=False, allow_blank=True, default="assignee=currentUser() ORDER BY created DESC")
    max_results = serializers.IntegerField(required=False, default=20, min_value=1, max_value=100)


class JiraCommentSerializer(serializers.Serializer):
    issue_key = serializers.CharField()
    body = serializers.CharField()
