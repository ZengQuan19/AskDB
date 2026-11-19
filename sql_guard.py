"""SQL 安全闸：解码之后的第二道检查。

为什么需要它？因为「编码表」只能保证**语句骨架**是安全的，
但 AI 依然可能被恶意提示词操纵，比如：
  - 把表名写成 `orders; DROP TABLE users`（如果解码器不够严）
  - 在值里塞 `1=1 UNION SELECT ...`（如果拼接而不是参数化）
所以这里对**最终要执行的 SQL 文本**再做一次与 AI 完全无关的规则检查。

设计原则：
1. 白名单优先 —— 只认 SELECT；
2. 宁可误杀，不可放过 —— 所有规则失败即拒绝，不做"修复后继续"；
3. 每条拒绝都要给出人能读懂的原因，方便排查。
"""
import re

# 出现即拒绝的关键字（词边界匹配，不会误伤 created_at 这种）
FORBIDDEN_WORDS = {
    # 写操作
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "replace", "rename", "merge", "upsert",
    # 权限 / 管理
    "grant", "revoke", "shutdown", "kill", "flush", "reset", "purge",
    # 执行 / 文件
    "call", "exec", "execute", "prepare", "handler", "load_file", "outfile",
    "dumpfile", "load", "into", "lock", "unlock",
    # 系统库
    "information_schema", "performance_schema", "mysql", "sys",
    # 危险函数 / 延时注入
    "sleep", "benchmark", "pg_sleep", "waitfor", "sys_eval", "xp_cmdshell",
    # 会话控制
    "set", "use", "declare", "begin", "commit", "rollback",
}

# 集合运算：安全模板永远不会产生，出现即视为攻击
FORBIDDEN_SET_OPS = ("union", "intersect", "except")

MAX_SQL_LENGTH = 20000


def _fail(reason):
    return False, reason


def extract_tables(sql):
    """粗略抽出 SQL 里引用的表名，用于白名单比对。

    只处理 FROM / JOIN 后面紧跟的标识符（可能带反引号或库名前缀）。
    """
    names = set()
    pattern = re.compile(
        r"\b(?:from|join)\s+(`?[\w$]+`?(?:\.`?[\w$]+`?)?)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(sql):
        raw = m.group(1).strip("`")
        # 去掉库名前缀：practice.orders -> orders
        names.add(raw.split(".")[-1].strip("`"))
    return names


def check_sql(sql, allowed_tables=None, require_limit=True):
    """检查一条 SQL 是否允许执行，返回 (ok, reason)。

    allowed_tables: 可访问表名的集合；None 表示不检查表名（不建议）。
    """
    if not sql or not sql.strip():
        return _fail("SQL 为空")

    sql = sql.strip()

    if len(sql) > MAX_SQL_LENGTH:
        return _fail(f"SQL 过长（{len(sql)} 字符，上限 {MAX_SQL_LENGTH}）")

    # 1. 注释：注释是绕过关键字检测最常用的手段
    if "--" in sql or "/*" in sql or "*/" in sql:
        return _fail("不允许 SQL 注释（-- 或 /* */）")
    if "#" in sql:
        return _fail("不允许 # 注释")

    # 2. 单语句：去掉结尾分号后不允许再出现分号
    body = sql.rstrip().rstrip(";").strip()
    if ";" in body:
        return _fail("只允许单条语句（检测到多余的分号）")
    if not body:
        return _fail("SQL 为空")

    # 3. 必须 SELECT 开头
    if not re.match(r"^select\b", body, re.IGNORECASE):
        return _fail("只允许以 SELECT 开头的查询语句")

    # 4. 禁词
    lowered = body.lower()
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return _fail(f"包含禁止关键字：{word}")

    # 5. 集合运算
    for op in FORBIDDEN_SET_OPS:
        if re.search(rf"\b{op}\b", lowered):
            return _fail(f"包含禁止的集合运算：{op.upper()}")

    # 6. 禁止通配符取列：SELECT * / SELECT a, * / SELECT t.*
    if re.search(r"\bselect\s+\*", lowered) or re.search(r",\s*\*\s*(,|\bfrom\b)", lowered):
        return _fail("不允许 SELECT *（请显式列出需要的列）")
    if re.search(r"\b[\w$`]+\.\*", lowered):
        return _fail("不允许通配符取列（如 t.*）")
    # COUNT(*) 是合法的，单独放行：上面两条正则不会命中它

    # 7. 表名白名单
    if allowed_tables is not None:
        used = extract_tables(body)
        if not used:
            return _fail("无法识别查询涉及的表名")
        unknown = used - set(allowed_tables)
        if unknown:
            return _fail(f"访问了未授权的表：{', '.join(sorted(unknown))}")

    # 8. LIMIT
    if require_limit and not re.search(r"\blimit\b", lowered):
        return _fail("缺少 LIMIT，拒绝全表扫描")

    return True, ""


def ensure_limit(sql, default=100):
    """没有 LIMIT 时补一个，纯兜底（正常路径由工具模板生成）。"""
    if re.search(r"\blimit\b", sql, re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')} LIMIT {int(default)}"


if __name__ == "__main__":
    cases = [
        ("SELECT id, amount FROM orders LIMIT 10", True),
        ("SELECT * FROM orders LIMIT 10", False),
        ("SELECT o.* FROM orders o LIMIT 10", False),
        ("SELECT COUNT(*) FROM orders LIMIT 10", True),
        ("DROP TABLE orders", False),
        ("SELECT 1; DROP TABLE orders", False),
        ("SELECT 1 -- 注释", False),
        ("SELECT 1 UNION SELECT password FROM users", False),
        ("SELECT id FROM orders", False),          # 没有 LIMIT
        ("SELECT id FROM orders LIMIT 10;", True),
        ("SELECT id FROM secret_table LIMIT 10", False),
    ]
    ok_all = True
    for sql, expect in cases:
        ok, reason = check_sql(sql, allowed_tables={"orders", "users"})
        mark = "OK  " if ok == expect else "FAIL"
        if ok != expect:
            ok_all = False
        print(f"{mark} {str(ok):5s} {sql[:48]:50s} {reason}")
    print("\nRESULT:", "ALL PASS" if ok_all else "SOME FAILED")
