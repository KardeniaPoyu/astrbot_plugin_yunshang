"""Local, bounded campus digest cards. Requires Pillow and a real CJK font.

Call ``render_digest`` in a worker thread. It returns absolute PNG paths in
page order; the caller owns their lifecycle. Catch ``RenderError`` to use the
text fallback. There is no network access or persistent state in this module.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import base64
import binascii
from io import BytesIO
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
MAX_HEIGHT = 16000
_CST = timezone(timedelta(hours=8))
_SCALE = 1  # Native long canvas: FreeType still anti-aliases text, without 4x RAM.
_BODY_LIMIT = 720
_BODY_LINES = 21
_THUMB_PAD = 10
_GALLERY_HEIGHT = 360
_GALLERY_GAP = 16
_DETAIL_CHAR_LIMIT = 20_000
_DETAIL_PAGE_LIMIT = 3
# Share-poster proportion: the code is ~20% of the card width, beside its call
# to action, not a full-width block. 216px stays scannable from a phone screen.
_DETAIL_QR_EDGE = 216
_DETAIL_QR_PAD = 12
_DETAIL_QR_TILE = _DETAIL_QR_EDGE + 2 * _DETAIL_QR_PAD
_DETAIL_QR_FRAME = 8
_DETAIL_QR_PANEL = _DETAIL_QR_TILE + 2 * (_DETAIL_QR_FRAME + 20)
_DETAIL_CARD_TOP = 176
_DETAIL_BODY_TOP = _DETAIL_CARD_TOP + 36 + 32 + 28
_DETAIL_LINE = 42
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
_SEP = " · "


def _caps(text):
    return text.isascii() and text.isupper() and len(text) > 1



_SEP_WIDTH = 22  # 9px gap, 4px dot, 9px gap.
_DOT = "#C9CDD4"
_TRACKING = 1.5
_AVATAR_COLORS = ("blue", "cyan", "green", "purple", "magenta", "orange", "geekblue", "volcano")
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
_ITEM_KEYS = {"id", "title", "published_at", "category", "image_count"}


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
    entire input before truncating it. Probe at most 512 characters at a time,
    rather than repeatedly shaping the whole remaining paragraph. An offset
    also avoids copying that remaining paragraph on every line.
    """
    if max_lines < 1 or width < font.getlength("…"):
        raise RenderError("Text region is too small")
    lines = []
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for paragraph_index, paragraph in enumerate(paragraphs):
        paragraph = paragraph.strip()
        offset, length = 0, len(paragraph)
        if not paragraph:
            lines.append("")
        while offset < length:
            limit = min(length - offset, 512)
            low, high = 0, min(32, limit)
            while font.getlength(paragraph[offset:offset + high]) <= width:
                low = high
                if high == limit:
                    break
                high = min(high * 2, limit)
            while low < high:
                middle = (low + high + 1) // 2
                if font.getlength(paragraph[offset:offset + middle]) <= width:
                    low = middle
                else:
                    high = middle - 1
            take = low
            if take == 0:
                raise RenderError("A glyph exceeds the text region")
            if offset + take < length:
                # Avoid breaking a short English word if there is a usable space.
                if paragraph[offset + take - 1].isascii() and paragraph[offset + take].isascii():
                    space = paragraph.rfind(" ", offset, offset + take) - offset
                    if space >= take // 2:
                        take = space + 1
                # Moving the preceding character with closing punctuation keeps
                # the line width fixed and does not hang punctuation off-canvas.
                candidate = take
                while candidate > 0 and (
                    paragraph[offset + candidate - 1] in _OPENING
                    or paragraph[offset + candidate] in _CLOSING
                ):
                    candidate -= 1
                if candidate:
                    take = candidate
            lines.append(paragraph[offset:offset + take].rstrip())
            offset += take
            while offset < length and paragraph[offset].isspace():
                offset += 1
            if len(lines) == max_lines:
                more = offset < length or any(p.strip() for p in paragraphs[paragraph_index + 1:])
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


def _validate(digest, planned_shown=None):
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
    shown = digest["shown"] if planned_shown is None else planned_shown
    if (not isinstance(items, list) or type(shown) is not int
            or not 0 <= shown <= min(len(items), digest["shown"], digest["total"])):
        raise RenderError("Digest counts do not match available items")
    selected = items[:shown]
    for item in selected:
        if not isinstance(item, dict) or not _ITEM_KEYS.issubset(item):
            raise RenderError("Missing required item fields")
        if not str(item["id"] or "").strip():
            raise RenderError("Every item requires a source post ID")
        if type(item["image_count"]) is not int or item["image_count"] < 0:
            raise RenderError("image_count must be a non-negative integer")
        if "gallery_count" in item and (type(item["gallery_count"]) is not int
                                        or not 0 <= item["gallery_count"] <= 3):
            raise RenderError("gallery_count must be an integer between zero and three")
        for key in ("like_count", "trace_count", "comment_count"):
            count = item.get(key)
            if count is not None and (type(count) is not int or count < 0):
                raise RenderError(f"{key} must be a non-negative integer or None")
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


def _load_thumbnail(image_path, size):
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
                bounds = tuple((edge - 2 * _THUMB_PAD) * _SCALE for edge in size)
                fit = min(bounds[0] / thumbnail.width, bounds[1] / thumbnail.height)
                # Fit in both directions at canvas resolution, including upscale:
                # thumbnail() alone leaves small cache images tiny after downsampling.
                size = tuple(max(1, round(edge * fit)) for edge in thumbnail.size)
                return thumbnail.resize(size, Image.LANCZOS)
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None


