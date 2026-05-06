"""Media source support for Hikvision recordings."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import asdict
from datetime import datetime, time, timedelta
import json
import logging
import secrets
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player.const import MediaClass
from homeassistant.components.media_source import (
    BrowseError,
    BrowseMediaSource,
    MediaSource,
    MediaSourceError,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .hikvision_device import HikvisionDevice

_LOGGER = logging.getLogger(__name__)

DATA_RECORDING_CACHE = f"{DOMAIN}_recording_cache"
DATA_VIEW_REGISTERED = f"{DOMAIN}_recording_view_registered"
RECORDING_DOWNLOAD_PATH = f"/api/{DOMAIN}/recording"
SIGNED_URL_LIFETIME = timedelta(hours=2)
RECORDING_CACHE_LIFETIME = timedelta(hours=2)
DATE_BUCKETS = (
    ("today", "Today", 0),
    ("yesterday", "Yesterday", 1),
    ("two_days_ago", "Two days ago", 2),
    ("three_days_ago", "Three days ago", 3),
    ("last_7_days", "Last 7 days", 6),
)


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up Hikvision recordings as a media source."""
    _async_register_download_view(hass)
    return HikvisionRecordingMediaSource(hass)


def _async_register_download_view(hass: HomeAssistant) -> None:
    """Register the recording download view once."""
    if hass.data.get(DATA_VIEW_REGISTERED):
        return
    hass.http.register_view(HikvisionRecordingDownloadView(hass))
    hass.data[DATA_VIEW_REGISTERED] = True


class HikvisionRecordingMediaSource(MediaSource):
    """Provide Hikvision camera and NVR recordings as a media source."""

    name = "Hikvision Recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem | None) -> BrowseMediaSource:
        """Browse Hikvision recordings."""
        identifier = item.identifier if item else ""
        if not identifier:
            return self._build_root()

        entry_id, path = self._split_entry_path(identifier)
        device = self._get_device(entry_id)

        if not path:
            return self._build_camera_list(entry_id, device)

        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "camera":
            camera_id = self._parse_camera_id(parts[1])
            return self._build_date_buckets(entry_id, device, camera_id)

        if len(parts) == 4 and parts[0] == "camera" and parts[2] == "range":
            camera_id = self._parse_camera_id(parts[1])
            return await self._build_recording_list(entry_id, device, camera_id, parts[3])

        raise BrowseError(f"Unknown Hikvision media source item: {identifier}")

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a recording to a signed Home Assistant download URL."""
        entry_id, path = self._split_entry_path(item.identifier)
        self._get_device(entry_id)
        parts = path.split("/", 1)
        if len(parts) != 2 or parts[0] != "recording":
            raise Unresolvable("Only recording items can be resolved")

        recording = _decode_recording(parts[1])
        token = secrets.token_urlsafe(24)
        cache = self.hass.data.setdefault(DATA_RECORDING_CACHE, {})
        self._cleanup_cache(cache)
        cache[token] = {
            "entry_id": entry_id,
            "playback_uri": recording["playback_uri"],
            "content_type": recording.get("content_type", "video/mp4"),
            "title": recording.get("title", "recording"),
            "expires": dt_util.utcnow() + RECORDING_CACHE_LIFETIME,
        }
        path = f"{RECORDING_DOWNLOAD_PATH}/{token}"
        return PlayMedia(async_sign_path(self.hass, path, SIGNED_URL_LIFETIME), recording.get("content_type", "video/mp4"))

    def _build_root(self) -> BrowseMediaSource:
        """Build the root item with loaded Hikvision entries."""
        children = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.disabled_by or not getattr(entry, "runtime_data", None):
                continue
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=entry.entry_id,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="hikvision_entry",
                    title=entry.title,
                    can_play=False,
                    can_expand=True,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.APP,
            media_content_type="hikvision_root",
            title=self.name,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _build_camera_list(self, entry_id: str, device: HikvisionDevice) -> BrowseMediaSource:
        """Build a list of cameras for one device."""
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/camera/{camera.id}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="hikvision_camera",
                title=camera.name,
                can_play=False,
                can_expand=True,
            )
            for camera in device.cameras
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=entry_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="hikvision_entry",
            title=device.device_info.name or "Hikvision",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _build_date_buckets(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
    ) -> BrowseMediaSource:
        """Build fixed date buckets for one camera."""
        camera = device.get_camera_by_id(camera_id)
        if not camera:
            raise BrowseError(f"Camera {camera_id} is not available")

        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/camera/{camera_id}/range/{key}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="hikvision_recording_range",
                title=title,
                can_play=False,
                can_expand=True,
            )
            for key, title, _days_back in DATE_BUCKETS
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry_id}/camera/{camera_id}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="hikvision_camera",
            title=camera.name,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    async def _build_recording_list(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
        bucket_key: str,
    ) -> BrowseMediaSource:
        """Build playable recording clips for a camera and date bucket."""
        camera = device.get_camera_by_id(camera_id)
        if not camera:
            raise BrowseError(f"Camera {camera_id} is not available")

        start_time, end_time, title = _bucket_range(bucket_key)
        try:
            recordings = await device.search_recordings(camera_id, start_time, end_time)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Cannot search Hikvision recordings: %s", err)
            raise MediaSourceError(str(err)) from err

        children = []
        for recording in recordings:
            recording_title = _recording_title(recording.start_time, recording.end_time, recording.name)
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}/recording/{_encode_recording(recording, recording_title)}",
                    media_class=MediaClass.VIDEO,
                    media_content_type=recording.content_type,
                    title=recording_title,
                    can_play=True,
                    can_expand=False,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry_id}/camera/{camera_id}/range/{bucket_key}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="hikvision_recording_range",
            title=f"{camera.name} - {title}",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    def _get_device(self, entry_id: str) -> HikvisionDevice:
        """Get the loaded Hikvision device for an entry."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            raise MediaSourceError(f"Hikvision entry {entry_id} is not loaded")
        return entry.runtime_data

    @staticmethod
    def _split_entry_path(identifier: str) -> tuple[str, str]:
        """Split a media source identifier into entry id and path."""
        parts = identifier.split("/", 1)
        return parts[0], parts[1] if len(parts) == 2 else ""

    @staticmethod
    def _parse_camera_id(value: str) -> int:
        """Parse a camera id."""
        try:
            return int(value)
        except ValueError as err:
            raise BrowseError(f"Invalid camera id: {value}") from err

    @staticmethod
    def _cleanup_cache(cache: dict[str, dict[str, Any]]) -> None:
        """Remove expired recording download tokens."""
        now = dt_util.utcnow()
        for token, item in list(cache.items()):
            if item["expires"] <= now:
                cache.pop(token, None)


