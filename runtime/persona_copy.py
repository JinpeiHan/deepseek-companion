from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REQUIRED_INTERACTIONS = ("headPat", "tail", "poke", "doubleClick")


def load_persona_copy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("characterName") != "小鲸鱼":
        raise ValueError("persona copy must name 小鲸鱼")
    interaction = value.get("interaction")
    if not isinstance(interaction, dict):
        raise ValueError("persona copy interaction must be an object")
    for group in _REQUIRED_INTERACTIONS:
        variants = interaction.get(group)
        if not isinstance(variants, list) or not variants or not all(isinstance(item, str) and item for item in variants):
            raise ValueError(f"persona interaction group is invalid: {group}")
    return value


def interaction_copy(copy: dict[str, Any], group: str, seed: int = 0) -> str:
    variants = copy["interaction"][group]
    return variants[abs(int(seed)) % len(variants)]