def _gallery_count(item):
    return item.get("gallery_count", min(3, item["image_count"]))


def _gallery_paths(item):
    paths = item.get("image_paths")
    if not isinstance(paths, list) or not paths:
        paths = [item.get("image_path")]
    return paths[:_gallery_count(item)]


def _gallery_size(count):
    return ((920 - _GALLERY_GAP * (count - 1)) // count, _GALLERY_HEIGHT)


def _metric_count(count):
    """Compact counts without rounding up or confusing unknown with zero."""
    if count is None:
        return "—"
    if count < 10_000:
        return str(count)
    if count >= 99_900_000_000:
        return "999亿+"
    unit, suffix = (100_000_000, "亿") if count >= 100_000_000 else (10_000, "万")
    whole, tenth = divmod(count * 10 // unit, 10)
    return f"{whole}.{tenth}{suffix}" if tenth else f"{whole}{suffix}"


class _Metrics:
    """Text measurement with the exact canvas fonts, without allocating an image."""

    def __init__(self, fonts):
        self.fonts = fonts

    def font(self, size, bold=False):
        return self.fonts.get(size * _SCALE, bold)

    def wrap(self, text, size, width, lines, bold=False):
        return _wrap(text, self.font(size, bold), width * _SCALE, lines)

    def length(self, text, size, bold=False):
        return self.font(size, bold).getlength(text) / _SCALE

    def tracked(self, text, size, bold=False):
        """Latin caps get letter spacing; CJK keeps the font's own spacing."""
        if not _caps(text):
            return self.length(text, size, bold)
        return self.length(text, size, bold) + _TRACKING * max(0, len(text) - 1)

    def meta_length(self, text, size, bold=False):
        parts = text.split(_SEP)
        return sum(self.tracked(part, size, bold) for part in parts) + _SEP_WIDTH * (len(parts) - 1)


class _Canvas(_Metrics):
    """Native-resolution long canvas with anti-aliased text and local shadows."""

    def __init__(self, fonts, height):
        super().__init__(fonts)
        self.image = Image.new("RGB", (WIDTH * _SCALE, height * _SCALE), _BG)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _s(values):
        return tuple(round(v * _SCALE) for v in values)

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
        """Paint a local shadow without allocating a full long-page mask."""
        x0, y0, x1, y1 = box
        pad = 28
        with Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0) as mask:
            ImageDraw.Draw(mask).rounded_rectangle(
                (pad + 2, pad + 6, pad + x1 - x0 - 2, pad + y1 - y0 + 6),
                radius=radius, fill=strength)
            with mask.filter(ImageFilter.GaussianBlur(14)) as blurred:
                self.image.paste(_SHADOW, (x0 - pad, y0 - pad, x1 + pad, y1 + pad), blurred)

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

    def thumbnail(self, x, y, image, size):
        width, height = size
        self.box((x, y, x + width, y + height), 12, "#F7F8FA", _BORDER)
        # Contain within an inset frame: even screenshot corners stay visible.
        fit = min((width - 2 * _THUMB_PAD) / image.width,
                  (height - 2 * _THUMB_PAD) / image.height)
        fitted_size = tuple(max(1, round(edge * fit)) for edge in image.size)
        with image.resize(fitted_size, Image.LANCZOS) as fitted:
            left = x + (width - fitted.width) // 2
            top = y + (height - fitted.height) // 2
            self.image.paste(fitted, (left, top))

    def badge(self, cx, cy, glyph, fill, ink="#FFFFFF"):
        """Filled round status icon (info / warning) with a centered glyph."""
        self.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=fill)
        self.text((cx, cy + 1), glyph, 16, ink, bold=True, anchor="mm")

    def avatar(self, cx, cy, radius, name):
        """Initial-letter avatar; a 4x mask keeps the circle edge smooth at 1x."""
        initial = name[:1] or "同"
        _, _, color = _TAGS[_AVATAR_COLORS[sum(map(ord, name)) % len(_AVATAR_COLORS)]]
        self.disc(cx, cy, 2 * radius, color)
        self.text((cx, cy), initial, round(radius * 0.95), "#FFFFFF", bold=True, anchor="mm")

    def disc(self, cx, cy, diameter, color):
        """Anti-aliased small circle: 4x mask, downsampled, at native scale."""
        size = max(2, round(diameter))
        x, y = round(cx - size / 2), round(cy - size / 2)
        with Image.new("L", (size * 4, size * 4), 0) as big:
            ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
            with big.resize((size, size), Image.LANCZOS) as mask:
                self.image.paste(color, (x, y, x + size, y + size), mask)

    def meta(self, xy, text, size, fill=_TEXT_3, bold=False, anchor="lm", dot=_DOT):
        """Metadata row: " · " separators become small drawn dots with even gaps.

        The SC face draws every middle dot full-width, which spaces rows unevenly.
        ``anchor`` is "lm", "mm" or "rm"; segments are drawn left to right.
        """
        x, y = xy
        width = self.meta_length(text, size, bold)
        x -= {"l": 0, "m": width / 2, "r": width}[anchor[0]]
        for index, part in enumerate(text.split(_SEP)):
            if index:
                self.disc(x + _SEP_WIDTH / 2, y + 1, 4, dot)
                x += _SEP_WIDTH
            if _caps(part):
                for char in part:
                    self.text((x, y), char, size, fill, bold, "lm")
                    x += self.length(char, size, bold) + _TRACKING
                x -= _TRACKING
            else:
                self.text((x, y), part, size, fill, bold, "lm")
                x += self.length(part, size, bold)
        return width

    def chip(self, x, cy, label, background=_BRAND_SOFT, ink=_BRAND, size=16):
        width = self.length(label, size, True) + 14
        self.box((x, cy - 12, x + width, cy + 12), 6, background)
        self.text((x + width / 2, cy), label, size, ink, bold=True, anchor="mm")
        return width

    def finish(self, height):
        if height == self.image.height:
            return self.image
        result = self.image.crop((0, 0, WIDTH, height))
        self.image.close()
        return result


