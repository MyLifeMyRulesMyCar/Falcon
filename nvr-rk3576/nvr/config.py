"""Configuration loading for the NVR."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nvr.inference.detector import COCO80_LABELS


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed or invalid."""


@dataclass
class ZoneConfig:
    name: str
    polygon: list[tuple[float, float]]
    trigger_classes: list[str]
    dwell_time_sec: float
    cooldown_sec: float


@dataclass
class CameraConfig:
    name: str
    url: str
    zones: list[ZoneConfig] = field(default_factory=list)


@dataclass
class NvrConfig:
    cameras: list[CameraConfig] = field(default_factory=list)


def load_config(path: str) -> NvrConfig:
    """Load an :class:`NvrConfig` from the YAML file at ``path``.

    Raises:
        ConfigError: if the file is missing/unreadable, ``cameras`` is
            missing or empty, any camera entry lacks ``name`` or ``url``, or
            a camera's ``zones`` fail validation (see ``_parse_zones``).

    Zone polygon coordinates are in the camera's *native decoded resolution*
    (whatever the M1 probe reported, e.g. 1280x720) — the same space the
    annotated preview renders in and the space :meth:`detector.detect` maps
    detections back to. Do NOT use the model's 640x640 letterboxed space:
    zones will either never trigger or line up in the wrong place.
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
        zones = parse_zones(entry.get("zones", []), entry["name"])
        parsed.append(CameraConfig(name=entry["name"], url=entry["url"], zones=zones))

    return NvrConfig(cameras=parsed)


def parse_zones(raw, camera_name: str) -> list[ZoneConfig]:
    """Validate and parse a camera's ``zones`` list; raise ConfigError with
    the offending zone name in the message. Catches a typo'd trigger class at
    load time instead of it silently never firing at runtime. Also used by
    the panel API to validate UI-supplied zones."""
    if not isinstance(raw, list):
        raise ConfigError(f"camera '{camera_name}': 'zones' must be a list")
    zones = []
    for z in raw:
        if not isinstance(z, dict):
            raise ConfigError(f"camera '{camera_name}': each zone must be a mapping")
        name = z.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"camera '{camera_name}': zone missing a 'name' string")
        polygon = _parse_polygon(z.get("polygon"), camera_name, name)
        trigger = z.get("trigger_classes")
        if (
            not isinstance(trigger, list)
            or not trigger
            or not all(isinstance(t, str) and t.strip() for t in trigger)
        ):
            raise ConfigError(
                f"camera '{camera_name}' zone '{name}': "
                "'trigger_classes' must be a non-empty list of class names"
            )
        unknown = [t for t in trigger if t not in COCO80_LABELS]
        if unknown:
            raise ConfigError(
                f"camera '{camera_name}' zone '{name}': unknown class(es) "
                f"{unknown} (must match COCO-80 labels)"
            )
        try:
            dwell = float(z.get("dwell_time_sec"))
            cooldown = float(z.get("cooldown_sec", 0.0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"camera '{camera_name}' zone '{name}': "
                "dwell_time_sec/cooldown_sec must be numbers"
            ) from exc
        if dwell <= 0:
            raise ConfigError(
                f"camera '{camera_name}' zone '{name}': dwell_time_sec must be > 0"
            )
        if cooldown < 0:
            raise ConfigError(
                f"camera '{camera_name}' zone '{name}': cooldown_sec must be >= 0"
            )
        zones.append(
            ZoneConfig(
                name=name,
                polygon=polygon,
                trigger_classes=trigger,
                dwell_time_sec=dwell,
                cooldown_sec=cooldown,
            )
        )
    return zones


def _parse_polygon(raw, camera_name: str, zone_name: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or len(raw) < 3:
        raise ConfigError(
            f"camera '{camera_name}' zone '{zone_name}': polygon must have >= 3 points"
        )
    polygon = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ConfigError(
                f"camera '{camera_name}' zone '{zone_name}': "
                "each polygon point must be [x, y]"
            )
        try:
            polygon.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"camera '{camera_name}' zone '{zone_name}': "
                "polygon points must be numbers"
            ) from exc
    return polygon


def write_config(path: str, cameras: list[CameraConfig]) -> None:
    """Persist cameras (name, url, zones) to ``path`` atomically.

    The serialized document is re-loaded with :func:`load_config` before the
    write so a serialization bug can never leave a broken config file on disk.
    Cameras are written in the given order; the ``zones`` key is omitted when
    empty. Hand-written comments in the original file are replaced by a
    generated header on the first save.
    """
    entries = []
    for c in cameras:
        entry: dict = {"name": c.name, "url": c.url}
        if c.zones:
            entry["zones"] = [
                {
                    "name": z.name,
                    "polygon": [[float(x), float(y)] for x, y in z.polygon],
                    "trigger_classes": list(z.trigger_classes),
                    "dwell_time_sec": float(z.dwell_time_sec),
                    "cooldown_sec": float(z.cooldown_sec),
                }
                for z in c.zones
            ]
        entries.append(entry)
    text = (
        "# Generated by the NVR control panel: name/url/zones edits made in\n"
        "# the UI are persisted here (hand edits are honored on load, but\n"
        "# replaced on the next save).\n"
        + yaml.safe_dump({"cameras": entries}, sort_keys=False)
    )
    tmp = f"{path}.tmp"
    Path(tmp).write_text(text, encoding="utf-8")
    try:
        load_config(tmp)  # round-trip validation before replacing
    except ConfigError:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
