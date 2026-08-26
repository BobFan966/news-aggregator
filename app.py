"""
Flask Web 服务
提供资讯聚合系统的后端 API 接口
"""
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from config import DB_PATH, FEATURED_THRESHOLD, AUTO_COLLECT_ENABLED, AUTO_COLLECT_CRON

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)  # 允许跨域


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让返回结果可以通过字段名访问
    return conn


def success_response(data, msg='ok'):
    """统一成功响应格式"""
    return jsonify({
        'code': 200,
        'msg': msg,
        'data': data
    })


def error_response(msg, code=500):
    """统一错误响应格式"""
    return jsonify({
        'code': code,
        'msg': msg,
        'data': None
    }), code


# ==================== 资讯相关接口 ====================

@app.route('/api/news', methods=['GET'])
def get_news():
    """
    获取资讯列表
    参数:
      - category: 可选，按分类筛选
      - limit: 默认 50
      - source: 可选，按来源筛选
      - min_score: 可选，最低分数
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # 构建查询
        query = '''
        SELECT id, title, url, summary, source_name, score, category, published_at, is_read,
               cover_img, media_type, media_url, duration
        FROM news
        WHERE 1=1
        '''
        params = []

        category = request.args.get('category')
        if category and category != '全部':
            if category == '精选':
                # 精选：评分 >= 80
                query += ' AND score >= 80'
            else:
                query += ' AND category = ?'
                params.append(category)

        source = request.args.get('source')
        if source:
            query += ' AND source_name = ?'
            params.append(source)

        min_score = request.args.get('min_score', type=int)
        if min_score:
            query += ' AND score >= ?'
            params.append(min_score)

        media_type = request.args.get('media_type')
        if media_type and media_type != 'all':
            query += ' AND media_type = ?'
            params.append(media_type)

        limit = request.args.get('limit', default=50, type=int)
        limit = min(limit, 200)  # 最大限制

        query += ' ORDER BY published_at DESC LIMIT ?'
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # 转换为字典列表
        news_list = []
        for row in rows:
            news_list.append({
                'id': row['id'],
                'title': row['title'],
                'url': row['url'],
                'summary': row['summary'],
                'source_name': row['source_name'],
                'score': row['score'],
                'category': row['category'],
                'published_at': row['published_at'],
                'is_read': row['is_read'],
                'cover_img': row['cover_img'],
                'media_type': row['media_type'] or 'article',
                'media_url': row['media_url'],
                'duration': row['duration'],
            })

        conn.close()
        return success_response(news_list)

    except Exception as e:
        return error_response(f'获取资讯失败: {e}')


@app.route('/api/hotlist', methods=['GET'])
def get_hotlist():
    """
    获取热榜
    优先返回今天采集到的、评分排名前 10 的资讯；
    若当天无数据，则逐天回退（昨天、前天……最多 7 天），取该日评分前 10。
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        today = date.today()

        def fetch_by_day(d):
            cursor.execute('''
            SELECT id, title, url, summary, source_name, score, category, published_at,
                   cover_img, media_type, media_url, duration
            FROM news
            WHERE created_at LIKE ?
            ORDER BY score DESC, published_at DESC
            LIMIT 10
            ''', (d.strftime('%Y-%m-%d') + '%',))
            return cursor.fetchall()

        # 优先当天；当天没有则逐天回退，最多往前 7 天
        rows = []
        used_date = today
        for offset in range(0, 8):
            target = today - timedelta(days=offset)
            candidate = fetch_by_day(target)
            if candidate:
                rows = candidate
                used_date = target
                break

        items = []
        for row in rows:
            items.append({
                'id': row['id'],
                'title': row['title'],
                'url': row['url'],
                'summary': row['summary'],
                'source_name': row['source_name'],
                'score': row['score'],
                'category': row['category'],
                'published_at': row['published_at'],
                'cover_img': row['cover_img'],
                'media_type': row['media_type'] or 'article',
                'media_url': row['media_url'],
                'duration': row['duration'],
            })

        conn.close()
        return success_response({
            'date': used_date.strftime('%Y-%m-%d'),
            'is_today': used_date == today,
            'total': len(items),
            'items': items
        })

    except Exception as e:
        return error_response(f'获取热榜失败: {e}')


