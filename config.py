# 配置文件
import os

# 数据库路径（绝对路径，避免 cwd 不同连错数据库）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'news.db')

# RSS 信源列表（均为实测可用的直连 RSS）
RSS_SOURCES = [
    # ===== 技术 =====
    {
        'name': 'V2EX',
        'url': 'https://www.v2ex.com/index.xml',
        'category': '技术',
        'is_active': 1
    },
    {
        'name': '阮一峰博客',
        'url': 'https://www.ruanyifeng.com/blog/atom.xml',
        'category': '技术',
        'is_active': 1
    },
    {
        'name': 'Hacker News',
        'url': 'https://news.ycombinator.com/rss',
        'category': '技术',
        'is_active': 1
    },
    {
        'name': 'InfoQ中文站',
        'url': 'https://www.infoq.cn/feed',
        'category': '技术',
        'is_active': 1
    },
    {
        'name': 'IT之家',
        'url': 'https://www.ithome.com/rss/',
        'category': '技术',
        'is_active': 1
    },

    # ===== 行业 =====
    {
        'name': '爱范儿',
        'url': 'https://www.ifanr.com/feed',
        'category': '行业',
        'is_active': 1
    },
    {
        'name': '极客公园',
        'url': 'https://www.geekpark.net/rss',
        'category': '行业',
        'is_active': 1
    },
    {
        'name': '虎嗅',
        'url': 'https://rss.huxiu.com/',
        'category': '行业',
        'is_active': 1
    },

    # ===== 财经 =====
    {
        'name': '未央网',
        'url': 'https://weiyangx.com/feed/',
        'category': '财经',
        'is_active': 1
    },

    # ===== 产品 =====
    {
        'name': '少数派',
        'url': 'https://sspai.com/feed',
        'category': '产品',
        'is_active': 1
    },
    {
        'name': '小众软件',
        'url': 'https://www.appinn.com/feed/',
        'category': '产品',
        'is_active': 1
    },

    # ===== 影像（照片类信源，default_media_type='image'）=====
    {
        'name': 'NASA APOD',
        'url': 'https://apod.nasa.gov/apod.rss',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
    {
        'name': 'NASA News',
        'url': 'https://www.nasa.gov/feed/',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
    {
        'name': 'Smashing Magazine',
        'url': 'https://www.smashingmagazine.com/feed/',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
    {
        'name': 'Unsplash Blog',
        'url': 'https://unsplash.com/blog/rss/',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
    {
        'name': 'Atlas Obscura',
        'url': 'https://www.atlasobscura.com/feeds/latest',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
    {
        'name': 'Colossal',
        'url': 'https://www.thisiscolossal.com/feed/',
        'category': '影像',
        'default_media_type': 'image',
        'is_active': 1
    },
]

# DeepSeek API 配置
DEEPSEEK_API_KEY = ''  # 填入你的 DeepSeek API Key
DEEPSEEK_API_URL = 'https://api.deepseek.com/chat/completions'

# AI 打分分类选项
CATEGORIES = ['技术', '产品', '行业', '学术', '其他']

# 精选阈值：评分达到此值标记为精选
FEATURED_THRESHOLD = 80

# 老资讯自动清理策略（按评分分级保留）
#   score <  FEATURED_THRESHOLD：保留 RETENTION_DAYS_NORMAL 天
#   score >= FEATURED_THRESHOLD：保留 RETENTION_DAYS_FEATURED 天
RETENTION_DAYS_NORMAL = 15
RETENTION_DAYS_FEATURED = 30

# ==================== 自动收集配置 ====================
# 后台调度器（APScheduler）按 cron 表达式周期触发采集 → 补抓 → 打分 → 清理
# 默认每天 0:00、6:00、12:00、18:00 整点执行（即「6:00 + 6h 的倍数」）
AUTO_COLLECT_ENABLED = True          # 服务启动时是否开启自动收集
AUTO_COLLECT_CRON = '0 */6 * * *'    # 分 时 日 月 周；*/6 表示 0,6,12,18 点
# 单次任务最长耗时（秒），超过则视为卡死并放行下一次
AUTO_COLLECT_JOB_TIMEOUT = 1200
