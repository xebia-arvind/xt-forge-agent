"""
Seed one end-to-end record set so the Phase 1 + Phase 2 changes are easy to inspect
in the Django admin and via the API.

Run:
    python manage.py seed_e2e_entry
    python manage.py seed_e2e_entry --client demo-tenant
    python manage.py seed_e2e_entry --client demo-tenant --reset

What gets created (all linked, all stamped with the same `Clients` row):

    Clients(slug=<arg>)                                                    +─┐
        ├── UserClient                                                       │
        │       owner: e2e_demo_user (password "e2e-demo")                   │
        ├── UIPage(/login)                                                   │
        │       ├── UIRouteSnapshot(BASELINE, is_current=True)               │
        │       │       └── UIElement(× 2)                                   │
        │       │       └── UIScreenshot                                     │
        │       └── UIRouteSnapshot(NEW_STRUCTURE, is_current=False)         │
        │               └── UIElement(× 2, one removed selector)             │
        ├── UIChangeLog(MINOR, baseline vs new)                              │
        ├── HealerRequest + SuggestedSelector(× 3)  ← Phase 2 shape          │
        ├── GenerationJob(DRAFT_READY)                                       │
        │       ├── GenerationScenario(SMOKE)                                │
        │       └── GeneratedArtifact(× 2)                                   │
        ├── TestRun(client=…, run_id=E2E_<slug>_<ts>)                        │
        │       └── TestCaseResult(FAILED, healing_outcome=SUCCESS)          │
        └── GenerationExecutionLink(job ↔ test_run)                          │
                                                                             │
JWT login payload to exercise the API end-to-end:                            │
    POST /auth/login/                                                        │
    {                                                                        │
       "email": "e2e@example.test",                                          │
       "password": "e2e-demo",                                                │
       "client_secret": "<the client's secret_key from above>"               │
    }                                                                        │
"""
from __future__ import annotations

