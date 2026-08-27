#!/usr/bin/env python3
"""
飞书群消息自动同步脚本 —— 拉取 wu2198 群发言，增量导入 kol-opinion-analyzer 数据库

功能：
1. 拉取飞书群内最新的机器人消息（wu2198 的发言）
2. 按 80% 文本相似度去重
3. 测试消息自动跳过
4. 已导入过的消息不会重复导入
5. 报告本次同步结果：新增导入条数、跳过条数

用法：
  python kol_sync_feishu.py

环境变量（必需）：
  FEISHU_APP_ID       飞书应用 App ID
  FEISHU_APP_SECRET   飞书应用 App Secret
  FEISHU_CHAT_ID      群 ID（可选，默认 oc_59301fc3e11c6e131f31ffb8acd4125a）
  KOL_DB_PATH          数据库路径（可选，默认本 skill 的 data/kol_opinions.db）
"""
import os
import sys
import json
import time
import sqlite3
import difflib
import urllib.request
import urllib.error

# ---------- 配置 ----------
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get(
    "KOL_DB_PATH",
    os.path.join(SKILL_DIR, "data", "kol_opinions.db"),
)
DEFAULT_CHAT_ID = "oc_59301fc3e11c6e131f31ffb8acd4125a"
CHAT_ID = os.environ.get("FEISHU_CHAT_ID", DEFAULT_CHAT_ID)
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# 测试消息过滤关键词
TEST_KEYWORDS = ["转发测试", "同步测试", "设备A同步测试", "test", "TEST"]

# 文本相似度去重阈值（0.8 = 80%）
SIMILARITY_THRESHOLD = 0.80


def feishu_request(url, token=None, method="GET", body=None):
    """调用飞书开放平台 API（标准库 urllib，无第三方依赖）"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": f"HTTP {e.code}"}


def get_tenant_token(app_id, app_secret):
    """获取 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = feishu_request(url, body={"app_id": app_id, "app_secret": app_secret}, method="POST")
    if resp.get("code") == 0:
        return resp.get("tenant_access_token")
    return None


def fetch_group_messages(token, chat_id, page_size=50):
    """拉取群消息列表（最新在前）"""
    url = (
        "https://open.feishu.cn/open-apis/im/v1/messages"
        f"?container_id_type=chat&container_id={chat_id}"
        f"&page_size={page_size}&sort_type=ByCreateTimeDesc"
    )
    resp = feishu_request(url, token=token)
    if resp.get("code") != 0:
        print(f"[ERROR] 拉取消息失败: {resp.get('msg', resp)}")
        return []
    items = resp.get("data", {}).get("items", [])
    return items


def extract_text(message_item):
    """从飞书消息 item 提取纯文本内容"""
    body = message_item.get("body", {})
    content_str = body.get("content", "")
    if not content_str:
        return ""
    try:
        content = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str
    # 飞书文本消息 content 结构: {"text": "..."}
    return content.get("text", "") or str(content)


def is_test_message(text):
    """判断是否测试消息"""
    if not text or not text.strip():
        return True
    for kw in TEST_KEYWORDS:
        if kw in text:
            return True
    return False


def text_similarity(a, b):
    """计算两段文本的相似度（0~1）"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_duplicate(conn, text, threshold=SIMILARITY_THRESHOLD):
    """判断文本是否与数据库已有记录高度相似（>80%）"""
    cur = conn.cursor()
    cur.execute("SELECT content FROM kol_records WHERE kol_name='wu2198'")
    for (existing,) in cur.fetchall():
        if existing and text_similarity(existing, text) >= threshold:
            return True
    return False


def import_message(conn, text, create_time_ms):
    """增量导入一条消息（按80%相似度去重）"""
    from datetime import datetime, timezone, timedelta
    # 飞书时间戳转北京时间（UTC+8）
    dt = datetime.fromtimestamp(create_time_ms / 1000, tz=timezone(timedelta(hours=8)))
    record_date = dt.strftime("%Y-%m-%d %H:%M")

    # 跳过测试消息
    if is_test_message(text):
        return "test_skip"

    # 去重
    if is_duplicate(conn, text):
        return "dup_skip"

    # 导入
    conn.execute(
        """INSERT INTO kol_records
           (kol_name, platform, content, extracted_viewpoints,
            related_assets, record_date, position_size, position_action, position_note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("wu2198", "飞书群", text, "", "", record_date, None, "", "飞书群自动同步"),
    )
    return "inserted"


def main():
    print("=" * 60)
    print("  飞书群消息自动同步（wu2198）")
    print("=" * 60)

    # 检查凭证
    if not APP_ID or not APP_SECRET:
        print("[ERROR] 缺少飞书应用凭证。请设置环境变量：")
        print("  FEISHU_APP_ID=你的App ID")
        print("  FEISHU_APP_SECRET=你的App Secret")
        sys.exit(1)

    # 获取 token
    print(f"[1/4] 获取飞书 tenant_access_token ...")
    token = get_tenant_token(APP_ID, APP_SECRET)
    if not token:
        print("[ERROR] 获取 token 失败，请检查 App ID / App Secret")
        sys.exit(1)
    print("  token 获取成功")

    # 拉取消息
    print(f"[2/4] 拉取群消息 (chat_id={CHAT_ID}) ...")
    messages = fetch_group_messages(token, CHAT_ID)
    print(f"  拉到 {len(messages)} 条消息")

    # 连接数据库
    print(f"[3/4] 连接数据库 {DB_PATH} ...")
    conn = sqlite3.connect(DB_PATH)

    # 导入（增量，去重）
    print("[4/4] 增量导入（80%相似度去重 + 跳过测试消息）...")
    inserted = 0
    dup_skipped = 0
    test_skipped = 0
    empty_skipped = 0

    # 飞书消息是最新在前，倒序导入（旧在前）
    for msg in reversed(messages):
        text = extract_text(msg)
        create_time = int(msg.get("create_time", 0) or 0)

        if not text or not text.strip():
            empty_skipped += 1
            continue

        result = import_message(conn, text, create_time)
        if result == "inserted":
            inserted += 1
            print(f"  ✅ 新增 [{record_date_of(msg)}]: {text[:40]}...")
        elif result == "dup_skip":
            dup_skipped += 1
        elif result == "test_skip":
            test_skipped += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name='wu2198'").fetchone()[0]
    conn.close()

    # 报告结果
    print("\n" + "=" * 60)
    print("  同步结果报告")
    print("=" * 60)
    print(f"  新增导入条数: {inserted}")
    print(f"  跳过条数: {dup_skipped}（重复，相似度>80%）")
    print(f"  测试消息跳过: {test_skipped}")
    print(f"  空消息跳过: {empty_skipped}")
    print(f"  数据库 wu2198 总条数: {total}")
    print("=" * 60)


def record_date_of(msg):
    """辅助：格式化消息时间（供打印）"""
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromtimestamp(int(msg.get("create_time", 0) or 0) / 1000,
                                    tz=timezone(timedelta(hours=8)))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return "??:??"


if __name__ == "__main__":
    main()
