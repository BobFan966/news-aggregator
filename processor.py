"""
AI 处理模块
对采集到的资讯进行去重、AI 打分、分类
支持两种模式：
1. DeepSeek API 打分（需要配置 API Key）
2. 规则打分（关键词匹配 + 来源权重，无需 API Key）
"""
import sqlite3
import json
import re
import math
import requests
from datetime import datetime
from config import (
    DB_PATH, DEEPSEEK_API_KEY, DEEPSEEK_API_URL, CATEGORIES,
    FEATURED_THRESHOLD, RETENTION_DAYS_NORMAL, RETENTION_DAYS_FEATURED
)


# ==================== 热度维度（C+D 混合）====================

def calc_local_hotness(title, summary, published_at):
    """
    本地热度代理分（0-20）—— D 路径
    不依赖外部 API，用内容形态信号近似"原文热度"。
    与 importance 维度的高价值词刻意互斥，避免双重计分。

      - 新鲜度（0-8）：按 published_at 距今天数衰减
      - 标题党指数（0-5）：数字 / 事件动作动词 / 传播效果词
      - 数据密度（0-4）：标题+摘要中数字个数
      - 互动暗示（0-3）：含"热议/争议/刷屏/破圈/爆款/热搜/网友"等词
    """
    title = title or ''
    summary = summary or ''
    published_at = published_at or ''
    text = f'{title} {summary}'

    # === 1. 新鲜度（0-8）===
    freshness = 1  # 缺失/解析失败给最小正分，不惩罚
    if published_at:
        try:
            pub_dt = datetime.strptime(published_at[:19], '%Y-%m-%d %H:%M:%S')
            days = (datetime.now() - pub_dt).days
            if days <= 0:
                freshness = 8
            elif days <= 3:
                freshness = 6
            elif days <= 7:
                freshness = 4
            elif days <= 30:
                freshness = 2
            else:
                freshness = 1
        except Exception:
            freshness = 1

    # === 2. 标题党指数（0-5）===
    title_score = 0
    # 含数字（版本号/年份/金额等 → 有可量化信息）
    if re.search(r'\d', title):
        title_score += 2
    # 事件动作动词（与 importance 的高价值词互斥）
    event_verbs = {
        '上线', '推出', '启用', '关停', '下架', '更新', '升级',
        '降价', '涨价', '接入', '集成', '兼容', '适配',
        '停止服务', '停止维护', '闭源'
    }
    if any(v in title for v in event_verbs):
        title_score += 2
    # 传播效果词
    spread_words = {
        '首发', '独家', '爆料', '官宣', '刷屏', '破圈', '爆款',
        '热搜', '抢先', '首发评测', '首发开箱'
    }
    if any(w in title for w in spread_words):
        title_score += 2
    title_score = min(5, title_score)

    # === 3. 数据密度（0-4）===
    nums = len(re.findall(r'\d+', text))
    if nums >= 5:
        data_density = 4
    elif nums >= 3:
        data_density = 3
    elif nums >= 1:
        data_density = 2
    else:
        data_density = 0

    # === 4. 互动暗示（0-3）===
    interaction_words = {
        '热议', '争议', '刷屏', '破圈', '爆款', '热搜',
        '引发关注', '网友', '讨论', '围观'
    }
    interaction = 3 if any(w in text for w in interaction_words) else 0

    return min(20, freshness + title_score + data_density + interaction)


def calc_view_hotness(view_count):
    """
    真实阅读数 → 热度分（0-20）—— C 路径
    采用对数级分档，档内用 log10 微调（最多 +2）。
    view_count 为 0/None 时返回 None，由调用方走 D 路径。
    """
    if not view_count or view_count <= 0:
        return None  # 视为缺失，走 D

    if view_count >= 100000:
        base = 18
    elif view_count >= 10000:
        base = 15
    elif view_count >= 1000:
        base = 11
    elif view_count >= 100:
        base = 7
    else:
        base = 4

    # 档内对数微调
    adjust = min(2, int(math.log10(view_count + 1) * 0.5))
    return min(20, base + adjust)


