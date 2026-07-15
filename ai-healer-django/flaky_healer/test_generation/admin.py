from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse, path
from django.utils.html import format_html

from .models import GenerationJob, GenerationScenario, GeneratedArtifact, GenerationExecutionLink


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job_id",
        "feature_name",
        "job_status",
        "coverage_mode",
        "created_by",
        "view_draft_link",
        "approve_link",
        "materialize_link",
        "created_on",
    )
    list_filter = ("job_status", "coverage_mode", "llm_model")
    search_fields = ("feature_name", "feature_description", "created_by", "approved_by")
    readonly_fields = (
        "job_id",
        "crawl_summary",
        "feature_summary",
        "llm_notes",
        "validation_summary",
        "materialized_manifest",
        "error_message",
        "drafting_started_on",
        "drafting_finished_on",
        "materialized_on",
    )
    ordering = ("-created_on",)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:job_pk>/quick-approve/",
                self.admin_site.admin_view(self.quick_approve),
                name="test_generation_generationjob_quick_approve",
            ),
            path(
                "<int:job_pk>/quick-materialize/",
                self.admin_site.admin_view(self.quick_materialize),
                name="test_generation_generationjob_quick_materialize",
            ),
        ]
        return custom + urls

    def view_draft_link(self, obj):
        url = reverse("generation_job_detail", args=[obj.job_id])
        return format_html('<a href="{}" target="_blank">View Draft</a>', url)

    view_draft_link.short_description = "View Draft"

    def approve_link(self, obj):
        url = reverse("admin:test_generation_generationjob_quick_approve", args=[obj.id])
        return format_html('<a href="{}">Approve</a>', url)

    approve_link.short_description = "Approve"

    def materialize_link(self, obj):
        url = reverse("admin:test_generation_generationjob_quick_materialize", args=[obj.id])
        return format_html('<a href="{}">Materialize</a>', url)

    materialize_link.short_description = "Materialize"

    def quick_approve(self, request, job_pk):
        obj = self.get_object(request, job_pk)
        if not obj:
            return HttpResponseRedirect("../")
        if obj.job_status in {GenerationJob.STATE_DRAFT_READY, GenerationJob.STATE_APPROVED}:
            obj.job_status = GenerationJob.STATE_APPROVED
            obj.approved_by = request.user.get_username()
            if not obj.approved_notes:
                obj.approved_notes = "Approved from Django admin quick action."
            obj.save(update_fields=["job_status", "approved_by", "approved_notes", "last_modified"])
            self.message_user(request, f"Generation job {obj.job_id} approved.")
        else:
            self.message_user(request, f"Cannot approve job in state={obj.job_status}", level="warning")
        return HttpResponseRedirect("../../")

    def quick_materialize(self, request, job_pk):
        from .generation_service import materialize_job

        obj = self.get_object(request, job_pk)
        if not obj:
            return HttpResponseRedirect("../")
        if obj.job_status not in {GenerationJob.STATE_APPROVED, GenerationJob.STATE_MATERIALIZED}:
            self.message_user(request, f"Cannot materialize job in state={obj.job_status}", level="warning")
            return HttpResponseRedirect("../../")
        result = materialize_job(obj, allow_overwrite=False)
        if result.ok:
            self.message_user(request, f"Materialized {len(result.written_files)} files.")
        else:
            summary = []
            if result.conflicts:
                summary.append(f"conflicts={len(result.conflicts)}")
            if result.errors:
                summary.append(f"errors={len(result.errors)}")
            self.message_user(request, f"Materialization incomplete ({', '.join(summary)}).", level="warning")
        return HttpResponseRedirect("../../")


@admin.register(GenerationScenario)
class GenerationScenarioAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job",
        "scenario_id",
        "title",
        "scenario_type",
        "priority",
        "selected_for_materialization",
        "created_on",
    )
    list_filter = ("scenario_type", "selected_for_materialization")
    search_fields = ("scenario_id", "title", "job__feature_name")
    ordering = ("job", "priority")


@admin.register(GeneratedArtifact)
class GeneratedArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job",
        "artifact_type",
        "relative_path",
        "validation_status",
        "llm_generate_link",
        "checksum",
        "created_on",
    )
    list_filter = ("artifact_type", "validation_status")
    search_fields = ("relative_path", "job__feature_name")
    readonly_fields = ("checksum", "validation_errors", "warnings")
    ordering = ("-created_on",)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:artifact_pk>/llm-generate/",
                self.admin_site.admin_view(self.llm_generate),
                name="test_generation_generatedartifact_llm_generate",
            ),
        ]
        return custom + urls

    def llm_generate_link(self, obj):
        url = reverse("admin:test_generation_generatedartifact_llm_generate", args=[obj.id])
        return format_html('<a href="{}">Generate via LLM</a>', url)

    llm_generate_link.short_description = "LLM Patch"

    def llm_generate(self, request, artifact_pk):
        from .generation_service import regenerate_job_artifacts_with_llm

        artifact = self.get_object(request, artifact_pk)
        if not artifact:
            return HttpResponseRedirect("../")
        try:
            summary = regenerate_job_artifacts_with_llm(artifact.job)
            self.message_user(
                request,
                (
                    f"LLM regeneration complete for job {artifact.job.job_id}. "
                    f"valid={summary.get('valid_artifacts', 0)} "
                    f"invalid={summary.get('invalid_artifacts', 0)} "
                    f"status={summary.get('status')}"
                ),
            )
        except Exception as exc:
            self.message_user(request, f"LLM regeneration failed: {str(exc)}", level="error")
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", "../../"))


@admin.register(GenerationExecutionLink)
class GenerationExecutionLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "test_run", "notes", "created_on")
    list_filter = ("test_run",)
    search_fields = ("job__feature_name", "test_run__run_id", "notes")
