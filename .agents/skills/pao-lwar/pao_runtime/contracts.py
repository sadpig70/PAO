from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    """A payload does not conform to its bundled PAO JSON contract."""


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"schema must be an object: {name}")
    return value


def _type_matches(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractError(f"unsupported schema type: {expected}")


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    alternatives = schema.get("oneOf")
    if alternatives is not None:
        matches = 0
        errors = []
        for option in alternatives:
            try:
                _validate(value, option, path)
                matches += 1
            except ContractError as error:
                errors.append(str(error))
        if matches != 1:
            raise ContractError(f"{path}: expected exactly one schema match; errors={errors}")
        return

    expected = schema.get("type")
    if expected is not None and not _type_matches(value, expected):
        raise ContractError(f"{path}: expected {expected}, got {type(value).__name__}")
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path}: value is not in {schema['enum']!r}")

    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ContractError(f"{path}: string is shorter than minLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise ContractError(f"{path}: string does not match {pattern}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ContractError(f"{path}: invalid date-time") from error

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path}: value is below minimum {schema['minimum']}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ContractError(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(
                key
                for key in set(value) - set(properties)
                if not any(re.fullmatch(pattern, key) for pattern in pattern_properties)
            )
            if extras:
                raise ContractError(f"{path}: unexpected keys {extras!r}")
        for key, child in properties.items():
            if key in value:
                _validate(value[key], child, f"{path}.{key}")
        for key, child_value in value.items():
            for pattern, child_schema in pattern_properties.items():
                if re.fullmatch(pattern, key):
                    _validate(child_value, child_schema, f"{path}.{key}")


def validate_contract(payload: dict[str, Any], schema_name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("$: expected object")
    _validate(payload, load_schema(schema_name), "$")
    return payload
