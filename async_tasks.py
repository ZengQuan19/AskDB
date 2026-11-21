"""问答异步任务队列：把耗时的 AI 调用从 HTTP 请求里挪到后台线程。

为什么需要它？
    一次问答要 3~30 秒（多轮工具调用 + 模型推理）。如果直接在视图里跑，
    一个请求就会占满一个 WSGI worker 几十秒；多个用户同时提问时，后来的请求
    只能干等，稍微多点人就会超时。改成「提交任务 → 立刻返回 task_id → 前端轮询」后：
      - HTTP 请求毫秒级返回，worker 立刻释放；
      - 并发度由线程池显式控制，不会被突然涌入的请求拖垮；
      - 超出容量时快速拒绝（QueueFull），而不是让所有人都卡住；
      - 前端还能拿到进度（第几轮、在调什么工具）。

为什么用线程池而不是 asyncio？
    下游全是阻塞式库：requests（HTTP）、pymysql / sqlite3（数据库）。
    在 Flask(WSGI) 里把它们包进 asyncio 不会变快，反而要多维护一套事件循环。
    线程池是最小改动、且真实有效的方案；等将来换成 ASGI（FastAPI/Quart），
    再把 _run 里换成 await 即可，接口不用动。

局限（有意为之）：
    - 纯内存：进程重启任务就丢，也不会跨进程共享。要横向扩容得换 Redis/Celery。
    - 单进程线程数有上限：真正的多机部署请上 Celery/RQ。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from config_store import load_config

# 默认值，可在 instance/config.json 的 "server" 段覆盖
DEFAULTS = {
    "max_concurrent": 4,      # 同时执行的任务数
    "queue_size": 20,         # 额外允许排队的任务数
    "per_session_limit": 2,   # 单个会话最多几个未完成任务
    "result_ttl": 600,        # 已完成任务保留多少秒（供轮询取结果）
}

_LOCK = threading.Lock()
_TASKS = {}               # task_id -> dict
_EXECUTOR = None
_EXECUTOR_SIZE = None


class QueueFull(Exception):
    """排队已满，调用方应回 429 并提示稍后再试。"""


# ---------------- 内部工具 ----------------

def _server_cfg():
    cfg = load_config().get("server") or {}
    out = {}
    for key, default in DEFAULTS.items():
        try:
            out[key] = int(cfg.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    # 兜底，避免配置写成 0 或负数导致线程池起不来
    out["max_concurrent"] = max(1, out["max_concurrent"])
    out["queue_size"] = max(0, out["queue_size"])
    out["per_session_limit"] = max(1, out["per_session_limit"])
    out["result_ttl"] = max(30, out["result_ttl"])
    return out


def _executor(size):
    """线程池按配置懒创建；配置变了（少见）就重建。"""
    global _EXECUTOR, _EXECUTOR_SIZE
    with _LOCK:
        if _EXECUTOR is None or _EXECUTOR_SIZE != size:
            _EXECUTOR = ThreadPoolExecutor(max_workers=size, thread_name_prefix="askdb")
            _EXECUTOR_SIZE = size
        return _EXECUTOR


def _cleanup_locked(ttl):
    """清掉过期的完成任务。必须在持锁时调用。"""
    now = time.time()
    stale = [
        tid for tid, t in _TASKS.items()
        if t["status"] in ("done", "error")
        and t.get("finished") and now - t["finished"] > ttl
    ]
    for tid in stale:
        _TASKS.pop(tid, None)


# ---------------- 对外接口 ----------------

def submit(sid, question, history=None):
    """提交一个问答任务，立即返回 task_id。

    超容量或该会话未完成任务过多时抛 QueueFull。
    """
    cfg = _server_cfg()
    executor = _executor(cfg["max_concurrent"])

    with _LOCK:
        _cleanup_locked(cfg["result_ttl"])
        pending = [t for t in _TASKS.values() if t["status"] in ("queued", "running")]
        if len(pending) >= cfg["max_concurrent"] + cfg["queue_size"]:
            raise QueueFull("服务器繁忙（排队已满），请稍后再试")

        mine = [t for t in pending if t.get("sid") == sid]
        if len(mine) >= cfg["per_session_limit"]:
            raise QueueFull("你还有未完成的提问，请等它结束或刷新页面")

        task_id = uuid.uuid4().hex
        _TASKS[task_id] = {
            "id": task_id,
            "sid": sid,
            "question": question,
            "status": "queued",
            "message": "已提交，等待执行…",
            "error": None,
            "result": None,
            "created": time.time(),
            "started": None,
            "finished": None,
        }

    executor.submit(_run, task_id, sid, question, history or [])
    return task_id


def _run(task_id, sid, question, history):
    """在线程池里真正执行的函数。注意：这里不能抛异常出去，否则任务会永远卡在 running。"""
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:            # 已被清理
            return
        task["status"] = "running"
        task["started"] = time.time()
        task["message"] = "正在思考…"

    def on_event(ev):
        # 进度回调：任何异常都不能影响主流程
        with _LOCK:
            item = _TASKS.get(task_id)
            if item is not None and ev.get("message"):
                item["message"] = ev["message"]

    try:
        # 延迟导入：避免 app -> async_tasks -> agent -> config_store 的加载顺序问题
        from agent import run_agent
        from chat_memory import add_turn

        answer, usage, trace = run_agent(question, history=history, on_event=on_event)

        # 结果落进会话记忆，前端轮询到 done 后刷新页面就能看到新一轮
        add_turn(sid, question, answer, trace, usage)
    except Exception as e:
        with _LOCK:
            item = _TASKS.get(task_id)
            if item is not None:
                item["status"] = "error"
                item["error"] = f"{type(e).__name__}: {e}"
                item["message"] = "执行失败"
                item["finished"] = time.time()
        return

    with _LOCK:
        item = _TASKS.get(task_id)
        if item is not None:
            item["status"] = "done"
            item["message"] = "完成"
            item["result"] = {"answer": answer, "usage": usage, "trace": trace}
            item["finished"] = time.time()


def snapshot(task_id):
    """给前端轮询用的快照；任务不存在返回 None。"""
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return None
        now = time.time()
        started = task.get("started") or now
        end = task.get("finished") or now
        return {
            "id": task["id"],
            "status": task["status"],
            "message": task["message"],
            "error": task["error"],
            "question": task["question"],
            "elapsed": round(max(0.0, end - started), 1),
        }


def stats():
    """健康检查用：当前排队/执行中的数量。"""
    with _LOCK:
        pending = [t for t in _TASKS.values() if t["status"] in ("queued", "running")]
        return {
            "pending": len(pending),
            "running": sum(1 for t in _TASKS.values() if t["status"] == "running"),
            "known": len(_TASKS),
            "max_concurrent": _EXECUTOR_SIZE or 0,
        }


def clear_all():
    """测试用：清空任务表（不会中断已提交的线程）。"""
    with _LOCK:
        _TASKS.clear()
