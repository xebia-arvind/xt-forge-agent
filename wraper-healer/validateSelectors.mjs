#!/usr/bin/env node
import { chromium } from "playwright";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const val = argv[i + 1];
    if (!key || !key.startsWith("--")) continue;
    args[key.slice(2)] = val;
    i += 1;
  }
  return args;
}

function safeJsonParse(raw, fallback) {
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function toAbsolute(baseUrl, maybeUrl) {
  try {
    return new URL(maybeUrl, baseUrl).toString();
  } catch {
    return null;
  }
}

async function run() {
  const args = parseArgs(process.argv);
  const baseUrl = args["base-url"] || "http://localhost:3000";
  const selectors = safeJsonParse(args["selectors"] || "[]", []);
  const urlsRaw = safeJsonParse(args["urls"] || "[]", []);
  const urls = urlsRaw.map((u) => toAbsolute(baseUrl, u)).filter(Boolean).slice(0, 30);

  const output = {
    base_url: baseUrl,
    checked_urls: urls,
    checked_selectors: selectors.length,
    results: [],
    warnings: [],
  };

  if (!selectors.length || !urls.length) {
    process.stdout.write(JSON.stringify(output));
    return;
  }

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(6000);

    for (const selectorRaw of selectors.slice(0, 120)) {
      const selector = String(selectorRaw || "").trim();
      if (!selector) continue;

      let matched = false;
      let matchedUrl = null;
      let lastError = "";

      for (const url of urls) {
        try {
          await page.goto(url, { waitUntil: "domcontentloaded" });
        } catch (err) {
          lastError = `goto_failed:${String(err).slice(0, 180)}`;
          continue;
        }

        try {
          const count = await page.locator(selector).count();
          if (count > 0) {
            matched = true;
            matchedUrl = url;
            break;
          }
          lastError = "not_found";
        } catch (err) {
          lastError = `invalid_or_runtime_selector:${String(err).slice(0, 180)}`;
        }
      }

      output.results.push({
        selector,
        matched,
        matched_url: matchedUrl,
        error: matched ? "" : lastError || "not_found",
      });
    }

    await context.close();
    await browser.close();
  } catch (err) {
    if (browser) {
      try {
        await browser.close();
      } catch {
        // ignore browser close errors
      }
    }
    output.warnings.push(`validator_failed:${String(err).slice(0, 220)}`);
  }

  process.stdout.write(JSON.stringify(output));
}

run();
