"""Tests for Hikvision recording media source."""

import pytest
import respx

from homeassistant.components.media_source import MediaSourceItem
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_next.const import DOMAIN
from custom_components.hikvision_next.media_source import async_get_media_source
from tests.conftest import TEST_HOST, load_fixture


@pytest.mark.parametrize("init_integration", ["DS-7608NXI-I2"], indirect=True)
async def test_recording_media_source_browse_root_and_cameras(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test browsing entries and cameras."""
    source = await async_get_media_source(hass)

    root = await source.async_browse_media(None)
    assert root.title == "Hikvision Recordings"
    assert len(root.children) == 1
    assert root.children[0].identifier == init_integration.entry_id

    item = MediaSourceItem(hass, DOMAIN, init_integration.entry_id, None)
    cameras = await source.async_browse_media(item)
    assert cameras.title == "nvr"
    assert len(cameras.children) == 4
    assert cameras.children[0].identifier == f"{init_integration.entry_id}/camera/1"


@respx.mock
@pytest.mark.parametrize("init_integration", ["DS-7608NXI-I2"], indirect=True)
async def test_recording_media_source_search_and_resolve(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test browsing recording search results and resolving a signed URL."""
    source = await async_get_media_source(hass)

    respx.post(f"{TEST_HOST}/ISAPI/ContentMgmt/search").respond(
        text=load_fixture("ISAPI/ContentMgmt.search", "two_recordings")
    )

    item = MediaSourceItem(hass, DOMAIN, f"{init_integration.entry_id}/camera/1/range/today", None)
    recordings = await source.async_browse_media(item)

    assert len(recordings.children) == 2
    assert recordings.children[0].can_play is True
    assert "clip-one.mp4" in recordings.children[0].title

    media_item = MediaSourceItem(
        hass,
        DOMAIN,
        recordings.children[0].identifier,
        None,
    )
    play_media = await source.async_resolve_media(media_item)

    assert play_media.mime_type == "video/mp4"
    assert play_media.url.startswith(f"/api/{DOMAIN}/recording/")
    assert "authSig=" in play_media.url

