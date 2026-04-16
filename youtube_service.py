"""YouTube API integration for playlist and video extraction."""

import asyncio
import logging
import re
from typing import Dict, List, Optional

from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


class YouTubeService:
    """Extract playlist and video information via YouTube Data API v3."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.youtube: Optional[Resource] = (
            build("youtube", "v3", developerKey=api_key, cache_discovery=False)
            if api_key
            else None
        )

    async def __aenter__(self) -> "YouTubeService":
        """Allow use as an async context manager."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close the underlying client on context exit."""
        await self.close()

    async def _execute(self, request):
        """Execute a Google API request without blocking the event loop."""
        return await asyncio.to_thread(request.execute)

    async def get_channel_playlists(self, channel_url: str) -> List[Dict]:
        """Extract all playlists from a YouTube channel."""
        if self.youtube is None:
            logger.warning("YouTube API key is missing. Cannot extract playlists.")
            return []

        channel_ref = self._extract_channel_identifier(channel_url)
        if not channel_ref:
            logger.warning("Could not parse channel URL: %s", channel_url)
            return []

        channel_id = await self._resolve_channel_id(channel_ref)
        if not channel_id:
            logger.warning("Could not resolve channel ID for URL: %s", channel_url)
            return []

        playlists: List[Dict] = []
        next_page_token: Optional[str] = None

        try:
            while True:
                request = self.youtube.playlists().list(
                    part="id,snippet,contentDetails",
                    channelId=channel_id,
                    maxResults=50,
                    pageToken=next_page_token,
                )
                response = await self._execute(request)
                for item in response.get("items", []):
                    playlist_id = item.get("id")
                    snippet = item.get("snippet", {})
                    content_details = item.get("contentDetails", {})
                    if not playlist_id:
                        continue
                    playlists.append(
                        {
                            "id": playlist_id,
                            "name": snippet.get("title", "Untitled Playlist"),
                            "url": f"https://www.youtube.com/playlist?list={playlist_id}",
                            "video_count": int(content_details.get("itemCount", 0)),
                        }
                    )

                next_page_token = response.get("nextPageToken")
                if not next_page_token:
                    break
        except HttpError as exc:
            logger.warning("YouTube API error while listing playlists for %s: %s", channel_url, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error listing playlists for %s: %s", channel_url, exc)
            return []

        logger.info("Found %d playlists for channel %s", len(playlists), channel_url)
        return playlists

    async def get_playlist_videos(self, playlist_id: str) -> List[Dict]:
        """Extract all videos from a playlist (with pagination and metadata)."""
        if self.youtube is None:
            logger.warning("YouTube API key is missing. Cannot extract videos.")
            return []

        try:
            ordered_video_ids: List[str] = []
            next_page_token: Optional[str] = None

            while True:
                request = self.youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=next_page_token,
                )
                response = await self._execute(request)
                for item in response.get("items", []):
                    content_details = item.get("contentDetails", {})
                    video_id = content_details.get("videoId")
                    if not video_id:
                        continue
                    ordered_video_ids.append(video_id)

                next_page_token = response.get("nextPageToken")
                if not next_page_token:
                    break

            details_by_id: Dict[str, Dict] = {}
            for i in range(0, len(ordered_video_ids), 50):
                batch_ids = ordered_video_ids[i:i + 50]
                if not batch_ids:
                    continue
                request = self.youtube.videos().list(
                    part="snippet,contentDetails,statistics",
                    id=",".join(batch_ids),
                    maxResults=50,
                )
                response = await self._execute(request)
                for item in response.get("items", []):
                    details_by_id[item.get("id", "")] = item

            videos: List[Dict] = []
            for idx, video_id in enumerate(ordered_video_ids, start=1):
                item = details_by_id.get(video_id, {})
                snippet = item.get("snippet", {})
                thumbnails = snippet.get("thumbnails", {})
                thumbnail = (
                    thumbnails.get("high", {}).get("url")
                    or thumbnails.get("medium", {}).get("url")
                    or thumbnails.get("default", {}).get("url")
                )
                statistics = item.get("statistics", {})
                videos.append(
                    {
                        "id": video_id,
                        "title": snippet.get("title", "Untitled Video"),
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "order": idx,
                        "duration": item.get("contentDetails", {}).get("duration"),
                        "thumbnail_url": thumbnail,
                        "view_count": int(statistics.get("viewCount", 0)) if statistics else 0,
                    }
                )

            logger.info("Found %d videos in playlist %s", len(videos), playlist_id)
            return videos
        except HttpError as exc:
            logger.warning("YouTube API error while listing videos for %s: %s", playlist_id, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error extracting videos from %s: %s", playlist_id, exc)
            return []

    @staticmethod
    def _extract_channel_identifier(channel_url: str) -> Optional[Dict[str, str]]:
        """Extract channel identifier type/value from URL."""
        if "/channel/" in channel_url:
            match = re.search(r"/channel/([a-zA-Z0-9_-]+)", channel_url)
            if match:
                return {"type": "id", "value": match.group(1)}
        if "/user/" in channel_url:
            match = re.search(r"/user/([a-zA-Z0-9_-]+)", channel_url)
            if match:
                return {"type": "username", "value": match.group(1)}
        if "/@" in channel_url:
            match = re.search(r"/@([a-zA-Z0-9._-]+)", channel_url)
            if match:
                return {"type": "handle", "value": match.group(1)}
        if "/c/" in channel_url:
            match = re.search(r"/c/([a-zA-Z0-9._-]+)", channel_url)
            if match:
                return {"type": "search", "value": match.group(1)}
        return None

    async def _resolve_channel_id(self, channel_ref: Dict[str, str]) -> Optional[str]:
        """Resolve channel ID using channel URL identifier details."""
        if self.youtube is None:
            return None

        ref_type = channel_ref["type"]
        ref_value = channel_ref["value"]

        if ref_type == "id":
            return ref_value

        try:
            if ref_type == "username":
                request = self.youtube.channels().list(part="id", forUsername=ref_value, maxResults=1)
                response = await self._execute(request)
                items = response.get("items", [])
                return items[0].get("id") if items else None

            if ref_type == "handle":
                try:
                    request = self.youtube.channels().list(part="id", forHandle=ref_value, maxResults=1)
                    response = await self._execute(request)
                    items = response.get("items", [])
                    if items:
                        return items[0].get("id")
                except Exception:  # noqa: BLE001
                    logger.debug("forHandle not supported, falling back to search for handle %s", ref_value)

            request = self.youtube.search().list(
                part="snippet",
                q=ref_value,
                type="channel",
                maxResults=1,
            )
            response = await self._execute(request)
            items = response.get("items", [])
            if not items:
                return None
            return items[0].get("id", {}).get("channelId")
        except HttpError as exc:
            logger.warning("YouTube API error while resolving channel '%s': %s", ref_value, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error resolving channel '%s': %s", ref_value, exc)
            return None

    async def close(self) -> None:
        """Release resources."""
        self.youtube = None
