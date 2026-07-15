import json
from ignore_rules.classifier import classify_element

def load_rules(path="ignore_rules/rules.json"):
    with open(path) as f:
        return json.load(f)


def filter_layout(layout, rules):
    filtered = []
    ignored = []

    for el in layout:
        classification = classify_element(el, rules)

        if classification == "DYNAMIC":
            ignored.append(el)
        else:
            filtered.append(el)

    return filtered, ignored
