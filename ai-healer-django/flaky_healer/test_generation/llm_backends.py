"""
LLM backend abstraction — Phase 1 of the robustness overhaul.

One implementation: `OpenAIBackend`. Ollama has been removed entirely per the
overhaul plan; if OpenAI is unreachable the pipeline halts with a clear error
rather than silently degrading to a weaker local model.

Every agent goes through `pick_backend(agent_name, tenant_config)` which
returns a `(LLMBackend, model_name)` tuple. Model routing lives here so
`agents.py` doesn't need to know which model powers which stage.

Cost + latency instrumentation is emitted as a `dict` from `.call(...)` so
callers can persist it onto `stage_history` for later analysis.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public error types
# ---------------------------------------------------------------------------
class LLMBackendError(RuntimeError):
    """Raised when the LLM backend can't be reached or returns something unusable."""


class LLMAPIKeyMissingError(LLMBackendError):
    """OPENAI_API_KEY isn't set. Pipeline should halt with a clear message."""


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------
# Agent name → default model. Overridable via env (`OPENAI_MODEL_<AGENT>`)
# or per-tenant `Clients.llm_config` JSON.
DEFAULT_MODELS: Dict[str, str] = {
    "feature_author": "gpt-4o-mini",
    "manual_test_author": "gpt-4o-mini",
    "plan_architect": "gpt-4o",
    "artifact_generator": "gpt-4o",
    "root_cause_fixer": "gpt-4o",
    "selector_verifier": "gpt-4o",
}

_ENV_KEY_BY_AGENT: Dict[str, str] = {
    "feature_author": "OPENAI_MODEL_FEATURE",
    "manual_test_author": "OPENAI_MODEL_MANUAL_TESTS",
    "plan_architect": "OPENAI_MODEL_PLAN",
    "artifact_generator": "OPENAI_MODEL_ARTIFACTS",
    "root_cause_fixer": "OPENAI_MODEL_FIXER",
    "selector_verifier": "OPENAI_MODEL_SELECTOR_VERIFIER",
}


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------
@dataclass
class LLMResult:
    """Structured output from an LLM call. Callers stash `usage` on stage_history."""
    payload: Dict[str, Any]              # parsed JSON response body
    model: str                            # actual model used
    usage: Dict[str, Any]                 # {input_tokens, cached_input_tokens, output_tokens, wall_clock_ms}
    raw_text: str = ""                    # unparsed response, for debugging

    def as_dict(self) -> Dict[str, Any]:
        return {"payload": self.payload, "model": self.model, "usage": self.usage}


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
class LLMBackend:
    name: str = "base"

    def call(self, *, system: str, user: str, model: str,
             temperature: float = 0.0, max_output_tokens: int = 2400,
             timeout_seconds: int = 120) -> LLMResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------
class OpenAIBackend(LLMBackend):
    name = "openai"

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMAPIKeyMissingError(
                "OPENAI_API_KEY is not set. Set it in the environment "
                "(the pipeline no longer has an offline fallback)."
            )
        self.client = OpenAI(api_key=key)

    def call(self, *, system: str, user: str, model: str,
             temperature: float = 0.0, max_output_tokens: int = 2400,
             timeout_seconds: int = 120) -> LLMResult:
        started = time.time()

        # `chat.completions` with `response_format` is the widely-supported
        # path for JSON mode. The Responses API is newer but not all models
        # (e.g. gpt-4o-mini) accept the same options. Sticking with chat.completions
        # for the non-tool-using code path keeps compatibility broad.
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_output_tokens,
                response_format={"type": "json_object"},
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — surface network + auth errors uniformly
            raise LLMBackendError(f"OpenAI call failed: {exc}") from exc

        wall_ms = int((time.time() - started) * 1000)
        text = (resp.choices[0].message.content if resp.choices else "") or ""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMBackendError(
                f"OpenAI returned non-JSON in json_object mode: {exc}. Head: {text[:200]!r}"
            ) from exc

        usage_obj = getattr(resp, "usage", None)
        cached = 0
        if usage_obj is not None:
            details = getattr(usage_obj, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0

        usage = {
            "input_tokens": getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0,
            "cached_input_tokens": cached,
            "output_tokens": getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0,
            "wall_clock_ms": wall_ms,
        }
        return LLMResult(payload=payload, model=model, usage=usage, raw_text=text)


# ---------------------------------------------------------------------------
# Model picker
# ---------------------------------------------------------------------------
def _model_for(agent_name: str, tenant_config: Optional[Dict[str, Any]] = None) -> str:
    """
    Resolve the model to use for `agent_name` in this order:
      1. `tenant_config["models"][agent_name]` if set on the Client's `llm_config`.
      2. Env var `OPENAI_MODEL_<AGENT>` if set.
      3. `DEFAULT_MODELS[agent_name]`.
    """
    if tenant_config:
        by_agent = (tenant_config.get("models") or {}).get(agent_name)
        if by_agent:
            return str(by_agent)
    env_key = _ENV_KEY_BY_AGENT.get(agent_name)
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
    return DEFAULT_MODELS.get(agent_name, "gpt-4o-mini")


_singleton: Optional[OpenAIBackend] = None


def _backend() -> OpenAIBackend:
    """Lazy-instantiate the OpenAI client so import time doesn't require the key."""
    global _singleton
    if _singleton is None:
        _singleton = OpenAIBackend()
    return _singleton


def pick_backend(agent_name: str,
                 tenant_config: Optional[Dict[str, Any]] = None) -> Tuple[LLMBackend, str]:
    """
    Return `(backend, model)` for the given agent. Every code path that calls
    the LLM goes through this — makes it easy to swap models or wrap with
    caching later.
    """
    return _backend(), _model_for(agent_name, tenant_config)


# ---------------------------------------------------------------------------
# Tenant-config helper — used by `agents.py` to read overrides off `job.client`.
# ---------------------------------------------------------------------------
def tenant_config_for_job(job) -> Dict[str, Any]:
    client = getattr(job, "client", None)
    if client is None:
        return {}
    return dict(getattr(client, "llm_config", None) or {})
