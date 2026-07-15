from django.contrib import admin

from ui_knowledge.models import UIChangeLog, UIElement, UIPage, UIRouteSnapshot, UIScreenshot


class HiddenFromSidebarAdmin(admin.ModelAdmin):
    def get_model_perms(self, request):
        return {}


class UIRouteSnapshotInline(admin.TabularInline):
    model = UIRouteSnapshot
    extra = 0
    show_change_link = True
    fields = ("version", "snapshot_type", "is_current", "dom_hash", "created_on")
    readonly_fields = ("created_on",)


class UIScreenshotInline(admin.TabularInline):
    model = UIScreenshot
    extra = 0
    fields = ("image_path", "viewport", "device", "created_on")
    readonly_fields = ("created_on",)


class UIElementInline(admin.TabularInline):
    model = UIElement
    extra = 0
    fields = ("selector", "tag", "role", "test_id", "intent_key", "stability_score", "created_on")
    readonly_fields = ("created_on",)


@admin.register(UIPage)
class UIPageAdmin(admin.ModelAdmin):
    list_display = ("id", "route", "title", "is_active", "created_on")
    list_filter = ("is_active",)
    search_fields = ("route", "title", "feature_name")
    ordering = ("-created_on",)
    inlines = [UIRouteSnapshotInline]


class UIRouteSnapshotAdmin(HiddenFromSidebarAdmin):
    list_display = ("id", "page", "version", "snapshot_type", "is_current", "created_on")
    list_filter = ("snapshot_type", "is_current")
    search_fields = ("page__route", "dom_hash")
    ordering = ("-created_on",)
    inlines = [UIScreenshotInline, UIElementInline]


class UIScreenshotAdmin(HiddenFromSidebarAdmin):
    list_display = ("id", "snapshot", "image_path", "created_on")
    list_filter = ("snapshot",)
    search_fields = ("snapshot__page__route", "image_path")
    ordering = ("-created_on",)


class UIElementAdmin(HiddenFromSidebarAdmin):
    list_display = ("id", "snapshot", "intent_key", "selector", "created_on")
    list_filter = ("intent_key", "snapshot")
    search_fields = ("snapshot__page__route", "selector", "test_id", "text")
    ordering = ("-created_on",)


@admin.register(UIChangeLog)
class UIChangeLogAdmin(admin.ModelAdmin):
    list_display = ("id", "page", "change_type", "auto_promoted", "created_on")
    list_filter = ("change_type", "auto_promoted")
    search_fields = ("page__route",)
    ordering = ("-created_on",)


admin.site.register(UIRouteSnapshot, UIRouteSnapshotAdmin)
admin.site.register(UIScreenshot, UIScreenshotAdmin)
admin.site.register(UIElement, UIElementAdmin)
