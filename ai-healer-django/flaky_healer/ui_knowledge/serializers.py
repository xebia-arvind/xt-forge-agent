
from rest_framework import serializers

class UIElementSerializer(serializers.Serializer):
    selector = serializers.CharField()
    tag = serializers.CharField(required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)
    text = serializers.CharField(required=False, allow_blank=True)
    test_id = serializers.CharField(required=False, allow_blank=True)
    intent_key = serializers.CharField(default="generic")


class UISnapshotSerializer(serializers.Serializer):

    route = serializers.CharField()
    title = serializers.CharField(required=False, allow_blank=True)
    feature_name = serializers.CharField(required=False, allow_blank=True)

    snapshot_type = serializers.CharField()
    dom_hash = serializers.CharField()

    screenshot_path = serializers.CharField(required=False)

    snapshot_json = serializers.JSONField()

    elements = UIElementSerializer(many=True)
