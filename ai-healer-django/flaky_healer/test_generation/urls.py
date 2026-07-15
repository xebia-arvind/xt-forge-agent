from django.urls import path

from .views import (
    GenerationJobApproveAPIView,
    GenerationJobArtifactUpdateAPIView,
    GenerationJobCreateAPIView,
    GenerationJobDetailAPIView,
    GenerationJobLinkRunAPIView,
    GenerationJobMaterializeAPIView,
    GenerationJobRejectAPIView,
    # Phase 6 stage endpoints
    PipelineJobCreateView,
    StageArtifactsApproveView,
    StageArtifactsRunView,
    StageExecuteApproveView,
    StageExecuteRunView,
    StageFeatureApproveView,
    StageFeatureRunView,
    StageManualTestsApproveView,
    StageManualTestsRunView,
    StagePlanApproveView,
    StagePlanRunView,
)

urlpatterns = [
    path("jobs/", GenerationJobCreateAPIView.as_view(), name="generation_job_create"),
    path("jobs/<uuid:job_id>/", GenerationJobDetailAPIView.as_view(), name="generation_job_detail"),
    path("jobs/<uuid:job_id>/approve/", GenerationJobApproveAPIView.as_view(), name="generation_job_approve"),
    path("jobs/<uuid:job_id>/artifacts/update/", GenerationJobArtifactUpdateAPIView.as_view(), name="generation_job_artifact_update"),
    path("jobs/<uuid:job_id>/materialize/", GenerationJobMaterializeAPIView.as_view(), name="generation_job_materialize"),
    path("jobs/<uuid:job_id>/reject/", GenerationJobRejectAPIView.as_view(), name="generation_job_reject"),
    path("jobs/<uuid:job_id>/link-run/", GenerationJobLinkRunAPIView.as_view(), name="generation_job_link_run"),

    # --- Phase 6 pipeline job intake ------------------------------------------
    path("pipeline-jobs/", PipelineJobCreateView.as_view(), name="pipeline_job_create"),

    # --- Phase 6 six-agent pipeline stages ------------------------------------
    # Symmetric /run/ + /approve/ pairs per stage. Each stage advances only
    # after explicit approval; agents are idempotent so /run/ can be replayed.
    path("jobs/<uuid:job_id>/stage/feature/run/",           StageFeatureRunView.as_view(),         name="stage_feature_run"),
    path("jobs/<uuid:job_id>/stage/feature/approve/",       StageFeatureApproveView.as_view(),     name="stage_feature_approve"),
    path("jobs/<uuid:job_id>/stage/manual-tests/run/",      StageManualTestsRunView.as_view(),     name="stage_manual_tests_run"),
    path("jobs/<uuid:job_id>/stage/manual-tests/approve/",  StageManualTestsApproveView.as_view(), name="stage_manual_tests_approve"),
    path("jobs/<uuid:job_id>/stage/plan/run/",              StagePlanRunView.as_view(),            name="stage_plan_run"),
    path("jobs/<uuid:job_id>/stage/plan/approve/",          StagePlanApproveView.as_view(),        name="stage_plan_approve"),
    path("jobs/<uuid:job_id>/stage/artifacts/run/",         StageArtifactsRunView.as_view(),       name="stage_artifacts_run"),
    path("jobs/<uuid:job_id>/stage/artifacts/approve/",     StageArtifactsApproveView.as_view(),   name="stage_artifacts_approve"),
    path("jobs/<uuid:job_id>/stage/execute/run/",           StageExecuteRunView.as_view(),         name="stage_execute_run"),
    path("jobs/<uuid:job_id>/stage/execute/approve/",       StageExecuteApproveView.as_view(),     name="stage_execute_approve"),
]
