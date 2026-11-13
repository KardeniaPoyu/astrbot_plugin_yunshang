import unittest
from presentation import format_overview, is_campus_request

class PresentationTests(unittest.TestCase):
    def result(self):
        return {'community':'民大校园墙','cached':False,'posts':[
            {'id':str(i),'text':'同学分享图书馆使用体验。'*100,'truncated':False,
             'published_at':'2026-11-13 16:20:00','image_count':0} for i in range(3)]}

    def test_list_is_bounded_and_blank_line_separated(self):
        output=format_overview(self.result())
        self.assertLess(len(output),450)
        self.assertEqual(output.count('（节选）'),3)
        self.assertIn('\n\n2.',output)
        self.assertIn('/校园圈 详情',output)

    def test_short_post_is_not_marked_truncated(self):
        result=self.result()
        result['posts']=[{**result['posts'][0],'text':'找同学一起自习','image_count':1}]
        output=format_overview(result)
        self.assertNotIn('（节选）',output)
        self.assertIn('图片未识别',output)

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

if __name__=='__main__':unittest.main()
