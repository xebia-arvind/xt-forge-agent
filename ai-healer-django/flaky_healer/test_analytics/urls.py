from django.urls import path
from django.views.generic import RedirectView
from clients.views import PickClientView
from .views import (
    ConfigPanelView,
    ExecutePanelView,
    FeatureReviewPanelView,
    GeneratePanelView,
    HealerAnalyticsView,
    JobsAnalyticsView,
    ManualTestsReviewPanelView,
    PlanReviewPanelView,
    PlaywrightResultAPIView,
    ReviewPanelView,
    TestAnalyticsDashboardView,
    TestAnalyticsLoginView,
    TestAnalyticsLogoutView,
    TestAnalyticsSummaryAPIView,
    TestCaseResultDetailAPIView,
    WorklistPanelView,
)

urlpatterns = [
    # Phase 6.6 — Landing page. Root of /test-analytics/ now lands on the
    # Jobs dashboard, which shows every job's stage-wise status and is
    # the daily working surface. Previous landing was Worklist; that
    # menu item is still one click away in the Workflow section.
    path("", RedirectView.as_view(pattern_name="panel_jobs", permanent=False)),
    path("login/", TestAnalyticsLoginView.as_view(), name="test_analytics_login"),
    path("logout/", TestAnalyticsLogoutView.as_view(), name="test_analytics_logout"),
    path("test-result/", PlaywrightResultAPIView.as_view(), name="test_result_create"),
    path("test-result/<int:id>/", TestCaseResultDetailAPIView.as_view(), name="test_result_detail"),
    path("summary/", TestAnalyticsSummaryAPIView.as_view(), name="test_analytics_summary"),
    path("dashboard/", TestAnalyticsDashboardView.as_view(), name="test_analytics_dashboard"),
    # Phase 3 workflow panels (replacement for Streamlit).
    path("worklist/", WorklistPanelView.as_view(), name="panel_worklist"),
    path("config/",   ConfigPanelView.as_view(),   name="panel_config"),
    path("generate/", GeneratePanelView.as_view(), name="panel_generate"),
    path("review/",   ReviewPanelView.as_view(),   name="panel_review"),
    path("execute/",  ExecutePanelView.as_view(),  name="panel_execute"),
    # --- Phase 6 pipeline review panels ---------------------------------------
    path("feature-review/",       FeatureReviewPanelView.as_view(),      name="panel_feature_review"),
    path("manual-tests-review/",  ManualTestsReviewPanelView.as_view(),  name="panel_manual_tests_review"),
    path("plan-review/",          PlanReviewPanelView.as_view(),         name="panel_plan_review"),
    # Analytics panels — same template as the legacy /dashboard/ but named URLs
    # so the sidebar and other panels can link to them consistently.
    path("healer/",   HealerAnalyticsView.as_view(),   name="panel_healer"),
    path("jobs/",     JobsAnalyticsView.as_view(),     name="panel_jobs"),
    path("pick-client/", PickClientView.as_view(), name="panel_pick_client"),
]
