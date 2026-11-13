"""Chat presentation; independent of the scheduled digest pipeline."""
import re

CHAT_GUIDANCE = '''

【校园墙回复呈现】仅在本轮查询校园墙时适用：先回答用户关心的事，同一事件的多条投稿合并说。
默认挑2–3个重点，每个重点1–2句，分成2–3个短段，段间必须留一个空行；通常120–220字，简单问题可以更短。
不要写报告式大标题、长铺垫或“总之”结语，不逐条复述全部资料，不把多个话题用逗号塞在同一长句里。
沿用当前人格的自然口吻和既有口癖，不因调用工具切换成公告腔。事实与吐槽分清；投稿、爆料只是发帖人的说法，不说成已查明的事实。
用户明确要求详细解释、原文、更多条目时，按要求展开并分段，不套用上述短摘要篇幅。
这些是呈现要求，不是要复述给用户的内容。'''


def is_campus_request(text):
    return bool(re.search(r'校园墙|校园圈|校友圈|民大表白墙', str(text or '')))


def format_overview(result):
    posts = result['posts']
    lines = [f"{result['community']} · {len(posts)}条投稿，未经核实"]
    for index, post in enumerate(posts, 1):
        body = re.sub(r'\s+', ' ', post.get('text') or '').strip()
        shortened = len(body) > 90 or post.get('truncated', False)
        if len(body) > 90:
            body = body[:90].rstrip() + '…'
        body = body or '（正文为空）'
        if shortened:
            body += '（节选）'
        date = str(post.get('published_at') or '')[:16].replace('T', ' ')
        note = ' · 图片未识别' if post.get('image_count') else ''
        lines.append(f"{index}. {body}\n#{post['id']} · {date}{note}")
    lines.append('看原文：/校园圈 详情 帖子ID')
    return '\n\n'.join(lines)
