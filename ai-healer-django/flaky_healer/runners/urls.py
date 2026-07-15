from django.urls import path

from .views import (
    ExecuteEnqueueView,
    GenerateEnqueueView,
    RunnerJobDetailView,
    RunnerJobListView,
    RunnerJobStreamView,
)

urlpatterns = [
    path("generate/", GenerateEnqueueView.as_view(), name="runner_generate"),
    path("execute/", ExecuteEnqueueView.as_view(), name="runner_execute"),
    path("jobs/", RunnerJobListView.as_view(), name="runner_job_list"),
    path("jobs/<int:job_id>/", RunnerJobDetailView.as_view(), name="runner_job_detail"),
    path("jobs/<int:job_id>/stream/", RunnerJobStreamView.as_view(), name="runner_job_stream"),
]
