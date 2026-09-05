"""Media source support for Hikvision recordings."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from contextlib import suppress
from datetime import datetime, time, timedelta
from hashlib import sha256
import logging
import re
import secrets
import shutil
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_player.const import MediaClass
from homeassistant.components.media_source import (
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

if TYPE_CHECKING:
    from .hikvision_device import HikvisionDevice

_LOGGER = logging.getLogger(__name__)

DATA_RECORDING_CACHE = f"{DOMAIN}_recording_cache"
DATA_RECORDING_SEQUENCE = f"{DOMAIN}_recording_sequence"
DATA_RECORDING_ITEM_CACHE = f"{DOMAIN}_recording_item_cache"
DATA_BROWSE_CACHE = f"{DOMAIN}_recording_browse_cache"
DATA_DAILY_DISTRIBUTION_CACHE = f"{DOMAIN}_daily_distribution_cache"
DATA_THUMBNAIL_CACHE = f"{DOMAIN}_thumbnail_cache"
DATA_REMUX_SEMAPHORE = f"{DOMAIN}_recording_remux_semaphore"
DATA_ACTIVE_REMUX = f"{DOMAIN}_active_recording_remux"
DATA_THUMBNAIL_SEMAPHORE = f"{DOMAIN}_thumbnail_semaphore"
DATA_VIEW_REGISTERED = f"{DOMAIN}_recording_view_registered"
RECORDING_DOWNLOAD_PATH = f"/api/{DOMAIN}/recording"
THUMBNAIL_PATH = f"/api/{DOMAIN}/thumbnail"
SIGNED_URL_LIFETIME = timedelta(hours=2)
BROWSE_CACHE_LIFETIME = timedelta(minutes=2)
DAILY_DISTRIBUTION_CACHE_LIFETIME = timedelta(minutes=30)
RECORDING_CACHE_LIFETIME = timedelta(hours=2)
THUMBNAIL_CACHE_LIFETIME = timedelta(hours=6)
SNAPSHOT_THUMBNAIL_IMAGE_LIFETIME = timedelta(minutes=5)
THUMBNAIL_FETCH_CONCURRENCY = 1
FFMPEG_SHUTDOWN_TIMEOUT_SECONDS = 5
REMUX_ACQUIRE_TIMEOUT_SECONDS = 8
REMUX_CANCEL_WAIT_SECONDS = 3
DATE_BUCKETS = (
    ("today", "Today", 0),
    ("yesterday", "Yesterday", 1),
)
BY_DATE_LOOKBACK_YEARS = 6


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up Hikvision recordings as a media source."""
    entries = hass.config_entries.async_entries(DOMAIN)
    _LOGGER.debug(
        "Creating Hikvision media source; config_entries=%d loaded_entries=%d",
        len(entries),
        sum(1 for entry in entries if not entry.disabled_by and getattr(entry, "runtime_data", None)),
    )
    _async_register_download_view(hass)
    return HikvisionRecordingMediaSource(hass)


def _async_register_download_view(hass: HomeAssistant) -> None:
    """Register the recording download view once."""
    if hass.data.get(DATA_VIEW_REGISTERED):
        _LOGGER.debug("Hikvision recording download view already registered")
        return
    hass.http.register_view(HikvisionRecordingDownloadView(hass))
    hass.http.register_view(HikvisionRecordingThumbnailView(hass))
    hass.data[DATA_VIEW_REGISTERED] = True
    _LOGGER.debug("Registered Hikvision recording download view at %s/{token}", RECORDING_DOWNLOAD_PATH)
    _LOGGER.debug("Registered Hikvision recording thumbnail view at %s/{token}", THUMBNAIL_PATH)


