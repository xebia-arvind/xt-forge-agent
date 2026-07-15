"""
Jira credentials, encrypted at rest.

The `api_token_encrypted` field stores a Fernet-encrypted blob of the operator's
Jira API token. Plaintext is never persisted. Getters/setters live on the model
so callers do not touch cryptography directly.

Scoped per `Clients` — every tenant has at most one active Jira connection.
"""
from __future__ import annotations

from django.db import models

from abstract.models import Common
from clients.models import Clients
from .crypto import decrypt, encrypt


class JiraConnection(Common):
    client = models.OneToOneField(
        Clients,
        on_delete=models.CASCADE,
        related_name="jira_connection",
    )
    base_url = models.URLField(help_text="e.g. https://xebiaww.atlassian.net")
    email = models.EmailField()
    # Fernet output is base64 ASCII; TextField keeps it simple across DB engines.
    api_token_encrypted = models.TextField()
    # Optional friendly label shown in the panel.
    display_name = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        verbose_name = "Jira Connection"
        verbose_name_plural = "Jira Connections"

    def __str__(self):
        return f"{self.client.slug} → {self.base_url}"

    # ------------------------------------------------------------------
    # Token access helpers
    # ------------------------------------------------------------------
    def set_api_token(self, plaintext: str) -> None:
        """Encrypt and store the caller-supplied Jira API token."""
        self.api_token_encrypted = encrypt(plaintext or "")

    def get_api_token(self) -> str:
        """Return the decrypted Jira API token."""
        if not self.api_token_encrypted:
            return ""
        return decrypt(self.api_token_encrypted)

    def auth_tuple(self):
        """Return (email, token) suitable for `requests.auth.HTTPBasicAuth`."""
        return (self.email, self.get_api_token())
