"""Local, bounded campus digest cards. Requires Pillow and a real CJK font.

Call ``render_digest`` in a worker thread. It returns absolute PNG paths in
page order; the caller owns their lifecycle. Catch ``RenderError`` to use the
text fallback. There is no network access or persistent state in this module.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import unicodedata
from uuid import uuid4

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Allow the host to import RenderError and use text fallback.
    Image = ImageDraw = ImageFont = None


class RenderError(RuntimeError):
    """The digest cannot be safely rendered; use the caller's text fallback."""


WIDTH = 1080
MAX_HEIGHT = 2400
_CST = timezone(timedelta(hours=8))
_PAPER = "#F6F1E7"
_CARD = "#FFFCF6"
_BURGUNDY = "#742F40"
_INK = "#302A28"
_MUTED = "#756C65"
_RULE = "#DFD4C5"
_TINT = "#EDE2D5"
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
    ]


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
                font = ImageFont.truetype(str(path), 22)
                missing = self._signature(font, "\U0010ffff")
                # Loading a .ttf successfully is not proof of Chinese support.
                probes = "校园同学投稿图摘录时间，。…〔〕"
                if any(self._signature(font, char) == missing for char in probes):
                    continue
                self.path = str(path)
                self._sizes[22] = font
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

    def get(self, size):
        if size not in self._sizes:
            self._sizes[size] = ImageFont.truetype(self.path, size)
        return self._sizes[size]

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


def _item_layout(item, fonts, start, number):
    title = _wrap(fonts.clean(item["title"], 320) or "同学分享", fonts.get(34), 850, 2)
    summary = _wrap(fonts.clean(item["summary"]) or "请查看原帖了解详情。", fonts.get(27), 850, 3)
    category = _wrap(fonts.clean(item["category"], 64).replace("\n", " ") or "校园日常", fonts.get(20), 630, 1)[0]
    source = _wrap("来源 #" + fonts.clean(item["id"], 128).replace("\n", " "), fonts.get(20), 450, 1)[0]
    info = _published(item["published_at"], start)
    if item["image_count"]:
        count = str(item["image_count"]) if item["image_count"] < 1000 else "999+"
        info += f" · {count} 张图片未识别"
    info = _wrap(info, fonts.get(20), 385, 1)[0]
    # These line advances are greater than the measured font ink height.
    height = 26 + 26 + 10 + 42 * len(title) + 12 + 37 * len(summary) + 16 + 26 + 26
    return dict(title=title, summary=summary, category=category, source=source,
                info=info, height=height, number=number)


def _text(draw, fonts, xy, text, size, fill=_INK, anchor="lt"):
    draw.text(xy, text, font=fonts.get(size), fill=fill, anchor=anchor)