class HikvisionRecordingMediaSource(MediaSource):
    """Provide Hikvision camera and NVR recordings as a media source."""

    name = "Hikvision Recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass
        _LOGGER.debug("Initialized Hikvision media source for domain %s", DOMAIN)

    async def async_browse_media(self, item: MediaSourceItem | None) -> BrowseMediaSource:
        """Browse Hikvision recordings."""
        identifier = item.identifier if item else ""
        _LOGGER.debug("Browsing Hikvision media source; identifier=%r", identifier)
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
            return await self._build_recording_list(entry_id, device, camera_id, parts[3], mode="bucket")

        if len(parts) >= 3 and parts[0] == "camera" and parts[2] == "date":
            camera_id = self._parse_camera_id(parts[1])
            return await self._build_recording_list(
                entry_id,
                device,
                camera_id,
                "today",
                tree_parts=parts[3:],
                mode="by_date",
            )

        raise BrowseError(f"Unknown Hikvision media source item: {identifier}")

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a recording to a signed Home Assistant remux URL."""
        entry_id, path = self._split_entry_path(item.identifier)
        self._get_device(entry_id)
        if not path.startswith("recording/"):
            raise Unresolvable("Only recording items can be resolved")

        token_part = path.rsplit("/", 1)[-1]
        token = token_part.split("_", 1)[1] if "_" in token_part else token_part
        item_cache = self.hass.data.setdefault(DATA_RECORDING_ITEM_CACHE, {})
        self._cleanup_cache(item_cache)
        recording = item_cache.get(token)
        if not recording or recording.get("entry_id") != entry_id:
            raise Unresolvable("Recording item expired or is not available")
        token = secrets.token_urlsafe(24)
        cache = self.hass.data.setdefault(DATA_RECORDING_CACHE, {})
        sequence = int(self.hass.data.get(DATA_RECORDING_SEQUENCE, 0)) + 1
        self.hass.data[DATA_RECORDING_SEQUENCE] = sequence
        self._cleanup_cache(cache)
        cache[token] = {
            "token": token,
            "sequence": sequence,
            "entry_id": entry_id,
            "playback_uri": recording["playback_uri"],
            "content_type": "video/mp4",
            "title": recording.get("title", "recording"),
            "size": recording.get("size", 0),
            "expires": dt_util.utcnow() + RECORDING_CACHE_LIFETIME,
        }
        path = f"{RECORDING_DOWNLOAD_PATH}/{token}"
        _LOGGER.debug(
            "Resolved Hikvision recording media to remux URL; entry_id=%s title=%s size=%s expires=%s",
            entry_id,
            recording.get("title", "recording"),
            recording.get("size", 0),
            cache[token]["expires"],
        )
        return PlayMedia(async_sign_path(self.hass, path, SIGNED_URL_LIFETIME), "video/mp4")

    def _build_root(self) -> BrowseMediaSource:
        """Build the root item with loaded Hikvision entries."""
        children = []
        entries = self.hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if entry.disabled_by or not getattr(entry, "runtime_data", None):
                _LOGGER.debug(
                    "Skipping Hikvision media source entry; entry_id=%s disabled_by=%s loaded=%s",
                    entry.entry_id,
                    entry.disabled_by,
                    bool(getattr(entry, "runtime_data", None)),
                )
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

        _LOGGER.debug(
            "Built Hikvision media source root; config_entries=%d visible_entries=%d",
            len(entries),
            len(children),
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
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
        _LOGGER.debug(
            "Built Hikvision camera list; entry_id=%s cameras=%d",
            entry_id,
            len(children),
        )
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
        children.append(
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/camera/{camera_id}/date",
                media_class=MediaClass.DIRECTORY,
                media_content_type="hikvision_recording_tree",
                title="By Date",
                can_play=False,
                can_expand=True,
            )
        )
        _LOGGER.debug(
            "Built Hikvision date buckets; entry_id=%s camera_id=%s buckets=%d",
            entry_id,
            camera_id,
            len(children),
        )
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
        tree_parts: list[str] | None = None,
        mode: str = "bucket",
    ) -> BrowseMediaSource:
        """Build recording folders and playable clips for a camera and date bucket."""
        camera = device.get_camera_by_id(camera_id)
        if not camera:
            raise BrowseError(f"Camera {camera_id} is not available")
        tree_parts = tree_parts or []
        if len(tree_parts) > 3:
            raise BrowseError(f"Unknown recording folder path: {'/'.join(tree_parts)}")
        if any(not part.isdigit() for part in tree_parts):
            raise BrowseError(f"Invalid recording folder path: {'/'.join(tree_parts)}")
        if mode == "by_date":
            return await self._build_by_date_tree(entry_id, device, camera_id, tree_parts)

        start_time, end_time, title = _bucket_range(bucket_key)
        _LOGGER.debug(
            "Searching Hikvision recordings; entry_id=%s camera_id=%s bucket=%s start=%s end=%s",
            entry_id,
            camera_id,
            bucket_key,
            start_time,
            end_time,
        )
        browse_cache = self.hass.data.setdefault(DATA_BROWSE_CACHE, {})
        cache_key = (entry_id, camera_id, bucket_key)
        cached = browse_cache.get(cache_key)
        now = dt_util.utcnow()
        if cached and cached["expires"] > now:
            recordings = cached["recordings"]
            pictures = cached["pictures"]
            _LOGGER.debug(
                "Using cached Hikvision recording list; entry_id=%s camera_id=%s bucket=%s recordings=%d pictures=%d",
                entry_id,
                camera_id,
                bucket_key,
                len(recordings),
                len(pictures),
            )
        else:
            try:
                recordings = await device.search_recordings(
                    camera_id, start_time, end_time, max_results=None
                )
            except Exception as err:  # pylint: disable=broad-except
                if cached:
                    recordings = cached["recordings"]
                    pictures = cached["pictures"]
                    _LOGGER.warning(
                        "Cannot refresh Hikvision recordings, using stale cache; entry_id=%s camera_id=%s bucket=%s error=%s",
                        entry_id,
                        camera_id,
                        bucket_key,
                        err,
                    )
                else:
                    _LOGGER.warning("Cannot search Hikvision recordings: %s", err)
                    raise MediaSourceError(str(err)) from err
            else:
                pictures = []
                if recordings:
                    try:
                        pictures = await device.search_recording_pictures(
                            camera_id, start_time, end_time, max_results=None
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.debug("Cannot search Hikvision recording pictures: %s", err)
                browse_cache[cache_key] = {
                    "recordings": recordings,
                    "pictures": pictures,
                    "expires": now + BROWSE_CACHE_LIFETIME,
                }
                self._cleanup_cache(browse_cache)

        thumbnail_cache = self.hass.data.setdefault(DATA_THUMBNAIL_CACHE, {})
        self._cleanup_cache(thumbnail_cache)
        item_cache = self.hass.data.setdefault(DATA_RECORDING_ITEM_CACHE, {})
        self._cleanup_cache(item_cache)
        thumbnails = self._build_thumbnail_urls(entry_id, camera_id, recordings, pictures, thumbnail_cache)
        children: list[BrowseMediaSource] = []
        for recording in recordings:
            recording_title = _recording_title(recording.start_time, recording.end_time, recording.name)
            media_token = secrets.token_urlsafe(18)
            local_start = dt_util.as_local(recording.start_time)
            dated_leaf = f'{local_start.strftime("%H%M%S")}_{media_token}'
            dated_identifier = (
                f'{entry_id}/recording/{local_start.strftime("%Y/%m/%d")}/{dated_leaf}'
            )
            item_cache[media_token] = {
                "entry_id": entry_id,
                "playback_uri": recording.playback_uri,
                "title": recording_title,
                "size": recording.size,
                "expires": now + RECORDING_CACHE_LIFETIME,
            }
            child = BrowseMediaSource(
                domain=DOMAIN,
                identifier=dated_identifier,
                media_class=MediaClass.VIDEO,
                media_content_type=recording.content_type,
                title=recording_title,
                can_play=True,
                can_expand=False,
                thumbnail=thumbnails.get(recording.playback_uri),
            )
            children.append(child)

        _LOGGER.debug(
            "Built Hikvision recording list; mode=bucket entry_id=%s camera_id=%s bucket=%s recordings=%d pictures=%d thumbnails=%d children=%d",
            entry_id,
            camera_id,
            bucket_key,
            len(recordings),
            len(pictures),
            len(thumbnails),
            len(children),
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

    async def _build_by_date_tree(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
        tree_parts: list[str],
    ) -> BrowseMediaSource:
        """Build a date tree using device daily distribution data."""
        camera = device.get_camera_by_id(camera_id)
        if not camera:
            raise BrowseError(f"Camera {camera_id} is not available")
        if len(tree_parts) > 3:
            raise BrowseError(f"Unknown recording folder path: {'/'.join(tree_parts)}")
        if any(not part.isdigit() for part in tree_parts):
            raise BrowseError(f"Invalid recording folder path: {'/'.join(tree_parts)}")

        base_identifier = f"{entry_id}/camera/{camera_id}/date"
        node_identifier = base_identifier
        node_title = f"{camera.name} - By Date"
        children_media_class = MediaClass.DIRECTORY

        if not tree_parts:
            years = await self._get_recording_years_with_data(entry_id, device, camera_id)
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{base_identifier}/{year:04d}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="hikvision_recording_year",
                    title=f"{year:04d}",
                    can_play=False,
                    can_expand=True,
                )
                for year in years
            ]
        elif len(tree_parts) == 1:
            year = int(tree_parts[0])
            months = await self._get_recording_months_with_data(entry_id, device, camera_id, year)
            if not months:
                raise BrowseError(f"No recordings found for year {year:04d}")
            node_identifier = f"{base_identifier}/{year:04d}"
            node_title = f"{year:04d}"
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{base_identifier}/{year:04d}/{month:02d}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="hikvision_recording_month",
                    title=f"{month:02d}",
                    can_play=False,
                    can_expand=True,
                )
                for month in months
            ]
        elif len(tree_parts) == 2:
            year = int(tree_parts[0])
            month = int(tree_parts[1])
            days = await self._get_recording_days_with_data(entry_id, device, camera_id, year, month)
            if not days:
                raise BrowseError(f"No recordings found for {year:04d}/{month:02d}")
            node_identifier = f"{base_identifier}/{year:04d}/{month:02d}"
            node_title = f"{year:04d}/{month:02d}"
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{base_identifier}/{year:04d}/{month:02d}/{day:02d}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="hikvision_recording_day",
                    title=f"{day:02d}",
                    can_play=False,
                    can_expand=True,
                )
                for day in days
            ]
        else:
            year = int(tree_parts[0])
            month = int(tree_parts[1])
            day = int(tree_parts[2])
            day_start_local = datetime(year, month, day, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            day_end_local = datetime.combine(day_start_local.date(), time.max, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            node_identifier = f"{base_identifier}/{year:04d}/{month:02d}/{day:02d}"
            node_title = f"{year:04d}/{month:02d}/{day:02d}"
            children = await self._build_recording_children_for_range(
                entry_id,
                device,
                camera_id,
                day_start_local.astimezone(dt_util.UTC),
                day_end_local.astimezone(dt_util.UTC),
            )
            children_media_class = MediaClass.VIDEO

        _LOGGER.debug(
            "Built Hikvision by-date tree; entry_id=%s camera_id=%s tree_parts=%s children=%d",
            entry_id,
            camera_id,
            "/".join(tree_parts) if tree_parts else "(root)",
            len(children),
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=node_identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="hikvision_recording_tree",
            title=node_title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=children_media_class,
        )

    async def _build_recording_children_for_range(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
        start_time: datetime,
        end_time: datetime,
    ) -> list[BrowseMediaSource]:
        """Build playable recording children for one explicit time range."""
        recordings = await device.search_recordings(camera_id, start_time, end_time, max_results=None)
        pictures = []
        if recordings:
            with suppress(Exception):
                pictures = await device.search_recording_pictures(camera_id, start_time, end_time, max_results=None)

        now = dt_util.utcnow()
        thumbnail_cache = self.hass.data.setdefault(DATA_THUMBNAIL_CACHE, {})
        self._cleanup_cache(thumbnail_cache)
        item_cache = self.hass.data.setdefault(DATA_RECORDING_ITEM_CACHE, {})
        self._cleanup_cache(item_cache)
        thumbnails = self._build_thumbnail_urls(entry_id, camera_id, recordings, pictures, thumbnail_cache)

        children: list[BrowseMediaSource] = []
        for recording in recordings:
            recording_title = _recording_title(recording.start_time, recording.end_time, recording.name)
            media_token = secrets.token_urlsafe(18)
            local_start = dt_util.as_local(recording.start_time)
            dated_leaf = f'{local_start.strftime("%H%M%S")}_{media_token}'
            dated_identifier = (
                f'{entry_id}/recording/{local_start.strftime("%Y/%m/%d")}/{dated_leaf}'
            )
            item_cache[media_token] = {
                "entry_id": entry_id,
                "playback_uri": recording.playback_uri,
                "title": recording_title,
                "size": recording.size,
                "expires": now + RECORDING_CACHE_LIFETIME,
            }
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=dated_identifier,
                    media_class=MediaClass.VIDEO,
                    media_content_type=recording.content_type,
                    title=recording_title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumbnails.get(recording.playback_uri),
                )
            )
        return children

    async def _get_recording_years_with_data(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
    ) -> list[int]:
        """Return recent years that have at least one recording day."""
        current_year = dt_util.now().year
        years = []
        for year in range(current_year, current_year - BY_DATE_LOOKBACK_YEARS, -1):
            months = await self._get_recording_months_with_data(entry_id, device, camera_id, year)
            if months:
                years.append(year)
        return years

    async def _get_recording_months_with_data(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
        year: int,
    ) -> list[int]:
        """Return months with at least one recording day."""
        months = []
        for month in range(12, 0, -1):
            days = await self._get_recording_days_with_data(entry_id, device, camera_id, year, month)
            if days:
                months.append(month)
        return months

    async def _get_recording_days_with_data(
        self,
        entry_id: str,
        device: HikvisionDevice,
        camera_id: int,
        year: int,
        month: int,
    ) -> list[int]:
        """Return sorted days with recordings for one month."""
        cache = self.hass.data.setdefault(DATA_DAILY_DISTRIBUTION_CACHE, {})
        cache_key = (entry_id, camera_id, year, month)
        now = dt_util.utcnow()
        cached = cache.get(cache_key)
        if cached and cached.get("expires") and cached["expires"] > now:
            return cached["days"]

        days = sorted(
            await device.get_recording_daily_distribution(camera_id, year, month),
            reverse=True,
        )
        cache[cache_key] = {
            "days": days,
            "expires": now + DAILY_DISTRIBUTION_CACHE_LIFETIME,
        }
        self._cleanup_cache(cache)
        return days

    def _get_device(self, entry_id: str) -> HikvisionDevice:
        """Get the loaded Hikvision device for an entry."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            _LOGGER.debug(
                "Cannot get Hikvision media source device; entry_id=%s entry_exists=%s loaded=%s",
                entry_id,
                bool(entry),
                bool(getattr(entry, "runtime_data", None)) if entry else False,
            )
            raise MediaSourceError(f"Hikvision entry {entry_id} is not loaded")
        return entry.runtime_data

    def _build_thumbnail_urls(
        self,
        entry_id: str,
        camera_id: int,
        recordings: list[Any],
        pictures: list[Any],
        thumbnail_cache: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Build signed thumbnail URLs for every recording."""
        thumbnails = {}
        snapshot_token = _snapshot_thumbnail_token(entry_id, camera_id)
        snapshot_url = async_sign_path(
            self.hass,
            f"{THUMBNAIL_PATH}/{snapshot_token}",
            SIGNED_URL_LIFETIME,
        )
        for recording in recordings:
            picture = _find_thumbnail_picture(recording, pictures)
            if not picture:
                thumbnail_cache.setdefault(snapshot_token, {}).update(
                    {
                        "kind": "snapshot",
                        "entry_id": entry_id,
                        "camera_id": camera_id,
                        "expires": dt_util.utcnow() + THUMBNAIL_CACHE_LIFETIME,
                    }
                )
                thumbnails[recording.playback_uri] = snapshot_url
                continue

            token = _thumbnail_token(picture.playback_uri)
            thumbnail_cache.setdefault(token, {}).update(
                {
                    "kind": "picture",
                    "entry_id": entry_id,
                    "playback_uri": picture.playback_uri,
                    "expires": dt_util.utcnow() + THUMBNAIL_CACHE_LIFETIME,
                }
            )
            thumbnails[recording.playback_uri] = async_sign_path(
                self.hass,
                f"{THUMBNAIL_PATH}/{token}",
                SIGNED_URL_LIFETIME,
            )
        return thumbnails

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
    """Proxy recording playback through Home Assistant."""

    requires_auth = True
    name = f"api:{DOMAIN}:recording"
    url = f"{RECORDING_DOWNLOAD_PATH}/{{token}}"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        """Stream a recording download."""
        _LOGGER.debug("Hikvision recording download requested; token=%s", token)
        cache = self.hass.data.setdefault(DATA_RECORDING_CACHE, {})
        HikvisionRecordingMediaSource._cleanup_cache(cache)
        recording = cache.get(token)
        if not recording:
            _LOGGER.debug("Hikvision recording download token missing or expired; token=%s", token)
            raise web.HTTPNotFound(text="Recording token expired or not found")

        entry = self.hass.config_entries.async_get_entry(recording["entry_id"])
        if not entry or not getattr(entry, "runtime_data", None):
            _LOGGER.debug(
                "Hikvision recording download entry unavailable; entry_id=%s",
                recording["entry_id"],
            )
            raise web.HTTPNotFound(text="Hikvision entry is not loaded")

        device: HikvisionDevice = entry.runtime_data
        remux_semaphores = self.hass.data.setdefault(DATA_REMUX_SEMAPHORE, {})
        remux_semaphore = remux_semaphores.setdefault(recording["entry_id"], asyncio.Semaphore(1))
        active_remux_by_entry = self.hass.data.setdefault(DATA_ACTIVE_REMUX, {})
        sequence = int(recording.get("sequence", 0))
        if remux_semaphore.locked():
            active = active_remux_by_entry.get(recording["entry_id"], {})
            active_token = active.get("token")
            active_sequence = int(active.get("sequence", 0))
            if active_token == token:
                _LOGGER.debug(
                    "Ignoring duplicate Hikvision recording request while same token is active; token=%s sequence=%s",
                    token,
                    sequence,
                )
                raise web.HTTPConflict(
                    text="Recording is already streaming",
                    headers={"Retry-After": "1"},
                )
            if sequence and active_sequence and sequence < active_sequence:
                _LOGGER.debug(
                    "Ignoring stale Hikvision recording request; token=%s sequence=%s active_token=%s active_sequence=%s",
                    token,
                    sequence,
                    active_token,
                    active_sequence,
                )
                raise web.HTTPConflict(
                    text="Stale recording request superseded by newer playback",
                    headers={"Retry-After": "1"},
                )
            await _async_stop_active_remux(self.hass, recording["entry_id"], "new playback request")
        acquired = False
        try:
            await asyncio.wait_for(remux_semaphore.acquire(), timeout=REMUX_ACQUIRE_TIMEOUT_SECONDS)
            acquired = True
        except asyncio.TimeoutError as err:
            _LOGGER.debug(
                "Timed out waiting for previous Hikvision recording playback to stop; entry_id=%s title=%s timeout=%ss",
                recording["entry_id"],
                recording["title"],
                REMUX_ACQUIRE_TIMEOUT_SECONDS,
            )
            raise web.HTTPServiceUnavailable(
                text="Previous Hikvision recording is still stopping",
                headers={"Retry-After": "2"},
            ) from err

        if shutil.which("ffmpeg") is None:
            if acquired:
                remux_semaphore.release()
            _LOGGER.warning("Cannot stream Hikvision recording because ffmpeg is not available")
            raise web.HTTPServiceUnavailable(text="ffmpeg is not available")

        try:
            rtsp_source = device.get_authenticated_recording_rtsp_source(recording["playback_uri"])
        except ValueError as err:
            if acquired:
                remux_semaphore.release()
            _LOGGER.warning(
                "Rejected Hikvision recording playback URI; entry_id=%s title=%s error=%s",
                recording["entry_id"],
                recording["title"],
                err,
            )
            raise web.HTTPBadRequest(text="Invalid recording source") from err

        active_remux = {
            "task": asyncio.current_task(),
            "process": None,
            "token": token,
            "sequence": sequence,
            "entry_id": recording["entry_id"],
            "title": recording["title"],
            "started": dt_util.utcnow(),
        }
        active_remux_by_entry[recording["entry_id"]] = active_remux

        def _register_process(process: asyncio.subprocess.Process) -> None:
            if active_remux_by_entry.get(recording["entry_id"]) is active_remux:
                active_remux["process"] = process

        _LOGGER.debug(
            "Remuxing Hikvision RTSP recording through Home Assistant; entry_id=%s title=%s source_size=%s range=%s",
            recording["entry_id"],
            recording["title"],
            recording["size"],
            request.headers.get("Range"),
        )
        response = web.StreamResponse(
            status=200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "video/mp4",
                "Content-Disposition": f'inline; filename="{_safe_recording_filename(recording["title"])}.mp4"',
            },
        )
        byte_count = 0
        try:
            await response.prepare(request)
            byte_count = await _stream_rtsp_remuxed_recording(
                rtsp_source,
                response,
                _register_process,
            )
            with suppress(ConnectionResetError, BrokenPipeError, RuntimeError):
                await response.write_eof()
        finally:
            if acquired:
                remux_semaphore.release()
            if active_remux_by_entry.get(recording["entry_id"], {}).get("task") is asyncio.current_task():
                active_remux_by_entry.pop(recording["entry_id"], None)
        _LOGGER.debug(
            "Finished Hikvision RTSP remuxed recording playback; entry_id=%s status=%s bytes=%d",
            recording["entry_id"],
            response.status,
            byte_count,
        )
        return response


class HikvisionRecordingThumbnailView(HomeAssistantView):
    """Proxy recording thumbnails through Home Assistant."""

    requires_auth = True
    name = f"api:{DOMAIN}:thumbnail"
    url = f"{THUMBNAIL_PATH}/{{token}}"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.Response:
        """Return a recording thumbnail."""
        cache = self.hass.data.setdefault(DATA_THUMBNAIL_CACHE, {})
        HikvisionRecordingMediaSource._cleanup_cache(cache)
        thumbnail = cache.get(token)
        if not thumbnail:
            raise web.HTTPNotFound(text="Thumbnail token expired or not found")

        entry = self.hass.config_entries.async_get_entry(thumbnail["entry_id"])
        if not entry or not getattr(entry, "runtime_data", None):
            raise web.HTTPNotFound(text="Hikvision entry is not loaded")

        device: HikvisionDevice = entry.runtime_data
        image = thumbnail.get("image")
        image_expires = thumbnail.get("image_expires")
        if image and image_expires and image_expires > dt_util.utcnow():
            _LOGGER.debug("Returning cached Hikvision thumbnail; token=%s kind=%s bytes=%d", token, thumbnail.get("kind"), len(image))
        else:
            thumbnail_semaphore = self.hass.data.setdefault(
                DATA_THUMBNAIL_SEMAPHORE,
                asyncio.Semaphore(THUMBNAIL_FETCH_CONCURRENCY),
            )
            async with thumbnail_semaphore:
                image = thumbnail.get("image")
                image_expires = thumbnail.get("image_expires")
                if image and image_expires and image_expires > dt_util.utcnow():
                    _LOGGER.debug("Returning cached Hikvision thumbnail after wait; token=%s kind=%s bytes=%d", token, thumbnail.get("kind"), len(image))
                elif thumbnail.get("kind") == "snapshot":
                    image = await _async_get_camera_snapshot_thumbnail(device, thumbnail["camera_id"])
                    if image:
                        thumbnail["image"] = image
                        thumbnail["image_expires"] = dt_util.utcnow() + SNAPSHOT_THUMBNAIL_IMAGE_LIFETIME
                else:
                    image = await device.download_recording_picture(thumbnail["playback_uri"])
                    if image:
                        thumbnail["image"] = image
                        thumbnail["image_expires"] = dt_util.utcnow() + THUMBNAIL_CACHE_LIFETIME
                        thumbnail["expires"] = dt_util.utcnow() + THUMBNAIL_CACHE_LIFETIME
        if not image:
            raise web.HTTPNotFound(text="Thumbnail is not available")

        return web.Response(
            body=image,
            headers={
                "Cache-Control": "private, max-age=300",
                "Content-Type": "image/jpeg",
            },
        )


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


def _safe_recording_filename(title: str) -> str:
    """Return a safe filename for Content-Disposition headers."""
    cleaned = re.sub(r'[^A-Za-z0-9._ -]+', "_", title).strip(" ._")
    return cleaned[:120] or "recording"


def _thumbnail_token(playback_uri: str) -> str:
    """Build a stable thumbnail token for one Hikvision picture URI."""
    return urlsafe_b64encode(sha256(playback_uri.encode()).digest()[:18]).decode().rstrip("=")


def _snapshot_thumbnail_token(entry_id: str, camera_id: int) -> str:
    """Build a stable thumbnail token for a camera snapshot fallback."""
    value = f"snapshot:{entry_id}:{camera_id}"
    return urlsafe_b64encode(sha256(value.encode()).digest()[:18]).decode().rstrip("=")


async def _async_get_camera_snapshot_thumbnail(device: HikvisionDevice, camera_id: int) -> bytes | None:
    """Get the current camera snapshot for missing recording thumbnails."""
    camera = device.get_camera_by_id(camera_id)
    if not camera or not camera.streams:
        _LOGGER.debug("Cannot build snapshot thumbnail; camera_id=%s has no stream", camera_id)
        return None
    image = await device.get_camera_image(camera.streams[0], width=320, height=180)
    if not image or not image.startswith(b"\xff\xd8"):
        _LOGGER.debug(
            "Snapshot thumbnail response was not JPEG; camera_id=%s bytes=%d",
            camera_id,
            len(image or b""),
        )
        return None
    _LOGGER.debug("Fetched snapshot thumbnail; camera_id=%s bytes=%d", camera_id, len(image))
    return image


def _find_thumbnail_picture(recording: Any, pictures: list[Any]) -> Any | None:
    """Find the nearest static picture for a recording."""
    if not pictures:
        return None

    in_clip = [picture for picture in pictures if recording.start_time <= picture.start_time <= recording.end_time]
    if in_clip:
        return min(in_clip, key=lambda picture: abs((picture.start_time - recording.start_time).total_seconds()))

    nearest = min(pictures, key=lambda picture: abs((picture.start_time - recording.start_time).total_seconds()))
    if abs((nearest.start_time - recording.start_time).total_seconds()) <= 30:
        return nearest
    return None


def _redact_rtsp_credentials(value: str) -> str:
    """Redact credentials from RTSP URLs before logging."""
    return re.sub(r"rtsp://[^/\s:@]+:[^/\s@]+@", "rtsp://***:***@", value)


async def _stream_rtsp_remuxed_recording(
    rtsp_source: str,
    response: web.StreamResponse,
    on_started: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> int:
    """Stream-copy a Hikvision RTSP recording into fragmented MP4."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_source,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-movflags",
        "empty_moov+frag_keyframe+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if on_started:
        on_started(process)
    assert process.stdout is not None
    assert process.stderr is not None

    stderr_task = asyncio.create_task(process.stderr.read())
    byte_count = 0
    client_disconnected = False
    cancelled = False
    try:
        while chunk := await process.stdout.read(64 * 1024):
            byte_count += len(chunk)
            await response.write(chunk)
    except asyncio.CancelledError:
        cancelled = True
        client_disconnected = True
        _LOGGER.debug("Hikvision recording remux cancelled while streaming output")
    except (ConnectionResetError, BrokenPipeError):
        client_disconnected = True
        _LOGGER.debug("Client disconnected during Hikvision recording remux")
    finally:
        if client_disconnected and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()

    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=FFMPEG_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _LOGGER.debug("Forcing ffmpeg termination after shutdown timeout")
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    returncode = process.returncode

    stderr = ""
    with suppress(asyncio.CancelledError, Exception):
        stderr = _redact_rtsp_credentials((await stderr_task).decode(errors="replace").strip())

    if client_disconnected:
        if stderr:
            _LOGGER.debug("Hikvision recording remux stopped after client disconnect; stderr=%s", stderr)
    elif returncode:
        _LOGGER.warning("Hikvision recording remux failed; returncode=%s stderr=%s", returncode, stderr)
    elif stderr:
        _LOGGER.debug("Hikvision recording remux stderr: %s", stderr)
    if cancelled:
        raise asyncio.CancelledError()
    return byte_count


async def _async_stop_active_remux(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    """Stop any active remux task so a new playback request can start."""
    active = hass.data.setdefault(DATA_ACTIVE_REMUX, {}).get(entry_id)
    if not active:
        return

    task = active.get("task")
    process = active.get("process")
    _LOGGER.debug(
        "Stopping active Hikvision remux before starting another playback; reason=%s entry_id=%s title=%s",
        reason,
        active.get("entry_id"),
        active.get("title"),
    )

    if process and process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()

    if isinstance(task, asyncio.Task) and task is not asyncio.current_task() and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(task, timeout=REMUX_CANCEL_WAIT_SECONDS)
