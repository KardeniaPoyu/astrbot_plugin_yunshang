"""Comments: parsing, selection, Top10 fetching, and both card layouts."""
import json
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

import cards
from client import CampusClient, comments_view, select_comments
from digest import build_digest

SCHOOL = {"current_community_id": 37, "current_community_name": "民大校园墙",
          "current_community_school_name": "中央民族大学"}
BEIJING = timezone(timedelta(hours=8))


def raw_post(post_id=91, **extra):
    return {"id": post_id, "community_id": 37, "user_id": 500, "detail": "正文",
            "create_time": "2026-11-13 12:00:00", "count_comment": 4, **extra}


def raw_comment(name, text, likes=None, user=1, replies=(), **extra):
    row = {"show_user_name": name, "detail": text, "create_time": "2026-11-13 12:30:00",
           "user_id": user, "reply_comment_list": list(replies), **extra}
    if likes is not None:
        row["count_star"] = likes
    return row


THREADS = [
    raw_comment("低赞", "第一条", 1),
    raw_comment("高赞", "联系我 13812345678", "40", replies=[
        raw_comment("路人", "顶", 9, user=2, reply_show_user_name="高赞"),
        raw_comment("楼主本人", "谢谢！", 0, user=500, reply_show_user_name="高赞"),
    ]),
    raw_comment("中赞", "第三条", 12),
    raw_comment("冷门", "没人看", 0, replies=[
        raw_comment("楼主本人", "已解决", 0, user=500, reply_show_user_name="冷门")]),
    raw_comment("空白", "   "),
]


class CommentParsingTests(unittest.TestCase):
    def test_fields_redaction_and_author_by_matching_user_id(self):
        comments = comments_view(raw_post(comment_list=THREADS))
        self.assertEqual([c["name"] for c in comments], ["低赞", "高赞", "中赞", "冷门"])
        top = comments[1]
        self.assertEqual(top["like_count"], 40)
        self.assertIn("[电话隐藏]", top["text"])
        self.assertNotIn("13812345678", top["text"])
        self.assertEqual([(r["name"], r["is_author"], r["reply_to"]) for r in top["replies"]],
                         [("路人", False, "高赞"), ("楼主本人", True, "高赞")])

    def test_explicit_author_flag_wins_and_unknown_likes_stay_unknown(self):
        row = raw_comment("某人", "内容", user=500, is_author=0)
        (comment,) = comments_view(raw_post(comment_list=[row]))
        self.assertFalse(comment["is_author"])
        self.assertIsNone(comment["like_count"])
        self.assertIsNone(comments_view(raw_post()))  # Not provided differs from empty.
        self.assertEqual(comments_view(raw_post(comment_list=[])), [])

    def test_selection_ranks_by_likes_and_keeps_author_replies(self):
        comments = comments_view(raw_post(comment_list=THREADS))
        picked = select_comments(comments, limit=2, replies=1)
        self.assertEqual([c["name"] for c in picked], ["高赞", "中赞"])
        self.assertEqual([r["name"] for r in picked[0]["replies"]], ["楼主本人"])
        detail = select_comments(comments, limit=2, replies=3, author_threads=2)
        self.assertEqual([c["name"] for c in detail], ["高赞", "中赞", "冷门"])
        self.assertEqual([r["name"] for r in detail[0]["replies"]], ["路人", "楼主本人"])


class CommentClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.details = {"91": raw_post(91, comment_list=THREADS),
                        "92": raw_post(92, comment_list=[]),
                        "93": raw_post(93, community_id=99, comment_list=THREADS)}
        self.paths = []

        def handler(request):
            body = json.loads(request.content)
            self.paths.append(request.url.path)
            if request.url.path.endswith("/getxcxcode"):
                return httpx.Response(200, json={"code": "0000", "data": {}})
            if request.url.path.endswith("/community/lists"):
                return httpx.Response(200, json={"code": "0000", "data": SCHOOL})
            if body["id"] == "94":
                return httpx.Response(500)
            return httpx.Response(200, json={"code": "0000", "data": self.details[body["id"]]})

        self.client = CampusClient("PHPSESSID=example", transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_hot_comments_is_best_effort_and_campus_checked(self):
        result = await self.client.hot_comments(["91", "92", "93", "94"])
        self.assertEqual(list(result), ["91"])
        self.assertEqual([c["name"] for c in result["91"]], ["高赞", "中赞"])
        self.assertEqual(set(self.paths), {"/base/community/lists", "/article/article/info"})
        self.assertFalse(self.client.expired)

    async def test_detail_view_carries_selected_comments(self):
        self.details["91"]["id"] = 91
        result = await self.client.query("detail", post_id="91")
        names = [c["name"] for c in result["posts"][0]["comments"]]
        self.assertEqual(names, ["高赞", "中赞", "低赞", "冷门"])


def comment(name, text, likes=None, author=False, replies=()):
    return dict(name=name, text=text, like_count=likes, is_author=author,
                replies=[dict(name=n, text=t, like_count=None, is_author=a, reply_to=to)
                         for n, t, a, to in replies])


class CommentCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def capture(self, render, *args):
        drawn, body = [], []
        original = ImageDraw.ImageDraw.text

        def text(draw, xy, value, *a, **kw):
            box = draw.textbbox(xy, value, font=kw["font"], anchor=kw.get("anchor"))
            self.assertGreaterEqual(min(box[:2]), 0)
            self.assertLessEqual(box[2], draw.im.size[0])
            drawn.append(value)
            if kw["font"].size == 26:
                body.append(value)
            return original(draw, xy, value, *a, **kw)

        with patch.object(ImageDraw.ImageDraw, "text", text):
            paths = render(*args)
        return paths, drawn, body

    def test_digest_item_shows_hot_comments_with_author_badge(self):
        posts = [dict(id=str(100 + i), text=f"原帖正文{i}", published_at="2026-11-13 12:00:00",
                      category="测试", image_count=0, like_count=50 - i, trace_count=0,
                      comment_count=3) for i in range(3)]
        start = datetime(2026, 11, 13, 12, tzinfo=BEIJING)
        hot = {"100": [comment("热心同学", "评论内容甲", 36, replies=[("发帖人", "楼主的回复", True, "热心同学")])],
               "101": [comment("无赞同学", "评论内容乙")]}
        digest = build_digest(dict(posts=posts, total=3), start, start + timedelta(hours=4), comments=hot)
        hot["100"][0]["name"] = "mutated"
        self.assertEqual(digest["items"][0]["comments"][0]["name"], "热心同学")
        self.assertNotIn("comments", digest["items"][2])
        with_comments, drawn, body = self.capture(cards.render_digest, digest, self.temp.name)
        for expected in ("高赞热评", "评论精选", "热心同学", "赞 36", "楼主", "回复 热心同学", "楼主的回复", "无赞同学"):
            self.assertIn(expected, drawn)
        self.assertNotIn("评论内容甲", body)
        for item in digest["items"]:
            item.pop("comments", None)
        without, *_ = self.capture(cards.render_digest, digest, self.temp.name)
        with Image.open(with_comments[0]) as a, Image.open(without[0]) as b:
            self.assertGreater(a.height, b.height)

    def test_detail_comments_only_on_last_page_with_counts(self):
        text = "\n".join(f"完整第{i:03}行" for i in range(430))
        post = dict(id="C-1", text=text, comment_count=18,
                    comments=[comment("甲同学", "第一条热评", 20), comment("发帖人", "楼主补充", 3, True)])
        paths, drawn, body = self.capture(cards.render_post_detail, post, "合成校园墙", self.temp.name)
        self.assertGreater(len(paths), 1)
        self.assertEqual(body, text.split("\n"))
        for expected in ("甲同学", "第一条热评", "楼主补充", "楼主", "共 18 条评论，展示 2 条"):
            self.assertEqual(drawn.count(expected), 1, expected)

    def test_detail_without_comments_matches_previous_layout(self):
        post = dict(id="C-2", text="原文")
        plain, *_ = self.capture(cards.render_post_detail, post, "合成校园墙", self.temp.name)
        empty, drawn, _ = self.capture(cards.render_post_detail, dict(post, comments=[]), "合成校园墙", self.temp.name)
        self.assertNotIn("评论精选", drawn)
        with Image.open(plain[0]) as a, Image.open(empty[0]) as b:
            self.assertEqual(a.tobytes(), b.tobytes())


if __name__ == "__main__":
    unittest.main()
