"""
URL configuration for flaky_healer project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static


def healthz(_request):
    """
    Liveness probe. Returns 200 without touching the DB, cache, or any
    external service. Render's health checker points here so the deploy
    goes green even before migrations have run — the actual DB-backed
    endpoints (/admin/, /test-analytics/) will 500 until then, but the
    container is confirmed alive.
    """
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz/", healthz),
    path("admin/", admin.site.urls),
    path("auth/", include("auth.urls")),
    path("api/", include("curertestai.urls")),
    path("test-analytics/", include("test_analytics.urls")),
    path("test-generation/", include("test_generation.urls")),
    path("ui-knowledge/", include("ui_knowledge.urls")),
    # Phase 3 — dashboard absorbs Streamlit
    path("integrations/jira/", include("integrations_jira.urls")),
    path("runners/", include("runners.urls")),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )
