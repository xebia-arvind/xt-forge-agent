#!/usr/bin/env node

import { spawn } from "child_process";
import path from "path";
import axios from "axios";

// =====================================================
// CONFIG
// =====================================================

const BASE_URL = process.env.BASE_URL || "http://localhost:3000";
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const MAX_ROUTES = Number(process.env.MAX_ROUTES || 20);
const MAX_DEPTH = Number(process.env.MAX_DEPTH || 2);
const MAX_INTERACTABLES = Number(process.env.MAX_INTERACTABLES || 200);
const FEATURE_NAME = process.env.FEATURE_NAME || "";
const SNAPSHOT_TYPE = process.env.SNAPSHOT_TYPE || "BASELINE";
const SCREENSHOT_DIR =
    process.env.UI_SCREENSHOT_DIR ||
    path.join(repoRoot(), "test-results", "ui-crawl-screenshots");
const INTENT_LLM_ENABLED = (process.env.INTENT_LLM_ENABLED || "true").toLowerCase() === "true";
const INTENT_LLM_URL = process.env.INTENT_LLM_URL || "http://127.0.0.1:11434/api/generate";
const INTENT_LLM_MODEL = process.env.INTENT_LLM_MODEL || "qwen2.5:7b";

// AUTO DISCOVERY ENTRY POINT
const SEED_URLS = process.env.SEED_URLS
    ? JSON.parse(process.env.SEED_URLS)
    : ["/"];

// =====================================================
// JWT AUTH (mirrors wraper-healer/auth.ts — /ui-knowledge/sync/ is behind JWT)
// =====================================================

const AUTH_CREDS = {
    email: process.env.XT_DJANGO_EMAIL || "arvind.kumar1@xebia.com",
    password: process.env.XT_DJANGO_PASSWORD || "admin",
    client_secret:
        process.env.XT_CLIENT_SECRET || "6b66a9e9-a970-4a0b-b2e6-73f1eb13497e",
};

let cachedToken = null;

async function loginForToken(backendUrl) {
    const loginUrl = `${backendUrl.replace(/\/$/, "")}/auth/login/`;
    console.log(`🔐 Authenticating against ${loginUrl}...`);
    const resp = await axios.post(loginUrl, AUTH_CREDS, {
        headers: { "Content-Type": "application/json" },
        timeout: 15000,
    });
    const access = resp?.data?.tokens?.access;
    if (!access) throw new Error("Django login returned no access token");
    cachedToken = access;
    console.log("✅ Authenticated. Token cached for this run.");
    return access;
}

async function getAccessToken(backendUrl) {
    if (cachedToken) return cachedToken;
    return loginForToken(backendUrl);
}

function clearCachedToken() {
    cachedToken = null;
}

async function authedRequest(backendUrl, config) {
    const send = async () => {
        const token = await getAccessToken(backendUrl);
        return axios({
            ...config,
            headers: {
                ...(config.headers || {}),
                Authorization: `Bearer ${token}`,
            },
        });
    };
    try {
        return await send();
    } catch (err) {
        if (err?.response?.status === 401) {
            console.log("⚠️ Received 401 — refreshing token and retrying once...");
            clearCachedToken();
            return await send();
        }
        throw err;
    }
}

// =====================================================
// HELPERS
// =====================================================

function repoRoot() {
    return process.cwd();
}

function crawlScriptPath() {
    return path.join(repoRoot(), "tests", "utils", "crawlContext.mjs");
}

function safeJsonParse(raw, fallback) {
    try {
        return JSON.parse(raw);
    } catch {
        return fallback;
    }
}

function normalizeIntentKey(value) {
    const cleaned = String(value || "")
        .toLowerCase()
        .trim()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "");
    return cleaned || "generic";
}

function normalizeFeatureName(value) {
    const cleaned = String(value || "").trim().replace(/\s+/g, " ");
    return cleaned;
}

