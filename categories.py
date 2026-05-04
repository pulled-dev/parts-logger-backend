"""Category dictionary for body panel and drivetrain tiles.

Maps internal tile keys to eBay search metadata. The frontend uses
list_categories() for the tile grid; the /lookup-panel endpoint uses
get_category() for search query construction.
"""

from __future__ import annotations


CATEGORIES: dict[str, dict] = {
    # ── Body panels (use_paint_code=True) ──────────────────────────
    "front_bumper": {
        "label": "Front Bumper",
        "search_keywords": "front bumper",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "rear_bumper": {
        "label": "Rear Bumper",
        "search_keywords": "rear bumper",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "bonnet": {
        "label": "Bonnet",
        "search_keywords": "bonnet",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "tailgate": {
        "label": "Tailgate",
        "search_keywords": "tailgate",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "ns_front_door": {
        "label": "N/S Front Door",
        "search_keywords": "passenger front door",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "os_front_door": {
        "label": "O/S Front Door",
        "search_keywords": "driver front door",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "ns_rear_door": {
        "label": "N/S Rear Door",
        "search_keywords": "passenger rear door",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "os_rear_door": {
        "label": "O/S Rear Door",
        "search_keywords": "driver rear door",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "ns_front_wing": {
        "label": "N/S Front Wing",
        "search_keywords": "passenger front wing",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "os_front_wing": {
        "label": "O/S Front Wing",
        "search_keywords": "driver front wing",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "roof": {
        "label": "Roof",
        "search_keywords": "roof panel",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "ns_rear_quarter": {
        "label": "N/S Rear Quarter",
        "search_keywords": "passenger rear quarter panel",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    "os_rear_quarter": {
        "label": "O/S Rear Quarter",
        "search_keywords": "driver rear quarter panel",
        "use_paint_code": True,
        "category_group": "body_panel",
    },
    # ── Drivetrain (use_paint_code=False) ──────────────────────────
    "engine": {
        "label": "Engine",
        "search_keywords": "engine",
        "use_paint_code": False,
        "category_group": "drivetrain",
    },
    "gearbox": {
        "label": "Gearbox",
        "search_keywords": "gearbox",
        "use_paint_code": False,
        "category_group": "drivetrain",
    },
    "turbo": {
        "label": "Turbo",
        "search_keywords": "turbo",
        "use_paint_code": False,
        "category_group": "drivetrain",
    },
}

VALID_CATEGORY_KEYS: set[str] = set(CATEGORIES.keys())


def get_category(key: str) -> dict | None:
    """Return category dict or None if key not found."""
    return CATEGORIES.get(key)


def list_categories() -> list[dict]:
    """Return list of {key, label, category_group} for frontend tile grid.
    Does NOT expose search_keywords."""
    return [
        {"key": k, "label": v["label"], "category_group": v["category_group"]}
        for k, v in CATEGORIES.items()
    ]


def list_category_keys() -> list[str]:
    """Return just the keys, useful for validation."""
    return list(CATEGORIES.keys())
