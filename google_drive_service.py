"""
Google Drive service module for Telegram Drive Monitor.

Provides an async-friendly wrapper around the Google Drive API v3.
All blocking API calls are executed in a thread pool executor so they
don't stall the asyncio event loop.
"""

import asyncio
import io
import logging
import time
from functools import partial
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from config import (
    BACKOFF_INITIAL_DELAY,
    BACKOFF_MAX_DELAY,
    BACKOFF_MAX_RETRIES,
    BACKOFF_MULTIPLIER,
    DRIVE_API_SCOPES,
    DRIVE_API_VERSION,
    DRIVE_FILE_FIELDS,
    DRIVE_FOLDER_ID,
    GOOGLE_CREDENTIALS_FILE,
    REQUEST_TIMEOUT,
)

# Chunk size for streaming Drive file downloads (4 MB)
_DOWNLOAD_CHUNK_SIZE: int = 4 * 1024 * 1024


class GoogleDriveService:
    """Async wrapper around the Google Drive API v3.

    Uses a service account for authentication and executes all blocking
    I/O in an executor to remain non-blocking within asyncio coroutines.

    Attributes:
        _service: The underlying Google API client resource object.
        _folder_id: The Drive folder ID being monitored.
        _cache: In-memory cache for file listings (TTL-based).
        _cache_time: Timestamp of the last cache population.
        _cache_ttl: Cache TTL in seconds.
    """

    _CACHE_TTL = 60  # seconds

    def __init__(self) -> None:
        """Initialise the service by building a Drive API client."""
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=DRIVE_API_SCOPES
        )
        self._service = build(
            "drive",
            DRIVE_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )
        self._folder_id: str = DRIVE_FOLDER_ID
        self._cache: List[Dict[str, Any]] = []
        self._cache_time: float = 0.0
        logger.info("GoogleDriveService initialised for folder '%s'.", self._folder_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _execute_with_backoff(self, request: Any) -> Any:
        """Execute a Drive API request with exponential backoff on rate limits.

        Args:
            request: A Drive API request object (has an ``.execute()`` method).

        Returns:
            The API response dict.

        Raises:
            HttpError: If retries are exhausted or the error is non-retryable.
        """
        delay = BACKOFF_INITIAL_DELAY
        for attempt in range(1, BACKOFF_MAX_RETRIES + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = exc.resp.status if exc.resp else 0
                if status in (429, 500, 503) and attempt < BACKOFF_MAX_RETRIES:
                    logger.warning(
                        "Drive API rate limit / server error (HTTP %d). "
                        "Retry %d/%d in %.1fs.",
                        status,
                        attempt,
                        BACKOFF_MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                    delay = min(delay * BACKOFF_MULTIPLIER, BACKOFF_MAX_DELAY)
                else:
                    raise
        raise RuntimeError("Drive API retries exhausted — this should not be reached.")

    async def _run_in_executor(self, func, *args) -> Any:
        """Run a blocking callable in the default thread-pool executor.

        Args:
            func: Callable to execute.
            *args: Positional arguments forwarded to *func*.

        Returns:
            The return value of *func*.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args))

    def _list_files_sync(self) -> List[Dict[str, Any]]:
        """Blocking implementation of file listing (runs in executor).

        Returns:
            List of file metadata dicts from the Drive API.
        """
        files: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        query = f"'{self._folder_id}' in parents and trashed = false"

        while True:
            request = self._service.files().list(
                q=query,
                spaces="drive",
                fields=f"nextPageToken, files({DRIVE_FILE_FIELDS})",
                orderBy="modifiedTime desc",
                pageSize=100,
                pageToken=page_token,
            )
            response = self._execute_with_backoff(request)
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        logger.debug("Listed %d files from Drive folder.", len(files))
        return files

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def list_files(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Return the list of files in the monitored folder.

        Results are cached for :attr:`_CACHE_TTL` seconds to reduce API calls.

        Args:
            force_refresh: Bypass the cache and fetch fresh data.

        Returns:
            List of file metadata dicts with keys matching :data:`DRIVE_FILE_FIELDS`.
        """
        now = time.monotonic()
        if not force_refresh and self._cache and (now - self._cache_time) < self._CACHE_TTL:
            logger.debug("Returning cached Drive file list (%d files).", len(self._cache))
            return self._cache

        try:
            files = await self._run_in_executor(self._list_files_sync)
            self._cache = files
            self._cache_time = now
            return files
        except HttpError as exc:
            logger.error("Drive API error while listing files: %s", exc)
            if self._cache:
                logger.warning("Returning stale cache due to API error.")
                return self._cache
            raise

    async def get_file_metadata(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch detailed metadata for a single file.

        Args:
            file_id: Google Drive file ID.

        Returns:
            Metadata dict or ``None`` if the file doesn't exist / access denied.
        """
        def _get() -> Dict[str, Any]:
            request = self._service.files().get(
                fileId=file_id,
                fields=DRIVE_FILE_FIELDS,
            )
            return self._execute_with_backoff(request)

        try:
            return await self._run_in_executor(_get)
        except HttpError as exc:
            if exc.resp and exc.resp.status == 404:
                logger.warning("File '%s' not found on Drive.", file_id)
                return None
            logger.error("Drive API error fetching file '%s': %s", file_id, exc)
            raise

    async def search_files(self, query: str) -> List[Dict[str, Any]]:
        """Search for files in the monitored folder by name substring.

        Args:
            query: Substring to search for (case-insensitive).

        Returns:
            List of matching file metadata dicts.
        """
        def _search() -> List[Dict[str, Any]]:
            escaped = query.replace("\\", "\\\\").replace("'", "\\'")
            q = (
                f"'{self._folder_id}' in parents "
                f"and name contains '{escaped}' "
                f"and trashed = false"
            )
            request = self._service.files().list(
                q=q,
                spaces="drive",
                fields=f"files({DRIVE_FILE_FIELDS})",
                orderBy="name",
                pageSize=50,
            )
            response = self._execute_with_backoff(request)
            return response.get("files", [])

        try:
            return await self._run_in_executor(_search)
        except HttpError as exc:
            logger.error("Drive API error during search for '%s': %s", query, exc)
            raise

    def invalidate_cache(self) -> None:
        """Clear the in-memory file listing cache."""
        self._cache = []
        self._cache_time = 0.0
        logger.debug("Drive file cache invalidated.")

    async def download_file(self, file_id: str) -> Optional[bytes]:
        """Download a file from Google Drive and return its content as bytes.

        Args:
            file_id: Google Drive file ID.

        Returns:
            The file content as :class:`bytes`, or ``None`` if the file was
            not found.

        Raises:
            HttpError: For Drive API errors other than 404.
        """
        def _download() -> bytes:
            request = self._service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        try:
            return await self._run_in_executor(_download)
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status == 404:
                logger.warning("File '%s' not found on Drive for download.", file_id)
                return None
            logger.error("Drive API error downloading file '%s': %s", file_id, exc)
            raise
