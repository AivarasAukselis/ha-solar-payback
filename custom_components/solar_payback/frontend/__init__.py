"""Serve and auto-load the dashboard card that ships with the integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "solar-payback-card.js"
CARD_URL_BASE = f"/{DOMAIN}_frontend"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card JS and add it to every dashboard (once per HA run)."""
    if hass.data.get(_REGISTERED):
        return
    if hass.http is None or "frontend" not in hass.config.components:
        # e.g. tests or a headless setup: nothing to serve to.
        return

    # Imported here so the integration still loads where these aren't set up.
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    path = Path(__file__).parent / CARD_FILENAME
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"{CARD_URL_BASE}/{CARD_FILENAME}", str(path), cache_headers=False)]
    )
    # The version in the URL makes browsers fetch the new card after an update.
    add_extra_js_url(hass, f"{CARD_URL_BASE}/{CARD_FILENAME}?v={integration.version}")
    hass.data[_REGISTERED] = True
    _LOGGER.debug("Registered dashboard card %s", CARD_FILENAME)
