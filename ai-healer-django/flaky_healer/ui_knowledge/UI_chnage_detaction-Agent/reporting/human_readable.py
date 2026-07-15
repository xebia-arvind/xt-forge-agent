def format_report(result):
    lines = []
    lines.append("UI Change Summary")
    lines.append("-" * 20)
    lines.append("")

    severity = result["severity"]["severity"]
    reasons = result["severity"]["reasons"]
    lines.append(f"Severity: {severity}")
    lines.append(f"Reasons: {reasons}")
    lines.append("")

    # ---- Visual ----
    visual = result["visual_diff"]
    if visual["visual_change"]:
        lines.append("Visual Changes:")
        lines.append(
            f"- Visual difference detected "
            f"({visual['pixel_change_ratio']*100:.2f}% of pixels changed)"
        )
        lines.append(
            f"- Structural similarity (SSIM): "
            f"{float(visual['ssim_score']):.4f}"
        )
        lines.append("")
    else:
        lines.append("No visual differences detected.")
        lines.append("")

    # ---- Layout ----
    layout = result["layout_diff"]

    if layout["size_changes"]:
        lines.append(f"{len(layout['size_changes'])} element(s) resized:")
        for change in layout["size_changes"]:
            old_h = change["old"]["h"]
            new_h = change["new"]["h"]
            delta_h = change["delta"]["dh"]

            lines.append(
                f"• {change['element']} height changed "
                f"from {old_h}px to {new_h}px "
                f"({delta_h:+}px)"
            )
        lines.append("")

    if layout["new_elements"]:
        lines.append(f"{len(layout['new_elements'])} new element(s) added:")
        for el in layout["new_elements"]:
            lines.append(f"• {el['element']}")
        lines.append("")

    if layout["missing_elements"]:
        lines.append(f"{len(layout['missing_elements'])} element(s) removed:")
        for el in layout["missing_elements"]:
            lines.append(f"• {el['element']}")
        lines.append("")

    if not (
        layout["size_changes"]
        or layout["new_elements"]
        or layout["missing_elements"]
    ):
        lines.append("No structural layout changes detected.")
        lines.append("")

    return "\n".join(lines)
