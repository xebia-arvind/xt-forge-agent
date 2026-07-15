"""
django-q2 tasks that shell out to `npm` / `npx` from the repo root and stream
stdout+stderr into the runner's log file.

Every task takes only the `RunnerJob.id`; all context lives on the row. This
keeps the queue payload tiny and lets the worker recover the full state on
retry without the caller having to pickle anything.
"""
from __future__ import annotations

import os
import subprocess

from django.conf import settings
from django.utils import timezone

from .models import RunnerJob


def _open_log(job: RunnerJob):
    # Log file is opened line-buffered so SSE tails see output immediately.
    fh = open(job.log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    fh.write(f"[runner] pid={os.getpid()} kind={job.kind} argv={job.argv}\n")
    return fh


def _run(job: RunnerJob):
    job.state = RunnerJob.STATE_RUNNING
    job.started_on = timezone.now()
    job.save(update_fields=["state", "started_on", "last_modified"])

    env = dict(os.environ)
    env["FORCE_COLOR"] = "0"
    env["NO_COLOR"] = "1"
    for k, v in (job.env_overrides or {}).items():
        env[str(k)] = str(v)

    fh = _open_log(job)
    try:
        # stdin=DEVNULL prevents `npx` (and any child prompt like
        # "cucumber-js@1.0.0 — Ok to proceed? (y)") from hanging the worker.
        # If the child needs interactive input, it now fails fast on EOF
        # rather than blocking forever waiting for stdin from a worker
        # process that has no TTY.
        proc = subprocess.Popen(
            job.argv,
            cwd=job.cwd or settings.REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        rc = proc.wait(timeout=settings.Q_CLUSTER.get("timeout", 3600))
        job.return_code = rc
        job.state = RunnerJob.STATE_SUCCESS if rc == 0 else RunnerJob.STATE_FAILED
    except subprocess.TimeoutExpired:
        proc.kill()
        job.return_code = -1
        job.state = RunnerJob.STATE_FAILED
        job.error_message = "Task exceeded Q_CLUSTER timeout."
        fh.write("\n[runner] TIMEOUT — process killed.\n")
    except Exception as exc:  # pragma: no cover - defensive
        job.return_code = -1
        job.state = RunnerJob.STATE_FAILED
        job.error_message = f"{type(exc).__name__}: {exc}"
        fh.write(f"\n[runner] ERROR: {job.error_message}\n")
    finally:
        job.finished_on = timezone.now()
        job.save(update_fields=["return_code", "state", "finished_on", "error_message", "last_modified"])
        fh.write(f"\n[runner] exit_code={job.return_code} state={job.state}\n")
        fh.close()


def run_generate(job_id: int) -> None:
    """Task entry point for GEN — runs `npm run gen:testcases`."""
    job = RunnerJob.objects.get(id=job_id)
    _run(job)


def run_execute(job_id: int) -> None:
    """Task entry point for EXECUTE — runs `npx playwright test [args]`."""
    job = RunnerJob.objects.get(id=job_id)
    _run(job)


def run_cucumber(job_id: int) -> None:
    """Task entry point for CUCUMBER — runs `npx cucumber-js [args]`.

    Uses the same `_run` primitive as the other kinds; nothing Cucumber-specific
    lives here. The `argv` and `env_overrides` on the RunnerJob row already
    encode the target profile (via TENANT_SLUG) and the report path.
    """
    job = RunnerJob.objects.get(id=job_id)
    _run(job)
