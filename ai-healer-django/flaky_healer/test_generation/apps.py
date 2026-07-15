from django.apps import AppConfig


class TestGenerationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'test_generation'
    verbose_name = "Test Generation"
