/**
 * Phase 6 — Cucumber world scaffolding.
 *
 * Boots a fresh Chromium browser + page per scenario and exposes them to step
 * definitions via CustomWorld. Mirrors the fixture pattern used by
 * fixtures/baseFixture.ts for the legacy .spec.ts flow — but Cucumber-shaped.
 *
 * Step definitions consume `this.page` (typed via CustomWorld) so they never
 * import Playwright's browser/context directly.
 *
 *   Given('I open the homepage', async function (this: CustomWorld) {
 *       await this.page.goto('/');
 *   });
 */

import { After, Before, setWorldConstructor, World, IWorldOptions } from "@cucumber/cucumber";
import { Browser, BrowserContext, Page, chromium } from "@playwright/test";

const HEADLESS = (process.env.HEADLESS ?? "true").toLowerCase() !== "false";
const BASE_URL = process.env.BASE_URL || "";
// Optional HTTP Basic Auth credentials — set by the Executor from the Jira
// story's `preconditions.http_basic` block. Playwright answers the auth
// dialog before any page loads; without this the browser hangs on
// unauthenticated storefronts (staging.pulze.com, etc.).
const HTTP_BASIC_USERNAME = process.env.HTTP_BASIC_USERNAME || "";
const HTTP_BASIC_PASSWORD = process.env.HTTP_BASIC_PASSWORD || "";

export class CustomWorld extends World {
    browser!: Browser;
    context!: BrowserContext;
    page!: Page;

    constructor(options: IWorldOptions) {
        super(options);
    }

    async open(): Promise<void> {
        this.browser = await chromium.launch({ headless: HEADLESS });
        const contextOptions: Parameters<Browser["newContext"]>[0] = {
            baseURL: BASE_URL || undefined,
        };
        if (HTTP_BASIC_USERNAME && HTTP_BASIC_PASSWORD) {
            contextOptions.httpCredentials = {
                username: HTTP_BASIC_USERNAME,
                password: HTTP_BASIC_PASSWORD,
            };
        }
        this.context = await this.browser.newContext(contextOptions);
        this.page = await this.context.newPage();
    }

    async close(): Promise<void> {
        // `?.` guards so a failed Before hook doesn't crash After.
        await this.page?.close().catch(() => undefined);
        await this.context?.close().catch(() => undefined);
        await this.browser?.close().catch(() => undefined);
    }
}

setWorldConstructor(CustomWorld);

Before(async function (this: CustomWorld) {
    await this.open();
});

After(async function (this: CustomWorld) {
    await this.close();
});