def calc_hotness(view_count, title, summary, published_at):
    """
    热度维度统一入口（0-20）：
      - C 路径：有真实阅读数（view_count > 0）→ 用 calc_view_hotness
      - D 路径：缺失/为 0 → 用 calc_local_hotness 本地代理
    """
    v = calc_view_hotness(view_count)
    if v is not None:
        return v
    return calc_local_hotness(title, summary, published_at)


def get_unscored_news(conn, limit=None):
    """获取未打分的资讯（扩展：查询 cover_img / media_type / duration 等字段）"""
    cursor = conn.cursor()
    # 为兼容老库，先查列名；若列不存在就取 NULL
    cursor.execute("PRAGMA table_info(news)")
    cols = {row[1] for row in cursor.fetchall()}
    select_fields = ['id', 'title', 'summary', 'source_name', 'published_at', 'view_count']
    for extra in ['cover_img', 'media_type', 'duration']:
        if extra in cols:
            select_fields.append(extra)
        else:
            select_fields.append(f'NULL AS {extra}')
    query = f"SELECT {', '.join(select_fields)} FROM news WHERE score = 0"
    if limit:
        query += f' LIMIT {limit}'
    cursor.execute(query)
    return cursor.fetchall()


def score_with_rules(title, summary, source_name='', published_at='', view_count=None,
                     cover_img=None, media_type='article', duration=None):
    """
    规则打分（无 API Key 时使用）
    参考 AIHOT 经验：分维度独立打分，再用权重公式组合，精选判断由代码完成。

    打分维度（四维共 100 分）：
      - 重要性（0-50）：高/中价值关键词命中数 + 多重叠加奖励
      - 时效性/来源权威度（0-10）：来源权威度
      - 信息量（0-20）：摘要长度 + 标题长度 + 空内容惩罚 + 有封面图奖励
      - 热度（0-20）：C 路径用真实阅读数（calc_view_hotness），
                     缺失走 D 本地代理（calc_local_hotness）

    最终分层（让分数形成层次，避免"全员精选"）：
      90-100  顶尖精选：多重高价值信号 + 优质来源 + 信息量足 + 高热度
      80-89   精选：高价值词 ≥2 或多重叠加 + 优质来源
      60-79   值得看：中等价值命中
      40-59   普通：仅命中常规词
      0-39    低质量：无关键词 / 无摘要 / 标题过短
    """
    title = title or ''
    summary = summary or ''
    text = f'{title} {summary}'.lower()

    # 高价值关键词（重要性维度的核心信号，命中即重要）
    high_keywords = {
        'ai', '人工智能', '大模型', 'llm', 'gpt', 'deepseek', 'gemini', 'claude',
        '开源', 'release', '发布', '突破', '重大', '里程碑', '融资', '投资', '估值',
        '并购', '上市', '革命', '颠覆', '创新'
    }
    # 中价值关键词（次级信号）
    medium_keywords = {
        '框架', 'library', '工具', '技术', '产品', '行业', '算法', '模型',
        '编程', '开发', '部署', 'rust', 'python', 'javascript', 'go语言',
        '数据库', 'server', '云', 'docker', 'kubernetes', '微服务',
        '市场', '增长', '报告', '趋势', '架构', '性能', '安全'
    }

    # 分类关键词
    category_keywords = {
        '技术': ['代码', '编程', '框架', '库', '算法', 'ai', 'llm', '模型', '开发', '部署', '开源', '技术', 'language', 'python', 'javascript', 'server', '数据库', 'rust', '架构', '性能'],
        '产品': ['产品', '发布', '上线', '新功能', '体验', '设计', 'ui', '用户', '产品经理', 'feature', 'launch', 'release', '更新', '版本'],
        '行业': ['融资', '投资', '并购', '上市', '估值', '行业', '市场', '公司', '企业', '战略', '报告', '数据', '增长', '财报', '营收'],
        '学术': ['论文', '研究', '学术', '大学', '实验室', 'science', 'research', 'paper', 'arxiv', '博士', '教授', '实验']
    }

    # 来源权威度（时效性维度，整体 ×0.66 取整，腾出 5 分给 importance）
    source_authority = {
        'Hacker News': 9,
        '阮一峰博客': 8,
        'InfoQ中文站': 8,
        'V2EX': 6,
        '虎嗅': 6,
        'IT之家': 5,
        '爱范儿': 5,
        '极客公园': 5,
        '未央网': 4,
        '少数派': 4,
        '小众软件': 3,
    }

    high_matched = sum(1 for kw in high_keywords if kw in text)
    medium_matched = sum(1 for kw in medium_keywords if kw in text)

    # === 维度 1：重要性（0-50）===
    importance = high_matched * 7 + medium_matched * 3
    # 多重高价值叠加奖励（命中越多权重越高，是"精选"的关键门槛）
    if high_matched >= 4:
        importance += 10
    elif high_matched >= 3:
        importance += 7
    elif high_matched >= 2:
        importance += 4
    importance = min(importance, 50)

    # === 维度 2：时效性 / 来源权威度（0-10）===
    timeliness = source_authority.get(source_name, 4)
    timeliness = min(timeliness, 10)

    # === 维度 3：信息量（0-20）===
    info = 0
    if len(summary) > 20:
        info += 5
    if len(summary) > 100:
        info += 4
    if len(summary) > 200:
        info += 3
    if len(title) > 20:
        info += 3
    if len(title) > 40:
        info += 2
    # 视觉信息奖励：有封面图 +2；视频类内容 +3（时长大于 60 秒再加 +1）
    if cover_img:
        info += 2
    if media_type == 'video':
        info += 3
        if duration and duration >= 60:
            info += 1
    elif media_type == 'image':
        info += 2
    # 空内容惩罚（无摘要 / 标题过短 → 拉低信息量分）
    if not summary.strip():
        info -= 8
    # 视频标题通常比文字短，放宽阈值
    short_title_threshold = 5 if media_type == 'video' else 8
    if len(title) < short_title_threshold:
        info -= 5
    info = max(0, min(info, 20))

    # === 维度 4：热度（0-20，C+D 混合）===
    hotness = calc_hotness(view_count, title, summary, published_at)

    # === 总分（四维加权）===
    score = importance + timeliness + info + hotness

    # 强制降档：未命中任何关键词的内容不算精选，压到"普通"区间（45 以下）
    if high_matched == 0 and medium_matched == 0:
        score = min(score, 45)

    # 限制 0-100
    score = max(0, min(100, int(score)))

    # === 分类判断 ===
    category = '其他'
    max_count = 0
    for cat, keywords in category_keywords.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > max_count:
            max_count = count
            category = cat

    return score, category