import hashlib
import json
import uuid

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Seed one fully-linked end-to-end record set for Phase 1/2 verification."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            default="e2e-demo",
            help="Client slug to create or reuse (default: e2e-demo).",
        )
        parser.add_argument(
            "--client-name",
            default="E2E Demo",
            help="Human-readable client name (default: 'E2E Demo').",
        )
        parser.add_argument(
            "--email",
            default="e2e@example.test",
            help="User email for login (default: e2e@example.test).",
        )
        parser.add_argument(
            "--password",
            default="e2e-demo",
            help="Plaintext password for the demo user (default: e2e-demo).",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Soft-delete every prior row that we created for this client before re-seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        from clients.models import Clients, UserClient
        from curertestai.models import HealerRequest, SuggestedSelector
        from test_analytics.models import TestRun, TestCaseResult
        from test_generation.models import (
            GeneratedArtifact,
            GenerationExecutionLink,
            GenerationJob,
            GenerationScenario,
        )
        from ui_knowledge.models import (
            UIChangeLog,
            UIElement,
            UIPage,
            UIRouteSnapshot,
            UIScreenshot,
        )

        slug = opts["client"]
        client_name = opts["client_name"]
        email = opts["email"]
        password = opts["password"]

        # 1) Client + user
        client, created_client = Clients.objects.get_or_create(
            slug=slug,
            defaults={"clientname": client_name},
        )
        if created_client:
            self.stdout.write(self.style.SUCCESS(f"+ Created Clients(slug={slug})"))
        else:
            self.stdout.write(f"  Reusing Clients(slug={slug})")

        user, created_user = User.objects.get_or_create(
            email=email,
            defaults={
                "username": email,
                "password": make_password(password),
            },
        )
        if created_user:
            self.stdout.write(self.style.SUCCESS(f"+ Created User(email={email}) (password: {password!r})"))
        else:
            # Reset the password so the command stays deterministic.
            user.password = make_password(password)
            user.save(update_fields=["password"])
            self.stdout.write(f"  Reusing User(email={email}); password reset to {password!r}")

        user_client, _ = UserClient.objects.get_or_create(user=user)
        user_client.clients.add(client)

        # 2) Optional reset of prior e2e rows
        if opts["reset"]:
            self.stdout.write(self.style.WARNING("  --reset: removing prior e2e rows for this client"))
            HealerRequest.objects.filter(client_id=client).delete()
            UIPage.objects.filter(client=client).delete()  # cascades to snapshots/elements/changes
            TestRun.objects.filter(client=client).delete()  # cascades to TestCaseResult
            GenerationJob.objects.filter(client=client).delete()  # cascades to scenarios/artifacts/links

        # 3) UI knowledge — baseline + drifted snapshot + change log
        page, _ = UIPage.objects.get_or_create(
            client=client,
            route="/login",
            defaults={"title": "Login", "feature_name": "Auth"},
        )
        # Mark older snapshots non-current so the new ones can take over cleanly.
        page.snapshots.update(is_current=False)

        baseline_payload = {
            "url": "/login",
            "elements_summary": "2 inputs + 1 button (baseline)",
        }
        baseline = UIRouteSnapshot.objects.create(
            page=page,
            snapshot_type="BASELINE",
            is_current=True,
            dom_hash=hashlib.sha256(b"baseline").hexdigest(),
            snapshot_json=baseline_payload,
            version=page.snapshots.count() + 1,
        )
        UIElement.objects.bulk_create(
            [
                UIElement(snapshot=baseline, selector="#username", tag="input", role="textbox", text="", test_id="", intent_key="login_username"),
                UIElement(snapshot=baseline, selector="#loginButton", tag="button", role="button", text="Sign In", test_id="", intent_key="homepage_signin_cta"),
            ]
        )
        UIScreenshot.objects.create(
            snapshot=baseline,
            image_path=f"test-results/seed/{slug}/login_baseline.png",
            viewport="1920x1080",
            device="Chrome",
        )

        new_struct_payload = {
            "url": "/login",
            "elements_summary": "username renamed, login button moved",
        }
        new_struct = UIRouteSnapshot.objects.create(
            page=page,
            snapshot_type="NEW_STRUCTURE",
            is_current=False,
            dom_hash=hashlib.sha256(b"new_struct").hexdigest(),
            snapshot_json=new_struct_payload,
            version=page.snapshots.count() + 1,
        )
        UIElement.objects.bulk_create(
            [
                UIElement(snapshot=new_struct, selector="input[name='email']", tag="input", role="textbox", text="", test_id="", intent_key="login_username"),
                UIElement(snapshot=new_struct, selector="button[aria-label='Sign In']", tag="button", role="button", text="Sign In", test_id="", intent_key="homepage_signin_cta"),
            ]
        )

        UIChangeLog.objects.create(
            page=page,
            baseline_snapshot=baseline,
            new_snapshot=new_struct,
            change_type="MINOR",
            added_selectors=["input[name='email']", "button[aria-label='Sign In']"],
            removed_selectors=["#username", "#loginButton"],
            auto_promoted=False,
        )

        self.stdout.write(self.style.SUCCESS("+ Created UIPage / 2 snapshots / 4 elements / 1 change log"))

        # 4) HealerRequest + suggested selectors (Phase 2 shape — no batch, no dom snapshot)
        heal = HealerRequest.objects.create(
            user_id=user,
            client_id=client,
            batch_id=0,  # IntegerField after Phase 2
            failed_selector="#loginButton",
            html="<html>...</html>",
            use_of_selector="click sign in",
            selector_type="css",
            url="https://www.carnival.com/login",
            healed_selector="button[aria-label='Sign In']",
            confidence=0.82,
            success=True,
            processing_time_ms=412,
            llm_used=True,
            screenshot_analyzed=False,
            intent_key="homepage_signin_cta",
            validation_status="VALID",
            validation_reason="Selector passed rule/history/LLM validation",
            dom_fingerprint=hashlib.sha256(b"fp_login").hexdigest(),
            candidate_snapshot=[
                {"selector": "button[aria-label='Sign In']", "score": 0.82, "tag": "button"},
                {"selector": "button.signin-cta", "score": 0.74, "tag": "button"},
                {"selector": "form button:has-text('Sign In')", "score": 0.68, "tag": "button"},
            ],
            history_assisted=True,
            history_hits=3,
            ui_change_level="MINOR_CHANGE",
        )
        for sel, score, base, attr in [
            ("button[aria-label='Sign In']", 0.82, 0.71, 0.30),
            ("button.signin-cta", 0.74, 0.66, 0.18),
            ("form button:has-text('Sign In')", 0.68, 0.62, 0.12),
        ]:
            SuggestedSelector.objects.create(
                healer_request=heal,
                selector=sel,
                score=score,
                base_score=base,
                attribute_score=attr,
                tag="button",
                text="Sign In",
                xpath=f"//button[normalize-space()='Sign In']",
            )
        self.stdout.write(self.style.SUCCESS(f"+ Created HealerRequest(id={heal.id}) + 3 SuggestedSelector"))

        # 5) GenerationJob (DRAFT_READY) + scenario + 2 artifacts
        job = GenerationJob.objects.create(
            client=client,
            feature_name="E2E demo — Login flow",
            feature_description="Seeded record set for Phase 1/2 verification.",
            seed_urls=["/", "/login"],
            intent_hints=["Accept consent", "Click Sign In"],
            coverage_mode=GenerationJob.COVERAGE_SMOKE_NEGATIVE,
            max_scenarios=2,
            max_routes=5,
            base_url="https://www.carnival.com",
            job_status=GenerationJob.STATE_DRAFT_READY,
            llm_model="qwen2.5-coder:7b",
            llm_temperature=0.0,
            feature_summary="Login flow including consent banner and credentials.",
            llm_notes=["Seeded by seed_e2e_entry."],
            validation_summary={"total_artifacts": 2, "valid_artifacts": 2, "invalid_artifacts": 0},
            created_by="seed_e2e_entry",
            drafting_started_on=timezone.now(),
            drafting_finished_on=timezone.now(),
        )
        GenerationScenario.objects.create(
            job=job,
            scenario_id="login-smoke-1",
            title="Valid user logs in",
            scenario_type=GenerationScenario.TYPE_SMOKE,
            priority=1,
            preconditions=["Browser at /login"],
            steps=[
                {"action": "fill email", "selector": "#username", "value": "demo@example.test"},
                {"action": "fill password", "selector": "#password", "value": "Test@123"},
                {"action": "click sign in", "selector": "#loginButton", "intent_key": "homepage_signin_cta"},
            ],
            expected_assertions=["greeting visible"],
            selected_for_materialization=True,
        )
        po_path = f"tests/pages/generated/LoginPage.ts"
        spec_path = f"tests/generated/login-smoke-1.spec.ts"
        po_content = (
            "// page object (demo)\n"
            "import { Page } from '@playwright/test';\n"
            "export class LoginPage { constructor(public page: Page) {} }\n"
        )
        spec_content = (
            "import { test, expect } from '../../wraper-healer/baseTest';\n"
            "import { selfHealingClick } from '../../wraper-healer/selfHealing';\n"
            "test('valid login', async ({ page }) => {\n"
            "  await page.goto('/login');\n"
            "  // intent_key kept so spec validator passes\n"
            "  expect(true).toBeTruthy();\n"
            "});\n"
        )
        GeneratedArtifact.objects.create(
            job=job,
            artifact_type=GeneratedArtifact.TYPE_PAGE_OBJECT,
            relative_path=po_path,
            content_draft=po_content,
            content_final=po_content,
            checksum=hashlib.sha256(po_content.encode()).hexdigest(),
            validation_status=GeneratedArtifact.VALID,
        )
        GeneratedArtifact.objects.create(
            job=job,
            artifact_type=GeneratedArtifact.TYPE_SPEC,
            relative_path=spec_path,
            content_draft=spec_content,
            content_final=spec_content,
            checksum=hashlib.sha256(spec_content.encode()).hexdigest(),
            validation_status=GeneratedArtifact.VALID,
        )
        self.stdout.write(self.style.SUCCESS(f"+ Created GenerationJob({job.job_id}) + 1 scenario + 2 artifacts"))

        # 6) TestRun + TestCaseResult (Phase 1: client-stamped, per-client run_id)
        run_id = f"E2E_{slug}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
        test_run, _ = TestRun.objects.get_or_create(
            client=client,
            run_id=run_id,
            defaults={
                "environment": "staging",
                "build_id": "BUILD_e2e_demo",
                "execution_time": 4.7,
            },
        )
        TestCaseResult.objects.create(
            client=client,
            test_run=test_run,
            test_name="E2E demo — login flow",
            status="FAILED",
            html="<html>...</html>",
            page_url="https://www.carnival.com/login",
            failed_selector="#loginButton",
            error_message="Locator timed out after 5000ms",
            failure_reason="Timeout",
            stack_trace="…",
            execution_time=2.1,
            failure_category="HEALED",
            healing_attempted=True,
            healing_outcome="SUCCESS",
            healed_selector="button[aria-label='Sign In']",
            healing_confidence=0.82,
            validation_status="VALID",
            ui_change_level="MINOR_CHANGE",
            history_assisted=True,
            history_hits=3,
            cache_hit=False,
            cache_fallback_to_fresh=False,
            root_cause="Original locator failed; selfHealingClick recovered.",
            step_events=[
                {"step_name": "click sign in", "step_type": "action", "status": "HEALED", "timestamp": timezone.now().isoformat()},
            ],
        )
        GenerationExecutionLink.objects.get_or_create(
            job=job,
            test_run=test_run,
            defaults={"notes": "Seeded by seed_e2e_entry."},
        )
        self.stdout.write(self.style.SUCCESS(f"+ Created TestRun({run_id}) + 1 TestCaseResult + link to job"))

        # 7) Summary
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== Seed complete ==="))
        self.stdout.write(json.dumps(
            {
                "client_slug": client.slug,
                "client_secret": str(client.secret_key),
                "user_email": email,
                "user_password": password,
                "job_id": str(job.job_id),
                "run_id": run_id,
                "healer_request_id": heal.id,
                "ui_page_route": page.route,
            },
            indent=2,
        ))
        self.stdout.write("")
        self.stdout.write("Try in Django admin:")
        self.stdout.write(f"  /admin/clients/clients/{client.secret_key}/change/")
        self.stdout.write(f"  /admin/test_generation/generationjob/{job.id}/change/")
        self.stdout.write(f"  /admin/test_analytics/testrun/{test_run.id}/change/")
        self.stdout.write(f"  /admin/curertestai/healerrequest/{heal.id}/change/")
        self.stdout.write(f"  /admin/ui_knowledge/uipage/{page.id}/change/")
        self.stdout.write("")
        self.stdout.write("Try via the API (JWT-required after Phase 1):")
        self.stdout.write("  curl -X POST http://127.0.0.1:8000/auth/login/ \\")
        self.stdout.write(f"       -H 'Content-Type: application/json' \\")
        self.stdout.write(
            f"       -d '{{\"email\":\"{email}\",\"password\":\"{password}\","
            f"\"client_secret\":\"{client.secret_key}\"}}'"
        )
