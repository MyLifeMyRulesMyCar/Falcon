"""Configuration loading for the NVR."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed or invalid."""


@dataclass
class CameraConfig:
    name: str
    url: str


@dataclass
class NvrConfig:
    cameras: list[CameraConfig] = field(default_factory=list)


def load_config(path: str) -> NvrConfig:
    """Load an :class:`NvrConfig` from the YAML file at ``path``.

    Raises:
        ConfigError: if the file is missing/unreadable, ``cameras`` is
            missing or empty, or any camera entry lacks ``name`` or ``url``.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse config file {path}: {exc}") from exc

    if not isinstance(raw, dict) or "cameras" not in raw:
        raise ConfigError("config file must contain a 'cameras' list")

    cameras = raw["cameras"]
    if not isinstance(cameras, list) or len(cameras) == 0:
        raise ConfigError("'cameras' must be a non-empty list")

    parsed: list[CameraConfig] = []
    for i, entry in enumerate(cameras):
        if not isinstance(entry, dict):
            raise ConfigError(f"camera entry {i} must be a mapping")
        if "name" not in entry or not isinstance(entry["name"], str):
            raise ConfigError(f"camera entry {i} is missing a 'name' string")
        if "url" not in entry or not isinstance(entry["url"], str):
            raise ConfigError(f"camera entry {i} is missing a 'url' string")
        parsed.append(CameraConfig(name=entry["name"], url=entry["url"]))

    return NvrConfig(cameras=parsed)