def score_with_ai(title, summary):
    """
    使用 DeepSeek API 打分
    返回 (score, category)，失败返回 (50, '其他')
    """
    if not DEEPSEEK_API_KEY:
        return None  # 无 Key 时返回 None，由调用方回退规则打分

    prompt = f'''请对下面这条资讯进行打分和分类。
标题：{title}
摘要：{summary[:300] if summary else '无'}

要求返回 JSON 格式：
{{
  "score": 0-100的整数（越重要分数越高）,
  "category": "技术|产品|行业|学术|其他"（五选一）
}}

打分维度：重要性、时效性、信息量。
只返回 JSON，不要其他文字。'''

    try:
        headers = {
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json'
        }
        data = {
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'system', 'content': '你是一个资讯编辑，擅长判断资讯的重要性和分类。只返回JSON。'},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.3,
            'response_format': {'type': 'json_object'}
        }
        resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        content = result['choices'][0]['message']['content']

        # 解析 JSON
        parsed = json.loads(content)
        score = int(parsed.get('score', 50))
        category = parsed.get('category', '其他')

        # 校验
        if category not in CATEGORIES:
            category = '其他'
        score = max(0, min(100, score))

        return score, category
    except Exception as e:
        print(f'    AI 调用失败: {e}，回退规则打分')
        return None


def score_news(title, summary, source_name='', published_at='', view_count=None,
               cover_img=None, media_type='article', duration=None):
    """统一打分入口：优先 AI，失败或无 Key 用规则。
    AI 模式不感知封面/媒体字段，仍按纯内容判断；回退规则时应用媒体维度加成。"""
    # 先尝试 AI
    result = score_with_ai(title, summary)
    if result:
        return result
    # 回退规则
    return score_with_rules(title, summary, source_name, published_at, view_count,
                            cover_img=cover_img, media_type=media_type, duration=duration)