def _comment_layout(canvas, comments, width, text_lines, reply_lines):
    """Measured comment threads: avatar, name, likes, text, indented replies.

    Sizes avoid 26px, which identifies original post body lines.
    """
    clean = canvas.fonts.clean
    blocks = []
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, dict):
            continue
        likes = comment.get("like_count")
        likes = f"赞 {_metric_count(likes)}" if type(likes) is int and likes > 0 else ""
        name_width = width - 48 - 64 - (canvas.length(likes, 19) + 16 if likes else 0)
        name = canvas.wrap(clean(comment.get("name"), 64).replace("\n", " ") or "匿名同学",
                           20, max(120, name_width), 1, bold=True)[0]
        text = canvas.wrap(clean(comment.get("text"), 600) or "（无文字）", 22, width - 48, text_lines)
        replies = []
        for reply in (comment.get("replies") or [])[:3]:
            if not isinstance(reply, dict):
                continue
            who = canvas.wrap(clean(reply.get("name"), 64).replace("\n", " ") or "匿名同学",
                              19, 300, 1, bold=True)[0]
            target = clean(reply.get("reply_to"), 64).replace("\n", " ")
            target = canvas.wrap("回复 " + target, 19, 300, 1)[0] if target else ""
            lines = canvas.wrap(clean(reply.get("text"), 600) or "（无文字）", 21, width - 68, reply_lines)
            replies.append(dict(name=who, author=bool(reply.get("is_author")), target=target,
                                lines=lines, height=28 + 32 * len(lines)))
        height = 36 + 34 * len(text)
        if replies:
            height += 12 + sum(r["height"] for r in replies) + 10 * (len(replies) - 1) + 4
        blocks.append(dict(name=name, author=bool(comment.get("is_author")), likes=likes,
                           text=text, replies=replies, height=height))
    return blocks


def _draw_comment(canvas, block, x, y, width):
    canvas.avatar(x + 18, y + 18, 18, block["name"])
    canvas.text((x + 48, y + 14), block["name"], 20, _TEXT, bold=True, anchor="lm")
    if block["author"]:
        canvas.chip(x + 56 + canvas.length(block["name"], 20, True), y + 14, "楼主")
    if block["likes"]:
        canvas.text((x + width, y + 14), block["likes"], 19, _TEXT_3, anchor="rm")
    row = y + 36
    for line in block["text"]:
        canvas.text((x + 48, row), line, 22, _TEXT_2)
        row += 34
    if block["replies"]:
        row += 12
        top = row
        for reply in block["replies"]:
            cursor = x + 68
            canvas.text((cursor, row + 12), reply["name"], 19, _TEXT, bold=True, anchor="lm")
            cursor += canvas.length(reply["name"], 19, True) + 8
            if reply["author"]:
                cursor += canvas.chip(cursor, row + 12, "楼主") + 8
            if reply["target"]:
                canvas.text((cursor, row + 12), reply["target"], 19, _TEXT_3, anchor="lm")
            line_y = row + 28
            for line in reply["lines"]:
                canvas.text((x + 68, line_y), line, 21, _TEXT_2)
                line_y += 32
            row += reply["height"] + 10
        canvas.box((x + 48, top, x + 51, row - 10), 1, _BORDER)


def _comments_title(comments):
    liked = any(type(c.get("like_count")) is int and c["like_count"] > 0
                for c in comments if isinstance(c, dict))
    return "高赞热评" if liked else "评论精选"


