from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.http import HttpResponseRedirect
from django.urls import path
from .models import (
    TestRun,
    TestCaseResult,
    AnalyticsDashboardLink,
)

# Register your models here.
class TestRunAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'run_id',
        'environment',
        'build_id',
        'execution_time',
        # 'summary_link',
        # 'dashboard_link',
        'created_on',
    )
    list_filter = ('environment', 'build_id')
    search_fields = ('run_id', 'build_id')
    ordering = ('-created_on',)

    def summary_link(self, obj):
        url = reverse("test_analytics_summary")
        return format_html(
            '<a href="{}?run_id={}" target="_blank">Open Summary</a>',
            url,
            obj.run_id,
        )
    summary_link.short_description = "Analytics Summary"

    def dashboard_link(self, obj):
        url = reverse("test_analytics_dashboard")
        return format_html(
            '<a href="{}?run_id={}" target="_blank">Open Dashboard</a>',
            url,
            obj.run_id,
        )
    dashboard_link.short_description = "Dashboard"
    
class TestCaseResultAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'test_name',
        'status',
        'failure_category',
        'healing_outcome',
        'test_run',
        # 'detail_link',
        # 'dashboard_link',
        'created_on',
    )
    list_filter = ('status', 'failure_category', 'healing_outcome', 'test_run')
    search_fields = ('test_name', 'test_run__run_id', 'failed_selector', 'root_cause')
    ordering = ('-created_on',)

    def detail_link(self, obj):
        url = reverse("test_result_detail", args=[obj.id])
        return format_html('<a href="{}" target="_blank">Open Detail</a>', url)
    detail_link.short_description = "Result Detail"

    def dashboard_link(self, obj):
        url = reverse("test_analytics_dashboard")
        run_id = obj.test_run.run_id if obj.test_run else ""
        return format_html(
            '<a href="{}?run_id={}" target="_blank">Open Dashboard</a>',
            url,
            run_id,
        )
    dashboard_link.short_description = "Dashboard"

admin.site.register(TestRun, TestRunAdmin)
admin.site.register(TestCaseResult, TestCaseResultAdmin)


@admin.register(AnalyticsDashboardLink)
class AnalyticsDashboardLinkAdmin(admin.ModelAdmin):
    """
    Menu-only admin entry that opens dashboard directly.
    """

    def changelist_view(self, request, extra_context=None):
        return HttpResponseRedirect(reverse("test_analytics_dashboard"))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _test_analytics_dashboard_admin_redirect(request):
    return HttpResponseRedirect(reverse("test_analytics_dashboard"))


_original_admin_get_urls = admin.site.get_urls


def _admin_get_urls_with_dashboard():
    urls = _original_admin_get_urls()
    custom_urls = [
        path(
            "test-analytics-dashboard/",
            admin.site.admin_view(_test_analytics_dashboard_admin_redirect),
            name="test_analytics_dashboard_admin",
        ),
    ]
    return custom_urls + urls


admin.site.get_urls = _admin_get_urls_with_dashboard

# Phase 4: point the admin's top-right "VIEW SITE" link (and post-logout redirect)
# at the XT-Forge dashboard so superusers can always jump back to it in one click.
admin.site.site_url = "/test-analytics/"
