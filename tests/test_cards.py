"""Renderer contracts, real font layout, and saved PNG integration checks."""

import copy
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

    def render_with_text(self, digest):
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
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", capture):
            paths = self.cards.render_digest(digest, self.temp.name)
        return paths, "\n".join(drawn)

    def test_png_pagination_counts_provenance_and_timezone(self):
        digest = fixture(8, scan_limited=True)
        before = copy.deepcopy(digest)
        paths, text = self.render_with_text(digest)
        self.assertEqual(len(paths), 2)
        self.assertEqual(digest, before)
        for path in paths:
            self.assertTrue(Path(path).is_absolute())
            with Image.open(path) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.mode, "RGB")
                self.assertEqual(im.width, 1080)
                self.assertLessEqual(im.height, 2400)
                self.assertGreater(im.height, 1000)
            self.assertLess(Path(path).stat().st_size, 2_000_000)
        for expected in ("本期展示", "12:00", "16:00", "北京时间", "AI",
                         "12", "8", "扫描未完成", "配图2张", "1 / 2", "2 / 2"):
            self.assertIn(expected, text)
        for i in range(1, 9):
            self.assertEqual(text.count(f"DEMO-{i:03}"), 1)
        self.assertEqual(text.count("寻找周末一起读书的同学"), 8)
        self.assertNotIn("非官方", text)

    def color_region(self, image, color):
        """Locate source-image pixels with tolerance for JPEG/resampling noise."""
        difference = ImageChops.difference(image, Image.new("RGB", image.size, color))
        red, green, blue = difference.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
            lambda value: 255 if value < 12 else 0)
        return mask.getbbox(), mask.histogram()[255]

    def test_cached_thumbnail_pixels_preserve_all_corners_and_aspect_ratio(self):
        colors = ((204, 14, 157), (17, 191, 70), (242, 208, 10), (11, 211, 213))
        for width, height, displayed in ((1024, 2048, (94, 188)),
                                          (2048, 1024, (212, 106)),
                                          (160, 160, (188, 188))):
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
                self.assertIn("配图3张", text)
                with Image.open(paths[0]) as rendered:
                    boxes = []
                    for color in colors:
                        box, pixels = self.color_region(rendered, color)
                        self.assertGreater(pixels, 100, "Source photo pixels must reach the PNG")
                        boxes.append(box)
                    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
                    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
                    self.assertGreater(left, 700, "Thumbnail belongs beside the text")
                    self.assertLess(right, 1000)
                    self.assertAlmostEqual((right - left) / (bottom - top), width / height, delta=0.05)
                    # Small cache images must fill the padded frame too, after
                    # the 2x canvas is reduced to the saved PNG's logical pixels.
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
                self.assertIn("/校园圈 详情", text)
                self.assertIn("/校园圈 图片", text)
                self.assertIn("原帖 #DEMO-001", text)

    def test_fifth_post_survives_pagination_boundary(self):
        paths, text = self.render_with_text(fixture(5))
        self.assertEqual(len(paths), 2)
        self.assertEqual(text.count("DEMO-005"), 1)
        with Image.open(paths[0]) as first, Image.open(paths[1]) as second:
            self.assertGreater(first.height, second.height)

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

    def test_worst_case_layout_has_no_off_canvas_text_and_caps_eight_items(self):
        digest = fixture(11, scan_limited=True, intro="很长的介绍，" * 3000,
                         community="很长的校园墙名称" * 3000)
        colors = [(90 + i * 20, 12, 170) for i in range(8)]
        for i, item in enumerate(digest["items"]):
            photo = Path(self.temp.name) / f"synthetic-{i}.jpg"
            with Image.new("RGB", (2048, 1536), colors[i % 8]) as source:
                source.save(photo, quality=95, subsampling=0)
            item.update(title="长标题（中英文 Mixed）：" * 3000,
                        summary=("超长段落👩‍🎓😀\n\n" + "Token" * 400) * 20,
                        category="很长的分类名称" * 200, image_count=99,
                        image_path=photo)
        paths, text = self.render_with_text(digest)
        self.assertEqual(len(paths), 2)
        for page, path in enumerate(paths):
            with Image.open(path) as im:
                self.assertEqual(im.width, 1080)
                self.assertLessEqual(im.height, 2400)
                for i, color in enumerate(colors):
                    _, pixels = self.color_region(im, color)
                    if page * 4 <= i < (page + 1) * 4:
                        self.assertGreater(pixels, 1000, f"Missing thumbnail for item {i + 1}")
                    else:
                        self.assertEqual(pixels, 0, "Only four thumbnails belong on each page")
        self.assertIn("…", text)
        self.assertNotIn("DEMO-009", text)
        self.assertIn("DEMO-008", text)
        self.assertNotIn("😀", text)
        self.assertNotIn("\u200d", text)

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
            ("2026-11-12T10:00:00+00:00", "CAMPUS DIGEST·每 6 小时", "每 6 小时一辑"),
            ("2026-11-13T04:00:00+00:00", "CAMPUS DIGEST·每 24 小时", "每 24 小时一辑"),
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
        original = Image.Image.save
        calls = 0

        def fail_second(image, fp, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic disk failure")
            return original(image, fp, *args, **kwargs)

        with patch.object(Image.Image, "save", fail_second):
            with self.assertRaises(self.cards.RenderError):
                self.cards.render_digest(fixture(8), self.temp.name)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [Path(existing)])


if __name__ == "__main__":
    unittest.main()