@app.route('/api/daily', methods=['GET'])
def get_daily():
    """
    获取今日日报
    返回今天评分最高的 20 条资讯，按分类分组
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        today = date.today().strftime('%Y-%m-%d')

        # 获取高评分资讯（评分排序，不限制日期，确保有数据）
        cursor.execute('''
        SELECT id, title, url, summary, source_name, score, category, published_at,
               cover_img, media_type, media_url, duration
        FROM news
        ORDER BY score DESC, published_at DESC
        LIMIT 20
        ''')
        rows = cursor.fetchall()

        # 按分类分组
        grouped = {}
        for row in rows:
            cat = row['category'] or '其他'
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append({
                'id': row['id'],
                'title': row['title'],
                'url': row['url'],
                'summary': row['summary'],
                'source_name': row['source_name'],
                'score': row['score'],
                'category': row['category'],
                'published_at': row['published_at'],
                'cover_img': row['cover_img'],
                'media_type': row['media_type'] or 'article',
                'media_url': row['media_url'],
                'duration': row['duration'],
            })

        # 整理为前端友好格式
        daily_data = {
            'date': today,
            'total': len(rows),
            'groups': [
                {'category': cat, 'items': items, 'count': len(items)}
                for cat, items in sorted(grouped.items(), key=lambda x: -len(x[1]))
            ]
        }

        conn.close()
        return success_response(daily_data)

    except Exception as e:
        return error_response(f'获取日报失败: {e}')


# ==================== 信源相关接口 ====================

@app.route('/api/sources', methods=['GET'])
def get_sources():
    """获取所有信源列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
        SELECT s.id, s.name, s.url, s.category, s.is_active, s.default_media_type,
               (SELECT COUNT(*) FROM news n WHERE n.source_name = s.name) as news_count
        FROM sources s
        ORDER BY s.id
        ''')
        rows = cursor.fetchall()

        sources = []
        for row in rows:
            sources.append({
                'id': row['id'],
                'name': row['name'],
                'url': row['url'],
                'category': row['category'],
                'is_active': bool(row['is_active']),
                'default_media_type': row['default_media_type'] or 'article',
                'news_count': row['news_count']
            })

        conn.close()
        return success_response(sources)

    except Exception as e:
        return error_response(f'获取信源失败: {e}')


@app.route('/api/sources', methods=['POST'])
def add_source():
    """添加新信源"""
    try:
        data = request.get_json()
        name = (data.get('name') or '').strip()
        url = (data.get('url') or '').strip()
        category = (data.get('category') or '其他').strip()
        default_media_type = (data.get('default_media_type') or 'article').strip()
        if default_media_type not in ('article', 'image', 'video'):
            default_media_type = 'article'

        if not name or not url:
            return error_response('信源名称和URL不能为空', 400)

        conn = get_db()
        cursor = conn.cursor()

        # 检查是否已存在
        cursor.execute('SELECT id FROM sources WHERE url = ?', (url,))
        if cursor.fetchone():
            conn.close()
            return error_response('该URL的信源已存在', 400)

        cursor.execute('''
        INSERT INTO sources (name, url, category, is_active, default_media_type)
        VALUES (?, ?, ?, 1, ?)
        ''', (name, url, category, default_media_type))
        conn.commit()
        source_id = cursor.lastrowid

        conn.close()
        return success_response({'id': source_id, 'name': name, 'url': url,
                                 'default_media_type': default_media_type}, '信源添加成功')

    except Exception as e:
        return error_response(f'添加信源失败: {e}')


@app.route('/api/sources/<int:source_id>/toggle', methods=['POST'])
def toggle_source(source_id):
    """启用/停用信源"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute('SELECT is_active FROM sources WHERE id = ?', (source_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return error_response('信源不存在', 404)

        new_status = 0 if row['is_active'] else 1
        cursor.execute('UPDATE sources SET is_active = ? WHERE id = ?', (new_status, source_id))
        conn.commit()
        conn.close()

        status_text = '已启用' if new_status else '已停用'
        return success_response({'id': source_id, 'is_active': bool(new_status)}, f'信源{status_text}')

    except Exception as e:
        return error_response(f'操作失败: {e}')


@app.route('/api/sources/<int:source_id>', methods=['DELETE'])
def delete_source(source_id):
    """删除信源"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sources WHERE id = ?', (source_id,))
        conn.commit()

        if cursor.rowcount == 0:
            conn.close()
            return error_response('信源不存在', 404)

        conn.close()
        return success_response(None, '信源删除成功')

    except Exception as e:
        return error_response(f'删除失败: {e}')


# ==================== 统计接口 ====================

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """获取系统统计信息"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM news')
        total_news = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM news WHERE score >= 80')
        high_quality = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM sources WHERE is_active = 1')
        active_sources = cursor.fetchone()[0]

        cursor.execute('SELECT AVG(score) FROM news WHERE score > 0')
        avg_score = cursor.fetchone()[0] or 0

        cursor.execute('''
        SELECT category, COUNT(*) as cnt FROM news
        WHERE category IS NOT NULL
        GROUP BY category ORDER BY cnt DESC LIMIT 5
        ''')
        top_categories = [{'name': r[0], 'count': r[1]} for r in cursor.fetchall()]

        conn.close()
        return success_response({
            'total_news': total_news,
            'high_quality': high_quality,
            'active_sources': active_sources,
            'avg_score': round(avg_score, 1),
            'top_categories': top_categories
        })

    except Exception as e:
        return error_response(f'获取统计失败: {e}')


# ==================== 采集触发接口 ====================

@app.route('/api/collect', methods=['POST'])
def trigger_collect():
    """手动触发采集 + 阅读数补抓 + 自动打分"""
    try:
        import subprocess
        import sys
        import os
        project_dir = os.path.dirname(os.path.abspath(__file__))

        # 第一步：RSS 采集
        collect_result = subprocess.run(
            [sys.executable, 'collector.py'],
            capture_output=True, text=True, timeout=180,
            cwd=project_dir
        )
        collect_output = collect_result.stdout[-800:]
        if collect_result.returncode != 0:
            collect_output += '\n' + collect_result.stderr[-300:]

        # 第二步：阅读数补抓（C 路径，仅对 4 个稳定信源）
        enrich_result = subprocess.run(
            [sys.executable, 'enrich.py'],
            capture_output=True, text=True, timeout=600,
            cwd=project_dir
        )
        enrich_output = enrich_result.stdout[-800:]
        if enrich_result.returncode != 0:
            enrich_output += '\n' + enrich_result.stderr[-300:]

        # 第三步：对新资讯打分
        score_result = subprocess.run(
            [sys.executable, 'processor.py'],
            capture_output=True, text=True, timeout=120,
            cwd=project_dir
        )
        score_output = score_result.stdout[-800:]
        if score_result.returncode != 0:
            score_output += '\n' + score_result.stderr[-300:]

        # 第四步：自动清理老资讯（score<80 保留 15 天，score>=80 保留 30 天）
        from processor import cleanup_old_news
        normal_deleted, featured_deleted = cleanup_old_news()
        cleanup_output = (
            f'清理完成：普通分(<{FEATURED_THRESHOLD})删除 {normal_deleted} 条，'
            f'高分(>={FEATURED_THRESHOLD})删除 {featured_deleted} 条'
        )

        full_output = (
            collect_output
            + '\n--- 阅读数补抓 ---\n' + enrich_output
            + '\n--- 自动打分 ---\n' + score_output
            + '\n--- 自动清理 ---\n' + cleanup_output
        )
        return success_response({
            'output': full_output,
            'returncode': score_result.returncode
        })
    except Exception as e:
        return error_response(f'采集失败: {e}')


# ==================== 自动收集调度接口 ====================

@app.route('/api/auto-collect/status', methods=['GET'])
def auto_collect_status():
    """返回自动收集调度器状态：开关、间隔、下次执行、上次结果"""
    try:
        from scheduler import get_status
        return success_response(get_status())
    except Exception as e:
        return error_response(f'获取调度状态失败: {e}')


@app.route('/api/auto-collect/toggle', methods=['POST'])
def auto_collect_toggle():
    """运行时开关 / 调整 cron 表达式
    body: { enabled?: bool, cron?: str }   # cron 为 5 字段：分 时 日 月 周
    """
    try:
        from scheduler import update_config, _build_trigger
        data = request.get_json() or {}
        enabled = data.get('enabled')
        cron = data.get('cron')
        if cron is not None:
            if not isinstance(cron, str) or len(cron.split()) != 5:
                return error_response('cron 需为 5 字段（分 时 日 月 周），例如 "0 */6 * * *"', 400)
            try:
                _build_trigger(cron)  # 预校验表达式合法
            except Exception as e:
                return error_response(f'cron 表达式非法: {e}', 400)
        new_status = update_config(enabled=enabled, cron=cron)
        return success_response(new_status, '调度配置已更新')
    except Exception as e:
        return error_response(f'更新调度失败: {e}')


@app.route('/api/auto-collect/run-now', methods=['POST'])
def auto_collect_run_now():
    """立即触发一次采集（异步，立即返回状态）"""
    try:
        from scheduler import trigger_now
        return success_response(trigger_now(), '已触发一次手动采集，结果稍后写入 status')
    except Exception as e:
        return error_response(f'触发失败: {e}')


# ==================== 前端页面 ====================

@app.route('/')
def index():
    """前端首页"""
    return send_from_directory('.', 'index.html')


if __name__ == '__main__':
    print('=' * 50)
    print('  资讯聚合系统 - Flask Web 服务')
    print(f'  启动时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('  API 地址: http://localhost:5001')
    print('  前端页面: http://localhost:5001/')
    # 启动自动收集后台调度器（仅 debug=False 时启动，避免 reloader 重复实例化）
    from scheduler import init_scheduler
    sched = init_scheduler(app)
    if sched and AUTO_COLLECT_ENABLED:
        print(f'  自动收集: 已启用，cron = {AUTO_COLLECT_CRON}（每天 0/6/12/18 点）')
    else:
        print('  自动收集: 未启用')
    print('=' * 50)
    print()
    app.run(host='0.0.0.0', port=5001, debug=False)
