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
    DOWNLOAD_TIMEOUT,
    MAX_FILE_SIZE,
    PAGE_SIZE,
    POLL_INTERVAL,
    TELEGRAM_BOT_TOKEN,
    TEMP_DIR,
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
            user.username if user else "unknown",
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

@approved_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command — send a welcome message with help text."""
    user = update.effective_user
    name = user.first_name if user else "there"
    text = (
        f"👋 Hello, *{escape_markdown(name)}*\\! Welcome to *Drive Monitor Bot*\\.\n\n"
        "I watch a Google Drive folder and notify you whenever files are added or updated\\.\n\n"
        "*Available commands:*\n"
        "• /start — Show this help message\n"
        "• /request `<message>` — Request access \\(for new users\\)\n"
        "• /requests — Review pending access requests \\(admin\\)\n"
        "• /approve `<user_id>` — Approve a request \\(admin\\)\n"
        "• /reject `<user_id>` — Reject a request \\(admin\\)\n"
        "• /list — Browse files in the monitored folder\n"
        "• /browse `<folder_id>` — Browse files inside a specific folder\n"
        "• /download `<filename>` — Download a file directly from Drive\n"
        "• /search `<filename>` — Search for a file by name\n"
        "• /monitor — Toggle monitoring on or off\n"
        "• /status — Show monitoring statistics\n"
        "• /links — Show important resource links\n"
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

    requested_at = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
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
        display_name = req.get("first_name") or req.get("username") or f"User {req['user_id']}"
        requested_at = req.get("requested_at") or "Unknown"
        message = req.get("message") or "-"
        text = (
            f"{idx}️⃣ *{escape_markdown(str(display_name))}* \\(ID: `{req['user_id']}`\\)\n"
            f"Message: \"{escape_markdown(str(message))}\"\n"
            f"Requested: {escape_markdown(str(requested_at))}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{req['user_id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{req['user_id']}"),
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

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram Drive Monitor Bot…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