def _item_layout(item, canvas, start, number, gallery_count):
    clean = canvas.fonts.clean
    raw_body = item.get("body")
    if raw_body is None:
        raw_body = item.get("summary", "")
    original_text = clean(raw_body, 2000)
    body_text = clean(raw_body, _BODY_LIMIT) or "原帖未提供文字。"
    title_text = clean(item["title"], 320)
    title_key = re.sub(r"\s+", "", title_text).rstrip("。！？.!?…")
    body_key = re.sub(r"\s+", "", original_text).rstrip("。！？.!?")
    duplicate_title = bool(title_key) and body_key.startswith(title_key)
    title = canvas.wrap(title_text, 32, 920, 2, bold=True) if title_text and not duplicate_title else []
    # Typical text gets up to 720 characters; the generous line bound also
    # handles pathological line breaks while keeping all ten posts in one PNG.
    body = canvas.wrap(body_text, 26, 920, _BODY_LINES)
    excerpt = bool(item.get("truncated") or item.get("source_truncated")
                   or len(str(raw_body)) > _BODY_LIMIT
                   or re.sub(r"\s+", "", "".join(body)) != re.sub(r"\s+", "", body_text))
    category = canvas.wrap(clean(item["category"], 64).replace("\n", " ") or "校园日常", 19, 560, 1)[0]
    images = ""
    if item["image_count"]:
        count = str(item["image_count"]) if item["image_count"] < 1000 else "999+"
        images = f"配图 {count} 张"
    metrics = f"点赞 {_metric_count(item.get('like_count'))} · 蹲蹲 {_metric_count(item.get('trace_count'))}"
    # Reserve the image counter, its icon and a 24px gap before fitting metrics.
    metrics_inset = canvas.length(images, 20) + 30 + 24 if images else 0
    source_width = min(520, 920 - metrics_inset - canvas.meta_length(metrics, 19) - 24)
    source = canvas.wrap("原帖 #" + clean(item["id"], 128).replace("\n", " "),
                         20, source_width, 1)[0]
    if item.get("comment_count") is not None:
        with_comments = metrics + f" · 评论 {_metric_count(item['comment_count'])}"
        if canvas.meta_length(with_comments, 19) <= 920 - metrics_inset - canvas.length(source, 20) - 24:
            metrics = with_comments
    when = canvas.wrap(_published(item["published_at"], start), 20, 300, 1)[0]
    title_height = 44 * len(title) + 10 if title else 0
    body_height = title_height + 39 * len(body) + (32 if excerpt else 0)
    if gallery_count:
        body_height += 22 + _GALLERY_HEIGHT
    comments = _comment_layout(canvas, item.get("comments"), 880, 2, 1)
    comments_top = body_height
    if comments:
        # Panel: 18 padding + 24 label + 14, threads 18 apart, 18 padding.
        body_height += 22 + 18 + 24 + 14 + sum(c["height"] for c in comments) + 18 * (len(comments) - 1) + 18
    height = 28 + 32 + 18 + body_height + 18 + 16 + 24 + 24
    return dict(title=title, body=body, excerpt=excerpt, category=category, source=source,
                images=images, when=when, height=height, number=number,
                metrics=metrics, metrics_inset=metrics_inset,
                colors=_tag_color(category), gallery_count=gallery_count, body_height=body_height,
                comments=comments, comments_top=comments_top,
                comments_title=_comments_title(item.get("comments") or []))


def _draw_item(canvas, item, y, gallery):
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
    for line in item["title"]:
        canvas.text((left, row), line, 32, _TEXT, bold=True)
        row += 44
    row += 10 if item["title"] else 0
    for line in item["body"]:
        canvas.text((left, row), line, 26, _TEXT_2)
        row += 39
    if item["excerpt"]:
        canvas.text((left, row + 4), "原文节选", 19, _TEXT_3)
        row += 32
    if gallery:
        row += 22
        # Failed slots disappear; surviving photos keep their contained fit.
        size = _gallery_size(len(gallery))
        for index, image in enumerate(gallery):
            canvas.thumbnail(left + index * (size[0] + _GALLERY_GAP), row, image, size)
    if item["comments"]:
        top = body_top + item["comments_top"] + 22
        canvas.box((left, top, right, body_top + item["body_height"]), 12, "#F7F8FA")
        canvas.text((left + 20, top + 18 + 12), item["comments_title"], 18, _TEXT_3, bold=True, anchor="lm")
        row = top + 18 + 24 + 14
        for block in item["comments"]:
            _draw_comment(canvas, block, left + 20, row, 880)
            row += block["height"] + 18
    row = body_top + item["body_height"] + 18
    canvas.line(((left, row), (right, row)), _DIVIDER)
    row += 16 + 12
    canvas.text((left, row), item["source"], 20, _TEXT_3, anchor="lm")
    canvas.meta((right - item["metrics_inset"], row), item["metrics"], 19, _TEXT_2, anchor="rm")
    if item["images"]:
        canvas.text((right, row), item["images"], 20, _TEXT_3, anchor="rm")
        canvas.picture(right - canvas.length(item["images"], 20) - 20, row, _TEXT_3)


def _band(canvas, height):
    """Brand band with a horizontal gradient and soft geometric accents."""
    width, height = WIDTH * _SCALE, height * _SCALE
    mask = Image.linear_gradient("L").rotate(90).resize((width, height))
    band = Image.composite(Image.new("RGB", (width, height), _BRAND),
                           Image.new("RGB", (width, height), _BRAND_DEEP), mask)
    glow = Image.new("L", (width, height), 0)
    accent = ImageDraw.Draw(glow)
    for cx, cy, r, alpha in ((960, -60, 280, 20), (1080, 240, 190, 14)):
        accent.ellipse(canvas._s((cx - r, cy - r, cx + r, cy + r)), fill=alpha)
    band.paste(Image.new("RGB", (width, height), "#FFFFFF"), mask=glow)
    canvas.image.paste(band)


