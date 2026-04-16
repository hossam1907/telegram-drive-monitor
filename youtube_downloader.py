"""YouTube video downloading using yt-dlp."""

import asyncio
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import yt_dlp

logger = logging.getLogger(__name__)


class YouTubeDownloader:
    """Download YouTube videos with format selection."""

    QUALITIES = [1080, 720, 480, 360, 240]

    @staticmethod
    def _extract_info(url: str) -> Dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    async def get_formats(self, url: str) -> List[Dict]:
        """Get available download formats."""
        info = await asyncio.to_thread(self._extract_info, url)
        formats = info.get("formats", []) or []
        title = info.get("title", "video")

        choices: List[Dict] = []
        for quality in self.QUALITIES:
            candidates = [
                f for f in formats
                if f.get("vcodec") not in (None, "none")
                and f.get("height")
                and int(f["height"]) <= quality
            ]
            if not candidates:
                continue
            best = max(
                candidates,
                key=lambda x: (
                    int(x.get("height") or 0),
                    float(x.get("tbr") or 0),
                ),
            )
            filesize = best.get("filesize") or best.get("filesize_approx")
            choices.append(
                {
                    "label": f"{quality}p",
                    "quality": quality,
                    "format_id": str(best["format_id"]),
                    "ext": best.get("ext") or "mp4",
                    "filesize": int(filesize) if filesize else None,
                    "title": title,
                    "audio_only": False,
                }
            )

        audio_candidates = [
            f for f in formats
            if f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")
        ]
        if audio_candidates:
            best_audio = max(audio_candidates, key=lambda x: float(x.get("abr") or x.get("tbr") or 0))
            audio_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
            choices.append(
                {
                    "label": "Audio",
                    "quality": 0,
                    "format_id": str(best_audio["format_id"]),
                    "ext": best_audio.get("ext") or "m4a",
                    "filesize": int(audio_size) if audio_size else None,
                    "title": title,
                    "audio_only": True,
                }
            )

        # Deduplicate repeated format IDs while preserving quality order.
        deduped: List[Dict] = []
        seen = set()
        for item in choices:
            key = (item["format_id"], item["audio_only"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return deduped

    @staticmethod
    def _download_to_file(
        url: str,
        format_id: str,
        suffix: str,
        audio_only: bool = False,
    ) -> Tuple[Optional[str], Optional[Dict]]:
        """Download the selected format to a temp file.

        Args:
            url: YouTube video URL.
            format_id: yt-dlp format ID selected by the user.
            suffix: Optional file suffix hint (for temp file extension).

        Returns:
            Tuple of downloaded file path (or ``None``) and extracted video info dict.
        """
        fd, temp_path = tempfile.mkstemp(prefix="yt_", suffix=suffix)
        os.close(fd)
        format_selector = format_id if audio_only else f"{format_id}+bestaudio/best"
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": format_selector,
            "outtmpl": temp_path,
            "overwrites": True,
            "restrictfilenames": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                downloaded_path = ydl.prepare_filename(info)
            if downloaded_path and os.path.exists(downloaded_path):
                return downloaded_path, info
            if os.path.exists(temp_path):
                return temp_path, info
            return None, info
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    async def download(
        self,
        url: str,
        format_id: str,
        ext_hint: str = "",
        audio_only: bool = False,
    ) -> Optional[Dict]:
        """Download video in specific format."""
        suffix = f".{ext_hint}" if ext_hint else ""
        try:
            path, info = await asyncio.to_thread(
                self._download_to_file,
                url,
                format_id,
                suffix,
                audio_only,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("yt-dlp failed for url=%s format_id=%s: %s", url, format_id, exc)
            raise RuntimeError("Failed to download requested YouTube format.") from exc
        if not path:
            return None
        return {
            "path": path,
            "title": (info or {}).get("title", "YouTube Video"),
        }
