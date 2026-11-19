"""AI 工具集：编码 → 解码 → 安全闸 → 执行。

安全模型（四道，任何一道不过就不执行）：

  ① 编码表 ENCODING_TABLE 只登记了 4 种**固定形态**的查询（Q1~Q4），
     AI 没有"自由书写 SQL"的能力，只能选一个 code 再填参数；
  ② 解码器逐字段校验：表名、列名必须真实存在于该库的结构里，
     操作符必须在白名单内，函数只认 SUM/AVG/COUNT/MIN/MAX；
  ③ 所有值一律走参数化占位符（MySQL 用 %s，SQLite 用 ?），
     值里的引号、分号根本不会进入 SQL 文本；
  ④ sql_guard.check_sql() 对生成的 SQL 再做一次与 AI 无关的规则检查。

也就是说：哪怕 AI 被提示词注入完全操纵，它能做到的极限也只是
"用安全的模板查它本来就有权限查的表"。
"""
import json
import re
from decimal import Decimal
from datetime import date, datetime

from db_operator import DB_list, connect, close, get_db, get_schema
from sql_guard import check_sql

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# ---------------- 错误类型 ----------------

class ToolError(Exception):
    """工具层的可预期错误（参数不对、表不存在等），会被转成给 AI 看的提示。"""


# ---------------- 编码表（唯一入口） ----------------

ENCODING_TABLE = {
    "Q1": "单表明细查询：SELECT <列> FROM <表> [WHERE] [ORDER BY] LIMIT",
    "Q2": "多表关联明细查询：Q1 + JOIN",
    "Q3": "单表聚合：SELECT SUM/AVG/COUNT/MIN/MAX(...) FROM <表> [WHERE]",
    "Q4": "分组聚合：Q3 + GROUP BY",
}

AGG_FUNCS = {
    "SUM": "SUM({col})",
    "AVG": "AVG({col})",
    "COUNT": "COUNT({col})",
    "COUNT_DISTINCT": "COUNT(DISTINCT {col})",
    "MIN": "MIN({col})",
    "MAX": "MAX({col})",
}

FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "like", "in", "between", "is null", "is not null"}


# ---------------- 结构缓存（校验表名/列名用） ----------------

_SCHEMA_CACHE = {}


def reset_schema_cache():
    """结构更新后调用，避免用旧结构校验新查询。"""
    _SCHEMA_CACHE.clear()


def _load_target(db_name):
    entry = get_db(db_name)
    if entry is None:
        available = [d["db"] for d in DB_list]
        raise ToolError(f"找不到数据源 {db_name!r}，可用的是：{available}")
    if db_name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[db_name] = get_schema(entry)
    return entry, _SCHEMA_CACHE[db_name]


# ---------------- 标识符校验 ----------------

def _table(schema, name):
    if name not in schema:
        raise ToolError(f"表 {name!r} 不存在，可用的表：{sorted(schema)}")
    return f"`{name}`"


def _split_qualified(raw, default_table):
    """"orders.user_id" -> ("orders", "user_id")；"user_id" -> (default, "user_id")。"""
    if "." in raw:
        t, c = raw.split(".", 1)
        return t.strip("`"), c.strip("`")
    return default_table, raw.strip("`")


def _column(schema, table, raw, qualify=False):
    t, c = _split_qualified(raw, table)
    if t not in schema:
        raise ToolError(f"列 {raw!r} 指向了不存在的表 {t!r}")
    names = {col["COLUMN_NAME"] for col in schema[t]["COLUMNS"]}
    if c not in names:
        raise ToolError(f"表 {t} 没有列 {c!r}，可用列：{sorted(names)}")
    return f"`{t}`.`{c}`" if qualify else f"`{c}`"


# ---------------- 片段构造 ----------------

def _where(schema, base_table, filters, ph):
    if not filters:
        return "", []
    parts, params = [], []
    for f in filters:
        if not isinstance(f, dict):
            raise ToolError("filters 的每一项都必须是对象")
        col = _column(schema, base_table, f.get("column", ""), qualify="." in str(f.get("column", "")))
        op = str(f.get("op", "=")).strip().lower()
        if op not in FILTER_OPS:
            raise ToolError(f"不支持的操作符 {op!r}，可用：{sorted(FILTER_OPS)}")

        value = f.get("value")
        if op == "is null" or op == "is not null":
            parts.append(f"{col} {op.upper()}")
        elif op == "in":
            if not isinstance(value, list) or not value:
                raise ToolError("in 操作符的 value 必须是非空数组")
            parts.append(f"{col} IN ({', '.join([ph] * len(value))})")
            params.extend(value)
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise ToolError("between 操作符的 value 必须是长度为 2 的数组")
            parts.append(f"{col} BETWEEN {ph} AND {ph}")
            params.extend(value)
        elif op == "like":
            parts.append(f"{col} LIKE {ph}")
            params.append(f"%{value}%")
        else:
            parts.append(f"{col} {op} {ph}")
            params.append(value)
    return " WHERE " + " AND ".join(parts), params


