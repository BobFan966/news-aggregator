"""
数据库迁移脚本：v3 —— 多媒体字段
给 news 表新增 4 列，用于承载「封面图 / 媒体类型 / 媒体URL / 时长」：
  - cover_img       TEXT     封面图或缩略图 URL（文章取首图，视频取缩略图，独立照片取主图）
  - media_type      TEXT     article / image / video（默认 article）
  - media_url       TEXT     高清原图 URL 或 视频直链 URL
  - duration        INTEGER  视频时长（秒），供前端展示

同时给 sources 表新增 default_media_type 列，用于标记信源默认的媒体类型。

幂等：列已存在则跳过；不删除、不修改现有数据。
"""
import sqlite3
from config import DB_PATH


NEW_COLUMNS = [
    ('cover_img', 'TEXT'),
    ('media_type', "TEXT DEFAULT 'article'"),
    ('media_url', 'TEXT'),
    ('duration', 'INTEGER'),
]


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # === news 表迁移 ===
    cursor.execute("PRAGMA table_info(news)")
    cols = {row[1] for row in cursor.fetchall()}

    added = []
    for col_name, col_def in NEW_COLUMNS:
        if col_name not in cols:
            cursor.execute(f"ALTER TABLE news ADD COLUMN {col_name} {col_def}")
            added.append(col_name)

    # 给老数据填默认值
    cursor.execute("UPDATE news SET media_type = 'article' WHERE media_type IS NULL OR media_type = ''")

    # === sources 表迁移 ===
    cursor.execute("PRAGMA table_info(sources)")
    src_cols = {row[1] for row in cursor.fetchall()}
    if 'default_media_type' not in src_cols:
        cursor.execute("ALTER TABLE sources ADD COLUMN default_media_type TEXT")
        added.append('sources.default_media_type')

    conn.commit()
    conn.close()

    if added:
        print(f'✓ 迁移完成，新增列: {", ".join(added)}')
        print(f'✓ 已将历史数据的 media_type 默认填充为 article')
    else:
        print('✓ 媒体相关列已存在，无需迁移')


if __name__ == '__main__':
    migrate()
