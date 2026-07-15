from django.db import models
import hashlib
import uuid
from django.utils.html import format_html
from django.utils.text import slugify
from PIL import Image
import os
from abstract.models import Common
from django.conf import settings
# Create your models here.

class MD5Field(models.CharField):
    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 32
        super().__init__(*args, **kwargs)

    def pre_save(self, model_instance, add):
        value = getattr(model_instance, self.attname)
        if value:
            md5_hash = hashlib.md5(value.encode()).hexdigest()
            setattr(model_instance, self.attname, md5_hash)
            return md5_hash
        return super().pre_save(model_instance, add)

class Clients(Common):
    secret_key = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clientname = models.CharField(max_length=255)
    # slug is used as the directory name for per-client materialization
    # (tests/generated/<slug>/, tests/pages/generated/<slug>/) and may appear in URLs.
    slug = models.SlugField(max_length=64, unique=True, blank=True)
    client_logo = models.ImageField(upload_to='uploads/clientlogo/', null= True)
    # Per-tenant LLM overrides consumed by `test_generation.llm_backends.pick_backend`.
    # Shape: {"models": {"artifact_generator": "gpt-4o", ...}, "openai_api_key": "…"}
    # Empty = use env vars / defaults.
    llm_config = models.JSONField(default=dict, blank=True)


    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.clientname or "") or "client"
            candidate = base[:64]
            i = 2
            while Clients.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                suffix = f"-{i}"
                candidate = (base[: 64 - len(suffix)] + suffix)
                i += 1
            self.slug = candidate
        super().save(*args, **kwargs)


    def __str__(self):

        return self.clientname

    def logo_preview(self):
        if self.client_logo:
            return format_html('<img src="{}" width="100" height="50" />', self.client_logo.url)
        return 'No Logo'


    class Meta:
        verbose_name_plural = "Manage Clients"

class UserClient(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_client"
    )
    clients = models.ManyToManyField(
        Clients,
        blank=True,
        related_name="users"
    )

    def __str__(self):
        return self.user.username

