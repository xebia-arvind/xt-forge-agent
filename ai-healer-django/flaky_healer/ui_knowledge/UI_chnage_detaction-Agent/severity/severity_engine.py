# severity/severity_engine.py

def calculate_severity(layout_diff, visual_diff):
    reasons = []
    severity = "LOW"

    # Missing elements = HIGH
    if layout_diff.get("missing_elements"):
        severity = "HIGH"
        reasons.append("Critical element removed")

    # Large position shifts
    for shift in layout_diff.get("position_shifts", []):
        dx = abs(shift["shift"]["dx"])
        dy = abs(shift["shift"]["dy"])
        if dx > 20 or dy > 20:
            severity = "HIGH"
            reasons.append(
                f"Large position shift detected ({dx}px, {dy}px)"
            )

    # Medium position shifts
    if severity != "HIGH":
        for shift in layout_diff.get("position_shifts", []):
            dx = abs(shift["shift"]["dx"])
            dy = abs(shift["shift"]["dy"])
            if dx > 5 or dy > 5:
                severity = "MEDIUM"
                reasons.append(
                    f"Moderate position shift ({dx}px, {dy}px)"
                )

    # Visual change without layout break
    if (
        visual_diff.get("visual_change")
        and not layout_diff.get("position_shifts")
        and severity == "LOW"
    ):
        severity = "MEDIUM"
        reasons.append("Visual change detected")

    # Cosmetic only
    if (
        visual_diff.get("pixel_change_ratio", 0) < 0.01
        and visual_diff.get("ssim_score", 1) > 0.97
        and severity == "LOW"
    ):
        reasons.append("Seems No change")

    if layout_diff.get("table_issues"):
        severity = "HIGH"
        reasons.append("Table structure changed")

    return {
        "severity": severity,
        "reasons": reasons
    }
