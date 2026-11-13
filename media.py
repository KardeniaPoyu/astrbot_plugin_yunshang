"""Private, bounded post-photo cache. ``get``/``close`` are async; cleanup is sync.

Only URL strings belong here; callers select the post's raw image fields.
Use a dedicated cache directory. Filesystem work and Pillow decoding run on one
worker; explicit synchronous cleanup should likewise be called off the event loop.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import dataclass
import hashlib
from http.cookiejar import CookieJar, DefaultCookiePolicy
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx


_HOST = "ysxy.img.qimiaoyuanfen.com"
_BODY_LIMIT = 4 * 1024 * 1024
_PIXEL_LIMIT = 8_000_000
_CACHE_LIMIT = 64 * 1024 * 1024
_TTL = 7 * 86400
_TIMEOUT = 10.0
_SCAN_LIMIT = 2048
_FILE_LIMIT = 1024
_OWN_FILE = re.compile(r"[0-9a-f]{64}\.jpg\Z")
_MIME_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG",
                 "image/webp": "WEBP", "image/gif": "GIF"}

# httpx INFO includes full URLs; httpcore DEBUG includes response headers.
# Context-local filtering leaves other plugins' traffic/logging untouched and
# reuses the scope on plugin reload instead of accumulating filters.
_FILTER_NAME = "yunshang_private_media"
_existing = next((f for f in logging.getLogger("httpx").filters
                  if f.name == _FILTER_NAME), None)
_private_http = (_existing.scope if _existing else
                 contextvars.ContextVar(_FILTER_NAME, default=False))


class _PrivateLogFilter(logging.Filter):
    def __init__(self):
        super().__init__(_FILTER_NAME)
        self.scope = _private_http

    def filter(self, record):
        return not self.scope.get()


for _name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection",
              "httpcore.proxy", "httpcore.socks", "PIL.PngImagePlugin",
              "PIL.TiffImagePlugin", "PIL.Image"):
    _logger = logging.getLogger(_name)
    if not any(f.name == _FILTER_NAME for f in _logger.filters):
        _logger.addFilter(_PrivateLogFilter())


def safe_image_urls(values: list[str]) -> list[str]:
    """Keep at most nine distinct URLs on the exact HTTPS photo origin."""
    result = []
    if not isinstance(values, list):
        return result
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or "\\" in value or "#" in value):
            continue
        try:
            parts = urlsplit(value)
            # Comparing the entire authority also rejects userinfo, IPs,
            # encoded/extra labels, trailing dots and malformed/empty ports.
            if (parts.scheme != "https"
                    or parts.netloc.lower() not in (_HOST, _HOST + ":443")):
                continue
            value.encode("utf-8")
        except (ValueError, UnicodeError):
            continue
        if value not in result:
            result.append(value)
            if len(result) == 9:
                break
    return result


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


@dataclass
class _Pending:
    task: asyncio.Task
    waiters: int = 0


class CampusMedia:
    def __init__(self, directory, transport=None):
        self.directory = Path(directory).absolute()
        self._http = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
            auth=None, cookies=CookieJar(policy=_NoCookies()), timeout=_TIMEOUT,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        self._downloads = asyncio.Semaphore(2)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="campus-media")
        self._files = threading.Lock()
        self._inflight = {}
        self._tasks = set()  # Includes abandoned tasks still unwinding cancellation.
        self._closed = False
        self._closing = None

    async def get(self, url) -> str | None:
        """Return an absolute, sanitized JPEG path, or None on any media failure."""
        if self._closed or not safe_image_urls([url]):
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        pending = self._inflight.get(key)
        if pending is None:
            pending = _Pending(asyncio.create_task(self._get(url, key)))
            self._inflight[key] = pending
            self._tasks.add(pending.task)
            pending.task.add_done_callback(lambda done: self._finished(key, pending))
        pending.waiters += 1
        try:
            # Cancellation is local while another consumer still needs the image.
            return await asyncio.shield(pending.task)
        finally:
            pending.waiters -= 1
            if pending.waiters == 0:
                # Detach before cancelling so a new caller can start a replacement
                # without joining a dying task. No await splits this transition.
                if self._inflight.get(key) is pending:
                    del self._inflight[key]
                if not pending.task.done():
                    pending.task.cancel()

    def _finished(self, key, pending):
        self._tasks.discard(pending.task)
        # An old task may finish after its replacement has registered consumers.
        if self._inflight.get(key) is pending:
            del self._inflight[key]

    async def _run(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(
            self._worker, contextvars.copy_context().run, function, *args)

    async def _get(self, url, key):
        token = _private_http.set(True)
        try:
            if self._closed:
                return None
            # Cached files need no network slot, even while both downloads stall.
            ready, cached = await self._run(self._cached, key)
            if not ready or cached:
                return cached
            # Hold the slot through decode: at most two bodies can be buffered.
            async with self._downloads:
                if self._closed:
                    return None
                async with asyncio.timeout(_TIMEOUT):
                    downloaded = await self._download(url)
                if downloaded is None:
                    return None
                body, mime = downloaded
                return await self._run(self._save, body, key, mime)
        except Exception:
            # Do not echo URLs, server text, filesystem paths or exception text.
            return None
        finally:
            _private_http.reset(token)

    async def _download(self, url):
        # Construct a request directly: never merge a client's headers/cookies.
        # auth=None also prevents any client/default authentication flow.
        request = httpx.Request("GET", url, headers={
            "Accept": ", ".join(_MIME_FORMATS), "Accept-Encoding": "identity"})
        response = await self._http.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if response.status_code != 200:
                return None
            # Refuse HTTP compression to avoid decompression before the byte cap.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return None
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime and mime not in _MIME_FORMATS and mime != "application/octet-stream":
                return None
            length = response.headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdigit()
                                       or int(length) > _BODY_LIMIT):
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > _BODY_LIMIT:
                    return None
                body.extend(chunk)
            if not body or (length is not None and len(body) != int(length)):
                return None
            return body, mime
        finally:
            await response.aclose()

    def _cached(self, key):
        with self._files:
            if self.directory.is_symlink():
                return False, None
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                self.directory.chmod(0o700)
            if not self._cleanup_locked():
                return False, None
            path = self.directory / (key + ".jpg")
            try:
                info = path.lstat()
            except FileNotFoundError:
                return True, None
            if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and 0 < info.st_size <= _BODY_LIMIT
                    and 0 <= time.time() - info.st_mtime < _TTL):
                return True, str(path)
            return False, None

    def _save(self, body, key, mime):
        # Import/codec work is lazy and stays on the single worker. No changes to
        # Pillow's process-wide MAX_IMAGE_PIXELS or LOAD_TRUNCATED_IMAGES flags.
        from PIL import Image, ImageOps

        output = BytesIO()
        with Image.open(BytesIO(body), formats=list(_MIME_FORMATS.values())) as source:
            if (source.format not in _MIME_FORMATS.values()
                    or (mime in _MIME_FORMATS and source.format != _MIME_FORMATS[mime])
                    or min(source.size) <= 0 or source.width * source.height > _PIXEL_LIMIT):
                return None
            source.seek(0)
            source.load()
            with ImageOps.exif_transpose(source) as oriented:
                oriented.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                with oriented.convert("RGB") as rgb, Image.new("RGB", oriented.size) as clean:
                    # A fresh image carries no EXIF, ICC, comments or other metadata.
                    clean.paste(rgb)
                    clean.save(output, format="JPEG", quality=85)
        data = output.getvalue()
        if not data or len(data) > _BODY_LIMIT:
            return None
        with self._files:
            if not self._cleanup_locked(reserve=len(data)):
                return None
            path = self.directory / (key + ".jpg")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix="." + key + ".",
                                                 suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                return str(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def cleanup(self):
        """Best-effort bounded cleanup of this directory's hash-named JPEGs only."""
        try:
            with self._files:
                self._cleanup_locked()
        except Exception:
            pass

    def _cleanup_locked(self, reserve=0):
        if self.directory.is_symlink():
            return False
        now = time.time()
        files = []
        with os.scandir(self.directory) as entries:
            for index, entry in enumerate(entries):
                if index >= _SCAN_LIMIT:
                    # A polluted directory cannot be fully accounted for in this
                    # bounded scan. Refuse new writes rather than exceed quota.
                    return False
                if not _OWN_FILE.fullmatch(entry.name):
                    continue
                # Windows DirEntry.stat() reports st_nlink=0; lstat supplies
                # the actual link count without following a planted symlink.
                info = os.lstat(entry.path)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if not 0 <= now - info.st_mtime < _TTL or not 0 < info.st_size <= _BODY_LIMIT:
                    os.unlink(entry.path)
                else:
                    files.append((info.st_mtime, entry.path, info.st_size))
        size = sum(item[2] for item in files)
        count = len(files)
        for _, path, length in sorted(files):
            if size + reserve <= _CACHE_LIMIT and count + bool(reserve) <= _FILE_LIMIT:
                break
            os.unlink(path)
            size -= length
            count -= 1
        return size + reserve <= _CACHE_LIMIT and count + bool(reserve) <= _FILE_LIMIT

    async def close(self):
        """Cancel pending fetches, close HTTP and wait off-loop for any decoder."""
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._closing)

    async def _finish_close(self):
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        token = _private_http.set(True)
        try:
            await self._http.aclose()
        except Exception:
            pass
        finally:
            _private_http.reset(token)
            await asyncio.to_thread(self._worker.shutdown, wait=True, cancel_futures=True)
