"""
Main entry point for Telegram Drive Monitor Bot.

Initialises the Telegram application, registers command handlers,
and starts the background Drive polling task.
"""

import asyncio
import functools
import io
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from config import (
    ADMIN_USER_IDS,
    DOWNLOAD_TIMEOUT,
    MAX_FILE_SIZE,
    PAGE_SIZE,
    POLL_INTERVAL,
    TELEGRAM_BOT_TOKEN,
    TEMP_DIR,
    YOUTUBE_API_KEY,
)
import database
from database import (
    count_files,
    get_all_files,
    get_monitoring_state,
    init_db,
    record_poll,
    search_files as db_search_files,
    set_monitoring_enabled,
    upsert_file,
    delete_stale_files,
)
from google_drive_service import GoogleDriveService
from youtube_service import YouTubeService
from youtube_downloader import YouTubeDownloader
from utils import (
    build_file_keyboard,
    build_files_keyboard,
    build_folder_keyboard,
    drive_view_link,
    escape_markdown,
    format_size,
    format_timestamp,
    get_file_category,
    get_mime_icon,
    get_mime_label,
    truncate_message,
    _FOLDER_MIME_TYPE,
)

logger = logging.getLogger(__name__)

# Module-level Drive service instance (created in post_init)
_drive: Optional[GoogleDriveService] = None
_yt_downloader = YouTubeDownloader()

YOUTUBE_CHANNELS = [
    "https://www.youtube.com/@CUFE_EPE_27",
    "https://www.youtube.com/@CUFE_EPE26",
    "https://www.youtube.com/@cufeepe2562",
]

COURSE_SEEDS = [
    {
        "course_name": "Digital Control Systems",
        "course_code": "EPE3090",
        "description": "Digital Control Systems EPE3090",
        "youtube_channel_id": "https://www.youtube.com/@CUFE_EPE_27",
    },
    {
        "course_name": "Economics of Power Generation",
        "course_code": "EPE3080",
        "description": "Economics of Power Generation EPE3080",
        "youtube_channel_id": "https://www.youtube.com/@CUFE_EPE_27",
    },
    {
        "course_name": "Electives",
        "course_code": "ELECTIVES",
        "description": "Electives",
        "youtube_channel_id": "https://www.youtube.com/@CUFE_EPE26",
    },
    {
        "course_name": "Electrical Communication Systems",
        "course_code": "ELC3181",
        "description": "Electrical Communication Systems ELC3181",
        "youtube_channel_id": "https://www.youtube.com/@CUFE_EPE26",
    },
    {
        "course_name": "Electrical Machines3",
        "course_code": "EPE3070",
        "description": "Electrical Machines3 EPE3070",
        "youtube_channel_id": "https://www.youtube.com/@cufeepe2562",
    },
    {
        "course_name": "Power Systems 2",
        "course_code": "EPE3060",
        "description": "Power Systems 2 (3060)",
        "youtube_channel_id": "https://www.youtube.com/@cufeepe2562",
    },
    {
        "course_name": "Protection",
        "course_code": "EPE3100",
        "description": "Protection EPE3100",
        "youtube_channel_id": "https://www.youtube.com/@CUFE_EPE_27",
    },
]


# ---------------------------------------------------------------------------
# Access control decorators
# ---------------------------------------------------------------------------

def admin_only(handler: Callable) -> Callable:
    """Decorator that restricts a command handler to configured admin users.

    Args:
        handler: The async command handler function.

    Returns:
        A wrapped handler that rejects non-admin requests.
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in ADMIN_USER_IDS:
            logger.warning(
                "Unauthorised access attempt by user %s (id=%s).",
                user.username if user else "unknown",
                user.id if user else "N/A",
            )
            if update.message:
                await update.message.reply_text(
                    "⛔ Access denied. This bot is restricted to authorised users only."
                )
            return
        return await handler(update, context)

    return wrapper


def approved_only(handler: Callable) -> Callable:
    """Decorator that allows admins and approved users."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return

        if user.id in ADMIN_USER_IDS or database.is_user_approved(user.id):
            return await handler(update, context)

        logger.warning(
            "Unapproved user access attempt by %s (id=%s).",
            user.username,
            user.id,
        )
        if update.message:
            await update.message.reply_text(
                "❌ You don't have access\\.\n\n"
                "Send `/request <message>` to request access\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    return wrapper


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command — send a welcome message with help text."""
    if update.message is None:
        return

    user = update.effective_user
    if user is None:
        return

    name = escape_markdown(user.first_name or "there")
    is_approved = user.id in ADMIN_USER_IDS or database.is_user_approved(user.id)

    if not is_approved:
        text = (
            f"Hello {name}\\! Welcome to Drive Monitor Bot\\.\n\n"
            "I watch a Google Drive folder and notify you whenever files are added or updated\\.\n\n"
            "To access this bot, please request access:\n"
            "/request <message>\n\n"
            "Example: /request I am a student in EPE 2026"
        )
    else:
        text = (
            f"Hello {name}\\! Welcome to Drive Monitor Bot\\.\n\n"
            "I watch a Google Drive folder and notify you whenever files are added or updated\\.\n\n"
            "Available commands:\n"
            "/list \\- Browse files\n"
            "/search <name> \\- Search files\n"
            "/download <name> \\- Download file\n"
            "/browse <id> \\- Browse folder\n"
            "/monitor \\- Toggle monitoring\n"
            "/status \\- Show statistics\n"
            "/links \\- Show resources\n"
            "/request <msg> \\- Request access\n"
            "/requests \\- Review requests \\(admin\\)\n"
            "/approve <id> \\- Approve user \\(admin\\)\n"
            "/reject <id> \\- Reject user \\(admin\\)\n"
            "/courses \\- Browse courses\n"
            "/course <code> \\- Course details\n"
            "/setup_courses \\- Setup courses \\(admin\\)\n"
            "/extract_youtube \\- Extract YouTube \\(admin\\)\n"
            "/download_youtube <url> \\- Download YouTube video\n"
            "/broadcast <msg> \\- Broadcast \\(admin\\)\n"
            "/broadcast_status \\- Broadcast status \\(admin\\)\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@approved_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /list command — paginated list of tracked files."""
    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]))
        except ValueError:
            pass
    await _send_file_list(update.message.reply_text, page)


