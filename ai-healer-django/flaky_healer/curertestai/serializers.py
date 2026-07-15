from rest_framework import serializers
from typing import Dict, Any, Optional


class HealRequestSerializer(serializers.Serializer):
    """Serializer for single heal request"""
    failed_selector = serializers.CharField(required=True)
    html = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    semantic_dom = serializers.JSONField(required=False, allow_null=True)
    use_of_selector = serializers.CharField(required=True)
    full_coverage = serializers.BooleanField(default=True)
    page_url = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    screenshot_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    selector_type = serializers.CharField(max_length=20, default="css")
    intent_key = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    skip_cache = serializers.BooleanField(required=False, default=False)
    
    def validate(self, attrs):
        """Validate that at least one of html or semantic_dom is provided"""
        if not attrs.get('html') and not attrs.get('semantic_dom'):
            raise serializers.ValidationError(
                "At least one of 'html' or 'semantic_dom' must be provided"
            )
        return attrs


class CandidateSerializer(serializers.Serializer):
    """Serializer for suggested selector candidates"""
    selector = serializers.CharField()
    score = serializers.FloatField()
    base_score = serializers.FloatField()
    attribute_score = serializers.FloatField()
    tag = serializers.CharField(allow_null=True, allow_blank=True)
    text = serializers.CharField(allow_null=True, allow_blank=True)
    xpath = serializers.CharField(allow_null=True, allow_blank=True)


class DebugInfoSerializer(serializers.Serializer):
    """Serializer for debug information"""
    total_candidates = serializers.IntegerField()
    engine = serializers.CharField()
    processing_time_ms = serializers.FloatField()
    vision_analyzed = serializers.BooleanField(default=False)
    vision_model = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    vision_success = serializers.BooleanField(required=False)
    validation_status = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    validation_reason = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    history_assisted = serializers.BooleanField(required=False)
    history_hits = serializers.IntegerField(required=False)
    retrieval_assisted = serializers.BooleanField(required=False)
    retrieval_hits = serializers.IntegerField(required=False)
    retrieved_versions = serializers.JSONField(required=False)
    dom_fingerprint = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    ui_change_level = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class HealResponseSerializer(serializers.Serializer):
    """Serializer for single heal response"""
    id = serializers.IntegerField()
    batch_id = serializers.IntegerField()
    message = serializers.CharField()
    chosen = serializers.CharField(allow_null=True)
    validation_status = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    validation_reason = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    llm_used = serializers.BooleanField(required=False)
    history_assisted = serializers.BooleanField(required=False)
    history_hits = serializers.IntegerField(required=False)
    retrieval_assisted = serializers.BooleanField(required=False)
    retrieval_hits = serializers.IntegerField(required=False)
    retrieved_versions = serializers.JSONField(required=False)
    dom_fingerprint = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    ui_change_level = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    candidates = CandidateSerializer(many=True)
    debug = DebugInfoSerializer()


class BatchHealRequestSerializer(serializers.Serializer):
    """Serializer for batch heal request"""
    selectors = HealRequestSerializer(many=True)
    
    def validate_selectors(self, value):
        """Ensure at least one selector is provided"""
        if not value:
            raise serializers.ValidationError("At least one selector must be provided")
        return value


class BatchHealResponseSerializer(serializers.Serializer):
    """Serializer for batch heal response"""
    id = serializers.IntegerField()
    results = HealResponseSerializer(many=True)
    total_processed = serializers.IntegerField()
    total_succeeded = serializers.IntegerField()
    total_failed = serializers.IntegerField()
    processing_time_ms = serializers.FloatField()
