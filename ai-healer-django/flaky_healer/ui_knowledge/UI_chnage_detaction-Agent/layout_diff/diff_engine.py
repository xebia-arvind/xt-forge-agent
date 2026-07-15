import json
from math import fabs
from ignore_rules.filter import filter_layout, load_rules
from ignore_rules.memory import update_change_record
POSITION_THRESHOLD = 5   # px
SIZE_THRESHOLD = 5       # px



def load_layout(path):
    with open(path) as f:
        return json.load(f)

# def element_key(el):
#     if el.get("id"):
#         return f"id::{el['id']}"
#     if el.get("text"):
#         return f"text::{el['tag']}::{el['text']}"
#     return f"class::{el['tag']}::{el.get('class')}"

# def element_key(el):
#     # 1️⃣ Best identity = ID
#     if el.get("id"):
#         return f"id::{el['id']}"

#     # 2️⃣ Fallback = geometry bucket
#     # rounding prevents micro pixel noise
#     x = round(el["x"] / 5) * 5
#     y = round(el["y"] / 5) * 5
#     w = round(el["width"] / 5) * 5
#     h = round(el["height"] / 5) * 5

#     return f"geom::{el['tag']}::{x}::{y}::{w}::{h}"

# def index_elements(elements):
#     index = {}
#     for el in elements:
#         index[element_key(el)] = el
#     return index
def find_matching_element(base_el, current_elements):
    # Skip table row-level elements from strict matching
    if base_el["tag"] in ["TR", "TD", "TBODY"]:
        return base_el  # treat as matched (ignore row-level diff)

    for curr_el in current_elements:
        if curr_el["tag"] != base_el["tag"]:
            continue

        dx = abs(curr_el["x"] - base_el["x"])
        dy = abs(curr_el["y"] - base_el["y"])

        if dx <= POSITION_THRESHOLD and dy <= POSITION_THRESHOLD:
            return curr_el

    return None
def extract_tables(layout):
    tables = []

    for el in layout:
        if el["tag"] == "TABLE":
            tables.append({
                "element": el,
                "headers": [],
                "rows": []
            })
    return tables

def extract_headers(layout):
    headers = []
    for el in layout:
        if el["tag"] == "TH":
            headers.append(el["text"])
    return headers

def diff_table(baseline_headers, current_headers):
    issues = []

    if len(baseline_headers) != len(current_headers):
        issues.append("COLUMN_COUNT_CHANGED")

    if baseline_headers != current_headers:
        issues.append("HEADER_TEXT_OR_ORDER_CHANGED")

    return issues

# def diff_layout(baseline, current,rules):
#     base_idx = index_elements(baseline)
#     curr_idx = index_elements(current)

#     position_shifts = []
#     size_changes = []
#     missing = []
#     new = []

#     for key, base_el in base_idx.items():
#         curr_el = curr_idx.get(key)

#         if not curr_el:
#             missing.append({
#                 "element": key,
#                 "issue": "ELEMENT_REMOVED"
#             })
#             continue

#         dx = curr_el["x"] - base_el["x"]
#         dy = curr_el["y"] - base_el["y"]

#         if fabs(dx) > POSITION_THRESHOLD or fabs(dy) > POSITION_THRESHOLD:
#             position_shifts.append({
#                 "element": key,
#                 "old": {"x": base_el["x"], "y": base_el["y"]},
#                 "new": {"x": curr_el["x"], "y": curr_el["y"]},
#                 "shift": {"dx": dx, "dy": dy}
#             })

#         dw = curr_el["width"] - base_el["width"]
#         dh = curr_el["height"] - base_el["height"]

#         if fabs(dw) > SIZE_THRESHOLD or fabs(dh) > SIZE_THRESHOLD:
#             size_changes.append({
#                 "element": key,
#                 "old": {"w": base_el["width"], "h": base_el["height"]},
#                 "new": {"w": curr_el["width"], "h": curr_el["height"]},
#                 "delta": {"dw": dw, "dh": dh}
#             })

#     for key in curr_idx:
#         if key not in base_idx:
#             new.append({
#                 "element": key,
#                 "issue": "NEW_ELEMENT"
#             })

#     return {
#         "position_shifts": position_shifts,
#         "size_changes": size_changes,
#         "missing_elements": missing,
#         "new_elements": new
#     }
# def diff_layout(baseline, current, rules):
#     base_idx = index_elements(baseline)
#     curr_idx = index_elements(current)

#     position_shifts = []
#     size_changes = []
#     missing = []
#     new = []

#     # 🔥 NEW: TABLE-AWARE DIFF (STRUCTURE ONLY)
#     table_issues = []
#         # if rules.get("table_rules", {}).get("enabled"):
#         #     table_issues = diff_table(baseline, current)
#     if rules.get("table_rules", {}).get("enabled"):
#         base_headers = extract_headers(baseline)
#         curr_headers = extract_headers(current)
#         table_issues = diff_table(base_headers, curr_headers)

#     for key, base_el in base_idx.items():
#         curr_el = curr_idx.get(key)

