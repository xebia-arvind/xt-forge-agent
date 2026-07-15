import { Page, Locator, TestInfo } from "@playwright/test";
import { authenticatedPost } from "./apiClient";
import { HealResponse } from "./healer";
import { addStepEvent, setFailureContext } from "./failureContext";
import { appendHealingReportLog } from "./healingReportLogger";

type Colorize = (message: string) => string;

type LoggerColors = {
    green: Colorize;
    red: Colorize;
    purple: Colorize;
    teal: Colorize;
    orange: Colorize;
};

const passthrough: Colorize = (message) => message;
let loggerColorsPromise: Promise<LoggerColors> | undefined;

function getLoggerColors(): Promise<LoggerColors> {
    if (!loggerColorsPromise) {
        loggerColorsPromise = (async () => {
            try {
                // Avoid require() on ESM-only chalk by forcing native dynamic import.
                const dynamicImport = new Function("specifier", "return import(specifier)");
                const chalkModule = await dynamicImport("chalk") as any;
                const chalk = chalkModule.default ?? chalkModule;

                return {
                    green: chalk.bold.hex("#39FF14"),
                    red: chalk.bold.hex("#FF3131"),
                    purple: chalk.bold.hex("#bc13fe"),
                    teal: chalk.bold.hex("#00C2A8"),
                    orange: chalk.bold.hex("#FF8C00"),
                };
            } catch {
                return {
                    green: passthrough,
                    red: passthrough,
                    purple: passthrough,
                    teal: passthrough,
                    orange: passthrough,
                };
            }
        })();
    }

    return loggerColorsPromise;
}

type HealingOptions = {
    use_of_selector: string;
    selector_type: string;
    intent_key?: string;
};

async function clickUsingResolvedSelector(
    page: Page,
    healedSelector: string,
    healedXPath: string,
    timeoutMs: number
): Promise<{
    strategy: "CSS" | "XPATH";
    usedSelector: string;
}> {
    const selector = (healedSelector || "").trim();
    const xpath = (healedXPath || "").trim();

    if (selector) {
        const cssLocator = page.locator(selector);
        const count = await cssLocator.count();
        if (count === 1) {
            await cssLocator.click({ timeout: timeoutMs });
            return { strategy: "CSS", usedSelector: selector };
        }
        if (count > 1 && xpath) {
            const xpathLocator = page.locator(`xpath=${xpath}`);
            const xpathCount = await xpathLocator.count();
            if (xpathCount >= 1) {
                await xpathLocator.first().click({ timeout: timeoutMs });
                return { strategy: "XPATH", usedSelector: xpath };
            }
        }
        if (count > 1) {
            throw new Error(`Healed selector matched ${count} elements (strict mode).`);
        }
    }

    if (xpath) {
        const xpathLocator = page.locator(`xpath=${xpath}`);
        const xpathCount = await xpathLocator.count();
        if (xpathCount >= 1) {
            await xpathLocator.first().click({ timeout: timeoutMs });
            return { strategy: "XPATH", usedSelector: xpath };
        }
    }

    throw new Error("No usable healed CSS/XPath selector could be executed.");
}

