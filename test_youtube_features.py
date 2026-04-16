"""Tests for YouTube extraction and download command helpers."""

import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class YouTubeFeatureTests(unittest.TestCase):
    """Validate YouTube API parsing and download command flow."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.creds_path = os.path.join(cls.temp_dir.name, "creds.json")
        with open(cls.creds_path, "w", encoding="utf-8") as fh:
            fh.write("{}")

        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["DRIVE_FOLDER_ID"] = "folder"
        os.environ["GOOGLE_CREDENTIALS_FILE"] = cls.creds_path
        os.environ["ADMIN_USER_IDS"] = "1"
        os.environ["DATABASE_PATH"] = os.path.join(cls.temp_dir.name, "test_youtube.db")
        os.environ["YOUTUBE_API_KEY"] = "fake-key"

        import config  # noqa: PLC0415
        import main  # noqa: PLC0415
        import youtube_service  # noqa: PLC0415

        importlib.reload(config)
        cls.main = importlib.reload(main)
        cls.youtube_service = importlib.reload(youtube_service)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_extract_channel_identifier_variants(self) -> None:
        parser = self.youtube_service.YouTubeService._extract_channel_identifier
        self.assertEqual(parser("https://www.youtube.com/@CUFE_EPE_27"), {"type": "handle", "value": "CUFE_EPE_27"})
        self.assertEqual(
            parser("https://www.youtube.com/channel/UC123abc"),
            {"type": "id", "value": "UC123abc"},
        )
        self.assertEqual(
            parser("https://www.youtube.com/user/test_user"),
            {"type": "username", "value": "test_user"},
        )

    def test_cmd_download_youtube_shows_quality_buttons(self) -> None:
        status_message = SimpleNamespace(edit_text=AsyncMock())
        reply_text = AsyncMock(return_value=status_message)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, first_name="Admin"),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(
            args=["https://www.youtube.com/watch?v=abc123"],
            user_data={},
        )

        formats = [
            {"label": "720p", "format_id": "22", "ext": "mp4", "filesize": 10_000_000, "audio_only": False},
            {"label": "Audio", "format_id": "140", "ext": "m4a", "filesize": 2_000_000, "audio_only": True},
        ]
        with patch.object(self.main._yt_downloader, "get_formats", AsyncMock(return_value=formats)):
            self.main.asyncio.run(self.main.cmd_download_youtube(update, context))

        reply_text.assert_awaited_once()
        status_message.edit_text.assert_awaited_once()
        self.assertIn("youtube_downloads", context.user_data)
        self.assertEqual(len(context.user_data["youtube_downloads"]), 1)

    def test_callback_query_youtube_request_expired(self) -> None:
        query = SimpleNamespace(
            data="yt:expired:0",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat_id=1),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            callback_query=query,
        )
        context = SimpleNamespace(user_data={}, bot=AsyncMock())

        self.main.asyncio.run(self.main.callback_query_handler(update, context))

        query.answer.assert_any_await("This download request expired. Please retry.", show_alert=True)


if __name__ == "__main__":
    unittest.main()
