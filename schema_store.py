"""数据库结构的「AI 友好文本」缓存。

设计要点：
1. 所有数据库的结构存在**同一个文件**（instance/schema_cache.json），key 就是库名；
2. MySQL / SQLite 用 "type" + "dialect" 双重标记，AI 写 SQL 时能选对方言；
3. 同一个库重新生成时，**整条记录先删后增**（不是往数组里追加）；
4. 另存一个全局开关：是否允许非管理员点「更新结构」。

注意：本模块会调用大模型（llm.chat），因此 refresh_schema() 是慢操作（数秒~数十秒）。
"""
import json
import os
from datetime import datetime
from pathlib import Path

from db_operator import get_db, get_schema
from llm import chat

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
SCHEMA_FILE = INSTANCE_DIR / "schema_cache.json"
SETTINGS_FILE = INSTANCE_DIR / "app_settings.json"

#TODO: 将数据库结构从提示词注入改为RAG查询，避免过大的库导致上下文爆炸以及干扰

# 与 agent.py 里那套「问答提示词」完全独立的一套提示词：
# 它的唯一任务是把原始结构改写成另一个模型能直接照着写 SQL 的文本。
SCHEMA_SYSTEM_PROMPT = """你是数据库建模助手。用户会给你一份从系统目录读出的原始表结构 JSON。

你的任务是把它改写成「直接可以交给另一个大模型用来写 SQL」的紧凑文本，必须遵守：

1. 只输出结果本身：不要寒暄、不要解释、不要“好的/以下是”之类的开场白。
2. 第一行固定格式：数据库：<库名> | 方言：<MySQL 或 SQLite>
3. 之后每个表输出一段 CREATE TABLE 语句，保留：列名、类型、是否可空、默认值、主键、自增等。
4. 有表注释/列注释就用注释形式保留，没有就省略，不要编造。
5. 不要新增、删除或重命名任何表与字段，不要编造数据类型。
6. 表和表之间空一行。
7. 不要使用 markdown 代码围栏（不要 ```），直接写纯文本。"""


# ---------------- 文件读写 ----------------

def _ensure_dir():
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        # 文件损坏时不让整个应用挂掉，退回默认值
        return default


def _write_json(path, data):
    """先写临时文件再原子替换，避免写到一半断电留下半个 JSON。"""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------- 结构缓存 ----------------

def load_all():
    """返回 {"库名": 记录}。"""
    return _read_json(SCHEMA_FILE, {}).get("databases", {})


def get_record(db_name):
    return load_all().get(db_name)


def remove_record(db_name):
    """删掉某个库的结构，返回是否真的删了。"""
    data = _read_json(SCHEMA_FILE, {})
    databases = data.get("databases", {})
    if db_name not in databases:
        return False
    del databases[db_name]
    data["databases"] = databases
    _write_json(SCHEMA_FILE, data)
    return True


def upsert_record(record):
    """整条替换：同一个库名，旧记录直接丢弃。"""
    data = _read_json(SCHEMA_FILE, {})
    databases = data.setdefault("databases", {})
    databases[record["db"]] = record        # dict 赋值天然就是「先删后增」
    _write_json(SCHEMA_FILE, data)
    return record


# ---------------- 原始结构 -> 紧凑 JSON ----------------

def _compact_schema(schema):
    """把 db_reader 的庞大 dict 压成喂给模型的最小信息集，省 token。"""
    tables = []
    for tname, t in schema.items():
        cols = []
        for c in t["COLUMNS"]:
            col = {
                "name": c["COLUMN_NAME"],
                "type": c["COLUMN_TYPE"],
                "nullable": c["IS_NULLABLE"] == "YES",
            }
            if c["COLUMN_KEY"]:
                col["key"] = c["COLUMN_KEY"]
            if c["COLUMN_DEFAULT"] is not None:
                col["default"] = str(c["COLUMN_DEFAULT"])
            if c["EXTRA"]:
                col["extra"] = c["EXTRA"]
            if c["COLUMN_COMMENT"]:
                col["comment"] = c["COLUMN_COMMENT"]
            cols.append(col)
        item = {"table": tname, "columns": cols}
        if t["TABLE_COMMENT"]:
            item["comment"] = t["TABLE_COMMENT"]
        if t["PRIMARY_KEY"]:
            item["primary_key"] = t["PRIMARY_KEY"]
        tables.append(item)
    return {"tables": tables}


def _dialect(db_type):
    return "SQLite" if db_type == "sqlite" else "MySQL"


# ---------------- 生成 ----------------

def generate_text(entry):
    """读结构 + 调大模型，返回 (文本, 表数, usage)。不写文件。"""
    raw = get_schema(entry)                     # 连库读信息目录
    compact = _compact_schema(raw)
    user_prompt = (
        f"数据库名：{entry['db']}\n"
        f"数据库类型：{_dialect(entry['type'])}\n"
        f"表数量：{len(compact['tables'])}\n"
        f"原始结构 JSON：\n{json.dumps(compact, ensure_ascii=False, indent=1)}"
    )
    text, usage, _ = chat(
        [
            {"role": "system", "content": SCHEMA_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
    )
    return text.strip(), len(compact["tables"]), usage


def refresh_schema(entry):
    """完整流程：读结构 -> AI 改写 -> 落盘（整条替换）-> 返回记录。"""
    text, table_count, usage = generate_text(entry)
    record = {
        "db": entry["db"],
        "type": entry["type"],                  # mysql / sqlite，机器判断用
        "dialect": _dialect(entry["type"]),     # MySQL / SQLite，给人看、也给模型看
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "table_count": table_count,
        "tokens": usage.get("total_tokens", 0),
        "text": text,
    }
    return upsert_record(record)


# ---------------- 给问答用的上下文 ----------------

def schema_context():
    """拼成注入提问提示词的一段文本；没有任何缓存时返回空串。"""
    databases = load_all()
    if not databases:
        return ""
    parts = []
    for name, rec in databases.items():
        parts.append(
            f"### 数据库：{name}（方言：{rec.get('dialect', '未知')}）\n"
            f"{rec.get('text', '').strip()}"
        )
    return "\n\n".join(parts)


# ---------------- 权限开关 ----------------

def allow_non_admin_update():
    """默认 False：只有管理员能更新结构。"""
    return bool(_read_json(SETTINGS_FILE, {}).get("allow_non_admin_update", False))


def set_allow_non_admin_update(value):
    data = _read_json(SETTINGS_FILE, {})
    data["allow_non_admin_update"] = bool(value)
    _write_json(SETTINGS_FILE, data)
    return bool(value)


if __name__ == "__main__":
    from db_operator import DB_list

    if not DB_list:
        print("DB_list 为空，先跑 db_operator.py 的示例或自己 add_db")
    else:
        print("当前缓存：", list(load_all()))
        print("允许非管理员更新结构：", allow_non_admin_update())
