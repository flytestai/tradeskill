#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片 OCR 辅助：调用 tesseract 识别图片文字。

- 未安装 tesseract 时静默返回空串（不影响主流程）。
- Windows 安装 tesseract 并勾选中文语言包（chi_sim）后加入 PATH 即可生效。
  用法:
    from ocr_image import ocr
    text = ocr("assets/feishu_images/img_xxx.jpg")
"""
import shutil
import subprocess


def _find_tesseract():
    p = shutil.which("tesseract")
    return p or "tesseract"


def ocr(image_path, lang="chi_sim+eng", timeout=60):
    """识别图片文字，返回字符串；失败或无 tesseract 返回空串。"""
    if not image_path:
        return ""
    try:
        r = subprocess.run([_find_tesseract(), image_path, "stdout", "-l", lang],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        return (r.stdout or "").strip()
    except Exception:
        return ""