def _header(canvas, digest, hours):
    _band(canvas, 300)
    canvas.meta((56, 64), f"CAMPUS DIGEST · 每 {hours} 小时更新", 19, "#C9D8FF", bold=True, dot="#7F9CF5")
    heading = "校园墙高赞精选" if digest.get("ranking") == "likes" else "校园墙新帖速览"
    canvas.text((54, 92), heading, 52, "#FFFFFF", bold=True)
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
    ranked = digest.get("ranking") == "likes"
    labels = ("统计时段", "收集新帖", "本期精选" if ranked else "本期展示")
    for column, label in zip(columns, labels):
        canvas.text((column, y + 34), label, 19, _TEXT_3)
    canvas.text((columns[0], y + 66), f"{start:%H:%M} – {end:%H:%M}", 38, _TEXT, bold=True)
    canvas.meta((columns[0], y + 130), date_label + " · 北京时间", 19, _TEXT_3)
    total = str(digest["total"]) if digest["total"] < 1_000_000 else "999999+"
    for column, value in ((columns[1], total), (columns[2], str(shown))):
        canvas.text((column, y + 66), value, 38, _TEXT, bold=True)
        canvas.text((column + canvas.length(value, 38, True) + 6, y + 100), "条", 20, _TEXT_2, anchor="ls")
    order = "按发布时间排序"
    if ranked:
        order = "按点赞排序（已收集部分）" if digest["scan_limited"] else "按点赞排序"
    canvas.text((columns[1], y + 120), order, 19, _TEXT_3)
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


def plan_digest(digest: dict, font_path="") -> dict:
    """Pure selection plan: one image, first ten eligible items, supplied order.

    No fonts or photos are opened and no output is written. ``font_path`` is
    accepted for call-site compatibility; selection no longer depends on it.
    Indices remain valid after pruning ``items[:plan['shown']]``. The caller
    may carry earlier omissions in ``digest.omitted``; rendering honors them.
    """
    _, _, eligible = _validate(digest)
    shown = min(10, len(eligible))
    return {"pages": [list(range(shown))] if shown else [],
            "shown": shown, "omitted": len(eligible) - shown}


def _check_plan(plan):
    if not isinstance(plan, dict) or not {"pages", "shown", "omitted"}.issubset(plan):
        raise RenderError("Invalid digest plan")
    shown, omitted = plan["shown"], plan["omitted"]
    if type(shown) is not int or not 0 <= shown <= 10 or type(omitted) is not int or omitted < 0:
        raise RenderError("Invalid planned counts")
    expected = [list(range(shown))] if shown else []
    pages = plan["pages"]
    if (pages != expected or not isinstance(pages, list)
            or any(type(i) is not int for group in pages for i in group)):
        raise RenderError("Plan must contain one contiguous prefix of at most ten posts")


def _page(digest, start, end, items, fonts, shown, omitted):
    hours = f"{(end - start).total_seconds() / 3600:g}"
    probe = _Canvas(fonts, 1)  # Same native font metrics as the final long image.
    probe.image.close()
    intro_text = fonts.clean(digest["intro"], 512) or "把刚刚发生的校园小事，收进这一页。"
    intro = probe.wrap(intro_text, 23, 880, 2)
    layouts = [_item_layout(item, probe, start, i + 1, _gallery_count(item))
               for i, item in enumerate(items)]
    notice = probe.wrap("发送「/校园墙 详情 帖子 ID」看全文，「/校园墙 图片 帖子 ID」看配图",
                        19, 900, 2)
    if omitted:
        notice.append(f"本期精选 {shown} 条，另有 {_metric_count(omitted)} 条未入选")
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
        _header(canvas, digest, hours)
        canvas.shadow((40, overview_top, 1040, overview_top + overview), 20, 46)
        _overview(canvas, digest, start, end, shown, intro, overview_top)
        y = top
        for index, (source, item) in enumerate(zip(items, layouts)):
            # At most this post's three resized photos are alive. Source JPEGs
            # close inside the loader, and every gallery closes before the next.
            with ExitStack() as resources:
                gallery = []
                for path in _gallery_paths(source):
                    photo = _load_thumbnail(path, (920, _GALLERY_HEIGHT))
                    if photo is not None:
                        resources.callback(photo.close)
                        gallery.append(photo)
                if len(gallery) != item["gallery_count"]:
                    item = _item_layout(source, probe, start, index + 1, len(gallery))
                canvas.shadow((40, y, 1040, y + item["height"]), 16, 26)
                _draw_item(canvas, item, y, gallery)
                y += item["height"] + 20

        bottom = y - 20
        actual_height = bottom + 28 + alert + note + 22 + 60
        y = bottom + 28
        if digest["scan_limited"]:
            background, border, ink = _TAGS["gold"]
            canvas.box((40, y, 1040, y + 52), 10, background, border)
            canvas.badge(68, y + 26, "!", "#FAAD14")
            canvas.text((92, y + 26), "仅展示已收集的新帖，可能还有遗漏", 20, _TEXT, anchor="lm")
            y += alert
        canvas.box((40, y, 1040, y + note), 10, "#E9EBEE")
        canvas.badge(68, y + 18 + 15, "i", _TEXT_3)
        for i, line in enumerate(notice):
            canvas.text((92, y + 18 + 15 + 30 * i), line, 19, _TEXT_2, anchor="lm")
        y += note + 22 + 30
        canvas.meta((44, y), f"云上校友圈 · 每 {hours} 小时一辑", 18, _TEXT_3)
        label = "1 / 1"
        canvas.text((1036, y), label, 20, _TEXT_2, bold=True, anchor="rm")
        dots = 1036 - canvas.length(label, 20, True) - 18
        canvas.box((dots - 22, y - 4, dots, y + 4), 4, _BRAND)
        return canvas.finish(actual_height)
    except Exception:
        canvas.image.close()
        raise


