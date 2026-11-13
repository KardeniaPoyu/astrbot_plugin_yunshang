"""Local, bounded campus digest cards. Requires Pillow and a real CJK font.

Call ``render_digest`` in a worker thread. It returns absolute PNG paths in
page order; the caller owns their lifecycle. Catch ``RenderError`` to use the
text fallback. There is no network access or persistent state in this module.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import unicodedata
from uuid import uuid4

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # Allow the host to import RenderError and use text fallback.
    Image = ImageDraw = ImageFilter = ImageFont = None


class RenderError(RuntimeError):
    """The digest cannot be safely rendered; use the caller's text fallback."""


WIDTH = 1080
MAX_HEIGHT = 2400
_CST = timezone(timedelta(hours=8))
_SCALE = 2  # Draw at 2x and downsample: Pillow shapes are not anti-aliased.
_THUMB_SIZE = (232, 208)
_THUMB_PAD = 10
_THUMB_GAP = 24
# Neutral text/surface tokens follow Lark (Feishu) UD; tags and alerts reuse
# Ant Design v5 preset palettes, so the card reads like a familiar IM card.
_BG = "#F2F3F5"
_SURFACE = "#FFFFFF"
_TEXT = "#1F2329"
_TEXT_2 = "#646A73"
_TEXT_3 = "#8F959E"
_BORDER = "#DEE0E3"
_DIVIDER = "#EFF0F1"
_BRAND = "#3370FF"
_BRAND_DEEP = "#1A3B9E"
_BRAND_SOFT = "#E1EAFF"
_SHADOW = (31, 35, 41)
# Ant Design preset (background, border, text) triples.
_TAGS = {
    "blue": ("#E6F4FF", "#91CAFF", "#0958D9"),
    "green": ("#F6FFED", "#B7EB8F", "#389E0D"),
    "orange": ("#FFF7E6", "#FFD591", "#D46B08"),
    "purple": ("#F9F0FF", "#D3ADF7", "#531DAB"),
    "cyan": ("#E6FFFB", "#87E8DE", "#08979C"),
    "magenta": ("#FFF0F6", "#FFADD2", "#C41D7F"),
    "geekblue": ("#F0F5FF", "#ADC6FF", "#1D39C4"),
    "volcano": ("#FFF2E8", "#FFBB96", "#D4380D"),
    "gold": ("#FFFBE6", "#FFE58F", "#D48806"),
}
_TAG_KEYWORDS = (
    (("失物", "招领", "寻物", "丢"), "orange"),
    (("学习", "自习", "考研", "课程", "搭子"), "blue"),
    (("二手", "交易", "出闲置", "转让", "旧书", "交换"), "green"),
    (("表白", "情感", "交友", "恋爱"), "magenta"),
    (("活动", "社团", "比赛", "讲座"), "purple"),
    (("求助", "提问", "咨询", "吐槽"), "volcano"),
    (("兼职", "实习", "招聘"), "cyan"),
)
_TAG_FALLBACK = ("geekblue", "cyan", "purple", "green", "blue")
_OPENING = frozenset("（【《〈「『〔［｛“‘([{«")
_CLOSING = frozenset("，。！？；：、）】》〉」』〕］｝”’…,.!?;:)]}»")
_EMOJI = re.compile(
    r"(?:[0-9#*]\ufe0f?\u20e3|[\U0001F000-\U0001FAFF\u2600-\u27BF]"
    r"[\ufe0e\ufe0f\u200d]*|[\u200d\ufe0e\ufe0f])+"
)
_REQUIRED = {
    "community", "window_start", "window_end", "total", "shown",
    "scan_limited", "mode", "intro", "items",
}
_ITEM_KEYS = {"id", "title", "summary", "published_at", "category", "image_count"}


