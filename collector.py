"""
RSS 采集脚本
从配置的 RSS 信源获取资讯，解析后存入数据库
"""
import sqlite3
import socket
import feedparser
from datetime import datetime
from config import DB_PATH, RSS_SOURCES

# 设置全局 socket 超时（秒），防止某些信源卡死
socket.setdefaulttimeout(15)


def get_active_sources(conn):
    """从数据库获取活跃的 RSS 信源（扩展：返回 default_media_type 列）。
    为兼容旧库，若表中不存在 default_media_type 列则返回 None。"""
    cursor = conn.cursor()
    # 先探列名，做兼容（老库 sources 表可能还没有 default_media_type 列）
    cursor.execute("PRAGMA table_info(sources)")
    cols = {row[1] for row in cursor.fetchall()}
    if 'default_media_type' in cols:
        cursor.execute(
            'SELECT name, url, category, default_media_type FROM sources WHERE is_active = 1'
        )
        rows = cursor.fetchall()
        # 把 None 的元组位填成 None
        return [(r[0], r[1], r[2], r[3] if len(r) > 3 else None) for r in rows]
    cursor.execute('SELECT name, url, category FROM sources WHERE is_active = 1')
    return [(r[0], r[1], r[2], None) for r in cursor.fetchall()]


def parse_date(entry):
    """解析 RSS 条目的发布时间"""
    date_fields = ['published_parsed', 'updated_parsed', 'created_parsed']
    for field in date_fields:
        if hasattr(entry, field) and getattr(entry, field):
            try:
                t = getattr(entry, field)
                return datetime(*t[:6]).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_raw_html(entry):
    """从 entry 中取出带 HTML 的原始正文片段（用于提图和摘要）。
    返回字符串或空串。
    优先级：content (列表，最完整，含配图) → summary → description。
    注意：之前把 summary 放在最前，WordPress 类信源（爱范儿、少数派）只在 content 里放图，导致封面提取失败。"""
    # 注意：遍历顺序决定优先级，content 必须先于 summary/description
    for field in ['content', 'summary', 'description']:
        value = entry.get(field, '')
        if isinstance(value, list):
            if len(value) > 0 and 'value' in value[0]:
                v = value[0]['value']
                if v:
                    return v
            continue
        if value:
            return value
    return ''


def extract_cover_img(entry):
    """从 RSS 条目中抽取封面图 URL（优先级从高到低）：
      1. <enclosure type="image/…"> 或 <media:thumbnail>/<media:content>（RSS 标准扩展）
      2. feedparser 解析出的 image / media_content / media_thumbnail 结构
      3. 正文中第一张 <img src="…">
    返回 (cover_url, media_url)：
      - cover_url：适合做缩略图的 URL（一般为第一张）
      - media_url：高清原图/大图 URL（如果能解析出更高清的，否则同 cover_url）
    两个都可能为 None。
    """
    import re

    # === 1. enclosure（RSS 标准）===
    enclosure = getattr(entry, 'enclosures', None) or []
    for enc in enclosure:
        href = enc.get('href') or enc.get('url')
        enc_type = (enc.get('type') or '').lower()
        if not href:
            continue
        # 明确是图片型 enclosure
        if enc_type.startswith('image/') or re.search(r'\.(png|jpe?g|gif|webp|bmp|svg)(\?|$)', href, re.I):
            return href, href

    # === 2. media:thumbnail / media:content（RSS 媒体扩展，flickr/yt/apod 都用）===
    # feedparser 会挂到 entry.media_thumbnail、entry.media_content（可能是列表或字典）
    def _pick_media(obj):
        if not obj:
            return None
        if isinstance(obj, list) and obj:
            for item in obj:
                url = item.get('url') or item.get('href')
                if url:
                    return url
            return None
        if isinstance(obj, dict):
            return obj.get('url') or obj.get('href')
        return None

    thumb_url = _pick_media(getattr(entry, 'media_thumbnail', None))
    content_url = _pick_media(getattr(entry, 'media_content', None))
    if content_url:
        # media_content 通常是大图，media_thumbnail 是缩略图；两者都有时，cover 用缩略图，高清用 content
        cover = thumb_url or content_url
        return cover, content_url
    if thumb_url:
        return thumb_url, thumb_url

    # === 3. 正文 HTML 中第一张 <img> ===
    html = _get_raw_html(entry)
    if html:
        # 非贪婪匹配 <img ... src="..."> 或 <img ... src='...'>，同时兼容 data-src / data-original
        img_match = re.search(
            r'<img[^>]+?(?:data-src|data-original|src)\s*=\s*'
            r'["\']([^"\']+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?[^"\']*)?)["\']',
            html, re.I
        )
        if img_match:
            img_url = img_match.group(1).strip()
            return img_url, img_url
        # 兜底：任何非空 src（不要求后缀，一些站点用 CDN 路径无后缀）
        img_match2 = re.search(
            r'<img[^>]+?(?:data-src|data-original|src)\s*=\s*["\'](https?://[^"\']+)["\']',
            html, re.I
        )
        if img_match2:
            img_url = img_match2.group(1).strip()
            return img_url, img_url

    return None, None


