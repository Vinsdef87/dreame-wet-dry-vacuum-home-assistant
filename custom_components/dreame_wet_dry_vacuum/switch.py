"""Switch platform for Dreame wet & dry vacuum (writable boolean properties)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DreameWetDryConfigEntry
from .const import KNOWN_SWITCH_PROPS
from .entity import DreameWetDryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DreameWetDryConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DreameWetDrySwitch(coordinator, key, meta)
        for key, meta in KNOWN_SWITCH_PROPS.items()
    )


class DreameWetDrySwitch(DreameWetDryEntity, SwitchEntity):
    """A boolean device setting (0/1).

    For `optimistic` props the current state isn't readable from any channel, so
    the entity is marked assumed_state and only reflects the last command sent
    (stored optimistically by the coordinator after a successful write).
    """

    def __init__(self, coordinator, key, meta) -> None:
        super().__init__(coordinator, key, meta)
        if meta.get("optimistic"):
            self._attr_assumed_state = True
        self._inverted = bool(meta.get("inverted"))

    @property
    def is_on(self) -> bool | None:
        raw = self._raw
        if raw is None:
            return None
        try:
            on = int(raw) != 0
        except (ValueError, TypeError):
            on = bool(raw)
        return not on if self._inverted else on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(0 if self._inverted else 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(1 if self._inverted else 0)
