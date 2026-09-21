#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端：统一封装 OpenAI 兼容接口（默认阿里云百炼 DashScope）。

用途
----
平台中所有需要大模型的地方都走这里，不再散落各脚本：
  1. 群问答的 AI 分析（services.qa_analyze）
  2. 分析报告的解读与结论润色（services.llm_summarize）
  3. 其他需要自然语言推理的场景

为什么单独抽一层
----------------
- 便于换供应商（百炼 / OpenAI / 其他 OpenAI 兼容端点只需改 base_url）
- 统一超时、重试、错误处理与用量记录
- **关键**：请求体以 UTF-8 字节流发送。实测在 Git Bash 下用 shell 拼接
  中文 JSON 会编码损坏（"上证指数" 变成乱码），必须在 Python 内构造 bytes。

配置（环境变量优先，其次 data/local_config.env）
------------------------------------------------
  LLM_PROVIDER      bailian | openai | custom（默认 bailian）
  LLM_API_KEY       API Key（必填）
  LLM_BASE_URL      覆盖默认端点
  LLM_MODEL         模型名（默认 qwen-plus）
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
    "openai":   {"base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    # 阿里云百炼 DashScope（OpenAI 兼容模式）—— 2026-09-19 新增
    #
    # 为什么切换：Kimi 账号因余额不足被停用
    #   （HTTP 429 / type=exceeded_current_quota_error / cash_balance 为负）。
    #
    # 实测对比（同一业务问句「上证3911 / 创业板3540，B反还是C杀」）：
    #     qwen-plus       2.9s  带 MACD/KDJ 指标分析   ← 默认选它
    #     qwen-flash      1.0s  带年线/通道分析
    #     qwen3.8-max     6.5s  逻辑辩证
    #     qwen3.7-max    11.9s  最专业
    #     kimi-k3（旧）17.7~21.5s ← 换百炼后群问答明显更快
    #
    # 该端点实测可列出 255 个模型（Qwen3/DeepSeek/GLM 等第三方）；
    # 本 preset 只固定默认模型，换模型改 LLM_MODEL 即可，无需改代码。
    "bailian":   {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "dashscope": {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "aliyun":    {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
}


class LLMError(Exception):
    """LLM 调用失败。"""


def _cfg(key: str, default: str = "") -> str:
    return (os.environ.get(key) or service_env(key, "") or default).strip()


def provider() -> str:
    return (_cfg("LLM_PROVIDER", "bailian") or "bailian").lower()


def api_key() -> str:
    return _cfg("LLM_API_KEY")


def base_url() -> str:
    explicit = _cfg("LLM_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    return PRESETS.get(provider(), PRESETS["bailian"])["base"]


def model() -> str:
    explicit = _cfg("LLM_MODEL")
    if explicit:
        return explicit
    return PRESETS.get(provider(), PRESETS["bailian"])["model"]


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
def _env(key: str) -> str:
    """读取环境变量（优先进程环境，其次 .env/local_config）。"""
    return (os.environ.get(key) or service_env(key, "") or "").strip()


def model_for(purpose: str = "") -> str:
    """按用途返回模型名（未配置则回退默认模型）。

    ⚠️ 这里**不能**用模块级常量硬编码模型名（2026-09-19 实测踩坑）
    ------------------------------------------------------------------
    原实现是模块级：
        MODEL_BY_PURPOSE = {"plan": os.environ.get("LLM_MODEL_PLAN", "kimi-k3")}
    于是切到阿里百炼后：
        provider = bailian
        base_url = https://dashscope.aliyuncs.com/...   ✅ 已跟随
        model    = kimi-k3                              ❌ 仍指向 Kimi 的模型
    → 会用 Kimi 的模型名去请求百炼，必然失败；而且失败原因
      （模型不存在）与配置看起来"没问题"形成矛盾，极难排查。

    根因：模块级常量在 import 时求值一次，且**默认值写死了某个供应商的模型**。
    修复：改为运行时求值（函数内读取），默认值留空 → 回退到 provider 的默认模型。
    这样「换供应商」只需改 LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY 三个变量，
    模型名自动跟随；只有需要**按用途指定不同模型**时才设 LLM_MODEL_PLAN 等。
    """
    m = _env("LLM_MODEL_" + (purpose or "").upper()) if purpose else ""
    if not m:
        m = _env("LLM_MODEL")
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
        raise LLMError("未配置 LLM_API_KEY")

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
            # ⚠️ 429 承载两种【语义完全不同】的情况，必须区分（2026-09-19 实测）
            # ------------------------------------------------------------------
            # 【A】余额不足 / 账号停用 —— 重试永远无效，且会掩盖真实原因
            #     实测 Moonshot：type=exceeded_current_quota_error
            #       "account ... is suspended due to insufficient balance"
            #     实测百炼同款语义亦可能以 429 返回（Arrearage / quota 类）。
            #     → 直接抛出，文案里点明"欠费/停用，需充值"，让人一眼看懂。
            #
            # 【B】速率限制 —— 瞬时，重试有效
            #     Moonshot 组织级限流在生产中很常见（群问答每 2 分钟一轮，
            #     每轮「规划+汇总」两次调用）。退避 3s / 8s / 16s。
            #
            # 若不做区分，【A】会被当作【B】反复重试后报"调用失败"，
            # 运维看到只会以为"限流了，等等就好" —— 而账户其实早已停用。
            if e.code == 429:
                low = detail.lower()
                quota_kw = ("insufficient balance", "exceeded_current_quota",
                            "arrearage", "suspended", "quota exceeded",
                            "insufficient_quota", "billing")
                if any(k in low for k in quota_kw):
                    raise LLMError(
                        "LLM 账户欠费/额度用尽（非限流，重试无效，需充值或换 Key）: %s"
                        % detail[:200])
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


def analyze_question(question: str, context: str = "", timeout: int = None) -> str:
    """群问答场景：结合平台多源数据回答问题。

    :param timeout: 覆盖默认 LLM_TIMEOUT。调用方（services.llm_ask）按
        「整体预算 − 已用时间」传入更紧的值，保证链路总耗时不超过上层超时。
    """
    kw = {"timeout": int(timeout)} if timeout else {}
    if context:
        prompt = ("【平台检索数据】\n%s\n\n【用户问题】\n%s\n\n"
                  "请基于上面的数据回答；数据不足的部分请直接说明。" % (context, question))
    else:
        prompt = ("【用户问题】\n%s\n\n注意：本次未取到平台数据，"
                  "请明确告知用户这一限制，不要凭记忆编造行情数字。" % question)
    return chat(prompt, system=SYSTEM_FINANCE, **kw)


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
    ap = argparse.ArgumentParser(description="LLM 客户端")
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
