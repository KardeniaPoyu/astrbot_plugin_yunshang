"""Bounded, credential-free photo fetching, using real Pillow codecs."""

import asyncio
import builtins
from io import BytesIO
import logging
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

import httpx
from PIL import Image, PngImagePlugin

import media


BASE = "https://ysxy.img.qimiaoyuanfen.com"
MIB = 1024 * 1024


def picture(fmt="PNG", size=(80, 40), **options):
    output = BytesIO()
    with Image.new("RGB", size, "red") as im:
        im.save(output, format=fmt, **options)
    return output.getvalue()


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class URLTests(unittest.TestCase):
    def test_unique_order_and_nine_image_limit(self):
        urls = [f"{BASE}/{n}.jpg?size=large" for n in range(12)]
        self.assertEqual(media.safe_image_urls([urls[0], *urls]), urls[:9])
        self.assertEqual(media.safe_image_urls([BASE + ":443/a.jpg"]),
                         [BASE + ":443/a.jpg"])

    def test_only_exact_https_origin_without_ambiguous_syntax(self):
        for value in [
            "http://ysxy.img.qimiaoyuanfen.com/a", "//ysxy.img.qimiaoyuanfen.com/a",
            "/photo.jpg", "data:image/png;base64,AA", "https://127.0.0.1/a",
            "https://[::1]/a", "https://ys.qimiaoyuanfen.com/a",
            BASE + ".evil.test/a", "https://sub.ysxy.img.qimiaoyuanfen.com/a",
            BASE + ":80/a", BASE + ":444/a", BASE + ":/a", BASE + ":bad/a",
            BASE + "./a", BASE + "/a#part", BASE + "/a#",
            "https://user:password@ysxy.img.qimiaoyuanfen.com/a",
            "https://@ysxy.img.qimiaoyuanfen.com/a", BASE + "@evil.test/a",
            "https://ysxy.img.qimiaoyuanfen.com\\@evil.test/a",
            "https://%79sxy.img.qimiaoyuanfen.com/a", " " + BASE + "/a",
            BASE + "/a\r\nCookie: secret", "https://ysxy.img.\nqimiaoyuanfen.com/a",
            "", None, {"ori": BASE + "/a"},
        ]:
            with self.subTest(value=value):
                self.assertEqual(media.safe_image_urls([value]), [])
        self.assertEqual(media.safe_image_urls(BASE + "/a"), [])


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "photos"
        self.requests = []
        self.response = lambda: httpx.Response(200, content=picture(),
                                               headers={"content-type": "image/png"})

        async def handler(request):
            self.requests.append(request)
            return self.response()

        self.transport = httpx.MockTransport(handler)
        self.cache = media.CampusMedia(self.directory, transport=self.transport)
        self.addAsyncCleanup(self.cache.close)

    async def test_real_formats_become_metadata_free_rgb_jpeg(self):
        for fmt, mime in [("JPEG", "image/jpeg"), ("PNG", "image/png"),
                          ("WEBP", "image/webp"), ("GIF", "image/gif")]:
            with self.subTest(fmt=fmt):
                body = picture(fmt)
                self.response = lambda: httpx.Response(200, content=body,
                                                       headers={"content-type": mime})
                result = await self.cache.get(f"{BASE}/{fmt}")
                self.assertIsNotNone(result)
                path = Path(result)
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.parent, self.directory)
                self.assertRegex(path.name, r"^[0-9a-f]{64}\.jpg$")
                with Image.open(path) as im:
                    self.assertEqual((im.format, im.mode, im.size), ("JPEG", "RGB", (80, 40)))
                    self.assertFalse(im.getexif())
                    self.assertNotIn("icc_profile", im.info)
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o077, 0)
                    self.assertEqual(self.directory.stat().st_mode & 0o077, 0)

    async def test_exif_rotation_resize_and_metadata_removal(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private description"
        body = picture("JPEG", (3000, 1000), exif=exif, icc_profile=b"private profile")
        self.response = lambda: httpx.Response(200, content=body)
        result = await self.cache.get(BASE + "/rotated")
        self.assertIsNotNone(result)
        with Image.open(result) as im:
            self.assertEqual(im.size, (683, 2048))
            self.assertFalse(im.getexif())
            self.assertNotIn("icc_profile", im.info)

    async def test_animated_gif_only_uses_first_frame(self):
        data = BytesIO()
        with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as last:
            first.save(data, format="GIF", save_all=True, append_images=[last], duration=50)
        self.response = lambda: httpx.Response(200, content=data.getvalue())
        result = await self.cache.get(BASE + "/animated")
        with Image.open(result) as im:
            red, green, blue = im.getpixel((10, 10))
            self.assertGreater(red, 240)
            self.assertLess(blue, 10)
            self.assertEqual(getattr(im, "n_frames", 1), 1)

    async def test_cache_reused_across_instances_and_expires_after_seven_days(self):
        url = BASE + "/cached"
        path = await self.cache.get(url)
        self.assertEqual(await self.cache.get(url), path)
        another = media.CampusMedia(self.directory, transport=self.transport)
        self.addAsyncCleanup(another.close)
        self.assertEqual(await another.get(url), path)
        self.assertEqual(len(self.requests), 1)
        old = time.time() - 7 * 86400 - 1
        os.utime(path, (old, old))
        self.assertEqual(await self.cache.get(url), path)
        self.assertEqual(len(self.requests), 2)

    async def test_bad_hosts_and_redirects_never_fetch_another_origin(self):
        self.assertIsNone(await self.cache.get("https://evil.test/secret"))
        self.assertFalse(self.requests)
        for target in ["https://evil.test/secret", BASE + "/other"]:
            self.response = lambda: httpx.Response(302, headers={"location": target})
            self.assertIsNone(await self.cache.get(BASE + "/redirect"))
        self.assertEqual(len(self.requests), 2)

    async def test_oversize_length_rejected_before_read_and_stream_limit_stops_reading(self):
        oversized = Body([b"x"])
        self.response = lambda: httpx.Response(200, headers={"content-length": str(4 * MIB + 1)},
                                               stream=oversized)
        self.assertIsNone(await self.cache.get(BASE + "/large-header"))
        self.assertEqual(oversized.reads, 0)
        self.assertTrue(oversized.closed)
        body = Body([b"x" * MIB] * 6)
        self.response = lambda: httpx.Response(200, stream=body)
        self.assertIsNone(await self.cache.get(BASE + "/large-stream"))
        self.assertLessEqual(body.reads, 5)
        self.assertTrue(body.closed)

    async def test_spoofed_unsupported_truncated_and_encoded_content_rejected(self):
        for payload, headers in [
            (b"<html>private server error</html>", {"content-type": "image/jpeg"}),
            (picture("BMP"), {"content-type": "image/png"}),
            (picture()[:45], {"content-type": "image/png"}),
            (picture(), {"content-type": "text/html"}),
            (picture(), {"content-encoding": "gzip"}),
            (picture(), {"content-length": "-1"}),
        ]:
            self.response = lambda: httpx.Response(200, stream=Body([payload]), headers=headers)
            self.assertIsNone(await self.cache.get(BASE + "/spoof"))
        self.assertFalse(list(self.directory.glob("*.jpg")))

    async def test_more_than_eight_megapixels_rejected_before_pixel_load(self):
        # Valid PNG IHDR/CRC, malicious dimensions, and a tiny compressed body.
        original = picture()
        ihdr = struct.pack(">II", 4000, 2001) + original[24:29]
        payload = (original[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
                   + original[33:])
        self.response = lambda: httpx.Response(200, content=payload)
        with patch.object(PngImagePlugin.PngImageFile, "load", side_effect=AssertionError("decoded")) as load:
            self.assertIsNone(await self.cache.get(BASE + "/dimensions"))
        load.assert_not_called()

    async def test_server_cookies_environment_and_auth_never_reach_requests(self):
        self.response = lambda: httpx.Response(200, content=picture(), headers={
            "set-cookie": "PHPSESSID=server-secret; Path=/; Domain=ysxy.img.qimiaoyuanfen.com"})
        netrc = Path(self.temp.name) / "netrc"
        netrc.write_text("machine ysxy.img.qimiaoyuanfen.com login private password secret\n")
        with patch.dict(os.environ, {"NETRC": str(netrc), "HTTPS_PROXY": "http://127.0.0.1:1"}):
            for n in range(3):
                self.assertIsNotNone(await self.cache.get(f"{BASE}/cookies/{n}"))
        for request in self.requests:
            for header in ("cookie", "authorization", "proxy-authorization"):
                self.assertNotIn(header, request.headers)
            self.assertTrue(all(0 < value <= 10 for value in request.extensions["timeout"].values()))

    async def test_private_http_logs_and_errors_do_not_expose_urls(self):
        secret_url = BASE + "/photo?token=private-token"
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("media-test").warning("unrelated log remains visible")
            self.assertIsNotNone(await self.cache.get(secret_url))
            def fail():
                raise httpx.ReadError("private-token " + secret_url)
            self.response = fail
            self.assertIsNone(await self.cache.get(secret_url + "2"))
            logging.getLogger("httpx").info("unrelated HTTP request")
        output = "\n".join(logs.output)
        self.assertNotIn("private-token", output)
        self.assertNotIn("ysxy.img", output)
        self.assertIn("unrelated HTTP request", output)

    async def test_download_concurrency_and_same_url_deduplication(self):
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            self.requests.append(request)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        urls = [BASE + "/duplicate"] * 5 + [f"{BASE}/parallel/{i}" for i in range(5)]
        paths = await asyncio.gather(*(other.get(url) for url in urls))
        self.assertTrue(all(paths))
        self.assertEqual(len(set(paths[:5])), 1)
        self.assertEqual(len(self.requests), 6)
        self.assertLessEqual(peak, 2)

    async def test_decoding_is_serial_and_does_not_block_event_loop(self):
        original = Image.Image.convert
        active = peak = 0
        thread_ids = set()
        guard = threading.Lock()
        def slow_convert(im, *args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
                thread_ids.add(threading.get_ident())
            time.sleep(0.03)
            try:
                return original(im, *args, **kwargs)
            finally:
                with guard:
                    active -= 1
        with patch.object(Image.Image, "convert", slow_convert):
            paths = await asyncio.gather(*(self.cache.get(f"{BASE}/decode/{i}") for i in range(4)))
        self.assertTrue(all(paths))
        self.assertEqual(peak, 1)
        self.assertNotIn(threading.get_ident(), thread_ids)

    async def test_cleanup_bounds_size_ttl_and_preserves_unrelated_files(self):
        self.directory.mkdir()
        foreign = self.directory / "user-photo.jpg"
        foreign.write_bytes(b"unrelated")
        nested = self.directory / "nested"
        nested.mkdir()
        (nested / ("a" * 64 + ".jpg")).write_bytes(b"unrelated")
        now = time.time()
        for i in range(18):
            path = self.directory / (f"{i:064x}.jpg")
            with path.open("wb") as output:
                output.truncate(4 * MIB)
            stamp = now - (8 * 86400 if i == 0 else 100 - i)
            os.utime(path, (stamp, stamp))
        self.cache.cleanup()
        own = [p for p in self.directory.glob("*.jpg") if len(p.stem) == 64]
        self.assertLessEqual(sum(p.stat().st_size for p in own), 64 * MIB)
        self.assertFalse((self.directory / ("0" * 64 + ".jpg")).exists())
        self.assertFalse((self.directory / (f"{1:064x}.jpg")).exists())
        self.assertEqual(foreign.read_bytes(), b"unrelated")
        self.assertTrue(next(nested.iterdir()).exists())

    async def test_missing_pillow_and_unwritable_cache_fail_closed(self):
        original_import = builtins.__import__
        def without_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("private import detail")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=without_pillow):
            self.assertIsNone(await self.cache.get(BASE + "/no-pillow"))
        self.assertFalse(list(self.directory.glob("*.jpg")))
        blocked = Path(self.temp.name) / "file-not-directory"
        blocked.write_bytes(b"keep")
        other = media.CampusMedia(blocked, transport=self.transport)
        self.addAsyncCleanup(other.close)
        self.assertIsNone(await other.get(BASE + "/blocked"))
        other.cleanup()
        self.assertEqual(blocked.read_bytes(), b"keep")

    async def test_failed_atomic_save_leaves_no_partial_file(self):
        with patch.object(media.os, "replace", side_effect=OSError("private filesystem detail")):
            self.assertIsNone(await self.cache.get(BASE + "/atomic"))
        self.assertFalse(list(self.directory.iterdir()))

    async def test_cancelled_waiter_does_not_cancel_shared_download(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def handler(request):
            self.requests.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        first = asyncio.create_task(other.get(BASE + "/shared"))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(other.get(BASE + "/shared"))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertIsNotNone(await second)
        self.assertEqual(len(self.requests), 1)

    async def test_last_waiter_timeout_cancels_active_downloads_and_frees_slots(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        cancellations = []
        async def handler(request):
            self.requests.append(request)
            if request.url.path.startswith("/slow/"):
                if len(self.requests) == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancellations.append(request.url.path)
                    if len(cancellations) == 2:
                        cancelled.set()
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        callers = [asyncio.create_task(other.get(f"{BASE}/slow/{i}")) for i in range(2)]
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            results = await asyncio.gather(
                *(asyncio.wait_for(task, 0.03) for task in callers), return_exceptions=True)
            self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
            await asyncio.wait_for(cancelled.wait(), 0.5)
            self.assertIsNotNone(await asyncio.wait_for(other.get(BASE + "/fresh"), 0.5))
        finally:
            for task in callers:
                task.cancel()
            await asyncio.gather(*callers, return_exceptions=True)

    async def test_timed_out_queued_batches_never_start_downloads(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def handler(request):
            self.requests.append(request)
            if request.url.path.startswith("/blocking/"):
                if len(self.requests) == 2:
                    started.set()
                await release.wait()
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        blockers = [asyncio.create_task(other.get(f"{BASE}/blocking/{i}")) for i in range(2)]
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            for batch in range(2):
                results = await asyncio.gather(*(asyncio.wait_for(
                    other.get(f"{BASE}/abandoned/{batch}/{i}"), 0.03)
                    for i in range(6)), return_exceptions=True)
                self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
            release.set()
            self.assertTrue(all(await asyncio.wait_for(asyncio.gather(*blockers), 0.5)))
            self.assertIsNotNone(await asyncio.wait_for(other.get(BASE + "/later"), 0.5))
            self.assertEqual(len(self.requests), 3)
            self.assertFalse(any("/abandoned/" in req.url.path for req in self.requests))
        finally:
            release.set()
            for task in blockers:
                task.cancel()
            await asyncio.gather(*blockers, return_exceptions=True)

    async def test_cached_image_bypasses_saturated_download_slots(self):
        started = asyncio.Event()
        count = 0
        async def handler(request):
            nonlocal count
            self.requests.append(request)
            if request.url.path.startswith("/slow/"):
                count += 1
                if count == 2:
                    started.set()
                await asyncio.Event().wait()
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        cached = await other.get(BASE + "/cached")
        self.assertIsNotNone(cached)
        blockers = [asyncio.create_task(other.get(f"{BASE}/slow/{i}")) for i in range(8)]
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            self.assertEqual(await asyncio.wait_for(other.get(BASE + "/cached"), 0.2), cached)
            self.assertEqual(len(self.requests), 3)
        finally:
            for task in blockers:
                task.cancel()
            await asyncio.gather(*blockers, return_exceptions=True)

    async def test_abandoned_completion_cannot_remove_replacement_or_its_waiters(self):
        started = asyncio.Event()
        cancelling = asyncio.Event()
        finish_old = asyncio.Event()
        old_finished = asyncio.Event()
        replacement_started = asyncio.Event()
        finish_replacement = asyncio.Event()
        joined = asyncio.Event()
        async def handler(request):
            self.requests.append(request)
            if len(self.requests) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelling.set()
                    await finish_old.wait()
                    raise
                finally:
                    old_finished.set()
            replacement_started.set()
            await finish_replacement.wait()
            return httpx.Response(200, content=picture())
        other = media.CampusMedia(self.directory, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        url = BASE + "/replacement"
        callers = [asyncio.create_task(other.get(url))]
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            callers[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await callers[0]
            await asyncio.wait_for(cancelling.wait(), 0.5)
            callers.append(asyncio.create_task(other.get(url)))
            await asyncio.wait_for(replacement_started.wait(), 0.5)
            finish_old.set()
            await asyncio.wait_for(old_finished.wait(), 0.5)
            await asyncio.sleep(0)  # Let the obsolete task's done callback run.
            async def join_replacement():
                joined.set()
                return await other.get(url)
            callers.append(asyncio.create_task(join_replacement()))
            await asyncio.wait_for(joined.wait(), 0.5)
            callers[1].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await callers[1]
            finish_replacement.set()
            self.assertIsNotNone(await asyncio.wait_for(callers[2], 0.5))
            self.assertEqual(len(self.requests), 2)
        finally:
            finish_old.set()
            finish_replacement.set()
            for task in callers:
                task.cancel()
            await asyncio.gather(*callers, return_exceptions=True)

    async def test_close_is_idempotent_and_prevents_requests(self):
        await self.cache.close()
        await self.cache.close()
        self.assertIsNone(await self.cache.get(BASE + "/closed"))
        self.assertFalse(self.requests)


if __name__ == "__main__":
    unittest.main()
