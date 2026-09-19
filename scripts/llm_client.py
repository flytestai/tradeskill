#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端：统一封装 Kimi（Moonshot）等 OpenAI 兼容接口。

用途
----
平台中所有需要大模型的地方都走这里，不再散落各脚本：
  1. 群问答的 AI 分析（services.qa_analyze）
  2. 分析报告的解读与结论润色（services.llm_summarize）
  3. 其他需要自然语言推理的场景

为什么单独抽一层
----------------
- 便于换供应商（Kimi / OpenAI / 其他 OpenAI 兼容端点只需改 base_url）
- 统一超时、重试、错误处理与用量记录
- **关键**：请求体以 UTF-8 字节流发送。实测在 Git Bash 下用 shell 拼接
  中文 JSON 会编码损坏（"上证指数" 变成乱码），必须在 Python 内构造 bytes。

配置（环境变量优先，其次 data/local_config.env）
------------------------------------------------
  LLM_PROVIDER      kimi | openai | custom（默认 kimi）
  LLM_API_KEY       API Key（必填）
  LLM_BASE_URL      覆盖默认端点
  LLM_MODEL         模型名（默认 kimi-k3）
  LLM_TIMEOUT       秒，默认 120
  LLM_MAX_TOKENS    默认 4000（kimi-k3 是推理模型，思考过程也占 token）

用法
----
    from llm_client import chat, is_configured

    if is_configured():
        answer = chat("上证指数3911点怎么看？", system="你是A股分析师")

CLI
---
    python scripts/llm_client.py --check                 # 检查配置与连通性
    python scripts/llm_client.py "上证指数怎么看"          # 直接提问
    python scripts/llm_client.py --json "..."            # 结构化输出（含用量）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

#: 各供应商的默认端点与模型
PRESETS = {
    "kimi":   {"base": "https://api.moonshot.cn/v1", "model": "kimi-k3"},
    "moonshot": {"base": "https://api.moonshot.cn/v1", "model": "kimi-k3"},
    "openai": {"base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}


class LLMError(Exception):
    """LLM 调用失败。"""


def _cfg(key: str, default: str = "") -> str:
    return (os.environ.get(key) or service_env(key, "") or default).strip()


def provider() -> str:
    return (_cfg("LLM_PROVIDER", "kimi") or "kimi").lower()


def api_key() -> str:
    return _cfg("LLM_API_KEY") or _cfg("MOONSHOT_API_KEY") or _cfg("KIMI_API_KEY")


def base_url() -> str:
    explicit = _cfg("LLM_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    return PRESETS.get(provider(), PRESETS["kimi"])["base"]


def model() -> str:
    explicit = _cfg("LLM_MODEL")
    if explicit:
        return explicit
    return PRESETS.get(provider(), PRESETS["kimi"])["model"]


def timeout_s() -> int:
    try:
        return int(_cfg("LLM_TIMEOUT", "120") or "120")
    except ValueError:
        return 120


def max_tokens() -> int:
    """kimi-k3 是推理模型：思考过程 (reasoning_content) 也计入 completion tokens，
    预算过小会导致 content 为空（实测 max_tokens=300 时回答为空）。"""
    try:
        return int(_cfg("LLM_MAX_TOKENS", "4000") or "4000")
    except ValueError:
        return 4000


def is_configured() -> bool:
    return bool(api_key())


# --------------------------------------------------------------------------
# 核心调用
# --------------------------------------------------------------------------

#: 用途 → 模型分工
#
# ⚠️ 规划模型的选择依据**已更新**（原注释「k2.6 仅 5s、k3 需 21s」是早期测量，
#    当时规划提示词还很短；现在技能目录+约束已 1680 字，不再适用）。
#    2026-09-19 实测（3 次平均）：
#        kimi-k2.6 → 21.5s      kimi-k3 → 17.7s      两者均 3/3 成功
#    k3 反而更快，故规划默认改用 k3。
#    规划单次超时由 skill_agent.PLAN_TIMEOUT 控制（默认 30s）；
#    且规则路由已先行兜底，规划超时不会导致「零数据」。
MODEL_BY_PURPOSE = {
    "plan": os.environ.get("LLM_MODEL_PLAN", "kimi-k3"),
    "summarize": os.environ.get("LLM_MODEL_SUMMARIZE", ""),   # 空=用默认 LLM_MODEL
    "chat": "",
}


def model_for(purpose: str = "") -> str:
    """按用途返回模型名（未配置则回退默认模型）。"""
    m = MODEL_BY_PURPOSE.get(purpose or "", "")
    return m or model()


def chat(prompt: str, system: str = "", history: list = None,
         temperature: float = None, max_tokens_: int = None,
         retries: int = 2, timeout: int = None, purpose: str = "") -> str:
    """发起一次对话，返回助手回复文本。

    :param prompt:      用户输入
    :param system:      系统提示词
    :param history:     历史消息 [{"role","content"}]，置于 prompt 之前
    :param temperature: 留空用模型默认。注意 kimi-k3 **只接受 1**，
                        传其他值会报 "invalid temperature"。
    """
    if not is_configured():
        raise LLMError("未配置 LLM_API_KEY（Kimi 密钥）")

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": prompt})

    used_model = model_for(purpose)
    payload = {
        "model": used_model,
        "messages": msgs,
        "max_tokens": max_tokens_ or max_tokens(),
    }
    # ⚠️ kimi-k3 与 kimi-k2.6 **都只接受 temperature=1**，传其他值会
    #   报 "invalid temperature: only 1 is allowed"（已实测）。
    #   故仅当显式传入 1 时才带上该字段，其余情况交给服务端默认。
    if temperature == 1:
        payload["temperature"] = 1

    # ⚠️ 必须编码为 UTF-8 字节流（见模块 docstring 的说明）
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = base_url() + "/chat/completions"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + api_key(),
        "Accept": "application/json",
    }

    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or timeout_s()) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = "HTTP %s: %s" % (e.code, detail)
            # ⚠️ 429 必须重试：Moonshot 组织级限流在生产中很常见
            #   （群问答每 2 分钟一轮，每轮「规划+汇总」两次调用，
            #    并发或密集调用会触发 "Organization Rate limit exceeded"）。
            #   退避时间取 3s / 8s / 16s，给足配额恢复时间。
            if e.code == 429:
                if attempt < retries:
                    time.sleep(3.0 * (2 ** attempt))
                continue
            # 其余 4xx 多为参数/鉴权问题，重试无意义
            if 400 <= e.code < 500:
                raise LLMError(last_err)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:200])
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    else:
        raise LLMError("LLM 调用失败: %s" % last_err)

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("响应无 choices: %s" % json.dumps(data, ensure_ascii=False)[:200])
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        rc = msg.get("reasoning_content") or ""
        usage = data.get("usage") or {}
        raise LLMError(
            "模型未返回正文（可能 max_tokens 不足）。"
            "reasoning 长度=%d, completion_tokens=%s, max_tokens=%s"
            % (len(rc), usage.get("completion_tokens"), payload["max_tokens"]))
    return content