def _joins(schema, base_table, joins, ph):
    if not joins:
        return "", []
    sql, params = "", []
    for j in joins:
        jt = j.get("table", "")
        sql += f" JOIN {_table(schema, jt)} ON "
        # on 写成左右两个 "表.列"，各自校验存在
        left = _column(schema, base_table, j.get("left", ""), qualify=True)
        right = _column(schema, jt, j.get("right", ""), qualify=True)
        sql += f"{left} = {right}"
    return sql, params


def _order(schema, base_table, order_by, aliases=None):
    """排序。

    aliases：聚合结果列的别名集合，允许 "ORDER BY total_amount DESC" 这种按指标排序。
    """
    if not order_by:
        return ""
    aliases = aliases or set()
    bits = []
    for o in order_by:
        raw = str(o.get("column", ""))
        if raw in aliases:
            col = f"`{raw}`"
        else:
            col = _column(schema, base_table, raw, qualify="." in raw)
        direction = "DESC" if str(o.get("direction", "asc")).lower() == "desc" else "ASC"
        bits.append(f"{col} {direction}")
    return " ORDER BY " + ", ".join(bits)


def _limit(payload):
    try:
        n = int(payload.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise ToolError("limit 必须是整数")
    return max(1, min(n, MAX_LIMIT))


# ---------------- 解码 ----------------

def decode(payload, entry, schema):
    """把 AI 的编码请求翻译成 (sql, params)。这里是唯一生成 SQL 的地方。"""
    ph = "?" if entry["type"] == "sqlite" else "%s"

    code = str(payload.get("code", "")).strip().upper()
    if code not in ENCODING_TABLE:
        raise ToolError(f"未知的编码 {code!r}，只能用：{sorted(ENCODING_TABLE)}")

    table = payload.get("table", "")
    _table(schema, table)

    joins = payload.get("joins") or []
    aggregations = payload.get("aggregations") or []
    group_by = payload.get("group_by") or []

    if code == "Q1" and (joins or aggregations):
        raise ToolError("Q1 不支持 joins / aggregations，请改用 Q2 或 Q3")
    if code == "Q2" and not joins:
        raise ToolError("Q2 需要提供 joins")
    if code == "Q3" and (joins or group_by or not aggregations):
        raise ToolError("Q3 需要 aggregations，且不能有 joins / group_by")
    if code == "Q4" and (joins or not aggregations or not group_by):
        raise ToolError("Q4 需要 aggregations + group_by，且不能有 joins")

    # --- 选择列 ---
    aliases = set()
    if code in ("Q1", "Q2"):
        columns = payload.get("columns") or []
        if not columns:
            raise ToolError("columns 不能为空（不允许 SELECT *）")
        if code == "Q2":
            select = ", ".join(_column(schema, table, c, qualify=True) for c in columns)
        else:
            select = ", ".join(_column(schema, table, c) for c in columns)
    else:
        select_bits = []
        for a in aggregations:
            func = str(a.get("func", "")).upper()
            if func not in AGG_FUNCS:
                raise ToolError(f"不支持的聚合函数 {func!r}，可用：{sorted(AGG_FUNCS)}")
            col = a.get("column", "")
            if func == "COUNT" and str(col).strip() == "*":
                expr = "COUNT(*)"
            else:
                expr = AGG_FUNCS[func].replace("{col}", _column(schema, table, col))
            alias = str(a.get("alias") or f"{func.lower()}_{str(col).replace('.', '_')}")
            if not re_match_alias(alias):
                raise ToolError(f"别名 {alias!r} 只能由字母、数字、下划线组成")
            aliases.add(alias)
            select_bits.append(f"{expr} AS `{alias}`")

        # 分组列一并输出
        if code == "Q4":
            for g in group_by:
                select_bits.append(_column(schema, table, g))
        select = ", ".join(select_bits)

    # --- 拼装 ---
    sql = f"SELECT {select} FROM `{table}`"
    join_sql, _ = _joins(schema, table, joins, ph)
    sql += join_sql

    where_sql, where_params = _where(schema, table, payload.get("filters") or [], ph)
    sql += where_sql

    if code == "Q4":
        sql += " GROUP BY " + ", ".join(_column(schema, table, g) for g in group_by)

    sql += _order(schema, table, payload.get("order_by") or [], aliases)
    sql += f" LIMIT {_limit(payload)}"

    return sql, where_params


def re_match_alias(alias):
    """别名只允许字母数字下划线，防止把 ` 或 ; 带进 SQL。"""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", alias))


# ---------------- 执行 ----------------

def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v


def _execute(entry, sql, params, limit):
    conn = connect(entry)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(limit)
        return {
            "ok": True,
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "row_count": len(rows),
            "sql": sql,
        }
    finally:
        close(conn)


# ---------------- 工具实现 ----------------

def _pick_db(args):
    """没指定库时：只有一个数据源就自动用它，否则要求 AI 明确指定。"""
    name = (args or {}).get("db") or ""
    if name:
        return name
    if len(DB_list) == 1:
        return DB_list[0]["db"]
    raise ToolError(f"请用 db 参数指定数据库，可用：{[d['db'] for d in DB_list]}")


def tool_list_tables(args=None):
    """列出所有数据源及其表名（不执行任何 SQL）。"""
    if not DB_list:
        raise ToolError("还没有配置任何数据源，请先在设置页添加并更新结构")
    out = []
    for item in DB_list:
        _, schema = _load_target(item["db"])
        out.append({
            "db": item["db"],
            "type": item["type"],
            "tables": sorted(schema.keys()),
        })
    return {"ok": True, "databases": out}


def tool_describe_table(args):
    """列出某张表的列定义（不执行任何 SQL）。"""
    db = _pick_db(args)
    _, schema = _load_target(db)
    table = (args or {}).get("table", "")
    _table(schema, table)
    return {
        "ok": True,
        "db": db,
        "table": table,
        "columns": schema[table]["COLUMNS"],
        "primary_key": schema[table]["PRIMARY_KEY"],
    }


def tool_run_query(args):
    """完整流水线：校验参数 -> 解码 -> 安全闸 -> 执行。"""
    db = _pick_db(args)
    entry, schema = _load_target(db)

    sql, params = decode(args, entry, schema)          # ①→② 解码

    ok, reason = check_sql(sql, allowed_tables=set(schema.keys()))   # ③ 安全闸
    if not ok:
        raise ToolError(f"安全闸拒绝执行：{reason}。SQL={sql}")

    result = _execute(entry, sql, params, _limit(args))              # ④ 执行
    result["db"] = db
    result["code"] = str(args.get("code", "")).upper()
    return result


HANDLERS = {
    "list_tables": tool_list_tables,
    "describe_table": tool_describe_table,
    "run_query": tool_run_query,
}


def call_tool(name, args):
    """给 Agent 调用的统一入口，异常统一转成 {"ok": False, "error": ...}。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"未知工具 {name!r}，可用：{sorted(HANDLERS)}"}
    try:
        if not isinstance(args, dict):
            args = json.loads(args or "{}")
        return handler(args)
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------- 暴露给大模型的工具描述 ----------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出所有数据源及其表名。不确定有哪些库/表时先调这个。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "查看某张表的完整列定义（列名、类型、主键、注释）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "db": {"type": "string", "description": "数据源名称"},
                    "table": {"type": "string", "description": "表名"},
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": (
                "按【编码】执行只读查询。你不能自由写 SQL，只能选择以下编码并填参数：\n"
                "Q1=单表明细查询（columns 必填）\n"
                "Q2=多表 JOIN 明细查询（columns + joins 必填，joins 形如 "
                "[{\"table\":\"users\",\"left\":\"orders.user_id\",\"right\":\"users.user_id\"}]）\n"
                "Q3=单表聚合（aggregations 必填，如 [{\"func\":\"SUM\",\"column\":\"amount\",\"alias\":\"total\"}]）\n"
                "Q4=分组聚合（aggregations + group_by 必填，如 [\"city\"]）\n"
                "filters 形如 [{\"column\":\"status\",\"op\":\"=\",\"value\":\"paid\"}]，"
                "op 只能是 = != > >= < <= like in between is null is not null。\n"
                "结果会以表格数据返回，你要基于返回的数字回答，不要编造。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "db": {"type": "string", "description": "数据源名称，只有一个时可省略"},
                    "code": {"type": "string", "enum": list(ENCODING_TABLE)},
                    "table": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "joins": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string"},
                                "left": {"type": "string"},
                                "right": {"type": "string"},
                            },
                            "required": ["table", "left", "right"],
                        },
                    },
                    "aggregations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "func": {"type": "string", "enum": list(AGG_FUNCS)},
                                "column": {"type": "string"},
                                "alias": {"type": "string"},
                            },
                            "required": ["func", "column"],
                        },
                    },
                    "group_by": {"type": "array", "items": {"type": "string"}},
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "op": {"type": "string"},
                                "value": {},
                            },
                            "required": ["column", "op"],
                        },
                    },
                    "order_by": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "direction": {"type": "string", "enum": ["asc", "desc"]},
                            },
                            "required": ["column"],
                        },
                    },
                    "limit": {"type": "integer", "description": f"默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}"},
                },
                "required": ["code", "table"],
            },
        },
    },
]
