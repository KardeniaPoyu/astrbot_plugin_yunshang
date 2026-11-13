"""Chat presentation; independent of the scheduled digest pipeline."""
import re
import copy

CHAT_GUIDANCE = '''

【校园墙回复呈现】仅在本轮查询校园墙时适用：先回答用户关心的事，同一事件的多条投稿合并说。
默认挑2–3个重点，每个重点1–2句，分成2–3个短段，段间必须留一个空行；通常120–220字，简单问题可以更短。
不要写报告式大标题、长铺垫或“总之”结语，不逐条复述全部资料，不把多个话题用逗号塞在同一长句里。
沿用当前人格的自然口吻和既有口癖，不因调用工具切换成公告腔。事实与吐槽分清；投稿、爆料只是发帖人的说法，不说成已查明的事实。
用户明确要求详细解释、原文、更多条目时，按要求展开并分段，不套用上述短摘要篇幅。
不附加“同学投稿内容未经核实”等固定免责声明，不复述工具内部的notice；按语境自然交代是谁的说法。
这些是呈现要求，不是要复述给用户的内容。'''


def is_campus_request(text):
    return bool(re.search(r'校园墙|校园圈|校友圈|民大表白墙', str(text or '')))


def format_counts(post):
    """Compact engagement labels; unknown is distinct from a known zero."""
    return ' · '.join(f'{label}{post[key]}'
                      for key, label in (('like_count', '赞'), ('trace_count', '蹲蹲'))
                      if type(post.get(key)) is int and post[key] >= 0)


def format_overview(result):
    posts = result['posts']
    lines = [f"{result['community']} · {len(posts)}条动态"]
    for index, post in enumerate(posts, 1):
        body = re.sub(r'\s+', ' ', post.get('text') or '').strip()
        shortened = len(body) > 90 or post.get('truncated', False)
        if len(body) > 90:
            body = body[:90].rstrip() + '…'
        body = body or '（正文为空）'
        if shortened:
            body += '（节选）'
        date = str(post.get('published_at') or '')[:16].replace('T', ' ')
        note = f" · 配图{post['image_count']}张" if post.get('image_count') else ''
        counts = format_counts(post)
        if counts:
            note = f' · {counts}' + note
        lines.append(f"{index}. {body}\n#{post['id']} · {date}{note}")
    lines.append('看原文：/校园圈 详情 帖子ID')
    return '\n\n'.join(lines)


def public_query_result(result):
    """The model gets source text and IDs, not long CDN links or local paths."""
    result = copy.deepcopy(result)
    for post in result.get('posts', []):
        for key in ('image_urls', 'thumbnail_urls', 'image_path'):
            post.pop(key, None)
    return result