export async function selfHealingClick(
    page: Page,
    locator: Locator,
    failedSelector: string,
    testInfo: TestInfo | undefined,
    options: HealingOptions
) {
    const colors = await getLoggerColors();

    setFailureContext(testInfo, {
        failedSelector,
        failureReason: options.use_of_selector,
        selectorType: options.selector_type,
        pageUrl: page.url(),
        healingAttempted: false,
        healingOutcome: "NOT_ATTEMPTED",
        healingConfidence: null,
    });

    try {
        // Try original Playwright locator
        const originalLocatorLog = `Original locator:  locator('${failedSelector}')`;
        console.log(originalLocatorLog);
        appendHealingReportLog(testInfo, originalLocatorLog);
        await locator.click({ timeout: 3000 });

        addStepEvent(testInfo, {
            step_name: options.use_of_selector,
            step_type: "action",
            status: "PASSED",
            failed_selector: failedSelector,
            message: "Original locator worked",
        });

        const originalPassLog = "✔ Original locator worked";
        console.log(colors.green(originalPassLog));
        appendHealingReportLog(testInfo, originalPassLog);

    } catch (error) {
        // Give the original locator one more chance before escalating to healer.
        try {
            await locator.click({ timeout: 5000 });
            addStepEvent(testInfo, {
                step_name: options.use_of_selector,
                step_type: "action",
                status: "PASSED",
                failed_selector: failedSelector,
                message: "Original locator worked on retry",
            });
            const retryPassLog = "✔ Original locator worked on retry";
            console.log(colors.green(retryPassLog));
            appendHealingReportLog(testInfo, retryPassLog);
            return;
        } catch {
            // Continue to healer fallback.
        }

        setFailureContext(testInfo, {
            failedSelector,
            failureReason: options.use_of_selector,
            selectorType: options.selector_type,
            pageUrl: page.url(),
            healingAttempted: true,
            healingOutcome: "FAILED",
            cacheHit: false,
            cacheFallbackToFresh: false,
            rootCause: "Original locator failed, attempting healer fallback",
        });

        const healerFallbackLog = "Locator failed. Sending to healer...";
        console.log(colors.red(healerFallbackLog))
        appendHealingReportLog(testInfo, healerFallbackLog);
        const html = await page.content();

        const screenshotBuffer = await page.screenshot();
        const screenshot = screenshotBuffer.toString("base64");
        const healRequestPayload = {
            test_name: testInfo?.title || "unknown_test",
            failed_selector: failedSelector,
            html,
            screenshot,
            page_url: page.url(),
            use_of_selector: options.use_of_selector,
            selector_type: options.selector_type,
            intent_key: options.intent_key,
        };

        let healedSelector = "";
        let healedConfidence: number | null = null;
        let validationStatus = "";
        let validationReason = "";
        let historyAssisted = false;
        let historyHits = 0;
        let uiChangeLevel = "UNKNOWN";
        let cacheHit = false;
        let cacheSourceId: number | undefined;
        let cacheFallbackToFresh = false;
        let healedXPath = "";
        let healingStrategyUsed: "CSS" | "XPATH" | "" = "";

        try {
            // authenticatedPost auto-logs in if no token is cached, and retries on 401
            const response = await authenticatedPost<HealResponse>("/heal/", healRequestPayload, testInfo);

            healedSelector = response.data.chosen || "";
            healedXPath = response.data.candidates?.[0]?.xpath || "";
            healedConfidence = response.data.candidates?.[0]?.score ?? null;
            validationStatus = response.data.validation_status || response.data.debug?.validation_status || "";
            validationReason = response.data.validation_reason || response.data.debug?.validation_reason || "";
            historyAssisted = response.data.history_assisted ?? response.data.debug?.history_assisted ?? false;
            historyHits = response.data.history_hits ?? response.data.debug?.history_hits ?? 0;
            uiChangeLevel = response.data.ui_change_level || response.data.debug?.ui_change_level || "UNKNOWN";
            cacheHit = response.data.debug?.cache_hit === true || response.data.debug?.engine === "history_cache";
            cacheSourceId = response.data.debug?.cache_source_id;

            if (validationStatus === "NO_SAFE_MATCH") {
                throw new Error(`Healing blocked by validation gate: ${validationReason || "No safe match"}`);
            }
            if (!healedSelector) {
                throw new Error("Healing failed: no selector returned");
            }

            const healedSelectorLog = `Using healed selector: ${healedSelector}`;
            console.log(colors.purple("Using healed selector:"), colors.purple(healedSelector));
            appendHealingReportLog(testInfo, healedSelectorLog);
            if (cacheHit) {
                console.log(
                    colors.teal(`Cache hit: reused historical selector${cacheSourceId ? ` (source_id=${cacheSourceId})` : ""}`)
                );
                appendHealingReportLog(
                    testInfo,
                    `Cache hit: reused historical selector${cacheSourceId ? ` (source_id=${cacheSourceId})` : ""}`
                );
            }

            // Retry using healed selector
            try {
                const clickResult = await clickUsingResolvedSelector(page, healedSelector, healedXPath, 5000);
                healingStrategyUsed = clickResult.strategy;
                if (clickResult.strategy === "XPATH") {
                    console.log(colors.teal("CSS ambiguous/failed, clicked via XPath fallback"));
                }
            } catch (clickError) {
                if (!cacheHit) {
                    throw clickError;
                }

                console.log(
                    colors.orange("Cached selector failed. Requesting fresh heal (skip cache)...")
                );

                const fallbackResponse = await authenticatedPost<HealResponse>(
                    "/heal/",
                    {
                        ...healRequestPayload,
                        skip_cache: true,
                    },
                    testInfo
                );

                healedSelector = fallbackResponse.data.chosen || "";
                healedXPath = fallbackResponse.data.candidates?.[0]?.xpath || "";
                healedConfidence = fallbackResponse.data.candidates?.[0]?.score ?? null;
                validationStatus = fallbackResponse.data.validation_status || fallbackResponse.data.debug?.validation_status || "";
                validationReason = fallbackResponse.data.validation_reason || fallbackResponse.data.debug?.validation_reason || "";
                historyAssisted = fallbackResponse.data.history_assisted ?? fallbackResponse.data.debug?.history_assisted ?? false;
                historyHits = fallbackResponse.data.history_hits ?? fallbackResponse.data.debug?.history_hits ?? 0;
                uiChangeLevel = fallbackResponse.data.ui_change_level || fallbackResponse.data.debug?.ui_change_level || "UNKNOWN";
                cacheHit = false;
                cacheSourceId = undefined;
                cacheFallbackToFresh = true;

                if (validationStatus === "NO_SAFE_MATCH" || !healedSelector) {
                    throw new Error(`Fresh healing after cache miss failed: ${validationReason || "No safe match"}`);
                }

                console.log(
                    colors.purple("Using freshly healed selector:"),
                    colors.purple(healedSelector)
                );
                appendHealingReportLog(testInfo, `Using freshly healed selector: ${healedSelector}`);
                const clickResult = await clickUsingResolvedSelector(page, healedSelector, healedXPath, 5000);
                healingStrategyUsed = clickResult.strategy;
                if (clickResult.strategy === "XPATH") {
                    console.log(colors.teal("Fresh heal clicked via XPath fallback"));
                }
            }

            addStepEvent(testInfo, {
                step_name: options.use_of_selector,
                step_type: "action",
                status: "HEALED",
                failed_selector: failedSelector,
                healed_selector: healedSelector,
                healing_confidence: healedConfidence,
                message: cacheHit
                    ? `Cached healed selector click succeeded via ${healingStrategyUsed || "CSS"}`
                    : `Healed selector click succeeded via ${healingStrategyUsed || "CSS"}`,
            });

            setFailureContext(testInfo, {
                failedSelector,
                failureReason: options.use_of_selector,
                selectorType: options.selector_type,
                pageUrl: page.url(),
                healingAttempted: true,
                healingOutcome: "SUCCESS",
                healedSelector,
                healingConfidence: healedConfidence,
                validationStatus,
                uiChangeLevel,
                historyAssisted,
                historyHits,
                cacheHit,
                cacheFallbackToFresh,
                rootCause: "Original locator failed but healed selector click succeeded",
            });

            const healedSuccessLog = "Click succeeded with healed selector.";
            console.log(colors.purple("Click succeeded with healed selector"));
            appendHealingReportLog(testInfo, healedSuccessLog);
        } catch (healerError: any) {
            const message = healerError?.message || "Unknown healer error";

            addStepEvent(testInfo, {
                step_name: options.use_of_selector,
                step_type: "action",
                status: "FAILED",
                failed_selector: failedSelector,
                healed_selector: healedSelector || undefined,
                healing_confidence: healedConfidence,
                message,
            });

            setFailureContext(testInfo, {
                failedSelector,
                failureReason: options.use_of_selector,
                selectorType: options.selector_type,
                pageUrl: page.url(),
                healingAttempted: true,
                healingOutcome: "FAILED",
                healedSelector: healedSelector || "",
                healingConfidence: healedConfidence,
                validationStatus: validationStatus || "NA",
                uiChangeLevel,
                historyAssisted,
                historyHits,
                cacheHit,
                cacheFallbackToFresh,
                rootCause: message,
            });

            throw healerError instanceof Error ? healerError : new Error(message);
        }
    }
}
