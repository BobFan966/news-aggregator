"""
自动收集调度器
基于 APScheduler BackgroundScheduler，按 cron 表达式周期性触发采集流程。

调度策略：默认 cron `0 */6 * * *`，即每天 0:00 / 6:00 / 12:00 / 18:00 整点触发。

设计要点：
1. 复用 app.trigger_collect 同一套子进程调用（collector → enrich → processor → cleanup）。
2. 互斥锁 _run_lock 防止上一次未跑完又触发下一次。
3. 状态字典 _status 供 /api/auto-collect/status 读取，前端展示。
4. 运行时可通过 update_config 动态调整开关与 cron 表达式，无需重启服务。
"""
import os
import sys
import subprocess
import threading
import traceback
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    DB_PATH,
    FEATURED_THRESHOLD,
    AUTO_COLLECT_ENABLED,
    AUTO_COLLECT_CRON,
    AUTO_COLLECT_JOB_TIMEOUT,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 全局调度器与状态
_scheduler = None
_run_lock = threading.Lock()  # 互斥：同一时刻只允许一个采集任务运行
_status_lock = threading.Lock()
_status = {
    'enabled': AUTO_COLLECT_ENABLED,
    'cron': AUTO_COLLECT_CRON,
    'last_run_at': None,         # ISO 字符串
    'last_run_status': None,     # 'success' / 'failed' / 'running' / 'skipped'
    'last_run_msg': '',          # 截断的输出/错误
    'next_run_at': None,         # ISO 字符串
    'last_duration_sec': None,
}


def _set_status(**kwargs):
    """线程安全地更新状态字典"""
    with _status_lock:
        _status.update(kwargs)


def get_status():
    """返回当前状态快照（含下次执行时间）"""
    with _status_lock:
        snapshot = dict(_status)
    # 计算 next_run_at
    if _scheduler is not None:
        try:
            job = _scheduler.get_job('auto_collect')
            if job and job.next_run_time:
                snapshot['next_run_at'] = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
            else:
                snapshot['next_run_at'] = None
        except Exception:
            snapshot['next_run_at'] = None
    return snapshot


def _do_collect():
    """实际采集任务，与 app.trigger_collect 流程一致。"""
    # 互斥：若上一次仍在跑，则跳过本次
    if not _run_lock.acquire(blocking=False):
        _set_status(
            last_run_status='skipped',
            last_run_msg='上一次任务尚未结束，本次跳过',
        )
        return

    started_at = datetime.now()
    _set_status(
        last_run_at=started_at.strftime('%Y-%m-%d %H:%M:%S'),
        last_run_status='running',
        last_run_msg='',
    )

    try:
        # 第一步：RSS 采集
        r1 = subprocess.run(
            [sys.executable, 'collector.py'],
            capture_output=True, text=True, timeout=180,
            cwd=PROJECT_DIR,
        )
        out1 = r1.stdout[-600:]
        if r1.returncode != 0:
            out1 += '\n' + r1.stderr[-200:]

        # 第二步：阅读数补抓
        r2 = subprocess.run(
            [sys.executable, 'enrich.py'],
            capture_output=True, text=True, timeout=600,
            cwd=PROJECT_DIR,
        )
        out2 = r2.stdout[-600:]
        if r2.returncode != 0:
            out2 += '\n' + r2.stderr[-200:]

        # 第三步：打分
        r3 = subprocess.run(
            [sys.executable, 'processor.py'],
            capture_output=True, text=True, timeout=120,
            cwd=PROJECT_DIR,
        )
        out3 = r3.stdout[-600:]
        if r3.returncode != 0:
            out3 += '\n' + r3.stderr[-200:]

        # 第四步：清理老资讯
        from processor import cleanup_old_news
        normal_deleted, featured_deleted = cleanup_old_news()
        out4 = (
            f'清理完成：普通分(<{FEATURED_THRESHOLD})删除 {normal_deleted} 条，'
            f'高分(>={FEATURED_THRESHOLD})删除 {featured_deleted} 条'
        )

        duration = (datetime.now() - started_at).total_seconds()
        full_msg = (
            f'[采集] {out1}\n[补抓] {out2}\n[打分] {out3}\n[{out4}]'
        )[-1500:]

        overall_ok = all(r.returncode == 0 for r in [r1, r2, r3])
        _set_status(
            last_run_status='success' if overall_ok else 'failed',
            last_run_msg=full_msg,
            last_duration_sec=round(duration, 1),
        )
    except subprocess.TimeoutExpired as e:
        _set_status(
            last_run_status='failed',
            last_run_msg=f'子进程超时: {e}',
            last_duration_sec=(datetime.now() - started_at).total_seconds(),
        )
    except Exception as e:
        _set_status(
            last_run_status='failed',
            last_run_msg=f'异常: {e}\n{traceback.format_exc()[-800:]}',
            last_duration_sec=(datetime.now() - started_at).total_seconds(),
        )
    finally:
        _run_lock.release()


def _build_trigger(cron_expr):
    """由 cron 表达式构造 CronTrigger。"""
    # 标准 5 字段：分 时 日 月 周
    fields = cron_expr.split()
    if len(fields) != 5:
        raise ValueError(f'cron 表达式需为 5 字段（分 时 日 月 周），收到: {cron_expr!r}')
    return CronTrigger(
        minute=fields[0], hour=fields[1], day=fields[2],
        month=fields[3], day_of_week=fields[4],
    )


def _reschedule():
    """根据 _status['enabled'] 与 _status['cron'] 重排任务。"""
    global _scheduler
    try:
        _scheduler.remove_job('auto_collect')
    except Exception:
        pass

    if _status['enabled']:
        trigger = _build_trigger(_status['cron'])
        _scheduler.add_job(
            func=_do_collect,
            trigger=trigger,
            id='auto_collect',
            max_instances=1,           # 同一任务最多 1 个实例
            coalesce=True,             # 积压多次触发合并为 1 次
            misfire_grace_time=600,    # 10 分钟内的迟到触发仍执行
        )


def init_scheduler(app=None):
    """在 Flask 启动时调用，初始化后台调度器。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(daemon=True)

    if AUTO_COLLECT_ENABLED:
        trigger = _build_trigger(_status['cron'])
        _scheduler.add_job(
            func=_do_collect,
            trigger=trigger,
            id='auto_collect',
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )

    _scheduler.start()
    return _scheduler


def update_config(enabled=None, cron=None):
    """运行时调整开关 / cron 表达式，立即重排任务。"""
    global _scheduler
    if _scheduler is None:
        return get_status()

    if enabled is not None:
        _set_status(enabled=enabled)

    if cron is not None:
        # 先校验表达式合法，不合法则原状不动
        try:
            _build_trigger(cron)
        except Exception as e:
            return get_status()
        _set_status(cron=cron)

    _reschedule()
    return get_status()


def trigger_now():
    """立即触发一次（不影响下次定时）"""
    threading.Thread(target=_do_collect, daemon=True).start()
    return get_status()


def shutdown():
    """优雅关闭调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
