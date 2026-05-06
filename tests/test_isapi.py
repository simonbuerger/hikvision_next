"""Tests for specific ISAPI responses."""

from contextlib import suppress
from datetime import UTC, datetime

import httpx
import respx
from custom_components.hikvision_next.isapi import StorageInfo
from tests.conftest import mock_endpoint, load_fixture


@respx.mock
async def test_storage(mock_isapi):
    isapi = mock_isapi

    mock_endpoint("ContentMgmt/Storage", "hdd1")
    storage_list = await isapi.get_storage_devices()
    assert len(storage_list) == 1
    assert storage_list[0] == StorageInfo(
        id=1,
        name="hdd1",
        type="SATA",
        status="ok",
        capacity=1907729,
        freespace=0,
        property="RW",
        ip="",
    )

    mock_endpoint("ContentMgmt/Storage", "hdd1_nas1")
    storage_list = await isapi.get_storage_devices()
    assert len(storage_list) == 2
    assert storage_list[0].type == "SATA"
    assert storage_list[1].type == "NFS"
    assert storage_list[1].ip != ""

    mock_endpoint("ContentMgmt/Storage", status_code=500)
    with suppress(Exception):
        storage_list = await isapi.get_storage_devices()
        assert len(storage_list) == 0


@respx.mock
async def test_notification_hosts(mock_isapi):
    isapi = mock_isapi

    mock_endpoint("Event/notification/httpHosts", "nvr_single_item")
    host_nvr = await isapi.get_alarm_server()

    mock_endpoint("Event/notification/httpHosts", "ipc_list")
    host_ipc = await isapi.get_alarm_server()

    assert host_nvr == host_ipc


@respx.mock
async def test_update_notification_hosts(mock_isapi):
    isapi = mock_isapi

    def update_side_effect(request, route):
        payload = load_fixture("ISAPI/Event.notification.httpHosts", "set_alarm_server_payload")
        if request.content.decode("utf-8") != payload:
            raise AssertionError("Request content does not match expected payload")
        return httpx.Response(200)

    mock_endpoint("Event/notification/httpHosts", "nvr_single_item")
    url = f"{isapi.host}/ISAPI/Event/notification/httpHosts"
    endpoint = respx.put(url).mock(side_effect=update_side_effect)
    await isapi.set_alarm_server("http://1.0.0.11:8123", "/api/hikvision")

    assert endpoint.called


@respx.mock
async def test_update_notification_hosts_from_ipaddress_to_hostname(mock_isapi):
    isapi = mock_isapi

    def update_side_effect(request, route):
        payload = load_fixture("ISAPI/Event.notification.httpHosts", "set_alarm_server_outside_network_payload")
        if request.content.decode("utf-8") != payload:
            raise AssertionError("Request content does not match expected payload")
        return httpx.Response(200)

    mock_endpoint("Event/notification/httpHosts", "nvr_single_item")
    url = f"{isapi.host}/ISAPI/Event/notification/httpHosts"
    endpoint = respx.put(url).mock(side_effect=update_side_effect)
    await isapi.set_alarm_server("https://ha.hostname.domain", "/api/hikvision")

    assert endpoint.called


@respx.mock
async def test_recording_download_capabilities(mock_isapi):
    """Test parsing recording download capabilities."""
    isapi = mock_isapi

    mock_endpoint("ContentMgmt/download/capabilities", "supported")
    ability = await isapi.get_download_capabilities()

    assert ability.by_time is True
    assert ability.by_file_name is True
    assert ability.to_usb is False


@respx.mock
async def test_search_recordings(mock_isapi):
    """Test recording search payload and parser."""
    isapi = mock_isapi

    def search_side_effect(request, route):
        payload = request.content.decode("utf-8")
        assert "<CMSearchDescription" in payload
        assert "<trackID>101</trackID>" in payload
        assert "<startTime>2026-05-06T00:00:00Z</startTime>" in payload
        return httpx.Response(200, text=load_fixture("ISAPI/ContentMgmt.search", "two_recordings"))

    endpoint = respx.post(f"{isapi.host}/ISAPI/ContentMgmt/search").mock(side_effect=search_side_effect)

    recordings = await isapi.search_recordings(
        1,
        datetime(2026, 5, 6, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 6, 23, 59, tzinfo=UTC),
    )

    assert endpoint.called
    assert len(recordings) == 2
    assert recordings[0].camera_id == 1
    assert recordings[0].track_id == 101
    assert recordings[0].name == "clip-one.mp4"
    assert recordings[0].size == 1234
    assert recordings[0].content_type == "video/mp4"


@respx.mock
async def test_download_recording(mock_isapi):
    """Test recording download request."""
    isapi = mock_isapi
    playback_uri = "rtsp://1.0.0.255/Streaming/tracks/101?name=clip-one.mp4"

    def download_side_effect(request, route):
        payload = request.content.decode("utf-8")
        assert "<downloadRequest" in payload
        assert "<playbackURI>rtsp://1.0.0.255/Streaming/tracks/101?name=clip-one.mp4</playbackURI>" in payload
        return httpx.Response(200, content=b"video bytes")

    endpoint = respx.post(f"{isapi.host}/ISAPI/ContentMgmt/download").mock(side_effect=download_side_effect)

    data = b"".join([chunk async for chunk in isapi.download_recording(playback_uri)])

    assert endpoint.called
    assert data == b"video bytes"
