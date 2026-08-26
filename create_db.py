"""
数据库建表脚本
创建 news 表和 sources 表
"""
import sqlite3
import os
from config import DB_PATH, RSS_SOURCES


def create_db():
    """创建数据库和表"""
    # 如果数据库已存在，先删除（开发用）
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f'已删除旧数据库: {DB_PATH}')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 创建 news 表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        summary TEXT,
        source_name TEXT,
        published_at TEXT,
        score INTEGER DEFAULT 0,
        category TEXT,
        is_read INTEGER DEFAULT 0,
        view_count INTEGER,
        view_collected_at TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''')
    print('✓ news 表创建成功')

    # 创建 sources 表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        category TEXT,
        is_active INTEGER DEFAULT 1,
        default_media_type TEXT
    )
    ''')
    print('✓ sources 表创建成功')

    # 插入默认 RSS 信源
    for source in RSS_SOURCES:
        cursor.execute('''
        INSERT INTO sources (name, url, category, is_active, default_media_type)
        VALUES (?, ?, ?, ?, ?)
        ''', (
            source['name'],
            source['url'],
            source['category'],
            source['is_active'],
            source.get('default_media_type')
        ))
    print(f'✓ 已插入 {len(RSS_SOURCES)} 个默认 RSS 信源')

    conn.commit()
    conn.close()
    print(f'\n数据库创建完成: {DB_PATH}')


def verify_db():
    """验证数据库内容"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 查询 sources 表
    cursor.execute('SELECT * FROM sources')
    sources = cursor.fetchall()
    print(f'\n--- 信源列表 (共 {len(sources)} 条) ---')
    for s in sources:
        print(f'  [{s[0]}] {s[1]} | {s[3]} | 活跃: {bool(s[4])}')
        print(f'      URL: {s[2]}')

    # 查询 news 表
    cursor.execute('SELECT COUNT(*) FROM news')
    count = cursor.fetchone()[0]
    print(f'\n--- 资讯表 ---')
    print(f'  当前资讯数量: {count} 条')

    # 查看表结构
    cursor.execute("PRAGMA table_info(news)")
    columns = cursor.fetchall()
    print(f'\n--- news 表结构 (共 {len(columns)} 列) ---')
    for col in columns:
        print(f'  {col[1]}: {col[2]}')

    conn.close()


if __name__ == '__main__':
    create_db()
    verify_db()
