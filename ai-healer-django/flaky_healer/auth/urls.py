from auth.views import LoginAPIView
from rest_framework.routers import DefaultRouter
from django.urls import path

urlpatterns = [
    path("login/", LoginAPIView.as_view(), name="login"),
]

