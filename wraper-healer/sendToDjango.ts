// Phase 1: /test-analytics/test-result/ now requires JWT so each row is
// stamped with the caller's tenant. We reuse the same token cache as the
// healer client (auth.ts → getAccessToken / clearCachedToken).

import { getAccessToken, clearCachedToken } from "./auth";

const ANALYTICS_URL = "http://127.0.0.1:8000/test-analytics/test-result/";

async function postOnce(payload: unknown, token: string) {
    const response = await fetch(ANALYTICS_URL, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`,
        },
        body: JSON.stringify(payload),
    });

    const rawBody = await response.text();
    let parsedBody: unknown = rawBody;
    try {
        parsedBody = rawBody ? JSON.parse(rawBody) : {};
    } catch {
        // Not JSON — keep raw text.
    }
    return { response, parsedBody };
}

export async function sendToDjango(payload: any) {
    try {
        let token = await getAccessToken();
        let { response, parsedBody } = await postOnce(payload, token);

        // If the token was rejected, refresh once and retry.
        if (response.status === 401) {
            clearCachedToken();
            token = await getAccessToken();
            ({ response, parsedBody } = await postOnce(payload, token));
        }

        console.log("✔ Data sent to Django", {
            status: response.status,
            response: parsedBody,
        });
        if (!response.ok) {
            throw new Error(
                `Django API request failed: ${response.status} ${response.statusText} | body=${JSON.stringify(parsedBody)}`
            );
        }
    } catch (error) {
        console.error("❌ Failed to send data:", error);
        throw error;
    }
}
