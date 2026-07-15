from django.test import SimpleTestCase

from .serializers import (
    GenerationJobCreateSerializer,
    GenerationJobApproveSerializer,
    GenerationJobMaterializeSerializer,
)
from .generation_service import _validate_relative_path, _validate_artifact_content


class GenerationSerializerTests(SimpleTestCase):
    def test_create_serializer_valid(self):
        payload = {
            "feature_name": "Wishlist",
            "feature_description": "Generate tests for wishlist flow",
            "seed_urls": ["/", "/product/1"],
            "coverage_mode": "SMOKE_NEGATIVE",
        }
        serializer = GenerationJobCreateSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_approve_serializer_valid(self):
        payload = {
            "approved_by": "demo-user",
            "notes": "Looks good",
            "include_scenario_ids": ["smoke_1"],
        }
        serializer = GenerationJobApproveSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_materialize_serializer_defaults(self):
        serializer = GenerationJobMaterializeSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["allow_overwrite"], False)


class GenerationGuardrailTests(SimpleTestCase):
    def test_relative_path_restrictions(self):
        self.assertEqual(_validate_relative_path("tests/generated/a.spec.ts"), [])
        self.assertTrue(_validate_relative_path("../escape.ts"))
        self.assertTrue(_validate_relative_path("/tmp/hack.ts"))

    def test_forbidden_patterns(self):
        content = "test.only('x', async () => { await page.waitForTimeout(1000); });"
        errors, warnings = _validate_artifact_content("SPEC", content)
        self.assertGreaterEqual(len(errors), 1)
        self.assertEqual(warnings, [])
