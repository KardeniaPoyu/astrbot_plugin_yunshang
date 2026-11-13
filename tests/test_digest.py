import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from digest import build_digest, build_prompt, candidates, digest_as_text

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 12, 8, tzinfo=TZ)
END = START + timedelta(hours=4)
POST = {'id':'42','text':'有同学想周末一起去图书馆吗？','published_at':'2026-11-12 10:20:00','category':'互助','image_count':0,'truncated':False}

class DigestTests(unittest.TestCase):
    def collection(self, posts=None):
        rows = [dict(POST)] if posts is None else posts
        return {'posts':rows,'total':len(rows),'scan_limited':False,'invalid_dates':0}

    def test_bad_json_uses_original_excerpt(self):
        d=build_digest(self.collection(),START,END,ai_reply='not JSON')
        self.assertEqual(d['mode'],'excerpt')
        self.assertEqual(d['items'][0]['summary'],POST['text'])

    def test_unknown_ids_and_duplicate_citations_are_not_used(self):
        reply={'intro':'今日互助','items':[{'id':'evil','title':'编造','summary':'编造'},
              {'id':'42','title':'图书馆结伴','summary':'有同学询问周末结伴去图书馆。'},
              {'id':'42','title':'重复','summary':'重复'}]}
        d=build_digest(self.collection(),START,END,ai_reply=json.dumps(reply))
        self.assertEqual(d['mode'],'ai')
        self.assertEqual([p['id'] for p in d['items']],['42'])

    def test_image_only_ai_claim_is_replaced_with_explicit_unknown(self):
        post={**POST,'text':'','image_count':2}
        d=build_digest(self.collection([post]),START,END,ai_reply=json.dumps({'items':[{'id':'42','title':'猜图','summary':'图片里有猫。'}]}))
        self.assertEqual(d['mode'],'excerpt')
        self.assertEqual(d['items'][0]['summary'],'这条投稿以配图为主。')

    def test_redacts_model_contact_information(self):
        d=build_digest(self.collection(),START,END,ai_reply=json.dumps({'items':[{'id':'42','title':'电话13800138000','summary':'QQ：123456789'}]}))
        rendered=digest_as_text(d)
        self.assertNotIn('13800138000',rendered)
        self.assertNotIn('123456789',rendered)

    def test_collection_count_and_partial_scan_are_honest(self):
        c=self.collection([{**POST,'id':str(i),'text':POST['text'] + str(i)} for i in range(12)])
        c['scan_limited']=True
        d=build_digest(c,START,END)
        self.assertEqual((d['total'],d['shown']),(12,4))
        self.assertIn('不代表完整',digest_as_text(d))
        self.assertEqual(digest_as_text(d,page=0).count('有同学想周末'),8) # title plus summary on 4 items
        self.assertNotIn('有同学想周末',digest_as_text(d,page=1))

    def test_prompt_has_bounded_sources_and_text(self):
        p=build_prompt([{**POST,'id':str(i),'text':'字'*3000 + str(i)} for i in range(200)])
        rows=json.loads(p)['posts']
        self.assertEqual(len(rows),4)
        self.assertEqual(sum(len(x['text']) for x in rows),4800)

    def test_empty_and_nonobject_reply_fall_back_safely(self):
        for response in ('[]','null','{"items":null}','{"items":[null]}'):
            self.assertEqual(build_digest(self.collection(),START,END,ai_reply=response)['mode'],'excerpt')
        self.assertEqual(build_digest(self.collection([]),START,END)['shown'],0)

    def test_prompt_marks_selected_post_truncation_without_mutating_sources(self):
        posts = [{**POST, 'id': str(i), 'text': '字' * 1500 + str(i)} for i in range(14)]
        rows = json.loads(build_prompt(posts))['posts']
        self.assertEqual(len(rows[0]['text']), 1200)
        self.assertTrue(rows[0]['truncated'])
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[-1]['text']), 1200)
        self.assertTrue(rows[-1]['truncated'])
        self.assertFalse(posts[0]['truncated'])
        self.assertEqual(len(posts[0]['text']), 1501)

    def test_high_likes_beyond_first_twenty_four_win_before_model_budget(self):
        posts = [{**POST, 'id': str(i), 'text': f'投稿{i}。' + '字' * 2000,
                  'like_count': i, 'trace_count': 0, 'comment_count': 0}
                 for i in range(40)]
        before = copy.deepcopy(posts)
        rows = json.loads(build_prompt(posts))['posts']
        self.assertEqual([row['id'] for row in rows], ['39', '38', '37', '36'])
        self.assertEqual([row['like_count'] for row in rows], [39, 38, 37, 36])
        self.assertTrue(all(row['trace_count'] == row['comment_count'] == 0 for row in rows))
        self.assertTrue(all(len(row['text']) == 1200 and row['truncated'] for row in rows))
        for limit in (4, 8, 999):
            digest = build_digest(self.collection(posts), START, END, max_items=limit)
            self.assertEqual([row['id'] for row in digest['items']], ['39', '38', '37', '36'])
            self.assertEqual((digest['ranking'], digest['shown'], digest['total']), ('likes', 4, 40))
        self.assertEqual(posts, before)

    def test_rank_priority_likes_traces_comments_then_actual_instant(self):
        posts = [
            {**POST, 'id': 'newest', 'text': '最新投稿', 'like_count': 10,
             'trace_count': 3, 'comment_count': 2, 'published_at': '2026-11-12T03:00:00Z'},
            {**POST, 'id': 'comments', 'text': '评论更多', 'like_count': 10,
             'trace_count': 3, 'comment_count': 3, 'published_at': '2026-11-12 09:00:00'},
            {**POST, 'id': 'traces', 'text': '蹲蹲更多', 'like_count': 10,
             'trace_count': 4, 'comment_count': 0, 'published_at': '2026-11-12 08:00:00'},
            {**POST, 'id': 'likes', 'text': '点赞更多', 'like_count': 11,
             'trace_count': 0, 'comment_count': 0, 'published_at': '2026-11-12 08:00:00'},
            {**POST, 'id': 'old', 'text': '看似钟点更晚', 'like_count': 10,
             'trace_count': 3, 'comment_count': 2, 'published_at': '2026-11-12T10:30:00+08:00'},
        ]
        digest = build_digest(self.collection(posts), START, END)
        self.assertEqual([row['id'] for row in digest['items']], ['likes', 'traces', 'comments', 'newest'])

    def test_known_zero_precedes_unknown_and_full_ties_preserve_source_order(self):
        posts = [
            {**POST, 'id': 'unknown', 'text': '未知点赞', 'like_count': None, 'trace_count': 900},
            {**POST, 'id': 'trace-unknown', 'text': '未知蹲蹲', 'like_count': 0, 'trace_count': None},
            {**POST, 'id': 'comment-unknown', 'text': '未知评论', 'like_count': 0,
             'trace_count': 0, 'comment_count': None},
            {**POST, 'id': 'tie-first', 'text': '稳定排序甲', 'like_count': 0,
             'trace_count': 0, 'comment_count': 0, 'published_at': '2026-11-12 10:20:00'},
            {**POST, 'id': 'tie-second', 'text': '稳定排序乙', 'like_count': 0,
             'trace_count': 0, 'comment_count': 0, 'published_at': '2026-11-12T02:20:00Z'},
        ]
        self.assertEqual([row['id'] for row in candidates(posts)],
                         ['tie-first', 'tie-second', 'comment-unknown', 'trace-unknown'])
        self.assertEqual([row['id'] for row in candidates(posts[:2])], ['trace-unknown', 'unknown'])

    def test_invalid_timestamp_ranks_last_without_reordering_equal_unknown_dates(self):
        posts = [{**POST, 'id': 'unknown-first', 'text': '未知时间甲', 'published_at': '原文未提供时间'},
                 {**POST, 'id': 'unknown-second', 'text': '未知时间乙', 'published_at': None},
                 {**POST, 'id': 'known', 'text': '已知时间'}]
        self.assertEqual([row['id'] for row in candidates(posts)], ['known', 'unknown-first', 'unknown-second'])

    def test_exact_text_and_images_dedupe_keeps_best_and_image_only_posts_stay_distinct(self):
        image_a = 'https://ysxy.img.qimiaoyuanfen.com/a.jpg'
        image_b = 'https://ysxy.img.qimiaoyuanfen.com/b.jpg'
        posts = [
            {**POST, 'id': 'duplicate-low', 'like_count': 1, 'image_urls': [image_a]},
            {**POST, 'id': 'duplicate-best', 'like_count': 100, 'image_urls': [image_a]},
            {**POST, 'id': 'different-image', 'like_count': 90, 'image_urls': [image_b]},
            {**POST, 'id': 'image-only-a', 'like_count': 80, 'text': '', 'image_count': 1, 'image_urls': [image_a]},
            {**POST, 'id': 'image-only-b', 'like_count': 70, 'text': '', 'image_count': 1, 'image_urls': [image_b]},
            {**POST, 'id': 'empty', 'like_count': 1000, 'text': '', 'image_count': 0},
        ]
        digest = build_digest(self.collection(posts), START, END)
        self.assertEqual([row['id'] for row in digest['items']],
                         ['duplicate-best', 'different-image', 'image-only-a', 'image-only-b'])
        self.assertEqual((digest['total'], digest['shown']), (6, 4))
        self.assertEqual(len(candidates([{**POST, 'id': 'low', 'like_count': 1},
                                         {**POST, 'id': 'high', 'like_count': 2}])), 1)

    def test_deduplication_uses_full_exact_text_before_truncation(self):
        posts = [{**POST, 'id': str(i), 'text': '字' * 1500 + str(i)} for i in range(5)]
        self.assertEqual([row['id'] for row in candidates(posts)], ['0', '1', '2', '3'])

    def test_selected_snapshots_do_not_alias_source_and_prompt_keeps_urls_internal(self):
        posts = [{**POST, 'like_count': 12, 'trace_count': 3, 'comment_count': 4,
                  'image_urls': ['https://ysxy.img.qimiaoyuanfen.com/private.jpg'],
                  'thumbnail_urls': ['https://ysxy.img.qimiaoyuanfen.com/private-thumb.jpg']}]
        before = copy.deepcopy(posts)
        selected = candidates(posts)
        digest = build_digest(self.collection(posts), START, END)
        prompt = build_prompt(posts)
        self.assertNotIn('qimiaoyuanfen', prompt)
        self.assertNotIn('image_urls', prompt)
        self.assertNotIn('thumbnail_urls', prompt)
        self.assertEqual(posts, before)
        selected[0]['image_urls'].append('new')
        digest['items'][0]['thumbnail_urls'].clear()
        self.assertEqual(posts, before)

    def test_ai_cannot_reorder_selected_ids_or_add_lower_ranked_posts(self):
        posts = [{**POST, 'id': str(i), 'text': f'同学{i}想找图书馆搭子。',
                  'like_count': i} for i in range(6)]
        reply = {'items': [{'id': str(i), 'title': '图书馆搭子',
                            'summary': f'同学{i}询问图书馆结伴。', 'like_count': 999}
                           for i in range(6)]}
        digest = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply))
        self.assertEqual([row['id'] for row in digest['items']], ['5', '4', '3', '2'])
        self.assertEqual([row['like_count'] for row in digest['items']], [5, 4, 3, 2])
        self.assertEqual(digest['mode'], 'ai')
        self.assertTrue(all('询问图书馆结伴' in row['summary'] for row in digest['items']))

    def test_missing_and_invalid_ai_rows_fall_back_per_selected_id_without_substitution(self):
        posts = [{**POST, 'id': str(i), 'text': f'同学{i}想找图书馆搭子。',
                  'like_count': i} for i in range(6)]
        reply = {'items': [
            {'id': '0', 'title': '低赞帖子', 'summary': '同学0询问图书馆结伴。'},
            {'id': '2', 'title': '图书馆搭子', 'summary': '同学2询问图书馆结伴。'},
            {'id': '4', 'title': '官方通知', 'summary': '领取500元。'},
            {'id': '3', 'title': None, 'summary': '不合法'},
        ]}
        digest = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply))
        self.assertEqual([row['id'] for row in digest['items']], ['5', '4', '3', '2'])
        self.assertEqual([row['summary'] for row in digest['items'][:3]],
                         [posts[5]['text'], posts[4]['text'], posts[3]['text']])
        self.assertEqual(digest['items'][3]['summary'], '同学2询问图书馆结伴。')
        self.assertEqual(digest['mode'], 'ai')
        capped = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply), max_items=2)
        self.assertEqual([row['id'] for row in capped['items']], ['5', '4'])
        self.assertEqual(capped['mode'], 'excerpt')

    def test_text_displays_only_known_likes_and_traces(self):
        posts = [{**POST, 'id': 'known', 'text': '已知互动', 'like_count': 12, 'trace_count': 0},
                 {**POST, 'id': 'trace', 'text': '仅蹲蹲已知', 'like_count': None, 'trace_count': 3},
                 {**POST, 'id': 'unknown', 'text': '未知互动', 'like_count': None, 'trace_count': None}]
        digest = build_digest(self.collection(posts), START, END)
        output = digest_as_text(digest)
        self.assertIn('赞12 · 蹲蹲0', output)
        self.assertIn('蹲蹲3', output)
        self.assertNotIn('赞None', output)
        self.assertNotIn('蹲蹲None', output)
        unknown = next(block for block in output.split('\n\n') if block.startswith('#unknown '))
        self.assertNotIn('赞', unknown)
        self.assertNotIn('蹲蹲', unknown)
        self.assertNotIn('qimiaoyuanfen', output)

    def test_prompt_preserves_existing_truncation_and_complete_boundary(self):
        rows = json.loads(build_prompt([
            {**POST, 'text': '字' * 1200},
            {**POST, 'id': '43', 'text': '短摘录', 'truncated': True},
        ]))['posts']
        self.assertFalse(rows[0]['truncated'])
        self.assertTrue(rows[1]['truncated'])

    def test_deeply_nested_model_json_uses_excerpt(self):
        reply = '[' * 1100 + '0' + ']' * 1100
        digest = build_digest(self.collection(), START, END, ai_reply=reply)
        self.assertEqual(digest['mode'], 'excerpt')
        self.assertEqual(digest['items'][0]['summary'], POST['text'])

    def test_long_unrecognized_numeral_run_uses_excerpt(self):
        reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': '一' * 15000 + '字'}]}
        digest = build_digest(self.collection(), START, END,
                              ai_reply=json.dumps(reply, ensure_ascii=False))
        self.assertEqual(digest['mode'], 'excerpt')
        self.assertEqual(digest['items'][0]['summary'], POST['text'])

    def test_oversized_model_fields_fall_back_before_grounding(self):
        for field, size in (('title', 129), ('summary', 601)):
            with self.subTest(field=field):
                row = {'id': '42', 'title': '图书馆结伴', 'summary': '同学询问图书馆结伴。'}
                row[field] = '一' * size
                with patch('digest._grounded', side_effect=AssertionError('oversized input reached grounding')):
                    digest = build_digest(self.collection(), START, END,
                                          ai_reply=json.dumps({'items': [row]}, ensure_ascii=False))
                self.assertEqual(digest['mode'], 'excerpt')
                self.assertEqual(digest['items'][0]['summary'], POST['text'])

    def test_unsupported_numbers_dates_and_amounts_use_excerpt(self):
        for claim in ('有500人报名。', '报名费50元。', '报名费五百元。',
                      '领取￥500补助。', '领取0.5万元。', '优惠10%。',
                      '11月13日集合。', '2026-11-13集合。', '下午三点集合。',
                      '明天集合。', '下周三集合。', '有５０人报名。'):
            with self.subTest(claim=claim):
                reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': claim}]}
                digest = build_digest(self.collection(), START, END, ai_reply=json.dumps(reply))
                self.assertEqual(digest['mode'], 'excerpt')
                self.assertEqual(digest['items'][0]['summary'], POST['text'])

    def test_numbers_cannot_be_recombined_into_new_dates_or_payment_amounts(self):
        post = {**POST, 'text': '11月12日和12月13日各有50人报名，费用5元。'}
        for claim in ('11月13日集合。', '报名费50元。', '有5人报名。', '报名费500元。'):
            with self.subTest(claim=claim):
                reply = {'items': [{'id': '42', 'title': '报名信息', 'summary': claim}]}
                digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
                self.assertEqual(digest['mode'], 'excerpt')
                self.assertEqual(digest['items'][0]['summary'], post['text'])

    def test_grounding_does_not_borrow_another_post_or_publication_date(self):
        posts = [dict(POST), {**POST, 'id': '43', 'text': '11月13日报名费50元。'}]
        for claim in ('11月13日报名费50元。', '2026-11-12集合。'):
            with self.subTest(claim=claim):
                reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': claim}]}
                digest = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply))
                self.assertEqual(digest['items'][0]['summary'], POST['text'])
                self.assertEqual(digest['mode'], 'excerpt')

    def test_engagement_counts_in_prompt_do_not_ground_new_body_facts(self):
        post = {**POST, 'like_count': 500, 'trace_count': 50, 'comment_count': 13}
        row = json.loads(build_prompt([post]))['posts'][0]
        self.assertEqual((row['like_count'], row['trace_count'], row['comment_count']), (500, 50, 13))
        for claim in ('已有500人报名。', '报名费50元。', '招募13人。', '获赞500。'):
            with self.subTest(claim=claim):
                reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': claim}]}
                digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
                self.assertEqual(digest['mode'], 'excerpt')
                self.assertEqual(digest['items'][0]['summary'], POST['text'])
                self.assertIn('赞500 · 蹲蹲50', digest_as_text(digest))

    def test_supported_numbers_dates_amounts_and_paraphrases_remain_ai(self):
        post = {**POST, 'text': '有同学想11月13日14:30一起去图书馆吗？招募3人，费用50元。'}
        summary = '同学询问11月13日14:30图书馆结伴，招募3人，费用50元。'
        reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': summary}]}
        digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
        self.assertEqual(digest['mode'], 'ai')
        self.assertEqual(digest['items'][0]['summary'], summary)

    def test_new_official_or_confirmed_assertions_in_title_or_summary_use_excerpt(self):
        for claim in ('官方通知', '校方已确认', '学校已发布通知', '学校宣布',
                      '已经证实', '确定关闭', '消息属实', '已实锤', 'Official announcement',
                      'Confirmed closure'):
            for field in ('title', 'summary'):
                with self.subTest(claim=claim, field=field):
                    row = {'id': '42', 'title': '图书馆结伴', 'summary': '同学询问图书馆结伴。'}
                    row[field] = claim
                    digest = build_digest(self.collection(), START, END,
                                          ai_reply=json.dumps({'items': [row]}))
                    self.assertEqual(digest['mode'], 'excerpt')
                    self.assertEqual(digest['items'][0]['summary'], POST['text'])

    def test_uncertain_source_does_not_become_confirmed_assertion(self):
        post = {**POST, 'text': '听说图书馆关闭，官方通知是否属实？尚未确认。'}
        for claim in ('官方通知属实，图书馆关闭。', '已确认图书馆关闭。'):
            with self.subTest(claim=claim):
                reply = {'items': [{'id': '42', 'title': '图书馆消息', 'summary': claim}]}
                digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
                self.assertEqual(digest['mode'], 'excerpt')
                self.assertEqual(digest['items'][0]['summary'], post['text'])
        summary = '同学询问官方通知是否属实，尚未确认图书馆关闭。'
        reply = {'items': [{'id': '42', 'title': '图书馆消息', 'summary': summary}]}
        digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
        self.assertEqual(digest['mode'], 'ai')
        self.assertEqual(digest['items'][0]['summary'], summary)

    def test_official_terms_already_present_in_source_are_allowed(self):
        post = {**POST, 'text': '转发官方通知：学校已确认图书馆周末开放。'}
        summary = '同学转发官方通知，学校已确认图书馆周末开放。'
        reply = {'items': [{'id': '42', 'title': '同学转发图书馆消息', 'summary': summary}]}
        digest = build_digest(self.collection([post]), START, END, ai_reply=json.dumps(reply))
        self.assertEqual(digest['mode'], 'ai')
        self.assertEqual(digest['items'][0]['summary'], summary)

    def test_invalid_rows_keep_their_excerpts_alongside_valid_ai_rows(self):
        posts = [dict(POST), {**POST, 'id': '43', 'text': '有没有同学想一起打羽毛球？'},
                 {**POST, 'id': '44', 'text': '', 'image_count': 1},
                 {**POST, 'id': '45', 'text': '有人一起吃饭吗？'}]
        reply = {'items': [
            {'id': '42', 'title': '官方通知', 'summary': '领取500元。'},
            {'id': '43', 'title': '羽毛球结伴', 'summary': '同学询问打羽毛球的伙伴。'},
            {'id': '44', 'title': '猜图', 'summary': '图片里是食堂。'},
            {'id': '45', 'title': None, 'summary': '不合法标题'},
            {'id': '42', 'title': '重复', 'summary': '重复'},
            {'id': 'unknown', 'title': '未知来源', 'summary': '未知来源'},
        ]}
        digest = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply))
        self.assertEqual(digest['mode'], 'ai')
        self.assertEqual([p['id'] for p in digest['items']], ['42', '43', '44', '45'])
        self.assertEqual(digest['items'][0]['summary'], POST['text'])
        self.assertEqual(digest['items'][1]['summary'], '同学询问打羽毛球的伙伴。')
        self.assertEqual(digest['items'][2]['summary'],'这条投稿以配图为主。')
        self.assertEqual(digest['items'][3]['summary'], posts[3]['text'])
        self.assertEqual((digest['total'], digest['shown']), (4, 4))
        self.assertTrue(all('text' not in item for item in digest['items']))
        capped = build_digest(self.collection(posts), START, END, ai_reply=json.dumps(reply), max_items=2)
        self.assertEqual([p['id'] for p in capped['items']], ['42', '43'])

    def test_model_intro_cannot_add_facts_even_when_items_are_valid(self):
        reply = {'intro': '学校已宣布明日停课，每人领取500元。',
                 'items': [{'id': '42', 'title': '图书馆结伴', 'summary': '同学询问周末图书馆结伴。'}]}
        digest = build_digest(self.collection(), START, END, ai_reply=json.dumps(reply))
        self.assertEqual(digest['mode'], 'ai')
        self.assertTrue(digest['intro'])
        for unsupported in ('学校', '明日', '停课', '500'):
            self.assertNotIn(unsupported, digest['intro'])

    def test_digest_has_no_fixed_disclaimer(self):
        digest=build_digest(self.collection(),START,END)
        self.assertNotIn('未经核实',digest['intro'])
        self.assertNotIn('未经核实',digest_as_text(digest))
        reply = {'items': [{'id': '42', 'title': '图书馆结伴', 'summary': '同学询问周末图书馆结伴。'}]}
        digest = build_digest(self.collection(), START, END, ai_reply=json.dumps(reply))
        self.assertNotIn('仅供参考', digest_as_text(digest))

if __name__=='__main__':unittest.main()
