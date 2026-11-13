"""Render fictional campus posts only; no API, contacts, or credentials.

From the repository root:
    python -B examples/render_preview.py
    python -B examples/render_preview.py --font /path/to/NotoSansCJK-Regular.ttc

The only persistent output is assets/preview.png (or the explicit --output).
Sample JPEGs are drawn locally, labeled synthetic, and deleted after rendering.
"""

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cards import _Fonts, render_digest


def demo_digest():
    """All IDs, posts, dates and counts below are synthetic demonstration data."""
    digest = {
        "community": "民大校园墙  /  合成示例 · 非真实投稿",
        "window_start": "2026-11-12T12:00:00+08:00",
        "window_end": "2026-11-12T16:00:00+08:00",
        "total": 16,
        "shown": 10,
        "scan_limited": False,
        "mode": "ai",
        "ranking": "likes",
        "intro": "这四小时，大家关注的学习、失物与校园日常。",
        "items": [
            {
                "id": "DEMO-001",
                "title": "周末自习，想找一个并肩努力的搭子",
                "summary": "想约同学周末在图书馆各自学习，间隙交流复习进度。\n偏好安静、有计划的节奏；时间与地点请以原帖为准。",
                "published_at": "2026-11-12T12:26:00+08:00",
                "category": "学习搭子",
                "image_count": 0,
                "like_count": 128, "trace_count": 23, "comment_count": 18,
            },
            {
                "id": "DEMO-002",
                "title": "一只米白色帆布袋，在等主人认领",
                "summary": "同学发帖称在校园公共区域捡到帆布袋，内有书本。\n失主请回原帖核对物品特征与认领方式。",
                "published_at": "2026-11-12T13:08:00+08:00",
                "category": "失物招领",
                "image_count": 2,
                "like_count": 96, "trace_count": 12, "comment_count": 7,
            },
            {
                "id": "DEMO-003",
                "title": "让读完的书，开始下一段旅程",
                "summary": "几本读完的文学与通识读物，希望交换给有兴趣的同学。\n书目、版本及品相见原帖，交换方式由双方自行确认。",
                "published_at": "2026-11-12T14:17:00+08:00",
                "category": "旧书交换",
                "image_count": 3,
                "like_count": 74, "trace_count": 19, "comment_count": 11,
            },
            {
                "id": "DEMO-004",
                "title": "晚风与散步，收集校园里的秋日颜色",
                "summary": "有同学想组织一次轻松的校园散步，用镜头记录秋色。\n欢迎摄影新手交流；集合安排及天气变化请查看原帖。",
                "published_at": "2026-11-12T15:42:00+08:00",
                "category": "校园日常",
                "image_count": 1,
                "like_count": 52, "trace_count": 8, "comment_count": 5,
            },
        ],
    }
    digest["items"][0].update(
        title="用三张插画，记录校园生活的片段",
        body="今天画了三张合成插画：一只帆布袋、一页读书清单，还有秋天的校园。所有图片都是程序绘制的示意图，不是真实照片。\n\n"
             "第一张练习用简单线条表现物品，第二张试着整理读书清单，第三张把暖色屋顶与树叶放在一起。\n\n"
             "想继续画一些日常的小场景。先把这些练习收在一起，看看以后会有什么新想法。")
    extra = (
        ("社团开放日的合成记录", "活动交流", "这是一份虚构的社团开放日记录。想试试画画、读书和摄影的小活动。\n\n先从喜欢的项目开始，慢慢找到愿意坚持的事情。"),
        ("今天也想分享一本好书", "读书交流", "最近在读一本虚构的自然笔记，里面记录了很多观察植物的小方法。\n\n我打算准备一个小本子，把散步看到的叶子画下来。"),
        ("整理笔记，让复习轻松一点", "学习分享", "把课堂笔记重新分了目录，还给不熟悉的部分做了标记。\n\n这些文字只是合成排版示例，不包含真实课程信息。"),
        ("秋天的晚风很舒服", "校园日常", "秋天的晚风很舒服"),
        ("一起记录校园的颜色", "摄影交流", "想记录一天里校园颜色的变化：午后的树影、傍晚的天空、夜里亮起的灯。\n\n这里的配图都是合成示意图，欢迎把它当作一个小小的创作练习。"),
        ("给忙碌的一天留一点空白", "校园日常", "今天给自己留了一段没有计划的时间，散步、听歌，慢慢整理思绪。\n\n这条合成帖子用来展示长图最后一条内容和来源信息。"),
    )
    for index, (title, category, body) in enumerate(extra, 5):
        digest["items"].append(dict(
            id=f"DEMO-{index:03}", title=title, category=category, body=body,
            summary="合成摘要，不替代原文。", published_at="2026-11-12T15:50:00+08:00",
            image_count=0, like_count=52 - index * 4, trace_count=15 - index, comment_count=index % 4))
    for item, count in zip(digest["items"], (3, 1, 2, 3, 0, 2, 1, 0, 3, 1)):
        item.setdefault("body", item["summary"])
        item["gallery_count"] = count
        item["image_count"] = count + (1 if count == 3 else 0)
    return digest


