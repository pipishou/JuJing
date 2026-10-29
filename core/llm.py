"""LLM 通道（零依赖，stdlib urllib）。

设计口径（MVP 定案 B1）：bot-team 自己直连 provider，不借 octop、不依赖 DSH 会话。

配置来源（优先级从高到低）：
  1) 环境变量 BOT_LLM_PROVIDER / BOT_LLM_MODEL / BOT_LLM_KEY / BOT_LLM_BASE
  2) env 文件（默认 <bot-team>/.env.local，可用 BOT_LLM_ENV_FILE 指定）
  3) 各 provider 的默认 key 环境变量（DEEPSEEK_API_KEY / BIGMODEL_API_KEY / DSH_OPENCODE_GO_KEY）

provider 预设（已实测 200）：
  opencode  https://opencode.ai/inference/go/v1   key 前缀 oc_sk_  模型 deepseek-v4.1-flash 等 30 个
  deepseek  https://api.deepseek.com/v1           key 前缀 sk-     模型 deepseek-chat
  bigmodel  https://open.bigmodel.cn/api/coding/paas/v4  key BM      模型 glm-5.3-flash

用法：
  python -B core/llm.py --check                 # 只验通（GET /models）
  python -B core/llm.py --models                # 列可用模型
  python -B core/llm.py --prompt "说一句话"      # 跑一次 chat
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env.local"

PROVIDERS = {
    "opencode": {
        "base": "https://opencode.ai/inference/go/v1",
        "key_envs": ("BOT_LLM_KEY", "DSH_OPENCODE_GO_KEY"),
        "model": "deepseek-v4.1-flash",
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_envs": ("DEEPSEEK_API_KEY", "BOT_LLM_KEY"),
        "model": "deepseek-chat",
    },
    "bigmodel": {
        "base": "https://open.bigmodel.cn/api/coding/paas/v4",
        "key_envs": ("BIGMODEL_API_KEY", "BOT_LLM_KEY"),
        "model": "glm-5.3-flash",
    },
}


class LLMError(RuntimeError):
    """配置或调用失败。"""


def load_env_file(path: pathlib.Path | None = None) -> dict[str, str]:
    """读 KEY=VALUE 文件；不存在返回空 dict。不覆盖已有环境变量。"""
    p = pathlib.Path(path or os.environ.get("BOT_LLM_ENV_FILE") or DEFAULT_ENV_FILE)
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _lookup(name: str, file_env: dict[str, str]) -> str | None:
    return os.environ.get(name) or file_env.get(name) or None


def config(provider: str | None = None) -> dict:
    """解析出 {provider, base, model, key, key_source}。key 缺失即抛 LLMError（fail closed）。"""
    file_env = load_env_file()
    name = provider or _lookup("BOT_LLM_PROVIDER", file_env) or "opencode"
    name = name.strip().lower()
    if name not in PROVIDERS:
        raise LLMError(f"未知 provider：{name}（可选 {', '.join(PROVIDERS)}）")
    preset = PROVIDERS[name]

    key = None
    key_source = None
    for env_name in preset["key_envs"]:
        v = _lookup(env_name, file_env)
        if v:
            key = v
            key_source = env_name
            break
    if not key:
        raise LLMError(
            f"没有拿到 {name} 的 key。请写 {DEFAULT_ENV_FILE}：\n"
            f"  BOT_LLM_PROVIDER={name}\n  BOT_LLM_MODEL={preset['model']}\n  BOT_LLM_KEY=<你的 key>"
        )

    base = (_lookup("BOT_LLM_BASE", file_env) or preset["base"]).rstrip("/")

    # 模型：provider 专属变量 > （同 provider 时）BOT_LLM_MODEL > 预设
    configured_provider = (_lookup("BOT_LLM_PROVIDER", file_env) or "opencode").strip().lower()
    model = _lookup(f"BOT_LLM_MODEL_{name.upper()}", file_env)
    if not model and name == configured_provider:
        model = _lookup("BOT_LLM_MODEL", file_env)
    model = model or preset["model"]

    headers = {}
    if name == "opencode":
        # opencode-go 要求带会话头做路由；缺了直接 400 MissingSessionID
        headers["x-opencode-session"] = os.environ.get("BOT_LLM_SESSION") or f"bot-team-{os.getpid()}-{int(time.time())}"
    return {
        "provider": name,
        "base": base,
        "model": model,
        "key": key,
        "key_source": key_source,
        "headers": headers,
    }


def _request(cfg: dict, path: str, payload: dict | None = None, timeout: int = 90) -> dict:
    url = f"{cfg['base']}{path}"
    headers = {
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        "User-Agent": "jujing-bot-team/1.0",
    }
    headers.update(cfg.get("headers") or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = (exc.read(600) or b"").decode("utf-8", "replace")
        raise LLMError(f"HTTP {exc.code} {url} → {body}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络层统一转 LLMError
        raise LLMError(f"{type(exc).__name__} {url} → {str(exc)[:200]}") from exc


def models(provider: str | None = None) -> list[str]:
    cfg = config(provider)
    data = _request(cfg, "/models", timeout=45)
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def chat(
    messages: list[dict],
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    timeout: int = 120,
) -> dict:
    """发一次 chat/completions，返回 {content, model, usage, elapsed_ms}。"""
    cfg = config(provider)
    payload: dict = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    started = time.time()
    data = _request(cfg, "/chat/completions", payload, timeout=timeout)
    elapsed_ms = int((time.time() - started) * 1000)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"响应里没有 choices[0].message.content：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    return {
        "content": content,
        "model": data.get("model") or payload["model"],
        "usage": data.get("usage") or {},
        "elapsed_ms": elapsed_ms,
        "provider": cfg["provider"],
    }


def healthcheck(provider: str | None = None) -> dict:
    cfg = config(provider)
    started = time.time()
    ids = models(cfg["provider"])
    return {
        "ok": True,
        "provider": cfg["provider"],
        "base": cfg["base"],
        "model": cfg["model"],
        "key_source": cfg["key_source"],
        "models": len(ids),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="bot-team LLM 通道自检")
    ap.add_argument("--provider", help="opencode | deepseek | bigmodel")
    ap.add_argument("--check", action="store_true", help="只验通（GET /models）")
    ap.add_argument("--models", action="store_true", help="列出模型 id")
    ap.add_argument("--prompt", help="跑一次 chat")
    ap.add_argument("--model", help="覆盖模型 id")
    args = ap.parse_args(argv)

    try:
        if args.models:
            print(json.dumps(models(args.provider), ensure_ascii=False, indent=2))
            return 0
        if args.prompt:
            out = chat([{"role": "user", "content": args.prompt}], provider=args.provider, model=args.model)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(healthcheck(args.provider), ensure_ascii=False, indent=2))
        return 0
    except LLMError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
