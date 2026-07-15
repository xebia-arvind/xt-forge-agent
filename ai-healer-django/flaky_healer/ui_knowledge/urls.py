from django.urls import path

from .views import (
    UISnapshotCreateAPI,
    UIChangeStatusAPIView,
)

urlpatterns = [
    path("sync/", UISnapshotCreateAPI.as_view(), name="sync_ui_knowledge"),
    path("change-status/", UIChangeStatusAPIView.as_view(), name="ui_change_status"),
]
