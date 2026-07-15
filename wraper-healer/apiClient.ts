import axios from "axios";
import type { TestInfo } from "@playwright/test";
import { getAccessToken, clearCachedToken } from "./auth";
import { appendHealingReportLog } from "./healingReportLogger";

const HEALER_API_BASE_URL = process.env.HEALER_API_BASE_URL || "http://127.0.0.1:8000/api";
const HEALER_API_TIMEOUT_MS = Number(process.env.HEALER_API_TIMEOUT_MS || "60000");

export const apiClient = axios.create({
    // Use localhost loopback for healer API calls.
    baseURL: HEALER_API_BASE_URL,
    timeout: HEALER_API_TIMEOUT_MS,
    headers: {
        "Content-Type": "application/json",
    },
});

function shouldRetryTransientError(err: any): boolean {
    const code = String(err?.code || "").toUpperCase();
    const status = Number(err?.response?.status || 0);
    if (code === "ECONNABORTED" || code === "ETIMEDOUT" || code === "ECONNRESET") return true;
    if (status >= 500 && status < 600) return true;
    return false;
}

/**
 * Authenticated POST — ensures a valid token is set before sending.
 * Automatically logs in if no token is cached, and retries once on 401.
 */
export async function authenticatedPost<T>(
    url: string,
    data: unknown,
    testInfo?: TestInfo
): Promise<{ data: T }> {
    const token = await getAccessToken(testInfo);

    try {
        const response = await apiClient.post<T>(url, data, {
            headers: { Authorization: `Bearer ${token}` },
        });
        return response;
    } catch (err: any) {
        // If the token was rejected, clear the cache and retry once with a fresh token
        if (err?.response?.status === 401) {
            console.warn("⚠️  Received 401 — token may have expired. Re-authenticating...");
            appendHealingReportLog(testInfo, "⚠️ Received 401 — token may have expired. Re-authenticating...");
            clearCachedToken();

            const freshToken = await getAccessToken(testInfo);
            const retryResponse = await apiClient.post<T>(url, data, {
                headers: { Authorization: `Bearer ${freshToken}` },
            });
            return retryResponse;
        }
        // Retry once for transient timeout/network/backend issues.
        if (shouldRetryTransientError(err)) {
            appendHealingReportLog(
                testInfo,
                `⚠️ Transient healer API error (${err?.code || err?.response?.status || "unknown"}). Retrying once...`
            );
            const retryResponse = await apiClient.post<T>(url, data, {
                headers: { Authorization: `Bearer ${token}` },
            });
            return retryResponse;
        }
        if (err?.response) {
            const statusCode = err.response.status;
            const body = typeof err.response.data === "string"
                ? err.response.data
                : JSON.stringify(err.response.data);
            throw new Error(`Healer API error ${statusCode}: ${body}`);
        }
        throw err;
    }
}
