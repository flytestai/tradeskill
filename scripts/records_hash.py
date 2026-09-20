#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""content_hash 统一计算：用于 kol_records 精确去重与唯一索引。

规则：
  - 文本消息：对「归一化正文」（去掉所有空白）做 md5
  - 图片消息：对 "img:" + image_key 做 md5（避免多张图片共用 "[图片消息]" 正文时哈希冲突）

所有写入 kol_records 的脚本应统一调用本模块，保证哈希口径一致。
"""
import hashlib

from common import normalize


def content_hash(content="", image_path=""):
    """返回 32 位 md5 十六进制字符串。图片消息需传入 image_path（image_key）。"""
    if image_path:
        base = "img:" + image_path
    else:
        base = normalize(content)
    return hashlib.md5(base.encode("utf-8")).hexdigest()