def _font_candidates():
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        windows / "msyh.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
        Path("/usr/local/share/fonts/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]


def _bold_sibling(path):
    """The conventional bold file next to a regular CJK font, if installed."""
    name = path.name
    for regular, bold in (("Regular", "Bold"), ("msyh.", "msyhbd.")):
        if regular in name:
            sibling = path.with_name(name.replace(regular, bold))
            if sibling.exists():
                return sibling
    return path


def _face(path, style):
    """Pick the Simplified Chinese face of a collection; index 0 is often JP.

    Noto CJK collections start with the Japanese face, which draws many shared
    code points with Japanese glyph shapes. PingFang keeps weights in one file.
    """
    best, best_score = 0, -1
    for index in range(32):
        try:
            family, face_style = ImageFont.truetype(str(path), 12, index=index).getname()
        except (OSError, ValueError):
            break
        score = 2 * family.endswith(" SC") + (face_style == style)
        if score > best_score:
            best, best_score = index, score
    return best


class _Fonts:
    """Per-render font/glyph caches, so concurrent renders share no mutable state."""

    def __init__(self, explicit):
        if ImageFont is None:
            raise RenderError("Pillow is required to render digest cards")
        self._sizes = {}
        self._coverage = {}
        candidates = [Path(explicit).expanduser()] if explicit else _font_candidates()
        for path in candidates:
            try:
                index = _face(path, "Regular")
                font = ImageFont.truetype(str(path), 22, index=index)
                missing = self._signature(font, "\U0010ffff")
                # Loading a .ttf successfully is not proof of Chinese support.
                probes = "校园同学投稿图摘录时间，。…〔〕"
                if any(self._signature(font, char) == missing for char in probes):
                    continue
                self.path = str(path)
                self._faces = {False: (self.path, index)}
                bold = path if explicit else _bold_sibling(path)
                bold_index = _face(bold, "Bold")
                if (bold, bold_index) == (path, index):
                    self._faces[True] = self._faces[False]
                else:
                    self._faces[True] = (str(bold), bold_index)
                self._sizes[22, False] = font
                self._missing = missing
                return
            except (OSError, ValueError):
                continue
        raise RenderError(
            "No usable CJK font. Install msyh.ttc or NotoSansCJK-Regular.ttc, "
            "or supply font_path to a Chinese-capable font."
        )

    @staticmethod
    def _signature(font, char):
        mask = font.getmask(char)
        return mask.size, bytes(mask)

    def get(self, size, bold=False):
        key = size, bold
        if key not in self._sizes:
            path, index = self._faces[bold]
            try:
                self._sizes[key] = ImageFont.truetype(path, size, index=index)
            except OSError:
                if not bold:
                    raise
                self._sizes[key] = self.get(size)
        return self._sizes[key]

    def clean(self, value, limit=4096):
        """Bound work; replace unsupported glyphs visibly, never with font tofu."""
        text = str(value) if value is not None else ""
        clipped = len(text) > limit
        text = unicodedata.normalize("NFC", text[:limit])
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _EMOJI.sub("〔表情〕", text)
        out = []
        for char in text:
            if char == "\n":
                out.append(char)
            elif char.isspace():
                out.append(" ")
            elif unicodedata.category(char).startswith("C"):
                # Drop invisible controls, but expose unknown/private-use characters.
                if unicodedata.category(char) in {"Co", "Cn"}:
                    out.append("〔缺字〕")
            else:
                if char not in self._coverage:
                    self._coverage[char] = self._signature(self.get(22), char) != self._missing
                out.append(char if self._coverage[char] else "〔缺字〕")
        result = re.sub(r"[^\S\n]+", " ", "".join(out)).strip()
        return result + ("…" if clipped else "")


def _ellipsis(text, font, width):
    text = text.rstrip().rstrip("…")
    while text and (font.getlength(text + "…") > width or text[-1] in _OPENING):
        text = text[:-1].rstrip()
    return text + "…"


def _wrap(text, font, width, max_lines):
    """Pixel wrapping with paragraph breaks, kinsoku punctuation, and hard limits.

    Prefer a word boundary for Latin text; split overlong tokens when necessary.
    For a pathological punctuation-only run, pixel safety takes precedence over
    punctuation rules. Work stops after max_lines + 1 rather than wrapping the
    entire input before truncating it.
    """
    if max_lines < 1 or width < font.getlength("…"):
        raise RenderError("Text region is too small")
    lines = []
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for paragraph_index, paragraph in enumerate(paragraphs):
        rest = paragraph.strip()
        if not rest:
            lines.append("")
        while rest:
            low, high = 0, len(rest)
            while low < high:
                middle = (low + high + 1) // 2
                if font.getlength(rest[:middle]) <= width:
                    low = middle
                else:
                    high = middle - 1
            take = low
            if take == 0:
                raise RenderError("A glyph exceeds the text region")
            if take < len(rest):
                # Avoid breaking a short English word if there is a usable space.
                if rest[take - 1].isascii() and rest[take].isascii():
                    space = rest.rfind(" ", 0, take)
                    if space >= take // 2:
                        take = space + 1
                # Moving the preceding character with closing punctuation keeps
                # the line width fixed and does not hang punctuation off-canvas.
                candidate = take
                while candidate > 0 and (
                    rest[candidate - 1] in _OPENING or rest[candidate] in _CLOSING
                ):
                    candidate -= 1
                if candidate:
                    take = candidate
            lines.append(rest[:take].rstrip())
            rest = rest[take:].lstrip()
            if len(lines) == max_lines:
                more = rest or any(p.strip() for p in paragraphs[paragraph_index + 1:])
                if more:
                    lines[-1] = _ellipsis(lines[-1], font, width)
                return lines
        if len(lines) >= max_lines:
            if any(p.strip() for p in paragraphs[paragraph_index + 1:]):
                lines[-1] = _ellipsis(lines[-1], font, width)
            return lines[:max_lines]
    return lines or [""]


def _window(value):
    if not isinstance(value, str):
        raise RenderError("Time windows must be timezone-aware ISO strings")
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError("naive window")
        return result.astimezone(_CST)
    except (ValueError, OverflowError) as exc:
        raise RenderError("Time windows must be timezone-aware ISO strings") from exc


def _validate(digest):
    if not isinstance(digest, dict) or not _REQUIRED.issubset(digest):
        raise RenderError("Missing required digest fields")
    start, end = _window(digest["window_start"]), _window(digest["window_end"])
    if start >= end:
        raise RenderError("Digest window end must follow start")
    if digest["mode"] not in ("ai", "excerpt"):
        raise RenderError("Digest mode must be ai or excerpt")
    if type(digest["scan_limited"]) is not bool:
        raise RenderError("scan_limited must be boolean")
    if any(type(digest[key]) is not int or digest[key] < 0 for key in ("total", "shown")):
        raise RenderError("Digest counts must be non-negative integers")
    items = digest["items"]
    if not isinstance(items, list) or not (digest["shown"] <= len(items) and digest["shown"] <= digest["total"]):
        raise RenderError("Digest counts do not match available items")
    selected = items[:min(digest["shown"], 8)]
    for item in selected:
        if not isinstance(item, dict) or not _ITEM_KEYS.issubset(item):
            raise RenderError("Missing required item fields")
        if not str(item["id"] or "").strip():
            raise RenderError("Every item requires a source post ID")
        if type(item["image_count"]) is not int or item["image_count"] < 0:
            raise RenderError("image_count must be a non-negative integer")
    return start, end, selected


def _published(value, start):
    try:
        timestamp = datetime.fromisoformat(str(value))
        # API timestamps without an offset are already campus local time.
        timestamp = timestamp.replace(tzinfo=_CST) if timestamp.utcoffset() is None else timestamp.astimezone(_CST)
        return timestamp.strftime("%H:%M" if timestamp.date() == start.date() else "%m.%d %H:%M")
    except (ValueError, OverflowError):
        return "时间未提供"


def _tag_color(category):
    for words, color in _TAG_KEYWORDS:
        if any(word in category for word in words):
            return _TAGS[color]
    # Stable across runs and processes, unlike hash() on str.
    return _TAGS[_TAG_FALLBACK[sum(map(ord, category)) % len(_TAG_FALLBACK)]]


def _load_thumbnail(image_path):
    """Read one trusted cache JPEG; failed/expired files leave a text-only item.

    The caller normalizes orientation and caps each edge at 2048px. Decode now
    so truncated images cannot fail later during drawing. Never fetch a URL.
    The returned RGB image is owned by the current render, not by the cache.
    """
    if not image_path:
        return None
    try:
        with Image.open(Path(image_path), formats=("JPEG",)) as source:
            if max(source.size) > 2048:
                return None
            with source.convert("RGB") as thumbnail:
                bounds = tuple((edge - 2 * _THUMB_PAD) * _SCALE for edge in _THUMB_SIZE)
                fit = min(bounds[0] / thumbnail.width, bounds[1] / thumbnail.height)
                # Fit in both directions at canvas resolution, including upscale:
                # thumbnail() alone leaves small cache images tiny after downsampling.
                size = tuple(max(1, round(edge * fit)) for edge in thumbnail.size)
                return thumbnail.resize(size, Image.LANCZOS)
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None


class _Canvas:
    """Logical-pixel drawing on a supersampled image; text must stay in bounds."""

    def __init__(self, fonts, height):
        self.fonts = fonts
        self.image = Image.new("RGB", (WIDTH * _SCALE, height * _SCALE), _BG)
        self.draw = ImageDraw.Draw(self.image)
        self.shadows = Image.new("L", self.image.size, 0)
        self._shadow_draw = ImageDraw.Draw(self.shadows)

    @staticmethod
    def _s(values):
        return tuple(round(v * _SCALE) for v in values)

    def font(self, size, bold=False):
        return self.fonts.get(size * _SCALE, bold)

    def wrap(self, text, size, width, lines, bold=False):
        return _wrap(text, self.font(size, bold), width * _SCALE, lines)

    def length(self, text, size, bold=False):
        return self.font(size, bold).getlength(text) / _SCALE

    def text(self, xy, text, size, fill=_TEXT, bold=False, anchor="lt"):
        self.draw.text(self._s(xy), text, font=self.font(size, bold), fill=fill, anchor=anchor)

    def box(self, box, radius, fill=None, outline=None, width=1):
        self.draw.rounded_rectangle(self._s(box), radius=radius * _SCALE, fill=fill,
                                    outline=outline, width=width * _SCALE if outline else 0)

    def line(self, points, fill, width=1):
        flat = [value for point in points for value in point]
        self.draw.line(self._s(flat), fill=fill, width=width * _SCALE, joint="curve")

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.draw.ellipse(self._s(box), fill=fill, outline=outline,
                          width=width * _SCALE if outline else 0)

    def shadow(self, box, radius, strength):
        """Queue an elevation shadow; composited under surfaces by ``elevate``."""
        x0, y0, x1, y1 = box
        self._shadow_draw.rounded_rectangle(self._s((x0 + 2, y0 + 6, x1 - 2, y1 + 6)),
                                            radius=radius * _SCALE, fill=strength)

    def elevate(self):
        blurred = self.shadows.filter(ImageFilter.GaussianBlur(14 * _SCALE))
        self.image.paste(Image.new("RGB", self.image.size, _SHADOW), mask=blurred)
        self.shadows.close()

    def tag(self, x, y, label, colors, size=19):
        background, border, ink = colors
        width = self.length(label, size) + 20
        self.box((x, y, x + width, y + 32), 6, background, border)
        self.text((x + 10, y + 16), label, size, ink, anchor="lm")
        return width

    def clock(self, cx, cy, color):
        self.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=color, width=2)
        self.line(((cx, cy - 5), (cx, cy), (cx + 4, cy + 3)), color, 2)

    def picture(self, cx, cy, color):
        self.box((cx - 10, cy - 8, cx + 10, cy + 8), 3, outline=color, width=2)
        self.draw.polygon(self._s((cx - 7, cy + 5, cx - 2, cy - 1, cx + 2, cy + 3,
                                   cx + 4, cy + 1, cx + 7, cy + 5)), fill=color)
        self.ellipse((cx + 2, cy - 5, cx + 6, cy - 1), fill=color)

    def thumbnail(self, x, y, image):
        width, height = _THUMB_SIZE
        self.box((x, y, x + width, y + height), 12, "#F7F8FA", _BORDER)
        # Contain within an inset frame: even screenshot corners stay visible.
        left = round(x * _SCALE) + (width * _SCALE - image.width) // 2
        top = round(y * _SCALE) + (height * _SCALE - image.height) // 2
        self.image.paste(image, (left, top))

    def badge(self, cx, cy, glyph, fill, ink="#FFFFFF"):
        """Filled round status icon (info / warning) with a centered glyph."""
        self.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=fill)
        self.text((cx, cy + 1), glyph, 16, ink, bold=True, anchor="mm")

    def finish(self):
        result = self.image.resize((WIDTH, self.image.height // _SCALE), Image.LANCZOS)
        self.image.close()
        return result


def _item_layout(item, canvas, start, number, thumbnail):
    clean = canvas.fonts.clean
    text_width = 920 - _THUMB_SIZE[0] - _THUMB_GAP if thumbnail is not None else 920
    title = canvas.wrap(clean(item["title"], 320) or "同学分享", 32, text_width, 2, bold=True)
    summary = canvas.wrap(clean(item["summary"]) or "请查看原帖了解详情。", 26, text_width, 3)
    category = canvas.wrap(clean(item["category"], 64).replace("\n", " ") or "校园日常", 19, 560, 1)[0]
    source = canvas.wrap("原帖 #" + clean(item["id"], 128).replace("\n", " "), 20, 520, 1)[0]
    images = ""
    if item["image_count"]:
        count = str(item["image_count"]) if item["image_count"] < 1000 else "999+"
        images = f"配图{count}张"
    when = canvas.wrap(_published(item["published_at"], start), 20, 300, 1)[0]
    # The image shares the bounded title/summary block instead of adding a row.
    body_height = max(44 * len(title) + 10 + 39 * len(summary),
                      _THUMB_SIZE[1] if thumbnail is not None else 0)
    height = 28 + 32 + 18 + body_height + 18 + 16 + 24 + 24
    return dict(title=title, summary=summary, category=category, source=source,
                images=images, when=when, height=height, number=number,
                colors=_tag_color(category), thumbnail=thumbnail, body_height=body_height)


def _draw_item(canvas, item, y):
    x0, x1 = 40, 1040
    left, right = x0 + 40, x1 - 40
    canvas.box((x0, y, x1, y + item["height"]), 16, _SURFACE, _BORDER)
    row = y + 28
    canvas.box((left, row, left + 44, row + 32), 8, _BRAND_SOFT)
    canvas.text((left + 22, row + 16), f"{item['number']:02}", 19, _BRAND, bold=True, anchor="mm")
    canvas.tag(left + 56, row, item["category"], item["colors"])
    when_width = canvas.length(item["when"], 20)
    canvas.text((right, row + 16), item["when"], 20, _TEXT_3, anchor="rm")
    canvas.clock(right - when_width - 18, row + 16, _TEXT_3)
    row += 32 + 18
    body_top = row
    if item["thumbnail"] is not None:
        canvas.thumbnail(right - _THUMB_SIZE[0], row, item["thumbnail"])
    for line in item["title"]:
        canvas.text((left, row), line, 32, _TEXT, bold=True)
        row += 44
    row += 10
    for line in item["summary"]:
        canvas.text((left, row), line, 26, _TEXT_2)
        row += 39
    row = body_top + item["body_height"] + 18
    canvas.line(((left, row), (right, row)), _DIVIDER)
    row += 16 + 12
    canvas.text((left, row), item["source"], 20, _TEXT_3, anchor="lm")
    if item["images"]:
        canvas.text((right, row), item["images"], 20, _TEXT_3, anchor="rm")
        canvas.picture(right - canvas.length(item["images"], 20) - 20, row, _TEXT_3)


def _header(canvas, digest, hours):
    """Brand band with a diagonal gradient and soft geometric accents."""
    width, height = WIDTH * _SCALE, 300 * _SCALE
    mask = Image.linear_gradient("L").rotate(90).resize((width, height))
    band = Image.composite(Image.new("RGB", (width, height), _BRAND),
                           Image.new("RGB", (width, height), _BRAND_DEEP), mask)
    glow = Image.new("L", (width, height), 0)
    accent = ImageDraw.Draw(glow)
    for cx, cy, r, alpha in ((960, -60, 280, 20), (1080, 240, 190, 14)):
        accent.ellipse(canvas._s((cx - r, cy - r, cx + r, cy + r)), fill=alpha)
    band.paste(Image.new("RGB", (width, height), "#FFFFFF"), mask=glow)
    canvas.image.paste(band)
    canvas.text((56, 52), f"CAMPUS DIGEST·每 {hours} 小时", 19, "#C9D8FF", bold=True)
    canvas.text((54, 92), "校园墙新帖速览", 52, "#FFFFFF", bold=True)
    community = canvas.wrap(canvas.fonts.clean(digest["community"], 256).replace("\n", " "), 24, 960, 1)[0]
    canvas.text((57, 168), community, 24, "#DCE6FF")


def _overview(canvas, digest, start, end, shown, intro, y):
    x0, x1 = 40, 1040
    height = 172 + (24 + 36 * len(intro) + 26 if intro else 0)
    canvas.box((x0, y, x1, y + height), 20, _SURFACE)
    if start.date() == end.date():
        date_label = start.strftime("%Y.%m.%d")
    else:
        date_label = f"{start:%m.%d} – {end:%m.%d}"
    columns = (x0 + 40, 480, 760)
    for column in columns[1:]:
        canvas.line(((column - 32, y + 36), (column - 32, y + 140)), _DIVIDER, 2)
    labels = ("统计时段（北京时间）", "已收集新帖", "本期展示")
    for column, label in zip(columns, labels):
        canvas.text((column, y + 34), label, 19, _TEXT_3)
    canvas.text((columns[0], y + 66), f"{start:%H:%M} – {end:%H:%M}", 38, _TEXT, bold=True)
    canvas.text((columns[0], y + 120), date_label, 19, _TEXT_3)
    total = str(digest["total"]) if digest["total"] < 1_000_000 else "999999+"
    for column, value in ((columns[1], total), (columns[2], str(shown))):
        canvas.text((column, y + 66), value, 38, _TEXT, bold=True)
        canvas.text((column + canvas.length(value, 38, True) + 6, y + 100), "条", 20, _TEXT_2, anchor="ls")
    canvas.text((columns[1], y + 120), "按发布时间去重", 19, _TEXT_3)
    if digest["mode"] == "ai":
        mode, colors = "AI 整理", _TAGS["blue"]
    else:
        mode, colors = "原文摘录", _TAGS["gold"]
    width = canvas.tag(columns[2], y + 114, mode, colors, 17)
    canvas.text((columns[2] + width + 10, y + 130), "以原帖为准", 19, _TEXT_3, anchor="lm")
    if intro:
        row = y + 172
        canvas.line(((x0 + 40, row), (x1 - 40, row)), _DIVIDER)
        row += 24
        canvas.box((x0 + 40, row + 4, x0 + 44, row + 36 * len(intro) - 6), 2, _BRAND)
        for line in intro:
            canvas.text((x0 + 60, row), line, 23, _TEXT_2)
            row += 36
    return height


def _page(digest, start, end, items, fonts, page_index, pages, shown, thumbnails):
    hours = f"{(end - start).total_seconds() / 3600:g}"
    probe = _Canvas(fonts, 1)  # Measure with the exact supersampled fonts.
    probe.image.close()
    intro_text = fonts.clean(digest["intro"], 512) or "把刚刚发生的校园小事，收进这一页。"
    intro = probe.wrap(intro_text, 23, 880, 2)
    layouts = [_item_layout(item, probe, start, page_index * 4 + i + 1, thumbnails[i])
               for i, item in enumerate(items)]
    notice = probe.wrap("查看全文：/校园圈 详情 <帖子ID>　查看配图：/校园圈 图片 <帖子ID>",
                        19, 900, 2)
    overview_top = 232
    overview = 172 + 24 + 36 * len(intro) + 26
    top = overview_top + overview + 28
    bottom = top + sum(item["height"] for item in layouts) + 20 * (len(layouts) - 1)
    alert = 68 if digest["scan_limited"] else 0
    note = 36 + 30 * len(notice)
    height = bottom + 28 + alert + note + 22 + 60
    if height > MAX_HEIGHT:
        raise RenderError("Card layout exceeds its maximum height")

    canvas = _Canvas(fonts, height)
    try:
        canvas.shadow((40, overview_top, 1040, overview_top + overview), 20, 46)
        y = top
        for item in layouts:
            canvas.shadow((40, y, 1040, y + item["height"]), 16, 26)
            y += item["height"] + 20
        canvas.elevate()
        _header(canvas, digest, hours)
        _overview(canvas, digest, start, end, shown, intro, overview_top)
        y = top
        for item in layouts:
            _draw_item(canvas, item, y)
            y += item["height"] + 20

        y = bottom + 28
        if digest["scan_limited"]:
            background, border, ink = _TAGS["gold"]
            canvas.box((40, y, 1040, y + 52), 10, background, border)
            canvas.badge(68, y + 26, "!", "#FAAD14")
            canvas.text((92, y + 26), "扫描未完成：仅展示已收集内容，数量可能不完整", 20, _TEXT, anchor="lm")
            y += alert
        canvas.box((40, y, 1040, y + note), 10, "#E9EBEE")
        canvas.badge(68, y + 18 + 15, "i", _TEXT_3)
        for i, line in enumerate(notice):
            canvas.text((92, y + 18 + 15 + 30 * i), line, 19, _TEXT_2, anchor="lm")
        y += note + 22 + 30
        canvas.text((44, y), f"每 {hours} 小时一辑·云上校友圈", 18, _TEXT_3, anchor="lm")
        label = f"{page_index + 1} / {pages}"
        canvas.text((1036, y), label, 20, _TEXT_2, bold=True, anchor="rm")
        dots = 1036 - canvas.length(label, 20, True) - 18
        for i in reversed(range(pages)):
            active = i == page_index
            width = 22 if active else 8
            canvas.box((dots - width, y - 4, dots, y + 4), 4, _BRAND if active else "#C9CDD4")
            dots -= width + 8
        return canvas.finish()
    except Exception:
        canvas.image.close()
        raise


def render_digest(digest: dict, output_dir, font_path="") -> list[str]:
    """Render at most eight posts, four per page, at 1080px and <=2400px tall.

    ``window_start``/``window_end`` must be aware ISO strings. Display uses
    Beijing time. ``shown`` selects the first N items and is capped at eight;
    the displayed count always matches the actual rendered selection. An empty
    selection returns [] without requiring Pillow, a font, or an output folder.
    Each call uses unique filenames and removes its own files if any page fails.

    An item's optional ``image_path`` is a trusted local cache JPEG (str/Path),
    already orientation-normalized with edges <=2048px. One contained thumbnail
    is shown; ``image_count`` remains the total number of original attachments.
    Missing, unreadable or corrupt images use the unchanged full-width text
    layout. This renderer never downloads, changes or deletes the cached image.
    """
    created = []
    pending = None
    try:
        start, end, selected = _validate(digest)
        if not selected:
            return []
        fonts = _Fonts(font_path)
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        batch = f"campus-{end:%Y%m%d-%H%M}-{uuid4().hex}"
        pages = (len(selected) + 3) // 4
        for index in range(pages):
            output = directory / f"{batch}-{index + 1:02}.png"
            pending = output.with_suffix(".tmp")
            items = selected[index * 4:(index + 1) * 4]
            with ExitStack() as resources:
                thumbnails = []
                for item in items:
                    thumbnail = _load_thumbnail(item.get("image_path"))
                    thumbnails.append(resources.enter_context(thumbnail)
                                      if thumbnail is not None else None)
                image = _page(digest, start, end, items, fonts, index, pages,
                              len(selected), thumbnails)
            try:
                image.save(pending, format="PNG", optimize=True)
            finally:
                image.close()
            pending.replace(output)
            created.append(str(output))
            pending = None
        return created
    except Exception as exc:
        for owned in ([pending] if pending is not None else []) + [Path(p) for p in created]:
            try:
                owned.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, RenderError):
            raise
        raise RenderError("Digest rendering failed; use the text fallback") from exc
