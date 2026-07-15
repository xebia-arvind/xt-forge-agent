// tests/baseTest.ts

import axios from "axios";
import { test as base, expect } from "@playwright/test";
import { sendToDjango } from "./sendToDjango";
import { getAccessToken } from "./auth";
import {
    clearFailureContext,
    getFailureContext,
    parseFailureFromError
} from "./failureContext";

export const test = base;
export { expect };

// Phase 1.5: at startup, ask the backend whether the entry URL's baseline still
// matches the current snapshot. Severe drift is logged as a soft warning so the
// run continues; flip UI_CHANGE_PREFLIGHT_BLOCK=true to fail-fast instead.
const UI_CHANGE_BASE = process.env.HEALER_API_BASE_URL || "http://127.0.0.1:8000/api";
const UI_CHANGE_ROOT = UI_CHANGE_BASE.replace(/\/api\/?$/, "");
const UI_CHANGE_PREFLIGHT_DISABLED = (process.env.UI_CHANGE_PREFLIGHT ?? "true").toLowerCase() === "false";
const UI_CHANGE_PREFLIGHT_BLOCK = (process.env.UI_CHANGE_PREFLIGHT_BLOCK ?? "false").toLowerCase() === "true";

test.beforeAll(async () => {
    if (UI_CHANGE_PREFLIGHT_DISABLED) return;
    const seed = process.env.BASE_URL;
    if (!seed) return;
    try {
        const token = await getAccessToken();
        const url = `${UI_CHANGE_ROOT}/ui-knowledge/change-status/?route=${encodeURIComponent(seed)}`;
        const resp = await axios.get(url, {
            headers: { Authorization: `Bearer ${token}` },
            timeout: 5000,
            validateStatus: () => true,
        });
        const level = String(resp.data?.detection?.ui_change_level || "").toUpperCase();
        if (level === "MAJOR_CHANGE" || level === "ELEMENT_REMOVED") {
            const msg = `⚠ UI baseline drift detected at ${seed}: ui_change_level=${level}. Run 'npm run sync:ui' to refresh.`;
            if (UI_CHANGE_PREFLIGHT_BLOCK) {
                throw new Error(msg);
            }
            console.warn(msg);
        }
    } catch (err) {
        // Soft failure — pre-flight should never block a run by default.
        if (UI_CHANGE_PREFLIGHT_BLOCK) throw err;
        console.warn("UI-change pre-flight skipped:", (err as Error).message);
    }
});

const generatedRunId = `RUN_${new Date()
    .toISOString()
    .replace(/[-:.TZ]/g, "")
    .slice(0, 14)}_${Math.random().toString(36).slice(2, 8)}`;

const generatedBuildId = `BUILD_${new Date()
    .toISOString()
    .replace(/[-:.TZ]/g, "")
    .slice(0, 14)}`;

const commitSha =
    process.env.GITHUB_SHA?.trim().slice(0, 8) ||
    process.env.CI_COMMIT_SHA?.trim().slice(0, 8) ||
    process.env.BUILD_COMMIT?.trim().slice(0, 8);

const RUN_ID = process.env.RUN_ID?.trim() || generatedRunId;
const BUILD_ID = process.env.BUILD_ID?.trim() ||
    (commitSha ? `BUILD_${commitSha}` : generatedBuildId);
const SAVE_ONLY_FAILED = (process.env.SAVE_ONLY_FAILED ?? "false").toLowerCase() === "true";

test.afterEach(async ({ page }, testInfo) => {

    const failed = testInfo.status !== testInfo.expectedStatus;
    if (SAVE_ONLY_FAILED && !failed) return;

    const html = page.isClosed() ? "" : await page.content();
    const screenshot = testInfo.attachments.find(a =>
        a.name.includes("screenshot")
    )?.path;

    const video = testInfo.attachments.find(a =>
        a.name.includes("video")
    )?.path;

    const trace = testInfo.attachments.find(a =>
        a.name.includes("trace")
    )?.path;

    const trackedFailure = getFailureContext(testInfo) || {};
    const parsedFailure = parseFailureFromError(testInfo.error?.message);
    const mergedFailure = {
        failedSelector: trackedFailure.failedSelector || parsedFailure.failedSelector || "",
        failureReason: trackedFailure.failureReason || parsedFailure.failureReason || "unknown",
        pageUrl: trackedFailure.pageUrl || (page.isClosed() ? "" : page.url()),
        healingAttempted: trackedFailure.healingAttempted ?? false,
        healingOutcome: trackedFailure.healingOutcome || "NOT_ATTEMPTED",
        healedSelector: trackedFailure.healedSelector || "",
        healingConfidence: trackedFailure.healingConfidence ?? null,
        validationStatus: trackedFailure.validationStatus || "",
        uiChangeLevel: trackedFailure.uiChangeLevel || "",
        historyAssisted: trackedFailure.historyAssisted ?? false,
        historyHits: trackedFailure.historyHits ?? 0,
        cacheHit: trackedFailure.cacheHit ?? false,
        cacheFallbackToFresh: trackedFailure.cacheFallbackToFresh ?? false,
        rootCause: trackedFailure.rootCause || parsedFailure.failureReason || "unknown",
        stepEvents: trackedFailure.stepEvents || [],
    };

    const payload = {
        run_id: RUN_ID,
        environment: "staging",
        build_id: BUILD_ID,
        run_execution_time: testInfo.duration,

        test_name: testInfo.title,
        status: failed ? "FAILED" : "PASSED",

        error_message: testInfo.error?.message,
        stack_trace: testInfo.error?.stack,

        page_url: mergedFailure.pageUrl,
        failed_selector: mergedFailure.failedSelector,
        failure_reason: mergedFailure.failureReason,
        healing_attempted: mergedFailure.healingAttempted,
        healing_outcome: mergedFailure.healingOutcome,
        healed_selector: mergedFailure.healedSelector,
        healing_confidence: mergedFailure.healingConfidence,
        validation_status: mergedFailure.validationStatus,
        ui_change_level: mergedFailure.uiChangeLevel,
        history_assisted: mergedFailure.historyAssisted,
        history_hits: mergedFailure.historyHits,
        cache_hit: mergedFailure.cacheHit,
        cache_fallback_to_fresh: mergedFailure.cacheFallbackToFresh,
        root_cause: mergedFailure.rootCause,
        step_events: mergedFailure.stepEvents,

        html: html,

        screenshot_path: screenshot,
        video_path: video,
        trace_path: trace,
    };

    try {
        await sendToDjango(payload);
    } finally {
        clearFailureContext(testInfo);
    }
});
