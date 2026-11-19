#统一化
import re
from collections import OrderedDict


# ---------------- 类型映射 ----------------

_SQLITE_TYPE_MAP = {
    "integer": "int", "int": "int", "bigint": "bigint",
    "smallint": "smallint", "tinyint": "tinyint", "mediumint": "mediumint",
    "text": "text", "varchar": "varchar", "char": "char", "clob": "longtext",
    "real": "double", "double": "double", "float": "float",
    "numeric": "decimal", "decimal": "decimal",
    "boolean": "tinyint", "bool": "tinyint",
    "blob": "blob",
    "date": "date", "datetime": "datetime",
    "timestamp": "timestamp", "time": "time",
}


def _sqlite_type_to_mysql(raw_type):
    """'INTEGER' -> 'int'，'VARCHAR(255)' -> 'varchar(255)'，空类型 -> 'text'"""
    if not raw_type or not raw_type.strip():
        return "text"
    raw_type = raw_type.strip()
    base = re.split(r"[\s(]", raw_type, 1)[0].lower()

    if base in _SQLITE_TYPE_MAP:
        mysql_base = _SQLITE_TYPE_MAP[base]
    else:
        b = base.upper()
        if "INT" in b:
            mysql_base = "int"
        elif any(k in b for k in ("CHAR", "CLOB", "TEXT")):
            mysql_base = "text"
        elif any(k in b for k in ("REAL", "FLOA", "DOUB")):
            mysql_base = "double"
        else:
            mysql_base = "decimal"

    m = re.search(r"\(([^)]*)\)", raw_type)
    return f"{mysql_base}({m.group(1)})" if m else mysql_base


def _norm_default(v):
    """SQLite 的 dflt_value 可能是 'CURRENT_TIMESTAMP' 或 "'abc'" """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.upper() == "NULL":
            return None
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
            return s[1:-1]
        return s
    return v


# ---------------- 行 -> MySQL 风格列 ----------------

def _mysql_row_to_col(row):
    # (TABLE_NAME, TABLE_COMMENT, COLUMN_NAME, COLUMN_TYPE,
    #  IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT, EXTRA)
    ctype = (row[3] or "").lower()
    data_type = ctype.split("(", 1)[0].split()[0] if ctype else ""
    return {
        "COLUMN_NAME": row[2],
        "COLUMN_TYPE": ctype,
        "DATA_TYPE": data_type,
        "IS_NULLABLE": (row[4] or "YES").upper(),
        "COLUMN_KEY": (row[5] or "").upper(),
        "COLUMN_DEFAULT": row[6],
        "COLUMN_COMMENT": row[7] or "",
        # EXTRA 可能是 'auto_increment' 或 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP'
        "EXTRA": row[8] or "",
        "_pk_order": 0,
    }


def _sqlite_row_to_col(row):
    # (table_name, column_name, column_type, notnull, dflt_value, pk)
    col_name, col_type, notnull, dflt, pk = row[1], row[2], row[3], row[4], row[5]
    mysql_type = _sqlite_type_to_mysql(col_type)
    data_type = mysql_type.split("(", 1)[0]
    is_pk = bool(pk)

    # SQLite 里 "INTEGER PRIMARY KEY" 是 rowid 别名，等价 auto_increment
    extra = "auto_increment" if is_pk and (col_type or "").strip().upper() == "INTEGER" else ""

    return {
        "COLUMN_NAME": col_name,
        "COLUMN_TYPE": mysql_type,
        "DATA_TYPE": data_type,
        "IS_NULLABLE": "NO" if (notnull or is_pk) else "YES",
        "COLUMN_KEY": "PRI" if is_pk else "",
        "COLUMN_DEFAULT": _norm_default(dflt),
        "COLUMN_COMMENT": "",
        "EXTRA": extra,
        "_pk_order": pk if pk else 9999,   # 复合主键排序用
    }


# ---------------- 主函数 ----------------

