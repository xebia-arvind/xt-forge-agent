import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.permissions import IsAuthenticated

from test_analytics.models import TestRun
from clients.mixins import require_client

from .models import GenerationJob, GenerationExecutionLink, GeneratedArtifact
from .serializers import (
    GenerationJobArtifactUpdateSerializer,
    GenerationJobApproveSerializer,
    GenerationJobCreateSerializer,
    GenerationJobDetailSerializer,
    GenerationJobLinkRunSerializer,
    GenerationJobMaterializeSerializer,
    GenerationJobRejectSerializer,
)
from .generation_service import (
    _available_intent_keys,
    _build_selector_map,
    _enrich_llm_context,
    _run_crawl_context,
    _sha256,
    _validate_artifact_content,
    _validate_artifacts,
    apply_approval_selection,
    generate_job_draft,
    materialize_job,
)
from . import ts_normalizer_client
from django.conf import settings as _django_settings

logger = logging.getLogger(__name__)


class _ClientScopedAPIView(APIView):
    """Authenticate via JWT (preferred for headless callers) or session (browser).
    All subclasses operate within `request.client`."""
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class GenerationJobCreateAPIView(_ClientScopedAPIView):
    def get(self, request):
        client = require_client(request)
        # Phase 6.5.1 — Jobs dashboard should show only jobs that have
        # STARTED the pipeline. Jira-worklist intake stubs (created but
        # never had their Feature agent run) live at STAGE_INTAKE and
        # get filtered out here. Fresh Jira jobs still enter via the
        # Worklist panel; they become visible on the dashboard once
        # the Feature stage kicks in.
        jobs = (
            GenerationJob.objects
            .filter(client=client)
            .exclude(stage=GenerationJob.STAGE_INTAKE)
            .order_by("-created_on")
        )
        # Simplified listing for the UI. `stage` is included so the Phase 6
        # panel pickers can highlight the current pipeline stage; legacy jobs
        # simply carry stage=INTAKE and the panels ignore it.
        data = [
            {
                "job_id": str(job.job_id),
                "feature_name": job.feature_name,
                "status": job.job_status,
                "stage": job.stage,
                "jira_issue_key": job.jira_issue_key,
                "created_on": job.created_on,
            }
            for job in jobs
        ]
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        client = require_client(request)
        serializer = GenerationJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        job = GenerationJob.objects.create(
            client=client,
            feature_name=data["feature_name"],
            feature_description=data["feature_description"],
            seed_urls=data.get("seed_urls") or [],
            intent_hints=data.get("intent_hints") or [],
            coverage_mode=data.get("coverage_mode", GenerationJob.COVERAGE_SMOKE_NEGATIVE),
            max_scenarios=data.get("max_scenarios", 8),
            max_routes=data.get("max_routes", 20),
            base_url=data.get("base_url", "http://localhost:3000"),
            created_by=data.get("created_by", ""),
            llm_model=os.getenv("TEST_GEN_LLM_MODEL", "qwen2.5-coder:7b"),
            llm_temperature=0.0,
            job_status=GenerationJob.STATE_DRAFTING,
        )

        job = generate_job_draft(
            job,
            manual_scenarios=data.get("manual_scenarios") or [],
        )
        
        return Response(
            {
                "job_id": str(job.job_id),
                "status": job.job_status,
                "created_on": job.created_on,
            },
            status=status.HTTP_201_CREATED,
        )


