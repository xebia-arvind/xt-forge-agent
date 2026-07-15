# ui_capture/capture.py

from playwright.sync_api import sync_playwright
import json
import os

VIEWPORT = {"width": 1366, "height": 768}


def capture_ui(url: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=VIEWPORT)

        page.goto(url, wait_until="networkidle")

        # 📸 Screenshot
        page.screenshot(
            path=os.path.join(output_dir, "screenshot.png"),
            full_page=False
        )

        # 📄 DOM snapshot
        with open(os.path.join(output_dir, "dom.html"), "w", encoding="utf-8") as f:
            f.write(page.content())

        # 🧱 Layout extraction (RAW — no filtering here)
        layout = page.evaluate("""
        () => {
            return Array.from(document.querySelectorAll('*'))
              .map(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return null;

                const s = getComputedStyle(el);

                return {
                    tag: el.tagName,
                    id: el.id || null,
                    class: el.className || null,
                    text: el.innerText?.slice(0, 50) || null,
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    color: s.color,
                    background: s.backgroundColor,
                    fontSize: s.fontSize
                };
              })
              .filter(Boolean);
        }
        """)

        # 💾 Save RAW layout
        with open(os.path.join(output_dir, "layout.json"), "w") as f:
            json.dump(layout, f, indent=2)

        browser.close()
