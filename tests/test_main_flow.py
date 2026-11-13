"""Exercise real plugin orchestration without starting an AstrBot process."""
import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid

from client import CampusError
from digest import build_digest, build_prompt, digest_as_text, SYSTEM_PROMPT


def plugin_class(overrides):
    path = Path(os.environ.get('YUNSHANG_TEST_MAIN_SOURCE', Path(__file__).parents[1] / 'main.py'))
    source = ast.parse(path.read_text(encoding='utf-8'))
    original = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'YunshangPlugin')
    names = {'_cfg_bool', '_prepare_digest', '_attach_thumbnails', '_render_locked'}
    methods = [n for n in original.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    cls = ast.ClassDef(name='Plugin', bases=[], keywords=[], body=methods, decorator_list=[])
    tree = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    scope = dict(asyncio=asyncio, time=time, uuid=uuid, CampusError=CampusError,
                 logger=logging.getLogger('test.campus.flow'), SYSTEM_PROMPT=SYSTEM_PROMPT,
                 build_digest=build_digest, build_prompt=build_prompt,
                 digest_as_text=digest_as_text, **overrides)
    exec(compile(tree, str(path), 'exec'), scope)
    return scope['Plugin']


class MainFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.rendered = []
        self.posts = [dict(id=str(1000+i), text=f'正文{i}\n'+'需要保留的段落\n'*8+f'末尾{i}',
                           published_at='2026-11-13 12:00:00', category='测试', truncated=False,
                           image_count=4, like_count=100-i, trace_count=0, comment_count=0,
                           image_urls=[f'photo-{i}-{j}' for j in range(4)],
                           thumbnail_urls=[f'thumb-{i}-{j}' for j in range(4)]) for i in range(30)]
        outer = self
        class Collector:
            def __init__(self, *args, **kwargs):pass
            async def fetch_window(self, *args):
                return dict(posts=copy.deepcopy(outer.posts), total=len(outer.posts), scanned=len(outer.posts),
                            scan_limited=False, invalid_dates=0)
            async def close(self):pass
        def renderer(digest, *args):
            outer.rendered.append(copy.deepcopy(digest))
            return [f'{outer.tmp.name}/long-image.png']
        self.overrides = dict(CampusClient=Collector, render_digest=renderer)
        self.plugin = self.make_plugin()
        self.end = datetime(2026,11,13,16,tzinfo=timezone(timedelta(hours=8)))

    def make_plugin(self, **updates):
        plugin = plugin_class({**self.overrides, **updates})()
        plugin.config = {'push_ai_enable':False,'card_images_enable':True}
        plugin.client = SimpleNamespace(school='测试学校',community='测试校园墙',community_id='13')
        plugin._render_lock = asyncio.Lock()
        plugin._data_dir = lambda:Path(self.tmp.name)
        async def get(url):
            self.calls.append(url)
            return f'/synthetic/{url}.jpg'
        plugin._media_client = lambda:SimpleNamespace(get=get)
        return plugin

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def prepare(self, plugin=None):
        return await (plugin or self.plugin)._prepare_digest(self.end-timedelta(hours=4),self.end,preview=True)

    async def test_top10_are_selected_before_loading_three_photos_each(self):
        digest,pages = await self.prepare()
        self.assertEqual((digest['total'],digest['shown']),(30,10))
        self.assertEqual(len(pages),1)
        self.assertTrue(all(p['kind']=='image' for p in pages))
        self.assertEqual(len(self.calls),30)
        self.assertTrue(all(len(p['image_paths'])==3 for p in self.rendered[0]['items']))
        self.assertEqual([p['id'] for p in digest['items']],[p['id'] for p in self.posts[:10]])
        for post in self.posts[:10]:
            self.assertIn(f"#{post['id']}\n",pages[0]['text'])
        self.assertIn('末尾0',pages[0]['text'])
        self.assertIn('末尾9',digest['items'][-1]['body'])
        self.assertNotIn('image_urls',digest['items'][0])

    async def test_gallery_preserves_order_and_does_not_fetch_a_fourth_image(self):
        async def get(url):
            self.calls.append(url)
            if url.endswith('-0'):await asyncio.sleep(0.01)
            return None if url.endswith('-1') else f'/synthetic/{url}.jpg'
        self.plugin._media_client = lambda:SimpleNamespace(get=get)
        item=copy.deepcopy(self.posts[0])
        await self.plugin._attach_thumbnails({'items':[item]})
        self.assertEqual(item['image_paths'],['/synthetic/thumb-0-0.jpg','/synthetic/thumb-0-2.jpg'])
        self.assertEqual(len(self.calls),3)

    async def test_render_failure_keeps_top10_in_one_bounded_text(self):
        for p in self.posts:p['text']=p['id']+'长正文'*500
        def fail(*args,**kwargs):raise OSError('synthetic rendering failure')
        digest,pages=await self.prepare(self.make_plugin(render_digest=fail))
        self.assertEqual((digest['shown'],len(pages)),(10,1))
        self.assertTrue(all(p['kind']=='text' for p in pages))
        self.assertLessEqual(len(pages[0]['text']),3500)
        for post in self.posts[:10]:self.assertIn(f"#{post['id']}\n",pages[0]['text'])
        self.assertNotIn('#1010\n',pages[0]['text'])

    async def test_unexpected_multiple_images_fall_back_to_one_text(self):
        digest,pages=await self.prepare(self.make_plugin(render_digest=lambda *args:['a.png','b.png']))
        self.assertEqual(digest['shown'],10)
        self.assertEqual(len(pages),1)
        self.assertEqual(pages[0]['kind'],'text')

    async def test_images_disabled_preserves_body(self):
        self.plugin.config['card_images_enable']=False
        _,pages=await self.prepare()
        self.assertTrue(all(not p.get('image_paths') for p in self.rendered[0]['items']))
        self.assertEqual(self.calls,[])
        self.assertIn('末尾0',pages[0]['text'])

    async def test_cancelled_render_keeps_lock_until_worker_finishes(self):
        entered=asyncio.Event()
        release=threading.Event()
        loop=asyncio.get_running_loop()
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
        first=asyncio.create_task(self.plugin._render_locked(blocked))
        await entered.wait()
        first.cancel()
        second=asyncio.create_task(self.plugin._render_locked(lambda:'second'))
        try:
            await asyncio.sleep(0.01)
            self.assertTrue(self.plugin._render_lock.locked())
            self.assertFalse(first.done())
            self.assertFalse(second.done())
        finally:release.set()
        with self.assertRaises(asyncio.CancelledError):await first
        self.assertEqual(await second,'second')

    async def test_repeated_cancellation_and_render_error_do_not_release_lock_early(self):
        entered=asyncio.Event()
        release=threading.Event()
        loop=asyncio.get_running_loop()
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
            raise OSError('synthetic image failure during reload')
        task=asyncio.create_task(self.plugin._render_locked(blocked))
        await entered.wait()
        try:
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertTrue(self.plugin._render_lock.locked())
                self.assertFalse(task.done())
        finally:release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.plugin._render_lock.locked())


if __name__=='__main__':unittest.main()
