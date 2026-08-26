"""
原文阅读数补抓模块（C 路径）
对 4 个稳定信源（IT之家、V2EX、未央网、小众软件）做页面爬取，
正则匹配"阅读/浏览/评论数"字段，写回 news.view_count。

设计要点：
  - 失败不重试：单条只请求 1 次，失败就标记 view_collected_at，避免风暴
  - 限流：每信源独立 sleep，V2EX 更慢（严格限流）
  - UA 伪装 + Referer 带 Google
  - 连续失败保护：同信源连续 5 次失败 → 暂停该信源 1 小时
  - URL 不匹配 → 直接标记已尝试，不浪费请求
  - 失败/为 0 → 走 D 路径（processor.py 内的 calc_local_hotness）

注意：VIEW_PARSERS 的正则为草案，正式上线前需用真实 URL 实测验证。
"""
import sqlite3
import re
import time
import random
from datetime import datetime, timedelta
import requests
from config import DB_PATH


# 各信源阅读数解析规则（草案，需实测验证后调整）
VIEW_PARSERS = {
    'IT之家': {
        'url_pattern': r'https?://www\.ithome\.com/.*?\.htm',
        'extractor': r'浏览[量数次]?\s*[:：]?\s*([\d,]+)',
        'sleep': (1, 2),
    },
    'V2EX': {
        'url_pattern': r'https?://www\.v2ex\.com/t/\d+',
        # V2EX 页面显示浏览数，HTML 中可能有 "view_count" JSON 或直接数字
        'extractor': r'浏览\s*([\d,]+)|"view_count"\s*:\s*(\d+)',
        'sleep': (2, 3),  # V2EX 严格限流，必须慢
    },
    '未央网': {
        'url_pattern': r'https?://weiyangx\.com/.*',
        # WordPress 通常用 wp-postviews 显示浏览数
        'extractor': r'浏览[量数次]?\s*[:：]?\s*([\d,]+)|"views"\s*[:：]\s*(\d+)',
        'sleep': (1, 2),
    },
    '小众软件': {
        'url_pattern': r'https?://www\.appinn\.com/.*',
        'extractor': r'浏览[量数次]?\s*[:：]?\s*([\d,]+)|"views"\s*[:：]\s*(\d+)',
        'sleep': (1, 2),
    },
}

# 模块级：记录各信源上次连续失败时间，做风控
_fail_streak = {}   # source -> 当前连续失败次数
_paused_until = {}   # source -> 暂停到何时（datetime）

# 连续失败多少次 → 暂停该信源
FAIL_STREAK_LIMIT = 5
PAUSE_MINUTES = 60

# 请求头伪装
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0 Safari/537.36',
    'Referer': 'https://www.google.com/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


def fetch_view_count(url, parser):
    """请求原文页面，正则提取阅读数。返回 int 或 None"""
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=8)
    resp.raise_for_status()
    m = re.search(parser['extractor'], resp.text)
    if not m:
        return None
    # 取第一个非空捕获组
    for g in m.groups():
        if g:
            return int(g.replace(',', ''))
    return None


def _is_paused(source):
    """检查某信源是否处于暂停期"""
    until = _paused_until.get(source)
    if until and datetime.now() < until:
        return True
    if until and datetime.now() >= until:
        # 暂停结束，清空
        _paused_until.pop(source, None)
        _fail_streak[source] = 0
    return False


def _record_failure(source):
    """记录一次失败，触发暂停则返回 True"""
    _fail_streak[source] = _fail_streak.get(source, 0) + 1
    if _fail_streak[source] >= FAIL_STREAK_LIMIT:
        _paused_until[source] = datetime.now() + timedelta(minutes=PAUSE_MINUTES)
        _fail_streak[source] = 0
        return True
    return False


def _record_success(source):
    """记录一次成功，清空失败计数"""
    _fail_streak[source] = 0


def enrich_view_counts(conn, limit=None):
    """对 C 信源补抓阅读数，写回 view_count 和 view_collected_at"""
    print('=' * 50)
    print(f'阅读数补抓开始 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 50)

    cursor = conn.cursor()
    sql = """
        SELECT id, url, source_name FROM news
        WHERE view_count IS NULL
          AND view_collected_at IS NULL
          AND source_name IN ('IT之家', 'V2EX', '未央网', '小众软件')
    """
    if limit:
        sql += f' LIMIT {limit}'
    cursor.execute(sql)
    rows = cursor.fetchall()

    if not rows:
        print('\n✓ 无待补抓的资讯。')
        return

    print(f'\n待补抓: {len(rows)} 条\n')

    success = 0
    fail = 0
    skipped = 0

    for i, (news_id, url, source) in enumerate(rows, 1):
        parser = VIEW_PARSERS.get(source)

        # 暂停期跳过
        if _is_paused(source):
            print(f'  [{i}/{len(rows)}] [{source}] 已暂停，跳过')
            skipped += 1
            continue

        # 无解析器或 URL 不匹配 → 标记已尝试，不浪费请求
        if not parser or not re.match(parser['url_pattern'], url):
            cursor.execute(
                "UPDATE news SET view_collected_at=? WHERE id=?",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), news_id))
            skipped += 1
            continue

        try:
            v = fetch_view_count(url, parser)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if v is not None and v > 0:
                cursor.execute(
                    "UPDATE news SET view_count=?, view_collected_at=? WHERE id=?",
                    (v, now, news_id))
                success += 1
                _record_success(source)
                print(f'  [{i}/{len(rows)}] [{source}] → {v}')
            else:
                # 解析失败或为 0：标记已尝试，下次不再爬
                cursor.execute(
                    "UPDATE news SET view_collected_at=? WHERE id=?",
                    (now, news_id))
                fail += 1
                if _record_failure(source):
                    print(f'  [{i}/{len(rows)}] [{source}] 连续失败已达上限，暂停 1 小时')
                else:
                    print(f'  [{i}/{len(rows)}] [{source}] 未匹配到阅读数')

            # 限流
            time.sleep(random.uniform(*parser['sleep']))

        except Exception as e:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "UPDATE news SET view_collected_at=? WHERE id=?",
                (now, news_id))
            fail += 1
            paused = _record_failure(source)
            msg = f'{source} 请求失败: {e}'
            if paused:
                msg += f' → 连续失败已达上限，暂停 {PAUSE_MINUTES} 分钟'
            print(f'  [{i}/{len(rows)}] [{msg}')

    conn.commit()
    print(f'\n{"=" * 50}')
    print(f'补抓完成：成功 {success} | 失败 {fail} | 跳过 {skipped}')
    print('=' * 50)


def run_enrich(limit=None):
    """CLI 入口"""
    conn = sqlite3.connect(DB_PATH)
    try:
        enrich_view_counts(conn, limit)
    finally:
        conn.close()


if __name__ == '__main__':
    run_enrich()
