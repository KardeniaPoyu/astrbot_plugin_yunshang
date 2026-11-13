"""Run real detail orchestration without AstrBot, network access, or credentials."""
import ast
import asyncio
import copy
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from client import CampusError
import presentation


def main_class_ast():
    path = Path(os.environ.get('YUNSHANG_TEST_MAIN_SOURCE', Path(__file__).parents[1] / 'main.py'))
    source = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == 'YunshangPlugin')
    return path, cls


@dataclass
class Image:
    path: str

    @classmethod
    def fromFileSystem(cls, path):
        return cls(str(path))


@dataclass
class Plain:
    text: str


@dataclass
class File:
    name: str
    file: str


@dataclass
class MessageChain:
    chain: list


class Event:
    def __init__(self, send_result=None):
        self.send_result = send_result
        self.attempts = []

    def chain_result(self, chain):
        return MessageChain(chain)

    def plain_result(self, text):
        return MessageChain([Plain(text)])

    async def send(self, chain):
        self.attempts.append(chain)
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.send_result


def plugin_class(renderer):
    path, original = main_class_ast()
    names = {'_enabled', '_cfg_bool', '_query', '_prepare_detail', '_detail_chain',
             '_attach_thumbnails', '_render_locked', 'campus_command', 'campus_wall'}
    methods = []
    for node in original.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        method = copy.deepcopy(node)
        # Remove AstrBot registrations only: staticmethod affects real binding.
        method.decorator_list = [decorator for decorator in method.decorator_list
                                 if isinstance(decorator, ast.Name)
                                 and decorator.id == 'staticmethod']
        methods.append(method)
    cls = ast.ClassDef(name='Plugin', bases=[], keywords=[], body=methods, decorator_list=[])
    annotations = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[annotations, cls], type_ignores=[]))
    scope = {name: value for name, value in vars(presentation).items() if not name.startswith('_')}
    scope.update(asyncio=asyncio, copy=copy, json=json, re=re, uuid=uuid, Path=Path,
                 CampusError=CampusError, Image=Image, Plain=Plain, File=File, MessageChain=MessageChain,
                 logger=logging.getLogger('test.campus.detail'), render_post_detail=renderer)
    exec(compile(tree, str(path), 'exec'), scope)
    return scope['Plugin']


class DetailFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.tail = '这是五千字之后的原帖末段，必须完整保留。'
        self.body = '第一段原文。\n\n' + '这是需要保留的详细原文。' * 450 + '\n\n' + self.tail
        # Synthetic normalized source fields, not claimed to be live post links.
        self.post_url = 'https://wxaurl.cn/detail-test-fixture'
        self.mini_app_path = '/pages/post/detail/detail?id=24680'
        self.post = dict(
            id='24680', text=self.body, summary='这只是摘要，不能代替原文。',
            published_at='2026-11-13 16:20:00', category='校园日常', truncated=False,
            image_count=4, like_count=12, trace_count=3, comment_count=1,
            image_urls=[f'https://ysxy.img.qimiaoyuanfen.com/private-original-{i}.jpg' for i in range(4)],
            thumbnail_urls=[f'https://ysxy.img.qimiaoyuanfen.com/private-thumb-{i}.jpg' for i in range(4)],
            post_url=self.post_url, mini_app_path=self.mini_app_path,
            mini_app_id='wx0123456789abcdef',
        )
        self.result = dict(ok=True, action='detail', community='测试校园墙', school='测试学校',
                           community_id='13', posts=[self.post], cached=False,
                           fetched_at='2026-11-13 16:21:00')
        self.query_calls, self.media_calls, self.render_calls, self.ai_calls = [], [], [], []
        self.query_error = None
        self.render_error = None
        self.paths = [str(self.directory / 'detail-01.png'), str(self.directory / 'detail-02.png')]

        async def query(action, **kwargs):
            self.query_calls.append((action, kwargs))
            if self.query_error:
                raise self.query_error
            return copy.deepcopy(self.result)

        async def get(url):
            self.media_calls.append(url)
            return str(self.directory / ('private-cache-' + url.rsplit('/', 1)[-1]))

        def renderer(post, community, output_dir, font_path=''):
            self.render_calls.append((copy.deepcopy(post), community, Path(output_dir), font_path,
                                      self.plugin._render_lock.locked()))
            if self.render_error:
                raise self.render_error
            return list(self.paths)

        def provider(*args, **kwargs):
            self.ai_calls.append((args, kwargs))
            raise AssertionError('Detail delivery must not ask an AI provider to rewrite the post')

        self.plugin = plugin_class(renderer)()
        self.plugin.config = {'enable': True, 'card_images_enable': True,
                              'push_ai_enable': True, 'push_font_path': '  fixture-font.ttc  '}
        self.plugin.client = SimpleNamespace(query=query, ready=True)
        self.plugin.context = SimpleNamespace(get_using_provider=provider, get_provider_by_id=provider)
        self.plugin._render_lock = asyncio.Lock()
        self.plugin._data_dir = lambda: self.directory
        self.plugin._media_client = lambda: SimpleNamespace(get=get)

    async def prepare(self):
        self.assertTrue(callable(getattr(self.plugin, '_prepare_detail', None)),
                        'The real plugin must implement _prepare_detail')
        return await self.plugin._prepare_detail('24680')

    async def command(self, event=None):
        return [message async for message in self.plugin.campus_command(event or Event(), '详情', '24680')]

    def assert_gallery_chain(self, message):
        self.assertIsInstance(message, MessageChain)
        self.assertEqual(len(message.chain), len(self.paths) + 1)
        self.assertTrue(all(isinstance(part, Image) for part in message.chain[:-1]))
        self.assertEqual([part.path for part in message.chain[:-1]], self.paths)
        self.assertIsInstance(message.chain[-1], Plain)
        self.assertIn(self.post_url, message.chain[-1].text)
        self.assertNotIn('private-', message.chain[-1].text)

    def assert_full_text_file_fallback(self, message):
        self.assertIsInstance(message, MessageChain)
        self.assertGreaterEqual(len(message.chain), 2)
        attachment = message.chain[0]
        self.assertIsInstance(attachment, File)
        self.assertEqual(attachment.name, '校园墙-24680-全文.txt')
        path = Path(attachment.file)
        self.assertEqual(path, self.render_calls[-1][2] / 'post-fulltext.txt')
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.directory.resolve()))
        self.assertTrue(path.parent.name.startswith('preview-detail-'))
        # Read the real artifact written by _prepare_detail, not a captured mock.
        saved = path.read_text(encoding='utf-8')
        self.assertEqual(saved, self.body)
        self.assertGreater(len(saved), 5000)
        self.assertTrue(saved.endswith(self.tail))
        self.assertTrue(all(isinstance(part, Plain) for part in message.chain[1:]))
        text = '\n'.join(part.text for part in message.chain[1:])
        self.assertIn(self.post_url, text)
        self.assertIn(self.post_url, message.chain[-1].text)
        self.assertNotIn(self.body, text)
        self.assertNotIn(self.tail, text)
        self.assertNotIn(self.post['summary'], text)
        self.assertNotIn(self.directory.name, text)

    def assert_public_preview(self, response):
        public = json.loads(response)
        self.assertTrue(public['ok'])
        self.assertEqual(len(public['posts']), 1)
        self.assertEqual(public['posts'][0]['text'], self.body[:1200])
        self.assertTrue(public['posts'][0]['truncated'])
        self.assertNotIn(self.tail, response)
        self.assertNotIn('private-', response)
        self.assertNotIn('full_text_file', public)
        for key in ('image_urls', 'thumbnail_urls', 'image_path', 'image_paths'):
            self.assertNotIn(key, public['posts'][0])
        self.assertNotIn(self.directory.name, response)
        self.assertEqual(self.post['text'], self.body)
        self.assertFalse(self.post['truncated'])
        return public

    def test_canonical_command_registers_wall_and_preserves_circle_alias(self):
        _, cls = main_class_ast()
        method = next(node for node in cls.body if getattr(node, 'name', None) == 'campus_command')
        decorators = [node for node in method.decorator_list if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute) and node.func.attr == 'command']
        self.assertEqual(len(decorators), 1)
        self.assertEqual(ast.literal_eval(decorators[0].args[0]), '校园墙')
        aliases = next((node.value for node in decorators[0].keywords if node.arg == 'alias'), None)
        self.assertIsNotNone(aliases)
        self.assertIn('校园圈', ast.literal_eval(aliases))

    async def test_prepare_retains_full_body_late_tail_and_uses_locked_renderer(self):
        result, paths, source = await self.prepare()
        self.assertGreater(len(self.body), 5000)
        self.assertTrue(result['ok'])
        self.assertEqual(paths, self.paths)
        self.assertEqual(len(self.query_calls), 1)
        self.assertEqual(self.query_calls[0][0], 'detail')
        self.assertEqual(self.query_calls[0][1]['post_id'], '24680')
        self.assertEqual(len(self.render_calls), 1)
        post, community, directory, font, lock_held = self.render_calls[0]
        self.assertEqual(post['text'], self.body)
        self.assertTrue(post['text'].endswith(self.tail))
        self.assertEqual((community, font, lock_held), ('测试校园墙', 'fixture-font.ttc', True))
        self.assertTrue(directory.is_relative_to(self.directory))
        self.assertEqual(self.media_calls, self.post['thumbnail_urls'][:3])
        self.assertEqual(len(post['image_paths']), 3)
        self.assertEqual(re.findall(r'https?://[^\s]+', source), [self.post_url])
        self.assertEqual(self.ai_calls, [])

    async def test_detail_command_yields_one_chain_with_every_page_then_source(self):
        event = Event()
        messages = await self.command(event)
        self.assertEqual(len(messages), 1)
        self.assert_gallery_chain(messages[0])
        self.assertEqual(event.attempts, [])  # Commands yield; tool calls actively send.
        self.assertEqual(len(self.query_calls), 1)
        self.assertEqual(self.ai_calls, [])

    async def test_detail_tool_sends_one_chain_and_returns_only_public_preview(self):
        event = Event()  # None is a successful framework send; only False rejects it.
        response = await self.plugin.campus_wall(event, action='detail', post_id='24680')
        self.assertEqual(len(event.attempts), 1)
        self.assert_gallery_chain(event.attempts[0])
        public = self.assert_public_preview(response)
        self.assertEqual(public['sent'], len(self.paths))
        self.assertEqual(self.render_calls[0][0]['text'], self.body)
        self.assertEqual(self.ai_calls, [])

    async def test_tool_reports_false_or_raised_send_failure_without_success_claim(self):
        cases = ((False, None), (OSError('synthetic private-send-failure'), None),
                 (False, OSError('synthetic renderer failure')))
        for outcome, render_error in cases:
            with self.subTest(outcome=type(outcome).__name__, file_fallback=render_error is not None):
                self.render_error = render_error
                event = Event(outcome)
                response = await self.plugin.campus_wall(event, action='detail', post_id='24680')
                status = json.loads(response)
                self.assertEqual(len(event.attempts), 1)
                if render_error:
                    self.assert_full_text_file_fallback(event.attempts[0])
                else:
                    self.assert_gallery_chain(event.attempts[0])
                self.assertFalse(status['ok'])
                self.assertEqual(status['error'], 'send_failed')
                self.assertFalse(status.get('sent'))
                self.assertFalse(status.get('full_text_delivered'))
                self.assertNotIn(self.tail, response)
                self.assertNotIn('private-', response)
                self.assertNotIn('full_text_file', status)
                self.assertNotIn(self.directory.name, response)

    async def test_prepare_render_failure_writes_full_utf8_file_without_truncation(self):
        self.render_error = OSError('synthetic renderer failure')
        result, paths, source = await self.prepare()
        self.assertTrue(result['ok'])
        self.assertEqual(paths, [])
        self.assertEqual(result['posts'][0]['text'], self.body)
        self.assertFalse(result['posts'][0]['truncated'])
        path = Path(result['full_text_file'])
        self.assertEqual(path.read_text(encoding='utf-8'), self.body)
        self.assertIn(self.tail.encode('utf-8'), path.read_bytes())
        self.assertIn(self.post_url, source)
        self.assertNotIn('full_text_file', self.result)

    async def test_render_failure_command_sends_complete_file_not_summary(self):
        self.render_error = OSError('synthetic renderer failure')
        messages = await self.command()
        self.assertEqual(len(messages), 1)
        self.assert_full_text_file_fallback(messages[0])
        self.assertEqual(self.ai_calls, [])

    async def test_empty_renderer_output_falls_back_without_losing_body(self):
        self.paths = []
        messages = await self.command()
        self.assertEqual(len(messages), 1)
        self.assert_full_text_file_fallback(messages[0])

    async def test_render_failure_tool_sends_complete_file_but_returns_public_preview(self):
        self.render_error = OSError('synthetic renderer failure')
        event = Event()
        response = await self.plugin.campus_wall(event, action='detail', post_id='24680')
        self.assertEqual(len(event.attempts), 1)
        self.assert_full_text_file_fallback(event.attempts[0])
        public = self.assert_public_preview(response)
        self.assertEqual(public['sent'], 0)
        self.assertTrue(public['full_text_delivered'])
        self.assertEqual(public['posts'][0]['full_text_chars'], len(self.body))

    async def test_short_render_failure_stays_plain_including_3000_character_boundary(self):
        self.render_error = OSError('synthetic renderer failure')
        for body in ('简短原文。\n\n末段完整保留。', '文' * 2996 + '完整末段'):
            with self.subTest(characters=len(body)):
                self.post['text'] = body
                result, paths, source = await self.prepare()
                self.assertTrue(result['ok'])
                self.assertEqual(paths, [])
                self.assertNotIn('full_text_file', result)
                messages = await self.command()
                self.assertEqual(len(messages), 1)
                self.assertTrue(all(isinstance(part, Plain) for part in messages[0].chain))
                text = '\n'.join(part.text for part in messages[0].chain)
                self.assertIn(body, text)
                self.assertIn(self.post_url, text)
                self.assertNotIn(self.post['summary'], text)
                self.assertEqual(list(self.directory.rglob('*.txt')), [])

    async def test_no_public_url_does_not_fabricate_a_web_link(self):
        self.post.pop('post_url')
        _, _, source = await self.prepare()
        self.assertNotRegex(source, r'https?://')
        self.assertIn(self.mini_app_path, source)
        self.assertNotIn('private-', source)

    async def test_no_source_fields_does_not_build_an_unverified_web_address(self):
        for key in ('post_url', 'mini_app_path', 'mini_app_id'):
            self.post.pop(key)
        _, _, source = await self.prepare()
        self.assertNotRegex(source, r'https?://')
        self.assertIn('24680', source)

    async def test_disabled_thumbnails_still_render_complete_detail_text(self):
        self.plugin.config['card_images_enable'] = False
        _, paths, _ = await self.prepare()
        self.assertEqual(paths, self.paths)
        self.assertEqual(self.media_calls, [])
        self.assertEqual(self.render_calls[0][0]['text'], self.body)
        self.assertFalse(self.render_calls[0][0].get('image_paths'))

    async def test_disabled_plugin_stops_query_render_and_tool_delivery(self):
        self.plugin.config['enable'] = False
        result, paths, source = await self.prepare()
        self.assertEqual((result['ok'], result['error'], paths, source), (False, 'disabled', [], ''))
        messages = await self.command()
        self.assertEqual(len(messages), 1)
        self.assertTrue(all(isinstance(part, Plain) for part in messages[0].chain))
        event = Event()
        status = json.loads(await self.plugin.campus_wall(event, action='detail', post_id='24680'))
        self.assertEqual((status['ok'], status['error']), (False, 'disabled'))
        self.assertEqual((self.query_calls, self.media_calls, self.render_calls, event.attempts), ([], [], [], []))

    async def test_missing_auth_preserves_error_without_media_or_tool_delivery(self):
        self.plugin.client.ready = False
        self.query_error = CampusError('login_required', '尚未配置有效登录态')
        result, paths, source = await self.prepare()
        self.assertEqual((result['ok'], result['error'], paths, source), (False, 'login_required', [], ''))
        messages = await self.command()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].chain, [Plain('尚未配置有效登录态')])
        event = Event()
        status = json.loads(await self.plugin.campus_wall(event, action='detail', post_id='24680'))
        self.assertEqual((status['ok'], status['error']), (False, 'login_required'))
        self.assertEqual((self.media_calls, self.render_calls, event.attempts), ([], [], []))

    async def test_empty_detail_response_returns_not_found_without_rendering(self):
        self.result['posts'] = []
        result, paths, source = await self.prepare()
        self.assertEqual((result['ok'], result['error'], paths, source), (False, 'not_found', [], ''))
        self.assertEqual((self.media_calls, self.render_calls), ([], []))


if __name__ == '__main__':
    unittest.main()
