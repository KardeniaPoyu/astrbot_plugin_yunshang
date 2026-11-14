"""Renderer contracts, real font layout, and saved PNG integration checks."""

import base64
import copy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

import cards


def fixture(count=4, **overrides):
    digest = {
        "community": "民大校园墙（合成示例）",
        "window_start": "2026-11-12T04:00:00+00:00",
        "window_end": "2026-11-12T08:00:00+00:00",
        "total": max(count, 12), "shown": count, "scan_limited": False,
        "mode": "ai", "intro": "午后的校园碎片：学习搭子、旧书流转，还有一份失物招领。",
        "items": [{
            "id": f"DEMO-{i + 1:03}", "title": "寻找周末一起读书的同学",
            "summary": "计划周末在图书馆读书，交流各自最近喜欢的作品。\n具体安排请查看原帖。",
            "published_at": "2026-11-12T06:15:00+00:00",
            "category": "学习交流", "image_count": i % 3,
        } for i in range(count)],
    }
    digest.update(overrides)
    return digest


class CardsTests(unittest.TestCase):
    cards = cards

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fonts = self.cards._Fonts("")

    def render_with_text(self, digest, positions=None, plan=None):
        """Observe the real Pillow draw calls while still drawing every glyph."""
        drawn = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            font = kwargs["font"]
            box = draw.textbbox(xy, text, font=font, anchor=kwargs.get("anchor"))
            self.assertGreaterEqual(box[0], 0)
            self.assertGreaterEqual(box[1], 0)
            self.assertLessEqual(box[2], draw.im.size[0])
            self.assertLessEqual(box[3], draw.im.size[1])
            drawn.append(text)
            if positions is not None:
                positions.append((text, box))
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", capture):
            paths = self.cards.render_digest(digest, self.temp.name, plan=plan)
        return paths, "\n".join(drawn)

    def render_detail_with_text(self, post):
        drawn, body = [], []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            font = kwargs["font"]
            box = draw.textbbox(xy, text, font=font, anchor=kwargs.get("anchor"))
            self.assertGreaterEqual(min(box[:2]), 0)
            self.assertLessEqual(box[2], draw.im.size[0])
            self.assertLessEqual(box[3], draw.im.size[1])
            drawn.append(text)
            if font.size == 26:
                body.append(text)
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", capture):
            paths = self.cards.render_post_detail(post, "合成校园墙", self.temp.name)
        return paths, "\n".join(drawn), body

    def test_detail_keeps_full_5000_character_text_and_tail_without_ai_summary(self):
        tail = "原文尾部完整保留"
        post = dict(id="DETAIL-001", text="文" * (5000 - len(tail)) + tail,
                    title="不能出现的AI标题", summary="不能替代原文的AI摘要", body="错误的摘要正文",
                    published_at="2026-11-12T06:15:00+00:00", like_count=128,
                    trace_count=7, comment_count=3, image_count=0)
        before = copy.deepcopy(post)
        paths, text, body = self.render_detail_with_text(post)
        self.assertEqual(post, before)
        self.assertEqual(len(paths), 1)
        self.assertEqual("".join(body), post["text"])
        for expected in ("帖子全文", "DETAIL-001", "2026.11.12 14:15", "点赞 128",
                         "蹲蹲 7", "评论 3", "/校园墙 详情", "/校园墙 图片"):
            self.assertIn(expected, text)
        for forbidden in ("不能出现的AI标题", "AI摘要", "错误的摘要正文", "原文节选", "/校园圈"):
            self.assertNotIn(forbidden, text)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.width, 1080)
            self.assertLessEqual(image.height, 16000)
            self.assertGreater(image.height, 5000)

    def test_detail_splits_every_paragraph_once_and_closes_each_native_page(self):
        paragraphs = [f"第{i:03}段合成原文，每段都需要完整展示。" for i in range(420)]
        post = dict(id="DETAIL-002", text="\n\n".join(paragraphs) + "\n最后一段尾声")
        saved, allocations = [], []
        original_save, original_new = Image.Image.save, Image.new

        def save_one_page(image, *args, **kwargs):
            if saved:
                with self.assertRaises(ValueError):
                    saved[-1].getpixel((0, 0))
            result = original_save(image, *args, **kwargs)
            saved.append(image)
            return result

        def native_only(mode, size, *args, **kwargs):
            allocations.append(size)
            return original_new(mode, size, *args, **kwargs)

        with patch.object(Image.Image, "save", save_one_page), patch.object(Image, "new", native_only):
            paths, text, body = self.render_detail_with_text(post)
        self.assertGreater(len(paths), 1)
        self.assertEqual(body, post["text"].split("\n"))
        for paragraph in paragraphs:
            self.assertEqual(body.count(paragraph), 1)
        for image in saved:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))
        self.assertTrue(all(w <= 1080 and h <= 16000 for w, h in allocations))
        for page, path in enumerate(paths, 1):
            self.assertTrue(Path(path).is_absolute())
            self.assertIn(f"{page} / {len(paths)}", text)
            with Image.open(path) as image:
                self.assertEqual(image.width, 1080)
                self.assertLessEqual(image.height, 16000)

    def test_detail_embeds_up_to_three_local_photos_and_closes_them(self):
        colors = ((204, 14, 157), (17, 191, 70), (242, 208, 10), (11, 211, 213))
        photos = []
        for i, color in enumerate(colors):
            path = Path(self.temp.name) / f"detail-synthetic-{i}.jpg"
            with Image.new("RGB", (160, 160), color) as source:
                source.save(path, quality=95, subsampling=0)
            photos.append(str(path))
        loaded = []
        original = self.cards._load_thumbnail

        def track_photo(*args):
            image = original(*args)
            loaded.append(image)
            return image

        post = dict(id="DETAIL-003", text="这是合成原文。\n\n三张配图展示在正文下方。",
                    image_paths=photos, image_count=4)
        with patch.object(self.cards, "_load_thumbnail", track_photo):
            paths, text, body = self.render_detail_with_text(post)
        self.assertEqual(len(paths), 1)
        self.assertEqual(body, post["text"].split("\n"))
        self.assertEqual(len(loaded), 3)
        self.assertIn("配图 4 张", text)
        for image in loaded:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))
        with Image.open(paths[0]) as image:
            for color in colors[:3]:
                self.assertGreater(self.color_region(image, color)[1], 1000)
            self.assertEqual(self.color_region(image, colors[3])[1], 0)

    def test_detail_failed_gallery_and_later_write_failure_leave_no_partial_batch(self):
        corrupt = Path(self.temp.name) / "corrupt-detail.jpg"
        corrupt.write_bytes(b"synthetic corrupt JPEG")
        post = dict(id="DETAIL-004", text="原文仍然完整。", image_count=2,
                    image_paths=[str(corrupt), str(Path(self.temp.name) / "missing.jpg")])
        paths, _, body = self.render_detail_with_text(post)
        self.assertEqual(body, [post["text"]])
        existing = {path: path.read_bytes() for path in Path(self.temp.name).iterdir()}
        post["text"] = "完整原文\n\n" * 400
        original, calls = Image.Image.save, 0

        def fail_second(image, fp, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                Path(fp).write_bytes(b"partial PNG")
                raise OSError("synthetic second-page failure")
            return original(image, fp, *args, **kwargs)

        with patch.object(Image.Image, "save", fail_second), self.assertRaises(self.cards.RenderError):
            self.cards.render_post_detail(post, "合成校园墙", self.temp.name)
        self.assertEqual({path: path.read_bytes() for path in Path(self.temp.name).iterdir()}, existing)

    def test_detail_requires_original_text_and_source_id(self):
        for post in ({"id": "D", "summary": "AI摘要"}, {"id": "", "text": "原文"},
                     {"id": "D", "text": None}, {"id": "D", "text": "原文", "like_count": -1}):
            with self.subTest(post=post), self.assertRaises(self.cards.RenderError):
                self.cards.render_post_detail(post, "合成校园墙", self.temp.name)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_wrap_measurement_work_is_linear_and_probes_are_bounded(self):
        class CountingFont:
            measured = 0
            longest_probe = 0

            def getlength(self, text):
                self.measured += len(text)
                self.longest_probe = max(self.longest_probe, len(text))
                return len(text) * 26

        for length in (20_000, 300_000):
            with self.subTest(length=length):
                font = CountingFont()
                source = "文" * length
                lines = self.cards._wrap(source, font, 920, length)
                self.assertEqual("".join(lines), source)
                self.assertLessEqual(font.longest_probe, 512)
                self.assertLess(font.measured, length * 12)

    def test_detail_excessive_characters_or_pages_raise_before_canvas_or_files(self):
        for text, message in (("文" * 20_001, "20000"),
                              ("完整一行\n" * 1300, "3 pages")):
            with self.subTest(message=message), patch.object(self.cards, "_Canvas") as canvas:
                with self.assertRaisesRegex(self.cards.RenderError, message):
                    self.cards.render_post_detail(dict(id="TOO-LONG", text=text),
                                                  "合成校园墙", self.temp.name)
                canvas.assert_not_called()
                self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_detail_accepts_exactly_20000_characters_without_losing_tail(self):
        source = "文" * 19_990 + "最后十字必须全部保留"
        self.assertEqual(len(source), 20_000)
        paths, _, body = self.render_detail_with_text(dict(id="LIMIT", text=source))
        self.assertEqual("".join(body), source)
        self.assertLessEqual(len(paths), 3)
        for path in paths:
            with Image.open(path) as rendered:
                self.assertLessEqual(rendered.height, 16000)

    def synthetic_qr_url(self, size=(430, 430), image_format="JPEG"):
        """A labeled test pattern, never a generated destination/mini-app code."""
        buffer = BytesIO()
        with Image.new("RGB", size, "white") as source:
            draw = ImageDraw.Draw(source)
            draw.rectangle((20, 20, 50, 50), fill=(204, 14, 157))
            draw.rectangle((100, 100, 330, 330), fill="#102020")
            draw.rectangle((130, 130, 300, 300), fill="white")
            draw.rectangle((180, 180, 250, 250), fill="#102020")
            source.save(buffer, format=image_format, quality=95, subsampling=0)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + encoded, buffer.getvalue()

    def test_detail_official_qr_is_poster_sized_beside_its_prompt_after_photos(self):
        data_url, jpeg = self.synthetic_qr_url()
        photo = Path(self.temp.name) / "detail-before-qr.jpg"
        with Image.new("RGB", (160, 160), (17, 191, 70)) as sample:
            sample.save(photo, quality=95, subsampling=0)
        post = dict(id="QR-DETAIL", text="完整原文，不为小程序码截短。", image_count=1,
                    image_paths=[str(photo)], mini_app_qr_data_url=data_url)
        loaded = []
        original = self.cards._load_detail_qr

        def track_qr(value):
            qr = original(value)
            loaded.append(qr)
            return qr

        with patch.object(self.cards, "_load_detail_qr", track_qr):
            paths, text, body = self.render_detail_with_text(post)
        self.assertEqual(len(paths), 1)
        self.assertEqual(body, [post["text"]])
        self.assertEqual(text.count("微信扫码打开原帖"), 1)
        edge, pad = self.cards._DETAIL_QR_EDGE, self.cards._DETAIL_QR_PAD
        with Image.open(paths[0]) as rendered, Image.open(BytesIO(jpeg)) as decoded:
            qr_marker, pixels = self.color_region(rendered, (204, 14, 157))
            photo_box, _ = self.color_region(rendered, (17, 191, 70))
            self.assertGreater(pixels, 20)
            # The whole official code, scaled uniformly with Lanczos: never
            # cropped, and its quiet edges stay white inside the tile padding.
            expected = decoded.convert("RGB").resize((edge, edge), Image.Resampling.LANCZOS).tobytes()
            matches = [(x, y) for x in range(qr_marker[0] - 16, qr_marker[0] - 4)
                       for y in range(qr_marker[1] - 16, qr_marker[1] - 4)
                       if rendered.crop((x, y, x + edge, y + edge)).tobytes() == expected]
            self.assertEqual(len(matches), 1)
            left, top = matches[0]
            self.assertGreater(top, photo_box[3])
            self.assertEqual(rendered.getpixel((left - pad // 2, top + edge // 2)), (255, 255, 255))
            # Poster proportion: about a fifth of the width, right of the prompt.
            self.assertLess(edge, rendered.width // 4)
            self.assertGreater(left, rendered.width // 2)
        for qr in loaded:
            with self.assertRaises(ValueError):
                qr.getpixel((0, 0))

    def test_detail_qr_only_on_last_bounded_page_with_all_body_lines(self):
        data_url, _ = self.synthetic_qr_url()
        post = dict(id="QR-LONG", text="\n".join(f"完整第{i:03}行" for i in range(430)),
                    mini_app_qr_data_url=data_url)
        paths, text, body = self.render_detail_with_text(post)
        self.assertGreater(len(paths), 1)
        self.assertEqual(body, post["text"].split("\n"))
        self.assertEqual(text.count("微信扫码打开原帖"), 1)
        for index, path in enumerate(paths):
            with Image.open(path) as rendered:
                self.assertLessEqual(rendered.height, 16000)
                _, pixels = self.color_region(rendered, (204, 14, 157))
                self.assertEqual(pixels > 0, index == len(paths) - 1)

    def test_invalid_or_oversized_detail_qr_is_identical_to_no_qr(self):
        valid, jpeg = self.synthetic_qr_url()
        too_many_pixels, _ = self.synthetic_qr_url((1001, 1000))
        mislabeled_png, _ = self.synthetic_qr_url(image_format="PNG")
        over_bytes = "data:image/jpeg;base64," + base64.b64encode(jpeg + b"x" * (512 * 1024)).decode("ascii")
        broken_jpeg = "data:image/jpeg;base64," + base64.b64encode(jpeg[:700]).decode("ascii")
        post = dict(id="QR-BAD", text="原文必须完整保留。\n\n来源命令也保留。")
        paths, expected_text, expected_body = self.render_detail_with_text(post)
        with Image.open(paths[0]) as rendered:
            expected_size, expected_pixels = rendered.size, rendered.tobytes()
        for value in (None, "", "https://example.invalid/qr.jpg", "data:image/jpeg;base64,%%%",
                      valid.replace("image/jpeg", "image/png"), over_bytes,
                      too_many_pixels, mislabeled_png, broken_jpeg):
            with self.subTest(kind=str(value)[:40]):
                post["mini_app_qr_data_url"] = value
                paths, text, body = self.render_detail_with_text(post)
                self.assertEqual((text, body), (expected_text, expected_body))
                with Image.open(paths[0]) as rendered:
                    self.assertEqual((rendered.size, rendered.tobytes()), (expected_size, expected_pixels))

    def test_digest_does_not_decode_or_display_detail_qr(self):
        digest = fixture(1)
        digest["items"][0]["mini_app_qr_data_url"] = self.synthetic_qr_url()[0]
        with patch.object(self.cards, "_load_detail_qr", side_effect=AssertionError("QR on digest")):
            paths, text = self.render_with_text(digest)
        self.assertEqual(len(paths), 1)
        self.assertNotIn("微信扫码打开原帖", text)

    def test_png_pagination_counts_provenance_and_timezone(self):
        digest = fixture(8, scan_limited=True)
        before = copy.deepcopy(digest)
        paths, text = self.render_with_text(digest)
        self.assertEqual(len(paths), 1)
        self.assertEqual(digest, before)
        for path in paths:
            self.assertTrue(Path(path).is_absolute())
            with Image.open(path) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.mode, "RGB")
                self.assertEqual(im.width, 1080)
                self.assertLessEqual(im.height, 16000)
            self.assertLess(Path(path).stat().st_size, 2_000_000)
        for expected in ("本期展示", "12:00", "16:00", "北京时间", "AI",
                         "12", "8", "可能还有遗漏", "配图 2 张", "1 / 1"):
            self.assertIn(expected, text)
        for i in range(1, 9):
            self.assertEqual(text.count(f"DEMO-{i:03}"), 1)
        self.assertEqual(text.count("寻找周末一起读书的同学"), 8)
        self.assertNotIn("非官方", text)

    def test_ranked_digest_labels_four_selected_posts_and_keeps_supplied_order(self):
        digest = fixture(4, ranking="likes")
        for item, likes in zip(digest["items"], (128, 96, 74, 52)):
            item.update(like_count=likes, trace_count=12, comment_count=7)
        before = copy.deepcopy(digest)
        paths, text = self.render_with_text(digest)
        self.assertEqual(len(paths), 1)
        self.assertEqual(digest, before)
        for expected in ("校园墙高赞精选", "本期精选", "按点赞排序", "每 4 小时更新",
                         "点赞 128\n蹲蹲 12\n评论 7", "1 / 1"):
            self.assertIn(expected, text)
        self.assertNotIn("校园墙新帖速览", text)
        self.assertNotIn("可能还有遗漏", text)
        self.assertLess(text.index("DEMO-001"), text.index("DEMO-004"))
        _, legacy = self.render_with_text(fixture(1))
        self.assertIn("校园墙新帖速览", legacy)
        self.assertIn("本期展示", legacy)

    def test_partial_ranked_digest_describes_incomplete_collection_without_disclaimers(self):
        _, text = self.render_with_text(fixture(4, ranking="likes", scan_limited=True))
        self.assertIn("按点赞排序（已收集部分）", text)
        self.assertIn("仅展示已收集的新帖，可能还有遗漏", text)
        for banned in ("扫描未完成", "未经核实", "图片未识别", "附图未识别"):
            self.assertNotIn(banned, text)

    def test_metrics_distinguish_unknown_zero_and_large_counts(self):
        digest = fixture(4, ranking="likes")
        digest["items"][0].update(like_count=None, trace_count=0, comment_count=None)
        digest["items"][1].update(like_count=0, trace_count=None, comment_count=0)
        digest["items"][2].update(like_count=12_345, trace_count=100_000_000,
                                  comment_count=99_999)
        # Missing metrics stay unknown, rather than being reported as zero.
        _, text = self.render_with_text(digest)
        for expected in ("点赞 — · 蹲蹲 0", "点赞 0 · 蹲蹲 — · 评论 0",
                         "点赞 1.2万 · 蹲蹲 1亿 · 评论 9.9万", "点赞 — · 蹲蹲 —"):
            # Separators are drawn dots, so segments appear as consecutive draws.
            self.assertIn(expected.replace(" · ", "\n"), text)
        self.assertNotIn("评论 —", text)
        self.assertNotIn("None", text)

    def test_footer_metrics_do_not_overlap_source_or_image_count(self):
        digest = fixture(4, ranking="likes")
        for i, item in enumerate(digest["items"]):
            item.update(id=(f"DEMO-{i:03}-" + "长来源编号" * 30) if i < 3 else "DEMO-SHORT",
                        image_count=9999, like_count=99_999_999,
                        trace_count=10 ** 100, comment_count=10 ** 100)
        positions = []
        paths, text = self.render_with_text(digest, positions)
        self.assertEqual(text.count("点赞 9999.9万\n蹲蹲 999亿+"), 4)
        self.assertEqual(text.count("评论 999亿+"), 1, "Omit comments when the source needs the space")
        self.assertEqual(text.count("配图 999+ 张"), 4)
        footers = [(label, box) for label, box in positions
                   if label.startswith(("原帖 #", "点赞 ", "配图"))]
        self.assertEqual(len(footers), 12)
        for offset in range(0, len(footers), 3):
            row = sorted(footers[offset:offset + 3], key=lambda entry: entry[1][0])
            for (_, left), (right_label, right) in zip(row, row[1:]):
                # Pixel assertions use real Pillow glyph bounds at render scale.
                # The image counter also needs room for its 20px icon.
                gap = 30 if right_label.startswith("配图") else 16
                self.assertLessEqual(left[2] + gap, right[0])
        with Image.open(paths[0]) as rendered:
            self.assertEqual(rendered.width, 1080)
            self.assertLessEqual(rendered.height, 2400)

    def test_invalid_optional_counts_raise_before_writing(self):
        for key in ("like_count", "trace_count", "comment_count"):
            for value in (-1, True, "12", 1.5):
                digest = fixture(1)
                digest["items"][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(self.cards.RenderError):
                    self.cards.render_digest(digest, self.temp.name)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_duplicate_title_is_omitted_and_original_body_is_drawn_once(self):
        sentence = "合成示例里的小动物真可爱"
        digest = fixture(1, mode="excerpt")
        digest["items"][0]["title"] = sentence
        for summary in (sentence, f" \t{sentence[:4]}　{sentence[4:]} 。！？ ", sentence + "..."):
            with self.subTest(summary=summary):
                digest["items"][0].update(body=summary, summary="AI摘要不能替代原文")
                positions = []
                paths, text = self.render_with_text(digest, positions)
                with Image.open(paths[0]) as rendered:
                    compact_height = rendered.height
                matching_draws = [label for label, _ in positions
                                  if "".join(label.split()).rstrip("。！？.!?") == sentence]
                self.assertEqual(len(matching_draws), 1)
                body_box = next(box for label, box in positions if label in matching_draws)
                self.assertLess(body_box[3] - body_box[1], 30, "Body uses 26px, not the 32px title")
                self.assertNotIn("AI摘要不能替代原文", text)
                self.assertNotIn("请查看原帖了解详情", text)
        digest["items"][0]["body"] = "另一句合成说明"
        paths, text = self.render_with_text(digest)
        self.assertIn("另一句合成说明", text)
        with Image.open(paths[0]) as rendered:
            self.assertGreater(rendered.height, compact_height)
        # A title clamped to two lines must not hide the longer summary's detail.
        long_text = "合成测试。" * 14 + "摘要里保留的细节"
        digest["items"][0].update(title=long_text[:20] + "…", body=long_text)
        _, text = self.render_with_text(digest)
        self.assertIn("摘要里保留的细节", text)
        for title in ("第一段原文", "第一段原文…", "第一段原文..."):
            digest["items"][0].update(title=title, body="第一段原文\n第二段保留更多内容")
            _, text = self.render_with_text(digest)
            self.assertEqual(text.count("第一段原文"), 1)
            self.assertIn("第二段保留更多内容", text)

    def color_region(self, image, color):
        """Locate source-image pixels with tolerance for JPEG/resampling noise."""
        difference = ImageChops.difference(image, Image.new("RGB", image.size, color))
        red, green, blue = difference.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
            lambda value: 255 if value < 12 else 0)
        return mask.getbbox(), mask.histogram()[255]

    def test_cached_thumbnail_pixels_preserve_all_corners_and_aspect_ratio(self):
        colors = ((204, 14, 157), (17, 191, 70), (242, 208, 10), (11, 211, 213))
        for width, height, displayed in ((1024, 2048, (170, 340)),
                                          (2048, 1024, (680, 340)),
                                          (160, 160, (340, 340))):
            with self.subTest(size=(width, height)):
                photo = Path(self.temp.name) / "synthetic-corners.jpg"
                with Image.new("RGB", (width, height)) as source:
                    draw = ImageDraw.Draw(source)
                    # Mark all four edges: cover/crop would lose these corner blocks.
                    draw.rectangle((0, 0, width - 1, height - 1), fill="#ECEFF2")
                    for box, color in zip((
                        (0, 0, width // 4, height // 4),
                        (3 * width // 4, 0, width - 1, height // 4),
                        (0, 3 * height // 4, width // 4, height - 1),
                        (3 * width // 4, 3 * height // 4, width - 1, height - 1),
                    ), colors):
                        draw.rectangle(box, fill=color)
                    source.save(photo, quality=95, subsampling=0)
                digest = fixture(1)
                digest["items"][0].update(image_path=str(photo), image_count=3)
                before = copy.deepcopy(digest)
                original = photo.read_bytes()
                paths, text = self.render_with_text(digest)
                self.assertEqual(digest, before)
                self.assertEqual(photo.read_bytes(), original)
                self.assertIn("配图 3 张", text)
                with Image.open(paths[0]) as rendered:
                    boxes = []
                    for color in colors:
                        box, pixels = self.color_region(rendered, color)
                        self.assertGreater(pixels, 100, "Source photo pixels must reach the PNG")
                        boxes.append(box)
                    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
                    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
                    self.assertGreater(left, 80)
                    self.assertLess(right, 1000)
                    self.assertAlmostEqual((right - left) / (bottom - top), width / height, delta=0.05)
                    # Small cache images fill the padded gallery frame too.
                    self.assertAlmostEqual(right - left, displayed[0], delta=2)
                    self.assertAlmostEqual(bottom - top, displayed[1], delta=2)

    def test_missing_corrupt_and_empty_image_paths_use_identical_text_layout(self):
        corrupt = Path(self.temp.name) / "corrupt.jpg"
        corrupt.write_bytes(b"not an image")
        truncated = Path(self.temp.name) / "truncated.jpg"
        with Image.new("RGB", (256, 256), "purple") as source:
            source.save(truncated)
        truncated.write_bytes(truncated.read_bytes()[:700])
        digest = fixture(1)
        digest["items"][0]["image_count"] = 3
        expected_paths, expected_text = self.render_with_text(digest)
        with Image.open(expected_paths[0]) as expected:
            expected_pixels, expected_size = expected.tobytes(), expected.size
        for image_path in (None, "", Path(self.temp.name) / "missing.jpg", corrupt, truncated):
            with self.subTest(image_path=image_path):
                digest["items"][0]["image_path"] = image_path
                paths, text = self.render_with_text(digest)
                self.assertEqual(text, expected_text)
                with Image.open(paths[0]) as rendered:
                    self.assertEqual(rendered.size, expected_size)
                    self.assertEqual(rendered.tobytes(), expected_pixels)

    def test_footer_keeps_commands_without_fixed_disclaimers(self):
        for mode in ("ai", "excerpt"):
            with self.subTest(mode=mode):
                _, text = self.render_with_text(fixture(2, mode=mode))
                for banned in ("未经核实", "图片未识别", "附图未识别"):
                    self.assertNotIn(banned, text)
                self.assertIn("/校园墙 详情", text)
                self.assertIn("/校园墙 图片", text)
                self.assertIn("原帖 #DEMO-001", text)

    def test_fifth_post_survives_pagination_boundary(self):
        paths, text = self.render_with_text(fixture(5))
        self.assertEqual(len(paths), 1)
        self.assertEqual(text.count("DEMO-005"), 1)

    def test_shown_selects_only_the_requested_items(self):
        paths, text = self.render_with_text(fixture(4, shown=2))
        self.assertEqual(len(paths), 1)
        self.assertIn("本期展示", text)
        self.assertIn("DEMO-002", text)
        self.assertNotIn("DEMO-003", text)

    def test_wrap_preserves_chinese_punctuation_paragraphs_and_long_tokens(self):
        font = self.fonts.get(28)
        source = "“读书计划”（第二期）：欢迎同学参与，一起交流！\n\nhttps://example.invalid/" + "Abc0123" * 35
        lines = self.cards._wrap(source, font, 275, 100)
        self.assertIn("", lines)
        self.assertEqual("".join(lines), source.replace("\n", ""))
        for line in lines:
            self.assertLessEqual(font.getlength(line), 275)
            if line:
                self.assertNotIn(line[0], "，。！？；：、）】》”’")
                self.assertNotIn(line[-1], "（【《“‘")

    def test_clamping_is_visible_and_keeps_every_line_in_bounds(self):
        font = self.fonts.get(28)
        lines = self.cards._wrap("很长的内容。" * 2000, font, 310, 3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(font.getlength(line) <= 310 for line in lines))
        self.assertEqual(lines, self.cards._wrap("很长的内容。" * 2000, font, 310, 3))

    def test_original_body_after_three_lines_and_paragraphs_survives(self):
        digest = fixture(1)
        body = "合成原文段落。" * 90 + "\n\n正文末行仍可见"
        self.assertLessEqual(len(body), 720)
        digest["items"][0].update(body=body, summary="摘要不能代替正文", truncated=True)
        positions = []
        paths, text = self.render_with_text(digest, positions)
        self.assertIn("正文末行仍可见", text)
        self.assertIn("原文节选", text)
        self.assertNotIn("摘要不能代替正文", text)
        paragraphs = [box for label, box in positions if "合成原文" in label]
        final = next(box for label, box in positions if label == "正文末行仍可见")
        self.assertGreater(len(paragraphs), 3)
        self.assertGreater(final[1] - paragraphs[-1][1], 39)
        with Image.open(paths[0]) as rendered:
            self.assertLessEqual(rendered.height, 16000)

    def test_top_ten_plan_is_pure_and_renders_exactly_one_png_in_rank_order(self):
        digest = fixture(15, ranking="likes", total=369)
        before = copy.deepcopy(digest)
        with patch.object(Image, "open", side_effect=AssertionError("planner decoded a photo")):
            plan = self.cards.plan_digest(digest)
        self.assertEqual(digest, before)
        self.assertEqual(plan, {"pages": [list(range(10))], "shown": 10, "omitted": 5})
        self.assertFalse(list(Path(self.temp.name).iterdir()))
        paths, text = self.render_with_text(digest, plan=plan)
        self.assertEqual(len(paths), 1)
        for i in range(10):
            self.assertEqual(text.count(f"原帖 #DEMO-{i + 1:03}"), 1)
        self.assertNotIn("DEMO-011", text)
        self.assertLess(text.index("DEMO-009"), text.index("DEMO-010"))
        self.assertIn("本期精选 10 条，另有 359 条未入选", text)
        self.assertNotIn("另有 0 条", text)
        for page, path in enumerate(paths):
            with Image.open(path) as rendered:
                self.assertEqual(rendered.width, 1080)
                self.assertLessEqual(rendered.height, 16000)
            self.assertIn(f"{page + 1} / {len(paths)}", text)

    def test_ten_long_bodies_with_galleries_are_bounded_and_decode_one_post_at_a_time(self):
        digest = fixture(13, ranking="likes", scan_limited=True, intro="长导语" * 200)
        photo = Path(self.temp.name) / "long-gallery.jpg"
        with Image.new("RGB", (160, 160), "#CC0E9D") as source:
            source.save(photo)
        for item in digest["items"]:
            item.update(title="短标题", body="原文" * 990 + "完整原文末尾", image_count=3, gallery_count=3)
        plan = self.cards.plan_digest(digest)
        self.assertEqual(plan, {"pages": [list(range(10))], "shown": 10, "omitted": 3})
        digest["items"] = digest["items"][:plan["shown"]]
        digest["omitted"] = 3
        # Keep the old digest.shown intentionally: the supplied plan owns selection.
        for item in digest["items"]:
            item.update(title="很长的新AI标题" * 60, image_paths=[str(photo)] * 3)
        loaded, allocations = [], []
        original_load, original_new = self.cards._load_thumbnail, Image.new

        def load_current_post(*args):
            if loaded and len(loaded) % 3 == 0:
                for previous in loaded[-3:]:
                    with self.assertRaises(ValueError):
                        previous.getpixel((0, 0))
            image = original_load(*args)
            loaded.append(image)
            return image

        def track_allocation(mode, size, *args, **kwargs):
            allocations.append(size)
            return original_new(mode, size, *args, **kwargs)

        with patch.object(self.cards, "_load_thumbnail", load_current_post), patch.object(Image, "new", track_allocation):
            paths, text = self.render_with_text(digest, plan=plan)
        self.assertEqual(len(loaded), 30)
        self.assertTrue(all(width <= 1080 and height <= 16000 for width, height in allocations))
        for previous in loaded:
            with self.assertRaises(ValueError):
                previous.getpixel((0, 0))
        self.assertEqual(len(paths), 1)
        self.assertIn("本期精选 10 条，另有 3 条未入选", text)
        self.assertEqual(text.count("原文节选"), 10)
        self.assertNotIn("完整原文末尾", text)
        self.assertNotIn("DEMO-011", text)
        for path in paths:
            with Image.open(path) as rendered:
                self.assertLessEqual(rendered.height, 16000)
                self.assertGreater(rendered.height, 14000)

    def test_disabled_gallery_low_counts_and_extreme_paragraphs(self):
        digest = fixture(2)
        for item in digest["items"]:
            item.update(image_count=99, gallery_count=0)
            item["image_paths"] = ["disabled.jpg"] * 3
        with patch.object(self.cards, "_load_thumbnail", side_effect=AssertionError("disabled gallery decoded")):
            paths, _ = self.render_with_text(digest)
        self.assertEqual(len(paths), 1)
        for count in range(4):
            plan = self.cards.plan_digest(fixture(count))
            self.assertEqual(len(plan["pages"]), 1 if count else 0)
            self.assertEqual(plan["shown"], count)
        huge = fixture(1)
        huge["items"][0]["body"] = "字\n" * 1000
        paths, text = self.render_with_text(huge)
        self.assertEqual(len(paths), 1)
        self.assertIn("原文节选", text)

    def test_two_and_three_photo_galleries_keep_corners_below_full_width_body(self):
        paths, palettes = [], []
        for i, (width, height) in enumerate(((320, 640), (640, 320), (160, 160))):
            palette = ((170 + i * 25, 15, 145), (15, 160 + i * 25, 45),
                       (230, 170 + i * 25, 15), (15, 170 + i * 25, 220))
            palettes.append(palette)
            path = Path(self.temp.name) / f"gallery-{i}.jpg"
            with Image.new("RGB", (width, height), "#ECEFF2") as source:
                draw = ImageDraw.Draw(source)
                for box, color in zip(((0, 0, width // 4, height // 4),
                                       (3 * width // 4, 0, width - 1, height // 4),
                                       (0, 3 * height // 4, width // 4, height - 1),
                                       (3 * width // 4, 3 * height // 4, width - 1, height - 1)), palette):
                    draw.rectangle(box, fill=color)
                source.save(path, quality=95, subsampling=0)
            paths.append(str(path))
        for count in (2, 3):
            with self.subTest(count=count):
                digest = fixture(1)
                digest["items"][0].update(body="全宽原文" * 10 + "\n正文终点", image_count=5,
                                          gallery_count=count, image_paths=paths[:count])
                positions = []
                outputs, text = self.render_with_text(digest, positions)
                body_bottom = next(box[3] for label, box in positions if label == "正文终点")
                self.assertIn("全宽原文" * 8, text)
                with Image.open(outputs[0]) as rendered:
                    for palette in palettes[:count]:
                        for color in palette:
                            box, pixels = self.color_region(rendered, color)
                            self.assertGreater(pixels, 100)
                            self.assertGreater(box[1], body_bottom)

    def test_supplied_plan_rejects_nonprefix_indices_before_creating_files(self):
        for pages in ([[1]], [[0], [2]], [[0], []], [[0, 0]], [[True]]):
            plan = {"pages": pages, "shown": sum(map(len, pages)), "omitted": 0}
            with self.subTest(pages=pages), self.assertRaises(self.cards.RenderError):
                self.cards.render_digest(fixture(3), self.temp.name, plan=plan)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_excerpt_mode_and_cross_midnight_window(self):
        paths, text = self.render_with_text(fixture(
            1, mode="excerpt", window_start="2026-11-12T20:00:00+08:00",
            window_end="2026-11-13T00:00:00+08:00"))
        self.assertEqual(len(paths), 1)
        for expected in ("原文摘录", "11.12", "11.13", "20:00", "00:00"):
            self.assertIn(expected, text)
        self.assertNotIn("AI 整理", text)

    def test_interval_labels_follow_six_and_twenty_four_hour_windows(self):
        for end, masthead, footer in (
            ("2026-11-12T10:00:00+00:00", "每 6 小时更新", "每 6 小时一辑"),
            ("2026-11-13T04:00:00+00:00", "每 24 小时更新", "每 24 小时一辑"),
        ):
            with self.subTest(end=end):
                _, text = self.render_with_text(fixture(1, window_end=end))
                self.assertIn(masthead, text)
                self.assertIn(footer, text)
                self.assertNotIn("每 4 小时", text)

    def test_collections_use_the_simplified_chinese_face_and_a_bold_weight(self):
        regular, bold = self.fonts.get(28), self.fonts.get(28, bold=True)
        if self.fonts.path.endswith("NotoSansCJK-Regular.ttc"):
            self.assertEqual(regular.getname(), ("Noto Sans CJK SC", "Regular"))
            self.assertEqual(bold.getname(), ("Noto Sans CJK SC", "Bold"))
        self.assertIsNot(regular, bold)

    def test_category_colors_are_stable_and_semantic(self):
        self.assertEqual(self.cards._tag_color("失物招领"), self.cards._TAGS["orange"])
        self.assertEqual(self.cards._tag_color("某个新分类"), self.cards._tag_color("某个新分类"))

    def test_invalid_or_non_cjk_explicit_font_fails_without_fallback(self):
        with self.assertRaises(self.cards.RenderError):
            self.cards.render_digest(fixture(1), self.temp.name, "missing-cjk.ttf")
        latin = Path("C:/Windows/Fonts/arial.ttf")
        if latin.exists():
            with self.assertRaises(self.cards.RenderError):
                self.cards.render_digest(fixture(1), self.temp.name, str(latin))
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_no_auto_font_fails_clearly(self):
        with patch.object(self.cards, "_font_candidates", return_value=[]):
            with self.assertRaisesRegex(self.cards.RenderError, "CJK"):
                self.cards.render_digest(fixture(1), self.temp.name)

    def test_explicit_valid_font_works(self):
        paths = self.cards.render_digest(fixture(1), self.temp.name, self.fonts.path)
        self.assertEqual(len(paths), 1)

    def test_unknown_glyphs_emoji_and_controls_never_reach_pillow(self):
        source = "好👩‍🎓👍🏽🇨🇳1️⃣\u202e文\x00\U0010ffff结束"
        cleaned = self.fonts.clean(source)
        self.assertIn("好", cleaned)
        self.assertIn("结束", cleaned)
        for forbidden in ("👩", "🎓", "👍", "🇨", "\u200d", "\ufe0f", "\u202e", "\x00", "\U0010ffff"):
            self.assertNotIn(forbidden, cleaned)
        missing = bytes(self.fonts.get(22).getmask("\U0010ffff"))
        for char in cleaned:
            if not char.isspace():
                self.assertNotEqual(bytes(self.fonts.get(22).getmask(char)), missing)

    def test_empty_digest_creates_no_images_and_needs_no_font(self):
        with patch.object(self.cards, "_font_candidates", return_value=[]):
            self.assertEqual(self.cards.render_digest(fixture(0, total=0), self.temp.name), [])
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_invalid_contracts_raise_render_error_before_writing(self):
        for changes in (
            {"window_start": "2026-11-12T12:00:00"},
            {"window_end": "2026-11-12T00:00:00+08:00"},
            {"mode": "official"}, {"total": 0}, {"shown": 3},
            {"scan_limited": "false"}, {"items": "bad"},
        ):
            with self.subTest(changes=changes), self.assertRaises(self.cards.RenderError):
                self.cards.render_digest(fixture(1, **changes), self.temp.name)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_two_runs_do_not_overwrite_previous_files(self):
        first = self.cards.render_digest(fixture(1), self.temp.name)
        original = Path(first[0]).read_bytes()
        second = self.cards.render_digest(fixture(1, mode="excerpt"), self.temp.name)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first[0]).read_bytes(), original)

    def test_write_failure_is_wrapped_and_only_this_batch_is_cleaned_up(self):
        existing = self.cards.render_digest(fixture(1), self.temp.name)[0]
        def fail_save(image, fp, *args, **kwargs):
            Path(fp).write_bytes(b"partial PNG")
            raise OSError("synthetic disk failure")

        with patch.object(Image.Image, "save", fail_save):
            with self.assertRaises(self.cards.RenderError):
                self.cards.render_digest(fixture(8), self.temp.name)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [Path(existing)])


if __name__ == "__main__":
    unittest.main()
