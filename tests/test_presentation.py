import copy
import json
import unittest
from presentation import format_overview, format_detail_source, is_campus_request, public_query_result

class PresentationTests(unittest.TestCase):
    def test_detail_source_shows_only_verified_link_or_actual_miniprogram_path(self):
        post={'id':'91','mini_app_path':'/pages/post/detail/detail?id=91'}
        source=format_detail_source(post,'测试校园墙')
        self.assertIn(post['mini_app_path'],source)
        self.assertNotIn('https://',source)
        post['post_url']='https://ys.qimiaoyuanfen.com/verified-share'
        self.assertIn(post['post_url'],format_detail_source(post,'测试校园墙'))
        for bad in ('https://unrelated.example/91','https://ys.qimiaoyuanfen.com@unrelated.example/91','https://ys.qimiaoyuanfen.com/91\nextra'):
            post['post_url']=bad
            source=format_detail_source(post,'测试校园墙')
            self.assertNotIn(bad,source)
            self.assertIn(post['mini_app_path'],source)

    def test_detail_qr_is_an_entry_not_raw_model_data(self):
        post={'id':'91','mini_app_path':'/pages/post/detail/detail?id=91',
              'mini_app_qr_data_url':'data:image/jpeg;base64,private-qr'}
        source=format_detail_source(post,'测试校园墙')
        self.assertIn('微信扫一扫',source)
        self.assertNotIn('private-qr',source)
        clean=public_query_result({'posts':[post]})
        self.assertNotIn('mini_app_qr_data_url',clean['posts'][0])
        self.assertIn('mini_app_qr_data_url',post)

    def result(self):
        return {'community':'民大校园墙','cached':False,'posts':[
            {'id':str(i),'text':'同学分享图书馆使用体验。'*100,'truncated':False,
             'published_at':'2026-11-13 16:20:00','image_count':0} for i in range(3)]}

    def test_list_is_bounded_and_blank_line_separated(self):
        output=format_overview(self.result())
        self.assertLess(len(output),450)
        self.assertEqual(output.count('（节选）'),3)
        self.assertIn('\n\n2.',output)
        self.assertIn('/校园墙 详情',output)

    def test_short_post_is_not_marked_truncated(self):
        result=self.result()
        result['posts']=[{**result['posts'][0],'text':'找同学一起自习','image_count':1}]
        output=format_overview(result)
        self.assertNotIn('（节选）',output)
        self.assertIn('配图1张',output)

    def test_empty_and_upstream_truncated_post_are_explicit(self):
        result=self.result()
        result['posts']=[{**result['posts'][0],'text':'','image_count':2},
                         {**result['posts'][1],'text':'已截取的短文字','truncated':True}]
        output=format_overview(result)
        self.assertIn('正文为空',output)
        self.assertIn('（节选）',output)

    def test_unrelated_chat_is_not_a_campus_request(self):
        for query in ('校园墙最近的热点','看看云上校友圈','民大表白墙怎么了'):
            self.assertTrue(is_campus_request(query))
        for query in ('今天吃什么','给我写一首诗','校园卡怎么挂失'):
            self.assertFalse(is_campus_request(query))

    def test_fixed_disclaimer_is_not_shown(self):
        output=format_overview(self.result())
        self.assertNotIn('未经核实',output)
        self.assertNotIn('同学投稿',output)

    def test_tool_reply_omits_raw_image_urls_without_mutating_source(self):
        result=self.result()
        result['posts'][0].update(image_urls=['https://ysxy.img.qimiaoyuanfen.com/a.jpg'],thumbnail_urls=['thumb'])
        clean=public_query_result(result)
        self.assertNotIn('image_urls',clean['posts'][0])
        self.assertNotIn('thumbnail_urls',clean['posts'][0])
        self.assertIn('image_urls',result['posts'][0])

    def test_overview_shows_compact_known_counts_and_keeps_unknowns_unknown(self):
        result = self.result()
        result['posts'] = [
            {**result['posts'][0], 'text': '已知互动', 'like_count': 12, 'trace_count': 0},
            {**result['posts'][1], 'text': '部分已知', 'like_count': None, 'trace_count': 3},
            {**result['posts'][2], 'text': '完全未知', 'like_count': None, 'trace_count': None},
        ]
        before = copy.deepcopy(result)
        output = format_overview(result)
        blocks = output.split('\n\n')
        self.assertIn('赞12 · 蹲蹲0', blocks[1])
        self.assertIn('蹲蹲3', blocks[2])
        self.assertNotIn('赞', blocks[2])
        self.assertNotIn('赞', blocks[3])
        self.assertNotIn('蹲蹲', blocks[3])
        self.assertNotIn('None', output)
        self.assertEqual(result, before)

    def test_engagement_labels_preserve_overview_paragraph_and_length_bounds(self):
        result = self.result()
        for post in result['posts']:
            post.update(like_count=150, trace_count=20, comment_count=10)
        output = format_overview(result)
        self.assertLess(len(output), 450)
        self.assertEqual(len(output.split('\n\n')), 5)  # Header, three posts, command.
        self.assertEqual(output.count('赞150 · 蹲蹲20'), 3)
        self.assertEqual(output.count('（节选）'), 3)
        for post in result['posts']:
            self.assertEqual(output.count(f"#{post['id']} ·"), 1)

    def test_public_tool_retains_metrics_and_source_snapshot_without_image_links(self):
        result = self.result()
        result['posts'][0].update(like_count=0, trace_count=None, comment_count=7,
                                  image_urls=['https://ysxy.img.qimiaoyuanfen.com/private.jpg'],
                                  thumbnail_urls=['private-thumbnail'], image_path='private-path', image_paths=['private-gallery'])
        before = copy.deepcopy(result)
        public = public_query_result(result)
        post = public['posts'][0]
        self.assertEqual((post['like_count'], post['trace_count'], post['comment_count']), (0, None, 7))
        self.assertNotIn('private', json.dumps(public))
        post['like_count'] = 99
        self.assertEqual(result, before)

if __name__=='__main__':unittest.main()
