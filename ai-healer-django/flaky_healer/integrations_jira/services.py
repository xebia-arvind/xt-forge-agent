"""
Thin Jira REST v3 client that consumes a `JiraConnection` and hides HTTP details
from the view layer.

Every method returns a Python dict already parsed from the Jira response; raises
`JiraError` on non-2xx so the view can convert it into a JSON error consistently.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from requests.auth import HTTPBasicAuth

from .models import JiraConnection


class JiraError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class JiraClient:
    def __init__(self, connection: JiraConnection):
        self.conn = connection
        self.auth = HTTPBasicAuth(*connection.auth_tuple())
        self.base = connection.base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base}{path if path.startswith('/') else '/' + path}"

    def _handle(self, resp: requests.Response) -> Dict[str, Any]:
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text[:400]}
            raise JiraError(
                f"Jira {resp.request.method} {resp.request.path_url} → {resp.status_code}: {detail}",
                status_code=resp.status_code,
            )
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return resp.json()
        return {"raw": resp.text}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def search(self, jql: str, max_results: int = 20) -> Dict[str, Any]:
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "status", "assignee", "priority", "created", "updated", "description"],
        }
        resp = requests.post(self._url("/rest/api/3/search/jql"), json=payload, auth=self.auth, timeout=15)
        return self._handle(resp)

    def issue(self, issue_key: str) -> Dict[str, Any]:
        resp = requests.get(
            self._url(f"/rest/api/3/issue/{issue_key}"),
            params={"fields": "summary,status,description,assignee"},
            auth=self.auth,
            timeout=15,
        )
        return self._handle(resp)

    def add_comment(self, issue_key: str, body_adf: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"body": body_adf}
        resp = requests.post(
            self._url(f"/rest/api/3/issue/{issue_key}/comment"),
            json=payload,
            auth=self.auth,
            timeout=15,
        )
        return self._handle(resp)

    def attach_file(self, issue_key: str, filename: str, content: bytes) -> Dict[str, Any]:
        resp = requests.post(
            self._url(f"/rest/api/3/issue/{issue_key}/attachments"),
            files={"file": (filename, content)},
            headers={"X-Atlassian-Token": "no-check"},
            auth=self.auth,
            timeout=30,
        )
        return self._handle(resp)


def build_adf_paragraph(text: str) -> Dict[str, Any]:
    """Minimal Atlassian Document Format builder for a single paragraph."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": str(text)}],
            }
        ],
    }