def render_digest(digest: dict, output_dir, font_path="", plan=None) -> list[str]:
    """Render up to ten ranked posts into ONE 1080px PNG, at most 16000px tall.

    ``window_start``/``window_end`` must be aware ISO strings. Display uses
    Beijing time. ``shown`` selects a prefix, capped at ten without reordering.
    An empty selection returns [] without requiring Pillow/fonts/output folders.
    Optional ``plan`` is the single-group dict from ``plan_digest`` and remains
    valid after the caller prunes items; omissions still appear on the image.
    Failed writes remove only this call's temporary output.

    ``body`` is the sanitized original text; only legacy items use ``summary``.
    Body excerpts preserve paragraphs, show up to 720 characters / 21 wrapped
    lines at 26px, and mark clipping or source truncation with 原文节选. A duplicate
    title is omitted, never the body. Titles and body both occupy the full width.

    ``image_paths`` supplies up to three trusted, normalized cache JPEGs (edges
    <=2048px); legacy ``image_path`` is the fallback. ``gallery_count`` (0..3)
    is authoritative, falling back to min(3, image_count). Photos are contained
    below the text. Failed galleries collapse without changing post selection.
    Decode/close is sequential per post, onto one native-resolution canvas.
    No network access or cache modifications occur.

    Optional ``like_count``/``trace_count``/``comment_count`` are non-negative
    integers or None (unknown). Likes and follows always show; comments appear
    only if they fit without crowding the source or image counter. Unknown likes
    and follows use —, zero stays 0, large counts use 万/亿 (capped at 999亿+).
    ``ranking='likes'`` selects ranked labels; item selection/order is the caller's
    responsibility.
    """
    created = []
    pending = None
    try:
        if plan is None:
            plan = plan_digest(digest, font_path)
        _check_plan(plan)
        start, end, selected = _validate(digest, plan["shown"])
        if not selected:
            return []
        omitted = digest.get("omitted", 0)
        if type(omitted) is not int or omitted < 0:
            raise RenderError("omitted must be a non-negative integer")
        omitted = max(omitted, plan["omitted"], digest["total"] - len(selected))
        fonts = _Fonts(font_path)
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        batch = f"campus-{end:%Y%m%d-%H%M}-{uuid4().hex}"
        output = directory / f"{batch}-01.png"
        pending = output.with_suffix(".tmp")
        image = _page(digest, start, end, selected, fonts, len(selected), omitted)
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


def _detail_record(post):
    """Validate detail inputs without importing digest selection or AI fields."""
    if (not isinstance(post, dict) or not str(post.get("id") or "").strip()
            or not isinstance(post.get("text"), str)):
        raise RenderError("Post detail requires a source ID and original text")
    if len(post["text"]) > _DETAIL_CHAR_LIMIT:
        raise RenderError("Post detail exceeds 20000 characters; use the full-text attachment")
    record = dict(post)
    paths = post.get("image_paths")
    fallback_count = len(paths) if isinstance(paths, list) else int(bool(post.get("image_path")))
    record.setdefault("image_count", fallback_count)
    if type(record["image_count"]) is not int or record["image_count"] < 0:
        raise RenderError("image_count must be a non-negative integer")
    if "gallery_count" in record and (type(record["gallery_count"]) is not int
                                      or not 0 <= record["gallery_count"] <= 3):
        raise RenderError("gallery_count must be an integer between zero and three")
    for key in ("like_count", "trace_count", "comment_count"):
        count = record.get(key)
        if count is not None and (type(count) is not int or count < 0):
            raise RenderError(f"{key} must be a non-negative integer or None")
    return record


