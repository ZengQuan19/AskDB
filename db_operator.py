"""数据源管理：登记、校验并保存数据库连接信息。

DB_list 是全局唯一的数据源清单，add_db() / remove_db() 是唯一写入口；
每次变更都会同步落盘到 instance/db_list.json，进程重启后自动装载。
后续读结构（db_reader）和执行查询，都从这里取连接参数。

条目统一是 dict，形如：
    {"db": "订单库", "type": "sqlite", "path": "C:/data/shop.db", "note": ""}
    {"db": "订单库", "type": "mysql", "host": "127.0.0.1", "port": 3306,
     "user": "root", "password": "***", "database": "shop", "charset": "utf8mb4", "note": ""}

注意：密码目前以**明文**存在 instance/db_list.json 里（该目录已被 .gitignore 排除），
加密留到接入正式登录时一起做。
"""
import json
import os
from pathlib import Path

from db_reader import MySQLdb_read, SQLite_read

# 全局数据源清单，add_db() 是唯一写入口
DB_list = []

SUPPORTED_TYPES = ("sqlite", "mysql")

DEFAULT_PORT = 3306

INSTANCE_DIR = Path(__file__).resolve().parent / "instance"
DBS_FILE = INSTANCE_DIR / "db_list.json"


# ---------------- 校验小工具（只负责报错，不返回 True/False） ----------------

def _fail(msg):
    raise ValueError(f"[add_db] {msg}")


def _check_name(db):
    """名字：非空字符串，且不能与已登记的重名。"""
    if not isinstance(db, str) or not db.strip():
        _fail("数据库名字 db 必须是非空字符串")
    name = db.strip()
    if any(item["db"] == name for item in DB_list):
        _fail(f"数据库名字 {name!r} 已存在，请换个名字")
    return name


def _check_type(db_type):
    """类型：只认 sqlite / mysql，大小写不敏感。"""
    if not isinstance(db_type, str):
        _fail(f"数据库类型 db_type 必须是字符串，收到 {type(db_type).__name__}")
    t = db_type.strip().lower()
    if t not in SUPPORTED_TYPES:
        _fail(f"不支持的数据库类型 {db_type!r}，目前只支持 {SUPPORTED_TYPES}")
    return t


def _check_port(port):
    """端口：能转成 int 且落在 1-65535。允许传字符串（表单提交就是这个类型）。"""
    try:
        p = int(port)
    except (TypeError, ValueError):
        _fail(f"端口 port 必须是整数，收到 {port!r}")
    if not 1 <= p <= 65535:
        _fail(f"端口 port 超出范围 1-65535：{p}")
    return p


def _check_sqlite_path(path):
    """SQLite 文件：必须存在。

    故意不自动建库——sqlite3.connect() 遇到不存在的路径会悄悄创建一个空库，
    之后读结构会返回一片空白，比直接报错难排查得多。
    """
    if not path:
        _fail("SQLite 必须提供文件路径 path")
    p = Path(str(path)).expanduser().resolve()
    if not p.is_file():
        _fail(f"SQLite 文件不存在：{p}")
    return str(p)


