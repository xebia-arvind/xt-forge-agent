"""
HTTP surface for the runners app:

    POST /runners/generate/               → enqueue a GEN job
    POST /runners/execute/                → enqueue an EXECUTE job
    GET  /runners/jobs/                   → list current-tenant jobs
    GET  /runners/jobs/<id>/              → job detail
    GET  /runners/jobs/<id>/stream/       → SSE tail of the log file
"""
from __future__ import annotations

import os
import time

from django.conf import settings
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_q.tasks import async_task
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from clients.mixins import require_client

from .models import RunnerJob
from .serializers import ExecuteRequestSerializer, GenerateRequestSerializer, RunnerJobSerializer


class ServerSentEventRenderer(BaseRenderer):
    """DRF renderer used by the SSE stream view.

    The stream view writes StreamingHttpResponse directly, so `render()` is
    never actually invoked — this class exists purely to satisfy DRF's
    content-negotiation step: it advertises `text/event-stream` so browsers
    setting `Accept: text/event-stream` (the default for `EventSource`) don't
    get a 406.
    """
    media_type = "text/event-stream"
    format = "txt"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):  # pragma: no cover
        if isinstance(data, (bytes, str)):
            return data
        return str(data or "").encode("utf-8")


class _ClientScopedAPIView(APIView):
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


def _make_log_path(job_id: int) -> str:
    return os.path.join(settings.RUNNER_LOG_DIR, f"job_{job_id}.log")


def _create_and_enqueue(client, kind: str, argv, env_overrides=None, task_path: str = "runners.tasks.run_generate"):
    job = RunnerJob.objects.create(
        client=client,
        kind=kind,
        state=RunnerJob.STATE_QUEUED,
        argv=list(argv),
        cwd=settings.REPO_ROOT,
        env_overrides=env_overrides or {},
        log_path="",  # set after id is known
    )
    job.log_path = _make_log_path(job.id)
    # Touch the file so SSE can tail immediately without a race.
    open(job.log_path, "a", encoding="utf-8").close()
    job.save(update_fields=["log_path", "last_modified"])
    async_task(task_path, job.id, task_name=f"{kind}-{job.id}")
    return job


class GenerateEnqueueView(_ClientScopedAPIView):
    def post(self, request):
        client = require_client(request)
        GenerateRequestSerializer(data=request.data).is_valid(raise_exception=True)
        job = _create_and_enqueue(
            client=client,
            kind=RunnerJob.KIND_GEN,
            argv=["npm", "run", "gen:testcases"],
            task_path="runners.tasks.run_generate",
        )
        return Response(RunnerJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class ExecuteEnqueueView(_ClientScopedAPIView):
    def post(self, request):
        client = require_client(request)
        serializer = ExecuteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Playwright discovers all specs when no path is given — default the
        # "run everything" case to the client's per-tenant subdirectory so we
        # don't accidentally trigger someone else's suite.
        spec = data.get("spec") or f"tests/generated/{client.slug}/"
        argv = ["npx", "playwright", "test", spec]
        if data["grep"]:
            argv += ["-g", data["grep"]]
        argv.append(f"--workers={data['workers']}")

        env_overrides = {"HEADLESS": "false" if data["headed"] else "true"}
        if data.get("base_url"):
            env_overrides["BASE_URL"] = data["base_url"]
        job = _create_and_enqueue(
            client=client,
            kind=RunnerJob.KIND_EXECUTE,
            argv=argv,
            env_overrides=env_overrides,
            task_path="runners.tasks.run_execute",
        )
        return Response(RunnerJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class RunnerJobListView(_ClientScopedAPIView):
    def get(self, request):
        client = require_client(request)
        qs = RunnerJob.objects.filter(client=client)[:50]
        return Response(RunnerJobSerializer(qs, many=True).data)


class RunnerJobDetailView(_ClientScopedAPIView):
    def get(self, request, job_id: int):
        client = require_client(request)
        job = get_object_or_404(RunnerJob, id=job_id, client=client)
        return Response(RunnerJobSerializer(job).data)


class RunnerJobStreamView(_ClientScopedAPIView):
    """
    Server-Sent Events tail of the job's log file.

    Emits `event: log` frames with each new line and `event: done` when the
    subprocess exits. The connection is kept alive with a comment ping every
    ~15 s so proxies don't time out during long silent stretches.
    """

    # Advertise text/event-stream so browsers using EventSource (Accept:
    # text/event-stream) satisfy DRF's content negotiation. JSONRenderer is
    # kept as a fallback for tools that request JSON explicitly.
    renderer_classes = [ServerSentEventRenderer]

    def get(self, request, job_id: int):
        client = require_client(request)
        job = get_object_or_404(RunnerJob, id=job_id, client=client)
        response = StreamingHttpResponse(self._iter(job), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def _iter(self, job: RunnerJob):
        path = job.log_path
        if not path or not os.path.exists(path):
            yield "event: error\ndata: log file missing\n\n"
            return

        last_ping = time.monotonic()
        stream_start = time.monotonic()
        # If a job hasn't started running after this many seconds, the qcluster
        # worker is probably down. Warn and let the browser stop waiting.
        queue_timeout_s = 120
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            # First flush: emit everything we already have.
            for line in fh:
                yield f"event: log\ndata: {line.rstrip()}\n\n"

            # Then tail — poll every 500 ms until job is terminal.
            terminal = {RunnerJob.STATE_SUCCESS, RunnerJob.STATE_FAILED, RunnerJob.STATE_CANCELLED}
            while True:
                where = fh.tell()
                line = fh.readline()
                if line:
                    yield f"event: log\ndata: {line.rstrip()}\n\n"
                else:
                    fh.seek(where)
                    time.sleep(0.5)
                    # Refresh state; break when done.
                    job.refresh_from_db(fields=["state", "return_code"])
                    if job.state == RunnerJob.STATE_QUEUED and (time.monotonic() - stream_start) > queue_timeout_s:
                        yield (
                            "event: log\ndata: [runner] Job is still QUEUED after 2 min. "
                            "Is `python manage.py qcluster` running?\n\n"
                        )
                        yield f"event: done\ndata: {job.state} rc=None\n\n"
                        return
                    if job.state in terminal:
                        # Drain anything that appeared after the last read.
                        tail = fh.read()
                        for extra in (tail or "").splitlines():
                            yield f"event: log\ndata: {extra}\n\n"
                        yield f"event: done\ndata: {job.state} rc={job.return_code}\n\n"
                        return
                    now = time.monotonic()
                    if now - last_ping > 15:
                        yield ": keep-alive\n\n"
                        last_ping = now