def synthetic_images(directory, font_path=""):
    """Draw an object illustration, a tall screenshot, and a landscape sample."""
    fonts = _Fonts(font_path)
    paths = {}
    for post_id, size in (("DEMO-002", (800, 600)),
                          ("DEMO-003", (600, 900)),
                          ("DEMO-004", (1000, 600))):
        with Image.new("RGB", size, "#F5F1E8") as image:
            draw = ImageDraw.Draw(image)
            width, height = size
            if post_id == "DEMO-002":
                draw.ellipse((180, 475, 640, 540), fill="#DFD5C3")
                draw.arc((280, 125, 520, 380), 180, 360, fill="#9A7954", width=20)
                draw.rounded_rectangle((220, 255, 580, 500), 22,
                                       fill="#FFFCF1", outline="#B8A080", width=8)
                draw.rounded_rectangle((275, 330, 525, 425), 10, fill="#E7EDDF")
                draw.text((400, 377), "帆布袋 · 示意", font=fonts.get(28),
                          fill="#657853", anchor="mm")
            elif post_id == "DEMO-003":
                # A complete fictional screenshot: both heading and bottom text
                # should survive the renderer's contain fit without edge cropping.
                draw.rectangle((0, 92, width, height), fill="#FFFFFF")
                draw.text((40, 130), "我的读书清单", font=fonts.get(46, bold=True), fill="#1F2329")
                draw.text((40, 205), "虚构界面 · 无真实投稿", font=fonts.get(26), fill="#646A73")
                for row, (label, color) in enumerate((("文学随笔", "#75A694"),
                                                      ("自然笔记", "#D8A864"),
                                                      ("通识阅读", "#7B96C8"))):
                    top = 290 + row * 155
                    draw.rounded_rectangle((40, top, 130, top + 115), 6, fill=color)
                    draw.line((55, top + 12, 55, top + 103), fill="#FFFFFF", width=3)
                    draw.text((160, top + 18), label, font=fonts.get(32), fill="#1F2329")
                    draw.text((160, top + 68), "示例书目 / 可交换", font=fonts.get(24), fill="#646A73")
                draw.line((40, 800, 560, 800), fill="#DEE0E3", width=2)
                draw.text((300, 845), "完整页面 · 合成示例", font=fonts.get(26),
                          fill="#646A73", anchor="mm")
            else:
                draw.rectangle((0, 92, width, height), fill="#DDECF3")
                draw.rectangle((0, 380, width, height), fill="#C7D4B3")
                draw.rounded_rectangle((360, 230, 660, 410), 8, fill="#F4E8D2")
                draw.polygon(((335, 230), (510, 155), (685, 230)), fill="#C47F60")
                for x in (400, 480, 560):
                    draw.rectangle((x, 275, x + 42, 340), fill="#90B3C2")
                draw.polygon(((470, 600), (590, 600), (550, 410), (510, 410)), fill="#F6E8C9")
                for x, y, color in ((190, 270, "#D6A15A"), (800, 300, "#B8BC70")):
                    draw.line((x, y, x, 475), fill="#967455", width=18)
                    draw.ellipse((x - 95, y - 120, x + 95, y + 70), fill=color)
                draw.text((500, 540), "校园秋色 · 虚构插画", font=fonts.get(32),
                          fill="#53634D", anchor="mm")
            draw.rectangle((0, 0, width, 92), fill="#29476B")
            draw.text((width // 2, 46), "合成示例 / SYNTHETIC", font=fonts.get(30, bold=True),
                      fill="#FFFFFF", anchor="mm")
            path = Path(directory) / f"{post_id}-synthetic.jpg"
            image.save(path, quality=93, subsampling=0)
            paths[post_id] = str(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", default="", help="Explicit Chinese-capable font path")
    parser.add_argument("--output", type=Path, default=ROOT / "assets" / "preview.png")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="campus-card-preview-") as scratch:
        photos = synthetic_images(scratch, args.font)
        digest = demo_digest()
        samples = list(photos.values())
        for index, item in enumerate(digest["items"]):
            item["image_paths"] = [samples[(index + offset) % 3]
                                   for offset in range(item["gallery_count"])]
        pages = render_digest(digest, scratch, args.font)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pages[0], output)
    print(output)


if __name__ == "__main__":
    main()
