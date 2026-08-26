"""
数据库迁移脚本：v2
给 news 表新增 view_count、view_collected_at 两列，
用于支持热度维度的真实阅读数采集（C 路径）。

- 幂等：列已存在则跳过
- 不删除/不修改现有数据
"""
import sqlite3
from config import DB_PATH


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(news)")
    cols = {row[1] for row in cursor.fetchall()}

    added = []
    if 'view_count' not in cols:
        cursor.execute("ALTER TABLE news ADD COLUMN view_count INTEGER")
        added.append('view_count')
    if 'view_collected_at' not in cols:
        cursor.execute("ALTER TABLE news ADD COLUMN view_collected_at TEXT")
        added.append('view_collected_at')

    conn.commit()
    conn.close()

    if added:
        print(f'✓ 迁移完成，新增列: {", ".join(added)}')
    else:
        print('✓ 列已存在，无需迁移')


if __name__ == '__main__':
    migrate()
