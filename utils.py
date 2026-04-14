"""
Utility functions for Telegram Drive Monitor.

Covers human-readable formatting, message truncation, Telegram markdown
escaping, Google Drive link construction, and inline keyboard helpers.
"""

import math
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import PAGE_SIZE, TELEGRAM_MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Size formatting
# ---------------------------------------------------------------------------

_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def format_size(size_bytes: Optional[int]) -> str:
    """Convert a byte count to a human-readable string.

    Args:
        size_bytes: File size in bytes, or ``None`` / negative values.

    Returns:
        A string such as ``"4.2 MB"`` or ``"N/A"``.

    Examples:
        >>> format_size(0)
        '0 B'
        >>> format_size(1536)
        '1.5 KB'
        >>> format_size(None)
        'N/A'
    """
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes == 0:
        return "0 B"
    unit_index = min(int(math.log(size_bytes, 1024)), len(_SIZE_UNITS) - 1)
    value = size_bytes / (1024 ** unit_index)
    if unit_index == 0:
        return f"{size_bytes} B"
    return f"{value:.1f} {_SIZE_UNITS[unit_index]}"


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def format_timestamp(iso_str: Optional[str]) -> str:
    """Convert an ISO-8601 timestamp to a user-friendly UTC string.

    Args:
        iso_str: ISO-8601 datetime string (e.g. ``"2024-01-15T10:30:00.000Z"``).

    Returns:
        A string like ``"2024-01-15 10:30 UTC"`` or ``"N/A"`` on failure.
    """
    if not iso_str:
        return "N/A"
    try:
        # Handle both 'Z' suffix and '+00:00' offset
        clean = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return iso_str


# ---------------------------------------------------------------------------
# MIME type display
# ---------------------------------------------------------------------------

_MIME_ICONS: dict = {
    "application/pdf": "📄",
    "application/zip": "🗜️",
    "application/x-zip-compressed": "🗜️",
    "application/msword": "📝",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "📝",
    "application/vnd.ms-excel": "📊",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "📊",
    "application/vnd.ms-powerpoint": "📽️",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "📽️",
    "application/vnd.google-apps.document": "📝",
    "application/vnd.google-apps.spreadsheet": "📊",
    "application/vnd.google-apps.presentation": "📽️",
    "application/vnd.google-apps.folder": "📁",
    "text/plain": "📃",
    "text/csv": "📊",
    "image/jpeg": "🖼️",
    "image/png": "🖼️",
    "image/gif": "🖼️",
    "image/svg+xml": "🖼️",
    "video/mp4": "🎬",
    "audio/mpeg": "🎵",
    "audio/mp3": "🎵",
}

_MIME_LABELS: dict = {
    "application/pdf": "PDF",
    "application/zip": "ZIP",
    "application/x-zip-compressed": "ZIP",
    "application/msword": "Word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.ms-excel": "Excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.ms-powerpoint": "PowerPoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.folder": "Folder",
    "text/plain": "Text",
    "text/csv": "CSV",
}


def get_mime_icon(mime_type: Optional[str]) -> str:
    """Return an emoji icon for a MIME type.

    Args:
        mime_type: MIME type string.

    Returns:
        An emoji string, defaulting to ``"📦"``.
    """
    if not mime_type:
        return "📦"
    if mime_type in _MIME_ICONS:
        return _MIME_ICONS[mime_type]
    if mime_type.startswith("image/"):
        return "🖼️"
    if mime_type.startswith("video/"):
        return "🎬"
    if mime_type.startswith("audio/"):
        return "🎵"
    if mime_type.startswith("text/"):
        return "📃"
    return "📦"


def get_mime_label(mime_type: Optional[str]) -> str:
    """Return a short human-readable label for a MIME type.

    Args:
        mime_type: MIME type string.

    Returns:
        Label string such as ``"PDF"`` or the raw MIME type if unknown.
    """
    if not mime_type:
        return "Unknown"
    return _MIME_LABELS.get(mime_type, mime_type.split("/")[-1].upper())


# ---------------------------------------------------------------------------
# Google Drive link helpers
# ---------------------------------------------------------------------------

def drive_view_link(file_id: str) -> str:
    """Construct the Google Drive web-view URL for a file.

    Args:
        file_id: Google Drive file ID.

    Returns:
        URL string.
    """
    return f"https://drive.google.com/file/d/{file_id}/view"


def drive_download_link(file_id: str) -> str:
    """Construct a direct Google Drive download URL for a file.

    Args:
        file_id: Google Drive file ID.

    Returns:
        URL string.
    """
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ---------------------------------------------------------------------------
# Telegram markdown helpers
# ---------------------------------------------------------------------------

# Characters that must be escaped in Telegram MarkdownV2
_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 formatting.

    Args:
        text: Plain text to escape.

    Returns:
        Escaped string safe for use in MarkdownV2 messages.
    """
    return _MD_SPECIAL.sub(r"\\\1", text)


def truncate_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> str:
    """Truncate *text* to fit Telegram's message length limit.

    Args:
        text: The message text.
        max_length: Maximum character count (default 4096).

    Returns:
        The original text if short enough, otherwise truncated with an
        ellipsis suffix.
    """
    if len(text) <= max_length:
        return text
    suffix = "\n\n… *(message truncated)*"
    return text[: max_length - len(suffix)] + suffix


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> Tuple[list, int, int]:
    """Slice *items* for a specific page.

    Args:
        items: Full list of items.
        page: Zero-based page index.
        page_size: Number of items per page.

    Returns:
        A tuple of ``(page_items, current_page, total_pages)``.
    """
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start : start + page_size], page, total_pages


def build_pagination_keyboard(
    callback_prefix: str,
    current_page: int,
    total_pages: int,
) -> Optional[InlineKeyboardMarkup]:
    """Create an inline keyboard with Previous / Next navigation buttons.

    Args:
        callback_prefix: Prefix string for callback data (e.g. ``"list"``).
        current_page: Zero-based index of the current page.
        total_pages: Total number of pages.

    Returns:
        An :class:`InlineKeyboardMarkup` with navigation buttons, or
        ``None`` if there is only one page.
    """
    if total_pages <= 1:
        return None

    buttons: List[InlineKeyboardButton] = []

    if current_page > 0:
        buttons.append(
            InlineKeyboardButton(
                "◀ Previous", callback_data=f"{callback_prefix}:{current_page - 1}"
            )
        )

    buttons.append(
        InlineKeyboardButton(
            f"{current_page + 1}/{total_pages}", callback_data="noop"
        )
    )

    if current_page < total_pages - 1:
        buttons.append(
            InlineKeyboardButton(
                "Next ▶", callback_data=f"{callback_prefix}:{current_page + 1}"
            )
        )

    return InlineKeyboardMarkup([buttons])


def build_file_keyboard(file_id: str, web_view_link: Optional[str] = None) -> InlineKeyboardMarkup:
    """Create an inline keyboard with a link to open a file in Drive.

    Args:
        file_id: Google Drive file ID (used as fallback if *web_view_link* is
            absent).
        web_view_link: Direct web-view URL from the Drive API.

    Returns:
        An :class:`InlineKeyboardMarkup` with an "Open in Drive" button.
    """
    url = web_view_link or drive_view_link(file_id)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Open in Drive", url=url)]]
    )