class HikvisionRecordingDownloadView(HomeAssistantView):
    """Proxy recording downloads through Home Assistant."""

    requires_auth = True
    name = f"api:{DOMAIN}:recording"
    url = f"{RECORDING_DOWNLOAD_PATH}/{{token}}"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        """Stream a recording download."""
        cache = self.hass.data.setdefault(DATA_RECORDING_CACHE, {})
        HikvisionRecordingMediaSource._cleanup_cache(cache)
        recording = cache.get(token)
        if not recording:
            raise web.HTTPNotFound(text="Recording token expired or not found")

        entry = self.hass.config_entries.async_get_entry(recording["entry_id"])
        if not entry or not getattr(entry, "runtime_data", None):
            raise web.HTTPNotFound(text="Hikvision entry is not loaded")

        device: HikvisionDevice = entry.runtime_data
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": recording["content_type"],
                "Content-Disposition": f'inline; filename="{recording["title"]}.mp4"',
            },
        )
        await response.prepare(request)
        async for chunk in device.download_recording(recording["playback_uri"]):
            await response.write(chunk)
        await response.write_eof()
        return response


def _bucket_range(bucket_key: str) -> tuple[datetime, datetime, str]:
    """Return UTC start/end datetimes and title for a date bucket."""
    now = dt_util.now()
    for key, title, days_back in DATE_BUCKETS:
        if bucket_key != key:
            continue
        if key == "last_7_days":
            start_date = (now - timedelta(days=days_back)).date()
            start_local = datetime.combine(start_date, time.min, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return start_local.astimezone(dt_util.UTC), now.astimezone(dt_util.UTC), title
        target_date = (now - timedelta(days=days_back)).date()
        start_local = datetime.combine(target_date, time.min, tzinfo=dt_util.DEFAULT_TIME_ZONE)
        end_local = datetime.combine(target_date, time.max, tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return start_local.astimezone(dt_util.UTC), end_local.astimezone(dt_util.UTC), title
    raise BrowseError(f"Unknown recording range: {bucket_key}")


def _recording_title(start_time: datetime, end_time: datetime, name: str) -> str:
    """Build a readable recording title."""
    start = dt_util.as_local(start_time).strftime("%H:%M:%S")
    end = dt_util.as_local(end_time).strftime("%H:%M:%S")
    if name:
        return f"{start} - {end} ({name})"
    return f"{start} - {end}"


def _encode_recording(recording, title: str) -> str:
    """Encode recording data into a media source-safe token."""
    data = asdict(recording)
    data["start_time"] = recording.start_time.isoformat()
    data["end_time"] = recording.end_time.isoformat()
    data["title"] = title
    return urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _decode_recording(value: str) -> dict[str, Any]:
    """Decode a recording media source token."""
    padding = "=" * (-len(value) % 4)
    try:
        return json.loads(urlsafe_b64decode(f"{value}{padding}"))
    except (ValueError, json.JSONDecodeError) as err:
        raise Unresolvable("Invalid recording item") from err
