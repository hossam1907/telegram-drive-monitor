"""YouTube playlist and video extraction service."""

import logging
import re
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class YouTubeService:
    """Extract playlist and video information from YouTube channels."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_channel_playlists(self, channel_url: str) -> List[Dict]:
        """Extract playlists from a YouTube channel playlists page."""
        try:
            response = await self.client.get(f"{channel_url.rstrip('/')}/playlists")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            playlists: Dict[str, Dict] = {}

            for link in soup.find_all("a", href=re.compile(r"/playlist\?list=")):
                playlist_url = link.get("href", "")
                playlist_id = self._extract_playlist_id(playlist_url)
                playlist_name = link.get("title") or link.get_text(strip=True)
                if not playlist_id or not playlist_name:
                    continue
                playlists[playlist_id] = {
                    "name": playlist_name,
                    "id": playlist_id,
                    "url": f"https://www.youtube.com{playlist_url}"
                    if playlist_url.startswith("/")
                    else playlist_url,
                }

            logger.info("Found %d playlists for channel %s", len(playlists), channel_url)
            return list(playlists.values())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error extracting playlists from %s: %s", channel_url, exc)
            return []

    async def get_playlist_videos(self, playlist_id: str) -> List[Dict]:
        """Extract videos from a YouTube playlist page."""
        try:
            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            response = await self.client.get(playlist_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            videos: List[Dict] = []
            seen_video_ids = set()

            for idx, link in enumerate(
                soup.find_all("a", href=re.compile(r"/watch\?v=[a-zA-Z0-9_-]+")),
                start=1,
            ):
                video_url = link.get("href", "")
                video_id = self._extract_video_id(video_url)
                video_title = link.get("title") or link.get_text(strip=True)
                if not video_id or not video_title or video_id in seen_video_ids:
                    continue
                seen_video_ids.add(video_id)
                videos.append(
                    {
                        "title": video_title,
                        "id": video_id,
                        "url": f"https://www.youtube.com{video_url}"
                        if video_url.startswith("/")
                        else video_url,
                        "order": len(videos) + 1,
                    }
                )

            logger.info("Found %d videos in playlist %s", len(videos), playlist_id)
            return videos
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error extracting videos from %s: %s", playlist_id, exc)
            return []

    @staticmethod
    def _extract_playlist_id(url: str) -> Optional[str]:
        """Extract playlist ID from URL."""
        match = re.search(r"list=([a-zA-Z0-9_-]+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _extract_video_id(url: str) -> Optional[str]:
        """Extract video ID from URL."""
        match = re.search(r"v=([a-zA-Z0-9_-]+)", url)
        return match.group(1) if match else None

    async def close(self) -> None:
        """Close async HTTP client."""
        await self.client.aclose()
