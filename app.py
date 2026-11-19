"""AskDB 主程序：页面路由 + 数据源展示。

安全要点：
- 敏感信息（数据库密码）在服务端就遮掉，非管理员拿到的 HTML 里根本没有真实密码。
- 管理员身份目前用「临时口令 + session」模拟，正式账号体系落地后只改 _is_admin()。
"""
import hmac
import os
import uuid

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from agent import run_agent
from chat_memory import MAX_ROUNDS as MEMORY_ROUNDS
from chat_memory import add_turn, reset as reset_memory, turns as memory_turns
from config_store import ensure_runtime_files, get_llm_config, mask_key, save_llm, save_tuning
from db_operator import DB_list, DEFAULT_PORT, add_db, get_db, get_schema, remove_db
from schema_store import (
    allow_non_admin_update,
    load_all,
    refresh_schema,
    remove_record,
    set_allow_non_admin_update,
)
from tools import reset_schema_cache

# 首次运行时把 instance/ 下缺的配置文件补齐（不会覆盖已有文件）
ensure_runtime_files()

app = Flask(__name__)
version = "0.2.0"

# session 依赖签名密钥。生产环境必须用环境变量注入，别用这里的兜底值
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-please-change-me"

# 临时管理员口令：账号体系做出来之前，用它演示「敏感信息仅管理员可见」。
# 正式登录上线后，只需要改 _is_admin() 和下面两个 admin 路由，模板一行都不用动。
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin"

# 遮罩串用固定长度，避免通过「几个点」反推密码长度
SECRET_MASK = "••••••••"


def _is_admin():
    return bool(session.get("is_admin"))


def _sid():
    """给每个浏览器会话一个稳定 id，用来索引服务端的对话记忆。"""
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    return sid


def _db_rows(show_secret):
    """把 DB_list 转成模板可直接渲染的行。

    权限判断只在这一处做：非管理员时，真实密码压根不进渲染数据。
    """
    rows = []
    for item in DB_list:
        if item["type"] == "sqlite":
            address = item["path"]
            user = "—"
            has_secret = False
            secret = ""
        else:
            address = f'{item["host"]}:{item["port"]}/{item["database"]}'
            user = item["user"]
            has_secret = True
            secret = item["password"] if show_secret else SECRET_MASK

        rows.append({
            "db": item["db"],
            "type": item["type"],
            "address": address,
            "user": user,
            "has_secret": has_secret,
            "masked": has_secret and not show_secret,
            "secret": secret,
            "note": item.get("note", ""),
        })
    return rows


@app.route("/ping")
def ping():
    return "pong"


@app.route("/")
def index():
    return render_template(
        "index.html",
        title="问数 AskDB",
        version=version,
        turns=memory_turns(_sid()),
        max_rounds=MEMORY_ROUNDS,
    )


@app.route("/ask", methods=["POST"])
def ask():
    """提一个新问题：带上最近几轮历史，跑完写回记忆，然后重定向（避免刷新重复提交）。"""
    question = (request.form.get("q") or "").strip()
    if not question:
        flash("请输入问题", "error")
        return redirect(url_for("index"))

    sid = _sid()
    history = memory_turns(sid)
    try:
        answer, usage, trace = run_agent(question, history=history)
    except Exception as e:
        app.logger.exception("问答失败")
        flash(f"问答失败：{type(e).__name__}: {e}", "error")
        return redirect(url_for("index"))

    add_turn(sid, question, answer, trace, usage)
    return redirect(url_for("index"))


@app.route("/reset", methods=["POST"])
def reset():
    """手动清空对话记忆。"""
    reset_memory(_sid())
    flash("对话已清空，可以重新开始问", "ok")
    return redirect(url_for("index"))


@app.route("/settings")
def settings():
    admin = _is_admin()
    allow_guest_update = allow_non_admin_update()
    rows = _db_rows(admin)

    # 把「结构缓存」的状态贴到每一行，模板据此显示「已生成 / 未生成」
    records = load_all()
    for row in rows:
        rec = records.get(row["db"])
        if rec:
            row["has_schema"] = True
            row["schema_info"] = f"{rec.get('table_count', '?')} 张表 · {rec.get('generated_at', '')}"
            row["dialect"] = rec.get("dialect", "")
        else:
            row["has_schema"] = False
            row["schema_info"] = "未生成"
            row["dialect"] = ""

    # 注意：这里只传「遮罩后的 key」，真实 key 永远不进模板
    llm = get_llm_config()
    llm_view = {
        "configured": bool((llm.get("api_key") or "").strip()),
        "masked": mask_key(llm.get("api_key")),
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "timeout": llm.get("timeout", 90),
        "temperature": llm.get("temperature", 0.2),
        "max_rounds": llm.get("max_rounds", 4),
    }

    return render_template(
        "settings.html",
        db_rows=rows,
        is_admin=admin,
        version=version,
        admin_default_warning=not os.environ.get("ADMIN_PASSWORD"),
        # 能不能点「更新结构」：管理员永远可以；非管理员取决于开关
        can_update=admin or allow_guest_update,
        allow_guest_update=allow_guest_update,
        schema_file="instance/schema_cache.json",
        llm=llm_view,
    )


