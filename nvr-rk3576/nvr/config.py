"""Configuration loading for the NVR."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
class MqttConfig:
    host: str
    port: int = 1883
    topic_prefix: str = "nvr"
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True


@dataclass
class HttpOutputConfig:
    url: str
    timeout_sec: float = 3.0
    enabled: bool = True


@dataclass
class SnapshotConfig:
    """v1.1 event snapshot retention: max file count per camera."""

    base_dir: str = "snapshots"
    max_per_camera: int = 200


@dataclass
class ClipConfig:
    """v1.3 event clips: post-roll only, ~6fps (LatestFrameStore's preview
    cadence — see nvr/output/clip_store.py). duration_sec is user-configured."""

    base_dir: str = "clips"
    max_per_camera: int = 30  # video files are much bigger than snapshots
    duration_sec: float = 10.0


@dataclass
class CameraConfig:
    name: str
    url: str
    zones: list[ZoneConfig] = field(default_factory=list)
    publish_zone_events: bool = True
    publish_detections: bool = False  # opt-in — high volume if left on
    detection_publish_interval_sec: float = 5.0  # throttle for publish_detections


@dataclass
class NvrConfig:
    cameras: list[CameraConfig]
    mqtt: Optional[MqttConfig] = None
    http_output: Optional[HttpOutputConfig] = None
    snapshots: Optional[SnapshotConfig] = None
    clips: Optional[ClipConfig] = None


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
        publish_zone = entry.get("publish_zone_events", True)
        publish_det = entry.get("publish_detections", False)
        interval = entry.get("detection_publish_interval_sec", 5.0)
        if not isinstance(publish_zone, bool) or not isinstance(publish_det, bool):
            raise ConfigError(
                f"camera entry {i}: publish_zone_events/publish_detections must be booleans"
            )
        try:
            interval = float(interval)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"camera entry {i}: detection_publish_interval_sec must be a number"
            ) from exc
        if interval <= 0:
            raise ConfigError(
                f"camera entry {i}: detection_publish_interval_sec must be > 0"
            )
        parsed.append(
            CameraConfig(
                name=entry["name"],
                url=entry["url"],
                zones=zones,
                publish_zone_events=publish_zone,
                publish_detections=publish_det,
                detection_publish_interval_sec=interval,
            )
        )

    mqtt = _parse_mqtt(raw.get("mqtt"))
    http_output = _parse_http_output(raw.get("http_output"))
    snapshots = _parse_snapshots(raw.get("snapshots"))
    clips = _parse_clips(raw.get("clips"))
    return NvrConfig(
        cameras=parsed, mqtt=mqtt, http_output=http_output,
        snapshots=snapshots, clips=clips,
    )


def _parse_mqtt(raw) -> Optional[MqttConfig]:
    """Validate the optional top-level ``mqtt`` section; None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'mqtt' must be a mapping")
    host = raw.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("'mqtt.host' must be a non-empty string")
    try:
        port = int(raw.get("port", 1883))
    except (TypeError, ValueError) as exc:
        raise ConfigError("'mqtt.port' must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("'mqtt.port' must be in 1..65535")
    prefix = raw.get("topic_prefix", "nvr")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ConfigError("'mqtt.topic_prefix' must be a non-empty string")
    username = raw.get("username")
    password = raw.get("password")
    if username is not None and not isinstance(username, str):
        raise ConfigError("'mqtt.username' must be a string or absent")
    if password is not None and not isinstance(password, str):
        raise ConfigError("'mqtt.password' must be a string or absent")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'mqtt.enabled' must be a boolean")
    return MqttConfig(
        host=host,
        port=port,
        topic_prefix=prefix,
        username=username,
        password=password,
        enabled=enabled,
    )


def _parse_http_output(raw) -> Optional[HttpOutputConfig]:
    """Validate the optional top-level ``http_output`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'http_output' must be a mapping")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("'http_output.url' must be a non-empty string")
    try:
        timeout = float(raw.get("timeout_sec", 3.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("'http_output.timeout_sec' must be a number") from exc
    if timeout <= 0:
        raise ConfigError("'http_output.timeout_sec' must be > 0")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'http_output.enabled' must be a boolean")
    return HttpOutputConfig(url=url, timeout_sec=timeout, enabled=enabled)


def _parse_snapshots(raw) -> Optional[SnapshotConfig]:
    """Validate the optional top-level ``snapshots`` section; None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'snapshots' must be a mapping")
    base_dir = raw.get("base_dir", "snapshots")
    if not isinstance(base_dir, str) or not base_dir.strip():
        raise ConfigError("'snapshots.base_dir' must be a non-empty string")
    try:
        max_per_camera = int(raw.get("max_per_camera", 200))
    except (TypeError, ValueError) as exc:
        raise ConfigError("'snapshots.max_per_camera' must be an integer") from exc
    if max_per_camera <= 0:
        raise ConfigError("'snapshots.max_per_camera' must be > 0")
    return SnapshotConfig(base_dir=base_dir, max_per_camera=max_per_camera)


def _parse_clips(raw) -> Optional[ClipConfig]:
    """Validate the optional top-level ``clips`` section; None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'clips' must be a mapping")
    base_dir = raw.get("base_dir", "clips")
    if not isinstance(base_dir, str) or not base_dir.strip():
        raise ConfigError("'clips.base_dir' must be a non-empty string")
    try:
        max_per_camera = int(raw.get("max_per_camera", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("'clips.max_per_camera' must be an integer") from exc
    if max_per_camera <= 0:
        raise ConfigError("'clips.max_per_camera' must be > 0")
    try:
        duration_sec = float(raw.get("duration_sec", 10.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("'clips.duration_sec' must be a number") from exc
    if duration_sec <= 0:
        raise ConfigError("'clips.duration_sec' must be > 0")
    return ClipConfig(
        base_dir=base_dir, max_per_camera=max_per_camera, duration_sec=duration_sec
    )


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


def write_config(
    path: str,
    cameras: list[CameraConfig],
    mqtt: Optional[MqttConfig] = None,
    http_output: Optional[HttpOutputConfig] = None,
    snapshots: Optional[SnapshotConfig] = None,
    clips: Optional[ClipConfig] = None,
) -> None:
    """Persist cameras (name, url, zones, publish flags) plus the optional
    top-level ``mqtt`` / ``http_output`` / ``snapshots`` / ``clips`` sections
    to ``path`` atomically.

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
        if c.publish_zone_events is not True:
            entry["publish_zone_events"] = c.publish_zone_events
        if c.publish_detections is not False:
            entry["publish_detections"] = c.publish_detections
        if c.detection_publish_interval_sec != 5.0:
            entry["detection_publish_interval_sec"] = c.detection_publish_interval_sec
        entries.append(entry)

    doc: dict = {"cameras": entries}
    if mqtt is not None:
        doc["mqtt"] = {
            "host": mqtt.host,
            "port": mqtt.port,
            "topic_prefix": mqtt.topic_prefix,
            **({"username": mqtt.username} if mqtt.username else {}),
            **({"password": mqtt.password} if mqtt.password else {}),
            "enabled": mqtt.enabled,
        }
    if http_output is not None:
        doc["http_output"] = {
            "url": http_output.url,
            "timeout_sec": http_output.timeout_sec,
            "enabled": http_output.enabled,
        }
    if snapshots is not None:
        doc["snapshots"] = {
            "base_dir": snapshots.base_dir,
            "max_per_camera": snapshots.max_per_camera,
        }
    if clips is not None:
        doc["clips"] = {
            "base_dir": clips.base_dir,
            "max_per_camera": clips.max_per_camera,
            "duration_sec": clips.duration_sec,
        }
    text = (
        "# Generated by the NVR control panel: name/url/zones and MQTT/HTTP\n"
        "# output edits made in the UI are persisted here (hand edits are\n"
        "# honored on load, but replaced on the next save).\n"
        + yaml.safe_dump(doc, sort_keys=False)
    )
    tmp = f"{path}.tmp"
    Path(tmp).write_text(text, encoding="utf-8")
    try:
        load_config(tmp)  # round-trip validation before replacing
    except ConfigError:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