def detect_media_type(entry, source_default=None):
    """判断内容类型：article / image / video。
    识别顺序：
      1. 显式 enclosure / media:content 的 type 字段含 video → video
      2. URL 域名（youtube/bilibili/vimeo/ted）→ video
      3. source_default 显式指定 → 用它
      4. 只有图片没有文字（summary 极短）→ image（暂不激进，留给 Step2/3 的专用源）
      5. 默认 article
    """
    import re

    # 1) enclosure / media_content 带 video type
    enclosure = getattr(entry, 'enclosures', None) or []
    for enc in enclosure:
        enc_type = (enc.get('type') or '').lower()
        if enc_type.startswith('video/'):
            return 'video'
    mc = getattr(entry, 'media_content', None)
    if isinstance(mc, list):
        for m in mc:
            mt = (m.get('type') or '').lower()
            if mt.startswith('video/'):
                return 'video'

    # 2) URL 暗示视频
    link = entry.get('link', '') or ''
    if re.search(r'(youtube\.com|youtu\.be|bilibili\.com|b23\.tv|vimeo\.com)', link, re.I):
        return 'video'
    # TED 官方播客 feed 里有 enclosure 视频
    if re.search(r'ted\.com/talks', link, re.I):
        return 'video'

    if source_default in ('image', 'video'):
        return source_default

    return 'article'


def extract_duration(entry):
    """从 RSS 媒体扩展中提取视频时长（秒），失败返回 None。"""
    import re
    mc = getattr(entry, 'media_content', None)
    if isinstance(mc, list):
        for m in mc:
            d = m.get('duration')
            if d is not None:
                try:
                    return int(d)
                except Exception:
                    return None
    # 兜底：<itunes:duration>00:10:30</itunes:duration> 的字符串
    for field in ['itunes_duration', 'duration']:
        d = entry.get(field)
        if not d:
            continue
        s = str(d).strip()
        if re.fullmatch(r'\d+', s):
            return int(s)
        parts = s.split(':')
        if len(parts) == 3:
            try:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            except Exception:
                return None
        if len(parts) == 2:
            try:
                return int(parts[0]) * 60 + int(parts[1])
            except Exception:
                return None
    return None


def get_summary(entry):
    """获取纯文本摘要（与旧版签名兼容，返回字符串）。"""
    import re
    value = _get_raw_html(entry)
    if not value:
        return ''
    # 去除 HTML 标签（简单处理）
    value = re.sub(r'<[^>]+>', '', str(value))
    value = value.strip()[:500]  # 限制长度
    return value or ''


def collect_from_source(source_name, source_url, source_category, source_default_media=None):
    """从单个 RSS 信源采集。
    source_default_media：信源默认的媒体类型（'image' / 'video'），未设置则自动检测。
    """
    print(f'  正在采集: {source_name} ...')
    try:
        feed = feedparser.parse(source_url)
        if feed.bozo and not feed.entries:
            print(f'    ✗ 解析失败: {feed.bozo_exception}')
            return []

        news_list = []
        cover_count = 0
        for entry in feed.entries:
            title = entry.get('title', '').strip()
            url = entry.get('link', '').strip()

            if not title or not url:
                continue

            summary = get_summary(entry)
            published_at = parse_date(entry)

            cover_img, media_url = extract_cover_img(entry)
            media_type = detect_media_type(entry, source_default_media)
            duration = extract_duration(entry) if media_type == 'video' else None

            if cover_img:
                cover_count += 1

            news_list.append({
                'title': title,
                'url': url,
                'summary': summary,
                'source_name': source_name,
                'published_at': published_at,
                'category': source_category,
                'cover_img': cover_img,
                'media_url': media_url,
                'media_type': media_type,
                'duration': duration,
            })

        print(f'    ✓ 采集到 {len(news_list)} 条资讯（含封面图 {cover_count} 条）')
        return news_list
    except Exception as e:
        print(f'    ✗ 采集异常: {e}')
        return []


def save_news(conn, news_list):
    """将资讯存入数据库（URL 唯一约束自动去重）"""
    cursor = conn.cursor()
    inserted = 0
    skipped = 0

    for news in news_list:
        try:
            cursor.execute('''
            INSERT INTO news (title, url, summary, source_name, published_at, category,
                              cover_img, media_type, media_url, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                news['title'],
                news['url'],
                news['summary'],
                news['source_name'],
                news['published_at'],
                news['category'],
                news.get('cover_img'),
                news.get('media_type', 'article'),
                news.get('media_url'),
                news.get('duration'),
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            # URL 已存在，跳过
            skipped += 1
        except Exception as e:
            print(f'    插入失败: {e}')
            skipped += 1

    conn.commit()
    print(f'    新增 {inserted} 条，跳过 {skipped} 条（已存在）')
    return inserted


def run_collect():
    """运行全部采集流程"""
    print('=' * 50)
    print(f'资讯采集开始 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 50)

    conn = sqlite3.connect(DB_PATH)

    # 获取活跃信源
    sources = get_active_sources(conn)
    if not sources:
        sources = [
            (s['name'], s['url'], s['category'], s.get('default_media_type'))
            for s in RSS_SOURCES if s.get('is_active', 1)
        ]
    print(f'\n共 {len(sources)} 个活跃信源\n')

    total_inserted = 0
    total_with_cover = 0
    for source in sources:
        if len(source) == 4:
            name, url, category, default_media = source
        else:
            name, url, category = source
            default_media = None
        news_list = collect_from_source(name, url, category, default_media)
        if news_list:
            inserted = save_news(conn, news_list)
            total_inserted += inserted
            total_with_cover += sum(1 for n in news_list if n.get('cover_img'))
        print()

    # 统计结果
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM news')
    total = cursor.fetchone()[0]

    cursor.execute('SELECT source_name, COUNT(*) FROM news GROUP BY source_name')
    by_source = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM news WHERE cover_img IS NOT NULL AND cover_img != ''")
    with_cover = cursor.fetchone()[0]

    conn.close()

    print('=' * 50)
    print(f'采集完成！本次新增: {total_inserted} 条')
    print(f'数据库总资讯数: {total} 条（含封面图 {with_cover} 条）')
    print(f'\n按信源统计:')
    for s in by_source:
        print(f'  {s[0]}: {s[1]} 条')
    print('=' * 50)


if __name__ == '__main__':
    run_collect()