def chat_full(prompt: str, system: str = "", **kw) -> dict:
    """返回结构化结果（含用量），便于记录与计费观察。"""
    t0 = time.time()
    text = chat(prompt, system=system, **kw)
    return {"text": text, "model": model_for(kw.get("purpose", "")), "provider": provider(),
            "elapsed_ms": int((time.time() - t0) * 1000)}


# --------------------------------------------------------------------------
# 便捷封装：平台内常用场景
# --------------------------------------------------------------------------

#: 金融场景的系统提示词（统一口径，避免各调用点风格不一）
SYSTEM_FINANCE = (
    "你是一名严谨的 A 股市场分析助手，服务于群聊里的提问者。要求：\n"
    "1. **有数据就必须用**：下面会给你平台检索到的数据（行情/财务/行业/研报/宏观/资讯），"
    "回答必须建立在这些数据上，并**引用具体数字**，不要泛泛而谈；\n"
    "2. **先给结论**，再给 2-4 条依据，结构清晰、篇幅适中（群聊场景，控制在 400 字内）；\n"
    "3. **数据缺失就明说**：如果给你的数据不足以回答某个点，直接讲"
    "「这块数据没取到」，禁止编造数字或凭印象作答；\n"
    "4. **标注口径**：数据带日期/来源的要说清楚（如「截至 9/18」）；"
    "指数与个股别混淆（上证 ≠ 创业板）；\n"
    "5. **必须提示风险**，不给出确定性收益承诺，不做买卖指令，只给分析框架与条件化建议；\n"
    "6. 回答末尾附一行：本内容由 AI 生成，仅供参考，不构成投资建议。"
)


def analyze_question(question: str, context: str = "") -> str:
    """群问答场景：结合平台多源数据回答问题。"""
    if context:
        prompt = ("【平台检索数据】\n%s\n\n【用户问题】\n%s\n\n"
                  "请基于上面的数据回答；数据不足的部分请直接说明。" % (context, question))
    else:
        prompt = ("【用户问题】\n%s\n\n注意：本次未取到平台数据，"
                  "请明确告知用户这一限制，不要凭记忆编造行情数字。" % question)
    return chat(prompt, system=SYSTEM_FINANCE)


def summarize(content: str, instruction: str = "") -> str:
    """分析报告解读：对已有数据/结论做归纳或润色。"""
    inst = instruction or "请用 3-5 条要点归纳以下内容的核心结论，并指出最需要关注的风险。"
    return chat("%s\n\n---\n%s" % (inst, content), system=SYSTEM_FINANCE)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def check() -> int:
    print("=== LLM 配置 ===")
    print("  provider : %s" % provider())
    print("  base_url : %s" % base_url())
    print("  model    : %s" % model())
    print("  api_key  : %s" % ("已配置 (%s…)" % api_key()[:10] if api_key() else "❌ 未配置"))
    print("  timeout  : %ss    max_tokens: %s" % (timeout_s(), max_tokens()))
    if not is_configured():
        return 2
    print("\n=== 连通性测试 ===")
    try:
        t0 = time.time()
        out = chat("只回复两个字：正常", max_tokens_=2000)
        print("  ✅ 调用成功（%.1fs）" % (time.time() - t0))
        print("  回复: %s" % out[:100])
        return 0
    except LLMError as e:
        print("  ❌ %s" % e)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 客户端（Kimi）")
    ap.add_argument("prompt", nargs="?", help="提问内容")
    ap.add_argument("--system", default=SYSTEM_FINANCE, help="系统提示词")
    ap.add_argument("--check", action="store_true", help="检查配置与连通性")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--summarize", metavar="FILE", help="对文件内容做归纳")
    args = ap.parse_args()

    if args.check:
        return check()

    try:
        if args.summarize:
            content = open(args.summarize, encoding="utf-8").read()
            text = summarize(content)
        elif args.prompt:
            text = chat(args.prompt, system=args.system)
        else:
            ap.print_help()
            return 1
    except LLMError as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "model": model(), "text": text},
                         ensure_ascii=False, indent=1))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
