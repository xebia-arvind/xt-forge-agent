from django.db import models
from abstract.models import Common
from django.contrib.auth.models import User
from clients.models import Clients


class HealerRequest(Common):
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    client_id = models.ForeignKey(Clients, on_delete=models.CASCADE, null=True)
    batch_id = models.IntegerField(default=0)
    failed_selector = models.TextField()
    html = models.TextField()
    use_of_selector = models.TextField()
    selector_type = models.CharField(max_length=20)
    url = models.URLField()
    healed_selector = models.TextField()
    confidence = models.FloatField()
    success = models.BooleanField(default=False)
    processing_time_ms = models.IntegerField()
    llm_used = models.BooleanField(default=False)
    screenshot_analyzed = models.BooleanField(default=False)
    intent_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    validation_status = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    validation_reason = models.TextField(null=True, blank=True)
    dom_fingerprint = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    candidate_snapshot = models.JSONField(null=True, blank=True)
    history_assisted = models.BooleanField(default=False)
    history_hits = models.IntegerField(default=0)
    ui_change_level = models.CharField(max_length=32, null=True, blank=True, db_index=True)


class SuggestedSelector(Common):
    healer_request = models.ForeignKey(HealerRequest, on_delete=models.CASCADE, related_name="suggested_selectors")
    selector = models.TextField()
    score = models.FloatField()
    base_score = models.FloatField()
    attribute_score = models.FloatField()
    tag = models.CharField(max_length=20)
    text = models.TextField()
    xpath = models.TextField()

    def __str__(self):
        return self.selector
