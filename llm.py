from unittest import case

import requests
import json
from llm_config import BASE_URL, API_KEY, MODEL

TIMEOUT = 60
#向ai提问
def chat(message, temperature=0.2, debug=False):
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MODEL,
        "temperature": temperature,
        "stream": False,
        "messages": message
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)

    Error_hints = {401: "key 错误或为空", 402: "余额不足", 429: "请求太频繁"}

    if resp.status_code != 200:
        hint = Error_hints.get(resp.status_code, "未知错误")
        raise RuntimeError(f"{hint}（HTTP {resp.status_code}）：{resp.text[:200]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage",{})
    return text, usage