#         # 1️⃣ Element removed (real UI removal)
#         if not curr_el:
#             missing.append({
#                 "element": key,
#                 "issue": "ELEMENT_REMOVED"
#             })
#             continue

#         # 🔥 2️⃣ IGNORE TEXT-ONLY CHANGE (EXACT PLACE)
#         if rules.get("ignore_if", {}).get("only_text_changed"):
#             same_box = (
#                 base_el["x"] == curr_el["x"] and
#                 base_el["y"] == curr_el["y"] and
#                 base_el["width"] == curr_el["width"] and
#                 base_el["height"] == curr_el["height"]
#             )

#             if same_box:
#                 # Same position & size → text-only change
#                 continue
#         text_changed = base_el.get("text") != curr_el.get("text")

#         update_change_record(key, text_changed)

#         # 3️⃣ Position shift detection
#         dx = curr_el["x"] - base_el["x"]
#         dy = curr_el["y"] - base_el["y"]

#         if fabs(dx) > POSITION_THRESHOLD or fabs(dy) > POSITION_THRESHOLD:
#             position_shifts.append({
#                 "element": key,
#                 "old": {"x": base_el["x"], "y": base_el["y"]},
#                 "new": {"x": curr_el["x"], "y": curr_el["y"]},
#                 "shift": {"dx": dx, "dy": dy}
#             })

#         # 4️⃣ Size change detection
#         dw = curr_el["width"] - base_el["width"]
#         dh = curr_el["height"] - base_el["height"]

#         if fabs(dw) > SIZE_THRESHOLD or fabs(dh) > SIZE_THRESHOLD:
#             size_changes.append({
#                 "element": key,
#                 "old": {"w": base_el["width"], "h": base_el["height"]},
#                 "new": {"w": curr_el["width"], "h": curr_el["height"]},
#                 "delta": {"dw": dw, "dh": dh}
#             })

#     # 5️⃣ New UI elements
#     for key in curr_idx:
#         if key not in base_idx:
#             new.append({
#                 "element": key,
#                 "issue": "NEW_ELEMENT"
#             })

#     return {
#         "position_shifts": position_shifts,
#         "size_changes": size_changes,
#         "missing_elements": missing,
#         "new_elements": new,
#         "table_issues": table_issues
#     }

def diff_layout(baseline, current, rules):
    position_shifts = []
    size_changes = []
    missing = []
    new = []

    # TABLE AWARE
    table_issues = []
    if rules.get("table_rules", {}).get("enabled"):
        base_headers = extract_headers(baseline)
        curr_headers = extract_headers(current)
        table_issues = diff_table(base_headers, curr_headers)

    # ---- MATCH BASELINE ELEMENTS ----
    for base_el in baseline:
        curr_el = find_matching_element(base_el, current)

        if not curr_el:
            missing.append({
                "element": base_el["tag"],
                "issue": "ELEMENT_REMOVED"
            })
            continue

        # Ignore text-only change
        if rules.get("ignore_if", {}).get("only_text_changed"):
            same_box = (
                base_el["x"] == curr_el["x"] and
                base_el["y"] == curr_el["y"] and
                base_el["width"] == curr_el["width"] and
                base_el["height"] == curr_el["height"]
            )
            if same_box:
                continue

        dx = curr_el["x"] - base_el["x"]
        dy = curr_el["y"] - base_el["y"]

        if abs(dx) > POSITION_THRESHOLD or abs(dy) > POSITION_THRESHOLD:
            position_shifts.append({
                "element": base_el["tag"],
                "old": {"x": base_el["x"], "y": base_el["y"]},
                "new": {"x": curr_el["x"], "y": curr_el["y"]},
                "shift": {"dx": dx, "dy": dy}
            })

        dw = curr_el["width"] - base_el["width"]
        dh = curr_el["height"] - base_el["height"]

        if base_el["tag"] in ["TABLE", "TBODY", "DIV"]:
            # Allow height growth
            if dw == 0 and dh > 0:
                continue

        if abs(dw) > SIZE_THRESHOLD or abs(dh) > SIZE_THRESHOLD:
            size_changes.append({
                "element": base_el["tag"],
                "old": {"w": base_el["width"], "h": base_el["height"]},
                "new": {"w": curr_el["width"], "h": curr_el["height"]},
                "delta": {"dw": dw, "dh": dh}
            })

    # ---- FIND NEW ELEMENTS ----
    for curr_el in current:
        base_match = find_matching_element(curr_el, baseline)
        if not base_match:
            new.append({
                "element": curr_el["tag"],
                "issue": "NEW_ELEMENT"
            })

    return {
        "position_shifts": position_shifts,
        "size_changes": size_changes,
        "missing_elements": missing,
        "new_elements": new,
        "table_issues": table_issues
    }

def prepare_layout(raw_layout):
    rules = load_rules()
    filtered, ignored = filter_layout(raw_layout, rules)
    return filtered

if __name__ == "__main__":
    baseline = load_layout("baseline/layout.json")
    current = load_layout("current/layout.json")

    diff = diff_layout(baseline, current)

    print(json.dumps(diff, indent=2))
