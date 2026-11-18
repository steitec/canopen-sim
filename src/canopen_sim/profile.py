"""Load a device profile (YAML or dict) and build a wired :class:`~canopen_sim.node.Node`.

A profile names the identity (0x1000, 0x1008, 0x1018), a behaviour model, the
heartbeat time, the default PDO layout and optional manufacturer objects. The
builder adds the objects in the order the node needs them: communication
objects, PDO objects, model objects, extra objects, then ``Node(...)``, which
binds the model and takes the defaults snapshot used by reset and restore.

Profile keys::

    name, description, sources, limits     # texts
    identity: {device_type, vendor_id, product_code, revision, serial, device_name}
    model: {type: io|encoder|drive, ...constructor parameters}
    heartbeat_ms: 1000
    tpdos / rpdos: [{number: 0..3, mapping: ["0x6041:0/16", ...],
                     transmission_type, event_timer_ms, inhibit_100us, enabled}]
    objects: [{index, sub, name, type, access, default, low, high, pdo}]

A dict (or YAML file) may name a base with ``profile:`` and override parts of
it; ``identity`` and ``model`` merge one level deep, everything else replaces.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from .comm_objects import PdoSpec, add_communication_objects, add_pdo_objects
from .models.base import Model
from .models.drive import DriveModel
from .models.encoder import EncoderModel
from .models.io import IoModel
from .node import Node
from .od import DataType, ObjectDictionary, SdoAbort, Variable, encode
from .signals import Signal, Source

_PROFILE_DIR = Path(__file__).with_name("profiles")

_KEYS = {"profile", "name", "description", "identity", "model", "heartbeat_ms",
         "tpdos", "rpdos", "objects", "sources", "limits"}
_IDENTITY_KEYS = {"device_type", "vendor_id", "product_code", "revision", "serial",
                  "device_name"}
_PDO_KEYS = {"number", "mapping", "transmission_type", "event_timer_ms",
             "inhibit_100us", "enabled"}
_OBJECT_KEYS = {"index", "sub", "name", "type", "access", "default", "low", "high", "pdo"}
_MODELS = {"io": IoModel, "encoder": EncoderModel, "drive": DriveModel}
_SIGNAL_PARAMS = {"constant": ("value",), "ramp": ("start", "stop", "period"),
                  "sine": ("offset", "amplitude", "period"), "noise": ("sigma",)}
_MAPPING = re.compile(r"^\s*(0x[0-9a-fA-F]+|\d+)\s*:\s*(0x[0-9a-fA-F]+|\d+)\s*/\s*(\d+)\s*$")
_STRINGS = (DataType.VISIBLE_STRING, DataType.OCTET_STRING)


class ProfileError(ValueError):
    """An unknown profile or an invalid profile description."""


def bundled_profiles() -> list[str]:
    return sorted(p.stem for p in _PROFILE_DIR.glob("*.yaml"))


def load_profile(source: str | Path | dict) -> dict[str, Any]:
    """Return the profile dict for a bundled name, a YAML path, or a dict."""
    if isinstance(source, dict):
        return source
    name = str(source)
    if name in bundled_profiles():
        path = _PROFILE_DIR / f"{name}.yaml"
    else:
        path = Path(source)
        if not path.is_file():
            raise ProfileError(
                f"unknown profile {name!r}; bundled: {', '.join(bundled_profiles())}")
    try:
        with path.open(encoding="utf-8") as handle:
            spec = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise ProfileError(f"cannot read profile {path}: {error}") from None
    if not isinstance(spec, dict):
        raise ProfileError(f"profile {path} is not a mapping")
    return spec


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge, one level deep for the nested identity/model mappings."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def resolve_profile(source: str | Path | dict) -> dict[str, Any]:
    """Load a profile and apply a ``profile:`` base (recursively)."""
    spec = load_profile(source)
    seen: list[str] = []
    while "profile" in spec:
        base = spec["profile"]
        if str(base) in seen:
            raise ProfileError(f"profile {base!r} refers to itself")
        seen.append(str(base))
        overrides = {k: v for k, v in spec.items() if k != "profile"}
        spec = _merge(load_profile(base), overrides)
    unknown = set(spec) - _KEYS
    if unknown:
        raise ProfileError(f"unknown profile key(s) {', '.join(sorted(unknown))}")
    return spec


# -- helpers --------------------------------------------------------------


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{what} must be an integer, not {value!r}")
    return value


def _mapping(text: Any) -> tuple[int, int, int]:
    match = _MAPPING.match(text) if isinstance(text, str) else None
    if match is None:
        raise ProfileError(f"bad PDO mapping entry {text!r}; expected 'INDEX:SUB/BITS' "
                           "such as '0x6041:0/16'")
    index, sub, bits = (int(g, 0) for g in match.groups())
    if not 0 <= index <= 0xFFFF or not 0 <= sub <= 0xFF or not 1 <= bits <= 64:
        raise ProfileError(f"PDO mapping entry {text!r} out of range")
    return index, sub, bits


def _pdo_specs(entries: Any, what: str) -> list[PdoSpec]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ProfileError(f"{what} must be a list")
    specs: list[PdoSpec] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProfileError(f"{what}: each PDO must be a mapping")
        unknown = set(entry) - _PDO_KEYS
        if unknown:
            raise ProfileError(f"{what}: unknown key(s) {', '.join(sorted(unknown))}")
        number = _int(entry.get("number"), f"{what} number")
        if not 0 <= number <= 3:
            raise ProfileError(f"{what}: number must be 0..3 (0 = PDO1), not {number}")
        if any(s.number == number for s in specs):
            raise ProfileError(f"{what}: PDO number {number} listed twice")
        mapping = entry.get("mapping", [])
        if not isinstance(mapping, list):
            raise ProfileError(f"{what}: mapping must be a list")
        if len(mapping) > 8:
            raise ProfileError(f"{what}: at most 8 mapped objects per PDO")
        spec = PdoSpec(number=number, mapping=[_mapping(m) for m in mapping])
        for key, limit in (("transmission_type", 0xFF), ("event_timer_ms", 0xFFFF),
                           ("inhibit_100us", 0xFFFF)):
            if key in entry:
                value = _int(entry[key], f"{what} {key}")
                if not 0 <= value <= limit:
                    raise ProfileError(f"{what}: {key} must be 0..{limit}")
                setattr(spec, key, value)
        if "enabled" in entry:
            if not isinstance(entry["enabled"], bool):
                raise ProfileError(f"{what}: enabled must be true or false")
            spec.enabled = entry["enabled"]
        specs.append(spec)
    return specs


def _check_mapping(od: ObjectDictionary, spec: PdoSpec, what: str, receive: bool) -> None:
    """The same rules the SDO mapping write enforces, plus writability for RPDOs."""
    total = 0
    for index, sub, bits in spec.mapping:
        label = f"{what} {spec.number + 1}: 0x{index:04X}:{sub}"
        try:
            var = od.entry(index, sub)
        except SdoAbort:
            raise ProfileError(f"{label} does not exist") from None
        if not var.pdo_mappable:
            raise ProfileError(f"{label} is not PDO-mappable")
        if receive and var.access not in ("rw", "wo"):
            raise ProfileError(f"{label} is not writable, so an RPDO cannot map it")
        if var.dtype in _STRINGS:
            limit = 64
            if bits % 8:
                raise ProfileError(f"{label}: string widths must be whole bytes")
        else:
            limit = 8 * len(encode(var.dtype, 0))
        if bits > limit:
            raise ProfileError(f"{label}: {bits} bits exceed the {limit}-bit data type")
        total += bits
    if total > 64:
        raise ProfileError(f"{what} {spec.number + 1}: {total} mapped bits exceed 64")


def _signals(raw: Any) -> dict[str, Signal]:
    if not isinstance(raw, dict):
        raise ProfileError("model signals must map channel names (di1, ai1, ...) to sources")
    out: dict[str, Signal] = {}
    for channel, value in raw.items():
        seed = None
        if isinstance(value, dict):
            seed = value.get("seed")
            value = value.get("sources")
        if not isinstance(value, list) or not value:
            raise ProfileError(f"signal {channel!r}: give a list of sources")
        sources = []
        for src in value:
            if not isinstance(src, dict) or src.get("type") not in _SIGNAL_PARAMS:
                raise ProfileError(f"signal {channel!r}: each source needs a type out of "
                                   f"{', '.join(_SIGNAL_PARAMS)}")
            params = {k: v for k, v in src.items() if k != "type"}
            needed = set(_SIGNAL_PARAMS[src["type"]])
            if set(params) != needed or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in params.values()):
                raise ProfileError(f"signal {channel!r}: {src['type']} needs numeric "
                                   f"{', '.join(sorted(needed))}")
            if "period" in params and params["period"] <= 0:
                raise ProfileError(f"signal {channel!r}: period must be positive")
            sources.append(Source(src["type"], {k: float(v) for k, v in params.items()}))
        out[str(channel)] = Signal(sources, start=time.monotonic(), seed=seed)
    return out


def _build_model(spec: Any) -> Model | None:
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ProfileError("model must be a mapping with a type")
    params = dict(spec)
    kind = params.pop("type", None)
    cls = _MODELS.get(kind)
    if cls is None:
        raise ProfileError(f"unknown model type {kind!r}; known: {', '.join(_MODELS)}")
    if "signals" in params and cls is IoModel:
        params["signals"] = _signals(params["signals"])
    try:
        return cls(**params)
    except (TypeError, ValueError) as error:
        raise ProfileError(f"bad parameters for model {kind!r}: {error}") from None


def _dtype(name: Any) -> DataType:
    try:
        return DataType[str(name).upper()]
    except KeyError:
        raise ProfileError(f"unknown data type {name!r}; known: "
                           f"{', '.join(t.name for t in DataType)}") from None


def _extra_object(entry: Any, od: ObjectDictionary) -> Variable:
    if not isinstance(entry, dict):
        raise ProfileError("each extra object must be a mapping")
    unknown = set(entry) - _OBJECT_KEYS
    if unknown:
        raise ProfileError(f"object: unknown key(s) {', '.join(sorted(unknown))}")
    index = _int(entry.get("index"), "object index")
    sub = _int(entry.get("sub", 0), "object sub-index")
    label = f"object 0x{index:04X}:{sub}"
    if not 0x2000 <= index <= 0x5FFF:
        raise ProfileError(f"{label}: extra objects must lie in 0x2000-0x5FFF")
    if not 0 <= sub <= 0xFF:
        raise ProfileError(f"{label}: sub-index must be 0..255")
    if (index, sub) in {(v.index, v.sub) for v in od.entries()}:
        raise ProfileError(f"{label} is defined twice")
    dtype = _dtype(entry.get("type"))
    access = entry.get("access", "rw")
    if access not in ("ro", "wo", "rw", "const"):
        raise ProfileError(f"{label}: access must be ro, wo, rw or const")
    default = entry.get("default")
    if dtype == DataType.VISIBLE_STRING:
        default = "" if default is None else default
        if not isinstance(default, str) or not default.isascii():
            raise ProfileError(f"{label}: default must be an ASCII string")
    elif dtype == DataType.OCTET_STRING:
        try:
            default = bytes.fromhex(default) if isinstance(default, str) else \
                bytes(default or b"")
        except (TypeError, ValueError):
            raise ProfileError(f"{label}: default must be a hex string or a byte list") \
                from None
    else:
        default = 0 if default is None else default
        if dtype == DataType.REAL32 and isinstance(default, int):
            default = float(default)
        try:
            encode(dtype, default)
        except Exception:  # struct.error, TypeError: wrong type or out of range
            raise ProfileError(f"{label}: default {default!r} does not fit "
                               f"{dtype.name}") from None
    bounds = {}
    for key in ("low", "high"):
        value = entry.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))):
            raise ProfileError(f"{label}: {key} must be a number")
        bounds[key] = value
    return Variable(index, sub, str(entry.get("name", label)), dtype, access, default,
                    low=bounds["low"], high=bounds["high"],
                    pdo_mappable=bool(entry.get("pdo", False)))


# -- builder --------------------------------------------------------------


def build_node(source: str | Path | dict, *, node_id: int, name: str | None = None) -> Node:
    """Build a fully wired Node from a bundled profile, a YAML path, or a dict."""
    if isinstance(node_id, bool) or not isinstance(node_id, int) or not 1 <= node_id <= 127:
        raise ProfileError(f"node ID must be an integer 1..127, not {node_id!r}")
    spec = resolve_profile(source)
    ident = spec.get("identity") or {}
    if not isinstance(ident, dict):
        raise ProfileError("identity must be a mapping")
    unknown = set(ident) - _IDENTITY_KEYS
    if unknown:
        raise ProfileError(f"identity: unknown key(s) {', '.join(sorted(unknown))}")
    model = _build_model(spec.get("model"))
    numbers = {}
    for key, default in (("device_type", 0), ("vendor_id", 0), ("product_code", 0),
                         ("revision", 0), ("serial", 0)):
        value = _int(ident.get(key, default), f"identity {key}")
        if not 0 <= value <= 0xFFFFFFFF:
            raise ProfileError(f"identity {key} must fit 32 bits")
        numbers[key] = value
    device_name = str(ident.get("device_name", "SIM-DEVICE"))
    heartbeat = _int(spec.get("heartbeat_ms", 1000), "heartbeat_ms")
    if not 0 <= heartbeat <= 0xFFFF:
        raise ProfileError("heartbeat_ms must be 0..65535")

    if "device_type" not in ident and isinstance(model, EncoderModel):
        numbers["device_type"] = 0x00020196 if model.multiturn else 0x00010196
    od = ObjectDictionary()
    add_communication_objects(od, device_name=device_name, heartbeat_ms=heartbeat,
                              **numbers)
    tpdos = _pdo_specs(spec.get("tpdos"), "TPDO")
    rpdos = _pdo_specs(spec.get("rpdos"), "RPDO")
    add_pdo_objects(od, node_id, tpdos=tpdos, rpdos=rpdos)
    if model is not None:
        for var in model.objects():
            od.add(var)
    extra = spec.get("objects") or []
    if not isinstance(extra, list):
        raise ProfileError("objects must be a list")
    for entry in extra:
        od.add(_extra_object(entry, od))
    for specs, what, receive in ((tpdos, "TPDO", False), (rpdos, "RPDO", True)):
        for pdo in specs:
            _check_mapping(od, pdo, what, receive)
    return Node(node_id, od, name or str(spec.get("name", "node")), model=model)
