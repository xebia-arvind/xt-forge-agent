from rest_framework import serializers
from .models import TestRun, TestCaseResult


class TestRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestRun
        fields = "__all__"


class TestCaseResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCaseResult
        fields = "__all__"
