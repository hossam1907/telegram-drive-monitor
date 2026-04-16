"""
Configuration module for Telegram Drive Monitor.

Loads and validates environment variables, defines constants, and configures logging.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env file if present
load_dotenv()


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

LOG_LEVEL_STR: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL: int = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: require an environment variable
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    """Return the value of an environment variable or raise an error.

    Args:
        name: The environment variable name.

    Returns:
        The string value of the variable.

    Raises:
        SystemExit: If the variable is not set.
    """
    value = os.getenv(name)
    if not value:
        logger.error("Required environment variable '%s' is not set. Check your .env file.", name)
        sys.exit(1)
    return value


def _get_int(name: str, default: int) -> int:
    """Return an integer environment variable, falling back to *default*.

    Args:
        name: The environment variable name.
        default: The default value if the variable is missing or invalid.

    Returns:
        The integer value.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Environment variable '%s' has non-integer value '%s'. Using default %d.",
            name,
            raw,
            default,
        )
        return default


# ---------------------------------------------------------------------------
# Required variables
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
DRIVE_FOLDER_ID: str = _require("DRIVE_FOLDER_ID")
GOOGLE_CREDENTIALS_FILE: str = _require("GOOGLE_CREDENTIALS_FILE")

# Admin user IDs — comma-separated list of Telegram user IDs
_admin_raw: str = _require("ADMIN_USER_IDS")
ADMIN_USER_IDS: List[int] = [int(uid.strip()) for uid in _admin_raw.split(",") if uid.strip()]

if not ADMIN_USER_IDS:
    logger.error("ADMIN_USER_IDS must contain at least one valid integer Telegram user ID.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Validate credentials file
# ---------------------------------------------------------------------------

_credentials_path = Path(GOOGLE_CREDENTIALS_FILE)
if not _credentials_path.exists():
    logger.error(
        "Google Service Account credentials file not found: '%s'.", GOOGLE_CREDENTIALS_FILE
    )
    sys.exit(1)
if not _credentials_path.is_file():
    logger.error(
        "Google Service Account credentials path is not a file: '%s'.", GOOGLE_CREDENTIALS_FILE
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Optional / defaulted variables
# ---------------------------------------------------------------------------

# Polling interval in seconds (default 300 = 5 minutes, min 60, max 3600)
POLL_INTERVAL: int = max(60, min(3600, _get_int("POLL_INTERVAL", 300)))

# Number of files per page when browsing the /list command
PAGE_SIZE: int = max(1, min(50, _get_int("PAGE_SIZE", 10)))

# SQLite database file path
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "drive_monitor.db")

# HTTP request timeout in seconds
REQUEST_TIMEOUT: int = _get_int("REQUEST_TIMEOUT", 30)

# YouTube Data API key (required for /extract_youtube)
YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")

# ---------------------------------------------------------------------------
# Google Drive API constants
# ---------------------------------------------------------------------------

DRIVE_API_SCOPES: List[str] = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_API_VERSION: str = "v3"

# Exponential backoff settings for Drive API rate limiting
BACKOFF_MAX_RETRIES: int = 10
BACKOFF_INITIAL_DELAY: float = 1.0   # seconds
BACKOFF_MAX_DELAY: float = 64.0      # seconds
BACKOFF_MULTIPLIER: float = 2.0

# Fields requested when listing Drive files
DRIVE_FILE_FIELDS: str = (
    "id, name, size, mimeType, modifiedTime, webViewLink, webContentLink, version"
)

# ---------------------------------------------------------------------------
# Telegram constants
# ---------------------------------------------------------------------------

# Maximum characters in a Telegram message
TELEGRAM_MAX_MESSAGE_LENGTH: int = 4096

# ---------------------------------------------------------------------------
# File download settings
# ---------------------------------------------------------------------------

# Maximum file size that can be sent via Telegram (50 MB)
MAX_FILE_SIZE: int = 50 * 1024 * 1024

# Timeout in seconds for downloading a file from Google Drive
DOWNLOAD_TIMEOUT: int = _get_int("DOWNLOAD_TIMEOUT", 60)

# Directory used for temporary downloaded files
TEMP_DIR: str = os.getenv("TEMP_DIR", tempfile.gettempdir())

logger.debug(
    "Configuration loaded. Poll interval: %ds, Page size: %d, Admins: %s",
    POLL_INTERVAL,
    PAGE_SIZE,
    ADMIN_USER_IDS,
)
