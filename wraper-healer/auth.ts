import axios from "axios";
import type { TestInfo } from "@playwright/test";
import { appendHealingReportLog } from "./healingReportLogger";

const AUTH_URL = "http://127.0.0.1:8000/auth/login/";

const CREDENTIALS = {
    email: "arvind.kumar1@xebia.com",
    password: "admin",
    client_secret: "6b66a9e9-a970-4a0b-b2e6-73f1eb13497e",
};

// In-memory token store (lives for the duration of the Playwright process)
let cachedToken: string | null = null;

/**
 * Returns a valid access token.
 * Authenticates against the healer backend if no token is cached yet.
 */
export async function getAccessToken(testInfo?: TestInfo): Promise<string> {
    if (cachedToken) {
        return cachedToken;
    }

    console.log("🔐 No token found — authenticating with healer backend...");
    appendHealingReportLog(testInfo, "🔐 No token found — authenticating with healer backend...");

    const response = await axios.post<{ tokens: { access: string; refresh?: string } }>(AUTH_URL, CREDENTIALS, {
        headers: { "Content-Type": "application/json" },
    });

    const token = response.data.tokens.access;

    if (!token) {
        throw new Error("Authentication failed: no access token returned");
    }

    cachedToken = token;
    console.log("✅ Authentication successful. Token cached for this session.");
    appendHealingReportLog(testInfo, "✅ Authentication successful. Token cached for this session.");

    return cachedToken;
}

/**
 * Clears the cached token (e.g. if a 401 is received).
 */
export function clearCachedToken(): void {
    cachedToken = null;
}
