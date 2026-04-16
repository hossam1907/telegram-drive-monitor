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
        import youtube_downloader  # noqa: PLC0415
        import youtube_service  # noqa: PLC0415

        importlib.reload(config)
        cls.main = importlib.reload(main)
        cls.youtube_downloader = importlib.reload(youtube_downloader)
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

    def test_get_formats_includes_video_only_qualities_and_audio(self) -> None:
        downloader = self.youtube_downloader.YouTubeDownloader()
        info = {
            "title": "Sample",
            "formats": [
                {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 3000, "ext": "mp4"},
                {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "tbr": 2200, "ext": "mp4"},
                {"format_id": "v480", "vcodec": "avc1", "acodec": "none", "height": 480, "tbr": 1500, "ext": "mp4"},
                {"format_id": "v360", "vcodec": "avc1", "acodec": "none", "height": 360, "tbr": 1000, "ext": "mp4"},
                {"format_id": "v240", "vcodec": "avc1", "acodec": "none", "height": 240, "tbr": 700, "ext": "mp4"},
                {"format_id": "a1", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "ext": "m4a"},
            ],
        }

        with patch.object(self.youtube_downloader.YouTubeDownloader, "_extract_info", return_value=info):
            formats = self.main.asyncio.run(downloader.get_formats("https://www.youtube.com/watch?v=abc123"))

        self.assertEqual(
            [item["label"] for item in formats],
            ["1080p", "720p", "480p", "360p", "240p", "Audio"],
        )

    def test_match_course_for_title_uses_code_then_fuzzy(self) -> None:
        courses = [
            {"course_id": 1, "course_code": "EPE3060", "course_name": "Power Systems 2"},
            {"course_id": 2, "course_code": "EPE3090", "course_name": "Digital Control Systems"},
        ]

        self.assertEqual(
            self.main._match_course_for_title("Power Systems 2 (EPE3060) | Tutorials", courses)["course_code"],
            "EPE3060",
        )
        self.assertIsNone(self.main._match_course_for_title("Conversion (EPE1020) | Tutorials", courses))
        self.assertEqual(
            self.main._match_course_for_title("Power System | Tutorials", courses)["course_code"],
            "EPE3060",
        )
        self.assertIsNone(self.main._match_course_for_title("Random Content", courses))

    def test_cmd_extract_youtube_reports_matching_categories(self) -> None:
        status_message = SimpleNamespace(edit_text=AsyncMock())
        reply_text = AsyncMock(return_value=status_message)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, first_name="Admin"),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(args=[], user_data={})

        playlists = [
            {"id": "p1", "name": "Power Systems 2 (EPE3060)", "url": "https://youtube.com/playlist?list=p1"},
            {"id": "p2", "name": "Conversion (EPE1020)", "url": "https://youtube.com/playlist?list=p2"},
            {"id": "p3", "name": "Random Content", "url": "https://youtube.com/playlist?list=p3"},
        ]
        videos_by_playlist = {
            "p1": [
                {"id": "v1", "title": "Lecture 1", "url": "https://youtube.com/watch?v=v1", "order": 1},
                {"id": "v2", "title": "Lecture 2", "url": "https://youtube.com/watch?v=v2", "order": 2},
            ],
            "p2": [{"id": "v3", "title": "Other", "url": "https://youtube.com/watch?v=v3", "order": 1}],
            "p3": [{"id": "v4", "title": "Misc", "url": "https://youtube.com/watch?v=v4", "order": 1}],
        }
        courses = [
            {"course_id": 1, "course_code": "EPE3060", "course_name": "Power Systems 2"},
            {"course_id": 2, "course_code": "EPE3090", "course_name": "Digital Control Systems"},
        ]

        service = SimpleNamespace(
            get_channel_playlists=AsyncMock(return_value=playlists),
            get_playlist_videos=AsyncMock(side_effect=lambda playlist_id: videos_by_playlist[playlist_id]),
            close=AsyncMock(),
        )

        with (
            patch.object(self.main, "YOUTUBE_CHANNELS", ["https://www.youtube.com/@test"]),
            patch.object(self.main, "YouTubeService", return_value=service),
            patch.object(self.main.database, "get_all_courses", return_value=courses),
            patch.object(self.main.database, "add_youtube_playlist") as add_playlist,
            patch.object(self.main.database, "add_youtube_video") as add_video,
        ):
            self.main.asyncio.run(self.main.cmd_extract_youtube(update, context))

        add_playlist.assert_called_once()
        self.assertEqual(add_video.call_count, 2)
        final_text = status_message.edit_text.await_args_list[-1].args[0]
        self.assertIn("Total Playlists Found: 3", final_text)
        self.assertIn("Successfully Matched: 1", final_text)
        self.assertIn("Skipped (different course codes): 1", final_text)
        self.assertIn("Unmatched (no course found): 1", final_text)
        self.assertIn("Total Videos Processed: 4", final_text)
        self.assertIn("Videos Added: 2", final_text)
        self.assertIn("Videos Skipped: 2", final_text)
        self.assertIn("EPE3060 (Power Systems 2): 1 playlist, 2 videos", final_text)


if __name__ == "__main__":
    unittest.main()
