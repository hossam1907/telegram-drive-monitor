"""Tests for course management database operations."""

import importlib
import os
import tempfile
import unittest


class CourseManagementDatabaseTests(unittest.TestCase):
    """Validate new course/youtube/broadcast/enrollment DB APIs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.creds_path = os.path.join(cls.temp_dir.name, "creds.json")
        cls.db_path = os.path.join(cls.temp_dir.name, "test.db")
        with open(cls.creds_path, "w", encoding="utf-8") as fh:
            fh.write("{}")

        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["DRIVE_FOLDER_ID"] = "folder"
        os.environ["GOOGLE_CREDENTIALS_FILE"] = cls.creds_path
        os.environ["ADMIN_USER_IDS"] = "1"
        os.environ["DATABASE_PATH"] = cls.db_path

        import database  # noqa: PLC0415

        cls.database = importlib.reload(database)
        cls.database.init_db()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_course_playlist_video_enrollment_and_broadcast(self) -> None:
        course_id = self.database.add_course(
            course_name="Digital Control Systems",
            course_code="EPE3090",
            description="Digital Control Systems EPE3090",
            drive_folder_id="drive-123",
            youtube_channel_id="channel-1",
        )
        self.assertGreater(course_id, 0)

        fetched_course = self.database.get_course_by_code("epe3090")
        self.assertIsNotNone(fetched_course)
        self.assertEqual(fetched_course["course_id"], course_id)

        self.database.add_youtube_playlist(
            playlist_id="PL123",
            course_id=course_id,
            playlist_name="Intro EPE3090",
            playlist_url="https://www.youtube.com/playlist?list=PL123",
            video_count=1,
        )
        self.database.add_youtube_video(
            video_id="VID001",
            playlist_id="PL123",
            course_id=course_id,
            video_title="Lecture 1",
            video_url="https://www.youtube.com/watch?v=VID001",
            video_order=1,
        )

        playlists = self.database.get_course_playlists(course_id)
        self.assertEqual(len(playlists), 1)
        videos = self.database.get_playlist_videos("PL123")
        self.assertEqual(len(videos), 1)
        self.assertEqual(self.database.get_course_video_count(course_id), 1)

        inserted = self.database.enroll_user_in_course(user_id=99, course_id=course_id)
        self.assertTrue(inserted)
        duplicate_insert = self.database.enroll_user_in_course(user_id=99, course_id=course_id)
        self.assertFalse(duplicate_insert)
        self.assertEqual(self.database.get_course_enrolled_users(course_id), [99])
        self.assertEqual(len(self.database.get_user_courses(99)), 1)

        msg_id = self.database.send_broadcast(
            sender_id=1,
            message_text="hello",
            target_type="course",
            target_course_id=course_id,
            delivery_count=1,
        )
        self.assertGreater(msg_id, 0)
        broadcasts = self.database.get_recent_broadcasts(limit=1)
        self.assertEqual(broadcasts[0]["target_type"], "course")
        self.assertEqual(broadcasts[0]["delivery_count"], 1)


if __name__ == "__main__":
    unittest.main()