class GenerationJobDetailAPIView(_ClientScopedAPIView):
    def get(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(
            GenerationJob.objects.prefetch_related("scenarios", "artifacts", "execution_links__test_run"),
            job_id=job_id,
            client=client,
        )
        # Fresh validation pass — the stored validation_errors on each row can
        # go stale (e.g. an earlier draft failed, later re-run repaired the
        # content but the row still carries old error strings). Re-run the
        # unified validator so the panel shows the truth, and persist any
        # deltas.
        _revalidate_artifacts_in_place(job)

        payload = GenerationJobDetailSerializer(job).data
        payload["status"] = job.job_status
        return Response(payload, status=status.HTTP_200_OK)


def _revalidate_artifacts_in_place(job: GenerationJob) -> None:
    """
    Re-run `ArtifactValidator` on every artifact of `job` and persist any
    change in `validation_status` / `validation_errors` back to the row. Uses
    `bulk_update` so a detail-fetch does at most one write regardless of
    artifact count. Silent no-op if nothing changed.
    """
    from test_generation.artifact_validation import validate_artifact

    dirty = []
    for a in job.artifacts.all():
        content = a.content_final or a.content_draft or ""
        result = validate_artifact(
            a.artifact_type,
            a.relative_path,
            content,
            ctx={"seed_urls": list(job.seed_urls or [])},
        )
        # Store rich `{rule, message, severity}` entries; legacy list-of-str
        # consumers can extract `.message` themselves.
        new_errors = [e.dict() for e in result.errors]
        new_warnings = [w.dict() for w in result.warnings]
        new_status = GeneratedArtifact.VALID if result.is_valid else GeneratedArtifact.INVALID
        if (
            a.validation_status != new_status
            or a.validation_errors != new_errors
            or a.warnings != new_warnings
        ):
            a.validation_status = new_status
            a.validation_errors = new_errors
            a.warnings = new_warnings
            dirty.append(a)
    if dirty:
        GeneratedArtifact.objects.bulk_update(
            dirty, ["validation_status", "validation_errors", "warnings"]
        )


class GenerationJobApproveAPIView(_ClientScopedAPIView):
    def post(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(GenerationJob, job_id=job_id, client=client)
        serializer = GenerationJobApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if job.job_status not in {GenerationJob.STATE_DRAFT_READY, GenerationJob.STATE_APPROVED}:
            return Response(
                {"error": f"Cannot approve from state={job.job_status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        apply_approval_selection(
            job=job,
            include_scenario_ids=data.get("include_scenario_ids"),
            exclude_scenario_ids=data.get("exclude_scenario_ids"),
        )

        job.approved_by = data["approved_by"]
        job.approved_notes = data.get("notes", "")
        job.job_status = GenerationJob.STATE_APPROVED
        job.save(update_fields=["approved_by", "approved_notes", "job_status", "last_modified"])
        return Response({"status": job.job_status}, status=status.HTTP_200_OK)


class GenerationJobMaterializeAPIView(_ClientScopedAPIView):
    def post(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(GenerationJob, job_id=job_id, client=client)
        serializer = GenerationJobMaterializeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        allow_overwrite = serializer.validated_data.get("allow_overwrite", False)

        if job.job_status not in {GenerationJob.STATE_APPROVED, GenerationJob.STATE_MATERIALIZED}:
            return Response(
                {"error": f"Cannot materialize from state={job.job_status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = materialize_job(job, allow_overwrite=allow_overwrite, client_slug=client.slug)
        if not result.ok:
            return Response(
                {
                    "status": job.job_status,
                    "written_files": result.written_files,
                    "write_report": {
                        "conflicts": result.conflicts,
                        "errors": result.errors,
                    },
                },
                status=status.HTTP_409_CONFLICT if result.conflicts else status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "status": job.job_status,
                "written_files": result.written_files,
                "write_report": {
                    "conflicts": [],
                    "errors": [],
                },
            },
            status=status.HTTP_200_OK,
        )


class GenerationJobRejectAPIView(_ClientScopedAPIView):
    def post(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(GenerationJob, job_id=job_id, client=client)
        serializer = GenerationJobRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")

        if job.job_status == GenerationJob.STATE_MATERIALIZED:
            return Response(
                {"error": "Cannot reject a materialized job"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job.rejected_reason = reason
        job.job_status = GenerationJob.STATE_REJECTED
        job.save(update_fields=["rejected_reason", "job_status", "last_modified"])
        return Response({"status": job.job_status}, status=status.HTTP_200_OK)


class GenerationJobLinkRunAPIView(_ClientScopedAPIView):
    def post(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(GenerationJob, job_id=job_id, client=client)
        serializer = GenerationJobLinkRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run_id = serializer.validated_data["run_id"]
        notes = serializer.validated_data.get("notes", "")

        test_run = get_object_or_404(TestRun, run_id=run_id, client=client)
        link, _ = GenerationExecutionLink.objects.get_or_create(
            job=job,
            test_run=test_run,
            defaults={"notes": notes},
        )
        if notes and link.notes != notes:
            link.notes = notes
            link.save(update_fields=["notes", "last_modified"])

        return Response(
            {
                "status": "LINKED",
                "job_id": str(job.job_id),
                "run_id": test_run.run_id,
                "link_id": link.id,
            },
            status=status.HTTP_200_OK,
        )


class GenerationJobArtifactUpdateAPIView(_ClientScopedAPIView):
    def post(self, request, job_id):
        client = require_client(request)
        job = get_object_or_404(GenerationJob, job_id=job_id, client=client)
        serializer = GenerationJobArtifactUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if job.job_status == GenerationJob.STATE_MATERIALIZED:
            return Response(
                {"error": "Cannot edit artifacts after materialization"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        relative_path = str(data["relative_path"] or "").strip()
        artifact = get_object_or_404(GeneratedArtifact, job=job, relative_path=relative_path)
        content = str(data["content"] or "")

        errors, warnings = _validate_artifact_content(
            artifact.artifact_type,
            content,
            path=artifact.relative_path,
            ctx={"seed_urls": list(job.seed_urls or [])},
        )
        is_valid = len(errors) == 0
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()

        artifact.content_final = content
        if data.get("update_draft", True):
            artifact.content_draft = content
        artifact.validation_status = GeneratedArtifact.VALID if is_valid else GeneratedArtifact.INVALID
        artifact.validation_errors = errors
        artifact.warnings = warnings
        artifact.checksum = checksum
        artifact.save(
            update_fields=[
                "content_final",
                "content_draft",
                "validation_status",
                "validation_errors",
                "warnings",
                "checksum",
                "last_modified",
            ]
        )

        all_artifacts = job.artifacts.all()
        invalid_count = all_artifacts.filter(validation_status=GeneratedArtifact.INVALID).count()
        total_count = all_artifacts.count()
        valid_count = total_count - invalid_count
        summary = dict(job.validation_summary or {})
        summary.update(
            {
                "total_artifacts": total_count,
                "valid_artifacts": valid_count,
                "invalid_artifacts": invalid_count,
                "manual_review_edited": True,
            }
        )
        job.validation_summary = summary
        job.save(update_fields=["validation_summary", "last_modified"])

        return Response(
            {
                "status": "UPDATED",
                "job_status": job.job_status,
                "artifact": {
                    "relative_path": artifact.relative_path,
                    "validation_status": artifact.validation_status,
                    "validation_errors": artifact.validation_errors,
                    "warnings": artifact.warnings,
                    "checksum": artifact.checksum,
                },
                "validation_summary": summary,
            },
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Phase 6 — pipeline job intake
# =============================================================================
class PipelineJobCreateView(_ClientScopedAPIView):
    """
    POST /pipeline-jobs/  — create a pipeline job in STAGE_INTAKE without kicking
    off any LLM work. Called from the Worklist panel when the operator picks a
    Jira ticket. The Feature Review panel auto-runs the Feature Author on load.

    Body:
        {
          "jira_issue_key":     "XX-99",     # required
          "feature_name":       "…",         # falls back to jira_issue_key
          "feature_description":"…",         # optional
          "base_url":           "https://…", # optional; falls back to client default
          "seed_urls":          ["/", …]     # optional; agents may extend
        }
    """

    def post(self, request):
        client = require_client(request)
        data = request.data or {}
        jira_key = str(data.get("jira_issue_key") or "").strip()
        if not jira_key:
            return Response({"error": "jira_issue_key is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        # Wrap the create so operational failures (e.g. unmigrated Phase 6
        # columns on the DB, unique-key clash) return a JSON body the browser
        # can surface instead of a bare 500.
        try:
            job = GenerationJob.objects.create(
                client=client,
                feature_name=str(data.get("feature_name") or jira_key),
                feature_description=str(data.get("feature_description") or ""),
                seed_urls=list(data.get("seed_urls") or ["/"]),
                base_url=str(data.get("base_url") or "http://localhost:3000"),
                created_by=request.user.username,
                jira_issue_key=jira_key,
                job_status=GenerationJob.STATE_DRAFTING,
                stage=GenerationJob.STAGE_INTAKE,
            )
            job.stage_history = [{
                "stage": job.stage,
                "agent": "intake",
                "decision": "created",
                "reviewer": request.user.username,
                "recorded_on": timezone.now().isoformat(),
                "notes": f"Seeded from Jira {jira_key}",
            }]
            job.save(update_fields=["stage_history", "last_modified"])
        except Exception as exc:
            # Log at ERROR so the traceback lands in the Django console + the
            # `logs/auth.log` file if it's routed there. The response body only
            # carries a short reason.
            logger.exception("Failed to create pipeline job for jira=%s", jira_key)
            msg = str(exc)
            hint = ""
            # Nudge the operator toward the most common cause: forgotten migration.
            lowered = msg.lower()
            if "unknown column" in lowered or "no such column" in lowered:
                hint = (
                    " · Phase 6 columns missing — run "
                    "`python manage.py migrate test_generation` and retry."
                )
            return Response(
                {"error": f"{type(exc).__name__}: {msg[:400]}{hint}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"job_id": str(job.job_id), "stage": job.stage},
                        status=status.HTTP_201_CREATED)


# =============================================================================
# Phase 6 — multi-agent pipeline stage endpoints
# =============================================================================
#
# Each stage exposes two POST endpoints:
#     /stage/<name>/run/       — invoke the agent, persist raw output on the job
#     /stage/<name>/approve/   — advance the job to the next stage
#
# Both are tenant-scoped; both take the job UUID in the URL.
# The agents live in test_generation/agents.py; these views are only HTTP glue
# and side-effects (fetching live Jira context, persisting artifacts to disk-
# bound rows, etc.).

from . import agents  # noqa: E402  — placed here so the import isn't hoisted above the legacy code


def _get_job_scoped(request, job_id) -> GenerationJob:
    """Fetch a job in the caller's tenant scope or 404."""
    client = require_client(request)
    return get_object_or_404(GenerationJob, job_id=job_id, client=client)


def _tenant_jira_client(client):
    """
    Return (JiraClient, JiraConnection) for this tenant, or (None, None) if no
    connection is configured. Local import keeps the module load light.
    """
    try:
        from integrations_jira.models import JiraConnection
        from integrations_jira.services import JiraClient
    except Exception as exc:  # pragma: no cover
        logger.warning("integrations_jira import failed: %s", exc)
        return None, None
    conn = JiraConnection.objects.filter(client=client).first()
    if not conn:
        return None, None
    try:
        return JiraClient(conn), conn
    except Exception as exc:
        logger.warning("Failed to build JiraClient for %s: %s", client.slug, exc)
        return None, None


def _adf_to_text(node) -> str:
    """
    Best-effort ADF → plain text walker for pre-filling the Feature Author with
    a Jira issue's rich-text description. Mirrors the walker used in the Config
    panel JS so behavior stays consistent.
    """
    if not node:
        return ""
    if isinstance(node, str):
        return node
    node_type = node.get("type") if isinstance(node, dict) else None
    if node_type == "text":
        return str(node.get("text") or "")
    inner = ""
    for child in (node.get("content") or []) if isinstance(node, dict) else []:
        inner += _adf_to_text(child)
    if node_type in {"paragraph", "heading", "listItem", "hardBreak"}:
        return inner + "\n"
    return inner


# -----------------------------------------------------------------------------
# Stage: Feature Author
# -----------------------------------------------------------------------------
class StageFeatureRunView(_ClientScopedAPIView):
    """POST /jobs/<uuid>/stage/feature/run/  — invoke agent 1."""

    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        # Prefer live Jira context when a connection + issue key are present.
        jira_summary = ""
        jira_description = ""
        if job.jira_issue_key:
            jclient, _ = _tenant_jira_client(job.client)
            if jclient:
                try:
                    issue = jclient.issue(job.jira_issue_key)
                    fields = (issue or {}).get("fields") or {}
                    jira_summary = str(fields.get("summary") or "")
                    jira_description = _adf_to_text(fields.get("description"))
                except Exception as exc:
                    logger.warning("Jira fetch failed for %s: %s", job.jira_issue_key, exc)
        try:
            output = agents.run_feature_agent(
                job,
                jira_summary=jira_summary,
                jira_description=jira_description,
            )
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"stage": job.stage, "output": output})


class StageFeatureApproveView(_ClientScopedAPIView):
    """POST /jobs/<uuid>/stage/feature/approve/

    Optional body: `{"edited_output": {...}, "reviewer_notes": "..."}` — if the
    reviewer tweaked the feature spec inline, pass it here and it overwrites
    `stage_feature_output` before the stage advances.
    """

    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        if job.stage != GenerationJob.STAGE_FEATURE:
            return Response(
                {"error": f"Cannot approve feature from stage={job.stage}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        edited = request.data.get("edited_output")
        notes = str(request.data.get("reviewer_notes") or "").strip()
        save_fields = ["stage", "stage_feature_output", "stage_history", "last_modified"]

        # Phase 13.5 — clients that DON'T ship an `edited_output` payload
        # (currently the PySide6 desktop app: its Feature panel is
        # read-only) still expect the Feature Author's extracted fields
        # to be promoted onto the job. Otherwise `job.base_url` sticks at
        # the model default `http://localhost:3000` + `job.seed_urls=['/']`
        # and every downstream stage (Manual → Plan → Artifacts) runs
        # against the wrong URL. Fall back to `stage_feature_output`
        # when the client didn't supply an edit.
        effective = edited if isinstance(edited, dict) else (job.stage_feature_output or {})

        if isinstance(effective, dict) and effective:
            # Persist edits (if any). When the client didn't send an edit,
            # this is a no-op because stage_feature_output already holds it.
            if isinstance(edited, dict):
                job.stage_feature_output = edited
            # The Feature Review "Seed URLs (comma-separated)" field is the
            # single source of truth for BOTH the app-under-test base URL and
            # the relative paths the tests navigate. Split absolute vs relative
            # here so downstream stages (Manual Tests, Plan, Artifacts, Execute)
            # see the operator's intent instead of the localhost fallback.
            base_url, seed_urls = _split_seed_urls(effective.get("seed_urls") or [])
            if base_url:
                job.base_url = base_url
                save_fields.append("base_url")
            if seed_urls:
                job.seed_urls = seed_urls
                save_fields.append("seed_urls")
            # Preconditions (HTTP Basic Auth, seed year, etc.) — reviewer can
            # override anything the Feature Author extracted from the Jira text.
            eff_pre = effective.get("preconditions")
            if isinstance(eff_pre, dict):
                job.preconditions = eff_pre
                save_fields.append("preconditions")

        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "feature_author",
            "decision": "approved",
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
            "notes": notes,
            "resolved_base_url": job.base_url,
            "resolved_seed_urls": job.seed_urls,
        })
        job.stage_history = history
        job.stage = GenerationJob.STAGE_MANUAL_TESTS
        job.save(update_fields=save_fields)
        return Response({
            "stage": job.stage,
            "base_url": job.base_url,
            "seed_urls": job.seed_urls,
        })


# -----------------------------------------------------------------------------
# Stage: Manual Tests
# -----------------------------------------------------------------------------
class StageManualTestsRunView(_ClientScopedAPIView):
    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        try:
            output = agents.run_manual_tests_agent(job)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"stage": job.stage, "output": output})


class StageManualTestsApproveView(_ClientScopedAPIView):
    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        if job.stage != GenerationJob.STAGE_MANUAL_TESTS:
            return Response(
                {"error": f"Cannot approve manual tests from stage={job.stage}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        edited = request.data.get("edited_output")
        notes = str(request.data.get("reviewer_notes") or "").strip()
        if isinstance(edited, dict):
            job.stage_manual_tests_output = edited
        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "manual_test_author",
            "decision": "approved",
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
            "notes": notes,
        })
        job.stage_history = history
        job.stage = GenerationJob.STAGE_PLAN
        job.save(update_fields=[
            "stage", "stage_manual_tests_output", "stage_history", "last_modified",
        ])
        return Response({"stage": job.stage})


# -----------------------------------------------------------------------------
# Stage: Plan Architect
# -----------------------------------------------------------------------------
def _resolve_plan_context(job: GenerationJob):
    """
    Build the enriched selector_map + allowed intent keys the Plan Architect
    and Artifact Generator both consume. Mirrors what generate_job_draft used
    to do inline, so we get the same Phase-5 enrichment for the pipeline.
    """
    # Cheap path first — reuse the crawl summary already stored on the job when
    # a previous stage populated it.
    crawl_summary = job.crawl_summary or {}
    if not crawl_summary.get("routes"):
        try:
            crawl_summary = _run_crawl_context(
                base_url=job.base_url,
                seed_urls=job.seed_urls or [],
                max_routes=job.max_routes,
            )
            job.crawl_summary = crawl_summary
        except Exception as exc:
            logger.warning("Crawl failed for job %s: %s", job.job_id, exc)
            crawl_summary = {"routes": [], "warnings": [f"crawl_error: {exc}"]}

    selector_map = _build_selector_map(crawl_summary)
    intent_keys = list(_available_intent_keys())
    notes = []
    planning_stub = {
        # Enrich against manual tests — treat each manual test as a scenario for
        # the merge walk. Structure only needs `steps[*].{selector,intent_key}`.
        "scenarios": _manual_tests_as_pseudo_scenarios(job.stage_manual_tests_output or {}),
    }
    _enrich_llm_context(planning_stub, selector_map, intent_keys, notes)
    return crawl_summary, selector_map, intent_keys, notes


def _manual_tests_as_pseudo_scenarios(manual_tests_output):
    """
    Convert manual test cases into pseudo-scenarios so `_enrich_llm_context`
    can walk them uniformly. Manual tests carry Given/When/Then arrays but no
    explicit selectors, so the walk is mostly a no-op — but if the reviewer
    pasted any inline `selector:` or `intent_key:` strings into the when/then
    edits, we'll pick them up.
    """
    scenarios = []
    for mt in (manual_tests_output or {}).get("manual_tests") or []:
        steps = []
        for clause in (mt.get("when") or []) + (mt.get("then") or []):
            # Reviewers can hand-annotate a clause like:
            #   "click #loginButton  intent_key=homepage_signin_cta"
            # This isn't required but is a nice escape hatch for edge cases.
            steps.append({"action": str(clause)})
        scenarios.append({"id": mt.get("id"), "title": mt.get("title"), "steps": steps})
    return scenarios


class StagePlanRunView(_ClientScopedAPIView):
    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)

        # Phase 5.6 — Auto-capture ui_knowledge here too, not just at
        # Artifact. The Plan agent picks the selectors that go into
        # `step.selector`; if it doesn't see ground truth, it invents
        # `#usernameInput` etc. and reviewers have to squint through
        # hallucinated JSON. Best-effort — any failure logs into the
        # capture report and Plan still runs.
        try:
            from . import ui_knowledge_capture
            capture_report = ui_knowledge_capture.ensure_snapshots_fresh(job)
            logger.info(
                "ui_knowledge_capture (plan stage) for job %s: captured=%d skipped=%d failed=%d",
                job.job_id,
                capture_report.get("captured", 0),
                capture_report.get("skipped", 0),
                capture_report.get("failed", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ui_knowledge_capture (plan) crashed for job %s: %s", job.job_id, exc)

        _, selector_map, intent_keys, enrichment_notes = _resolve_plan_context(job)
        # Persist crawl_summary now so re-runs skip the crawl.
        try:
            output = agents.run_plan_agent(
                job,
                selector_map=selector_map,
                intent_keys=intent_keys,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        # Append enrichment notes onto the job's llm_notes for the review UI.
        job.llm_notes = list(job.llm_notes or []) + enrichment_notes
        job.save(update_fields=["crawl_summary", "llm_notes", "last_modified"])
        return Response({"stage": job.stage, "output": output})


class StagePlanApproveView(_ClientScopedAPIView):
    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        if job.stage != GenerationJob.STAGE_PLAN:
            return Response(
                {"error": f"Cannot approve plan from stage={job.stage}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        edited = request.data.get("edited_output")
        notes = str(request.data.get("reviewer_notes") or "").strip()
        if isinstance(edited, dict):
            job.stage_plan_output = edited
        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "plan_architect",
            "decision": "approved",
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
            "notes": notes,
        })
        job.stage_history = history
        job.stage = GenerationJob.STAGE_ARTIFACTS
        job.save(update_fields=[
            "stage", "stage_plan_output", "stage_history", "last_modified",
        ])
        return Response({"stage": job.stage})


# -----------------------------------------------------------------------------
# Stage: Artifact Generator
# -----------------------------------------------------------------------------
# Keys the artifact agent SHOULD emit, plus fallbacks the LLM sometimes
# invents. Iteration order = preferred → fallback; first non-empty list wins.
_ARTIFACT_KEY_ALIASES = {
    GeneratedArtifact.TYPE_FEATURE: (
        "features", "feature_files", "featureFiles", "gherkin", "gherkin_files",
    ),
    GeneratedArtifact.TYPE_STEP_DEFINITIONS: (
        "step_definitions", "stepDefinitions", "step_defs", "steps", "stepdefs",
    ),
    GeneratedArtifact.TYPE_PAGE_OBJECT: (
        "page_objects", "pageObjects", "pages", "pom",
    ),
}


def _entries_for(agent_output: dict, keys: tuple) -> list:
    """Return the first non-empty list under any of `keys` in `agent_output`."""
    for k in keys:
        val = agent_output.get(k)
        if isinstance(val, list) and val:
            return val
    return []


# ---------------------------------------------------------------------------
# Page-object coercion — one class per seed URL
# ---------------------------------------------------------------------------
def _coerce_page_objects_to_seed_urls(raw_artifacts: List[dict], seed_urls: List[str]) -> List[dict]:
    """
    Reshape the LLM's page-object emissions into exactly one class per seed URL.

    Behaviour:
    - Build the target set from `seed_urls` (e.g. `/` → `HomePage`, `/login` →
      `LoginPage`).
    - Keep any LLM-emitted page object whose class name matches a target.
    - Merge everything else into the first target's class by appending the
      extras' method bodies as comments (so the operator can still see what the
      LLM tried to write) — this preserves the SafetyNet behaviour from
      Manual Tests and Plan.
    - Rewrite step-definitions imports so they only reference the surviving
      class names + file paths.
    """
    from test_generation.agents import _seed_url_to_class_name

    if not seed_urls:
        return raw_artifacts

    # Dedupe while preserving order — the mapper (post-fix) now correctly
    # produces the same class name for URL variants that share the same
    # path (e.g. `https://x.com/foo` and `/foo` both → `FooPage`). We
    # want exactly ONE page-object per unique class name; anything else
    # produces the phantom-duplicate-class bug.
    _seen: set = set()
    target_names: List[str] = []
    for u in seed_urls:
        name = _seed_url_to_class_name(u)
        if name not in _seen:
            _seen.add(name)
            target_names.append(name)
    target_paths = {
        name: f"tests/pages/generated/{name}.ts"
        for name in target_names
    }
    target_set = set(target_names)

    features = [a for a in raw_artifacts if a["artifact_type"] == GeneratedArtifact.TYPE_FEATURE]
    stepdefs = [a for a in raw_artifacts if a["artifact_type"] == GeneratedArtifact.TYPE_STEP_DEFINITIONS]
    page_objects = [a for a in raw_artifacts if a["artifact_type"] == GeneratedArtifact.TYPE_PAGE_OBJECT]
    others = [
        a for a in raw_artifacts
        if a["artifact_type"] not in (
            GeneratedArtifact.TYPE_FEATURE,
            GeneratedArtifact.TYPE_STEP_DEFINITIONS,
            GeneratedArtifact.TYPE_PAGE_OBJECT,
        )
    ]

    # Bucket page objects by their class name (path basename minus `.ts`).
    kept_by_name: Dict[str, dict] = {}
    extras: List[dict] = []
    for po in page_objects:
        stem = Path(po["relative_path"]).stem  # e.g. "LoginPage" from "tests/pages/generated/LoginPage.ts"
        if stem in target_set and stem not in kept_by_name:
            # Force the target path even if LLM emitted a slightly different one.
            po = dict(po)
            po["relative_path"] = target_paths[stem]
            kept_by_name[stem] = po
        else:
            extras.append(po)

    # Any target class we didn't get from the LLM — synthesise a minimal stub
    # so the pipeline still ends with N page-object files (matches seed count).
    for name in target_names:
        if name in kept_by_name:
            continue
        kept_by_name[name] = {
            "artifact_type": GeneratedArtifact.TYPE_PAGE_OBJECT,
            "relative_path": target_paths[name],
            "content": _stub_page_object(name),
        }

    # Fold extras into the first target. Previous behaviour buried the
    # extras' methods inside a /* ... */ comment block — dead code that
    # confused reviewers and left step-defs calling undefined methods
    # (`homePage.acceptCookies()` where `acceptCookies` lived in the
    # comment). New behaviour: promote the FIRST extra to be the body
    # of the target class (renamed to the target's class name) when
    # the target is currently a stub; then append any further extras
    # as the legacy comment block for reviewer visibility.
    if extras:
        first = target_names[0]
        base = dict(kept_by_name[first])
        if _class_body_is_empty(base.get("content") or ""):
            # Target is a stub / no methods → adopt the first extra's body.
            adopt = extras[0]
            promoted = _rename_class(
                str(adopt.get("content") or ""),
                new_name=first,
            )
            base["content"] = promoted
            leftover = extras[1:]
            if leftover:
                base["content"] += "\n\n" + _extras_comment_block(leftover)
        else:
            base["content"] = base["content"] + "\n\n" + _extras_comment_block(extras)
        kept_by_name[first] = base

    # Rewrite step-def imports so they only reference the surviving classes.
    # Pass the ordered list — the first-seed class is the merge target so
    # phantom `new PhantomName(...)` calls become `new FirstSeedClass(...)`,
    # which for Pulze is `ItItPage` (homepage) — the class we just
    # populated with the real methods. Passing a set here would let
    # alphabetical ordering pick the wrong target (e.g.
    # `ItItAccountDashboardPage` over `ItItPage`).
    stepdefs = [
        _rewrite_stepdefs_imports(sd, target_names) for sd in stepdefs
    ]

    return features + stepdefs + list(kept_by_name.values()) + others


def _rewrite_stepdefs_imports(stepdef: dict, allowed_class_names) -> dict:
    """
    Strip / rewrite page-object references in a step-definitions file so
    only the surviving class names remain. Handles both the `import` line
    AND every `new PhantomName(...)` constructor call in the step bodies.
    Any reference to a class the coercion merged away is replaced with
    the merged-target class (the FIRST surviving name in seed order —
    conventionally the homepage class, which is where extras get
    promoted by `_coerce_page_objects_to_seed_urls`).

    `allowed_class_names` may be an ORDERED list (preferred — first-seed
    class becomes the merge target) or a plain set (fallback: alphabetical).
    """
    content = stepdef.get("content") or ""
    if not content or not allowed_class_names:
        return stepdef

    # Preserve seed order when a list/tuple is passed; fall back to
    # alphabetical only for legacy callers.
    if isinstance(allowed_class_names, (list, tuple)):
        merge_target = allowed_class_names[0]
        allowed_set = set(allowed_class_names)
    else:
        merge_target = sorted(allowed_class_names)[0]
        allowed_set = set(allowed_class_names)
    # Use allowed_set for membership checks below (was `allowed_class_names`).
    allowed_class_names = allowed_set

    # Track class names the LLM referenced but that aren't in the survivor
    # set. These are the "phantoms" whose `new` calls must be rewritten.
    phantom_names: set = set()

    def _replace_import(m):
        symbol = m.group("symbol").strip()
        module = m.group("module")
        if "tests/pages/generated/" not in module:
            return m.group(0)
        ident_match = re.search(r"\b([A-Z][A-Za-z0-9_]*)\b", symbol)
        if not ident_match:
            return m.group(0)
        ident = ident_match.group(1)
        if ident in allowed_class_names:
            return m.group(0)
        phantom_names.add(ident)
        return f"import {{ {merge_target} }} from '../../tests/pages/generated/{merge_target}';"

    new_content = re.sub(
        r"import\s+(?P<symbol>[\w\{\}\s,]+)\s+from\s+['\"](?P<module>[^'\"]+)['\"]\s*;?",
        _replace_import,
        content,
    )

    # Also find phantoms that are referenced ONLY in step bodies (LLM
    # emitted the `new` call but never wrote the import). Scan for any
    # `new SomeName(` where SomeName isn't in the survivor set.
    for m in re.finditer(r"\bnew\s+([A-Z][A-Za-z0-9_]*)\s*\(", new_content):
        cls = m.group(1)
        if cls not in allowed_class_names:
            phantom_names.add(cls)

    # Rewrite every `new PhantomName(` → `new MergeTarget(`.
    for phantom in phantom_names:
        new_content = re.sub(
            rf"\bnew\s+{re.escape(phantom)}\s*\(",
            f"new {merge_target}(",
            new_content,
        )

    # Ensure the merge-target class is IMPORTED (if the LLM omitted its
    # import entirely and referenced it only via `new`). Best-effort: if
    # the import isn't already present, prepend one.
    if phantom_names and not re.search(
        rf"\bimport\s+\{{[^}}]*\b{re.escape(merge_target)}\b[^}}]*\}}\s+from\s+['\"]",
        new_content,
    ):
        # Insert after the first existing @cucumber/cucumber or
        # @playwright/test import — keeps imports grouped at the top.
        import_line = (
            f"import {{ {merge_target} }} from "
            f"'../../tests/pages/generated/{merge_target}';\n"
        )
        anchor = re.search(
            r"^import\s+[^;]*from\s+['\"](?:@cucumber/cucumber|@playwright/test)['\"]\s*;\s*\n",
            new_content, re.MULTILINE,
        )
        if anchor:
            new_content = new_content[:anchor.end()] + import_line + new_content[anchor.end():]
        else:
            new_content = import_line + new_content

    if new_content == content:
        return stepdef
    stepdef = dict(stepdef)
    stepdef["content"] = new_content
    return stepdef


def _split_seed_urls(entries) -> tuple[str, list[str]]:
    """
    Turn the "Seed URLs (comma-separated)" field from Feature Review into
    `(base_url, seed_urls)`. Rules:

      * The first entry that starts with `http://` or `https://` becomes
        `base_url` (host + optional path prefix like `/it-IT`).
      * Remaining full URLs are ignored — the pipeline is single-tenant per
        job by design.
      * Every relative entry (`/`, `/login`, `/account/dashboard`, etc.)
        becomes a `seed_urls` element in input order.
      * If NO absolute URL appears, `base_url` is returned as "" so the
        caller can decide whether to keep the previous value.

    Idempotent for already-clean input.
    """
    base_url = ""
    seed_urls: list[str] = []
    for entry in (entries or []):
        s = str(entry or "").strip()
        if not s:
            continue
        if s.lower().startswith(("http://", "https://")):
            if not base_url:
                base_url = s.rstrip("/")
        else:
            # Guarantee a leading slash so downstream Playwright/Cucumber
            # doesn't get relative-path confusion.
            if not s.startswith("/"):
                s = "/" + s
            seed_urls.append(s)
    return base_url, seed_urls


def _stub_page_object(class_name: str) -> str:
    """Minimal Playwright page-object stub the operator can flesh out later."""
    return (
        "import { Page } from '@playwright/test';\n"
        f"export class {class_name} {{\n"
        "  constructor(private page: Page) {}\n"
        "  // TODO: add selector fields + async action methods matching the plan.\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# Deterministic post-processing of LLM step-defs / page-objects
# ---------------------------------------------------------------------------
_STEPDEF_DECORATOR_METHOD_RE = re.compile(
    # `@Given('step text', ...) async methodName(param1, param2) {`
    r"@\s*(?P<verb>Given|When|Then|And|But)\s*\(\s*"
    r"(?P<step>['\"`])(?P<text>.*?)(?P=step)\s*\)\s*\n?\s*"
    r"(?:async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)\s*\{",
    re.DOTALL,
)


def _normalize_step_definitions_regex_fallback(stepdef: dict) -> dict:
    """
    Rewrite decorator-class step defs into Cucumber-JS module-level registrations.

    Transforms every `@Given('text', ...) async name(params) { … }` method inside
    an `export default class Steps { … }` wrapper into a top-level:

        Given('text', async function (params) { … });

    Also drops the class wrapper + constructor. If the file wasn't in that shape
    to begin with, we return it unchanged (`_normalize_step_definitions` is a
    no-op unless we detect the decorator pattern).

    Concurrently:
      * Ensures `import { expect } from '@playwright/test';` is present when
        the body references `expect(`.
      * Rewrites any `import HomePage from '.../HomePage'` (default) to
        `import { HomePage } from '.../HomePage'` (named).
      * Removes `And`/`But` from the `@cucumber/cucumber` import if they aren't
        used (they ARE valid — we just don't want to force them in).
    """
    content = stepdef.get("content") or ""
    if not content:
        return stepdef

    # --- Unescape literal `\n`, `\t`, `\r` sequences the LLM sometimes emits ---
    # These arrive as backslash-n characters (not actual newlines) — TS
    # rejects them as `TS1127 Invalid character`. Convert to real whitespace
    # so the rest of the normalizer + validator sees well-formed source.
    if "\\n" in content or "\\t" in content or "\\r" in content:
        content = (
            content.replace("\\r\\n", "\n")
                   .replace("\\n", "\n")
                   .replace("\\t", "  ")
                   .replace("\\r", "")
        )

    matches = list(_STEPDEF_DECORATOR_METHOD_RE.finditer(content))
    changed = False

    # --- Rewrite each decorator method into a module-level registration. -----
    if matches:
        pieces: List[str] = []
        cursor = 0
        for m in matches:
            pieces.append(content[cursor:m.start()])
            verb = m.group("verb")
            step_text = m.group("text")
            step_quote = m.group("step")
            params = m.group("params").strip()
            body_start = m.end()
            body_end = _find_matching_brace(content, body_start - 1)
            if body_end == -1:
                # Fallback: couldn't match braces, skip this method (kept as-is).
                pieces.append(content[m.start():body_start])
                cursor = body_start
                continue
            body = content[body_start:body_end]
            # Trim one leading blank line + trailing indentation for prettier output.
            body = body.rstrip() + "\n"
            registration = (
                f"{verb}({step_quote}{step_text}{step_quote}, async function ({params}) {{\n"
                f"{body}}});"
            )
            pieces.append(registration)
            cursor = body_end + 1  # skip past the closing `}`
        pieces.append(content[cursor:])
        content = "".join(pieces)
        changed = True

    # --- Strip the `export default class <Name> { … }` wrapper if present. ---
    class_match = re.search(r"export\s+default\s+class\s+\w+\s*\{", content)
    if class_match:
        wrapper_start = class_match.start()
        brace_open = content.index("{", class_match.start())
        brace_close = _find_matching_brace(content, brace_open)
        if brace_close != -1:
            inside = content[brace_open + 1:brace_close]
            # Drop any constructor(…) { … } that lived inside the class.
            inside = re.sub(
                r"constructor\s*\([^)]*\)\s*\{[^}]*\}\s*",
                "",
                inside,
                count=1,
            )
            content = content[:wrapper_start] + inside.strip() + "\n" + content[brace_close + 1:]
            changed = True

    # --- Ensure named page-object imports ------------------------------------
    def _default_to_named(m: re.Match) -> str:
        symbol = m.group(1).strip()
        module = m.group(2)
        return f"import {{ {symbol} }} from '{module}';"

    new_content = re.sub(
        r"import\s+([A-Z]\w*)\s+from\s+['\"]([^'\"]*tests/pages/generated/[^'\"]+)['\"]\s*;?",
        _default_to_named,
        content,
    )
    if new_content != content:
        content = new_content
        changed = True

    # --- Remove module-scope `const … = new PageObject(...)` ---------------
    # LLMs love writing `const homePage = new HomePage(this.page);` at the top
    # of the file. At module scope `this` is undefined, so requiring the file
    # throws before any step registers. Strip ALL such lines — but REMEMBER
    # the (varName -> ClassName) mapping so we can re-inject the assignment
    # at the top of each step body that references the variable. Without the
    # re-injection every downstream step throws `Cannot find name 'homePage'`.
    module_new_re = re.compile(
        r"^\s*const\s+(?P<name>\w+)\s*=\s*new\s+(?P<cls>[A-Z]\w*)\s*\([^)]*\)\s*;?\s*\n",
        re.MULTILINE,
    )
    var_class_map: Dict[str, str] = {}
    for m in module_new_re.finditer(content):
        var_class_map.setdefault(m.group("name"), m.group("cls"))
    stripped = module_new_re.sub("", content)
    if stripped != content:
        content = stripped
        changed = True

    # Also detect ANY imported page-object class and register multiple
    # candidate variable names — LLMs use both `homePage` (matches HomePage
    # exactly) and shorter forms like `home`, `accountLogin` (matches
    # AccountLoginPage minus the `Page` suffix). Cover all of them.
    def _camel(name: str) -> str:
        return name[:1].lower() + name[1:]

    for m in re.finditer(
        r"import\s*\{\s*([A-Z]\w*)\s*\}\s*from\s*['\"][^'\"]*(?:tests/pages/generated|pages/generated)[^'\"]*['\"]",
        content,
    ):
        cls = m.group(1)
        # `HomePage` → candidates: homePage, home
        # `AccountLoginPage` → candidates: accountLoginPage, accountLogin
        candidates = {_camel(cls)}
        if cls.endswith("Page"):
            candidates.add(_camel(cls[:-4]))
        for var in candidates:
            var_class_map.setdefault(var, cls)

    # Re-inject `const <var> = new <Class>(this.page);` at the top of each
    # step body that references `<var>` but never assigns to it locally.
    if var_class_map:
        step_body_re = re.compile(
            r"(?P<head>(?:Given|When|Then)\s*\(\s*['\"][^'\"]*['\"]\s*,\s*async\s+function\s*\([^)]*\)\s*\{)"
            r"(?P<body>[\s\S]*?)"
            r"(?P<tail>\n\s*\}\s*\)\s*;?)",
        )

        def _inject(m: re.Match) -> str:
            body = m.group("body")
            inject: List[str] = []
            for var, cls in var_class_map.items():
                references = re.search(rf"(?<![A-Za-z0-9_.]){re.escape(var)}\b", body)
                already_assigned = re.search(
                    rf"\b(?:const|let|var)\s+{re.escape(var)}\b", body
                )
                if references and not already_assigned:
                    inject.append(f"  const {var} = new {cls}(this.page);")
            if not inject:
                return m.group(0)
            return m.group("head") + "\n" + "\n".join(inject) + body + m.group("tail")

        new_content = step_body_re.sub(_inject, content)
        if new_content != content:
            content = new_content
            changed = True

    # --- Fix same-quote nesting inside string literals ----------------------
    # LLMs love writing `'[data-testid='foo']'` or `'[type='submit']'` — the
    # outer `'…'` is closed by the FIRST inner `'`, TypeScript then sees the
    # rest as junk and reports `TS1005 ',' expected`. Same for `"…"…"`. Fix
    # by flipping the outer quote to the other kind so the inner quotes are
    # preserved literally.
    #
    # Strategy: walk the text line-by-line; for each line find balanced
    # candidate strings that (a) contain an odd number of same-quote chars and
    # (b) look like a CSS/attribute selector (contain `=` or `[`). Rebuild the
    # line with the outer quote flipped.
    def _fix_line(line: str) -> str:
        for outer in ("'", '"'):
            other = '"' if outer == "'" else "'"
            # Pattern: <outer>...<outer>...<outer>...<outer>  with `=` inside
            # (attribute selector shape). Non-greedy so we catch the smallest.
            pattern = rf"{re.escape(outer)}([^{re.escape(outer)}\n]*={re.escape(outer)}[^{re.escape(outer)}\n]*{re.escape(outer)}[^{re.escape(outer)}\n]*){re.escape(outer)}"
            def _rewrite(m: re.Match) -> str:
                inner = m.group(1)
                if other in inner:
                    return m.group(0)  # can't flip without a real conflict
                return f"{other}{inner}{other}"
            line = re.sub(pattern, _rewrite, line)
        return line

    lines = content.split("\n")
    fixed_lines = [_fix_line(ln) for ln in lines]
    flipped_content = "\n".join(fixed_lines)
    if flipped_content != content:
        content = flipped_content
        changed = True

    # --- Deduplicate imports (LLM sometimes emits the same import twice) ----
    seen: set = set()
    kept_lines: List[str] = []
    dedup_changed = False
    for line in content.split("\n"):
        m = re.match(
            r"\s*import\s+(?:\{[^}]*\}|[A-Za-z_$][\w$]*)\s+from\s+['\"][^'\"]+['\"]\s*;?\s*$",
            line,
        )
        if m:
            key = re.sub(r"\s+", " ", line.strip().rstrip(";"))
            if key in seen:
                dedup_changed = True
                continue
            seen.add(key)
        kept_lines.append(line)
    if dedup_changed:
        content = "\n".join(kept_lines)
        changed = True

    # --- Ensure `expect` import if `expect(` is used -------------------------
    if "expect(" in content and not re.search(
        r"import\s*\{[^}]*\bexpect\b[^}]*\}\s*from\s*['\"]@playwright/test['\"]",
        content,
    ):
        content = "import { expect } from '@playwright/test';\n" + content
        changed = True

    # --- Map And(...) / But(...) → the previous Given/When/Then verb --------
    # @cucumber/cucumber v11 exports only Given/When/Then. `And` and `But` in
    # step defs must be registered with whichever of the three the preceding
    # step used (Gherkin `And`/`But` inherit the previous keyword's context).
    def _remap_and_but(text: str) -> str:
        # Walk registration calls in order, remember the last real verb, rewrite
        # And/But calls to that verb.
        result: List[str] = []
        cursor = 0
        last_verb = "Given"   # sensible default when the file starts with And
        for m in re.finditer(
            r"(?<![A-Za-z0-9_@])(Given|When|Then|And|But)\s*\(",
            text,
        ):
            verb = m.group(1)
            result.append(text[cursor:m.start()])
            if verb in ("And", "But"):
                result.append(last_verb + "(")
            else:
                result.append(verb + "(")
                last_verb = verb
            cursor = m.end()
        result.append(text[cursor:])
        return "".join(result)

    remapped = _remap_and_but(content)
    if remapped != content:
        content = remapped
        changed = True

    # --- Ensure Given/When/Then imports match usage --------------------------
    used_verbs = set()
    for verb in ("Given", "When", "Then"):
        if re.search(rf"(?<![A-Za-z0-9_@]){verb}\s*\(", content):
            used_verbs.add(verb)
    if used_verbs:
        cuc_import = re.search(
            r"import\s*\{([^}]*)\}\s*from\s*['\"]@cucumber/cucumber['\"]\s*;?",
            content,
        )
        if cuc_import:
            raw = {s.strip() for s in cuc_import.group(1).split(",") if s.strip()}
            # Drop any illegal And/But from the import list, add missing verbs.
            merged = sorted((raw - {"And", "But"}) | used_verbs)
            new_import = (
                "import { " + ", ".join(merged) + " } from '@cucumber/cucumber';"
            )
            if new_import != content[cuc_import.start():cuc_import.end()].rstrip(";") + ";":
                content = content[:cuc_import.start()] + new_import + content[cuc_import.end():]
                changed = True
        else:
            merged = sorted(used_verbs)
            content = (
                "import { " + ", ".join(merged) + " } from '@cucumber/cucumber';\n" + content
            )
            changed = True

    if changed:
        stepdef = dict(stepdef)
        stepdef["content"] = content
    return stepdef


def _find_matching_brace(text: str, open_idx: int) -> int:
    """
    Return the index of the `}` that closes the `{` at `open_idx`, respecting
    strings and comments. Returns -1 if not found. Simpler string/comment
    handling than the validator's balanced-braces scanner — good enough for
    class-body extraction on LLM output.
    """
    depth = 0
    in_str: str = ""
    in_line_comment = False
    in_block_comment = False
    i = open_idx
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1; continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2; continue
            i += 1; continue
        if in_str:
            if c == "\\":
                i += 2; continue
            if c == in_str:
                in_str = ""
            i += 1; continue
        if c == "/" and nxt == "/":
            in_line_comment = True; i += 2; continue
        if c == "/" and nxt == "*":
            in_block_comment = True; i += 2; continue
        if c in ("'", '"', "`"):
            in_str = c; i += 1; continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _normalize_page_object_regex_fallback(po: dict) -> dict:
    """
    Coerce common LLM shape drift into a valid page-object file:

      * `class Foo` → `export class Foo`
      * `export default new Foo()` → dropped
      * `export default Foo` → dropped (class already exported by name above)
    """
    content = po.get("content") or ""
    if not content:
        return po
    changed = False

    # Add `export` in front of `class <Name>` when missing.
    def _add_export(m: re.Match) -> str:
        return "export class " + m.group(1)

    new_content = re.sub(
        r"(?<![\w.])(?<!export\s)class\s+([A-Z]\w*)\b",
        _add_export,
        content,
    )
    if new_content != content:
        content = new_content
        changed = True

    # Drop `export default new Foo()` (with optional args + trailing semicolon).
    stripped = re.sub(
        r"export\s+default\s+new\s+[A-Z]\w*\s*\([^)]*\)\s*;?\s*",
        "",
        content,
    )
    if stripped != content:
        content = stripped
        changed = True

    # Drop plain `export default Foo;` (class is already named-exported).
    stripped = re.sub(r"export\s+default\s+[A-Z]\w*\s*;?\s*$", "", content, flags=re.MULTILINE)
    if stripped != content:
        content = stripped
        changed = True

    if changed:
        po = dict(po)
        po["content"] = content
    return po


# ---------------------------------------------------------------------------
# Sidecar-aware public wrappers (Phase 2)
# ---------------------------------------------------------------------------
def _normalize_via_sidecar(
    artifact: dict,
    artifact_type: str,
    context: dict,
) -> tuple[dict, dict | None]:
    """
    Try the AST sidecar (`test_generation/ts_normalizer/index.mjs`). Falls
    back to the regex normalizer on any failure.

    Returns `(artifact, sidecar_report_or_None)`. When the sidecar reports
    zero transformations we still return its report so operators can see
    the sidecar ran cleanly.
    """
    mode = getattr(_django_settings, "TS_NORMALIZER_MODE", "ast")
    content = artifact.get("content") or ""
    if not content:
        return artifact, None

    def _regex_pass(a: dict) -> dict:
        if artifact_type == GeneratedArtifact.TYPE_STEP_DEFINITIONS:
            return _normalize_step_definitions_regex_fallback(a)
        if artifact_type == GeneratedArtifact.TYPE_PAGE_OBJECT:
            return _normalize_page_object_regex_fallback(a)
        return a

    if mode == "regex":
        return _regex_pass(artifact), {
            "path": artifact.get("relative_path"),
            "sidecar": "disabled",
            "transformations": [],
        }

    new_content, report = ts_normalizer_client.normalize(
        artifact_type=artifact_type,
        relative_path=artifact.get("relative_path") or "unknown.ts",
        content=content,
        context=context,
    )

    if new_content is None:
        # Sidecar failed — fall back to regex.
        after = _regex_pass(artifact)
        fallback_report = dict(report or {})
        fallback_report["path"] = artifact.get("relative_path")
        fallback_report.setdefault("transformations", [])
        fallback_report["fallback"] = "regex"
        return after, fallback_report

    # Sidecar succeeded. In `both` mode, apply the regex normalizer on top
    # so we don't regress any edge case the sidecar doesn't cover yet.
    out = dict(artifact)
    out["content"] = new_content
    if mode == "both":
        out = _regex_pass(out)

    sidecar_report = {
        "path":            artifact.get("relative_path"),
        "sidecar":         report.get("sidecar", "ast"),
        "transformations": report.get("transformations") or [],
        "diagnostics":     report.get("diagnostics") or [],
    }
    return out, sidecar_report


def _normalize_step_definitions(stepdef: dict, context: dict | None = None) -> tuple[dict, dict | None]:
    """Public wrapper: sidecar-first, regex-fallback normalizer for step defs."""
    return _normalize_via_sidecar(
        stepdef,
        GeneratedArtifact.TYPE_STEP_DEFINITIONS,
        context or {},
    )


def _normalize_page_object(po: dict, context: dict | None = None) -> tuple[dict, dict | None]:
    """Public wrapper: sidecar-first, regex-fallback normalizer for page objects."""
    return _normalize_via_sidecar(
        po,
        GeneratedArtifact.TYPE_PAGE_OBJECT,
        context or {},
    )


def _class_name_from_path(rel_path: str) -> str:
    """
    Best-effort: `tests/pages/generated/blu-b2c/HomePage.ts` → `HomePage`.
    Feeds the sidecar's `known_page_objects` context so `named-imports`
    can safely rewrite default imports to named form.
    """
    if not rel_path:
        return ""
    tail = rel_path.replace("\\", "/").split("/")[-1]
    if tail.endswith(".ts"):
        tail = tail[:-3]
    # Keep only leading identifier chars — no punctuation.
    out = []
    for ch in tail:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out)


def _normalize_feature_file(artifact: dict) -> dict:
    """
    Coerce Gherkin step lines so their parameter values are QUOTED. This
    fixes the recurring "undefined step" bug where the LLM wrote:

        And I select my birth year as 1990          ← unquoted
        And I enter my username as 'valid_username' ← quoted (inconsistent)

    but the step-defs used `{string}` uniformly:

        When('I select my birth year as {string}', async function (year) { … })

    Cucumber can only match `{string}` to a QUOTED literal, so the
    unquoted `1990` reports as an undefined step and the run collapses.
    We rewrite unquoted trailing literals (integers, booleans, and bare
    words) to single-quoted form so both `{string}` and `{int}` step
    signatures match consistently. Idempotent — already-quoted values
    are left alone.
    """
    content = str(artifact.get("content") or "")
    if not content:
        return artifact

    def _rewrite_line(line: str) -> str:
        # Only touch Gherkin step lines (Given / When / Then / And / But).
        m = re.match(r"^(\s*(?:Given|When|Then|And|But)\s+)(.*)$", line)
        if not m:
            return line
        prefix, body = m.group(1), m.group(2)
        # Don't touch comments / tags.
        if body.startswith("#") or body.startswith("@"):
            return line
        # Common shape: "... as <VALUE>" or "... to <VALUE>" or "... = <VALUE>"
        # where VALUE is an unquoted numeric / boolean / bare identifier.
        # We quote when VALUE is trailing (end of line) and is a simple
        # number or a short bare word (< 32 chars, no whitespace).
        def _quote_match(m2):
            value = m2.group(2)
            # Already quoted → leave alone.
            if value.startswith(("'", '"')):
                return m2.group(0)
            return f'{m2.group(1)}\'{value}\''
        # Match trailing unquoted values after common keywords.
        new_body = re.sub(
            r"(\bas\s+|\bto\s+|=\s+)([^\s'\"]+)\s*$",
            _quote_match,
            body,
        )
        return prefix + new_body

    new_content = "\n".join(_rewrite_line(l) for l in content.splitlines())
    if not content.endswith("\n"):
        pass  # preserve
    elif not new_content.endswith("\n"):
        new_content += "\n"
    if new_content == content:
        return artifact
    out = dict(artifact)
    out["content"] = new_content
    return out


def _class_body_is_empty(content: str) -> bool:
    """
    True when the sole `export class X { ... }` block in `content` contains
    NO methods (no `async foo(...) {` / `foo(...) {`) and NO readonly
    fields. Used by `_coerce_page_objects_to_seed_urls` to decide whether
    it's safe to overwrite the target class's body with an extra's body
    (as opposed to appending as dead-code comments).
    """
    text = content or ""
    # Find the first class body, then look for method/field declarations
    # inside it.
    class_match = re.search(r"class\s+\w+\s*\{([\s\S]*?)\}\s*(?:$|\n)", text)
    if not class_match:
        return True
    body = class_match.group(1)
    # A `constructor(...) {}` alone is empty; anything else with `(` or
    # `readonly` counts as content.
    has_method = re.search(
        r"(?:async\s+)?[A-Za-z_$][\w$]*\s*\([^)]*\)\s*(?::[^{;]+)?\s*\{",
        body,
    )
    if has_method and "constructor" not in has_method.group(0):
        return False
    has_readonly = re.search(r"\breadonly\s+[A-Za-z_$]", body)
    if has_readonly:
        return False
    # If the only thing inside is `constructor(private page: Page) {}`,
    # it's effectively empty.
    return True


def _rename_class(content: str, new_name: str) -> str:
    """
    Rewrite the FIRST `export class X` / `class X` occurrence in `content`
    to use `new_name`. Also renames any `new X(...)` constructor calls in
    the same file (rare but possible when the extra had internal recursion).
    Idempotent — running twice is safe.
    """
    text = content or ""
    match = re.search(r"(export\s+)?class\s+(\w+)\b", text)
    if not match:
        return text
    old_name = match.group(2)
    if old_name == new_name:
        return text
    # Replace as a whole word to avoid clobbering substrings.
    return re.sub(rf"\b{re.escape(old_name)}\b", new_name, text)


def _extras_comment_block(extras: List[dict]) -> str:
    parts = ["/* --- Merged from LLM-emitted extra classes ---"]
    for e in extras:
        parts.append(f" * From: {e.get('relative_path')}")
        content = (e.get("content") or "").splitlines()
        for line in content[:60]:  # cap so a runaway class doesn't bloat the file
            parts.append(" *   " + line)
        if len(content) > 60:
            parts.append(f" *   ...({len(content) - 60} more lines truncated)")
        parts.append(" *")
    parts.append(" */\n")
    return "\n".join(parts)


def _persist_agent_artifacts(job: GenerationJob, agent_output: dict):
    """
    Convert the artifact-agent's JSON payload into GeneratedArtifact rows,
    validate them (path + content + TypeScript parse), and replace any prior
    rows for the same job.

    Also stashes a small preview of the raw agent output onto the last
    `stage_history` entry so operators can inspect what the LLM actually
    returned when zero artifacts land in the DB.

    Returns (rows, validation_summary, diagnostic).
    `diagnostic` is a dict describing what was extracted from the payload,
    used by the endpoint response so the panel can surface it.
    """
    if not isinstance(agent_output, dict):
        agent_output = {}

    def _classify_by_path(path: str) -> str:
        """
        Determine the artifact type from its on-disk path. The LLM sometimes
        misgroups files (e.g. drops a page object into the `step_definitions`
        array), and the validator downstream applies type-specific rules —
        so we trust the path, not the LLM's label.
        """
        p = (path or "").replace("\\", "/").lower()
        if p.endswith(".feature"):
            return GeneratedArtifact.TYPE_FEATURE
        if p.startswith("features/steps/") or p.endswith("-steps.ts") or p.endswith("_steps.ts"):
            return GeneratedArtifact.TYPE_STEP_DEFINITIONS
        if p.startswith("tests/pages/generated/") or p.endswith("pageobject.ts") or p.endswith("page.ts"):
            return GeneratedArtifact.TYPE_PAGE_OBJECT
        # Fallback — keep whatever the LLM said.
        return ""

    bundle_counts = {}
    raw_artifacts = []
    misgrouped = []  # {declared, actual, path}
    for declared_type, keys in _ARTIFACT_KEY_ALIASES.items():
        entries = _entries_for(agent_output, keys)
        bundle_counts[declared_type] = len(entries)
        for entry in entries:
            path = str((entry or {}).get("path") or "").strip()
            content = str((entry or {}).get("content") or "")
            if not path or not content:
                continue
            actual_type = _classify_by_path(path) or declared_type
            if actual_type != declared_type:
                misgrouped.append({
                    "declared": declared_type,
                    "actual": actual_type,
                    "path": path,
                })
            raw_artifacts.append({
                "artifact_type": actual_type,
                "relative_path": path,
                "content": content,
            })

    # Enforce "one page object per seed URL": if the LLM emitted extras
    # (ConsentPopup, HomePageLinks, InvalidCredentialsSubmissionAttempt, ...),
    # collapse them into the seed-URL-mapped classes. Also rewrites step-def
    # imports so they only reference the surviving class names.
    seed_urls = list(job.seed_urls or []) or ["/"]
    raw_artifacts = _coerce_page_objects_to_seed_urls(raw_artifacts, seed_urls)

    # Phase 5.3 — Deterministic locator fill. Walk every page-object file
    # and swap any locator that isn't in ui_knowledge's ground truth for
    # a matching-engine pick. Runs BEFORE normalization so the AST sidecar
    # operates on corrected content. No LLM in this loop.
    deterministic_fill_report = None
    try:
        from . import deterministic_fill
        deterministic_fill_report = deterministic_fill.apply_deterministic_fill(
            job, raw_artifacts
        )
        logger.info(
            "deterministic_fill for job %s: scanned=%d swapped=%d misses=%d",
            job.job_id,
            deterministic_fill_report.get("locators_scanned", 0),
            deterministic_fill_report.get("locators_swapped", 0),
            len(deterministic_fill_report.get("misses") or []),
        )
    except Exception as exc:  # noqa: BLE001 — never block persistence
        logger.exception("deterministic_fill crashed for job %s: %s", job.job_id, exc)
        deterministic_fill_report = {"enabled": True, "error": f"crashed: {exc}"}

    # Normalization pass (sidecar-first, regex fallback). Every transformed
    # file returns a small `sidecar_report` describing what got rewritten —
    # we collect these and stash them onto the stage_history entry so the
    # review UI (Django panel + desktop app) can show operators exactly
    # which auto-fixes ran.
    normalizer_report: List[dict] = []
    slug = getattr(job, "slug", "") or (job.client.slug if getattr(job, "client_id", None) else "")
    known_page_objects = sorted({
        _class_name_from_path(a["relative_path"])
        for a in raw_artifacts
        if a["artifact_type"] == GeneratedArtifact.TYPE_PAGE_OBJECT
    } - {""})
    norm_ctx = {"slug": slug, "known_page_objects": known_page_objects}

    normalized: List[dict] = []
    for a in raw_artifacts:
        if a["artifact_type"] == GeneratedArtifact.TYPE_STEP_DEFINITIONS:
            new_a, report = _normalize_step_definitions(a, norm_ctx)
        elif a["artifact_type"] == GeneratedArtifact.TYPE_PAGE_OBJECT:
            new_a, report = _normalize_page_object(a, norm_ctx)
        elif a["artifact_type"] == GeneratedArtifact.TYPE_FEATURE:
            new_a = _normalize_feature_file(a)
            report = None
        else:
            new_a, report = a, None
        normalized.append(new_a)
        if report and (report.get("transformations") or report.get("sidecar") == "error"):
            normalizer_report.append(report)
    raw_artifacts = normalized

    validated, summary = _validate_artifacts(raw_artifacts)

    # Replace prior rows atomically.
    job.artifacts.all().delete()
    rows = []
    for a in validated:
        rows.append(GeneratedArtifact(
            job=job,
            artifact_type=a["artifact_type"],
            relative_path=a["relative_path"],
            content_draft=a["content"],
            content_final=a["content"],
            checksum=_sha256(a["content"]),
            validation_status=a["validation_status"],
            validation_errors=a["validation_errors"],
            warnings=a["warnings"],
        ))
    GeneratedArtifact.objects.bulk_create(rows)

    # Diagnostic to help debug when the LLM returned nothing usable.
    diagnostic = {
        "top_level_keys": sorted(list(agent_output.keys())),
        "bundle_counts": bundle_counts,
        "persisted_count": len(rows),
        "misgrouped": misgrouped,   # LLM put a file under the wrong array key
        "notes": agent_output.get("notes") or [],
        "normalizer_report": normalizer_report,
        "deterministic_fill_report": deterministic_fill_report,
    }

    # Stash a preview of the raw agent output on the most recent history entry
    # so operators can see what the LLM said when persisted_count == 0.
    try:
        raw_preview = json.dumps(agent_output, default=str)[:4000]
        history = list(job.stage_history or [])
        if history:
            history[-1] = dict(history[-1] or {})
            history[-1]["raw_output_preview"] = raw_preview
            history[-1]["diagnostic"] = diagnostic
            job.stage_history = history
            job.save(update_fields=["stage_history", "last_modified"])
    except Exception:
        # Diagnostic write is best-effort; don't fail the endpoint over it.
        pass

    return rows, summary, diagnostic


class StageArtifactsRunView(_ClientScopedAPIView):
    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)

        # Phase 5.1 — Auto-capture ui_knowledge snapshots BEFORE plan-context
        # resolution so the artifact generator has fresh ground truth for
        # every seed URL. Best-effort: any failure is logged into the
        # capture report and the pipeline continues (the generator will
        # still work off whatever ui_knowledge already had).
        capture_report = None
        try:
            from . import ui_knowledge_capture
            capture_report = ui_knowledge_capture.ensure_snapshots_fresh(job)
            logger.info(
                "ui_knowledge_capture for job %s: captured=%d skipped=%d failed=%d",
                job.job_id,
                capture_report.get("captured", 0),
                capture_report.get("skipped", 0),
                capture_report.get("failed", 0),
            )
        except Exception as exc:  # noqa: BLE001 — never block the stage
            logger.exception("ui_knowledge_capture crashed for job %s: %s", job.job_id, exc)
            capture_report = {"enabled": True, "error": f"capture crashed: {exc}"}

        # Phase 9.7 — hard-fail when the Artifact stage is about to run with
        # no ground-truth locators. Otherwise the LLM invents selectors like
        # `[data-testid="logo-image"]` and every downstream repair pass has
        # to unwind them. Skipped for brand-new tenants (no base_url yet)
        # so onboarding isn't blocked. Kill-switch:
        # `XT_FORGE_REQUIRE_UI_KNOWLEDGE=false` in .env.
        require_ui = getattr(_django_settings, "XT_FORGE_REQUIRE_UI_KNOWLEDGE", True)
        if require_ui and job.base_url:
            from .agents import _build_ground_truth_inventory
            inventory = _build_ground_truth_inventory(job)
            # Phase 11.6 — first-run race guard. `ensure_snapshots_fresh`
            # above may have JUST written snapshot + element rows. On some
            # connection isolation combos (autocommit-per-statement on
            # Postgres) the inventory query above sees them stale. If we
            # captured something new but the inventory is empty, sleep 500ms
            # and re-query once before failing. Bounded — one retry, max 1s
            # added latency in the false-negative case.
            if (
                not inventory.get("per_url")
                and (capture_report or {}).get("captured", 0) > 0
            ):
                import time
                logger.info(
                    "ui_knowledge inventory empty after capture (captured=%d). "
                    "Sleeping 500ms and retrying visibility check.",
                    capture_report.get("captured", 0),
                )
                time.sleep(0.5)
                inventory = _build_ground_truth_inventory(job)
            if not inventory.get("per_url"):
                return Response(
                    {
                        "error": (
                            "ui_knowledge is empty for this tenant's seed URLs. "
                            "Run POST /ui_knowledge/sync/ for the seed URLs, then "
                            "retry this stage. To bypass this check (not "
                            "recommended — see Phase 9.7), set "
                            "XT_FORGE_REQUIRE_UI_KNOWLEDGE=false in the backend "
                            ".env."
                        ),
                        "capture_report": capture_report,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        _, selector_map, intent_keys, _ = _resolve_plan_context(job)
        try:
            output = agents.run_artifacts_agent(
                job,
                selector_map=selector_map,
                intent_keys=intent_keys,
            )
        except ValueError as exc:
            # ValueError commonly comes from _call_ollama_json when the LLM
            # returned truncated / unparseable JSON. Persist the reason onto
            # stage_history so the panel can surface it, then return 400 so the
            # front-end shows the message inline.
            reason = str(exc)
            try:
                history = list(job.stage_history or [])
                history.append({
                    "stage": job.stage,
                    "agent": "artifact_generator",
                    "decision": "llm_error",
                    "recorded_on": timezone.now().isoformat(),
                    "notes": reason[:2000],
                })
                job.stage_history = history
                job.save(update_fields=["stage_history", "last_modified"])
            except Exception:
                pass
            return Response({"error": reason}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Artifact agent raised for job %s", job.job_id)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        try:
            _, summary, diagnostic = _persist_agent_artifacts(job, output)
        except Exception as exc:
            logger.exception("Failed to persist agent artifacts for job %s", job.job_id)
            return Response({"error": f"persist failed: {exc}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        job.validation_summary = summary
        job.save(update_fields=["validation_summary", "last_modified"])

        # Phase 9.9 — surface the selector membership report the agent
        # produced. Attached to the same diagnostic bag as the selector
        # verifier + normalizer reports; also stashed on stage_history so
        # both review panels can render "N hallucinated selectors stripped".
        membership_report = output.pop("_selector_membership_report", None)
        if isinstance(membership_report, dict) and membership_report.get("enabled"):
            diagnostic["selector_membership_report"] = membership_report
            try:
                history = list(job.stage_history or [])
                if history:
                    history[-1] = dict(history[-1] or {})
                    prior_diag = history[-1].get("diagnostic") or {}
                    prior_diag["selector_membership_report"] = membership_report
                    history[-1]["diagnostic"] = prior_diag
                    job.stage_history = history
                    job.save(update_fields=["stage_history", "last_modified"])
            except Exception:  # noqa: BLE001
                pass
        # If the LLM returned nothing usable, help the operator diagnose it.
        if diagnostic["persisted_count"] == 0:
            logger.warning(
                "Artifact agent produced no persistable artifacts for job %s. "
                "top_level_keys=%s bundle_counts=%s",
                job.job_id, diagnostic["top_level_keys"], diagnostic["bundle_counts"],
            )

        # Selector verifier — probe every locator in every generated
        # page-object against the live DOM. Rewrites page-objects whose
        # locators don't resolve (via a tool-using GPT-4o loop). Best-effort:
        # never blocks the Artifact-stage response. Reads its kill-switch
        # from settings.SELECTOR_VERIFY_ENABLED.
        selector_verify_report = None
        if diagnostic["persisted_count"] > 0:
            try:
                from . import selector_verifier
                selector_verify_report = selector_verifier.verify(job)
                diagnostic["selector_verify_report"] = selector_verify_report
                # If any files were rewritten, re-validate their persisted
                # rows so the review panel shows the new validation status.
                if (selector_verify_report or {}).get("files_rewritten"):
                    try:
                        _revalidate_artifacts_in_place(job)
                    except Exception:  # noqa: BLE001
                        logger.exception("Post-verify revalidation failed for job %s", job.job_id)
                # Stash the report on the last stage_history entry so both
                # review panels can render it without hitting a new endpoint.
                try:
                    history = list(job.stage_history or [])
                    if history:
                        history[-1] = dict(history[-1] or {})
                        prior_diag = history[-1].get("diagnostic") or {}
                        prior_diag["selector_verify_report"] = selector_verify_report
                        history[-1]["diagnostic"] = prior_diag
                        job.stage_history = history
                        job.save(update_fields=["stage_history", "last_modified"])
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.exception("Selector verifier crashed for job %s: %s", job.job_id, exc)
                diagnostic["selector_verify_report"] = {
                    "enabled": True,
                    "error": f"verifier crashed: {exc}",
                }

        # Surface the Phase-5.1 auto-capture report so operators can see in
        # the review panel whether ground-truth snapshots were freshened for
        # this run's seed URLs.
        if capture_report is not None:
            diagnostic["ui_knowledge_capture"] = capture_report
            try:
                history = list(job.stage_history or [])
                if history:
                    history[-1] = dict(history[-1] or {})
                    prior_diag = history[-1].get("diagnostic") or {}
                    prior_diag["ui_knowledge_capture"] = capture_report
                    history[-1]["diagnostic"] = prior_diag
                    job.stage_history = history
                    job.save(update_fields=["stage_history", "last_modified"])
            except Exception:  # noqa: BLE001 — history stash is best-effort
                pass

        return Response({
            "stage": job.stage,
            "validation_summary": summary,
            "notes": output.get("notes") or [],
            "diagnostic": diagnostic,
        })


class StageArtifactsApproveView(_ClientScopedAPIView):
    """Approve + materialize + advance stage to EXECUTE in one shot."""

    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        if job.stage != GenerationJob.STAGE_ARTIFACTS:
            return Response(
                {"error": f"Cannot approve artifacts from stage={job.stage}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Materialize using the tenant slug (Phase 1 Option A per-client dirs).
        client_slug = getattr(getattr(job, "client", None), "slug", "") or ""
        try:
            result = materialize_job(
                job,
                allow_overwrite=True,
                client_slug=client_slug,
            )
        except Exception as exc:
            return Response({"error": f"materialize failed: {exc}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        if not result.ok:
            return Response(
                {
                    "error": "materialization produced conflicts/errors",
                    "conflicts": result.conflicts,
                    "errors": result.errors,
                    "written_files": result.written_files,
                },
                status=status.HTTP_409_CONFLICT if result.conflicts else status.HTTP_400_BAD_REQUEST,
            )

        notes = str(request.data.get("reviewer_notes") or "").strip()
        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "artifact_generator",
            "decision": "approved",
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
            "notes": notes,
            "written_files": result.written_files,
        })
        job.stage_history = history
        job.stage = GenerationJob.STAGE_EXECUTE
        job.job_status = GenerationJob.STATE_MATERIALIZED
        job.materialized_on = timezone.now()
        job.save(update_fields=[
            "stage", "job_status", "materialized_on", "stage_history", "last_modified",
        ])
        return Response({
            "stage": job.stage,
            "written_files": result.written_files,
        })


# -----------------------------------------------------------------------------
# Stage: Execute — fires the retry loop via django-q2
# -----------------------------------------------------------------------------
class StageExecuteRunView(_ClientScopedAPIView):
    """POST /jobs/<uuid>/stage/execute/run/  — enqueue the Executor task.

    The task lives in test_generation/executor.py::run_and_repair. It runs up
    to 3 Cucumber iterations, applying LLM-generated patches between failures,
    and updates `job.stage_execute_output` incrementally so the panel can poll.
    """

    def post(self, request, job_id):
        from django_q.tasks import async_task
        job = _get_job_scoped(request, job_id)
        # Phase 6.2 — Allow re-execute on completed jobs too. Original gate
        # only allowed EXECUTE (fresh run) and HUMAN_REVIEW_NEEDED (post-
        # human-fix retry). Now REPORT and DONE are also allowed so an
        # operator can re-run a passing job on demand (daily smoke,
        # regression check after a UI change, etc.).
        allowed_stages = {
            GenerationJob.STAGE_EXECUTE,
            GenerationJob.STAGE_HUMAN_REVIEW_NEEDED,
            GenerationJob.STAGE_REPORT,
            GenerationJob.STAGE_DONE,
        }
        if job.stage not in allowed_stages:
            return Response(
                {"error": f"Cannot execute from stage={job.stage}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Guard against duplicate enqueues. When a user smashes the "Run"
        # button while an earlier task is still processing (either
        # legitimately running OR sitting in the Django-Q queue because
        # the worker is down), each click was wiping stage_execute_output
        # and stacking another task. The result: the first task's output
        # gets clobbered, subsequent tasks queue behind it, and the
        # dashboard shows the job pinned at EXECUTE forever with no
        # iteration data.
        #
        # We treat the job as "already running" when: stage == EXECUTE
        # AND execute_iteration == 0 AND no iterations yet AND the most
        # recent enqueue in stage_history is < 60 seconds old. Return
        # 409 so the frontend can toast a clear message instead of
        # silently piling on.
        if (
            job.stage == GenerationJob.STAGE_EXECUTE
            and (job.execute_iteration or 0) == 0
            and not (job.stage_execute_output or {}).get("iterations")
        ):
            from datetime import datetime, timedelta, timezone as _tz
            recent_enqueue = None
            for h in reversed(job.stage_history or []):
                if (h or {}).get("agent") == "executor" and (h or {}).get("decision") in (
                    "enqueued", "smoke-rerun", "re-executed",
                ):
                    recent_enqueue = h
                    break
            if recent_enqueue and recent_enqueue.get("recorded_on"):
                try:
                    ts = datetime.fromisoformat(
                        str(recent_enqueue["recorded_on"]).replace("Z", "+00:00")
                    )
                    age = (datetime.now(_tz.utc) - ts).total_seconds()
                    if age < 60:
                        return Response(
                            {
                                "error": (
                                    "An execute task was enqueued for this job "
                                    f"{int(age)}s ago and hasn't produced any iterations "
                                    "yet. Wait for it to finish (or check that "
                                    "`manage.py qcluster` is running) before triggering another."
                                )
                            },
                            status=status.HTTP_409_CONFLICT,
                        )
                except (ValueError, TypeError):
                    pass
        # Tag the audit decision by intent so operators can filter later.
        rerun_intent = (
            "re-executed"
            if job.stage in (GenerationJob.STAGE_REPORT, GenerationJob.STAGE_DONE)
            else "enqueued"
        )

        # Phase 6.5.3 — Smoke-mode inference. A re-run on a previously
        # GREEN job (stage=REPORT or DONE) should behave like an Azure
        # Pipeline stage: run the existing Cucumber test as-is, produce
        # a pass/fail report, and STOP after iteration 1 if it fails —
        # no LLM, no Fixer. The operator sees the regression and decides
        # whether to trigger a full heal loop from the Execute panel.
        current_output = dict(job.stage_execute_output or {})
        prior_final_state = str(current_output.get("final_state") or "").upper()
        is_smoke_rerun = (
            prior_final_state == "GREEN"
            and job.stage in (GenerationJob.STAGE_REPORT, GenerationJob.STAGE_DONE)
        )

        # Phase 6.5.2 — Preserve iteration history on re-run. Archive
        # the current iterations into `previous_runs[]` before clearing
        # the slate. Convergence-detection state (previous_diagnoses,
        # patch_signatures) resets because those are per-run signals.
        current_iterations = list(current_output.get("iterations") or [])
        previous_runs = list(current_output.get("previous_runs") or [])
        if current_iterations:
            previous_runs.append({
                "run_index":       len(previous_runs) + 1,
                "final_state":     current_output.get("final_state"),
                "green_iteration": current_output.get("green_iteration"),
                "smoke_mode":      bool(current_output.get("smoke_mode")),
                "smoke_mode_failed": bool(current_output.get("smoke_mode_failed")),
                "started_on":      (current_iterations[0] or {}).get("started_on"),
                "finished_on":     (current_iterations[-1] or {}).get("finished_on"),
                "iterations":      current_iterations,
            })

        job.execute_iteration = 0
        job.stage_execute_output = {
            "iterations":         [],
            "previous_runs":      previous_runs,
            "previous_diagnoses": [],
            "patch_signatures":   [],
            "smoke_mode":         is_smoke_rerun,
        }
        job.stage = GenerationJob.STAGE_EXECUTE
        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "executor",
            "decision": "smoke-rerun" if is_smoke_rerun else rerun_intent,
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
        })
        job.stage_history = history
        job.save(update_fields=[
            "execute_iteration", "stage_execute_output", "stage", "stage_history", "last_modified",
        ])
        async_task(
            "test_generation.executor.run_and_repair",
            str(job.job_id),
            smoke_mode=is_smoke_rerun,
        )
        return Response(
            {"stage": job.stage, "enqueued": True, "smoke_mode": is_smoke_rerun},
            status=status.HTTP_202_ACCEPTED,
        )


class StageExecuteApproveView(_ClientScopedAPIView):
    """POST /jobs/<uuid>/stage/execute/approve/

    On success: fires the Jira Reporter agent, then posts an ADF comment to
    the job's Jira issue and attaches the materialized `.feature`, step defs,
    page objects, and the last iteration's log tail.

    Only valid when `stage_execute_output.final_state == "GREEN"` — the panel
    disables the button otherwise.
    """

    def post(self, request, job_id):
        job = _get_job_scoped(request, job_id)
        exec_out = job.stage_execute_output or {}
        if exec_out.get("final_state") != "GREEN":
            return Response(
                {"error": "Job has not reached a green execute state; cannot push to Jira yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not job.jira_issue_key:
            return Response(
                {"error": "Job has no jira_issue_key; nothing to push."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1. Reporter agent — build a human-readable summary.
        try:
            report = agents.run_reporter_agent(job)
        except Exception as exc:
            return Response({"error": f"reporter agent failed: {exc}"},
                            status=status.HTTP_502_BAD_GATEWAY)

        # 2. Push comment + attachments via integrations_jira.
        jclient, _ = _tenant_jira_client(job.client)
        if not jclient:
            return Response(
                {"error": "No Jira connection configured for this tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        push_result = _push_report_to_jira(jclient, job, report, exec_out)
        history = list(job.stage_history or [])
        history.append({
            "stage": job.stage,
            "agent": "jira_reporter",
            "decision": "pushed",
            "reviewer": request.user.username,
            "recorded_on": timezone.now().isoformat(),
            "notes": (report or {}).get("headline") or "",
        })
        job.stage_history = history
        job.stage = GenerationJob.STAGE_DONE
        job.save(update_fields=["stage", "stage_history", "last_modified"])
        return Response({
            "stage": job.stage,
            "jira_issue_key": job.jira_issue_key,
            "comment_posted": push_result.get("comment_posted"),
            "attachments": push_result.get("attachments"),
            "attach_errors": push_result.get("attach_errors"),
            "headline": (report or {}).get("headline"),
        })


# --- Jira push helpers -------------------------------------------------------
def _adf_text(text: str, marks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """ADF text node — optional inline marks (strong, code, link)."""
    node: Dict[str, Any] = {"type": "text", "text": str(text)}
    if marks:
        node["marks"] = marks
    return node


def _adf_paragraph(*text_nodes) -> Dict[str, Any]:
    return {"type": "paragraph", "content": list(text_nodes)}


def _adf_heading(level: int, text: str) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_table_cell(cell_type: str, text: str, *, is_code: bool = False) -> Dict[str, Any]:
    """Build a single ADF table cell. `cell_type` is 'tableHeader' or 'tableCell'."""
    marks = [{"type": "code"}] if is_code else None
    return {
        "type": cell_type,
        "content": [{"type": "paragraph", "content": [_adf_text(text or "", marks)]}],
    }


def _adf_table(headers: List[str], rows: List[List[str]]) -> Dict[str, Any]:
    """Build an ADF table with a header row + N body rows."""
    header_row = {
        "type": "tableRow",
        "content": [_adf_table_cell("tableHeader", h) for h in headers],
    }
    body_rows = [
        {"type": "tableRow",
         "content": [_adf_table_cell("tableCell", str(v)) for v in row]}
        for row in rows
    ]
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": [header_row] + body_rows}


def _adf_panel(panel_type: str, *paragraphs) -> Dict[str, Any]:
    """
    ADF panel node — one of: info, note, warning, success, error.
    Contents must be block-level nodes (paragraphs, lists, etc.).
    """
    return {
        "type": "panel",
        "attrs": {"panelType": panel_type},
        "content": list(paragraphs),
    }


def _adf_bullet_list(items: List[str], *, is_code: bool = False) -> Dict[str, Any]:
    marks = [{"type": "code"}] if is_code else None
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem",
             "content": [{"type": "paragraph",
                          "content": [_adf_text(str(item), marks)]}]}
            for item in items
        ],
    }


def _adf_from_report(report: dict, job=None, exec_out: Optional[dict] = None) -> dict:
    """
    Build a rich ADF document from (a) the Reporter agent's structured output
    AND (b) the raw stage_execute_output (Phase 6.1). Produces a beautiful
    Jira comment with:
      - Emoji-prefixed h2 headline (GREEN → 🟢, RED → 🔴)
      - Info/Success/Error panel with feature name + iteration count + scenarios summary + base URL
      - Body paragraphs from Reporter's `body_markdown`
      - Iteration table (# / Status / Pass-Fail / Duration / Diagnosis)
      - Failed-scenario table (only when RED)
      - Warning panel when any regression_report level ∈ {MAJOR_CHANGE, ELEMENT_REMOVED}
      - Bullet list of Reporter `highlights`
      - Bullet list of patches_applied (paths as inline code)
      - Footer link back to the Jobs dashboard

    Backwards compatible: when `job` / `exec_out` are None, produces the same
    minimal doc the old function did (headline + paragraphs + highlights).
    """
    content: List[Dict[str, Any]] = []
    headline = str((report or {}).get("headline") or "").strip()
    body_md = str((report or {}).get("body_markdown") or "").strip()
    highlights = list((report or {}).get("highlights") or [])

    # -- Compute state metadata (only when exec_out was passed in) --
    iterations: List[Dict[str, Any]] = list((exec_out or {}).get("iterations") or [])
    final_state = str((exec_out or {}).get("final_state") or "").upper()
    is_green = final_state == "GREEN"
    is_red = final_state in ("RED", "HUMAN_REVIEW_NEEDED", "STUCK_CONVERGED")
    emoji = "🟢" if is_green else ("🔴" if is_red else "⚙️")

    # -- Section 1: Headline heading --
    if headline:
        content.append(_adf_heading(2, f"{emoji} {headline}"))
    elif iterations:
        content.append(_adf_heading(2, f"{emoji} Test execution report"))

    # -- Section 2: Summary panel --
    if job is not None and iterations:
        passed_iters = sum(1 for it in iterations if it.get("all_passed"))
        total_iters = len(iterations)
        green_iter = (exec_out or {}).get("green_iteration")
        last_it = iterations[-1] if iterations else {}
        last_scenarios = list(last_it.get("scenarios") or [])
        last_passed = sum(1 for s in last_scenarios if s.get("status") == "passed")
        last_total = len(last_scenarios)

        summary_lines = [
            _adf_paragraph(
                _adf_text("Feature: ", [{"type": "strong"}]),
                _adf_text(str(getattr(job, "feature_name", "") or "(unnamed)")),
            ),
            _adf_paragraph(
                _adf_text("Result: ", [{"type": "strong"}]),
                _adf_text(final_state or "UNKNOWN"),
            ),
            _adf_paragraph(
                _adf_text("Iterations: ", [{"type": "strong"}]),
                _adf_text(
                    f"{total_iters} total"
                    + (f" · went green on #{green_iter}" if is_green and green_iter else "")
                ),
            ),
            _adf_paragraph(
                _adf_text("Scenarios (final iteration): ", [{"type": "strong"}]),
                _adf_text(f"{last_passed}/{last_total} passed"),
            ),
        ]
        base_url = str(getattr(job, "base_url", "") or "")
        if base_url:
            summary_lines.append(_adf_paragraph(
                _adf_text("Base URL: ", [{"type": "strong"}]),
                _adf_text(base_url, [{"type": "link", "attrs": {"href": base_url}}]),
            ))
        panel_type = "success" if is_green else ("error" if is_red else "info")
        content.append(_adf_panel(panel_type, *summary_lines))

    # -- Section 3: Reporter body_markdown (verbatim, paragraph-split) --
    if body_md:
        for para in [p for p in body_md.split("\n\n") if p.strip()]:
            content.append(_adf_paragraph(_adf_text(para.strip())))

    # -- Section 4: Iteration table --
    if iterations:
        content.append(_adf_heading(3, "Iterations"))
        rows = []
        for it in iterations:
            it_num = it.get("iteration", "?")
            status = "✓ passed" if it.get("all_passed") else ("crash" if it.get("crash_log_tail") else "✗ failed")
            scenarios = list(it.get("scenarios") or [])
            passed = sum(1 for s in scenarios if s.get("status") == "passed")
            total = len(scenarios)
            pass_fail = f"{passed}/{total}"
            # Compute duration if timestamps present.
            duration = "-"
            try:
                if it.get("started_on") and it.get("finished_on"):
                    from datetime import datetime as _dt
                    start = _dt.fromisoformat(it["started_on"].replace("Z", "+00:00"))
                    end = _dt.fromisoformat(it["finished_on"].replace("Z", "+00:00"))
                    duration = f"{(end - start).total_seconds():.1f}s"
            except Exception:  # noqa: BLE001
                pass
            diagnosis = str(it.get("diagnosis") or "")[:140]
            rows.append([str(it_num), status, pass_fail, duration, diagnosis])
        content.append(_adf_table(
            headers=["#", "Status", "Scenarios", "Duration", "Diagnosis"],
            rows=rows,
        ))

    # -- Section 5: Failed-scenario table (RED runs only) --
    if is_red and iterations:
        last_it = iterations[-1]
        failed = [s for s in (last_it.get("scenarios") or []) if s.get("status") == "failed"]
        if failed:
            content.append(_adf_heading(3, "Failed scenarios"))
            rows = []
            for s in failed[:15]:  # cap
                sf = s.get("step_failure") or {}
                step_text = f"{(sf.get('keyword') or '').strip()} {sf.get('name') or ''}".strip()
                err_head = str(sf.get("error_head") or "")[:200]
                rr = s.get("regression_report") or {}
                ui_level = str(rr.get("ui_change_level") or "").replace("_", " ").title() or "—"
                rows.append([
                    str(s.get("feature") or ""),
                    str(s.get("name") or ""),
                    step_text,
                    err_head,
                    ui_level,
                ])
            content.append(_adf_table(
                headers=["Feature", "Scenario", "Failed step", "Error", "UI change"],
                rows=rows,
            ))

    # -- Section 6: UI regression warning panel --
    if iterations:
        last_it = iterations[-1]
        regressions = []
        for s in (last_it.get("scenarios") or []):
            rr = s.get("regression_report") or {}
            level = str(rr.get("ui_change_level") or "").upper()
            if level in ("MAJOR_CHANGE", "ELEMENT_REMOVED"):
                added = list(rr.get("added_selectors") or [])[:3]
                removed = list(rr.get("removed_selectors") or [])[:3]
                delta_bits = []
                if added:
                    delta_bits.append(f"+{len(added)} added")
                if removed:
                    delta_bits.append(f"-{len(removed)} removed")
                regressions.append((s.get("name") or "?", level, ", ".join(delta_bits)))
        if regressions:
            paragraphs = [_adf_paragraph(_adf_text(
                "UI changed since the last capture. Check whether the test needs updating:",
            ))]
            paragraphs.append(_adf_bullet_list([
                f"{name}: {level.replace('_', ' ').lower()} ({delta})"
                for name, level, delta in regressions
            ]))
            content.append(_adf_panel("warning", *paragraphs))

    # -- Section 7: Reporter highlights --
    if highlights:
        content.append(_adf_heading(3, "Highlights"))
        content.append(_adf_bullet_list([str(h) for h in highlights]))

    # -- Section 8: Patches applied (deduped across iterations) --
    all_patches: List[str] = []
    for it in iterations:
        for p in (it.get("patches_applied") or []):
            if p and p not in all_patches:
                all_patches.append(p)
    if all_patches:
        content.append(_adf_heading(3, "Patches applied"))
        content.append(_adf_bullet_list(all_patches, is_code=True))

    # -- Section 9: Footer link back to Jobs dashboard --
    if job is not None:
        from django.conf import settings as _dj_settings
        public_base = str(getattr(_dj_settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
        if public_base:
            job_url = f"{public_base}/test-analytics/jobs/#{getattr(job, 'job_id', '')}"
            content.append(_adf_paragraph(
                _adf_text("Full run details: "),
                _adf_text(job_url, [{"type": "link", "attrs": {"href": job_url}}]),
            ))

    # -- Safety fallback: never post an empty comment --
    if not content:
        content = [_adf_paragraph(_adf_text(
            "Test run completed. No details provided by the reporter agent."
        ))]
    return {"type": "doc", "version": 1, "content": content}


def _push_report_to_jira(jclient, job, report, exec_out):
    """
    Push a rich ADF comment to the Jira issue (Phase 6.1). Never raises.

    By default, artifact + runner-log attachments are skipped — the goal is a
    single, beautifully-formatted comment inside Jira, not a scavenger hunt
    through downloads. Set `JIRA_REPORT_ATTACH_ARTIFACTS=on` in settings to
    restore the legacy attachment path (safety valve for tenants that need
    the raw files).
    """
    from pathlib import Path
    from django.conf import settings as _dj_settings

    result = {"comment_posted": False, "attachments": [], "attach_errors": []}

    # Rich ADF comment — the primary artifact of the push.
    try:
        jclient.add_comment(
            job.jira_issue_key,
            _adf_from_report(report, job=job, exec_out=exec_out),
        )
        result["comment_posted"] = True
    except Exception as exc:
        result["attach_errors"].append(f"comment failed: {exc}")

    # Legacy attachment path — off by default. Kept as a kill-switch.
    attach_flag = str(getattr(_dj_settings, "JIRA_REPORT_ATTACH_ARTIFACTS", "off")).lower()
    if attach_flag not in ("on", "1", "true", "yes"):
        return result

    for artifact in job.artifacts.all():
        rp = artifact.relative_path
        content = artifact.content_final or artifact.content_draft or ""
        if not rp or not content:
            continue
        filename = Path(rp).name
        try:
            jclient.attach_file(job.jira_issue_key, filename, content.encode("utf-8"))
            result["attachments"].append(filename)
        except Exception as exc:
            result["attach_errors"].append(f"attach {filename} failed: {exc}")

    iterations = (exec_out or {}).get("iterations") or []
    if iterations:
        last = iterations[-1]
        runner_id = last.get("runner_job_id")
        if runner_id:
            try:
                from runners.models import RunnerJob
                rj = RunnerJob.objects.get(id=runner_id)
                if rj.log_path and Path(rj.log_path).exists():
                    log_bytes = Path(rj.log_path).read_bytes()
                    tail = log_bytes[-64_000:]
                    jclient.attach_file(job.jira_issue_key,
                                        f"runner-{runner_id}.log", tail)
                    result["attachments"].append(f"runner-{runner_id}.log")
            except Exception as exc:
                result["attach_errors"].append(f"attach log failed: {exc}")

    return result
