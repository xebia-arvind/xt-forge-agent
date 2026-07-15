"""
Minimal Playwright MCP client used by the Root-Cause Fixer loop.

We spawn `npx --yes @playwright/mcp@latest` as a subprocess and speak JSON-RPC
2.0 over its stdio channel. Only three MCP tool calls are needed:

    * `browser_navigate(url)` — open a page.
    * `browser_snapshot()`    — get the accessibility tree + URL.
    * (implicit) close — killed by the context manager on exit.

This module is intentionally free-standing: no external MCP library. When the
`@playwright/mcp` binary is missing or malfunctioning, callers get a `None`
client and fall back to legacy behaviour (empty DOM). MCP is a nice-to-have
augment, not a hard dependency.

Wire it via:

    with playwright_mcp_client() as client:
        if client is not None:
            client.open(url)
            snap = client.snapshot()
            dom  = snap.get("html", "")
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# Env-tunable so the operator can pin a specific @playwright/mcp version.
_MCP_PACKAGE = os.environ.get("PLAYWRIGHT_MCP_PACKAGE", "@playwright/mcp@latest")
_MCP_STARTUP_TIMEOUT = float(os.environ.get("PLAYWRIGHT_MCP_STARTUP_TIMEOUT", "20"))
_MCP_CALL_TIMEOUT = float(os.environ.get("PLAYWRIGHT_MCP_CALL_TIMEOUT", "20"))


class MCPUnavailable(RuntimeError):
    """Raised when the Playwright MCP server can't be launched or reached."""


class PlaywrightMCPClient:
    """
    Thin JSON-RPC 2.0 client over stdio. Not thread-safe — one client instance
    per iteration cycle. Every call has a hard timeout so a hung server can't
    jam the Django-Q worker.
    """

    def __init__(self, package: str = _MCP_PACKAGE):
        self.package = package
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._reader_thread: Optional[threading.Thread] = None
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._pending_ready = threading.Event()
        self._stderr_lines: list = []

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not shutil.which("npx"):
            raise MCPUnavailable("npx not found on PATH")

        try:
            self.proc = subprocess.Popen(
                ["npx", "--yes", self.package],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, "CI": "1", "NO_COLOR": "1"},
            )
        except OSError as exc:
            raise MCPUnavailable(f"Could not spawn Playwright MCP: {exc}") from exc

        # Drain stderr in the background so a chatty server never blocks writes.
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr, daemon=True,
        )
        self._stderr_thread.start()

        self._reader_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader_thread.start()

        # Initialize handshake — MCP spec requires an `initialize` first.
        try:
            self._call("initialize", {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "xt-forge-root-cause-fixer", "version": "0.1"},
                "capabilities": {},
            }, timeout=_MCP_STARTUP_TIMEOUT)
        except Exception as exc:
            self.stop()
            raise MCPUnavailable(f"MCP initialize failed: {exc}") from exc

        # Notify the server we're ready.
        try:
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            # Non-fatal — some servers don't require the notification.
            pass

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    # ------------------------------------------------------------------
    def open(self, url: str) -> Dict[str, Any]:
        return self._tool_call("browser_navigate", {"url": url})

    def snapshot(self) -> Dict[str, Any]:
        """
        Returns whatever the MCP server calls its DOM/accessibility snapshot.

        `@playwright/mcp` exposes `browser_snapshot` which returns an
        accessibility-tree-style YAML block; we surface the raw text so the
        LLM prompt can decide what to keep. If a `text` field is present in
        the tool result we return it under `html` (best-effort naming — the
        prompt just interpolates it, doesn't care about the field name).
        """
        result = self._tool_call("browser_snapshot", {})
        # `tools/call` results wrap payloads in `content: [{type, text}, …]`.
        text = ""
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                text += str(item.get("text") or "")
        return {"raw": result, "html": text}

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------
    def _tool_call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("tools/call", {"name": tool, "arguments": arguments})

    def _call(self, method: str, params: Dict[str, Any], timeout: float = _MCP_CALL_TIMEOUT) -> Dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise MCPUnavailable("MCP server not running")

        rid = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        with self._pending_lock:
            self._pending[rid] = {"event": threading.Event(), "result": None, "error": None}

        self._send(payload)

        entry = self._pending[rid]
        got = entry["event"].wait(timeout=timeout)
        with self._pending_lock:
            self._pending.pop(rid, None)
        if not got:
            raise MCPUnavailable(f"MCP call {method!r} timed out after {timeout}s")
        if entry["error"] is not None:
            raise MCPUnavailable(f"MCP {method!r} error: {entry['error']}")
        return entry["result"] or {}

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPUnavailable("MCP stdin closed")
        line = json.dumps(payload) + "\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPUnavailable(f"MCP stdin write failed: {exc}") from exc

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # MCP servers occasionally emit non-JSON banner lines. Ignore.
                continue
            rid = msg.get("id")
            if rid is None:
                # Notifications — ignored in this minimal client.
                continue
            with self._pending_lock:
                entry = self._pending.get(rid)
            if not entry:
                continue
            if "error" in msg:
                entry["error"] = msg["error"]
            else:
                entry["result"] = msg.get("result") or {}
            entry["event"].set()

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.rstrip()
            if not line:
                continue
            # Keep a small tail for diagnostics; MCP servers can be chatty.
            self._stderr_lines.append(line)
            if len(self._stderr_lines) > 200:
                self._stderr_lines = self._stderr_lines[-200:]

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines[-50:])


@contextlib.contextmanager
def playwright_mcp_client(package: str = _MCP_PACKAGE) -> Iterator[Optional[PlaywrightMCPClient]]:
    """
    Context manager. Yields a running client, or `None` if MCP is unavailable
    (missing npx, spawn failure, initialize timeout). The caller must handle
    the `None` case — the whole feature is optional.
    """
    client: Optional[PlaywrightMCPClient] = None
    try:
        client = PlaywrightMCPClient(package=package)
        client.start()
        yield client
    except MCPUnavailable as exc:
        logger.warning(
            "Playwright MCP unavailable — proceeding without live DOM: %s",
            exc,
        )
        if client is not None:
            logger.debug("MCP stderr tail:\n%s", client.stderr_tail())
        yield None
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