@approved_only
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /search <query> command — find files by name substring."""
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /search `<filename>`\n\nExample: `/search report`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query = " ".join(context.args)
    if len(query) < 2:
        await update.message.reply_text("⚠️ Please enter at least 2 characters to search.")
        return

    await update.message.reply_text(f"🔍 Searching for *{escape_markdown(query)}*…",
                                    parse_mode=ParseMode.MARKDOWN_V2)

    # Search local DB first for speed; results may lag behind Drive
    results = db_search_files(query, limit=50)

    if not results:
        await update.message.reply_text(
            f"No files found matching *{escape_markdown(query)}*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = [f"🔍 *Search results for:* `{escape_markdown(query)}`\n"]
    for f in results[:PAGE_SIZE]:
        icon = get_mime_icon(f.get("mime_type"))
        name = escape_markdown(f["name"])
        size = format_size(f.get("size"))
        modified = format_timestamp(f.get("modified_time"))
        lines.append(
            f"{icon} {name}\n"
            f"   📏 {escape_markdown(size)} · 🕐 {escape_markdown(modified)}\n"
        )

    if len(results) > PAGE_SIZE:
        lines.append(f"\n_Showing first {PAGE_SIZE} of {len(results)} results\\._")

    keyboard = build_files_keyboard(results[:PAGE_SIZE], page=0, total_pages=1)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@approved_only
async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /browse <folder_id> command — list contents of a Drive folder."""
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /browse `<folder_id>`\n\n"
            "You can get the folder ID from a folder's Drive link or the /list view\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    folder_id = context.args[0].strip()
    context.user_data["nav_stack"] = [folder_id]
    await _send_folder_contents(update.message.reply_text, folder_id, parent_id="root")