def update_news_score(conn, news_id, score, category):
    """更新资讯的打分和分类"""
    cursor = conn.cursor()
    cursor.execute('''
    UPDATE news SET score = ?, category = ? WHERE id = ?
    ''', (score, category, news_id))
    conn.commit()


def cleanup_old_news(conn=None):
    """
    清理老资讯（按评分分级保留）：
      - score <  FEATURED_THRESHOLD：保留 RETENTION_DAYS_NORMAL 天
      - score >= FEATURED_THRESHOLD：保留 RETENTION_DAYS_FEATURED 天

    日期以 published_at 为准，缺失时回退到 created_at；
    返回 (normal_deleted, featured_deleted) 两个删除计数。
    """
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 删除普通分（< 阈值）且超过保留期的资讯
    cursor.execute('''
        DELETE FROM news
        WHERE score < ?
          AND datetime(COALESCE(NULLIF(published_at, ''), created_at))
              < datetime('now', 'localtime', ?)
    ''', (FEATURED_THRESHOLD, f'-{RETENTION_DAYS_NORMAL} days'))
    normal_deleted = cursor.rowcount

    # 删除高分（>= 阈值）且超过更长保留期的资讯
    cursor.execute('''
        DELETE FROM news
        WHERE score >= ?
          AND datetime(COALESCE(NULLIF(published_at, ''), created_at))
              < datetime('now', 'localtime', ?)
    ''', (FEATURED_THRESHOLD, f'-{RETENTION_DAYS_FEATURED} days'))
    featured_deleted = cursor.rowcount

    conn.commit()
    if own_conn:
        conn.close()

    return normal_deleted, featured_deleted


def run_process(limit=None):
    """批量处理未打分的资讯"""
    print('=' * 50)
    print(f'AI 打分处理开始 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    mode = 'AI API' if DEEPSEEK_API_KEY else '规则模式（无 API Key）'
    print(f'打分模式: {mode}')
    print('=' * 50)

    conn = sqlite3.connect(DB_PATH)

    # 获取未打分资讯
    news_list = get_unscored_news(conn, limit)
    if not news_list:
        print('\n✓ 所有资讯均已打分，无需处理。')
        conn.close()
        return

    print(f'\n待处理资讯数: {len(news_list)} 条\n')

    processed = 0
    for i, news in enumerate(news_list, 1):
        # 字段顺序：id, title, summary, source_name, published_at, view_count, cover_img, media_type, duration
        news_id, title, summary, source_name, published_at, view_count, \
            cover_img, media_type, duration = news
        short_title = title[:40] + ('...' if len(title) > 40 else '')

        print(f'  [{i}/{len(news_list)}] {short_title}')
        print(f'      来源: {source_name} | 阅读数: {view_count if view_count else "(无)"} '
              f'| 媒体: {media_type or "article"}{(" · 封面" if cover_img else "")}'
              f'{(" · " + str(duration) + "s") if duration else ""}')

        score, category = score_news(
            title, summary or '', source_name, published_at or '', view_count,
            cover_img=cover_img, media_type=media_type or 'article', duration=duration
        )
        update_news_score(conn, news_id, score, category)

        print(f'      → 打分: {score} | 分类: {category}')
        processed += 1

    # 统计结果
    cursor = conn.cursor()
    cursor.execute('SELECT score, COUNT(*) FROM news GROUP BY score >= 80')
    high_quality = cursor.fetchall()

    cursor.execute('SELECT category, COUNT(*) FROM news GROUP BY category')
    by_category = cursor.fetchall()

    cursor.execute('SELECT AVG(score) FROM news WHERE score > 0')
    avg_score = cursor.fetchone()[0] or 0

    conn.close()

    print(f'\n{"=" * 50}')
    print(f'处理完成！共打分: {processed} 条')
    print(f'平均分: {avg_score:.1f}')
    print(f'\n按分类统计:')
    for c in by_category:
        print(f'  {c[0]}: {c[1]} 条')
    print('=' * 50)


if __name__ == '__main__':
    # 处理所有未打分的资讯
    run_process()
