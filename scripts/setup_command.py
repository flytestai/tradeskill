#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群内「设置」指令直通处理：持仓监控 / 一次性价格提醒 / 关键位。

背景
----
群里 @机器人 的「设置」类指令（设置持仓监控、设置一次性价格提醒、设置关键位）
原先会跟普通提问一样走 `build_context + Kimi 分析`，又慢又容易答非所问。

本模块在 qa_analyzer.process() 的最前面做拦截：

    1. 确定性解析（零 LLM）：按固定格式直接解析并执行设置；
    2. 格式不对解析不了时，再交给 LLM 把自然语言转成脚本的设置格式去执行。

支持的设置意图（与脚本一一对应）：

  · 持仓监控   → position_monitor.py（监控 wu2198 仓位变化）
  · 价格提醒   → price_alerts.py   add（跌破/突破/涨到/区间，一次性触发）
  · 关键位     → level_monitor.py  set（支撑/压力/风控线/目标等点位）

用法
----
    from setup_command import handle_setup
    handled, reply = handle_setup(question, sender, sender_id, chat_id, dry_run=False)

    # 独立自测：
    python setup_command.py "创业板指跌破3356就提醒我"
    python setup_command.py            # 跑内置用例
"""
from __future__ import annotations

import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: 设置类动作词（比「帮我/看看」更窄，避免把普通分析问句误判为设置）
_SETUP_VERBS = ("设置", "提醒", "通知", "监控", "到价", "盯",
                "跟我说", "告诉我", "喊我", "叫我",
                "设个", "设一个", "加个", "加一条", "挂个")

#: 疑问句式标志 → 是提问，不是设置，直接交给普通 AI 分析
_QUESTION_MARKS = ("吗", "？", "?", "会不会", "能不能", "是否", "怎么样",
                   "怎么看", "多少", "如何", "什么时候", "何时", "几点")

#: 关键位类型关键词 → 归一化类型
_LEVEL_TYPE_MAP = (
    ("支撑", "支撑"),
    ("压力", "压力"),
    ("阻力", "压力"),
    ("风控线", "风控线"),
    ("目标", "目标"),
    ("关键位", "关键位"),
    ("点位", "关键位"),
)

#: 指数别名归一化（与 price_alerts.INDEX_ALIASES 保持一致的口径）
_INDEX_ALIASES = {
    "创业板指": "创业板指", "创业板": "创业板指",
    "上证指数": "上证指数", "上证": "上证指数", "沪指": "上证指数",
    "深证成指": "深证成指", "深成指": "深证成指", "深证": "深证成指",
    "科创50": "科创50", "科创板": "科创50",
    "沪深300": "沪深300", "中证500": "中证500", "中证1000": "中证1000",
    "上证50": "上证50", "恒生科技": "恒生科技", "纳斯达克": "纳斯达克",
}

_NUM = re.compile(r"\d")


def _fmt_num(x):
    if x is None:
        return ""
    try:
        s = ("%.6f" % float(x)).rstrip("0").rstrip(".")
    except Exception:
        s = str(x)
    return s


def _has_price_cond(text):
    """是否含明确的价格方向条件词（跌破/突破/涨到/区间/到价…）。"""
    try:
        from price_alerts import COND_KEYWORDS
        for kws in COND_KEYWORDS.values():
            for kw in kws:
                if kw in text:
                    return True
    except Exception:
        pass
    return any(w in text for w in ("到价", "触发价", "挂单"))


def looks_like_setup(text):
    """宽松闸门：是否像一条「设置」指令。False 则直接走普通 AI 分析。"""
    t = text or ""
    # 疑问句 → 是提问不是设置
    if any(w in t for w in _QUESTION_MARKS):
        return False
    # 持仓/仓位监控：无需数字
    if any(w in t for w in ("仓位", "持仓", "几米")):
        return True
    # 关键位/点位（支撑/压力/… + 数字）
    if any(w in t for w in ("支撑", "压力", "阻力", "关键位", "风控线", "目标位", "点位")) \
            and _NUM.search(t):
        return True
    # 价格提醒（设置动词 + 数字）
    if any(w in t for w in _SETUP_VERBS) and _NUM.search(t):
        return True
    return False


# --------------------------------------------------------------------------
# 确定性解析
# --------------------------------------------------------------------------

def _parse_price_alert(text):
    """复用 price_alerts 的自然语言解析，返回 (target, cond, price, price2) 或 None。"""
    try:
        from price_alerts import parse_alert_text
        r = parse_alert_text(text)
    except Exception:
        return None
    if not r:
        return None
    target, cond, price, price2 = r
    if not target or price is None:
        return None
    if cond not in ("below", "above", "range"):
        return None
    return r


def _parse_key_level(text):
    """解析关键位设置，返回 (index, level, type, note) 或 None。

    示例：创业板指支撑位3540、上证压力位3996 说明B反前高
    """
    index = None
    for alias, full in sorted(_INDEX_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in text:
            index = full
            break
    if not index:
        return None

    # 优先取「支撑/压力/关键位…」紧邻的数字
    m = re.search(r"(?:支撑|压力|阻力|关键位|风控线|目标位|点位)[^\d]{0,5}(\d+(?:\.\d+)?)", text)
    if not m:
        m = _NUM.search(text)
    if not m:
        return None
    try:
        level = float(m.group(1))
    except ValueError:
        return None

    ltype = "关键位"
    for kw, tp in _LEVEL_TYPE_MAP:
        if kw in text:
            ltype = tp
            break

    note = ""
    m2 = re.search(r"(?:说明|备注|note)[:：]?\s*(.{0,30})", text, re.I)
    if m2:
        note = re.sub(r"[。，,;；\s]+$", "", m2.group(1)).strip()

    return index, level, ltype, note


# --------------------------------------------------------------------------
# 执行（确定性，直接调用对应脚本）
# --------------------------------------------------------------------------

def _exec_position_monitor(dry_run=False, kol="wu2198"):
    try:
        import position_monitor as pm
    except Exception as e:
        return "⚠️ 持仓监控模块加载失败：%s" % str(e)[:120]
    history = pm.get_position_history(kol)
    if not history:
        return "⚠️ 暂未读取到 %s 的仓位记录，请稍后再试" % kol
    latest = history[-1]
    size = latest.get("position_size")
    action = latest.get("position_action") or "持有"
    rdate = latest.get("record_date") or ""
    note = (latest.get("position_note") or "").strip()
    if size <= 1:
        status = "🚨 接近清仓（1米）—— 她躲暴跌的信号"
    elif size <= 2:
        status = "🟠 防御（2米）"
    elif size >= 5:
        status = "🔴 进攻（%s米）" % _fmt_num(size)
    else:
        status = "🟡 中性（%s米）" % _fmt_num(size)
    lines = [
        "✅ 已开启持仓监控（%s）" % kol,
        "🕐 最新记录：%s" % (rdate or "—"),
        "📊 当前仓位：%s米（%s）" % (_fmt_num(size), action),
        "当前状态：%s" % status,
    ]
    if note:
        lines.append("📝 备注：%s" % note[:40])
    lines.append("💡 仓位变化时将自动推送提醒到群里")
    if dry_run:
        lines.insert(0, "[dry-run]")
    return "\n".join(lines)


def _exec_price_alert(target, cond, price, price2, note, chat_id, sender,
                      sender_id, dry_run=False):
    cond_txt = {"below": "跌破", "above": "突破/涨到", "range": "区间"}[cond]
    rng = (_fmt_num(price) + " ~ " + _fmt_num(price2)) if price2 else _fmt_num(price)
    if dry_run:
        return "[dry-run] 将设置价格提醒：%s %s %s" % (target, cond_txt, rng)
    try:
        from price_alerts import add_alert
    except Exception as e:
        return "⚠️ 价格提醒模块加载失败：%s" % str(e)[:120]
    aid = add_alert(target, cond, price, price2, note=note or "",
                    chat_id=chat_id or "", created_by=sender or "",
                    created_by_id=sender_id or "")
    if aid:
        return ("✅ 已设置价格提醒\n🎯 %s：%s %s\n⏰ 触发后会自动在群里提醒"
                % (target, cond_txt, rng))
    return "⚠️ 未设置成功：%s %s %s 可能已存在相同提醒" % (target, cond_txt, rng)


def _exec_key_level(index, level, ltype, note, dry_run=False):
    if dry_run:
        return "[dry-run] 将设置关键位：%s %s（%s）" % (index, _fmt_num(level), ltype or "关键位")
    try:
        import level_monitor as lm
    except Exception as e:
        return "⚠️ 关键位模块加载失败：%s" % str(e)[:120]
    try:
        import datetime
        levels = lm.load_levels()
        if index not in levels:
            levels[index] = []
        # 同点位覆盖旧记录（与 level_monitor.set_level 行为一致）
        levels[index] = [x for x in levels[index] if x.get("level") != level]
        updated = datetime.date.today().isoformat()
        levels[index].append({"level": level, "type": ltype or "关键位",
                              "note": note or "", "asof": updated})
        lm.save_levels(levels)
        try:
            with open(lm.ASOF_FILE, "w", encoding="utf-8") as f:
                f.write(updated)
        except Exception:
            pass
    except Exception as e:
        return "⚠️ 关键位设置失败：%s" % str(e)[:120]
    tail = ("｜%s" % note) if note else ""
    return "✅ 已设置关键位\n📍 %s：%s（%s）%s\n⏰ 接近/突破/跌破该位时将自动提醒" % (
        index, _fmt_num(level), ltype or "关键位", tail)


# --------------------------------------------------------------------------
# AI 兜底：自然语言 → 脚本设置格式
# --------------------------------------------------------------------------

_SETUP_SYSTEM = (
    "你是群里提醒机器人的指令解析器。用户想设置监控或提醒，但表述不规范。\n"
    "请把用户意图解析成一个 JSON 对象，只输出 JSON，不要多余文字、不要代码块。\n"
    "可用动作：\n"
    '1. 价格提醒：{"action":"price_alert","target":"创业板指","cond":"below","price":3356,"price2":null}\n'
    '   cond 只能是 below(跌破)/above(突破或涨到)/range(区间，需同时给 price 与 price2)\n'
    '2. 关键位：{"action":"key_level","index":"创业板指","level":3540,"type":"支撑","note":"B反风控线"}\n'
    '   type 只能是 支撑/压力/风控线/目标/关键位\n'
    '3. 持仓监控：{"action":"position_monitor"}\n'
    '4. 无法解析：{"action":"none"}\n'
    "价格和点位必须是纯数字（不要带单位）。标的用规范名："
    "上证指数/深证成指/创业板指/科创50/沪深300/恒生科技/纳斯达克，或 6 位股票代码。"
)


def _extract_json(raw):
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _help_reply():
    return (
        "❌ 没能识别出可执行的设置，请按下面格式再发一次：\n"
        "· 价格提醒：`创业板指跌破3356就提醒我` / `上证指数突破4000提醒`\n"
        "· 关键位：`创业板指支撑位3540` / `上证压力位3996 说明B反前高`\n"
        "· 持仓监控：`监控仓位` / `持仓监控`"
    )


def _ai_fallback(text, sender, sender_id, chat_id, dry_run=False):
    from llm_client import chat, is_configured, LLMError
    if not is_configured():
        return "❌ 无法识别设置指令，且 AI 未配置。" + _help_reply()
    try:
        raw = chat(text, system=_SETUP_SYSTEM, max_tokens_=800, purpose="setup")
    except LLMError as e:
        return "❌ 设置解析失败（AI 调用异常）：%s" % str(e)[:120]
    except Exception as e:
        return "❌ 设置解析失败：%s" % str(e)[:120]

    data = _extract_json(raw)
    if not data:
        return _help_reply()
    action = (data.get("action") or "").strip()
    try:
        if action == "price_alert":
            target = data.get("target")
            cond = data.get("cond")
            price = data.get("price")
            price2 = data.get("price2")
            if not target or cond not in ("below", "above", "range") or price is None:
                return _help_reply()
            return _exec_price_alert(target, cond, float(price),
                                     float(price2) if price2 is not None else None,
                                     "", chat_id, sender, sender_id, dry_run)
        if action == "key_level":
            index = data.get("index")
            level = data.get("level")
            ltype = data.get("type") or "关键位"
            note = data.get("note") or ""
            if not index or level is None:
                return _help_reply()
            return _exec_key_level(index, float(level), ltype, note, dry_run)
        if action == "position_monitor":
            return _exec_position_monitor(dry_run=dry_run)
    except Exception as e:
        return "❌ 设置执行失败：%s" % str(e)[:120]
    return _help_reply()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def handle_setup(question, sender="", sender_id="", chat_id="", dry_run=False):
    """处理一条「设置」指令。

    返回 (handled, reply)：
      handled=False 表示这不是设置指令，调用方应继续走普通 AI 分析；
      handled=True  表示已按设置意图处理，reply 为要发回群里的正文。
    """
    text = (question or "").strip()
    if not text or not looks_like_setup(text):
        return False, ""

    # 1) 持仓/仓位监控
    if any(w in text for w in ("仓位", "持仓", "几米")):
        return True, _exec_position_monitor(dry_run=dry_run)

    # 2) 价格提醒（有明确涨跌/到价条件）
    if _has_price_cond(text):
        r = _parse_price_alert(text)
        if r:
            target, cond, price, price2 = r
            return True, _exec_price_alert(target, cond, price, price2,
                                           note=text[:40], chat_id=chat_id,
                                           sender=sender, sender_id=sender_id,
                                           dry_run=dry_run)

    # 3) 关键位
    if any(w in text for w in ("支撑", "压力", "阻力", "关键位", "风控线", "目标位", "点位")):
        r = _parse_key_level(text)
        if r:
            index, level, ltype, note = r
            return True, _exec_key_level(index, level, ltype, note, dry_run=dry_run)

    # 4) AI 兜底：自然语言 → 脚本设置格式
    return True, _ai_fallback(text, sender, sender_id, chat_id, dry_run=dry_run)


_CASES = [
    "创业板指跌破3356就提醒我",
    "设置 上证指数突破4000提醒",
    "设置 创业板指支撑位3540",
    "上证压力位3996 说明B反前高",
    "帮我把创业板指跌到3300的时候跟我说一下",
    "监控仓位",
    "持仓监控",
    "帮我看看上证指数3900点怎么看",   # 普通分析，不应被当成设置
    "告诉我创业板指会不会跌到3300",   # 疑问句，不应被当成设置
    "茅台现在多少钱",                 # 普通查询
]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="设置指令直通处理（自测）")
    ap.add_argument("text", nargs="?", help="指令原文")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sender", default="测试用户")
    ap.add_argument("--sender-id", default="")
    ap.add_argument("--chat-id", default="")
    args = ap.parse_args()

    if not args.text:
        for t in _CASES:
            handled, reply = handle_setup(t, args.sender, args.sender_id,
                                          args.chat_id, dry_run=args.dry_run)
            print("\n=== %s ===\nhandled=%s\n%s" % (t, handled, reply))
        return

    handled, reply = handle_setup(args.text, args.sender, args.sender_id,
                                  args.chat_id, dry_run=args.dry_run)
    print("handled=%s" % handled)
    print(reply)


if __name__ == "__main__":
    main()