def _page(digest, start, end, items, fonts, page_index, pages, shown):
    hours = f"{(end - start).total_seconds() / 3600:g}"
    interval_label = " ".join(hours.zfill(2) + "H")
    layouts = [_item_layout(item, fonts, start, page_index * 4 + i + 1) for i, item in enumerate(items)]
    top = 456
    footer_height = 144 if digest["scan_limited"] else 112
    bottom = top + sum(item["height"] for item in layouts) + 16 * (len(layouts) - 1)
    height = bottom + footer_height
    if height > MAX_HEIGHT:
        raise RenderError("Card layout exceeds its maximum height")
    image = Image.new("RGB", (WIDTH, height), _PAPER)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 9), fill=_BURGUNDY)
    _text(draw, fonts, (56, 47), f"C A M P U S   /   {interval_label}", 19, _BURGUNDY)
    draw.rounded_rectangle((738, 36, 1024, 80), radius=22, fill=_BURGUNDY)
    _text(draw, fonts, (881, 46), "同学投稿 · 非官方", 22, _CARD, "mt")
    _text(draw, fonts, (54, 105), "校园墙 · 新帖集", 58, _BURGUNDY)
    community = _wrap(fonts.clean(digest["community"], 256).replace("\n", " "), fonts.get(25), 960, 1)[0]
    _text(draw, fonts, (58, 184), community, 25, _MUTED)
    draw.line((56, 233, 1024, 233), fill=_RULE, width=2)

    if start.date() == end.date():
        date_label = start.strftime("%Y.%m.%d")
        time_label = f"{start:%H:%M} — {end:%H:%M}"
    else:
        date_label = start.strftime("%Y.%m.%d") + " → " + end.strftime("%Y.%m.%d")
        time_label = f"{start:%H:%M} — {end:%H:%M}"
    _text(draw, fonts, (58, 258), date_label + "  /  北京时间", 21, _MUTED)
    _text(draw, fonts, (56, 295), time_label, 34, _BURGUNDY)
    draw.line((617, 259, 617, 331), fill=_RULE, width=2)
    total = str(digest["total"]) if digest["total"] < 1_000_000 else "999999+"
    count_label = f"已收集 {total}  /  展示 {shown}"
    _text(draw, fonts, (651, 259), count_label, 23, _BURGUNDY)
    mode = "AI 整理 · 请以原帖为准" if digest["mode"] == "ai" else "原文摘录 · 请以原帖为准"
    _text(draw, fonts, (651, 303), mode, 21, _MUTED)
    intro = fonts.clean(digest["intro"], 512) or "把刚刚发生的校园小事，收进这一页。"
    for i, line in enumerate(_wrap(intro, fonts.get(25), 948, 2)):
        _text(draw, fonts, (58, 365 + i * 34), line, 25, _MUTED)

    y = top
    for item in layouts:
        draw.rounded_rectangle((48, y + 3, 1032, y + item["height"] + 3), radius=20, fill=_TINT)
        draw.rounded_rectangle((48, y, 1032, y + item["height"]), radius=20, fill=_CARD, outline=_RULE, width=1)
        _text(draw, fonts, (76, y + 24), f"{item['number']:02}", 33, _BURGUNDY)
        draw.line((78, y + 75, 112, y + 75), fill=_RULE, width=2)
        cat_width = fonts.get(20).getlength(item["category"]) + 24
        draw.rounded_rectangle((144, y + 21, 144 + cat_width, y + 54), radius=8, fill=_TINT)
        _text(draw, fonts, (156, y + 26), item["category"], 20, _BURGUNDY)
        row = y + 62
        for line in item["title"]:
            _text(draw, fonts, (144, row), line, 34)
            row += 42
        row += 12
        for line in item["summary"]:
            _text(draw, fonts, (144, row), line, 27, _MUTED)
            row += 37
        row += 16
        _text(draw, fonts, (144, row), item["source"], 20, _MUTED)
        _text(draw, fonts, (1004, row), item["info"], 20, _MUTED, "rt")
        y += item["height"] + 16

    line_y = bottom + 28
    draw.line((56, line_y, 1024, line_y), fill=_RULE, width=2)
    footer_y = line_y + 19
    if digest["scan_limited"]:
        _text(draw, fonts, (58, footer_y), "扫描未完成 · 仅展示已收集内容，数量可能不完整", 22, _BURGUNDY)
        footer_y += 34
    _text(draw, fonts, (58, footer_y), "同学投稿，内容未经核实；图片未识别。请回原帖确认详情。", 20, _MUTED)
    _text(draw, fonts, (58, footer_y + 33), f"每 {hours} 小时一辑  /  校园生活的片刻记录", 18, _MUTED)
    _text(draw, fonts, (1024, footer_y + 31), f"{page_index + 1} / {pages}", 22, _BURGUNDY, "rt")
    return image


def render_digest(digest: dict, output_dir, font_path="") -> list[str]:
    """Render at most eight posts, four per page, at 1080px and <=2400px tall.

    ``window_start``/``window_end`` must be aware ISO strings. Display uses
    Beijing time. ``shown`` selects the first N items and is capped at eight;
    the displayed count always matches the actual rendered selection. An empty
    selection returns [] without requiring Pillow, a font, or an output folder.
    Each call uses unique filenames and removes its own files if any page fails.
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
            image = _page(digest, start, end, selected[index * 4:(index + 1) * 4],
                          fonts, index, pages, len(selected))
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
