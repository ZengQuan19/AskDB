"""大模型调用封装。

配置不再写死在 llm_config.py 里，而是每次调用时从 config_store 读取
（instance/config.json），所以在网页上改完设置立即生效，不用重启。
"""
import requests

from config_store import get_llm_config

# 常见错误码 -> 人话
ERROR_HINTS = {
    400: "请求格式错误（可能是模型不支持工具调用，或模型名写错）",
    401: "API Key 错误或未配置",
    402: "余额不足",
    404: "接口地址不对（检查 Base URL / 模型名）",
    429: "请求太频繁",
    500: "服务端错误",
    503: "服务不可用",
}


def chat(messages, temperature=None, debug=False, tools=None):
    """向大模型提问。

    temperature 传 None 时使用网页里配置的值；显式传值则覆盖（比如结构改写固定用 0.1）。

    返回 (text, usage, tool_calls)：
      text       模型回复的文本（可能为空串，比如它只想调工具时）
      usage      token 用量
      tool_calls 模型请求调用的工具列表，没有就是 []
    """
    cfg = get_llm_config()

    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("还没有配置 API Key：请到「设置 → 模型连接」里填写后保存")

    base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    model = (cfg.get("model") or "deepseek-chat").strip()
    timeout = int(cfg.get("timeout") or 90)
    if temperature is None:
        temperature = float(cfg.get("temperature", 0.2))

    payload = {
        "model": model,
        "temperature": temperature,
        "stream": False,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    if debug:
        print("[llm] ->", {k: v for k, v in payload.items() if k != "messages"})

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if resp.status_code != 200:
        hint = ERROR_HINTS.get(resp.status_code, "未知错误")
        raise RuntimeError(f"{hint}（HTTP {resp.status_code}）：{resp.text[:300]}")

    data = resp.json()
    message = data["choices"][0]["message"]

    if debug:
        print("[llm] <-", message)

    return (
        message.get("content") or "",
        data.get("usage", {}),
        message.get("tool_calls") or [],
    )
