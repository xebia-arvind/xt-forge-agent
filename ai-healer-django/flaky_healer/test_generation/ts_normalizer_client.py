"""
Thin Python-side wrapper around the Node `ts_normalizer` sidecar.

Contract with the sidecar (see `test_generation/ts_normalizer/README.md`):

  stdin  → { artifact_type, relative_path, content, context: { slug, known_page_objects } }
  stdout → { ok, content, transformations, errors, diagnostics }

Callers should treat this as best-effort. If the sidecar fails for any
reason (crash, timeout, `node` not installed, deps not installed) we return
`(None, error_report)` and the caller falls back to the regex path.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)


def normalize(
    artifact_type: str,
    relative_path: str,
    content: str,
    context: Optional[dict] = None,
) -> Tuple[Optional[str], dict]:
    """
    Invoke the ts_normalizer sidecar.

    Returns `(new_content, report)` on success where `report` looks like
    `{"transformations": [...], "diagnostics": [...], "sidecar": "ast"}`.
    Returns `(None, {"sidecar": "unavailable", "error": "..."})` on any
    failure so the caller can fall back to the legacy regex normalizer.
    """
    if not content:
        return content, {"transformations": [], "sidecar": "skipped-empty"}

    payload = {
        "artifact_type": artifact_type,
        "relative_path": relative_path,
        "content": content,
        "context": context or {},
    }

    script_path = getattr(settings, "TS_NORMALIZER_SCRIPT", "")
    node_bin    = getattr(settings, "TS_NORMALIZER_NODE_BIN", "node")
    timeout     = float(getattr(settings, "TS_NORMALIZER_TIMEOUT_SECONDS", 15))

    if not script_path:
        return None, {"sidecar": "unavailable", "error": "TS_NORMALIZER_SCRIPT not configured"}

    try:
        proc = subprocess.run(
            [node_bin, script_path],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, {"sidecar": "unavailable", "error": f"node binary '{node_bin}' not found"}
    except subprocess.TimeoutExpired:
        return None, {"sidecar": "unavailable", "error": f"sidecar timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001 — never let normalizer crash the pipeline
        return None, {"sidecar": "unavailable", "error": f"sidecar crashed: {exc}"}

    if proc.returncode != 0:
        return None, {
            "sidecar": "unavailable",
            "error": f"sidecar exit {proc.returncode}",
            "stderr_tail": (proc.stderr or b"").decode("utf-8", "replace")[-2000:],
        }

    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not raw:
        return None, {"sidecar": "unavailable", "error": "sidecar produced empty stdout"}

    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, {
            "sidecar": "unavailable",
            "error": f"sidecar stdout not JSON: {exc}",
            "stdout_tail": raw[-2000:],
        }

    if not response.get("ok"):
        return None, {
            "sidecar": "error",
            "errors": response.get("errors") or [],
            "transformations": response.get("transformations") or [],
        }

    new_content = response.get("content", content)
    report = {
        "sidecar": "ast",
        "transformations": response.get("transformations") or [],
        "diagnostics": response.get("diagnostics") or [],
    }
    return new_content, report