@app.route("/settings/test", methods=["POST"])
def settings_test():
    """测试单个数据源能否连上并读到结构。"""
    name = request.form.get("db", "").strip()
    entry = get_db(name)
    if entry is None:
        flash(f"找不到数据源：{name or '(空)'}", "error")
        return redirect(url_for("settings"))
    try:
        schema = get_schema(entry)
    except Exception as e:  # 驱动报错五花八门，统一展示，细节留给控制台
        app.logger.exception("测试连接失败：%s", name)
        flash(f"{name} 连接失败：{type(e).__name__}: {e}", "error")
    else:
        flash(f"{name} 连接正常，共读到 {len(schema)} 张表", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/db/add", methods=["POST"])
def settings_db_add():
    """新增数据源：表单 -> add_db() -> 落盘 instance/db_list.json。"""
    if not _is_admin():
        flash("只有管理员能新增数据源", "error")
        return redirect(url_for("settings"))

    kind = (request.form.get("type") or "").strip().lower()
    note = request.form.get("note") or ""

    if kind == "sqlite":
        # SQLite 只传 path，传别的字段 add_db 会直接报错（故意的，避免误解配置生效）
        kwargs = {"path": request.form.get("path") or None, "note": note}
    else:
        kwargs = {
            "host": request.form.get("host") or None,
            "port": request.form.get("port") or DEFAULT_PORT,
            "user": request.form.get("user") or None,
            "password": request.form.get("password") or "",
            "database": request.form.get("database") or None,
            "charset": request.form.get("charset") or "utf8mb4",
            "note": note,
        }

    try:
        entry = add_db(request.form.get("db", ""), kind, **kwargs)
    except (ValueError, TypeError) as e:
        flash(f"添加失败：{e}", "error")
    else:
        flash(f"已添加数据源：{entry['db']}（{entry['type']}）", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/db/remove", methods=["POST"])
def settings_db_remove():
    """删除数据源，连同它的结构缓存一起清掉。"""
    if not _is_admin():
        flash("只有管理员能删除数据源", "error")
        return redirect(url_for("settings"))

    name = request.form.get("db", "").strip()
    if not remove_db(name):
        flash(f"找不到数据源：{name or '(空)'}", "error")
        return redirect(url_for("settings"))

    if remove_record(name):
        flash(f"已删除数据源「{name}」及其结构缓存", "ok")
    else:
        flash(f"已删除数据源「{name}」", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/db/update", methods=["POST"])
def settings_db_update():
    """读结构 → 调大模型改写成 AI 友好文本 → 覆盖式保存。"""
    if not (_is_admin() or allow_non_admin_update()):
        flash("没有权限更新结构：请联系管理员在设置页开放", "error")
        return redirect(url_for("settings"))

    name = request.form.get("db", "").strip()
    entry = get_db(name)
    if entry is None:
        flash(f"找不到数据源：{name or '(空)'}", "error")
        return redirect(url_for("settings"))

    try:
        record = refresh_schema(entry)   # 慢操作：要连库 + 调大模型
        reset_schema_cache()             # 结构变了，工具层的校验缓存必须失效
    except Exception as e:
        app.logger.exception("更新结构失败：%s", name)
        flash(f"{name} 结构更新失败：{type(e).__name__}: {e}", "error")
    else:
        flash(
            f"{name} 结构已更新：{record['table_count']} 张表 / "
            f"{record['dialect']} / {record['tokens']} tokens",
            "ok",
        )
    return redirect(url_for("settings"))


@app.route("/settings/permission", methods=["POST"])
def settings_permission():
    """管理员开关：是否允许非管理员也点「更新结构」。"""
    if not _is_admin():
        flash("只有管理员能修改这个开关", "error")
        return redirect(url_for("settings"))

    allow = request.form.get("allow_non_admin_update") == "on"
    set_allow_non_admin_update(allow)
    flash("已开放：非管理员也能更新结构" if allow else "已收紧：只有管理员能更新结构", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/llm", methods=["POST"])
def settings_llm():
    """保存模型连接信息（仅管理员）。api_key 留空表示保持原值。"""
    if not _is_admin():
        flash("只有管理员能修改模型连接配置", "error")
        return redirect(url_for("settings"))
    try:
        save_llm({
            "api_key": request.form.get("api_key", ""),
            "base_url": request.form.get("base_url", ""),
            "model": request.form.get("model", ""),
            "timeout": request.form.get("timeout", ""),
        })
    except ValueError as e:
        flash(f"保存失败：{e}", "error")
    else:
        flash("模型配置已保存，立即生效（无需重启）", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/llm/tuning", methods=["POST"])
def settings_llm_tuning():
    """保存生成参数（temperature / 最大轮数），所有使用者都可以调。"""
    try:
        save_tuning({
            "temperature": request.form.get("temperature", ""),
            "max_rounds": request.form.get("max_rounds", ""),
        })
    except ValueError as e:
        flash(f"保存失败：{e}", "error")
    else:
        flash("生成参数已保存", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/admin", methods=["POST"])
def settings_admin():
    pwd = request.form.get("password", "")
    # 先编码成 bytes：hmac.compare_digest 不接受含非 ASCII 的 str（中文口令会 TypeError）
    if hmac.compare_digest(pwd.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        session["is_admin"] = True
        flash("已进入管理员模式，可查看敏感信息", "ok")
    else:
        flash("口令错误", "error")
    return redirect(url_for("settings"))


@app.route("/settings/admin/exit", methods=["POST"])
def settings_admin_exit():
    session.pop("is_admin", None)
    flash("已退出管理员模式", "ok")
    return redirect(url_for("settings"))


if __name__ == '__main__':
    app.run(host="127.0.0.1", port=5000, debug=True)