function extractJsonObject(text) {
    const raw = String(text || "").trim();
    if (!raw) return "";
    const start = raw.indexOf("{");
    if (start < 0) return "";
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = start; i < raw.length; i += 1) {
        const ch = raw[i];
        if (inString) {
            if (escaped) {
                escaped = false;
            } else if (ch === "\\") {
                escaped = true;
            } else if (ch === "\"") {
                inString = false;
            }
            continue;
        }
        if (ch === "\"") {
            inString = true;
            continue;
        }
        if (ch === "{") depth += 1;
        if (ch === "}") {
            depth -= 1;
            if (depth === 0) return raw.slice(start, i + 1);
        }
    }
    return "";
}

async function classifyRouteSemanticsWithLLM(route, interactables) {
    if (!INTENT_LLM_ENABLED) {
        return {
            featureName: normalizeFeatureName(FEATURE_NAME),
            intents: new Array(interactables.length).fill("generic"),
        };
    }

    const compact = interactables.map((el, idx) => ({
        idx,
        selector: String((el.selector_hints && el.selector_hints[0]) || ""),
        text: String(el.text || ""),
        tag: String(el.tag || ""),
        role: String(el.role || ""),
        test_id: String(el.test_id || ""),
        aria_label: String(el.aria_label || ""),
        href: String(el.href || ""),
    }));

    const prompt = [
        "You are classifying route feature context and UI element intents for test automation.",
        "Return strict JSON only with shape:",
        "{\"feature_name\":\"Checkout\",\"intents\":[{\"idx\":0,\"intent_key\":\"add_to_cart\"}]}",
        "Rules:",
        "- feature_name must be short human-readable phrase (2-5 words), specific to page/flow",
        "- intent_key must be lowercase snake_case",
        "- infer specific user action intent (add_to_cart, checkout, payment, wishlist, search, navigation, submit_form, etc.)",
        "- if unclear use generic",
        "- if route has no clear feature context, use generic as feature_name",
        "",
        `route: ${route.url || "/"}`,
        `title: ${route.title || ""}`,
        `elements: ${JSON.stringify(compact)}`,
    ].join("\n");

    try {
        const res = await axios.post(
            INTENT_LLM_URL,
            {
                model: INTENT_LLM_MODEL,
                prompt,
                stream: false,
                format: "json",
                options: {
                    temperature: 0,
                    num_predict: 1200,
                },
            },
            { timeout: 60000 }
        );
        //console.log("Intent LLM response:", res.data);
        const envelope = res?.data || {};
        let payload = {};
        if (typeof envelope.response === "string") {
            payload = safeJsonParse(envelope.response, {});
            if (!Object.keys(payload).length) {
                payload = safeJsonParse(extractJsonObject(envelope.response), {});
            }
        } else if (typeof envelope.response === "object" && envelope.response) {
            payload = envelope.response;
        } else if (typeof envelope === "object" && envelope) {
            payload = envelope;
        }

        const byIdx = new Map();
        for (const row of payload.intents || []) {
            const idx = Number(row?.idx);
            if (!Number.isInteger(idx)) continue;
            byIdx.set(idx, normalizeIntentKey(row?.intent_key));
        }
        const inferredFeatureName = normalizeFeatureName(payload.feature_name);
        return {
            featureName: inferredFeatureName || normalizeFeatureName(FEATURE_NAME),
            intents: compact.map((_, idx) => byIdx.get(idx) || "generic"),
        };
    } catch (err) {
        console.warn(`⚠ Intent LLM fallback (route=${route.url || "/"}):`, err?.message || err);
        return {
            featureName: normalizeFeatureName(FEATURE_NAME),
            intents: new Array(interactables.length).fill("generic"),
        };
    }
}

// =====================================================
// RUN CRAWLER
// =====================================================

