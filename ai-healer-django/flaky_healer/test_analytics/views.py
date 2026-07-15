from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.db.models import Count, Case, When, Value, CharField
from django.db.utils import OperationalError, ProgrammingError
from django.views.generic import TemplateView
from django.views import View
from django.urls import reverse

from .models import (
    TestRun,
    TestCaseResult,
)
from .serializers import TestCaseResultSerializer
from .classifier import classify_failure
from test_generation.models import GenerationJob
from clients.mixins import require_client


def _user_client_ids(user):
    """Return the set of Clients.secret_key values the dashboard user can see."""
    if not user.is_authenticated:
        return set()
    try:
        user_client = getattr(user, "user_client", None)
        if user_client is None:
            return set()
        return set(user_client.clients.values_list("secret_key", flat=True))
    except Exception:
        return set()


def _strip_non_bmp(value):
    if isinstance(value, str):
        return "".join(ch for ch in value if ord(ch) <= 0xFFFF)
    if isinstance(value, list):
        return [_strip_non_bmp(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_non_bmp(v) for k, v in value.items()}
    return value


def _truncate_string(value, max_len: int):
    return str(value or "")[:max_len]


class PlaywrightResultAPIView(APIView):
    # Phase 1: ingest endpoint now requires JWT so we can stamp the tenant on every row.
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        client = require_client(request)
        data = request.data.copy()
        try:
            # 🔥 Create or get TestRun scoped to this client (run_id is unique per-client).
            test_run, _ = TestRun.objects.get_or_create(
                client=client,
                run_id=data.get("run_id"),
                defaults={
                    "environment": data.get("environment"),
                    "build_id": data.get("build_id"),
                    "execution_time": data.get("run_execution_time"),
                }
            )

            # Inject FK + denormalized client on each test-case row.
            data["test_run"] = test_run.id
            data["client"] = client.secret_key
            data["execution_time"] = data.get("execution_time") or data.get("run_execution_time")

            # Enrich payload with analytics classification
            data.update(classify_failure(data))

            # Defensive cleanup for DB compatibility on special payloads.
            data = _strip_non_bmp(data)
            data["run_id"] = _truncate_string(data.get("run_id"), 100)
            data["environment"] = _truncate_string(data.get("environment"), 50)
            data["build_id"] = _truncate_string(data.get("build_id"), 100)
            data["test_name"] = _truncate_string(data.get("test_name"), 255)
            data["status"] = _truncate_string(data.get("status"), 20)
            data["failure_category"] = _truncate_string(data.get("failure_category"), 64)
            data["healing_outcome"] = _truncate_string(data.get("healing_outcome"), 32)
            data["validation_status"] = _truncate_string(data.get("validation_status"), 32)
            data["ui_change_level"] = _truncate_string(data.get("ui_change_level"), 32)

            serializer = TestCaseResultSerializer(data=data)

            if serializer.is_valid():
                instance = serializer.save()
                return Response(
                    {
                        "message": "Saved successfully",
                        "id": instance.id,
                        "failure_category": instance.failure_category,
                        "healing_outcome": instance.healing_outcome,
                    },
                    status=status.HTTP_201_CREATED
                )

            return Response(serializer.errors, status=400)
        except (OperationalError, ProgrammingError) as exc:
            return Response(
                {
                    "error": "Analytics database write failed",
                    "detail": str(exc),
                    "hint": "Check DB charset/migrations. Common fix: utf8mb4 + migrate.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class TestAnalyticsSummaryAPIView(APIView):
    """
    GET /test-analytics/summary/
    Optional filters:
      - run_id
      - build_id
      - environment
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        run_id = request.query_params.get("run_id")
        build_id = request.query_params.get("build_id")
        environment = request.query_params.get("environment")

        # Scope to clients the logged-in dashboard user is a member of.
        allowed_client_ids = _user_client_ids(request.user)
        qs = TestCaseResult.objects.select_related("test_run").filter(
            client_id__in=allowed_client_ids,
        )

        if run_id:
            qs = qs.filter(test_run__run_id=run_id)
        if build_id:
            qs = qs.filter(test_run__build_id=build_id)
        if environment:
            qs = qs.filter(test_run__environment=environment)

        total_tests = qs.count()
        passed = qs.filter(status="PASSED").count()
        failed = qs.filter(status="FAILED").count()
        skipped = qs.filter(status="SKIPPED").count()

        failure_breakdown = list(
            qs.filter(status="FAILED")
            .values("failure_category")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        healing_attempted = qs.filter(healing_attempted=True).count()
        healing_success = qs.filter(healing_outcome="SUCCESS").count()
        healing_failed = qs.filter(healing_outcome="FAILED").count()
        healing_not_attempted = qs.filter(
            healing_outcome__in=[None, "", "NOT_ATTEMPTED"]
        ).count()
        healing_false_positive = qs.filter(
            failure_category="HEALING_FALSE_POSITIVE"
        ).count()
        cache_hits = qs.filter(cache_hit=True).count()
        cache_fallback_to_fresh = qs.filter(cache_fallback_to_fresh=True).count()
        cache_misses = qs.filter(healing_attempted=True, cache_hit=False).count()

        healing_qs = qs.filter(healing_attempted=True)
        assisted_qs = healing_qs.filter(history_assisted=True)
        non_assisted_qs = healing_qs.filter(history_assisted=False)
        assisted_attempts = assisted_qs.count()
        assisted_success = assisted_qs.filter(healing_outcome="SUCCESS").count()
        non_assisted_attempts = non_assisted_qs.count()
        non_assisted_success = non_assisted_qs.filter(healing_outcome="SUCCESS").count()

        def _rate(success: int, attempts: int) -> float:
            if attempts == 0:
                return 0.0
            return round((success * 100.0) / attempts, 2)

        history_bucketed = list(
            healing_qs.annotate(
                history_bucket=Case(
                    When(history_hits=0, then=Value("0")),
                    When(history_hits=1, then=Value("1")),
                    When(history_hits__gte=2, history_hits__lte=3, then=Value("2-3")),
                    When(history_hits__gte=4, then=Value("4+")),
                    default=Value("0"),
                    output_field=CharField(),
                )
            )
            .values("history_bucket")
            .annotate(
                attempts=Count("id"),
                success=Count(Case(When(healing_outcome="SUCCESS", then=1))),
            )
            .order_by("history_bucket")
        )

        history_effectiveness = []
        for row in history_bucketed:
            attempts = row["attempts"] or 0
            success_count = row["success"] or 0
            history_effectiveness.append(
                {
                    "history_bucket": row["history_bucket"],
                    "attempts": attempts,
                    "success": success_count,
                    "success_rate": _rate(success_count, attempts),
                }
            )

        top_failed_selectors = list(
            qs.filter(status="FAILED")
            .exclude(failed_selector__isnull=True)
            .exclude(failed_selector__exact="")
            .values("failed_selector")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        recent_failures = list(
            qs.filter(status="FAILED")
            .order_by("-created_on")
            .values(
                "id",
                "test_name",
                "failure_category",
                "healing_outcome",
                "failed_selector",
                "healed_selector",
                "validation_status",
                "ui_change_level",
                "history_assisted",
                "history_hits",
                "healing_confidence",
                "created_on",
                "test_run__run_id",
            )[:10]
        )

        generation_jobs_qs = GenerationJob.objects.filter(client_id__in=allowed_client_ids)
        if run_id:
            generation_jobs_qs = generation_jobs_qs.filter(
                execution_links__test_run__run_id=run_id
            ).distinct()
        if build_id:
            generation_jobs_qs = generation_jobs_qs.filter(
                execution_links__test_run__build_id=build_id
            ).distinct()
        if environment:
            generation_jobs_qs = generation_jobs_qs.filter(
                execution_links__test_run__environment=environment
            ).distinct()

        job_status_counts = list(
            generation_jobs_qs.values("job_status").annotate(count=Count("id")).order_by("job_status")
        )
        total_jobs = generation_jobs_qs.count()
        approved_jobs = generation_jobs_qs.filter(job_status=GenerationJob.STATE_APPROVED).count()
        materialized_jobs = generation_jobs_qs.filter(job_status=GenerationJob.STATE_MATERIALIZED).count()
        generated_execution = (
            TestCaseResult.objects.filter(
                client_id__in=allowed_client_ids,
                test_run__generation_links__isnull=False,
            )
            .values("status")
            .annotate(count=Count("id"))
        )
        generated_total = sum(row["count"] for row in generated_execution)
        generated_passed = next((row["count"] for row in generated_execution if row["status"] == "PASSED"), 0)
        generated_failed = next((row["count"] for row in generated_execution if row["status"] == "FAILED"), 0)
        generated_healing_attempted = TestCaseResult.objects.filter(
            client_id__in=allowed_client_ids,
            test_run__generation_links__isnull=False,
            healing_attempted=True,
        ).count()
        generated_healing_success = TestCaseResult.objects.filter(
            client_id__in=allowed_client_ids,
            test_run__generation_links__isnull=False,
            healing_outcome="SUCCESS",
        ).count()
        recent_jobs = []
        for job in generation_jobs_qs.order_by("-created_on")[:30]:
            validation = job.validation_summary or {}
            recent_jobs.append(
                {
                    "job_id": str(job.job_id),
                    "feature_name": job.feature_name,
                    "job_status": job.job_status,
                    "created_by": job.created_by or "",
                    "approved_by": job.approved_by or "",
                    "created_on": job.created_on,
                    "drafting_finished_on": job.drafting_finished_on,
                    "materialized_on": job.materialized_on,
                    "total_artifacts": int(validation.get("total_artifacts") or 0),
                    "valid_artifacts": int(validation.get("valid_artifacts") or 0),
                    "invalid_artifacts": int(validation.get("invalid_artifacts") or 0),
                }
            )

        return Response(
            {
                "filters": {
                    "run_id": run_id,
                    "build_id": build_id,
                    "environment": environment,
                },
                "totals": {
                    "total_tests": total_tests,
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                },
                "failure_breakdown": failure_breakdown,
                "healing_summary": {
                    "attempted": healing_attempted,
                    "success": healing_success,
                    "failed": healing_failed,
                    "not_attempted": healing_not_attempted,
                    "false_positive": healing_false_positive,
                },
                "cache_summary": {
                    "cache_hits": cache_hits,
                    "cache_misses": cache_misses,
                    "cache_fallback_to_fresh": cache_fallback_to_fresh,
                },
                "history_summary": {
                    "assisted_attempts": assisted_attempts,
                    "assisted_success": assisted_success,
                    "assisted_success_rate": _rate(assisted_success, assisted_attempts),
                    "non_assisted_attempts": non_assisted_attempts,
                    "non_assisted_success": non_assisted_success,
                    "non_assisted_success_rate": _rate(non_assisted_success, non_assisted_attempts),
                    "buckets": history_effectiveness,
                },
                "top_failed_selectors": top_failed_selectors,
                "recent_failures": recent_failures,
                "generation_summary": {
                    "total_jobs": total_jobs,
                    "job_status_breakdown": job_status_counts,
                    "recent_jobs": recent_jobs,
                    "approved_jobs": approved_jobs,
                    "materialized_jobs": materialized_jobs,
                    "approval_ratio": round((approved_jobs * 100.0 / total_jobs), 2) if total_jobs else 0.0,
                    "materialization_ratio": round((materialized_jobs * 100.0 / total_jobs), 2) if total_jobs else 0.0,
                    "generated_test_results": {
                        "total": generated_total,
                        "passed": generated_passed,
                        "failed": generated_failed,
                        "pass_rate": round((generated_passed * 100.0 / generated_total), 2) if generated_total else 0.0,
                        "healing_attempted": generated_healing_attempted,
                        "healing_success": generated_healing_success,
                    },
                },
            },
            status=status.HTTP_200_OK,
        )


class TestCaseResultDetailAPIView(APIView):
    """
    GET /test-analytics/test-result/<id>/
    Returns full test result details including step_events timeline.
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, id: int):
        allowed_client_ids = _user_client_ids(request.user)
        instance = get_object_or_404(
            TestCaseResult.objects.select_related("test_run").filter(client_id__in=allowed_client_ids),
            id=id
        )

        serializer = TestCaseResultSerializer(instance)
        data = serializer.data
        data["run_id"] = instance.test_run.run_id
        data["build_id"] = instance.test_run.build_id
        data["environment"] = instance.test_run.environment

        return Response(data, status=status.HTTP_200_OK)


class TestAnalyticsDashboardView(LoginRequiredMixin, TemplateView):
    """
    HTML dashboard rendered for both `/healer/` and `/jobs/`. The active tab is
    identified by the URL path itself (subclasses set `default_tab`), not by a
    query parameter. The sidebar highlight and the in-page tab visibility both
    read from the `active_panel` context var.

    The legacy `/dashboard/` route falls back to `?tab=` for back-compat with
    old bookmarks.
    """

    template_name = "test_analytics/dashboard.html"
    login_url = "test_analytics_login"
    # Empty on the base class so `_resolved_tab` falls through to `?tab=` for
    # /dashboard/. Subclasses (HealerAnalyticsView / JobsAnalyticsView) pin it.
    default_tab: str = ""

    def _resolved_tab(self) -> str:
        """
        For subclass URLs (/healer/, /jobs/) `default_tab` is authoritative.
        The bare /dashboard/ route only reads ?tab= if the subclass didn't
        pin a value.
        """
        if self.default_tab in ("healer", "jobs"):
            return self.default_tab
        return "jobs" if self.request.GET.get("tab") == "jobs" else "healer"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["selected_run_id"] = self.request.GET.get("run_id", "")
        context["selected_build_id"] = self.request.GET.get("build_id", "")
        context["selected_environment"] = self.request.GET.get("environment", "")
        # Drives sidebar highlight AND initial JS tab (via the template).
        context["active_panel"] = self._resolved_tab()

        from clients.models import Clients

        allowed_client_ids = _user_client_ids(self.request.user)
        runs = (
            TestRun.objects.filter(client_id__in=allowed_client_ids)
            .values("run_id", "build_id", "environment")
            .order_by("-created_on")[:200]
        )
        context["runs"] = list(runs)

        # Phase 4 — same tenant context as _PanelView so the shared layout can
        # render the client picker + admin link consistently across all panels.
        user_clients = list(
            Clients.objects.filter(secret_key__in=allowed_client_ids)
            .values("secret_key", "clientname", "slug")
            .order_by("clientname")
        )
        active_client = getattr(self.request, "client", None)
        context["user_clients"] = user_clients
        context["active_client"] = active_client
        context["is_superuser"] = bool(self.request.user.is_superuser)
        context["needs_client_pick"] = False  # legacy dashboard does not gate on tenant
        return context


# =====================================================================
# Phase 3 — dashboard panels
# =====================================================================
class _PanelView(LoginRequiredMixin, TemplateView):
    """Base for the 5 workflow panels.

    Injects tenant context on every render:
        - `user_clients`  — the list of Clients the current user may see.
        - `active_client` — the resolved tenant (set by ClientResolutionMiddleware).
        - `is_superuser`  — controls admin-only UI (Jira config link, etc.).
        - `needs_client_pick` — true when the user has 2+ clients and no active
          selection yet; layout.html renders the picker card instead of panel body.
    """
    login_url = "test_analytics_login"
    active_panel: str = ""

    def get_context_data(self, **kwargs):
        from clients.models import Clients

        ctx = super().get_context_data(**kwargs)
        ctx["active_panel"] = self.active_panel

        user = self.request.user
        allowed_ids = _user_client_ids(user)
        user_clients = list(
            Clients.objects.filter(secret_key__in=allowed_ids)
            .values("secret_key", "clientname", "slug")
            .order_by("clientname")
        )
        active_client = getattr(self.request, "client", None)

        ctx["user_clients"] = user_clients
        ctx["active_client"] = active_client
        ctx["is_superuser"] = bool(user.is_superuser)
        # Only show the picker screen when the user has real choices to make;
        # single-client users are auto-selected by the middleware.
        ctx["needs_client_pick"] = active_client is None and len(user_clients) >= 1
        return ctx


class WorklistPanelView(_PanelView):
    template_name = "test_analytics/panels/worklist.html"
    active_panel = "worklist"


class ConfigPanelView(_PanelView):
    template_name = "test_analytics/panels/config.html"
    active_panel = "config"


class GeneratePanelView(_PanelView):
    template_name = "test_analytics/panels/generate.html"
    active_panel = "generate"


class ReviewPanelView(_PanelView):
    template_name = "test_analytics/panels/review.html"
    active_panel = "review"


class ExecutePanelView(_PanelView):
    template_name = "test_analytics/panels/execute.html"
    active_panel = "execute"


class HealerAnalyticsView(TestAnalyticsDashboardView):
    default_tab = "healer"


class JobsAnalyticsView(_PanelView):
    """
    Phase 6.3 — Jobs is now the landing dashboard. Full-width table of every
    job for the active tenant, with stage-progression pills, per-row
    Re-run / Push-to-Jira actions, and auto-refresh while runs are in flight.

    The page itself is server-rendered scaffolding; the row data comes from
    an XHR to `/test-generation/jobs/` (list endpoint) so we can refresh
    every 10s without a full page reload.
    """
    template_name = "test_analytics/panels/jobs.html"
    active_panel = "jobs"


# --- Phase 6 review panels (one per pipeline stage that needs human review) -
class FeatureReviewPanelView(_PanelView):
    template_name = "test_analytics/panels/feature_review.html"
    active_panel = "feature_review"


class ManualTestsReviewPanelView(_PanelView):
    template_name = "test_analytics/panels/manual_tests_review.html"
    active_panel = "manual_tests_review"


class PlanReviewPanelView(_PanelView):
    template_name = "test_analytics/panels/plan_review.html"
    active_panel = "plan_review"


class TestAnalyticsLoginView(View):
    template_name = "test_analytics/login.html"

    def get(self, request):
        # Post-login destination: the workflow entry point (Worklist), so browser
        # users start at the same place every time regardless of where they came from.
        next_url = request.GET.get("next") or reverse("panel_worklist")
        if request.user.is_authenticated:
            return redirect(next_url)
        return render(request, self.template_name, {"next": next_url, "error": ""})

    def post(self, request):
        username = str(request.POST.get("username") or "").strip()
        password = str(request.POST.get("password") or "")
        next_url = str(request.POST.get("next") or reverse("panel_worklist")).strip()
        if not next_url.startswith("/"):
            next_url = reverse("panel_worklist")

        user = authenticate(request, username=username, password=password)
        if user is None:
            return render(
                request,
                self.template_name,
                {
                    "next": next_url,
                    "error": "Invalid username or password.",
                },
                status=401,
            )
        login(request, user)
        return redirect(next_url)


class TestAnalyticsLogoutView(View):
    def post(self, request):
        logout(request)
        return redirect("test_analytics_login")

    def get(self, request):
        logout(request)
        return redirect("test_analytics_login")
