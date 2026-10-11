"""Read-only probe of the Dreamehome app plugin for this model.

The Dreame app downloads a per-model React Native plugin. Writes made from the
app succeed while our cloud sendCommand times out (code 80001), so the plugin is
the reference for the real command path. This probe asks the Dreame cloud for
the plugin package (same request the app makes), downloads it in memory and
extracts the API paths and the code around property writes.

Nothing is written to the device and nothing is stored on disk.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any

import aiohttp

from .api import DreameAPI, DreameAPIError

_LOGGER = logging.getLogger(__name__)

APPPLUGIN_PATH = "/dreame-product/upgrades/appplugin"
APP_VERSIONS = (160, 155, 150, 148)
MAX_DOWNLOAD = 40 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024

# Quoted API-looking string literals
_PATH_RE = re.compile(r"""["'`](/?dreame-[a-zA-Z0-9_\-]+/[^"'`\s]{2,120})["'`]""")
_KEYWORDS = (
    "sendCommand",
    "set_properties",
    "setProperties",
    "setProps",
    "devOTC",
    "OTC",
    "callMethod",
    "sendAction",
    "iotstatus",
    "iotuserdata",
    "publish",
)
_SNIPPET = 350
_MAX_SNIPPETS_PER_KEYWORD = 6


async def _authed_get(api: DreameAPI, url: str, params: dict[str, Any]) -> dict[str, Any]:
    session = await api._get_session()  # noqa: SLF001 - internal probe
    async with session.get(
        url, headers=api._get_auth_headers(), params=params, timeout=aiohttp.ClientTimeout(total=30)  # noqa: SLF001
    ) as resp:
        body = await resp.json(content_type=None)
        return {"http_status": resp.status, "body": body}


async def _get_plugin_meta(api: DreameAPI, model: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    url = api._base_url + APPPLUGIN_PATH  # noqa: SLF001
    attempts: dict[str, Any] = {}
    for ver in APP_VERSIONS:
        try:
            res = await _authed_get(api, url, {"model": model, "appVer": ver, "os": 1})
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            attempts[str(ver)] = {"error": str(err)}
            continue
        attempts[str(ver)] = res
        data = (res.get("body") or {}).get("data") if isinstance(res.get("body"), dict) else None
        if isinstance(data, dict) and data.get("url"):
            return data, attempts
    return None, attempts


async def _download(api: DreameAPI, url: str) -> bytes:
    session = await api._get_session()  # noqa: SLF001
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        resp.raise_for_status()
        buf = io.BytesIO()
        async for chunk in resp.content.iter_chunked(65536):
            buf.write(chunk)
            if buf.tell() > MAX_DOWNLOAD:
                raise ValueError("plugin package too large")
        return buf.getvalue()


def _analyse(blob: bytes) -> dict[str, Any]:
    """Runs in an executor: unzip and grep (CPU bound)."""
    out: dict[str, Any] = {"files": [], "paths": [], "snippets": {}}
    paths: set[str] = set()
    snippets: dict[str, list[str]] = {k: [] for k in _KEYWORDS}
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        out["error"] = "not a zip"
        return out
    for info in zf.infolist():
        out["files"].append(f"{info.filename} ({info.file_size})")
        if info.is_dir() or info.file_size > MAX_FILE:
            continue
        if not info.filename.endswith((".js", ".bundle", ".jsbundle", ".json", ".txt", ".hbc")) and "bundle" not in info.filename:
            continue
        text = zf.read(info).decode("utf-8", errors="ignore")
        paths.update(m.group(1) for m in _PATH_RE.finditer(text))
        for kw in _KEYWORDS:
            if len(snippets[kw]) >= _MAX_SNIPPETS_PER_KEYWORD:
                continue
            start = 0
            while len(snippets[kw]) < _MAX_SNIPPETS_PER_KEYWORD:
                i = text.find(kw, start)
                if i < 0:
                    break
                snippets[kw].append(
                    f"[{info.filename}@{i}] " + text[max(0, i - _SNIPPET): i + _SNIPPET]
                )
                start = i + len(kw) + _SNIPPET
    out["files"] = out["files"][:200]
    out["paths"] = sorted(paths)
    out["snippets"] = {k: v for k, v in snippets.items() if v}
    return out


async def async_probe_plugin(hass, api: DreameAPI, model: str, device_id: str) -> dict[str, Any]:
    """Return plugin metadata, extracted API paths and write-related snippets."""
    result: dict[str, Any] = {"model": model}

    # Read-only device endpoints the app calls (may reveal the command channel)
    for name, path in (
        ("device_info", "/dreame-user-iot/iotuserbind/device/info"),
        ("dev_otc_info", "/dreame-user-iot/iotstatus/devOTCInfo"),
    ):
        try:
            result[name] = await api._authed_post(api._base_url + path, {"did": str(device_id)})  # noqa: SLF001
        except DreameAPIError as err:
            result[name] = {"error": str(err)}

    meta, attempts = await _get_plugin_meta(api, model)
    result["plugin_meta"] = meta
    if meta is None:
        result["plugin_attempts"] = attempts
        return result

    try:
        blob = await _download(api, meta["url"])
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        result["download_error"] = str(err)
        return result
    result["plugin_bytes"] = len(blob)
    result["analysis"] = await hass.async_add_executor_job(_analyse, blob)
    return result