async function runCrawler() {
    return new Promise((resolve, reject) => {
        const script = crawlScriptPath();

        const args = [
            script,
            "--base-url",
            BASE_URL,
            "--seed-urls",
            JSON.stringify(SEED_URLS),
            "--max-routes",
            String(MAX_ROUTES),
            "--max-depth",
            String(MAX_DEPTH),
            "--max-interactables",
            String(MAX_INTERACTABLES),
            "--screenshot-dir",
            SCREENSHOT_DIR,
        ];

        console.log("🚀 Running UI crawler...");
        console.log("Seeds:", SEED_URLS);

        const proc = spawn("node", args, {
            cwd: repoRoot(),
            stdio: ["ignore", "pipe", "pipe"],
        });

        let stdout = "";
        let stderr = "";

        proc.stdout.on("data", (d) => (stdout += d.toString()));
        proc.stderr.on("data", (d) => (stderr += d.toString()));

        proc.on("close", (code) => {
            if (code !== 0) {
                return reject(
                    new Error(`Crawler failed rc=${code}\n${stderr.slice(0, 500)}`)
                );
            }

            try {
                function extractJson(raw) {
                    const start = raw.indexOf("{");
                    const end = raw.lastIndexOf("}");

                    if (start === -1 || end === -1) {
                        throw new Error("No JSON found in crawler output");
                    }

                    return raw.slice(start, end + 1);
                }

                const json = extractJson(stdout);
                const parsed = JSON.parse(json);
                resolve(parsed);
            } catch (err) {
                reject(new Error("Crawler returned invalid JSON"));
            }
        });
    });
}

// =====================================================
// SEND TO DJANGO
// =====================================================

async function sendToBackend(data) {
    console.log("📡 Sending UI knowledge → Django...");

    const routes = data?.routes || [];
    let success = 0;
    let failed = 0;
    const errors = [];

    for (const route of routes) {
        const routeInteractables = (route.interactables || []).slice(0, MAX_INTERACTABLES);
        const routeSemantics = await classifyRouteSemanticsWithLLM(route, routeInteractables);
        const inferredFeatureName = routeSemantics.featureName || normalizeFeatureName(FEATURE_NAME);
        const intentByIndex = routeSemantics.intents || [];
        const elements = [];
        for (let i = 0; i < routeInteractables.length; i += 1) {
            const el = routeInteractables[i];
            const selector =
                (el.selector_hints && el.selector_hints[0]) ||
                (el.test_id ? `[data-testid="${el.test_id}"]` : "") ||
                (el.id ? `#${el.id}` : "") ||
                (el.aria_label ? `${el.tag || "button"}[aria-label="${el.aria_label}"]` : "") ||
                (el.text ? `${el.tag || "button"}:has-text("${(el.text || "").slice(0, 40)}")` : "");

            if (!selector) continue; // skip elements without a usable selector

            elements.push({
                selector: String(selector),
                tag: String(el.tag || ""),
                role: String(el.role || ""),
                text: String(el.text || ""),
                test_id: String(el.test_id || ""),
                intent_key: String(intentByIndex[i] || "generic"),
            });
        }

        const payload = {
            route: route.url || "/",
            title: route.title || "",
            feature_name: inferredFeatureName,
            snapshot_type: SNAPSHOT_TYPE,
            dom_hash: route.dom_hash || "",
            screenshot_path: String(route.screenshot_path || ""),
            snapshot_json: route,
            elements,
        };

        try {
            const res = await authedRequest(BACKEND_URL, {
                method: "POST",
                url: `${BACKEND_URL.replace(/\/$/, "")}/ui-knowledge/sync/`,
                data: payload,
                headers: {
                    "Content-Type": "application/json",
                },
                timeout: 30000,
            });
            console.log(`✔ Stored snapshot for ${payload.route} (elements=${elements.length}) status=${res.status}`);
            success += 1;
        } catch (err) {
            failed += 1;
            const msg = err?.response?.data || err?.message || "unknown error";
            errors.push({ route: payload.route, error: msg });
            console.warn(`✖ Failed snapshot ${payload.route}:`, msg);
        }
    }

    console.log(`✅ Sync complete: success=${success}, failed=${failed}`);
    if (errors.length) {
        console.log("Errors:", errors);
    }
}

// =====================================================
// MAIN
// =====================================================

async function main() {
    try {
        console.log("====================================");
        console.log(" AI UI KNOWLEDGE SYNC STARTED ");
        console.log("====================================");

        const crawl = await runCrawler();
        console.log(crawl);
        console.log(`✔ Routes discovered: ${crawl.routes?.length || 0}`);

        if (crawl.warnings?.length) {
            console.warn("⚠ Warnings:");
            crawl.warnings.forEach((w) => console.warn("-", w));
        }

        await sendToBackend(crawl);

        console.log("🎉 UI Knowledge updated successfully");
    } catch (err) {
        console.error("❌ Sync failed:", err.message);
        process.exit(1);
    }
}

main();
