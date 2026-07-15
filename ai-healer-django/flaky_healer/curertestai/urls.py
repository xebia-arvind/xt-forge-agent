from django.urls import path
from .views import HealAPIView, BatchHealAPIView

urlpatterns = [
    path('heal/', HealAPIView.as_view(), name='heal'),
    path('heal/batch/', BatchHealAPIView.as_view(), name='batch_heal'),
]
