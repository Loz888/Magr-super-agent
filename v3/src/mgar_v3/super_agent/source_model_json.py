"""Bounded transports use strict JSON: duplicate keys and non-finite numbers are ambiguous."""

import json
from typing import Any


def decode_model_json(raw: bytes | bytearray | str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate provider JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("Non-finite provider JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
