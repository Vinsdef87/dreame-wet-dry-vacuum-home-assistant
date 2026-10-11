"""Diagnostics for Dreame wet & dry vacuum.

Besides the usual snapshot, this dump doubles as a property-discovery tool:
it lists every (siid, piid) received over MQTT (mapped or not) and scans the
cloud-cached status endpoint over a wide siid/piid grid, so unmapped or
"unreadable" properties can be identified without packet captures.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import DreameWetDryConfigEntry
from .api import DreameAPIError
from .const import (
    KNOWN_BINARY_PROPS,
    KNOWN_BUTTON_PROPS,
    KNOWN_MQTT_PROPS,
    KNOWN_NUMBER_PROPS,
    KNOWN_SELECT_PROPS,
    KNOWN_SWITCH_PROPS,
)

TO_REDACT = {
    CONF_USERNAME, CONF_PASSWORD, "mac", "did", "device_id", "uid", "sn", "localip", "localIp",
    "ip", "key", "token", "secret", "bindKey", "masterUid", "masterName", "mobile", "email",
}

# Scan grid for the cloud status endpoint. 30 x 80 = 2400 keys, sent in chunks.
SCAN_SIIDS = range(1, 31)
SCAN_PIIDS = range(1, 81)
SCAN_CHUNK = 200

_ALL_KNOWN: dict[tuple[int, int], str] = {}
for _table, _kind in (
    (KNOWN_MQTT_PROPS, "sensor"),
    (KNOWN_BINARY_PROPS, "binary_sensor"),
    (KNOWN_SWITCH_PROPS, "switch"),
    (KNOWN_NUMBER_PROPS, "number"),
    (KNOWN_SELECT_PROPS, "select"),
    (KNOWN_BUTTON_PROPS, "button"),
):
    for _k, _meta in _table.items():
        _ALL_KNOWN.setdefault(_k, f"{_kind}:{_meta['key']}")


def _label(key: tuple[int, int]) -> str:
    return _ALL_KNOWN.get(key, "UNMAPPED")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DreameWetDryConfigEntry
) -> dict[str, Any]:
    """Return diagnostics plus a property-discovery scan."""
    coordinator = entry.runtime_data

    # 1. Everything currently held in memory (MQTT push + HTTP poll)
    live = {
        f"{s}.{p}": {"value": v, "mapped_as": _label((s, p))}
        for (s, p), v in sorted(coordinator.props.items())
    }

    # 2. Wide scan of the cloud-cached status endpoint
    keys = [f"{s}.{p}" for s in SCAN_SIIDS for p in SCAN_PIIDS]
    scan: dict[str, Any] = {}
    scan_errors: list[str] = []
    for i in range(0, len(keys), SCAN_CHUNK):
        try:
            found = await coordinator.api.get_status_props(
                coordinator.device_id, keys[i : i + SCAN_CHUNK]
            )
        except (DreameAPIError, ValueError) as err:
            scan_errors.append(f"chunk {i}: {err}")
            continue
        for k, v in found.items():
            s, _, p = k.partition(".")
            scan[k] = {"value": v, "mapped_as": _label((int(s), int(p)))}

    # 3. Realtime RPC on the writable props whose state is not published
    optimistic = [
        {"siid": s, "piid": p}
        for table in (KNOWN_SWITCH_PROPS, KNOWN_NUMBER_PROPS, KNOWN_SELECT_PROPS)
        for (s, p), meta in table.items()
        if meta.get("optimistic")
    ]
    try:
        rpc = await coordinator.api.get_properties(coordinator.device_id, optimistic, attempts=1)
    except DreameAPIError as err:
        rpc = [{"error": str(err)}]

    # 4. App plugin probe: heavy (8 MB download), run it from the
    #    plugin_probe module on demand instead of on every diagnostics call.
    probe = {"skipped": True}

    # 5. Realtime RPC read (app style) of siid 1 + 16 + 17: finds settings the
    #    cloud cache never publishes (self-clean mode 1.8, high-level mode 1.6...)
    rpc_scan: list[Any] = []
    candidates = [
        (1, 5), (1, 6), (1, 8), (1, 9), (1, 10), (1, 13), (1, 15), (1, 16), (1, 17),
        (16, 3), (16, 5), (16, 8), (17, 1), (17, 2), (17, 3), (17, 8),
    ]
    try:
        rpc_scan = await coordinator.api.get_properties(
            coordinator.device_id,
            [{"siid": s, "piid": p} for s, p in candidates],
            attempts=2,
        )
    except DreameAPIError as err:
        rpc_scan = [{"error": str(err)}]

    unmapped_scan = sorted(k for k, v in scan.items() if v["mapped_as"] == "UNMAPPED")
    unmapped_live = sorted(k for k, v in live.items() if v["mapped_as"] == "UNMAPPED")

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "snapshot": async_redact_data(coordinator.snapshot, TO_REDACT),
        "model": coordinator.device_info_raw.get("model"),
        "mqtt_connected": bool(coordinator.mqtt),
        "iot_prefix": coordinator.api.iot_prefix,
        "summary": {
            "live_props": len(live),
            "scan_hits": len(scan),
            "unmapped_in_scan": unmapped_scan,
            "unmapped_live": unmapped_live,
        },
        "live_props": live,
        "status_scan": scan,
        "status_scan_errors": scan_errors,
        "rpc_get_properties_optimistic": rpc,
        "rpc_scan": rpc_scan,
        "device_info_raw": async_redact_data(coordinator.device_info_raw, TO_REDACT),
        "plugin_probe": async_redact_data(probe, TO_REDACT),
    }
