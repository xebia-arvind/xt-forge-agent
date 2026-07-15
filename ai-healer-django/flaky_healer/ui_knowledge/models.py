from django.db import models
from clients.models import Clients

# Create your models here.
class UIPage(models.Model):

    # Tenant scope; nullable to support backfill of pre-Phase-1 rows.
    client = models.ForeignKey(
        Clients,
        on_delete=models.PROTECT,
        null=True,
        related_name="ui_pages",
        db_index=True,
    )
    route = models.CharField(max_length=255)   # /cart
    title = models.CharField(max_length=255, blank=True)

    feature_name = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)

    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        # Routes are unique per-client so two tenants can both have "/login".
        unique_together = ("client", "route")


class UIRouteSnapshot(models.Model):

    SNAPSHOT_TYPE = (
        ("BASELINE", "Baseline"),
        ("NEW_STRUCTURE", "New Structure"),
    )

    page = models.ForeignKey(
        UIPage,
        on_delete=models.CASCADE,
        related_name="snapshots"
    )

    version = models.IntegerField(default=1)

    snapshot_type = models.CharField(
        max_length=20,
        choices=SNAPSHOT_TYPE,
        default="BASELINE"
    )

    is_current = models.BooleanField(default=True)

    # DOM snapshot hash
    dom_hash = models.CharField(max_length=64)

    # full crawl data
    snapshot_json = models.JSONField()

    created_on = models.DateTimeField(auto_now_add=True)


class UIScreenshot(models.Model):

    snapshot = models.ForeignKey(
        UIRouteSnapshot,
        on_delete=models.CASCADE,
        related_name="screenshots"
    )

    image_path = models.TextField()

    # optional metadata
    viewport = models.CharField(max_length=50, blank=True)
    device = models.CharField(max_length=50, blank=True)

    created_on = models.DateTimeField(auto_now_add=True)


class UIElement(models.Model):

    snapshot = models.ForeignKey(
        UIRouteSnapshot,
        on_delete=models.CASCADE,
        related_name="elements"
    )

    selector = models.TextField()

    tag = models.CharField(max_length=30, blank=True)
    role = models.CharField(max_length=50, blank=True)
    text = models.TextField(blank=True)

    test_id = models.CharField(max_length=100, blank=True)
    element_id = models.CharField(max_length=100, blank=True)

    intent_key = models.CharField(max_length=100, default="generic")

    stability_score = models.FloatField(default=1.0)

    created_on = models.DateTimeField(auto_now_add=True)


class UIChangeLog(models.Model):

    CHANGE_TYPE = (
        ("NO_CHANGE", "No Change"),
        ("MINOR", "Minor Change"),
        ("STRUCTURAL", "Structural Change"),
    )

    page = models.ForeignKey(UIPage, on_delete=models.CASCADE)

    baseline_snapshot = models.ForeignKey(
        UIRouteSnapshot,
        on_delete=models.CASCADE,
        related_name="baseline_changes"
    )

    new_snapshot = models.ForeignKey(
        UIRouteSnapshot,
        on_delete=models.CASCADE,
        related_name="new_changes"
    )

    change_type = models.CharField(
        max_length=20,
        choices=CHANGE_TYPE
    )

    added_selectors = models.JSONField(default=list)
    removed_selectors = models.JSONField(default=list)

    auto_promoted = models.BooleanField(default=False)

    created_on = models.DateTimeField(auto_now_add=True)