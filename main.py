"""
Main entry point for Telegram Drive Monitor Bot.

Initialises the Telegram application, registers command handlers,
and starts the background Drive polling task.
"""

import asyncio
import functools
import logging
from typing import Callable, Optional

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
    PAGE_SIZE,
    POLL_INTERVAL,
    TELEGRAM_BOT_TOKEN,
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
from utils import (
    build_file_keyboard,
    build_pagination_keyboard,
    escape_markdown,
    format_size,
    format_timestamp,
    get_mime_icon,
    get_mime_label,
    truncate_message,
)

logger = logging.getLogger(__name__)

# Module-level Drive service instance (created in post_init)
_drive: Optional[GoogleDriveService] = None


# ---------------------------------------------------------------------------
# Admin access decorator
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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command — send a welcome message with help text."""
    user = update.effective_user
    name = user.first_name if user else "there"
    text = (
        f"👋 Hello, *{escape_markdown(name)}*\\! Welcome to *Drive Monitor Bot*\\.\n\n"
        "I watch a Google Drive folder and notify you whenever files are added or updated\\.\n\n"
        "*Available commands:*\n"
        "• /start — Show this help message\n"
        "• /list — Browse files in the monitored folder\n"
        "• /search `<filename>` — Search for a file by name\n"
        "• /monitor — Toggle monitoring on or off\n"
        "• /status — Show monitoring statistics\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /list command — paginated list of tracked files."""
    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]))
        except ValueError:
            pass
    await _send_file_list(update.message.reply_text, page)


@admin_only
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
        file_id = f["file_id"]
        view_url = f"https://drive.google.com/file/d/{file_id}/view"
        lines.append(
            f"{icon} [{name}]({view_url})\n"
            f"   📏 {escape_markdown(size)} · 🕐 {escape_markdown(modified)}\n"
        )

    if len(results) > PAGE_SIZE:
        lines.append(f"\n_Showing first {PAGE_SIZE} of {len(results)} results\\._")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


@admin_only
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /monitor command — toggle background monitoring."""
    state = get_monitoring_state()
    currently_enabled = bool(state.get("is_enabled", True))
    new_state = not currently_enabled
    set_monitoring_enabled(new_state)

    if new_state:
        await update.message.reply_text(
            "✅ *Monitoring enabled\\.*\n"
            f"I will check for Drive changes every *{POLL_INTERVAL // 60} minute(s)*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.message.reply_text(
            "⏸️ *Monitoring paused\\.*\n"
            "Send /monitor again to re\\-enable\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


@admin_only
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


# ---------------------------------------------------------------------------
# Callback query handler for pagination
# ---------------------------------------------------------------------------

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks (pagination buttons)."""
    query = update.callback_query
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


# ---------------------------------------------------------------------------
# Shared file-list renderer
# ---------------------------------------------------------------------------

async def _send_file_list(reply_fn, page: int) -> None:
    """Build and send a paginated file list message.

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
        file_id = f["file_id"]
        view_url = f"https://drive.google.com/file/d/{file_id}/view"
        lines.append(
            f"{icon} [{name}]({view_url}) — {escape_markdown(size)}"
        )

    text = truncate_message("\n".join(lines))
    keyboard = build_pagination_keyboard("list", page, total_pages)

    await reply_fn(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


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
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram Drive Monitor Bot…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
