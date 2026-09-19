#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON 文件的**原子写入**与**损坏可见**读取。

解决什么问题
------------
平台有多个「状态文件」：待处理队列、去重记录、水位、关键位……
它们的读写原本是裸 `json.dump` / `json.load` + `except: pass`，
存在两个实测可复现的数据丢失路径：

1. **写一半被中断 → 文件损坏 → 读取静默返回空**
   进程被 kill、磁盘满、断电都可能在 `json.dump` 中途停下，
   留下一个被截断的 JSON。随后 `except Exception: return []`
   把它当成「空文件」—— 实测：

       队列文件被截断 → load() 返回 [] → 整条待处理问题被丢弃
       去重文件被截断 → load() 返回 {} → 去重记录全丢，重复回复

2. **无法区分「真的是空」和「读坏了」**
   两者都返回空容器，调用方无从判断，故障完全无痕。

本模块的做法
------------
- `write_json()`：先写同目录临时文件 → `os.replace()` 原子替换。
  `os.replace` 在同一文件系统内是原子的（POSIX 与 Windows 均保证），
  因此**不存在"半个文件"的中间态**：要么是旧的完整内容，要么是新的。
- `read_json()`：解析失败**不静默吞掉**，而是：
    · 把损坏文件另存为 `<name>.corrupt-<时间戳>` 留证
    · 返回调用方指定的 `default`
    · 可选回调 `on_error` 让上层记录日志
  这样既不会因坏文件而崩，也不会让故障无痕。

用法
----
    from safe_json import read_json, write_json

    items = read_json(path, default=[], on_error=lambda e: log(e))
    write_json(path, items)
"""
from __future__ import annotations

import json
import os
import tempfile
import time


def _backup_corrupt(path: str) -> str:
    """把损坏文件改名留证，返回备份路径（失败返回空串）。"""
    try:
        bak = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        os.replace(path, bak)
        return bak
    except Exception:
        return ""


def read_json(path: str, default=None, on_error=None, keep_corrupt: bool = True):
    """读取 JSON 文件。

    :param default:      文件不存在或解析失败时返回的值
    :param on_error:     可选回调 on_error(exc, path, backup_path)，用于记录日志
    :param keep_corrupt: 解析失败时是否把损坏文件改名留证（默认 True）
    """
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # ⚠️ 关键：不能静默当成「空」。留证 + 通知上层，让故障可见。
        bak = _backup_corrupt(path) if keep_corrupt else ""
        if on_error:
            try:
                on_error(e, path, bak)
            except Exception:
                pass
        return default


def write_json(path: str, data, indent: int = 2) -> bool:
    """**原子**写入 JSON 文件。成功返回 True。

    实现：同目录临时文件 → fsync → os.replace 原子替换。
    同目录是为了保证与目标文件在同一文件系统（跨设备 replace 非原子）。
    """
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())      # 确保落盘后再替换
            except Exception:
                pass                       # 某些文件系统不支持 fsync，忽略
        os.replace(tmp, path)              # 原子替换
        return True
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


def append_json_list(path: str, item, key: str = None, limit: int = 0) -> bool:
    """向 JSON 数组文件**原子**追加一项。

    :param key:   若给定，则按 item[key] 去重（已存在则不追加）
    :param limit: 若 >0，保留最后 limit 项
    """
    items = read_json(path, default=[])
    if not isinstance(items, list):
        items = []
    if key and any(isinstance(x, dict) and x.get(key) == item.get(key) for x in items):
        return False
    items.append(item)
    if limit and len(items) > limit:
        items = items[-limit:]
    return write_json(path, items)


def read_text(path: str, default: str = "") -> str:
    """读取纯文本（不存在/不可读时返回 default）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text(path: str, text: str) -> bool:
    """**原子**写入纯文本（临时文件 → os.replace）。

    与 write_json 同理：避免读取方读到写了一半的内容。
    实测场景：elliott 波浪缓存由 skill 子进程写入、由主进程读取，
    非原子写会出现「读到半截文本」并当成有效缓存返回。
    """
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False