def build_schema_dict(mysql_rows=None, sqlite_rows=None):
    """
    把 MySQLdb_read() / SQLite_read() 的 fetchall() 结果合并成 MySQL 风格的 dict。

    返回:
      {
        "表名": {
          "TABLE_NAME":    str,
          "TABLE_COMMENT": str,
          "PRIMARY_KEY":   [列名, ...],
          "COLUMNS": [
            {
              "COLUMN_NAME":    str,
              "COLUMN_TYPE":    str,   # 含长度/精度，如 varchar(255)
              "DATA_TYPE":      str,   # 不含长度，如 varchar
              "IS_NULLABLE":    "YES" | "NO",
              "COLUMN_KEY":     "PRI" | "UNI" | "MUL" | "",
              "COLUMN_DEFAULT": Any,
              "COLUMN_COMMENT": str,
              "EXTRA":          str,
            }, ...
          ],
        }, ...
      }

    同时给两个来源时按列名去重，MySQL 优先。
    """
    if mysql_rows is None and sqlite_rows is None:
        raise ValueError("mysql_rows 和 sqlite_rows 至少要提供一个")

    tables = OrderedDict()

    def _ensure(tname, tcomment=""):
        if tname not in tables:
            tables[tname] = {
                "TABLE_NAME": tname,
                "TABLE_COMMENT": tcomment or "",
                "COLUMNS": [],
                "_seen": set(),
            }
        return tables[tname]

    if mysql_rows:
        for row in mysql_rows:
            t = _ensure(row[0], row[1])
            col = _mysql_row_to_col(row)
            if col["COLUMN_NAME"] in t["_seen"]:
                continue
            t["_seen"].add(col["COLUMN_NAME"])
            t["COLUMNS"].append(col)

    if sqlite_rows:
        for row in sqlite_rows:
            t = _ensure(row[0])
            col = _sqlite_row_to_col(row)
            if col["COLUMN_NAME"] in t["_seen"]:
                continue
            t["_seen"].add(col["COLUMN_NAME"])
            t["COLUMNS"].append(col)

    for t in tables.values():
        t.pop("_seen", None)
        pk_cols = sorted(
            (c for c in t["COLUMNS"] if c["COLUMN_KEY"] == "PRI"),
            key=lambda c: c["_pk_order"],
        )
        t["PRIMARY_KEY"] = [c["COLUMN_NAME"] for c in pk_cols]
        if len(pk_cols) != 1:
            # 只有单列主键才可能是 SQLite 的 rowid 别名；
            # 复合主键里的 INTEGER 只是普通整数列，不能标 auto_increment
            for c in t["COLUMNS"]:
                if c["EXTRA"] == "auto_increment":
                    c["EXTRA"] = ""
        for c in t["COLUMNS"]:
            c.pop("_pk_order", None)

    return dict(tables)


#全自动读取MySQL数据库
def MySQLdb_read(conn, dbname):
    cur = conn.cursor()
    cur.execute(
        """SELECT t.TABLE_NAME,
        t.TABLE_COMMENT,
        c.COLUMN_NAME,
        c.COLUMN_TYPE,
        c.IS_NULLABLE,
        c.COLUMN_KEY,
        c.COLUMN_DEFAULT,
        c.COLUMN_COMMENT,
        c.EXTRA
        FROM information_schema.TABLES t
        JOIN information_schema.COLUMNS c
        ON c.TABLE_SCHEMA = t.TABLE_SCHEMA
        AND c.TABLE_NAME = t.TABLE_NAME
        WHERE t.TABLE_SCHEMA = %s
        AND t.TABLE_TYPE = 'BASE TABLE'
        ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION""",(dbname,))
    db_structure = cur.fetchall()
    cur.close()
    schema = build_schema_dict(db_structure)
    return schema

def SQLite_read(conn, dbname):   #此处dbname表示数据库名，我偷懒了~
    cur = conn.cursor()
    # 注意：notnull 是 SQLite 保留字，必须写成 p."notnull" 否则语法错误
    cur.execute(
        """SELECT
m.name AS table_name,
p.name AS column_name,
p.type AS column_type,
p."notnull",
p.dflt_value,
p.pk
FROM sqlite_master AS m
        JOIN pragma_table_info(m.name) AS p
    WHERE m.type = 'table'
    AND m.name NOT LIKE 'sqlite_%'
    ORDER BY m.name, p.cid""")
    db_structure = cur.fetchall()
    cur.close()
    # 必须用关键字传参：位置参数是 mysql_rows，会把 SQLite 行交给 MySQL 解析器
    schema = build_schema_dict(sqlite_rows=db_structure)
    return schema