# runner.py

import os
from ui_capture.capture import capture_ui
from layout_diff.diff_engine import (
    diff_layout,
    load_layout,
    prepare_layout
)
from visual_diff.visual_diff import visual_diff
from severity.severity_engine import calculate_severity
from ignore_rules.filter import load_rules
from reporting.human_readable import format_report
# Load rules once
rules = load_rules()

URL = "file:///Users/arvind.kumar1/Downloads/ui_change_detection_sampledata/dashboard_current.html"

BASELINE_DIR = "ui-data/baseline"
CURRENT_DIR = "ui-data/current"


def capture_baseline():
    print("📸 Capturing BASELINE UI")
    capture_ui(URL, BASELINE_DIR)


def capture_current():
    print("📸 Capturing CURRENT UI")
    capture_ui(URL, CURRENT_DIR)


def main():
    baseline_layout_path = os.path.join(BASELINE_DIR, "layout.json")
    current_layout_path = os.path.join(CURRENT_DIR, "layout.json")

    baseline_shot = os.path.join(BASELINE_DIR, "screenshot.png")
    current_shot = os.path.join(CURRENT_DIR, "screenshot.png")

    # 1️⃣ First run → create baseline
    if not os.path.exists(baseline_layout_path):
        os.makedirs(BASELINE_DIR, exist_ok=True)
        capture_baseline()
        print("✅ Baseline created")
        return

    # 2️⃣ Capture current UI
    capture_current()

    # 3️⃣ Load RAW layouts
    baseline_raw = load_layout(baseline_layout_path)
    current_raw = load_layout(current_layout_path)

    # 4️⃣ Apply ignore rules (DATA vs UI separation happens HERE)
    baseline_filtered = prepare_layout(baseline_raw)
    current_filtered = prepare_layout(current_raw)

    # 5️⃣ Layout diff (UI-only)
    layout_diff_result = diff_layout(
        baseline_filtered,
        current_filtered,
        rules
    )

    # 6️⃣ Visual diff
    visual_diff_result = visual_diff(
        baseline_path=baseline_shot,
        current_path=current_shot,
        mask_path=None,
        output_path="ui-data/visual_diff_result.png"
    )

    # 7️⃣ Severity decision
    severity_result = calculate_severity(
        layout_diff_result,
        visual_diff_result
    )

    # 8️⃣ Final result
    final_result = {
        "layout_diff": layout_diff_result,
        "visual_diff": visual_diff_result,
        "severity": severity_result
    }

    print("🚨 FINAL DIFF RESULT")
    print(final_result)

    print("\nHUMAN READABLE REPORT\n")
    print(format_report(final_result))


if __name__ == "__main__":
    main()
