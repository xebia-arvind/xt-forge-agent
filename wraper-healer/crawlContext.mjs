#!/usr/bin/env node
import { chromium } from "playwright";
import fs from "fs";
import path from "path";
import os from "os";


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

function toAbsolute(baseUrl, raw) {
  if (!raw) return null;
  try {
    return new URL(raw, baseUrl).toString();
  } catch {
    return null;
  }
}

function normalizePath(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return url;
  }
}

function sanitizeFileName(input) {
  return String(input || "")
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 120) || "route";
}

function screenshotFileName(routeUrl, index) {
  try {
    const u = new URL(routeUrl);
    const pathPart = u.pathname === "/" ? "home" : u.pathname.replace(/\//g, "_");
    return `${String(index).padStart(3, "0")}_${sanitizeFileName(pathPart)}.png`;
  } catch {
    return `${String(index).padStart(3, "0")}_route.png`;
  }
}

function ignoreRoute(url) {
  const lower = (url || "").toLowerCase();
  return (
    lower.includes("/logout") ||
    lower.endsWith(".png") ||
    lower.endsWith(".jpg") ||
    lower.endsWith(".jpeg") ||
    lower.endsWith(".svg") ||
    lower.endsWith(".css") ||
    lower.endsWith(".js") ||
    lower.includes("/static/")
  );
}

async function extractRouteData(page, maxInteractables) {
  return page.evaluate((limit) => {
    function domHash() {
      const body = document.body?.innerHTML || "";
      let hash = 0;
      for (let i = 0; i < body.length; i++) {
        hash = ((hash << 5) - hash) + body.charCodeAt(i);
        hash |= 0;
      }
      return String(hash);
    }

    function selectorScore(el) {
      if (el.getAttribute("data-testid")) return 100;
      if (el.id) return 90;
      if (el.getAttribute("aria-label")) return 80;
      if (el.getAttribute("name")) return 70;
      if (el.className) return 40;
      return 20;
    }

    const nodes = Array.from(
      document.querySelectorAll(
        'a, button, input, textarea, select, [role], [data-testid], [aria-label]'
      )
    );

    const interactables = nodes.slice(0, limit).map((el) => {
      const rect = el.getBoundingClientRect();
      const tag = (el.tagName || "").toLowerCase();
      const role = el.getAttribute("role") || "";
      const testId = el.getAttribute("data-testid") || "";
      const aria = el.getAttribute("aria-label") || "";
      const id = el.id || "";
      const name = el.getAttribute("name") || "";
      const type = el.getAttribute("type") || "";
      const text = (el.textContent || "")
        .trim()
        .replace(/\s+/g, " ")
        .slice(0, 120);
      const href = el.getAttribute("href") || "";

      const selectorHints = [];
      if (testId) selectorHints.push(`[data-testid="${testId}"]`);
      if (id) selectorHints.push(`#${id}`);
      if (tag && role) selectorHints.push(`${tag}[role="${role}"]`);
      if (tag && aria) selectorHints.push(`${tag}[aria-label="${aria}"]`);
      if (tag && name) selectorHints.push(`${tag}[name="${name}"]`);
      if (tag && type) selectorHints.push(`${tag}[type="${type}"]`);
      if (tag && href) selectorHints.push(`${tag}[href="${href}"]`);
      if (tag && text) selectorHints.push(`${tag}:has-text("${text.slice(0, 40)}")`);

      return {
        tag,
        role,
        test_id: testId,
        aria_label: aria,
        id,
        name,
        type,
        text,
        href,
        selector_hints: selectorHints.slice(0, 8),
        selector_score: selectorScore(el),
        layout: {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
        },
        parent_tag: el.parentElement?.tagName?.toLowerCase() || "",
      };
    });

    const forms = Array.from(document.querySelectorAll("form")).map((form, index) => {
      const fields = Array.from(form.querySelectorAll("input, textarea, select")).map((field) => ({
        tag: (field.tagName || "").toLowerCase(),
        name: field.getAttribute("name") || "",
        id: field.getAttribute("id") || "",
        type: field.getAttribute("type") || "",
        placeholder: field.getAttribute("placeholder") || "",
      }));
      return {
        form_index: index,
        action: form.getAttribute("action") || "",
        method: form.getAttribute("method") || "get",
        fields: fields.slice(0, 40),
      };
    });

    const links = Array.from(document.querySelectorAll("a[href]")).map((a) => ({
      href: a.getAttribute("href") || "",
      text: (a.textContent || "").trim().replace(/\s+/g, " ").slice(0, 80),
    }));

    return {
      title: document.title || "",
      dom_hash: domHash(),
      interactables,
      forms: forms.slice(0, 20),
      links: links.slice(0, 300),
    };
  }, maxInteractables);
}

async function run() {
  const args = parseArgs(process.argv);
  const baseUrl = args["base-url"] || "http://localhost:3000";
  const maxRoutes = Number(args["max-routes"] || 20);
  const maxDepth = Number(args["max-depth"] || 2);
  const maxInteractables = Number(args["max-interactables"] || 200);
  const screenshotDir = args["screenshot-dir"] || "";
  const seedUrls = safeJsonParse(args["seed-urls"] || "[]", []);

  const queue = [];
  const visited = new Set();
  const warnings = [];

  const initialSeeds = seedUrls.length ? seedUrls : ["/"];
  for (const seed of initialSeeds) {
    const abs = toAbsolute(baseUrl, seed);
    if (!abs) continue;
    queue.push({ url: abs, depth: 0 });
  }

  let browser;
  const routes = [];
  try {
    const userCache = path.join(os.homedir(), "Library", "Caches", "ms-playwright");
    const cftPath = path.join(
      userCache,
      "chromium-1208",
      "chrome-mac-arm64",
      "Google Chrome for Testing.app",
      "Contents",
      "MacOS",
      "Google Chrome for Testing"
    );
    const launchOptions = {
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"]
    };
    const envExecutablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    if (envExecutablePath && fs.existsSync(envExecutablePath)) {
      launchOptions.executablePath = envExecutablePath;
    } else if (fs.existsSync(cftPath)) {
      launchOptions.executablePath = cftPath;
    }
    const envChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;
    if (envChannel) {
      launchOptions.channel = envChannel;
    }
    browser = await chromium.launch(launchOptions);
    const contextOptions = {};
    const basicUser = process.env.HTTP_BASIC_USERNAME || "";
    const basicPass = process.env.HTTP_BASIC_PASSWORD || "";
    if (basicUser && basicPass) {
      contextOptions.httpCredentials = { username: basicUser, password: basicPass };
    }
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    page.setDefaultTimeout(6000);
    if (screenshotDir) {
      fs.mkdirSync(screenshotDir, { recursive: true });
    }

    while (queue.length > 0 && routes.length < maxRoutes) {
      const current = queue.shift();
      if (!current) break;
      const normalized = normalizePath(current.url);
      if (visited.has(normalized)) continue;
      if (ignoreRoute(current.url)) continue;
      visited.add(normalized);

      try {
        await page.goto(current.url, { waitUntil: "domcontentloaded" });
      } catch (err) {
        warnings.push(`Failed to open ${current.url}: ${String(err).slice(0, 180)}`);
        continue;
      }

      // Async banners (OneTrust cookies, chat widgets, GDPR modals) are
      // typically injected 1-3s AFTER domcontentloaded. Without this
      // settle delay, the crawler snapshots too early and misses them
      // entirely — every downstream stage then has to un-guess a
      // selector that ui_knowledge never captured. Env-tunable; set
      // POST_LOAD_SETTLE_MS=0 to disable if a portal is truly instant.
      const settleMs = Number(process.env.POST_LOAD_SETTLE_MS || 3000);
      if (settleMs > 0) {
        await Promise.race([
          page.waitForLoadState("networkidle", { timeout: settleMs }).catch(() => null),
          page.waitForTimeout(settleMs),
        ]);
      }

      // If the page redirected DURING the settle (e.g. auth-gated route
      // sending us to Azure B2C sign-in), the initial `domcontentloaded`
      // event is stale and `page.evaluate` will die with
      // "Execution context was destroyed". Wait for the current URL to
      // reach a stable domcontentloaded state before extraction. Retry
      // once on context-destroyed as a belt-and-suspenders fallback.
      try {
        await page.waitForLoadState("domcontentloaded", { timeout: 5000 });
      } catch { /* ignore — extraction will retry */ }

      let data;
      try {
        data = await extractRouteData(page, maxInteractables);
      } catch (err) {
        const msg = String(err || "");
        if (msg.includes("Execution context was destroyed") || msg.includes("Target closed")) {
          // The page navigated mid-extract. Wait for the new DOM, then retry once.
          try {
            await page.waitForLoadState("domcontentloaded", { timeout: 8000 });
            await page.waitForTimeout(1500);
            data = await extractRouteData(page, maxInteractables);
          } catch (err2) {
            warnings.push(`Extraction failed for ${current.url} after retry: ${String(err2).slice(0, 180)}`);
            continue;
          }
        } else {
          warnings.push(`Extraction failed for ${current.url}: ${msg.slice(0, 180)}`);
          continue;
        }
      }
      let screenshotPath = "";
      if (screenshotDir) {
        try {
          const filename = screenshotFileName(page.url(), routes.length + 1);
          const fullPath = path.resolve(path.join(screenshotDir, filename));
          await page.screenshot({ path: fullPath, fullPage: true });
          screenshotPath = fullPath;
        } catch (err) {
          warnings.push(`Screenshot failed for ${page.url()}: ${String(err).slice(0, 180)}`);
        }
      }
      routes.push({
        url: page.url(),
        title: data.title,
        depth: current.depth,
        interactables: data.interactables,
        forms: data.forms,
        dom_hash: data.dom_hash,
        screenshot_path: screenshotPath,
      });

      if (current.depth >= maxDepth) continue;
      for (const link of data.links || []) {
        const abs = toAbsolute(baseUrl, link.href);
        if (!abs || ignoreRoute(abs)) continue;
        try {
          const asUrl = new URL(abs);
          const base = new URL(baseUrl);
          if (asUrl.origin !== base.origin) continue;
        } catch {
          continue;
        }
        queue.push({ url: abs, depth: current.depth + 1 });
      }
    }

    await context.close();
    await browser.close();
  } catch (err) {
    if (browser) {
      try {
        await browser.close();
      } catch {
        // ignore close errors
      }
    }
    warnings.push(`Crawler failed: ${String(err).slice(0, 240)}`);
  }

  const response = {
    base_url: baseUrl,
    seed_urls: initialSeeds,
    max_routes: maxRoutes,
    max_depth: maxDepth,
    routes_visited: routes.length,
    routes,
    warnings,
  };

  process.stdout.write(JSON.stringify(response));
}

run();
