import json
import os

MEMORY_FILE = "ignore_rules/change_memory.json"


def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return {}
    with open(MEMORY_FILE) as f:
        return json.load(f)


def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def update_change_record(element_key, changed):
    memory = load_memory()

    if element_key not in memory:
        memory[element_key] = {
            "runs": 0,
            "changes": 0
        }

    memory[element_key]["runs"] += 1

    if changed:
        memory[element_key]["changes"] += 1

    save_memory(memory)


def change_frequency(element_key):
    memory = load_memory()

    if element_key not in memory:
        return 0

    data = memory[element_key]

    if data["runs"] == 0:
        return 0

    return data["changes"] / data["runs"]
