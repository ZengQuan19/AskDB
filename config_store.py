"""运行时配置中心：确保配置文件存在，并提供读写。

设计：
- 所有运行期配置都放在 instance/ 下（该目录已被 .gitignore 排除，不会误提交）。
- **首次运行自动创建**：config.json / db_list.json / schema_cache.json / app_settings.json
  缺哪个补哪个，不会覆盖已有内容。
- 兼容旧的 llm_config.py：如果 instance/config.json 还不存在、而 llm_config.py 在，
  会把里面的 API_KEY / BASE_URL / MODEL 迁移进来（迁移一次，之后以 config.json 为准）。

为什么要迁移？因为早期版本的密钥写在 llm_config.py 里，直接换成新方案会让人以为配置丢了。
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"

CONFIG_FILE = INSTANCE_DIR / "config.json"
DB_LIST_FILE = INSTANCE_DIR / "db_list.json"
SCHEMA_CACHE_FILE = INSTANCE_DIR / "schema_cache.json"
APP_SETTINGS_FILE = INSTANCE_DIR / "app_settings.json"
LEGACY_LLM_CONFIG = BASE_DIR / "llm_config.py"

DEFAULT_CONFIG = {
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.2,
        "max_rounds": 4,
        "timeout": 90,
    },
    # 服务端并发控制：问答异步任务队列读取这些值（见 async_tasks.py）
    "server": {
        "max_concurrent": 4,      # 同时执行几个问答
        "queue_size": 20,         # 额外允许排队的数量
        "per_session_limit": 2,   # 单个会话最多几个未完成任务
        "result_ttl": 600,        # 完成任务保留秒数（前端轮询用）
    },
}

# 约束：网页上改的值必须落在这个区间，避免填出 999 度这种灾难
LIMITS = {
    "temperature": (0.0, 2.0),
    "max_rounds": (1, 8),
    "timeout": (5, 300),
}


# ---------------- 文件工具 ----------------

def _write_json(path, data):
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


# ---------------- 迁移旧配置 ----------------

def _legacy_llm_values():
    """从旧的 llm_config.py 读取值；读不到就返回空 dict（绝不抛异常）。"""
    if not LEGACY_LLM_CONFIG.exists():
        return {}
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_legacy_llm_config", LEGACY_LLM_CONFIG)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)     # 旧文件可能 raise SystemExit，所以用 BaseException 兜
    except BaseException:
        return {}

    return {
        "api_key": str(getattr(module, "API_KEY", "") or "").strip(),
        "base_url": str(getattr(module, "BASE_URL", "") or "").strip(),
        "model": str(getattr(module, "MODEL", "") or "").strip(),
    }


# ---------------- 主流程 ----------------

def ensure_runtime_files():
    """缺什么补什么。已存在的文件一律不动。"""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        config = json.loads(json.dumps(DEFAULT_CONFIG))     # 深拷贝
        legacy = _legacy_llm_values()
        for key in ("api_key", "base_url", "model"):
            if legacy.get(key):
                config["llm"][key] = legacy[key]
        _write_json(CONFIG_FILE, config)

    if not DB_LIST_FILE.exists():
        _write_json(DB_LIST_FILE, {"databases": []})
    if not SCHEMA_CACHE_FILE.exists():
        _write_json(SCHEMA_CACHE_FILE, {"databases": {}})
    if not APP_SETTINGS_FILE.exists():
        _write_json(APP_SETTINGS_FILE, {"allow_non_admin_update": False})


def load_config():
    """读配置，并把缺失的键用默认值补齐（升级后加了新字段也不至于 KeyError）。"""
    ensure_runtime_files()
    data = _read_json(CONFIG_FILE, {})
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for section, values in (data or {}).items():
        if isinstance(values, dict) and section in merged:
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def get_llm_config():
    return load_config().get("llm", {})


# ---------------- 写入 ----------------

def _coerce_number(name, value):
    lo, hi = LIMITS[name]
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字，收到 {value!r}")
    if not (lo <= num <= hi):
        raise ValueError(f"{name} 必须在 {lo} ~ {hi} 之间，收到 {num}")
    return int(num) if name in ("max_rounds", "timeout") else round(num, 2)


def save_llm(fields):
    """保存模型连接信息（管理员）。api_key 留空表示「保持原值不变」。"""
    config = load_config()
    llm = config["llm"]

    api_key = (fields.get("api_key") or "").strip()
    if api_key:                       # 留空 = 不改
        llm["api_key"] = api_key

    base_url = (fields.get("base_url") or "").strip()
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        llm["base_url"] = base_url.rstrip("/")

    model = (fields.get("model") or "").strip()
    if model:
        llm["model"] = model

    if fields.get("timeout") not in (None, ""):
        llm["timeout"] = _coerce_number("timeout", fields["timeout"])

    _write_json(CONFIG_FILE, config)
    return llm


def save_tuning(fields):
    """保存生成参数（所有人可用）。"""
    config = load_config()
    llm = config["llm"]

    for name in ("temperature", "max_rounds"):
        if fields.get(name) not in (None, ""):
            llm[name] = _coerce_number(name, fields[name])

    _write_json(CONFIG_FILE, config)
    return llm


def mask_key(key):
    """只露出尾部 4 位，用于页面展示。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}...{key[-4:]}"


if __name__ == "__main__":
    ensure_runtime_files()
    cfg = load_config()
    print("配置文件:", CONFIG_FILE)
    print("已存在:", CONFIG_FILE.exists())
    print("模型:", cfg["llm"]["model"], "| temperature:", cfg["llm"]["temperature"])
    print("API Key:", mask_key(cfg["llm"]["api_key"]) or "(未配置)")