def _load_detail_qr(value):
    """Decode only a bounded official JPEG data URL, keeping its quiet edges."""
    prefix = "data:image/jpeg;base64,"
    max_bytes = 512 * 1024
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    if len(value) - len(prefix) > 4 * ((max_bytes + 2) // 3):
        return None
    try:
        decoded = base64.b64decode(value[len(prefix):], validate=True)
        if not decoded or len(decoded) > max_bytes:
            return None
        with ExitStack() as resources:
            source = resources.enter_context(Image.open(BytesIO(decoded), formats=("JPEG",)))
            if source.width * source.height > 1_000_000:
                return None
            rgb = source.convert("RGB")  # Fully decode so truncated files cannot reach the card.
            resources.callback(rgb.close)
            fit = min(_DETAIL_QR_EDGE / rgb.width, _DETAIL_QR_EDGE / rgb.height, 1)
            if fit < 1:
                # Lanczos keeps the round sun-code dots smooth at a fractional
                # scale; nearest-neighbour turns them into uneven blocks.
                fitted = rgb.resize(tuple(max(1, round(edge * fit)) for edge in rgb.size),
                                    Image.Resampling.LANCZOS)
                resources.callback(fitted.close)
            else:
                fitted = rgb
            tile = Image.new("RGB", (_DETAIL_QR_TILE, _DETAIL_QR_TILE), "white")
            tile.paste(fitted, ((_DETAIL_QR_TILE - fitted.width) // 2,
                                (_DETAIL_QR_TILE - fitted.height) // 2))
            return tile
    except (binascii.Error, ValueError, OSError, Image.DecompressionBombError):
        return None


def _detail_comments_height(blocks):
    if not blocks:
        return 0
    return 14 + 28 + 30 + 22 + sum(b["height"] for b in blocks) + 41 * (len(blocks) - 1)


def _detail_chrome(metadata, gallery, qr, comments=False):
    """Heights around the body: (card bottom offset after body, page tail)."""
    after_body = (28 + _GALLERY_HEIGHT if gallery else 0) + 28 + 1 + 20 + 28 + 32
    if comments:
        after_body += _detail_comments_height(metadata["comments"])
    if qr:
        after_body += _DETAIL_QR_PANEL + 40
        tail = 0
    else:
        tail = 24 + 36 + 30 * len(metadata["notice"])
    return after_body, tail + 28 + 56


def _detail_page(metadata, lines, fonts, page_index, pages, gallery, qr=None):
    """Render one bounded native page; no canvas survives its save/close cycle."""
    last = page_index == pages - 1
    after_body, tail = _detail_chrome(metadata, gallery, qr is not None, last)
    bottom = _DETAIL_BODY_TOP + _DETAIL_LINE * len(lines) + after_body
    height = bottom + tail
    if height > MAX_HEIGHT:
        raise RenderError("Post detail page exceeds its maximum height")
    left, right = 80, 1000
    canvas = _Canvas(fonts, height)
    try:
        _band(canvas, 240)
        canvas.meta((56, 56), "POST DETAIL · 帖子全文", 19, "#C9D8FF", bold=True, dot="#7F9CF5")
        canvas.text((54, 80), metadata["community"], 44, "#FFFFFF", bold=True)
        canvas.shadow((40, _DETAIL_CARD_TOP, 1040, bottom), 20, 30)
        canvas.box((40, _DETAIL_CARD_TOP, 1040, bottom), 20, _SURFACE)

        row = _DETAIL_CARD_TOP + 36
        x = left
        if metadata["category"]:
            x += canvas.tag(x, row, metadata["category"], _tag_color(metadata["category"])) + 14
        canvas.text((x, row + 16), metadata["source"], 20, _TEXT_3, anchor="lm")
        canvas.text((right, row + 16), metadata["when"], 20, _TEXT_3, anchor="rm")
        canvas.clock(right - canvas.length(metadata["when"], 20) - 18, row + 16, _TEXT_3)

        row = _DETAIL_BODY_TOP
        for line in lines:
            canvas.text((left, row), line, 26, _TEXT)
            row += _DETAIL_LINE
        if gallery:
            row += 28
            size = _gallery_size(len(gallery))
            for index, photo in enumerate(gallery):
                canvas.thumbnail(left + index * (size[0] + _GALLERY_GAP), row, photo, size)
            row += _GALLERY_HEIGHT
        row += 28
        canvas.line(((left, row), (right, row)), _DIVIDER)
        row += 20 + 14
        canvas.meta((left, row), metadata["metrics"], 20, _TEXT_2)
        if metadata["images"]:
            canvas.text((right, row), metadata["images"], 20, _TEXT_3, anchor="rm")
            canvas.picture(right - canvas.length(metadata["images"], 20) - 20, row, _TEXT_3)

        if last and metadata["comments"]:
            row += 14 + 28
            canvas.text((left, row), metadata["comments_heading"], 22, _TEXT, bold=True)
            if metadata["comments_note"]:
                canvas.text((right, row + 15), metadata["comments_note"], 18, _TEXT_3, anchor="rm")
            row += 30 + 22
            for index, block in enumerate(metadata["comments"]):
                if index:
                    canvas.line(((left + 48, row - 21), (right, row - 21)), _DIVIDER)
                _draw_comment(canvas, block, left, row, 920)
                row += block["height"] + 41
            row -= 41
        if qr is not None:
            top = row + 14 + 32
            canvas.box((left, top, right, top + _DETAIL_QR_PANEL), 14, "#F5F7FA")
            frame = _DETAIL_QR_FRAME
            tile_x, tile_y = right - 20 - frame - _DETAIL_QR_TILE, top + 20 + frame
            canvas.box((tile_x - frame, tile_y - frame, tile_x + _DETAIL_QR_TILE + frame,
                        tile_y + _DETAIL_QR_TILE + frame), 12, "#FFFFFF", _BORDER)
            canvas.image.paste(qr, (tile_x, tile_y))
            block = 40 + 14 + 30 + 22 + 30 * len(metadata["commands"])
            text_y = top + (_DETAIL_QR_PANEL - block) // 2
            canvas.text((left + 32, text_y), "微信扫码打开原帖", 30, _TEXT, bold=True)
            canvas.text((left + 32, text_y + 54), "长按识别小程序码，查看评论与互动", 20, _TEXT_2)
            for index, (label, command) in enumerate(metadata["commands"]):
                canvas.text((left + 32, text_y + 106 + 30 * index), label, 19, _TEXT_3)
                canvas.text((left + 104, text_y + 106 + 30 * index), command, 19, _TEXT_2)
            row = bottom + 28
        else:
            row = bottom + 24
            note = 36 + 30 * len(metadata["notice"])
            canvas.box((40, row, 1040, row + note), 10, "#E9EBEE")
            canvas.badge(68, row + 33, "i", _TEXT_3)
            for index, line in enumerate(metadata["notice"]):
                canvas.text((92, row + 33 + index * 30), line, 19, _TEXT_2, anchor="lm")
            row += note + 28
        row += 28
        canvas.meta((44, row), "云上校友圈 · 帖子全文", 18, _TEXT_3)
        canvas.text((1036, row), f"{page_index + 1} / {pages}", 20, _TEXT_2, bold=True, anchor="rm")
        return canvas.finish(height)
    except Exception:
        canvas.image.close()
        raise


def render_post_detail(post: dict, community, output_dir, font_path="") -> list[str]:
    """Render the FULL supplied ``post['text']`` in ordered absolute PNG paths.

    Normal posts use one image; longer bodies split at wrapped-line boundaries
    into at most three 1080px-wide pages of at most 16000px. Inputs over 20000
    characters or three pages raise RenderError before any page canvas/file is
    created, so the caller can attach the full text. Accepted text is never
    clipped or summarized; every paragraph/blank line appears once.
    ``id`` and string ``text`` are required; time and engagement counts are
    optional. Local ``image_paths`` / legacy ``image_path`` use the same trusted
    JPEG and optional ``gallery_count`` contract as the digest (at most three).
    Photos appear once, after the final body line, and unreadable files fall back
    gracefully. Optional ``mini_app_qr_data_url`` is the official JPEG data URL
    (<=512 KiB decoded, <=1M pixels); it is scaled to 216px with a white quiet
    zone, beside the call to action after photos on the last page only. Invalid data omits that section. No source URL
    or QR is synthesized. Canvases are saved/closed one at a time, without
    network access. On failure only this call's files are removed. Run in the
    caller's worker thread; catch RenderError for its full-text fallback.
    """
    created, pending, qr = [], None, None
    try:
        record = _detail_record(post)
        fonts = _Fonts(font_path)

        def wrap(text, size, width, limit, bold=False):
            return _wrap(text, fonts.get(size, bold), width, limit)

        source_id = fonts.clean(record["id"], 128).replace("\n", " ")
        try:
            timestamp = datetime.fromisoformat(str(record.get("published_at")))
            timestamp = timestamp.replace(tzinfo=_CST) if timestamp.utcoffset() is None else timestamp.astimezone(_CST)
            when = timestamp.strftime("%Y.%m.%d %H:%M")
        except (ValueError, OverflowError):
            when = "时间未提供"
        metrics = f"点赞 {_metric_count(record.get('like_count'))} · 蹲蹲 {_metric_count(record.get('trace_count'))}"
        if record.get("comment_count") is not None:
            metrics += f" · 评论 {_metric_count(record['comment_count'])}"
        count = record["image_count"]
        images = f"配图 {count if count < 1000 else '999+'} 张" if count else ""
        category = wrap(fonts.clean(record.get("category"), 64).replace("\n", " "), 19, 300, 1)[0]
        metadata = dict(
            source=wrap("原帖 #" + source_id, 20, 380, 1)[0],
            community=wrap(fonts.clean(community, 256).replace("\n", " ") or "校园墙", 44, 960, 1, True)[0],
            category=category, when=when, metrics=metrics, images=images,
            notice=wrap(f"发送「/校园墙 详情 {source_id}」看全文，「/校园墙 图片 {source_id}」看配图", 19, 900, 2),
            commands=[("看全文", wrap(f"/校园墙 详情 {source_id}", 19, 480, 1)[0]),
                      ("看配图", wrap(f"/校园墙 图片 {source_id}", 19, 480, 1)[0])])
        probe = _Metrics(fonts)
        comments = record.get("comments") if isinstance(record.get("comments"), list) else []
        metadata["comments"] = _comment_layout(probe, comments, 920, 6, 3)
        total = record.get("comment_count")
        shown = len(metadata["comments"])
        metadata["comments_heading"] = _comments_title(comments)
        metadata["comments_note"] = (f"共 {_metric_count(total)} 条评论，展示 {shown} 条"
                                     if type(total) is int and total > shown else "")
        qr = _load_detail_qr(record.get("mini_app_qr_data_url"))
        # Reserve the final page's photos and code on every page: simple and bounded.
        after_body, tail = _detail_chrome(metadata, _gallery_count(record), qr is not None, True)
        per_page = (MAX_HEIGHT - _DETAIL_BODY_TOP - after_body - tail) // _DETAIL_LINE
        if per_page < 1:
            raise RenderError("Post detail body region is too small")
        # Reserve final-page media before wrapping. Stop after one overflow line,
        # before allocating any page canvas or creating any output files.
        raw = record["text"].replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for paragraph in raw.split("\n"):
            cleaned = fonts.clean(paragraph, len(paragraph))
            remaining = per_page * _DETAIL_PAGE_LIMIT - len(lines)
            lines.extend(wrap(cleaned, 26, 920, remaining + 1))
            if len(lines) > per_page * _DETAIL_PAGE_LIMIT:
                raise RenderError("Post detail exceeds 3 pages; use the full-text attachment")
        if not raw.strip():
            lines = ["原帖未提供文字。"]
        pages = (len(lines) + per_page - 1) // per_page
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        batch = f"campus-detail-{uuid4().hex}"
        for index in range(pages):
            output = directory / f"{batch}-{index + 1:02}.png"
            pending = output.with_suffix(".tmp")
            with ExitStack() as resources:
                gallery = []
                if index == pages - 1:
                    for path in _gallery_paths(record):
                        photo = _load_thumbnail(path, (920, _GALLERY_HEIGHT))
                        if photo is not None:
                            resources.callback(photo.close)
                            gallery.append(photo)
                image = _detail_page(metadata, lines[index * per_page:(index + 1) * per_page],
                                     fonts, index, pages, gallery, qr if index == pages - 1 else None)
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
        raise RenderError("Post detail rendering failed; use the text fallback") from exc
    finally:
        if qr is not None:
            qr.close()