@approved_only
async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /download <filename> command — download a file from Drive."""
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /download `<filename>`\n\nExample: `/download report\\.pdf`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    query = " ".join(context.args)
    results = db_search_files(query, limit=10)

    if not results:
        await update.message.reply_text(
            f"❌ No files found matching *{escape_markdown(query)}*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if len(results) == 1:
        await _process_download(context.bot, update.effective_chat.id, results[0]["file_id"])
        return

    # Multiple matches — let the user pick
    lines = [f"🔍 *Found {len(results)} files matching* `{escape_markdown(query)}`\\:\n"]
    for f in results:
        icon = get_mime_icon(f.get("mime_type"))
        name = escape_markdown(f["name"])
        size = escape_markdown(format_size(f.get("size")))
        lines.append(f"{icon} {name} — {size}")

    keyboard = build_files_keyboard(results, page=0, total_pages=1)
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


@approved_only
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /monitor command — toggle background monitoring."""
    state = get_monitoring_state()
    currently_enabled = bool(state.get("is_enabled", True))
    new_state = not currently_enabled
    set_monitoring_enabled(new_state)

    if new_state:
        await update.message.reply_text(
            "✅ *Monitoring enabled\\.*\n"
            f"I will check for Drive changes every *{POLL_INTERVAL // 60} minutes*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.message.reply_text(
            "⏸️ *Monitoring paused\\.*\n"
            "Send /monitor again to re\\-enable\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


@approved_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /status command — display monitoring statistics."""
    state = get_monitoring_state()
    is_enabled = bool(state.get("is_enabled", True))
    last_poll = format_timestamp(state.get("last_poll"))
    started_at = format_timestamp(state.get("started_at"))
    total_new = state.get("total_new", 0)
    total_updated = state.get("total_updated", 0)
    total_tracked = count_files()

    status_icon = "🟢" if is_enabled else "🔴"
    status_label = "Active" if is_enabled else "Paused"

    text = (
        f"📊 *Bot Status*\n\n"
        f"{status_icon} Monitoring: *{escape_markdown(status_label)}*\n"
        f"⏱️ Poll interval: *{escape_markdown(str(POLL_INTERVAL // 60))} min*\n"
        f"📁 Tracked files: *{escape_markdown(str(total_tracked))}*\n"
        f"🆕 New files detected: *{escape_markdown(str(total_new))}*\n"
        f"♻️ Updated files: *{escape_markdown(str(total_updated))}*\n"
        f"🕐 Last poll: *{escape_markdown(last_poll)}*\n"
        f"🚀 Monitoring started: *{escape_markdown(started_at)}*\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@approved_only
async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /links command — show important resource links."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 EPE 2025", url="https://www.youtube.com/@cufeepe2562"),
            InlineKeyboardButton("📺 EPE 2026", url="https://www.youtube.com/@CUFE_EPE26"),
        ],
        [
            InlineKeyboardButton("📺 EPE 2027", url="https://www.youtube.com/@CUFE_EPE_27"),
        ],
        [
            InlineKeyboardButton("🏫 Faculty Site", url="https://chreg.eng.cu.edu.eg/"),
        ],
    ])

    text = (
        "*📚 Important Resources*\n\n"
        "*📺 YouTube Channels:*\n"
        "• EPE 2025 Channel\n"
        "• EPE 2026 Channel\n"
        "• EPE 2027 Channel\n\n"
        "*🏫 Faculty:*\n"
        "• Engineering Faculty Site"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


def _match_course_for_title(title: str, courses: List[Dict]) -> Optional[Dict]:
    """Find the best matching course for a playlist/video title."""
    title_upper = title.upper()
    for course in courses:
        code = (course.get("course_code") or "").upper()
        name = (course.get("course_name") or "").upper()
        if code and code in title_upper:
            return course
        if name and any(part and part in title_upper for part in name.split()):
            return course
    return None


@admin_only
async def cmd_setup_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create/refresh the predefined 7 courses."""
    for seed in COURSE_SEEDS:
        database.add_course(
            course_name=seed["course_name"],
            course_code=seed["course_code"],
            description=seed["description"],
            youtube_channel_id=seed["youtube_channel_id"],
        )
    await update.message.reply_text(
        "✅ Courses setup complete\\. Added/updated 7 courses\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_extract_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract playlists/videos from configured channels and map to courses."""
    if not YOUTUBE_API_KEY:
        await update.message.reply_text(
            "❌ `YOUTUBE_API_KEY` is missing in your environment\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    courses = database.get_all_courses()
    if not courses:
        await update.message.reply_text(
            "ℹ️ No courses found\\. Run /setup_courses first\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    status_msg = await update.message.reply_text("⏳ Extracting YouTube playlists and videos…")
    service = YouTubeService(YOUTUBE_API_KEY)
    playlist_count = 0
    video_count = 0

    try:
        for channel_url in YOUTUBE_CHANNELS:
            playlists = await service.get_channel_playlists(channel_url)
            for playlist in playlists:
                matched_course = _match_course_for_title(playlist["name"], courses)
                course_id = matched_course["course_id"] if matched_course else None
                videos = await service.get_playlist_videos(playlist["id"])
                database.add_youtube_playlist(
                    playlist_id=playlist["id"],
                    course_id=course_id,
                    playlist_name=playlist["name"],
                    playlist_url=playlist["url"],
                    video_count=len(videos),
                )
                playlist_count += 1
                for video in videos:
                    video_course = matched_course or _match_course_for_title(video["title"], courses)
                    database.add_youtube_video(
                        video_id=video["id"],
                        playlist_id=playlist["id"],
                        course_id=video_course["course_id"] if video_course else course_id,
                        video_title=video["title"],
                        video_url=video["url"],
                        video_order=video["order"],
                        duration=video.get("duration"),
                        thumbnail_url=video.get("thumbnail_url"),
                        view_count=video.get("view_count"),
                    )
                    video_count += 1
    finally:
        await service.close()

    await status_msg.edit_text(
        f"✅ YouTube extraction complete\\.\n"
        f"📺 Playlists processed: *{playlist_count}*\n"
        f"🎬 Videos processed: *{video_count}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"


@approved_only
async def cmd_download_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download YouTube video with quality selection."""
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: `/download_youtube <url>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    url = context.args[0].strip()
    if not _is_youtube_url(url):
        await update.message.reply_text(
            "❌ Invalid YouTube URL\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    status_msg = await update.message.reply_text("⏳ Fetching available qualities…")
    try:
        formats = await _yt_downloader.get_formats(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch YouTube formats for %s: %s", url, exc)
        await status_msg.edit_text(
            "❌ Could not fetch video formats\\. Check the link and try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not formats:
        await status_msg.edit_text(
            "❌ No downloadable formats found for this video\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    request_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    downloads = context.user_data.setdefault("youtube_downloads", {})
    downloads[request_id] = {"url": url, "formats": formats}

    rows = []
    for idx, fmt in enumerate(formats):
        size_label = f" • {format_size(fmt['filesize'])}" if fmt.get("filesize") else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{fmt['label']}{size_label}",
                    callback_data=f"yt:{request_id}:{idx}",
                )
            ]
        )

    await status_msg.edit_text(
        "🎞️ Choose quality:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@approved_only
async def cmd_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all available courses with content counts."""
    courses = database.get_all_courses()
    if not courses:
        await update.message.reply_text(
            "No courses available yet\\. Ask admin to run /setup_courses\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = ["*Available Courses:*\n"]
    for idx, course in enumerate(courses, start=1):
        code = course.get("course_code") or "N/A"
        name = course.get("course_name") or "Unknown"
        lines.append(f"{idx}\\. {code} \\- {name}")

    lines.append("\nUse /course CODE to view details\\.")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@approved_only
async def cmd_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show details for a specific course."""
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: `/course <course_code>`\n\nExample: `/course EPE3090`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    code = context.args[0].upper().strip()
    course = database.get_course_by_code(code)
    if not course:
        await update.message.reply_text(
            f"❌ Course `{escape_markdown(code)}` not found\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    course_id = int(course["course_id"])
    playlists = database.get_course_playlists(course_id)
    drive_matches = db_search_files(code, limit=10)

    lines = [
        f"📚 *{escape_markdown(course['course_name'])}* "
        f"\\({escape_markdown(course.get('course_code') or 'N/A')}\\)\n",
        "📺 *YouTube Content:*",
    ]

    if playlists:
        for playlist in playlists[:8]:
            count = playlist.get("video_count")
            if count is None:
                count = 0
            lines.append(
                f"• {escape_markdown(playlist['playlist_name'])} "
                f"\\({escape_markdown(str(count))} videos\\)"
            )
    else:
        lines.append("• No playlists linked yet\\.")

    lines.append("\n📁 *Drive Materials:*")
    if drive_matches:
        for item in drive_matches[:6]:
            lines.append(f"• {escape_markdown(item['name'])}")
    else:
        lines.append("• No matching Drive materials found\\.")

    keyboard_rows = []
    if playlists:
        for idx, playlist in enumerate(playlists, start=1):
            playlist_url = playlist.get("playlist_url")
            if playlist_url:
                playlist_name = playlist.get("playlist_name", f"Playlist {idx}")
                keyboard_rows.append(
                    [InlineKeyboardButton(f"▶️ {playlist_name}", url=playlist_url)]
                )
    if course.get("drive_folder_id"):
        keyboard_rows.append(
            [InlineKeyboardButton("📁 View Drive Folder", url=drive_view_link(course["drive_folder_id"]))]
        )
    keyboard_rows.append(
        [InlineKeyboardButton("✅ Enroll in Course", callback_data=f"enroll:{course_id}")]
    )
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /broadcast <message> command."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <message>\n\nExample: /broadcast New lecture notes\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    message = " ".join(context.args).strip()
    context.user_data["broadcast_message"] = message
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("All Users", callback_data="bcast:all")],
        [InlineKeyboardButton("Select Course", callback_data="bcast:course")],
        [InlineKeyboardButton("Cancel", callback_data="bcast:cancel")],
    ])

    await update.message.reply_text(
        f"Broadcast Message\n\nMessage: {message}\n\nSend to:",
        parse_mode=None,
        reply_markup=keyboard,
    )


@admin_only
async def cmd_broadcast_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent broadcast delivery status."""
    broadcasts = database.get_recent_broadcasts(limit=10)
    if not broadcasts:
        await update.message.reply_text("📭 No broadcasts sent yet\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = ["📊 *Recent Broadcasts:*\n"]
    for idx, item in enumerate(broadcasts, start=1):
        target = "All Users"
        if item.get("target_type") == "course":
            target = f"{item.get('course_code') or item.get('course_name') or 'Course'} Only"
        lines.append(
            f"{idx}\\. \"{escape_markdown(item['message_text'][:80])}\"\n"
            f"   Sent: {escape_markdown(str(item.get('sent_at') or 'Unknown'))}\n"
            f"   Delivered: {escape_markdown(str(item.get('delivery_count') or 0))} users\n"
            f"   Target: {escape_markdown(target)}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /request <message> command for access requests."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    if user.id in ADMIN_USER_IDS or database.is_user_approved(user.id):
        await update.message.reply_text(
            "✅ You already have access\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: `/request <message>`\n\n"
            "Example: `/request I am a student in EPE 2026`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    request_message = " ".join(context.args).strip()
    database.submit_access_request(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        message=request_message,
    )

    await update.message.reply_text(
        "✅ Your request has been sent\\!\n"
        "The admin will review it soon\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    requested_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    admin_text = (
        "📥 *New access request\\!*\n\n"
        f"👤 {escape_markdown(user.first_name or 'Unknown')} "
        f"\\(ID: `{user.id}`\\)\n"
        f"Message: \"{escape_markdown(request_message)}\"\n"
        f"Requested: {escape_markdown(requested_at)}\n\n"
        "Check with: /requests"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to notify admin %s about request from %s: %s",
                           admin_id, user.id, exc)


@admin_only
async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /requests command — show pending access requests."""
    pending = database.get_pending_requests()
    if not pending:
        await update.message.reply_text("✅ No pending access requests.")
        return

    await update.message.reply_text(f"📥 Pending requests: {len(pending)}")
    for idx, req in enumerate(pending, start=1):
        user_id = req.get("user_id")
        display_name = req.get("first_name") or req.get("username") or f"User {user_id}"
        requested_at = req.get("requested_at") or "Unknown"
        message = req.get("message") or "-"
        text = (
            f"{idx}️⃣ *{escape_markdown(str(display_name))}* \\(ID: `{user_id}`\\)\n"
            f"Message: \"{escape_markdown(str(message))}\"\n"
            f"Requested: {escape_markdown(str(requested_at))}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{user_id}"),
        ]])
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )


@admin_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /approve <user_id> command."""
    await _handle_request_decision(update, context, approve=True)


@admin_only
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reject <user_id> command."""
    await _handle_request_decision(update, context, approve=False)


# ---------------------------------------------------------------------------
# Callback query handler for pagination
# ---------------------------------------------------------------------------

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks (pagination, download, and folder browsing)."""
    query = update.callback_query
    user = update.effective_user
    if user is None:
        return
    if user.id not in ADMIN_USER_IDS and not database.is_user_approved(user.id):
        await query.answer("You don't have access. Use /request first.", show_alert=True)
        return
    await query.answer()

    data: str = query.data or ""

    if data == "noop":
        return

    if data.startswith("list:"):
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await _send_file_list(query.edit_message_text, page)

    elif data.startswith("download:"):
        file_id = data.split(":", 1)[1]
        await _process_download(context.bot, query.message.chat_id, file_id)

    elif data.startswith("yt:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid format selection.", show_alert=True)
            return
        request_id, fmt_index_raw = parts[1], parts[2]
        downloads = context.user_data.get("youtube_downloads", {})
        payload = downloads.get(request_id)
        if not payload:
            await query.answer("This download request expired. Please retry.", show_alert=True)
            return
        try:
            fmt_index = int(fmt_index_raw)
            selected = payload["formats"][fmt_index]
        except (TypeError, ValueError, IndexError, KeyError):
            await query.answer("Invalid format.", show_alert=True)
            return

        await query.edit_message_text("⏳ Downloading selected format…")
        downloaded_path: Optional[str] = None
        try:
            result = await _yt_downloader.download(
                url=payload["url"],
                format_id=selected["format_id"],
                ext_hint=selected.get("ext", ""),
                audio_only=bool(selected.get("audio_only")),
            )
            if not result:
                await query.edit_message_text(
                    "❌ Could not download this format\\. Please try another one\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return

            downloaded_path = result["path"]
            file_size = os.path.getsize(downloaded_path)
            if file_size > MAX_FILE_SIZE:
                acceptable_formats = [
                    fmt for fmt in payload["formats"]
                    if fmt.get("filesize") and int(fmt["filesize"]) < MAX_FILE_SIZE
                ]
                suggestion = (
                    f"\nTry: {acceptable_formats[-1]['label']}"
                    if acceptable_formats
                    else "\nTry a lower quality option."
                )
                await query.edit_message_text(
                    (
                        f"⚠️ Downloaded file is too large for Telegram "
                        f"\\({escape_markdown(format_size(file_size))} > 50 MB\\)\\.{suggestion}"
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return

            caption = (
                f"🎬 *{escape_markdown(result.get('title', 'YouTube Video'))}*\n"
                f"📦 {escape_markdown(format_size(file_size))}\n"
                f"🎞️ {escape_markdown(selected['label'])}"
            )
            await query.edit_message_text("📤 Uploading to Telegram…")
            with open(downloaded_path, "rb") as fh:
                if selected.get("audio_only"):
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=fh,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        filename=os.path.basename(downloaded_path),
                    )
                else:
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=fh,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
            await query.edit_message_text("✅ Download complete.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube download failed for callback %s: %s", data, exc)
            await query.edit_message_text(
                "❌ Download failed\\. Please try another format or link\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        finally:
            if downloaded_path and os.path.exists(downloaded_path):
                try:
                    os.remove(downloaded_path)
                except OSError as exc:
                    logger.debug("Failed to remove YouTube temp file %s: %s", downloaded_path, exc)
            downloads.pop(request_id, None)

    elif data.startswith("folder:"):
        # Open a subfolder — push it onto the nav stack, determine parent before appending
        folder_id = data.split(":", 1)[1]
        nav_stack: list = context.user_data.setdefault("nav_stack", [])
        # The parent for the back button is the current top of the stack (where we came from)
        parent_id = nav_stack[-1] if nav_stack else "root"
        nav_stack.append(folder_id)
        await _send_folder_contents(query.edit_message_text, folder_id, parent_id=parent_id)

    elif data.startswith("back:"):
        target_folder_id = data.split(":", 1)[1]
        nav_stack = context.user_data.get("nav_stack", [])
        # Pop the current folder off the stack
        if nav_stack:
            nav_stack.pop()
        # "back:root" means "return to /list"
        if target_folder_id == "root":
            context.user_data["nav_stack"] = []
            await _send_file_list(query.edit_message_text, 0)
        else:
            # Display the target folder; its own parent is the item before it in the stack
            grandparent_id = nav_stack[-2] if len(nav_stack) >= 2 else "root"
            await _send_folder_contents(query.edit_message_text, target_folder_id, parent_id=grandparent_id)

    elif data == "home":
        context.user_data["nav_stack"] = []
        await _send_file_list(query.edit_message_text, 0)

    elif data.startswith("approve:") or data.startswith("reject:"):
        if user.id not in ADMIN_USER_IDS:
            await query.answer("Only admins can review requests.", show_alert=True)
            return
        approve = data.startswith("approve:")
        try:
            target_user_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Invalid request user ID.", show_alert=True)
            return

        await _resolve_access_request(
            context=context,
            admin_user_id=user.id,
            target_user_id=target_user_id,
            approve=approve,
        )
        action_label = "approved" if approve else "rejected"
        await query.edit_message_text(
            f"✅ Request for user `{target_user_id}` marked as *{action_label}*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif data.startswith("enroll:"):
        try:
            course_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Invalid course ID.", show_alert=True)
            return
        inserted = database.enroll_user_in_course(user_id=user.id, course_id=course_id)
        course = database.get_course_by_id(course_id)
        course_name = course["course_name"] if course else "course"
        if inserted:
            await query.answer("Enrolled successfully.", show_alert=True)
            await query.message.reply_text(
                f"✅ You are now enrolled in *{escape_markdown(course_name)}*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await query.answer("Already enrolled.", show_alert=True)

    elif data == "bcast:cancel":
        context.user_data.pop("broadcast_message", None)
        await query.edit_message_text("❌ Broadcast cancelled\\.", parse_mode=ParseMode.MARKDOWN_V2)

    elif data == "bcast:all":
        if user.id not in ADMIN_USER_IDS:
            await query.answer("Only admins can broadcast.", show_alert=True)
            return
        message = context.user_data.get("broadcast_message")
        if not message:
            await query.answer("Broadcast message expired. Retry /broadcast.", show_alert=True)
            return
        recipients = sorted(set(list(ADMIN_USER_IDS) + database.get_approved_users()))
        sent = 0
        for recipient in recipients:
            try:
                await context.bot.send_message(
                    chat_id=recipient,
                    text=f"📢 *Broadcast*\n\n{escape_markdown(message)}",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Broadcast to %s failed: %s", recipient, exc)
        database.send_broadcast(
            sender_id=user.id,
            message_text=message,
            target_type="all",
            delivery_count=sent,
        )
        context.user_data.pop("broadcast_message", None)
        await query.edit_message_text(
            f"✅ Broadcast sent to *{sent}* users\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif data == "bcast:course":
        if user.id not in ADMIN_USER_IDS:
            await query.answer("Only admins can broadcast.", show_alert=True)
            return
        courses = database.get_all_courses()
        if not courses:
            await query.answer("No courses found.", show_alert=True)
            return
        rows = []
        for course in courses:
            label = course.get("course_code") or course.get("course_name")
            rows.append(
                [InlineKeyboardButton(f"📚 {label}", callback_data=f"bcast_course:{course['course_id']}")]
            )
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bcast:cancel")])
        await query.edit_message_text(
            "Select target course:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("bcast_course:"):
        if user.id not in ADMIN_USER_IDS:
            await query.answer("Only admins can broadcast.", show_alert=True)
            return
        message = context.user_data.get("broadcast_message")
        if not message:
            await query.answer("Broadcast message expired. Retry /broadcast.", show_alert=True)
            return
        try:
            course_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Invalid course.", show_alert=True)
            return
        recipients = sorted(set(database.get_course_enrolled_users(course_id)))
        sent = 0
        course = database.get_course_by_id(course_id)
        course_label = (course.get("course_code") or course.get("course_name")) if course else "course"
        for recipient in recipients:
            try:
                await context.bot.send_message(
                    chat_id=recipient,
                    text=(
                        f"📢 *Course Broadcast* \\({escape_markdown(course_label)}\\)\n\n"
                        f"{escape_markdown(message)}"
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Course broadcast to %s failed: %s", recipient, exc)
        database.send_broadcast(
            sender_id=user.id,
            message_text=message,
            target_type="course",
            target_course_id=course_id,
            delivery_count=sent,
        )
        context.user_data.pop("broadcast_message", None)
        await query.edit_message_text(
            f"✅ Course broadcast sent to *{sent}* users "
            f"\\({escape_markdown(course_label)}\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _handle_request_decision(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   approve: bool) -> None:
    """Handle /approve and /reject commands."""
    if update.message is None:
        return

    if not context.args:
        command = "approve" if approve else "reject"
        await update.message.reply_text(
            f"ℹ️ Usage: `/{command} <user_id>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return

    resolved = await _resolve_access_request(
        context=context,
        admin_user_id=update.effective_user.id if update.effective_user else None,
        target_user_id=target_user_id,
        approve=approve,
    )
    if not resolved:
        await update.message.reply_text(
            f"❌ No request found for user `{target_user_id}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    action_label = "approved" if approve else "rejected"
    await update.message.reply_text(
        f"✅ User `{target_user_id}` {action_label}\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _resolve_access_request(context: ContextTypes.DEFAULT_TYPE,
                                  admin_user_id: Optional[int],
                                  target_user_id: int,
                                  approve: bool) -> bool:
    """Approve or reject a request and notify the user."""
    request = database.get_access_request(target_user_id)
    if request is None:
        return False

    if approve:
        changed = database.approve_request(target_user_id, reviewed_by=admin_user_id)
    else:
        changed = database.reject_request(target_user_id, reviewed_by=admin_user_id)

    if not changed:
        return False

    if approve:
        text = (
            "✅ Your access has been approved\\!\n"
            "You can now use: /list, /search, /download, /monitor, /status, /links\\."
        )
    else:
        text = "❌ Your request was rejected\\."

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not notify user %s after request review: %s", target_user_id, exc)

    return True


# ---------------------------------------------------------------------------
# File download helper
# ---------------------------------------------------------------------------

async def _process_download(bot, chat_id: int, file_id: str) -> None:
    """Download a file from Google Drive and send it to a Telegram chat.

    Workflow:
    1. Fetch file metadata from Drive.
    2. If it is a Google Workspace file, send the Drive link instead.
    3. If the file exceeds the 50 MB Telegram limit, send a warning + link.
    4. Otherwise download to a temp file, detect the file category, and send
       it to Telegram as the appropriate media type.
    5. Clean up the temp file regardless of outcome.

    Args:
        bot: The :class:`telegram.Bot` instance.
        chat_id: Telegram chat ID to send the file to.
        file_id: Google Drive file ID.
    """
    try:
        status_msg = await bot.send_message(chat_id=chat_id, text="⏳ Fetching file info…")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not send status message for download of '%s': %s", file_id, exc)
        return

    temp_path: Optional[str] = None

    try:
        metadata = await _drive.get_file_metadata(file_id)
        if metadata is None:
            await status_msg.edit_text("❌ File not found on Google Drive\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        file_name: str = metadata.get("name", "file")
        mime_type: str = metadata.get("mimeType", "") or ""
        size_raw = metadata.get("size")
        size: Optional[int] = int(size_raw) if size_raw else None
        modified_time: Optional[str] = metadata.get("modifiedTime")
        view_url: str = metadata.get("webViewLink") or drive_view_link(file_id)

        # Google Workspace files cannot be downloaded with get_media
        if mime_type.startswith("application/vnd.google-apps."):
            await status_msg.edit_text(
                f"ℹ️ *{escape_markdown(file_name)}* is a Google Workspace file and "
                f"cannot be downloaded directly\\.\n\n"
                f"[Open in Google Drive]({view_url})",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=False,
            )
            return

        # Enforce the 50 MB Telegram upload limit
        if size is not None and size > MAX_FILE_SIZE:
            await status_msg.edit_text(
                f"⚠️ *{escape_markdown(file_name)}* is too large to send via Telegram\\.\n"
                f"📏 Size: {escape_markdown(format_size(size))} \\(limit: 50 MB\\)\n\n"
                f"[Open in Google Drive]({view_url})",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=False,
            )
            return

        await status_msg.edit_text("⏳ Downloading from Google Drive…")

        try:
            file_bytes = await asyncio.wait_for(
                _drive.download_file(file_id),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                f"⏱️ Download timed out\\. [Open in Drive]({view_url})",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        if file_bytes is None:
            await status_msg.edit_text(
                f"❌ Could not download the file\\. [Open in Drive]({view_url})",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Write to a temporary file to avoid memory pressure on large files
        suffix = os.path.splitext(file_name)[1] or ""
        fd, temp_path = tempfile.mkstemp(suffix=suffix, dir=TEMP_DIR)
        with os.fdopen(fd, "wb") as fh:
            fh.write(file_bytes)

        caption = (
            f"📁 *{escape_markdown(file_name)}*\n"
            f"📏 {escape_markdown(format_size(size if size is not None else len(file_bytes)))}\n"
            f"🕐 {escape_markdown(format_timestamp(modified_time))}"
        )

        await status_msg.edit_text("📤 Uploading to Telegram…")

        category = get_file_category(mime_type)
        with open(temp_path, "rb") as fh:
            if category == "photo":
                await bot.send_photo(
                    chat_id=chat_id, photo=fh,
                    caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
                )
            elif category == "video":
                await bot.send_video(
                    chat_id=chat_id, video=fh,
                    caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
                )
            elif category == "audio":
                await bot.send_audio(
                    chat_id=chat_id, audio=fh,
                    caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
                    filename=file_name,
                )
            else:
                await bot.send_document(
                    chat_id=chat_id, document=fh,
                    caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
                    filename=file_name,
                )

        await status_msg.delete()

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Error downloading file '%s': %s", file_id, exc, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ An error occurred while downloading the file\\.\n"
                f"[Open in Google Drive]({drive_view_link(file_id)})",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as inner_exc:  # noqa: BLE001
            logger.debug("Could not edit status message after download error: %s", inner_exc)

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as exc:
                logger.warning("Could not remove temp file '%s': %s", temp_path, exc)



# ---------------------------------------------------------------------------
# Folder contents renderer
# ---------------------------------------------------------------------------

async def _send_folder_contents(reply_fn, folder_id: str, parent_id: str) -> None:
    """Fetch and display the contents of a Drive folder with navigation buttons.

    Args:
        reply_fn: Callable used to send or edit the message.
        folder_id: Google Drive folder ID to list.
        parent_id: Drive folder ID of the parent folder (for the Back button),
            or the sentinel string ``"root"`` to return to the /list view.
    """
    items = await _drive.list_folder_contents(folder_id)

    if items is None:
        await reply_fn("❌ Cannot access this folder\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Fetch folder name for header
    folder_meta = await _drive.get_file_metadata(folder_id)
    folder_name = folder_meta.get("name", "Folder") if folder_meta else "Folder"

    if not items:
        # Empty folder
        nav_keyboard = build_folder_keyboard([], parent_id=parent_id)
        await reply_fn(
            f"📂 *{escape_markdown(folder_name)}*\n\n_This folder is empty\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=nav_keyboard,
        )
        return

    lines = [f"📂 *{escape_markdown(folder_name)}*\n"]
    for item in items:
        mime = item.get("mimeType", "")
        name = escape_markdown(item.get("name", "Unnamed"))
        size = format_size(item.get("size"))
        if mime == _FOLDER_MIME_TYPE:
            lines.append(f"📁 {name} \\(Folder\\)")
        else:
            icon = get_mime_icon(mime)
            lines.append(f"{icon} {name} — {escape_markdown(size)}")

    text = truncate_message("\n".join(lines))
    keyboard = build_folder_keyboard(items, parent_id=parent_id)

    await reply_fn(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Shared file-list renderer
# ---------------------------------------------------------------------------

async def _send_file_list(reply_fn, page: int) -> None:
    """Build and send a paginated file list message with download buttons.

    Args:
        reply_fn: Callable used to send or edit the message
            (e.g. ``message.reply_text`` or ``query.edit_message_text``).
        page: Zero-based page number to display.
    """
    all_files = get_all_files(limit=1000)
    total = len(all_files)

    if total == 0:
        await reply_fn(
            "📭 No files are currently tracked\\. "
            "Monitoring will populate this list automatically\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    import math
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    page_files = all_files[start: start + PAGE_SIZE]

    lines = [f"📁 *Monitored Folder* — Page {page + 1}/{total_pages} \\({total} files\\)\n"]
    for f in page_files:
        icon = get_mime_icon(f.get("mime_type"))
        name = escape_markdown(f["name"])
        size = format_size(f.get("size"))
        lines.append(
            f"{icon} {name} — {escape_markdown(size)}"
        )

    text = truncate_message("\n".join(lines))
    keyboard = build_files_keyboard(page_files, page, total_pages)

    await reply_fn(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Auto-download helpers
# ---------------------------------------------------------------------------

def _is_downloadable_file(mime_type: Optional[str]) -> bool:
    """Return ``True`` if the MIME type represents a downloadable file.

    Folders and Google Workspace application types (Docs, Sheets, Slides, etc.)
    cannot be fetched with the Drive ``get_media`` endpoint and are excluded.

    Args:
        mime_type: MIME type string from the Drive API, or ``None``.

    Returns:
        ``True`` when the file can be downloaded directly.
    """
    if not mime_type:
        return False
    return not mime_type.startswith("application/vnd.google-apps.")


async def _download_and_send_file(
    app: Application,
    admin_ids: list,
    file_id: str,
    file_name: str,
    file_size: int,
    mime_type: str,
    modified_time: Optional[str],
) -> None:
    """Download a file from Drive and send it to all admin users.

    Writes the file content to a temporary file, determines the correct
    Telegram send method from the MIME type, and sends the file to every
    admin ID.  The temporary file is always removed afterwards.

    Args:
        app: The running :class:`telegram.ext.Application` instance.
        admin_ids: List of Telegram user IDs to notify.
        file_id: Google Drive file ID.
        file_name: Human-readable file name (used as filename and in caption).
        file_size: File size in bytes (used in caption).
        mime_type: MIME type of the file.
        modified_time: ISO-8601 modification timestamp, or ``None``.

    Raises:
        asyncio.TimeoutError: If the download exceeds :data:`DOWNLOAD_TIMEOUT`.
        Exception: Any other error during download or upload is re-raised so
            the caller can fall back to sending a text notification.
    """
    logger.info(
        "Downloading file '%s' (%s) for auto-notification…",
        file_name,
        format_size(file_size),
    )

    file_bytes = await asyncio.wait_for(
        _drive.download_file(file_id),
        timeout=DOWNLOAD_TIMEOUT,
    )

    if file_bytes is None:
        logger.warning("File '%s' not found on Drive (download returned None).", file_name)
        return

    # Use an in-memory buffer so the bytes are only read once regardless of
    # the number of admin users to notify.
    buf = io.BytesIO(file_bytes)
    caption = (
        f"📄 *{escape_markdown(file_name)}*\n"
        f"📏 {escape_markdown(format_size(file_size))}\n"
        f"🕐 {escape_markdown(format_timestamp(modified_time))}"
    )
    category = get_file_category(mime_type)

    for admin_id in admin_ids:
        buf.seek(0)
        try:
            if category == "photo":
                await app.bot.send_photo(
                    chat_id=admin_id,
                    photo=buf,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            elif category == "video":
                await app.bot.send_video(
                    chat_id=admin_id,
                    video=buf,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            elif category == "audio":
                await app.bot.send_audio(
                    chat_id=admin_id,
                    audio=buf,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    filename=file_name,
                )
            else:
                await app.bot.send_document(
                    chat_id=admin_id,
                    document=buf,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    filename=file_name,
                )
            logger.info("Sent file '%s' to admin %d.", file_name, admin_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send file to admin %d: %s", admin_id, exc)


# ---------------------------------------------------------------------------
# Background monitoring task
# ---------------------------------------------------------------------------

async def _polling_task(app: Application) -> None:
    """Background coroutine that polls Google Drive for file changes.

    Runs indefinitely, sleeping for :data:`POLL_INTERVAL` seconds between
    cycles.  Each cycle lists all files in the folder, upserts them into
    the database, and sends change notifications to all admin users.

    Args:
        app: The running :class:`telegram.ext.Application` instance.
    """
    global _drive
    logger.info("Drive polling task started. Interval: %ds.", POLL_INTERVAL)

    while True:
        try:
            state = get_monitoring_state()
            if not state.get("is_enabled", True):
                logger.debug("Monitoring is disabled — skipping poll cycle.")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            logger.info("Starting Drive poll cycle.")
            files = await _drive.list_files(force_refresh=True)

            new_count = 0
            updated_count = 0
            seen_ids = []

            for file in files:
                file_id = file.get("id", "")
                if not file_id:
                    continue
                seen_ids.append(file_id)

                name = file.get("name", "Unnamed")
                size_raw = file.get("size")
                size = int(size_raw) if size_raw is not None else None
                mime_type = file.get("mimeType")
                modified_time = file.get("modifiedTime")
                version = file.get("version")
                web_view_link = file.get("webViewLink", "")

                status = upsert_file(
                    file_id=file_id,
                    name=name,
                    size=size,
                    mime_type=mime_type,
                    modified_time=modified_time,
                    version=version,
                )

                if status in ("new", "updated"):
                    action = "🆕 NEW" if status == "new" else "♻️ UPDATED"
                    if status == "new":
                        new_count += 1
                    else:
                        updated_count += 1

                    # Try to download and send the actual file instead of a text notification
                    can_download = _is_downloadable_file(mime_type) and size is not None
                    file_sent = False

                    if can_download and size < MAX_FILE_SIZE:
                        try:
                            await _download_and_send_file(
                                app=app,
                                admin_ids=ADMIN_USER_IDS,
                                file_id=file_id,
                                file_name=name,
                                file_size=size,
                                mime_type=mime_type,
                                modified_time=modified_time,
                            )
                            file_sent = True
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Failed to download/send file '%s': %s. Sending link instead.",
                                name,
                                exc,
                            )
                    elif can_download and size >= MAX_FILE_SIZE:
                        logger.info(
                            "File '%s' too large (%s). Sending Drive link instead.",
                            name,
                            format_size(size),
                        )

                    if file_sent:
                        continue

                    # Fallback: send a text notification with a Drive link
                    icon = get_mime_icon(mime_type)
                    msg = (
                        f"{action} file in Drive\\!\n\n"
                        f"{icon} *{escape_markdown(name)}*\n"
                        f"📁 Type: {escape_markdown(get_mime_label(mime_type))}\n"
                        f"📏 Size: {escape_markdown(format_size(size))}\n"
                        f"🕐 Modified: {escape_markdown(format_timestamp(modified_time))}\n"
                    )
                    keyboard = build_file_keyboard(file_id, web_view_link)

                    for admin_id in ADMIN_USER_IDS:
                        try:
                            await app.bot.send_message(
                                chat_id=admin_id,
                                text=msg,
                                parse_mode=ParseMode.MARKDOWN_V2,
                                reply_markup=keyboard,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Could not notify admin %d: %s", admin_id, exc
                            )

            # Remove records for files that no longer exist in the folder
            delete_stale_files(seen_ids)
            record_poll(new_count, updated_count)
            _drive.invalidate_cache()

            logger.info(
                "Poll cycle complete. New: %d, Updated: %d, Total tracked: %d.",
                new_count,
                updated_count,
                count_files(),
            )

        except asyncio.CancelledError:
            logger.info("Drive polling task cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in polling task: %s", exc, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers.

    Args:
        update: The incoming update (may be ``None``).
        context: The callback context carrying the exception.
    """
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Called after the application is initialised.

    Creates the database schema and starts the background polling task.

    Args:
        app: The :class:`telegram.ext.Application` instance.
    """
    global _drive
    init_db()
    _drive = GoogleDriveService()
    # Schedule the polling coroutine as a background task
    asyncio.create_task(_polling_task(app))
    logger.info("Bot initialised. Polling task scheduled.")


def main() -> None:
    """Build and run the Telegram bot application."""
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("request", cmd_request))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("setup_courses", cmd_setup_courses))
    app.add_handler(CommandHandler("extract_youtube", cmd_extract_youtube))
    app.add_handler(CommandHandler("download_youtube", cmd_download_youtube))
    app.add_handler(CommandHandler("courses", cmd_courses))
    app.add_handler(CommandHandler("course", cmd_course))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("broadcast_status", cmd_broadcast_status))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram Drive Monitor Bot…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
