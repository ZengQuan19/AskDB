"""会话记忆：按浏览器会话保存最近几轮问答，供追问使用。

为什么放内存而不是 session cookie？
  Flask 的 session 是签过名的 cookie，浏览器每次请求都会带上来，
  而一轮问答（含表格）动辄几百上千字，塞进 cookie 会超过 4KB 上限。
  所以这里用「服务端字典 + 会话 id」。

几个取舍：
  - 纯内存：进程重启就清空。对本地单机工具来说够用，也不会把聊天内容写进磁盘。
  - deque(maxlen=3)：超过 3 轮自动淘汰最旧的一条，不用手动裁剪。
  - 加锁：Flask 开发服务器默认多线程，不加锁可能读到半更新的列表。
"""
import threading
from collections import deque
from datetime import datetime

# 记忆轮数：一问一答算一轮
MAX_ROUNDS = 3

_LOCK = threading.Lock()
_STORE = {}          # session_id -> deque[{q, a, trace, usage, time}]


def turns(sid):
    """取某个会话的历史（按时间正序，最多 MAX_ROUNDS 条）。"""
    with _LOCK:
        return list(_STORE.get(sid, ()))


def add_turn(sid, question, answer, trace=None, usage=None):
    """追加一轮；超出 MAX_ROUNDS 时最旧的一轮自动被挤掉。"""
    with _LOCK:
        bucket = _STORE.setdefault(sid, deque(maxlen=MAX_ROUNDS))
        bucket.append({
            "q": question,
            "a": answer,
            "trace": trace or [],
            "usage": usage or {},
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        return list(bucket)


def reset(sid):
    """清空某个会话的记忆，返回是否真的清掉了东西。"""
    with _LOCK:
        return bool(_STORE.pop(sid, None))


def count(sid):
    with _LOCK:
        return len(_STORE.get(sid, ()))


def clear_all():
    """测试用：清空所有会话。"""
    with _LOCK:
        _STORE.clear()
