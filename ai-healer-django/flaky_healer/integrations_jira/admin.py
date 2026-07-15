"""
Admin surface for JiraConnection.

Design goals:
- Never render `api_token_encrypted` (ciphertext) as user-visible text.
- Give the admin a plain password input to *set* a new token; a blank submission
  leaves the existing ciphertext untouched.
- Show whether a token is currently stored (yes/no) without revealing the value.
"""
from django import forms
from django.contrib import admin

from .models import JiraConnection


class JiraConnectionAdminForm(forms.ModelForm):
    api_token = forms.CharField(
        label="API token",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Paste a new Jira API token. Leave blank to keep the current value.",
    )

    class Meta:
        model = JiraConnection
        # api_token_encrypted deliberately excluded — ciphertext is not editable
        # or visible through this form.
        fields = ("client", "base_url", "email", "display_name", "api_token")

    def save(self, commit=True):
        instance: JiraConnection = super().save(commit=False)
        plaintext = self.cleaned_data.get("api_token") or ""
        if plaintext:
            instance.set_api_token(plaintext)
        if commit:
            instance.save()
        return instance


@admin.register(JiraConnection)
class JiraConnectionAdmin(admin.ModelAdmin):
    form = JiraConnectionAdminForm
    list_display = ("client", "base_url", "email", "display_name", "token_status", "last_modified")
    search_fields = ("client__clientname", "client__slug", "email", "base_url")
    readonly_fields = ("token_status", "created_on", "last_modified")

    def token_status(self, obj):
        if obj and obj.api_token_encrypted:
            return "✔ stored"
        return "— not set —"
    token_status.short_description = "Token stored?"