def _require(value, field):
    """MySQL 必填项检查。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        _fail(f"MySQL 必须提供 {field}")
    return value.strip() if isinstance(value, str) else value


# ---------------- 主函数 ----------------

def add_db(db, db_type, *, path=None, host=None, port=DEFAULT_PORT,
           user=None, password=None, database=None, charset="utf8mb4", note=""):
    """登记一个数据源，成功后追加到 DB_list 并返回该条目。

    必填：
        db       数据库在 AskDB 里的显示名（唯一）
        db_type  "sqlite" 或 "mysql"

    SQLite 专用：
        path     数据库文件路径，必须已存在

    MySQL 专用：
        host, user, database   必填
        port     默认 3306
        password 默认 None（无密码账号），注意目前是明文存在内存里
        charset  默认 utf8mb4

    通用：
        note     备注，随便写

    参数给错（缺字段、给错类型的字段、重名）一律抛 ValueError，不会留下半条脏数据。
    """
    entry = _build_entry(db, db_type, path=path, host=host, port=port,
                         user=user, password=password, database=database,
                         charset=charset, note=note)
    DB_list.append(entry)
    _save_db_list()
    return entry


def _build_entry(db, db_type, *, path=None, host=None, port=DEFAULT_PORT,
                 user=None, password=None, database=None, charset="utf8mb4", note=""):
    """只做校验并构造条目，不改 DB_list、不落盘（供 add_db 与装载复用）。"""
    name = _check_name(db)
    kind = _check_type(db_type)

    if kind == "sqlite":
        if host is not None or user is not None or password is not None or database is not None:
            _fail("SQLite 数据源不需要 host/user/password/database，请只提供 path")
        return {
            "db": name,
            "type": "sqlite",
            "path": _check_sqlite_path(path),
            "note": note,
        }

    if path is not None:
        _fail("MySQL 数据源不需要 path，请提供 host/port/user/database")
    return {
        "db": name,
        "type": "mysql",
        "host": _require(host, "host"),
        "port": _check_port(port),
        "user": _require(user, "user"),
        "password": password if password is not None else "",
        "database": _require(database, "database"),
        "charset": charset or "utf8mb4",
        "note": note,
    }


def remove_db(name):
    """按名字删掉数据源并落盘，返回是否真的删了。"""
    for i, item in enumerate(DB_list):
        if item["db"] == name:
            DB_list.pop(i)
            _save_db_list()
            return True
    return False


# ---------------- 落盘 / 装载 ----------------

def _save_db_list():
    """原子写：先写 .tmp 再替换，避免中途失败留下半个 JSON。"""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DBS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"databases": DB_list}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DBS_FILE)


def load_db_list():
    """启动时把 instance/db_list.json 读回内存。

    单条坏了只跳过并提示，不让整个应用起不来
    （比如 SQLite 文件被删了，那条就装载失败）。
    """
    try:
        with open(DBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return DB_list
    except (json.JSONDecodeError, OSError) as e:
        print(f"[db_operator] 读取 {DBS_FILE} 失败，忽略：{e}")
        return DB_list

    for item in data.get("databases", []):
        try:
            entry = _build_entry(
                item.get("db"),
                item.get("type"),
                path=item.get("path"),
                host=item.get("host"),
                port=item.get("port", DEFAULT_PORT),
                user=item.get("user"),
                password=item.get("password"),
                database=item.get("database"),
                charset=item.get("charset"),
                note=item.get("note") or "",
            )
        except (ValueError, TypeError) as e:
            print(f"[db_operator] 跳过无效数据源 {item.get('db')!r}：{e}")
            continue
        DB_list.append(entry)      # 装载时不再回写文件
    return DB_list


def get_db(name):
    """按名字取数据源条目，取不到返回 None。"""
    for item in DB_list:
        if item["db"] == name:
            return item
    return None


# ---------------- 连接 ----------------

def _connect_sqlite(entry, read_only):
    import sqlite3

    # 用 as_uri() 而不是字符串拼接：它会正确转义空格、中文和盘符
    uri = Path(entry["path"]).as_uri()
    if read_only:
        # mode=ro：SQLite 自己就会拒绝写操作，比靠 SQL 关键字过滤可靠
        uri += "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(entry["path"])


def _connect_mysql(entry, read_only):
    try:
        import pymysql
    except ImportError as e:
        raise RuntimeError(
            "缺少 MySQL 驱动，请先执行：pip install pymysql"
        ) from e

    conn = pymysql.connect(
        host=entry["host"],
        port=entry["port"],
        user=entry["user"],
        password=entry["password"],
        database=entry["database"],
        charset=entry.get("charset") or "utf8mb4",
        connect_timeout=5,
        read_timeout=30,
    )
    if read_only:
        # 应用层再加一道闸：本会话不允许写。老版本 MySQL 不支持时忽略
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception:
            pass
    return conn


def connect(entry, read_only=True):
    """按数据源条目建立连接。

    read_only 默认 True：SQLite 走 mode=ro，MySQL 执行 SET SESSION READ ONLY。
    调用方负责 close()，推荐用 get_schema() 这种自带 finally 的封装。
    """
    if not isinstance(entry, dict) or "type" not in entry:
        raise ValueError("[connect] 需要 add_db() 登记出来的数据源条目")
    if entry["type"] == "sqlite":
        return _connect_sqlite(entry, read_only)
    if entry["type"] == "mysql":
        return _connect_mysql(entry, read_only)
    raise ValueError(f"[connect] 未知的数据源类型：{entry['type']!r}")


def close(conn):
    """安静地关闭连接，不掩盖原始异常。"""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def get_schema(entry):
    """连接 → 读结构 → 关闭，返回 db_reader 那套统一 dict。"""
    conn = connect(entry)
    try:
        if entry["type"] == "sqlite":
            # SQLite_read 的第二个参数只是占位（它读整个文件），传名字便于日志区分
            return SQLite_read(conn, entry["db"])
        return MySQLdb_read(conn, entry["database"])
    finally:
        close(conn)


# 模块导入时就把上次保存的数据源读回内存（文件不存在则静默跳过）
load_db_list()


if __name__ == "__main__":
    # 直接运行本文件可自测（PyCharm 里右键 Run 即可）
    # 注意：本示例会把两条临时数据源写进 instance/db_list.json，跑完自动撤掉。
    import sqlite3
    import tempfile

    demo_path = Path(tempfile.mkdtemp()) / "demo.db"
    _c = sqlite3.connect(demo_path)
    _c.executescript(
        "CREATE TABLE t_user (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL);"
        "CREATE TABLE t_order (id INTEGER PRIMARY KEY, amount DECIMAL(10,2), status VARCHAR(16));"
    )
    _c.commit()
    _c.close()

    print("登记数据源：")
    print(" ", add_db("演示库", "sqlite", path=str(demo_path), note="自测生成的临时库"))
    print(" ", add_db("本地MySQL", "MySQL", host="127.0.0.1", port="3306",
                      user="root", password="", database="shop"))
    print(f"\nDB_list 共 {len(DB_list)} 条\n")

    print("读取「演示库」的结构：")
    schema = get_schema(get_db("演示库"))
    for tname, t in schema.items():
        cols = ", ".join(c["COLUMN_NAME"] for c in t["COLUMNS"])
        print(f"  {tname}  主键={t['PRIMARY_KEY']}  列=[{cols}]")

    remove_db("演示库")
    remove_db("本地MySQL")
    print("\n已清理示例数据源，当前 DB_list：", DB_list)

