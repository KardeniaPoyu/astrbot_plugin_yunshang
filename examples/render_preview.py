"""Render fictional campus posts only; no API, contacts, or credentials.

From the repository root:
    python -B examples/render_preview.py
    python -B examples/render_preview.py --font /path/to/NotoSansCJK-Regular.ttc

The only persistent output is assets/preview.png (or the explicit --output).
"""

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cards import render_digest


def demo_digest():
    """All IDs, posts, dates and counts below are synthetic demonstration data."""
    return {
        "community": "民大校园墙  /  合成示例 · 非真实投稿",
        "window_start": "2026-11-12T12:00:00+08:00",
        "window_end": "2026-11-12T16:00:00+08:00",
        "total": 12,
        "shown": 4,
        "scan_limited": False,
        "mode": "ai",
        "intro": "有人寻找一起自习的伙伴，有人分享读过的好书。\n这四小时里，校园生活又添了几笔温暖的注脚。",
        "items": [
            {
                "id": "DEMO-001",
                "title": "周末自习，想找一个并肩努力的搭子",
                "summary": "想约同学周末在图书馆各自学习，间隙交流复习进度。\n偏好安静、有计划的节奏；时间与地点请以原帖为准。",
                "published_at": "2026-11-12T12:26:00+08:00",
                "category": "学习搭子",
                "image_count": 0,
            },
            {
                "id": "DEMO-002",
                "title": "一只米白色帆布袋，在等主人认领",
                "summary": "同学发帖称在校园公共区域捡到帆布袋，内有书本。\n失主请回原帖核对物品特征；附图内容尚未识别。",
                "published_at": "2026-11-12T13:08:00+08:00",
                "category": "失物招领",
                "image_count": 2,
            },
            {
                "id": "DEMO-003",
                "title": "让读完的书，开始下一段旅程",
                "summary": "几本读完的文学与通识读物，希望交换给有兴趣的同学。\n书目、版本及品相见原帖，交换方式由双方自行确认。",
                "published_at": "2026-11-12T14:17:00+08:00",
                "category": "旧书交换",
                "image_count": 3,
            },
            {
                "id": "DEMO-004",
                "title": "晚风与散步，收集校园里的秋日颜色",
                "summary": "有同学想组织一次轻松的校园散步，用镜头记录秋色。\n欢迎摄影新手交流；集合安排及天气变化请查看原帖。",
                "published_at": "2026-11-12T15:42:00+08:00",
                "category": "校园日常",
                "image_count": 1,
            },
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", default="", help="Explicit Chinese-capable font path")
    parser.add_argument("--output", type=Path, default=ROOT / "assets" / "preview.png")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="campus-card-preview-") as scratch:
        pages = render_digest(demo_digest(), scratch, args.font)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pages[0], output)
    print(output)


if __name__ == "__main__":
    main()
