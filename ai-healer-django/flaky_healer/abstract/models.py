from django.db import models
from django.utils import timezone

STATUS_CHOICES = (
    ("a", "Active"),
    ("i", "Inactive"),
)

class Common(models.Model):
    # ---- Audit fields ----
    created_on = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    # ---- Status / lifecycle ----
    status = models.CharField(
        max_length=1,
        choices=STATUS_CHOICES,
        default="a",
        db_index=True
    )

    # ---- Soft delete ----
    is_deleted = models.BooleanField(default=False)
    deleted_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["-created_on"]

    # ---- Common methods ----
    def activate(self):
        self.status = "a"
        self.save(update_fields=["status"])

    def deactivate(self):
        self.status = "i"
        self.save(update_fields=["status"])

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_on = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_on"])

    def restore(self):
        self.is_deleted = False
        self.deleted_on = None
        self.save(update_fields=["is_deleted", "deleted_on"])
