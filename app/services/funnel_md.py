"""A funnel as one .md file: settings and behaviour rules in a YAML header, the steps for the model below it.

    ---
    key: restaurant
    name: Restaurant
    label: order
    triggers: [save_order, website_post]
    fields: [{key: name, label: name, required: true, max: 100}, ...]
    checks: [{type: opening_hours, message: "..."}]
    rules:
      - name: Don't ask delivery or pickup again
        when: customer_said_fulfilment
        ...                               (services/rules.py says what a rule can hold)
    ---
    RESTAURANT ORDERS ...                 (the steps: what the model reads)

Rules are stored with the funnel's checks ({"type": "rule", ...}), so the file round-trips: to_md(from_md(x)) == x.
Keep the files in the repo (funnels/*.md) for history; the super admin imports one to make it a new version.
"""
import yaml

_ORDER = ("key", "name", "label", "triggers", "fields", "checks", "rules")


class FunnelFileError(ValueError):
    pass


def from_md(text: str) -> dict:
    """{key, name, label, steps, fields, triggers, checks} (FunnelIn's shape) from a funnel .md file.
    Raises FunnelFileError with a message for the super admin."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    if not text.startswith("---\n"):
        raise FunnelFileError('The file must start with a "---" line, then the settings, then another "---" line.')
    head, sep, steps = text[4:].partition("\n---\n")
    if not sep:
        raise FunnelFileError('The settings must end with a "---" line before the steps.')
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as exc:
        raise FunnelFileError(f"The settings are not valid YAML: {exc}") from None
    if not isinstance(meta, dict):
        raise FunnelFileError("The settings must be 'name: value' lines.")
    rules = meta.pop("rules", None) or []
    checks = meta.pop("checks", None) or []
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise FunnelFileError("rules must be a list, each starting with '- name:'.")
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        raise FunnelFileError("checks must be a list.")
    checks = [c for c in checks if c.get("type") != "rule"] + [{"type": "rule", **r} for r in rules]
    return {**meta, "steps": steps.strip("\n"), "checks": checks}


def to_md(funnel: dict) -> str:
    """The .md file for a funnel ({key, name, label, steps, fields, triggers, checks})."""
    checks = funnel.get("checks") or []
    meta = {
        "key": funnel.get("key"), "name": funnel.get("name"), "label": funnel.get("label"),
        "triggers": funnel.get("triggers") or [], "fields": funnel.get("fields") or [],
        "checks": [c for c in checks if c.get("type") != "rule"],
        "rules": [{k: v for k, v in c.items() if k != "type"} for c in checks if c.get("type") == "rule"],
    }
    head = yaml.safe_dump({k: meta[k] for k in _ORDER}, sort_keys=False, allow_unicode=True, width=110,
                          default_flow_style=None)  # word lists on one line each
    return f"---\n{head}---\n{(funnel.get('steps') or '').strip()}\n"
