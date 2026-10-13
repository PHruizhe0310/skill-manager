#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能管家 skm —— 第一步：收集

把散落在电脑各处的大模型 Skill 收集起来，分好类，一次性打包发给项目。

本文件只依赖 Python 标准库。命令行入口：python skm.py <命令>
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

APP_NAME = "skm"
APP_TITLE = "技能管家"
VERSION = "0.1.0"
SCHEMA_VERSION = 1

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"

SKILL_MD = "SKILL.md"
META_FILE = "skill.json"
UNCATEGORIZED = "未分类"

# 收录时跳过的目录/文件（构建产物、依赖目录，不属于 Skill 本体）
SKIP_NAMES = {
    ".git", ".github", ".idea", ".vscode", ".venv", "venv", "env",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt", "target", ".cache", ".DS_Store",
    "Thumbs.db", ".ipynb_checkpoints",
}
MAX_SCAN_DEPTH = 4

HASH_LEN = 12


# ══════════════════════════════════════════════════════════════════════
#  控制台输出（Windows 控制台适配：UTF-8 + 虚拟终端颜色 + 中日韩宽度对齐）
# ══════════════════════════════════════════════════════════════════════

class SkmError(Exception):
    """可预期的错误，直接打印友好信息而不是堆栈。"""


class Abort(Exception):
    """用户主动取消。"""


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class C:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    GREY = "\x1b[90m"


_NO_COLOR = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"


def paint(text: str, *styles: str) -> str:
    if _NO_COLOR or not styles:
        return text
    return "".join(styles) + text + C.RESET


def term_width(default: int = 100) -> int:
    try:
        return max(60, min(shutil.get_terminal_size((default, 24)).columns, 160))
    except Exception:
        return default


def dw(text) -> int:
    """字符串在终端里的显示宽度（中文/全角算 2 格）。"""
    text = "" if text is None else str(text)  # 分类树是用户手改的 JSON，字段可能不是字符串
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def trunc(text, width: int) -> str:
    """按显示宽度截断，超出部分用省略号。"""
    text = "" if text is None else str(text)
    if width <= 1 or dw(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        if unicodedata.combining(ch):
            out.append(ch)
            continue
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > width - 1:
            break
        out.append(ch)
        used += step
    return "".join(out) + "…"


def pad(text, width: int, align: str = "left") -> str:
    text = "" if text is None else str(text)
    gap = max(0, width - dw(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def line(char: str = "─", width: int | None = None) -> str:
    return paint(char * (width or term_width()), C.GREY)


def header(title: str, extra: str = "") -> None:
    width = term_width()
    left = f"{APP_TITLE} {paint('v' + VERSION, C.GREY)}"
    body = f"{left}  {title}"
    if extra:
        body += paint(f"  {extra}", C.GREY)
    print(paint(body, C.BOLD, C.CYAN))
    print(line(width=width))


def ok(text: str) -> None:
    print(f"{paint('OK', C.GREEN, C.BOLD)} {text}")


def warn(text: str) -> None:
    print(f"{paint('!!', C.YELLOW, C.BOLD)} {text}")


def fail(text: str) -> None:
    print(f"{paint('XX', C.RED, C.BOLD)} {text}")


def info(text: str) -> None:
    print(f"{paint('  ', C.GREY)}{text}")


def rule() -> None:
    print(line())


# ══════════════════════════════════════════════════════════════════════
#  JSON 读写（原子写，方便手工改 JSON 后随时重开）
# ══════════════════════════════════════════════════════════════════════

def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名必须带线程 id：网页服务是多线程的，批量操作会同时发好几个写请求，
    # 都往同一个 xxx.tmp 上写会撞成 Permission denied（Windows 上尤其明显）。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise SkmError(f"{path.name} 解析失败：{exc}\n"
                       f"    请检查 {path} 是否被手工改坏（或从备份恢复）")


def write_json(path: Path, data) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_text_smart(path: Path) -> str:
    """按 utf-8-sig / utf-8 / gb18030 / latin-1 顺序尝试解码，兼容 Windows 下的中文文件。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_size(num: int) -> str:
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= step
    return f"{value:.1f}GB"


# ══════════════════════════════════════════════════════════════════════
#  YAML frontmatter 极简解析器（标准库没有 yaml，自己写够用的那部分）
#  支持：key: value / key: "value" / key: [a, b] / key: >- 折叠块 /
#        key: | 字面块 / 嵌套映射（metadata: 下的子键）
# ══════════════════════════════════════════════════════════════════════

_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")
_BLOCK_MARKERS = (">-", ">", "|+", "|", ">+", ">+-")


def split_frontmatter(text: str):
    """返回 (frontmatter 文本, 正文)。没有 frontmatter 时返回 (None, 全文)。"""
    stripped = text.lstrip("\ufeff")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, stripped
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    return None, stripped


def _parse_inline_list(value: str):
    inner = value[1:-1].strip()
    if not inner:
        return []
    items = []
    buf = ""
    quote = ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf.strip().strip("\"'"))
            buf = ""
        else:
            buf += ch
    items.append(buf.strip().strip("\"'"))
    return [item for item in items if item]


def parse_yaml_block(block: str) -> dict:
    """把 frontmatter 文本解析成 dict（只支持 Skill 元数据会用到的语法子集）。"""
    lines = block.splitlines()
    result: dict = {}
    stack = [(-1, result)]
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line_text = raw.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        match = _KEY_RE.match(line_text)
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()

        if value in _BLOCK_MARKERS:  # >- / | 等块标量
            index += 1
            buf = []
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    buf.append("")
                    index += 1
                    continue
                next_indent = len(nxt) - len(nxt.lstrip(" "))
                if next_indent <= indent:
                    break
                buf.append(nxt.strip())
                index += 1
            while buf and not buf[-1]:
                buf.pop()
            if value.startswith(">"):
                chunks, current = [], []
                for item in buf:
                    if item == "":
                        chunks.append(" ".join(current))
                        current = []
                    else:
                        current.append(item)
                chunks.append(" ".join(current))
                parent[key] = "\n".join(chunks)
            else:
                parent[key] = "\n".join(buf)
            continue

        if value == "":  # 可能是嵌套映射
            nxt = index + 1
            if nxt < len(lines) and lines[nxt].strip():
                next_indent = len(lines[nxt]) - len(lines[nxt].lstrip(" "))
                if next_indent > indent:
                    child: dict = {}
                    parent[key] = child
                    stack.append((indent, child))
                    index += 1
                    continue
            parent[key] = ""
            index += 1
            continue

        if value.startswith("[") and value.endswith("]"):
            parent[key] = _parse_inline_list(value)
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            parent[key] = value[1:-1]
        else:
            parent[key] = value
        index += 1
    return result


# ══════════════════════════════════════════════════════════════════════
#  Skill 分析：读介绍 / 取标签 / 算哈希
# ══════════════════════════════════════════════════════════════════════

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def first_heading(body: str) -> str:
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            return match.group(2).strip()
    return ""


def first_paragraph(body: str, limit: int = 220) -> str:
    """取正文第一段自然语言文字：跳过标题、代码块、引用、列表符号。"""
    in_fence = False
    buf: list[str] = []
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            if not in_fence:
                in_fence = True
                if buf:
                    break
            else:
                in_fence = False
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            if buf:
                break
            continue
        if stripped.startswith("#"):
            if buf:
                break
            continue
        if stripped.startswith((">", "|", "---", "***", "===")):
            if buf:
                break
            continue
        if re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            if buf:
                break
            continue
        buf.append(stripped)
    text = " ".join(buf)
    text = re.sub(r"\s+", " ", text).strip()
    if dw(text) > limit:
        text = trunc(text, limit)
    return text


def clean_inline(value: str) -> str:
    """压平 description 里的换行/多余空格（>- 折叠块常带换行）。"""
    text = value.replace("\r", "\n")
    chunks = [chunk.strip() for chunk in text.split("\n")]
    merged = " ".join(chunk for chunk in chunks if chunk)
    return re.sub(r"\s+", " ", merged).strip()


def content_hash(text: str) -> str:
    """正文哈希：忽略行尾空白与换行风格差异，避免无意义的"改了就变"报警。"""
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:HASH_LEN]


TAG_RULES: dict[str, tuple[str, ...]] = {
    "渗透测试": ("渗透测试", "pentest", "penetration test", "渗透"),
    "红队": ("红队", "red team", "redteam", "红队作战", "attack simulation"),
    "侦察测绘": ("侦察", "recon", "资产测绘", "攻击面", "attack surface", "信息收集", "osint"),
    "漏洞利用": ("漏洞利用", "exploit", "漏洞", "vulnerability", "cve", "0day", "zero-day", "漏洞挖掘", "poc"),
    "后渗透": ("后渗透", "post-exploitation", "提权", "权限提升", "privilege escalation", "横向移动", "lateral movement"),
    "免杀对抗": ("免杀", "evasion", "绕过检测", "edr", "av bypass", "对抗检测", "反检测"),
    "社会工程": ("社会工程", "钓鱼", "phishing", "spear", "社工"),
    "应急响应": ("应急响应", "incident response", "溯源", "取证", "forensic", "应急"),
    "逆向工程": ("逆向", "reverse engineering", "reversing", "反编译", "ida", "ghidra", "ollydbg"),
    "移动安全": ("移动安全", "android", "ios", "apk", "移动端", "mobile"),
    "密码学": ("密码学", "cryptography", "crypto", "加密", "解密"),
    "无线硬件": ("无线", "wifi", "wireless", "蓝牙", "bluetooth", "射频", "rf安全"),
    "二进制安全": ("二进制", "binary", "shellcode", "pwn", "栈溢出", "rop"),
    "样本分析": ("恶意样本", "malware", "病毒分析", "样本分析", "木马", "ransomware", "混淆"),
    "AI安全": ("ai安全", "ai 安全", "llm", "大模型", "prompt injection", "提示词注入", "模型安全", "ai应用"),
    "云安全": ("云安全", "cloud security", "k8s", "kubernetes", "容器安全"),
    "法律检索": ("法律检索", "法条", "法规", "statute", "法律数据库", "检索法规"),
    "案例研究": ("案例研究", "判例", "案例分析", "jurisprudence", "裁判文书", "案例检索"),
    "法律文书": ("法律文书", "起诉状", "代理词", "判决书", "法律意见书", "代理"),
    "合同合规": ("合同", "合规", "尽调", "due diligence", "隐私政策", "数据合规", "合同审查"),
    "学术写作": ("论文", "学术", "paper", "文献", "citation", "综述", "开题"),
    "PPT": ("ppt", "幻灯片", "slide", "powerpoint", "演示文稿"),
    "表格数据": ("excel", "表格", "xlsx", "数据清洗", "透视表", "csv"),
    "PDF": ("pdf",),
    "图像视频": ("图像", "图片", "配图", "抠图", "logo", "视频", "剪辑"),
    "前端": ("前端", "frontend", "react", "vue", "css", "tailwind", "ui设计",
             "web design", "web interface", "accessibility", "组件库"),
    "后端": ("后端", "backend", "api", "数据库", "postgres", "mysql", "redis", "服务"),
    "测试": ("测试", "test", "e2e", "pytest", "单元测试"),
    "自动化": ("自动化", "automation", "脚本", "script", "workflow", "批量"),
    "翻译": ("翻译", "translate", "translation"),
    "文档写作": ("文档", "document", "word", "docx", "markdown", "写作", "write"),
    "版本控制": ("git", "版本控制", "commit", "分支", "branch", "diff"),
}


def guess_tags(name: str, summary: str, body: str, limit: int = 6) -> list[str]:
    """从名称/简介/正文里抓关键词作为建议标签（按命中次数排序）。"""
    haystack = f"{name}\n{summary}\n{body}".lower()
    scores: list[tuple[int, str]] = []
    for tag, keywords in TAG_RULES.items():
        hits = 0
        for keyword in keywords:
            needle = keyword.lower()
            # 中文关键词直接计数，英文词按单词边界避免误伤
            if re.match(r"^[\x00-\x7f]+$", needle):
                hits += len(re.findall(rf"\b{re.escape(needle)}\b", haystack))
            else:
                hits += haystack.count(needle)
        if hits:
            scores.append((hits, tag))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [tag for _, tag in scores[:limit]]


def analyze_skill_md(text: str) -> dict:
    """把一份 SKILL.md 解析成 {name, summary, tags, hash, body}。"""
    front, body = split_frontmatter(text)
    meta = parse_yaml_block(front) if front else {}
    meta = meta if isinstance(meta, dict) else {}

    name = clean_inline(str(meta.get("name") or "")).strip()
    if not name:
        name = first_heading(body)
    if not name:
        name = "未命名 Skill"

    summary = clean_inline(str(meta.get("description") or "")).strip()
    if not summary:
        summary = first_paragraph(body)

    tags: list[str] = []
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        for key in ("tags", "tag", "keywords"):
            value = nested.get(key)
            if isinstance(value, list):
                tags = [clean_inline(str(item)).strip() for item in value]
                break
            if isinstance(value, str) and value:
                tags = [part.strip() for part in re.split(r"[,，、]", value) if part.strip()]
                break
    if isinstance(meta.get("tags"), list) and not tags:
        tags = [clean_inline(str(item)).strip() for item in meta["tags"]]

    tags = [tag for tag in tags if tag][:12]
    if not tags:
        tags = guess_tags(name, summary, body)

    return {
        "name": name,
        "summary": summary,
        "tags": tags,
        "hash": content_hash(text),
        "body": body,
        "meta": meta,
    }


# ══════════════════════════════════════════════════════════════════════
#  ID（Skill 文件夹名）处理
# ══════════════════════════════════════════════════════════════════════

_SLUG_KEEP_RE = re.compile(r"[^a-z0-9]+")
_ILLEGAL_FS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def slugify(text: str) -> str:
    """把任意名字转成安全的文件夹名。

    - 名字里带中文 → 保留中文（Windows 支持），只清掉文件系统非法字符。
      这样 data\\skills\\案例研究助手 比 skill-64f89e 好认得多，
      也不会让「PPT 排版」和「PPT 内容」都塌成 ppt 撞名。
    - 纯 ASCII 名字 → 转小写连字符风格（Attack Surface Recon → attack-surface-recon）
    - 两者都没有 → 退回哈希命名
    """
    raw = unicodedata.normalize("NFKC", str(text)).strip()
    if _HAS_CJK_RE.search(raw):
        cleaned = _ILLEGAL_FS_RE.sub("_", raw)
        cleaned = re.sub(r"\s+", "-", cleaned).strip("-. ")
        name = cleaned[:64]
    else:
        name = re.sub(r"-{2,}", "-", _SLUG_KEEP_RE.sub("-", raw.lower()).strip("-"))[:64]
    if not name:
        name = f"skill-{hashlib.sha256(str(text).encode('utf-8')).hexdigest()[:6]}"
    if name.lower() in _RESERVED_NAMES:
        name = f"{name}-skill"
    return name


def unique_id(base: str, taken) -> str:
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


# ══════════════════════════════════════════════════════════════════════
#  默认分类树（信息对抗技术 + 法学 方向，用户可随时手工增删改）
# ══════════════════════════════════════════════════════════════════════

def default_categories() -> dict:
    tree = [
        ("信息对抗", "渗透测试 / 红队作战全流程", [
            ("侦察测绘", "资产测绘、指纹识别、子域与端口"),
            ("漏洞利用", "漏洞验证、POC、提权"),
            ("后渗透", "持久化、横向移动、数据窃取"),
            ("社会工程", "钓鱼、钓鱼邮件、诱骗"),
            ("免杀对抗", "EDR/杀软对抗、痕迹清理"),
            ("对抗报告", "战果整理、复盘、合规披露"),
        ]),
        ("安全研究", "漏洞与逆向方向", [
            ("漏洞研究", "CVE跟踪、漏洞挖掘、补丁分析"),
            ("逆向工程", "反汇编、脱壳、协议分析"),
            ("二进制与移动端", "ELF/PE、安卓/iOS"),
            ("密码学", "加密算法分析、密码强度"),
            ("无线与硬件", "WiFi、蓝牙、RFID、嵌入式"),
            ("样本分析", "恶意样本、混淆、行为分析"),
        ]),
        ("AI安全", "大模型与 AI 应用方向", [
            ("模型应用攻击", "提示词注入、越狱、工具滥用"),
            ("数据与隐私", "训练数据泄露、隐私合规"),
            ("AI 辅助安全", "用 AI 做检测、研判与自动化"),
        ]),
        ("数字取证与反制", "事后追查与自我保护", [
            ("取证分析", "磁盘/内存/流量取证"),
            ("溯源反追踪", "攻击者溯源、痕迹关联"),
            ("隐蔽通信", "匿名化、隐写、通道伪装"),
        ]),
        ("法学", "法律研究与文书方向", [
            ("法律检索", "法条法规检索、法律数据库"),
            ("案例研究", "判例检索、类案分析"),
            ("法律文书", "起诉状、代理词、法律意见书"),
            ("合同与合规", "合同审查、合规尽调、隐私政策"),
            ("立法与政策", "立法研究、政策分析"),
            ("学术写作", "论文、文献综述、引用规范"),
        ]),
        ("网络与运维", "服务器与基础设施", [
            ("系统管理", "Windows/Linux 运维"),
            ("容器与部署", "Docker、K8s、CI/CD"),
            ("网络排障", "连通性、DNS、抓包"),
            ("监控与日志", "日志分析、告警"),
        ]),
        ("开发工程", "写代码相关", [
            ("前端", "页面、组件、样式"),
            ("后端", "接口、服务、数据库"),
            ("测试", "单测、E2E、质量"),
            ("代码审查与重构", "Review、重构、坏味道"),
            ("版本控制", "Git、分支、提交规范"),
            ("文档", "README、注释、接口文档"),
        ]),
        ("办公与自动化", "日常办公效率", [
            ("PPT", "排版、内容生成、视觉美化"),
            ("表格与数据", "Excel、清洗、透视、图表"),
            ("文档处理", "PDF、Word、格式转换"),
            ("图像与视频", "配图、修图、剪辑"),
            ("写作与翻译", "撰写、润色、多语言"),
            ("自动化脚本", "批处理、流程自动化"),
        ]),
        (UNCATEGORIZED, "新收录的先落这里", []),
    ]
    categories = []
    for name, desc, children in tree:
        categories.append({
            "name": name,
            "desc": desc,
            "children": [{"name": child_name, "desc": child_desc}
                         for child_name, child_desc in children],
        })
    return {"version": SCHEMA_VERSION, "categories": categories}


# ══════════════════════════════════════════════════════════════════════
#  默认扫描位置
# ══════════════════════════════════════════════════════════════════════

def default_scan_roots() -> list[dict]:
    home = Path.home()
    raw = [
        ("~/.config/opencode/skills", "opencode 全局技能目录"),
        ("~/.config/opencode/skill", "opencode 全局技能目录（单数兼容）"),
        ("~/.claude/skills", "Claude Code 技能目录"),
        ("~/.agents/skills", "通用 Agent 技能目录"),
        ("~/.codex/skills", "Codex 技能目录"),
        ("~/.gemini/skills", "Gemini 技能目录"),
        ("~/.copilot/skills", "GitHub Copilot 技能目录"),
    ]
    roots = []
    for path, label in raw:
        roots.append({"path": str(home / path[2:].replace("/", os.sep)), "label": label,
                      "enabled": True, "builtin": True})
    return roots


def project_scan_subdirs() -> list[str]:
    return [".opencode/skills", ".opencode/skill", ".claude/skills", ".agents/skills",
            ".codex/skills", "skills"]


# ══════════════════════════════════════════════════════════════════════
#  技能库
# ══════════════════════════════════════════════════════════════════════

# 网页服务是多线程的，而 Library 里的 catalog 是共享的可变对象：
# 界面上「勾 5 个一起改分类」会同时发 5 个 PUT，不串行化的话
# 读-改-写会互相覆盖，写文件也会撞车。命令行是单线程，用不用锁都一样。
WRITE_LOCK = threading.RLock()


class Library:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.skills_dir = self.dir / "skills"
        self.trash_dir = self.dir / "_trash"
        self.records_dir = self.dir / "_projects"
        self.catalog_path = self.dir / "catalog.json"
        self.categories_path = self.dir / "categories.json"
        self.projects_path = self.dir / "projects.json"
        self.config_path = self.dir / "config.json"
        self.catalog: dict = {}
        self.categories: dict = {}
        self.projects: dict = {}
        self.config: dict = {}
        self._ready = False
        self._body_index: dict | None = None
        self._body_stamps: dict = {}

    # ---------- 初始化 / 落盘 ----------

    def ensure(self) -> "Library":
        if self._ready:
            return self
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

        self.catalog = read_json(self.catalog_path, None) or {
            "version": SCHEMA_VERSION, "createdAt": now_iso(), "updatedAt": now_iso(), "skills": {},
        }
        self.catalog.setdefault("skills", {})
        self.categories = read_json(self.categories_path, None) or default_categories()
        self.projects = read_json(self.projects_path, None) or {
            "version": SCHEMA_VERSION, "projects": {},
        }
        self.config = read_json(self.config_path, None) or {
            "version": SCHEMA_VERSION,
            "scanRoots": default_scan_roots(),
            "customRoots": [],
            "collectDup": "ask",
        }
        self.config.setdefault("scanRoots", default_scan_roots())
        self.config.setdefault("customRoots", [])
        self.config.setdefault("collectDup", "ask")

        self._recover_missing_entries()
        self._ready = True
        self.save_all()
        return self

    def _recover_missing_entries(self) -> None:
        r"""自愈：skills\ 下有文件夹但 catalog 没记录（手工拷进来的、或条目被误删）
        → 重新登记。

        关键：优先用文件夹里已有的 skill.json 还原元数据。
        否则重新分析 SKILL.md 会把分类、收藏、来源全丢成默认值，
        等于"恢复"反而破坏了用户已有的整理结果。
        """
        if not self.skills_dir.exists():
            return
        known = set(self.catalog["skills"].keys())
        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir() or child.name in known:
                continue
            skill_md = find_skill_md(child)
            if not skill_md:
                continue
            record = None
            meta_path = child / META_FILE
            if meta_path.is_file():
                try:
                    existing = json.loads(meta_path.read_text(encoding="utf-8-sig"))
                    if isinstance(existing, dict) and existing.get("id") == child.name:
                        record = existing
                except Exception:
                    record = None
            if record is None:
                try:
                    info = analyze_skill_md(read_text_smart(skill_md))
                except Exception:
                    continue
                record = self._build_record(
                    skill_id=child.name, info=info,
                    source={"type": "unknown", "path": str(child),
                            "addedAt": today(), "contentHash": info["hash"]},
                )
            else:
                info = analyze_skill_md(read_text_smart(skill_md))
                record["contentHash"] = info["hash"]
                record.setdefault("usedBy", [])
                record.setdefault("starred", False)
                record.setdefault("category", UNCATEGORIZED)
            # 分类必须真实存在于分类树里，否则清回未分类
            if self.resolve_or_none(record.get("category")) is None:
                record["category"] = UNCATEGORIZED
            self.catalog["skills"][child.name] = record
            write_json(meta_path, record)

    def resolve_or_none(self, category: str) -> str | None:
        """校验分类，不存在返回 None（不抛异常）。"""
        try:
            return self.resolve_category(category)
        except SkmError:
            return None

    def save_all(self) -> None:
        self.catalog["version"] = SCHEMA_VERSION
        self.catalog["updatedAt"] = now_iso()
        write_json(self.catalog_path, self.catalog)
        write_json(self.categories_path, self.categories)
        write_json(self.projects_path, self.projects)
        write_json(self.config_path, self.config)
        self.invalidate_body_index()

    # ---------- 正文索引（网页搜索用） ----------

    def body_index(self) -> dict:
        """{id: 正文纯文本（小写）}，第一次搜索时才建，之后复用。

        每次调用都会 stat 一遍所有 SKILL.md（几十个 stat，可以忽略不计），
        只把"改动过的"重新读一遍。这样不管你是通过界面改的、命令行改的、
        还是直接拿记事本手工改的 SKILL.md，搜索结果都不会过期——
        比单纯"写操作后作废整个索引"可靠得多。
        """
        index = self._body_index or {}
        stale = True
        if self._body_index is not None and set(index) == set(self.catalog["skills"]):
            stale = False
            for skill_id, stamp in self._body_stamps.items():
                path = self.skill_md_path(skill_id)
                try:
                    info = path.stat()
                except OSError:
                    stale = True
                    break
                if (info.st_mtime_ns, info.st_size) != stamp:
                    stale = True
                    break
        if stale:
            index = {}
            stamps = {}
            for skill_id in self.catalog["skills"]:
                path = self.skill_md_path(skill_id)
                if not path.is_file():
                    continue
                try:
                    info = path.stat()
                    _, body = split_frontmatter(read_text_smart(path))
                except OSError:
                    continue
                # 保留代码块内容：渗透测试类 Skill 的关键词几乎都在 bash 块里
                plain = markdown_to_text(body, limit=200000, keep_code=True)
                index[skill_id] = plain.lower()
                stamps[skill_id] = (info.st_mtime_ns, info.st_size)
            self._body_index = index
            self._body_stamps = stamps
        return index

    def invalidate_body_index(self) -> None:
        self._body_index = None
        self._body_stamps = {}

    # ---------- 分类 ----------

    def category_paths(self) -> dict[str, str]:
        """{'PPT/排版': 'PPT', '未分类': ''} —— 小类名 → 一级类名。"""
        mapping = {UNCATEGORIZED: ""}
        for cat in self.categories.get("categories", []):
            top = cat.get("name", "")
            mapping[top] = ""
            for child in cat.get("children", []) or []:
                mapping[f"{top}/{child.get('name','')}"] = top
        return mapping

    def resolve_category(self, category: str) -> str:
        """校验分类路径存在，不存在就报错（返回规范写法）。"""
        category = (category or UNCATEGORIZED).strip().replace("\\", "/").strip("/")
        known = self.category_paths()
        if category not in known:
            top_names = [name for name, parent in known.items() if not parent and name]
            raise SkmError(f"分类「{category}」不存在。可用一级分类：{'、'.join(top_names)}\n"
                           f"    新增分类请直接编辑 {self.categories_path.name}")
        return category

    def ensure_category_exists(self, category: str) -> None:
        """--create 时自动补出分类节点。"""
        if category in self.category_paths():
            return
        parts = [part.strip() for part in category.replace("\\", "/").split("/") if part.strip()]
        top = self.categories["categories"]
        if not parts:
            return
        node = next((item for item in top if item.get("name") == parts[0]), None)
        if node is None:
            node = {"name": parts[0], "desc": "", "children": []}
            top.append(node)
        for sub in parts[1:]:
            node.setdefault("children", [])
            child = next((item for item in node["children"] if item.get("name") == sub), None)
            if child is None:
                child = {"name": sub, "desc": ""}
                node["children"].append(child)
            node = child

    # ---------- Skill 记录 ----------

    def _build_record(self, skill_id: str, info: dict, source: dict) -> dict:
        return {
            "id": skill_id,
            "name": info["name"],
            "summary": info["summary"],
            "tags": info["tags"],
            "category": UNCATEGORIZED,
            "source": source,
            "starred": False,
            "usedBy": [],
            "updatedAt": today(),
        }

    def skill_dir(self, skill_id: str) -> Path:
        return self.skills_dir / skill_id

    def skill_md_path(self, skill_id: str) -> Path:
        return self.skill_dir(skill_id) / SKILL_MD

    def get(self, skill_id: str) -> dict:
        record = self.catalog["skills"].get(skill_id)
        if record is None:
            raise SkmError(f"技能库里没有「{skill_id}」。用 skm list 看看有哪些")
        return record

    def find_by_hash(self, digest: str) -> list[str]:
        return [sid for sid, rec in self.catalog["skills"].items()
                if rec.get("source", {}).get("contentHash") == digest]

    def resolve_selector(self, token: str) -> str:
        """允许用 id 或名称片段定位一个技能。"""
        token = token.strip()
        if token in self.catalog["skills"]:
            return token
        matches = [sid for sid, rec in self.catalog["skills"].items() if token == rec.get("name")]
        if len(matches) == 1:
            return matches[0]
        lowered = token.lower()
        matches = [sid for sid in self.catalog["skills"].keys() if lowered in sid.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SkmError(f"找不到技能「{token}」")
        raise SkmError(f"「{token}」匹配到多个：{'、'.join(matches[:8])}，请用 id 精确指定")

    def set_category(self, skill_ids, category: str, create: bool = False) -> None:
        if create:
            self.ensure_category_exists(category)
        category = self.resolve_category(category)
        for skill_id in skill_ids:
            record = self.get(skill_id)
            record["category"] = category
            write_json(self.skill_dir(skill_id) / META_FILE, record)

    def set_tags(self, skill_id: str, tags: list[str], replace: bool = False) -> None:
        record = self.get(skill_id)
        merged = list(tags) if replace else record.get("tags", []) + tags
        seen, result = set(), []
        for tag in merged:
            tag = str(tag).strip()
            if tag and tag not in seen:
                seen.add(tag)
                result.append(tag)
        record["tags"] = result
        write_json(self.skill_dir(skill_id) / META_FILE, record)

    def count_by_category(self, rollup: bool = True) -> dict[str, int]:
        """各分类的 Skill 数量。

        rollup=True 时把「PPT/排版」的计数也累加到父级「PPT」，
        这样分类树里一级分类显示的是整棵子树的数量，而不是 0。
        """
        counts: dict[str, int] = {}
        for record in self.catalog["skills"].values():
            category = record.get("category") or UNCATEGORIZED
            counts[category] = counts.get(category, 0) + 1
        if not rollup:
            return counts
        for path in list(counts):
            if "/" in path:
                top = path.split("/", 1)[0]
                counts[top] = counts.get(top, 0) + counts[path]
        return counts


# ══════════════════════════════════════════════════════════════════════
#  查找 / 复制 Skill 文件夹
# ══════════════════════════════════════════════════════════════════════

def find_skill_md(folder: Path) -> Path | None:
    """在文件夹里找 SKILL.md（容忍大小写与 skill.md 写法）。"""
    if not folder.is_dir():
        return None
    for name in (SKILL_MD, "skill.md", "Skill.md", "SKILL.MD", "Skill.Md"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    try:
        for child in folder.iterdir():
            if child.is_file() and child.name.lower() == "skill.md":
                return child
    except OSError:
        return None
    return None


def copy_skill_tree(src: Path, dst: Path) -> list[str]:
    """复制 Skill 本体到库里，返回相对路径清单。跳过依赖/构建目录。"""
    dst.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for root, dirs, names in os.walk(src):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_NAMES)
        rel_root = Path(root).relative_to(src)
        for name in sorted(names):
            if name in SKIP_NAMES or name.endswith((".pyc", ".tmp")):
                continue
            source_file = Path(root) / name
            rel = rel_root / name
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            files.append(rel.as_posix())
    return files


def iter_skill_folders(root: Path, max_depth: int = MAX_SCAN_DEPTH):
    """在 root 下逐层找含 SKILL.md 的文件夹。

    命中后不再往该文件夹下钻：避免"外层目录也含 SKILL.md"时把内层技能一起吞掉。
    """

    def walk(folder: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except (OSError, PermissionError):
            return
        subdirs = [item for item in entries
                   if item.is_dir() and item.name not in SKIP_NAMES and not item.name.startswith(".")]
        if folder != root and find_skill_md(folder) is not None:
            yield folder
            return
        for sub in subdirs:
            yield from walk(sub, depth + 1)

    yield from walk(root, 1)


# ══════════════════════════════════════════════════════════════════════
#  收录核心
# ══════════════════════════════════════════════════════════════════════

def collect(lib: Library, source_path: Path, source_type: str = "local",
            assume_yes: bool = False, category: str | None = None) -> dict | None:
    """把一个 SKILL.md（或其所在文件夹）收录进库。返回记录，放弃则返回 None。"""
    if not source_path.exists():
        raise SkmError(f"路径不存在：{source_path}")

    if source_path.is_file():
        skill_md = source_path
        tree_root = source_path.parent
        single_file = True
    else:
        skill_md = find_skill_md(source_path)
        if skill_md is None:
            raise SkmError(f"{source_path} 里没有 SKILL.md")
        tree_root = source_path
        single_file = False

    text = read_text_smart(skill_md)
    info = analyze_skill_md(text)

    base_id = slugify(skill_md.parent.name if single_file else tree_root.name)
    taken = set(lib.catalog["skills"].keys())

    # --- 查重 1：同一文件夹名已收录 ---
    if base_id in taken:
        existing = lib.catalog["skills"][base_id]
        same_hash = existing.get("source", {}).get("contentHash") == info["hash"]
        if same_hash:
            print(f"{paint('·', C.GREY)} {paint(base_id, C.BOLD)} 已收录且内容一致，跳过"
                  f"{paint('（' + existing.get('source', {}).get('path', '') + '）', C.GREY)}")
            return None
        if not assume_yes:
            print(f"{paint('!', C.YELLOW, C.BOLD)} 「{base_id}」已存在，但内容不同：")
            print(f"    库里：{existing.get('source', {}).get('path', '?')}"
                  f"（{existing.get('source', {}).get('addedAt', '?')}）")
            print(f"    本次：{skill_md}")
            choice = ask_choice("覆盖库里版本 / 跳过 / 另存为新技能", ["覆盖", "跳过", "另存"], 2)
        else:
            choice = "覆盖"
        if choice == "跳过":
            return None
        if choice == "另存":
            base_id = unique_id(base_id, taken)
        else:
            shutil.rmtree(lib.skill_dir(base_id), ignore_errors=True)
            return _write_skill(lib, base_id, info, skill_md, tree_root, single_file,
                                {"type": source_type, "path": str(tree_root),
                                 "addedAt": today(), "contentHash": info["hash"],
                                 "files": 0}, category)
    else:
        # --- 查重 2：内容哈希一样但文件夹名不同 ---
        twins = lib.find_by_hash(info["hash"])
        if twins:
            print(f"{paint('!', C.YELLOW, C.BOLD)} 内容与已有技能完全相同：{'、'.join(twins)}")
            if not assume_yes:
                choice = ask_choice("仍然收录为新技能 / 跳过", ["仍然收录", "跳过"], 2)
            else:
                choice = "跳过"
            if choice == "跳过":
                return None
            base_id = unique_id(base_id, taken)

    source = {
        "type": source_type,
        "path": str(tree_root if not single_file else skill_md),
        "addedAt": today(),
        "contentHash": info["hash"],
        "files": 0,
    }
    return _write_skill(lib, base_id, info, skill_md, tree_root, single_file, source, category)


def _write_skill(lib: Library, skill_id: str, info: dict, skill_md: Path, tree_root: Path,
                 single_file: bool, source: dict, category: str | None) -> dict:
    target_dir = lib.skill_dir(skill_id)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    if single_file:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_md, target_dir / SKILL_MD)
        files = [SKILL_MD]
    else:
        files = copy_skill_tree(tree_root, target_dir)
    source["files"] = len(files)

    record = lib._build_record(skill_id, info, source)
    if category:
        try:
            record["category"] = lib.resolve_category(category)
        except SkmError:
            lib.ensure_category_exists(category)
            record["category"] = category
    lib.catalog["skills"][skill_id] = record
    write_json(target_dir / META_FILE, record)
    return record


def print_collected(record: dict) -> None:
    tags = " ".join("#" + tag for tag in record.get("tags", [])[:5])
    print(f"{paint('✓', C.GREEN, C.BOLD)} 已收录 {paint(record['id'], C.BOLD)}"
          f"  {paint(record['name'], C.CYAN)}")
    if record.get("summary"):
        info(f"{trunc(record['summary'], term_width() - 10)}")
    if tags:
        info(paint(tags, C.MAGENTA))
    info(paint(f"→ {lib_path_hint(record)}", C.GREY))


def lib_path_hint(record: dict) -> str:
    return f"{record.get('source', {}).get('path', '')}"


# ══════════════════════════════════════════════════════════════════════
#  交互
# ══════════════════════════════════════════════════════════════════════

def is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {paint('[' + default + ']', C.GREY)}" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise Abort()
    except KeyboardInterrupt:
        print()
        raise Abort()
    return answer or default


def ask_choice(prompt: str, options: list[str], default: int = 1) -> str:
    while True:
        answer = ask(f"{prompt} {paint('[1-' + str(len(options)) + '，默认 ' + str(default) + ']', C.GREY)}",
                     str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if not answer:
            return options[default - 1]
        for option in options:
            if answer == option or option.startswith(answer):
                return option
        print(paint(f"  请输入 1-{len(options)} 之间的编号", C.RED))


def parse_selection(text: str, total: int) -> list[int]:
    """解析 '1,3,5-8' / 'all' 这类选择串，返回排好序的编号列表。"""
    text = text.strip().lower()
    if text in ("all", "a", "全部", "y", "yes"):
        return list(range(1, total + 1))
    picked: set[int] = set()
    for chunk in re.split(r"[,，\s]+", text):
        if not chunk:
            continue
        if "-" in chunk:
            left, _, right = chunk.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                raise SkmError(f"看不懂的编号：{chunk}")
            if start > end:
                start, end = end, start
            picked.update(range(start, end + 1))
        else:
            try:
                picked.add(int(chunk))
            except ValueError:
                raise SkmError(f"看不懂的编号：{chunk}")
    return sorted(item for item in picked if 1 <= item <= total)


# ══════════════════════════════════════════════════════════════════════
#  命令：init
# ══════════════════════════════════════════════════════════════════════

def cmd_init(args, lib: Library) -> int:
    header("初始化技能库")
    ok(f"数据目录：{paint(str(lib.dir), C.CYAN)}")
    for name in (lib.skills_dir, lib.trash_dir, lib.records_dir):
        ok(f"目录就绪：{name}")
    for path in (lib.catalog_path, lib.categories_path, lib.projects_path, lib.config_path):
        ok(f"清单就绪：{path.name}")
    print()
    info(f"当前收录 {len(lib.catalog['skills'])} 个 Skill，"
         f"{len(lib.categories.get('categories', []))} 个一级分类")
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令：scan
# ══════════════════════════════════════════════════════════════════════

def resolve_scan_roots(lib: Library, extra: list[str] | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def push(raw: str, label: str) -> None:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        path = Path(expanded)
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    for item in lib.config.get("scanRoots", []):
        if item.get("enabled", True):
            push(item.get("path", ""), item.get("label", ""))
    for item in lib.config.get("customRoots", []):
        if isinstance(item, dict):
            if item.get("enabled", True):
                push(item.get("path", ""), item.get("label", "自定义"))
        else:
            push(str(item), "自定义")
    # 已登记项目的技能目录
    for project in lib.projects.get("projects", {}).values():
        base = Path(os.path.expanduser(project.get("path", "")))
        for sub in project_scan_subdirs():
            push(str(base / sub.replace("/", os.sep)), f"项目 {project.get('name','')}")
    for item in extra or []:
        push(item, "命令行指定")
    return roots


def scan_candidates(lib: "Library", roots: list[Path]) -> dict:
    """扫描给定位置，找出所有含 SKILL.md 的文件夹，并区分「未收录」和「已收录」。

    命令行 `skm scan` 和网页界面的「+收录」按钮共用这一份逻辑，
    保证两边看到的候选、判定重复的结果完全一致。

    返回：{roots, found, pending, already, fileTotal, dupCount, missingRoots, errors}
    """
    data_key = os.path.normcase(str(lib.dir.resolve() if lib.dir.exists() else lib.dir))

    found: list[dict] = []
    by_id: dict[str, dict] = {}
    missing: list[str] = []
    errors: list[str] = []
    file_total = 0

    for root in roots:
        try:
            if not root.exists() or not root.is_dir():
                missing.append(str(root))
                continue
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            continue
        for folder in iter_skill_folders(root):
            skill_md = find_skill_md(folder)
            if skill_md is None:
                continue
            try:
                if data_key in os.path.normcase(str(folder.resolve())):
                    continue  # 不扫描技能库自己，避免自我收录
            except OSError:
                continue
            file_total += 1
            try:
                parsed = analyze_skill_md(read_text_smart(skill_md))
            except Exception as exc:
                errors.append(f"读不出来 {skill_md}: {exc}")
                continue
            candidate_id = slugify(folder.name)
            entry = by_id.get(candidate_id)
            if entry is None:
                entry = {"id": candidate_id, "folder": str(folder),
                         "skillMd": str(skill_md), "path": str(folder),
                         "name": parsed["name"], "summary": parsed["summary"],
                         "tags": parsed["tags"], "hash": parsed["hash"],
                         "others": []}
                by_id[candidate_id] = entry
                found.append(entry)
            else:
                entry["others"].append(str(folder))

    found.sort(key=lambda item: item["id"].lower())

    pending: list[dict] = []
    already: list[dict] = []
    for entry in found:
        twins = lib.find_by_hash(entry["hash"])
        if entry["id"] in lib.catalog["skills"]:
            entry["collectedAs"] = entry["id"]
            already.append(entry)
        elif twins:
            entry["collectedAs"] = twins[0]
            already.append(entry)
        else:
            pending.append(entry)

    return {
        "roots": [str(item) for item in roots],
        "found": found,
        "pending": pending,
        "already": already,
        "fileTotal": file_total,
        "dupCount": sum(len(item["others"]) for item in found),
        "missingRoots": missing,
        "errors": errors,
    }


def cmd_scan(args, lib: Library) -> int:
    extra = list(args.path or []) + list(getattr(args, "paths", None) or [])
    if args.add_root:
        for item in args.add_root:
            lib.config.setdefault("customRoots", []).append(
                {"path": item, "label": "自定义", "enabled": True})

    roots = resolve_scan_roots(lib, extra)
    header("扫描本机已有 Skill", paint(f"共 {len(roots)} 个位置", C.GREY))

    result = scan_candidates(lib, roots)
    found = result["found"]
    pending = result["pending"]
    already = result["already"]
    file_total = result["fileTotal"]
    dup_count = result["dupCount"]
    missing_roots = result["missingRoots"]

    print()
    if not found:
        print(paint("没有发现任何 SKILL.md。", C.YELLOW))
        print()
        info(f"已探测 {len(roots)} 个位置，其中 {len(missing_roots)} 个不存在（首次使用属正常）。")
        print()
        info("可以手动指定路径：")
        info(paint("  skm scan --path D:\\share\\skills", C.CYAN))
        info(paint("  skm scan --add-root D:\\share\\skills    # 记住这个位置，以后每次都扫", C.CYAN))
        print()
        return 0

    # pending / already 已在 scan_candidates 里分好（命令行和网页界面共用同一套判定）
    width_id = min(max([dw(item["id"]) for item in found] + [4]), 34)

    if pending:
        print(f"发现 {paint(str(len(pending)), C.BOLD, C.CYAN)} 个未收录的 Skill：")
        print()
        for index, entry in enumerate(pending, 1):
            note = ""
            if entry["others"]:
                note = paint(f"  (另有 {len(entry['others'])} 处同名)", C.GREY)
            path_w = max(20, term_width() - width_id - 14)
            print(f"  {paint(pad(f'[{index}]', 5), C.YELLOW)}"
                  f"{paint(pad(trunc(entry['id'], width_id), width_id + 1), C.BOLD)}"
                  f"{trunc(str(entry['path']), path_w)}{note}")
        print()

    if already:
        print(paint(f"另有 {len(already)} 个已收录（跳过）：", C.GREY))
        for entry in already[:8]:
            print(paint(f"     {trunc(entry['id'], width_id + 1)} → 库中 {entry['collectedAs']}", C.GREY))
        if len(already) > 8:
            print(paint(f"     … 还有 {len(already) - 8} 个", C.GREY))
        print()

    if not pending:
        print(paint("全部都已在库里了。", C.GREEN))
        return 0

    print(paint(f"共读到 {file_total} 个 SKILL.md（{len(found)} 个不同技能"
                + (f"，{dup_count} 个是重复位置" if dup_count else "")
                + f"），缺 {len(missing_roots)} 个位置。", C.GREY))
    print()

    if args.select:
        chosen = parse_selection(args.select, len(pending))
    elif args.yes:
        chosen = list(range(1, len(pending) + 1))
    elif not is_tty():
        info("非交互环境，已自动全部收录（加 --yes 同效，或用 --select 1,3,5-8 指定编号）。")
        chosen = list(range(1, len(pending) + 1))
    else:
        print(paint("输入编号收录（1,3,5-8 回车确认，直接回车取消，全部收录输 all）：", C.GREY))
        answer = ask(paint("> ", C.BOLD), "")
        if not answer.strip():
            print(paint("已取消，什么都没收录。", C.GREY))
            return 0
        chosen = parse_selection(answer, len(pending))
        if not chosen:
            print(paint("没选中任何编号，已取消。", C.GREY))
            return 0

    rule()
    print(f"准备收录 {len(chosen)} 个 Skill：")
    print()
    collected: list[dict] = []
    skipped = 0
    for number in chosen:
        entry = pending[number - 1]
        try:
            record = collect(lib, Path(entry["path"]), "scan", assume_yes=args.yes,
                             category=getattr(args, "category", None))
        except SkmError as exc:
            fail(str(exc))
            skipped += 1
            continue
        if record is None:
            skipped += 1
            continue
        collected.append(record)
        print_collected(record)
        # 同一 id 的其它位置记进 alsoAt，便于以后知道还有哪些地方有这个 Skill
        for other in entry["others"]:
            record["source"].setdefault("alsoAt", [])
            record["source"]["alsoAt"].append(str(other))

    lib.save_all()
    print()
    ok(f"新增 {paint(str(len(collected)), C.BOLD)} 个"
       + (f"，跳过 {skipped} 个" if skipped else "")
       + f"，库里共 {len(lib.catalog['skills'])} 个 Skill")
    counts = lib.count_by_category()
    print(paint(f"   分类分布：{counts.get(UNCATEGORIZED, 0)} 个在「{UNCATEGORIZED}」"
                f"（用 skm cat 或网页界面整理）", C.GREY))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令：add
# ═══════════════════════════════════════════════════════════════════════

def cmd_add(args, lib: Library) -> int:
    if args.text:
        return add_from_text(args, lib)

    targets = list(args.target or [])
    if not targets:
        raise SkmError("请指定要收录的路径，或用 skm add --text 粘贴新建")

    header(f"手动收录 {len(targets)} 个目标")
    collected = 0
    for raw in targets:
        path = Path(os.path.expanduser(raw))
        try:
            record = collect(lib, path, "local", assume_yes=args.yes,
                             category=args.category)
        except SkmError as exc:
            fail(str(exc))
            continue
        if record is None:
            continue
        print_collected(record)
        collected += 1
    lib.save_all()
    print()
    ok(f"新增 {collected} 个，库里共 {len(lib.catalog['skills'])} 个 Skill")
    return 0


def add_from_text(args, lib: Library) -> int:
    header("粘贴新建一个 Skill")
    print(paint(f"提示：内容可以多行；单独一行输入 {paint('<<<END>>>', C.CYAN)} 或按 Ctrl+C 结束。", C.GREY))
    print()

    if args.name:
        name = args.name
    else:
        name = ask(f"{paint('名称', C.BOLD)}", "")
    if not name:
        raise Abort()

    if args.summary:
        summary = args.summary
    else:
        summary = ask(f"{paint('简介', C.BOLD, C.CYAN)}（一句话，之后可改）", "")
    if not summary:
        summary = first_paragraph("") or name

    body_lines: list[str] = []
    if args.file:
        body_lines = read_text_smart(Path(os.path.expanduser(args.file))).splitlines()
    else:
        print(paint(f"{paint('内容', C.BOLD, C.MAGENTA)}：", C.GREY))
        try:
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() in ("<<<END>>>", "<<<END"):
                    break
                body_lines.append(line)
        except KeyboardInterrupt:
            print()
            raise Abort()

    body = "\n".join(body_lines).rstrip()
    if not body.strip():
        raise SkmError("内容是空的，没有生成任何东西")
    if not body.lstrip().startswith("#"):
        body = f"# {name}\n\n{body}"

    text = body
    parsed = analyze_skill_md(text)
    parsed["name"] = name          # 用户给的名字优先于正文第一个标题
    parsed["summary"] = summary    # 用户给的简介优先于自动提取
    if not parsed["tags"]:
        parsed["tags"] = guess_tags(name, summary, body)

    skill_id = slugify(name)
    taken = set(lib.catalog["skills"].keys())
    if skill_id in taken:
        skill_id = unique_id(skill_id, taken)
        print(warn(f"同名技能已存在，另存为 {skill_id}"))

    target_dir = lib.skill_dir(skill_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / SKILL_MD).write_text(body + "\n", encoding="utf-8", newline="\n")

    source = {"type": "text", "path": "skm add --text", "addedAt": today(),
              "contentHash": parsed["hash"], "files": 1}
    record = lib._build_record(skill_id, parsed, source)
    lib.catalog["skills"][skill_id] = record
    if args.category:
        try:
            record["category"] = lib.resolve_category(args.category)
        except SkmError:
            lib.ensure_category_exists(args.category)
            record["category"] = args.category
    write_json(target_dir / META_FILE, record)
    lib.save_all()

    ok(f"已保存：{paint(str(target_dir / SKILL_MD), C.CYAN)}")
    print_collected(record)
    print()
    info(paint(f"下一步：skm cat {skill_id} <分类>  归类；skm tag {skill_id} <标签>  打标签", C.GREY))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令：list
# ══════════════════════════════════════════════════════════════════════

def filter_records(lib: Library, args) -> list[dict]:
    records = list(lib.catalog["skills"].values())
    if args.category:
        target = args.category.replace("\\", "/").strip("/")
        records = [r for r in records
                   if (r.get("category") or UNCATEGORIZED) == target
                   or (r.get("category") or "").startswith(target + "/")]
    if args.tag:
        records = [r for r in records if args.tag in r.get("tags", [])]
    if args.starred:
        records = [r for r in records if r.get("starred")]
    if args.unused:
        records = [r for r in records if not r.get("usedBy")]
    if args.source:
        records = [r for r in records if r.get("source", {}).get("type") == args.source]
    if args.name:
        needle = args.name.lower()
        records = [r for r in records
                   if needle in r["id"].lower() or needle in (r.get("name") or "").lower()]
    if not args.all:
        records.sort(key=lambda r: (not r.get("starred"), r["id"].lower()))
    return records


def cmd_list(args, lib: Library) -> int:
    records = filter_records(lib, args)

    if args.json:
        print(json.dumps([{
            "id": r["id"], "name": r.get("name"), "summary": r.get("summary"),
            "tags": r.get("tags", []), "category": r.get("category") or UNCATEGORIZED,
            "starred": r.get("starred", False),
            "source": r.get("source", {}),
        } for r in records], ensure_ascii=False, indent=2))
        return 0

    title = "技能清单"
    if args.category:
        title += f"  分类={args.category}"
    if args.tag:
        title += f"  标签={args.tag}"
    header(title, paint(f"{len(records)} 条 / 共 {len(lib.catalog['skills'])} 个", C.GREY))

    if not records:
        print()
        print(paint("还没有符合条件的 Skill。", C.YELLOW))
        print()
        info("先来收集：")
        info(paint("  skm scan                     # 扫描本机已有的 Skill", C.CYAN))
        info(paint("  skm add <路径>               # 手动指定 SKILL.md 或文件夹", C.CYAN))
        info(paint("  skm add --text               # 粘贴文本新建一个", C.CYAN))
        print()
        return 0

    width = term_width()
    name_w = min(max(dw(r["id"]) for r in records), 26)
    cat_w = min(max(dw(r.get("category") or UNCATEGORIZED) for r in records), 18)
    tags_w = min(max(dw(" ".join(r.get("tags", [])[:3])) for r in records), 28)
    src_w = 6

    print()
    head = (pad("ID", name_w + 2) + pad("分类", cat_w + 2)
            + pad("标签", tags_w + 2) + pad("来源", src_w + 2) + "介绍")
    print(paint(head, C.GREY))
    print(line())

    for record in records:
        star = paint("★", C.YELLOW) if record.get("starred") else " "
        skill_id = pad(trunc(record["id"], name_w), name_w + 2 - (2 if record.get("starred") else 1))
        if record.get("starred"):
            skill_id = " " + skill_id
        category = pad(trunc(record.get("category") or UNCATEGORIZED, cat_w), cat_w + 2)
        tags = pad(trunc(" ".join(record.get("tags", [])[:3]), tags_w), tags_w + 2)
        src = pad(record.get("source", {}).get("type", "-"), src_w + 2)
        summary = trunc(record.get("summary") or "", max(10, width - name_w - cat_w - tags_w - src_w - 8))
        print(star + paint(skill_id, C.BOLD) + paint(category, C.CYAN)
              + paint(tags, C.MAGENTA) + paint(src, C.GREY) + paint(summary, C.DIM))

    print(line())
    # 分布图按当前过滤结果统计，而不是整库；只列叶子分类，父级汇总在「skm cat --list」里看
    local: dict[str, int] = {}
    for record in records:
        key = record.get("category") or UNCATEGORIZED
        local[key] = local.get(key, 0) + 1
    print(paint("分类分布：", C.GREY))
    parts = [f"{paint(k, C.CYAN)} {v}" for k, v in sorted(local.items(), key=lambda kv: (-kv[1], kv[0]))]
    for chunk_start in range(0, len(parts), 6):
        print("  " + "   ".join(parts[chunk_start:chunk_start + 6]))
    print()
    info(paint("看详情：skm show <id>     改分类：skm cat <id> <分类>     标签：skm tag <id> <标签...>", C.GREY))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令：show / cat / tag（第二步的入口，先给命令行版）
# ══════════════════════════════════════════════════════════════════════

def cmd_show(args, lib: Library) -> int:
    skill_id = lib.resolve_selector(args.id)
    record = lib.get(skill_id)
    header(record.get("name") or skill_id, skill_id)

    skill_md = lib.skill_md_path(skill_id)
    body = read_text_smart(skill_md) if skill_md.exists() else ""

    print()
    print(paint("介绍：", C.BOLD) + (record.get("summary") or paint("(无)", C.GREY)))
    print(paint("分类：", C.BOLD) + paint(record.get("category") or UNCATEGORIZED, C.CYAN))
    print(paint("标签：", C.BOLD) + paint(" ".join("#" + t for t in record.get("tags", [])) or "(无)", C.MAGENTA))
    print(paint("收藏：", C.BOLD) + ("★ 是" if record.get("starred") else "否"))
    source = record.get("source", {})
    print(paint("来源：", C.BOLD) + f"{source.get('type', '-')}  {source.get('path', '-')}")
    print(paint("收录：", C.BOLD) + f"{source.get('addedAt', '-')}    哈希：{source.get('contentHash', '-')}")
    print(paint("位置：", C.BOLD) + f"{lib.skill_dir(skill_id)}  ({source.get('files', 0)} 个文件)")
    if record.get("usedBy"):
        print(paint("项目：", C.BOLD) + "、".join(record["usedBy"]))

    if body:
        print()
        print(line())
        limit = args.lines if args.lines > 0 else 40
        lines = body.splitlines()
        print(paint(f"SKILL.md（共 {len(lines)} 行，显示前 {min(limit, len(lines))} 行）", C.GREY))
        print(line())
        for line_text in lines[:limit]:
            print(paint(line_text, C.DIM))
        if len(lines) > limit:
            print(paint(f"… 还有 {len(lines) - limit} 行（skm show {skill_id} --lines 0 看全文）", C.GREY))
    return 0


def _split_tokens(values) -> list[str]:
    """把 'a,b'、'a，b' 这种逗号分隔的写法拆开。

    注意 values 可能是单个字符串（argparse 的 --ids 只收一个值），
    直接迭代字符串会按字符拆开，所以先归一成列表。
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    tokens: list[str] = []
    for value in values:
        tokens.extend(part.strip() for part in re.split(r"[,，]", value) if part.strip())
    return tokens


def cmd_cat(args, lib: Library) -> int:
    """分类命令。参数形态：

    skm cat --list                          看分类树和各分类数量
    skm cat <id...> <分类>                   改这些 Skill 的分类（最后一个参数是分类）
    skm cat --ids a,b <分类>                 逗号分隔的 id
    skm cat --category <源分类> <目标分类>     把某个分类下全部移走
    skm cat <分类>                          只看这个分类下有哪些 Skill
    """
    items = _split_tokens(args.items)
    extra_ids = _split_tokens(args.id_list)
    bulk_source = args.category

    if args.list or (not items and not extra_ids and not bulk_source):
        return show_category_tree(lib)

    target = items[-1] if items else None
    head = items[:-1] if items else []
    ids = _split_tokens(head) + extra_ids

    # --category <源分类> <目标分类>：整类搬家
    if bulk_source:
        if not target:
            raise SkmError("用法：skm cat --category <源分类> <目标分类>")
        source_cat = lib.resolve_category(bulk_source)
        moving = [sid for sid, rec in lib.catalog["skills"].items()
                  if (rec.get("category") or UNCATEGORIZED) == source_cat]
        if not moving:
            warn(f"分类「{source_cat}」下没有 Skill")
            return 0
        lib.set_category(moving, target, create=args.create)
        lib.save_all()
        ok(f"把「{source_cat}」下 {len(moving)} 个 Skill 移到「{lib.resolve_category(target)}」")
        for sid in moving:
            print(paint(f"   {trunc(sid, 46)}", C.GREY))
        return 0

    # skm cat <分类>：只看一眼，不改
    if not ids and len(items) == 1:
        if items[0] in lib.category_paths():
            show_category_members(lib, lib.resolve_category(items[0]))
            return 0
        if items[0] in lib.catalog["skills"]:
            raise SkmError("缺少目标分类。用法：skm cat <id> <分类>")
        raise SkmError(f"既不是已收录的技能，也不是已有分类：{items[0]}\n"
                       f"    skm cat --list 看有哪些分类")

    if not target:
        raise SkmError("缺少目标分类。用法：skm cat <id...> <分类>")
    if not ids:
        raise SkmError("缺少技能 id。用法：skm cat <id...> <分类>")

    resolved = [lib.resolve_selector(token) for token in ids]
    lib.set_category(resolved, target, create=args.create)
    lib.save_all()
    ok(f"{len(resolved)} 个 Skill → {paint(lib.resolve_category(target), C.CYAN, C.BOLD)}")
    for sid in resolved:
        print(paint(f"   {trunc(sid, 46)}", C.GREY))
    return 0


def show_category_members(lib: Library, category: str) -> int:
    records = [rec for rec in lib.catalog["skills"].values()
               if (rec.get("category") or UNCATEGORIZED) == category]
    header(category, paint(f"{len(records)} 个 Skill", C.GREY))
    print()
    if not records:
        print(paint("这个分类下还是空的。", C.GREY))
        return 0
    for record in sorted(records, key=lambda r: r["id"].lower()):
        tags = " ".join("#" + t for t in record.get("tags", [])[:4])
        print(f"  {paint(pad(record['id'], 32), C.BOLD)}"
              f"{paint(trunc(record.get('summary') or '', max(10, term_width() - 52)), C.DIM)}")
        if tags:
            print(f"  {paint(' ' * 32 + tags, C.MAGENTA)}")
    return 0


def show_category_tree(lib: Library) -> int:
    counts = lib.count_by_category()
    header("分类树", paint(f"共 {len(lib.categories.get('categories', []))} 个一级分类", C.GREY))
    total = len(lib.catalog["skills"])
    print()
    print(f"{paint(paint('●', C.CYAN) + ' 全部', C.BOLD)} {paint(pad(str(total), 5, 'right'), C.BOLD)}")
    for cat in lib.categories.get("categories", []):
        name = cat.get("name", "?")
        count = counts.get(name, 0)
        desc = cat.get("desc") or ""
        print(f"{paint('○', C.CYAN)} {paint(pad(name, 14), C.BOLD)} "
              f"{paint(pad(str(count), 5, 'right'), C.CYAN if count else C.GREY)}"
              f"{paint('  ' + desc, C.GREY)}")
        for child in cat.get("children", []) or []:
            child_name = child.get("name", "")
            full = f"{name}/{child_name}"
            child_count = counts.get(full, 0)
            cdesc = child.get("desc") or ""
            print(f"   {paint('·', C.GREY)} {pad(child_name, 14)} "
                  f"{paint(pad(str(child_count), 5, 'right'), C.CYAN if child_count else C.GREY)}"
                  f"{paint('  ' + cdesc, C.GREY)}")
    print()
    info(paint(f"改分类树：直接编辑 {lib.categories_path}（支持你随时增删改各级分类）", C.GREY))
    return 0


def cmd_tag(args, lib: Library) -> int:
    items = _split_tokens(args.items)
    if not items:
        raise SkmError("用法：skm tag <id> [标签...]")
    ids = [lib.resolve_selector(token) for token in items[:1]]
    tags = items[1:]
    if not tags:
        for sid in ids:
            record = lib.get(sid)
            print(f"{paint(sid, C.BOLD)}  "
                  + paint(" ".join("#" + t for t in record.get("tags", [])) or "(无标签)", C.MAGENTA))
        return 0
    for sid in ids:
        lib.set_tags(sid, tags, replace=args.replace)
        record = lib.get(sid)
        print(f"{paint(sid, C.BOLD)}  " + paint(" ".join("#" + t for t in record.get("tags", [])), C.MAGENTA))
    lib.save_all()
    return 0


def cmd_star(args, lib: Library) -> int:
    ids = [lib.resolve_selector(token) for token in args.id]
    value = not args.off
    for sid in ids:
        lib.get(sid)["starred"] = value
        write_json(lib.skill_dir(sid) / META_FILE, lib.get(sid))
    lib.save_all()
    ok(f"{len(ids)} 个 Skill 已{'收藏' if value else '取消收藏'}")
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令：info / roots
# ══════════════════════════════════════════════════════════════════════

def cmd_info(args, lib: Library) -> int:
    header("技能库信息")
    print()
    print(f"  程序目录  {BASE_DIR}")
    print(f"  数据目录  {paint(str(lib.dir), C.CYAN)}")
    print(f"  Skill 数  {paint(str(len(lib.catalog['skills'])), C.BOLD)}")
    print(f"  分类数    {len(lib.categories.get('categories', []))} 个一级")
    print(f"  清单文件  {lib.catalog_path.name} / {lib.categories_path.name} / "
          f"{lib.projects_path.name} / {lib.config_path.name}")
    disk = sum(f.stat().st_size for f in lib.skills_dir.rglob("*") if f.is_file()) if lib.skills_dir.exists() else 0
    print(f"  占用空间  {human_size(disk)}")
    print()
    info(paint("这些 JSON 都是明文，随时可以打开看、手工改。", C.GREY))
    return 0


def cmd_roots(args, lib: Library) -> int:
    header("扫描位置")
    print()
    for item in lib.config.get("scanRoots", []):
        path = Path(os.path.expanduser(item.get("path", "")))
        mark = paint("✓", C.GREEN) if path.exists() else paint("—", C.GREY)
        print(f"  {mark} {pad(str(path), 52)} {paint(item.get('label', ''), C.GREY)}")
    customs = lib.config.get("customRoots", [])
    if customs:
        print()
        print(paint("  自定义位置：", C.GREY))
        for item in customs:
            path = Path(os.path.expanduser(item.get("path", "") if isinstance(item, dict) else str(item)))
            mark = paint("✓", C.GREEN) if path.exists() else paint("—", C.GREY)
            print(f"  {mark} {pad(str(path), 52)}")
    print()
    info(paint("新增：skm scan --add-root <路径>", C.GREY))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  极简 Markdown 渲染器
#
#  plan 第六节要求不装任何第三方依赖，但详情抽屉要显示 SKILL.md 内容，
#  所以自己渲染。覆盖 Skill 正文里真实会出现的语法：
#  ATX 标题、围栏代码块、有序/无序/嵌套列表、表格、引用、分隔线、
#  行内代码、粗体、斜体、删除线、链接、图片、自动链接。
#
#  安全前提：所有文本先 HTML 转义，转义之后才做正则替换插入标签，
#  所以原文里的 < > & 不可能逃逸成标签；链接只放行 http/https/mailto，
#  javascript: 之类一律降级成纯文本。
# ══════════════════════════════════════════════════════════════════════

def esc(text) -> str:
    """HTML 转义（& 必须最先处理）。"""
    text = "" if text is None else str(text)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_SAFE_SCHEME_RE = re.compile(r"^(?:https?:|mailto:|ftp:)", re.IGNORECASE)
_SAFE_IMG_RE = re.compile(r"^https?:", re.IGNORECASE)


def _md_link(href: str, inner_html: str) -> str:
    """只允许安全协议的链接，否则降级成纯文本。"""
    target = (href or "").strip()
    if _SAFE_SCHEME_RE.match(target):
        return f'<a href="{esc(target)}" target="_blank" rel="noopener noreferrer">{inner_html}</a>'
    return f'<span class="md-badlink">{inner_html}</span>'


def _md_image(href: str, alt: str) -> str:
    """图片同样必须校验协议。

    只放行 http/https：img 的 src 不像 a 的 href 那样会被浏览器拦，
    javascript: / data: 在 <img src> 上某些浏览器仍会执行或泄漏，
    所以这里宁可把非法图片整个丢掉，只留 alt 文字。
    """
    target = (href or "").strip()
    if _SAFE_IMG_RE.match(target):
        return f'<img src="{esc(target)}" alt="{alt}" loading="lazy">'
    return f'<span class="md-badlink">[{alt or "图片"}]</span>'


def render_inline(text: str) -> str:
    """渲染一行内的 Markdown。先转义，再逐条替换。"""
    out = esc(text)
    # 行内代码优先，避免代码里的 * _ 被当成强调
    codes: list[str] = []

    def stash_code(match: re.Match) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00CODE{len(codes) - 1}\x00"

    out = re.sub(r"`+([^`]+?)`+", stash_code, out)
    # 图片 / 链接
    out = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
                 lambda m: _md_image(m.group(2), m.group(1)), out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                 lambda m: _md_link(m.group(2), m.group(1)), out)
    # 自动链接 <https://…>
    out = re.sub(r"&lt;((?:https?|mailto):[^&\s]+)&gt;",
                 lambda m: _md_link(m.group(1), esc(m.group(1))), out)
    # 强调
    out = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", out)
    out = re.sub(r"~~(.+?)~~", r"<del>\1</del>", out)
    # 换行（同一段里的软换行）
    out = out.replace("\n", "<br>")
    for index, code in enumerate(codes):
        out = out.replace(f"\x00CODE{index}\x00", code)
    return out


_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
_ATX_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# 列表块里出现的代码行（收集时加的前缀，_build_list_tree 靠它认出来）
_CODE_MARK = "\x00code\x00"
# 顶层围栏按 CommonMark 只认最多 3 格缩进，但**列表项里的围栏必然缩进更深**
# （本项目实测：列表项里的 ```markdown 前面有 5 个空格），
# 所以判断「列表块内部」有没有围栏必须用这个不限制缩进的版本。
_LIST_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)")


def _is_block_start(line: str) -> bool:
    """判断这一行是否是新块的开头（用来终止上一段的收集）。"""
    if not line.strip():
        return True
    return bool(_FENCE_RE.match(line) or _ATX_RE.match(line) or _HR_RE.match(line)
                or line.lstrip().startswith(">")
                or _UL_RE.match(line) or _OL_RE.match(line))


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells, buf, in_code = [], "", False
    for ch in stripped:
        if ch == "\\":
            buf += ch
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(buf.strip())
            buf = ""
        else:
            buf += ch
    cells.append(buf.strip())
    return cells


def _collect_list(lines: list[str], start: int) -> tuple[list[str], int]:
    """收集一个列表块（含嵌套与续行），返回 (块内行, 下一行下标)。

    **列表项里可以嵌代码块**，这是真实文件里非常常见的写法：
    ```
    - 创建清单文件，包含：
      ```markdown
      - 幻灯片 0：xxx
      ```
    ```
    代码块里的 `- xxx` 是**给人看的示例文本**，不是列表项。
    不认 fences 的话它们会被当成嵌套列表，渲染出的 <li> 就比源文件多
    （`tests\test_markdown.py` 的「<li> 数量必须与源文件严格相等」会当场抓住）。
    所以这里给代码行加个内部前缀，交给 _build_list_tree 原样输出成 <pre>。
    """
    base: int | None = None
    block: list[str] = []
    index = start
    in_fence = False
    fence_char = ""
    while index < len(lines):
        line = lines[index]
        fence = _LIST_FENCE_RE.match(line)
        if fence:
            # 开围栏or 收围栏：收的时候要求同类围栏且长度不短于开的那道
            token = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, token[0]
            elif token[0] == fence_char and len(token) >= len(fence_char) * 3:
                in_fence = False
            block.append(_CODE_MARK + line)
            index += 1
            continue
        if in_fence:
            block.append(_CODE_MARK + line)
            index += 1
            continue
        if not line.strip():
            look = index + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines) and lines[look].startswith(" " * ((base or 0) + 2)):
                block.append("")
                index += 1
                continue
            break
        marker = _UL_RE.match(line) or _OL_RE.match(line)
        if marker:
            indent = len(marker.group(1))
            if base is None:
                base = indent
            if indent < base:
                break
            block.append(line)
            index += 1
            continue
        if base is not None and line.startswith(" " * (base + 1)):
            block.append(line)   # 列表项的续行
            index += 1
            continue
        break
    return block, index


def _build_list_tree(block: list[str]) -> list[dict]:
    """把缩进列表块转成列表节点数组。

    节点：{"ordered": bool, "level": int, "items": [{"text": str, "sub": [节点]}]}
    顶层可能并列多个列表（比如一个 ul 后面紧跟一个 ol），所以返回数组。

    子列表挂在父条目内部（<li>文字<ul>…</ul></li>）而不是另起一个 <li>，
    这样渲染出的 <li> 数量与源文件的列表项数量严格一致。
    """
    top: list[dict] = []
    stack: list[tuple[int, dict]] = []

    def pick(parent_items, indent: int, ordered: bool) -> dict:
        subs = parent_items[-1]["sub"] if parent_items else []
        if subs and subs[-1]["level"] == indent and subs[-1]["ordered"] == ordered:
            return subs[-1]
        node = {"ordered": ordered, "level": indent, "items": []}
        if parent_items:
            subs.append(node)
        else:
            top.append(node)
        return node

    for raw in block:
        if not raw.strip():
            continue
        if raw.startswith(_CODE_MARK):
            # 列表项里的代码块：挂到上一个条目上，不当成列表项
            if stack and stack[-1][1]["items"]:
                stack[-1][1]["items"][-1].setdefault("code", []).append(
                    raw[len(_CODE_MARK):])
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        marker = _UL_RE.match(raw) or _OL_RE.match(raw)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if marker:
            ordered = _OL_RE.match(raw) is not None
            if stack and indent > stack[-1][0] and stack[-1][1]["items"]:
                parent_items = stack[-1][1]["items"]
            else:
                stack.clear()
                parent_items = []
            node = pick(parent_items, indent, ordered)
            stack.append((indent, node))
            node["items"].append({"text": marker.group(3), "sub": []})
        elif stack and stack[-1][1]["items"]:
            stack[-1][1]["items"][-1]["text"] += " " + raw.strip()   # 续行
    return top


def _code_block_html(lines: list[str]) -> str:
    """列表项里的围栏代码 → **一个** <pre><code>。

    两件事：
    - 去掉首尾的 ``` 围栏行（它们是"代码的边框"，不是代码内容）
    - 整块合并成一个 <pre>。不合并的话每一行都会各包一个 <pre>，
      详情抽屉里会看到一堆碎片化的灰条。
    """
    body = list(lines)
    while body and _LIST_FENCE_RE.match(body[0]):
        body.pop(0)
    while body and _LIST_FENCE_RE.match(body[-1]):
        body.pop()
    return f"<pre><code>{esc(chr(10).join(body))}</code></pre>"


def _render_list(node: dict) -> str:
    tag = "ol" if node.get("ordered") else "ul"
    parts = []
    for item in node.get("items", []):
        inner = render_inline(item.get("text", ""))
        if item.get("code"):
            inner += _code_block_html(item["code"])
        for sub in item.get("sub", []):
            inner += _render_list(sub)
        parts.append(f"<li>{inner}</li>")
    return f"<{tag}>" + "".join(parts) + f"</{tag}>"


def render_markdown(text: str) -> str:
    """把 Markdown 渲染成 HTML 片段。所有输入都经过 esc()，不会产生注入。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0] * 3
            lang = fence.group(2)
            index += 1
            buf = []
            while index < total and not lines[index].strip().startswith(marker):
                buf.append(lines[index])
                index += 1
            index += 1  # 跳过收尾的 ```
            attr = f' class="lang-{esc(lang)}"' if lang else ""
            out.append(f"<pre><code{attr}>{esc(chr(10).join(buf))}</code></pre>")
            continue

        heading = _ATX_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            index += 1
            continue

        # 表格：当前行有 |，下一行是 |---|---| 分隔行
        if ("|" in line and index + 1 < total
                and _TABLE_SEP_RE.match(lines[index + 1]) and "-" in lines[index + 1]):
            header = _split_table_row(line)
            aligns = []
            for cell in _split_table_row(lines[index + 1]):
                mark = cell.strip()
                if mark.startswith(":") and mark.endswith(":"):
                    aligns.append("center")
                elif mark.endswith(":"):
                    aligns.append("right")
                else:
                    aligns.append("left")
            index += 2
            rows = []
            while index < total and lines[index].strip() and "|" in lines[index]:
                rows.append(_split_table_row(lines[index]))
                index += 1
            head_html = "".join(
                f'<th style="text-align:{aligns[i] if i < len(aligns) else "left"}">'
                f"{render_inline(cell)}</th>"
                for i, cell in enumerate(header))
            body_html = ""
            for row in rows:
                cells = "".join(
                    f'<td style="text-align:{aligns[i] if i < len(aligns) else "left"}">'
                    f"{render_inline(cell)}</td>"
                    for i, cell in enumerate(row))
                body_html += f"<tr>{cells}</tr>"
            out.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while index < total and lines[index].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s{0,3}>\s?", "", lines[index]))
                index += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(buf))}</blockquote>")
            continue

        if _UL_RE.match(line) or _OL_RE.match(line):
            block, index = _collect_list(lines, index)
            out.append("".join(_render_list(node) for node in _build_list_tree(block)))
            continue

        if not line.strip():
            index += 1
            continue

        buf = []
        while index < total and lines[index].strip() and not _is_block_start(lines[index]):
            buf.append(lines[index].strip())
            index += 1
        if buf:
            out.append(f"<p>{render_inline(' '.join(buf))}</p>")
        else:
            index += 1

    return "\n".join(out)


def markdown_to_text(text: str, limit: int = 400, keep_code: bool = False) -> str:
    """把 Markdown 压成纯文本。

    keep_code=False 时剥掉代码块（用于生成简介）。
    keep_code=True 时保留代码块内容 —— 搜索必须用这个：
    渗透测试类 Skill 的关键词（nuclei、crt.sh、Kerberoast…）几乎全在 bash 代码块里，
    剥掉等于搜不到最该搜的东西。
    """
    if not keep_code:
        text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    plain = re.sub(r"^#{1,6}\s+", "", text or "", flags=re.M)   # 标题标记
    plain = re.sub(r"[#>*`_~\-|]+", " ", plain)               # 行内标记
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]

# ══════════════════════════════════════════════════════════════════════
#  网页界面：skm ui
#
#  plan 第三步要求的界面：分类树 + 卡片墙 + 详情抽屉。
#  只用标准库：http.server 起本地服务，前端是一个内联 CSS/JS 的单 HTML 文件，
#  不引任何 CDN（要能离线用）。
#
#  安全考虑（这是个会写文件的本地服务）：
#   1. 只绑 127.0.0.1，不绑 0.0.0.0 —— 里面的 JSON 带本机路径，不该暴露到局域网
#   2. 校验 Host 头，防 DNS rebinding（防止 evil.com 把域名解析到 127.0.0.1 来打本服务）
#   3. 写操作要求 Content-Type: application/json 且不带跨域 Origin，
#      浏览器对非简单请求会先发 OPTIONS，而我们不回 CORS 头，跨源请求自然被拦
#   4. 所有 Markdown 输出都经过 esc()，链接/图片只放行 http(s) 协议
# ══════════════════════════════════════════════════════════════════════

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, unquote, parse_qs  # noqa: E402


def search_bodies(lib: Library, query: str, limit: int = 400) -> dict:
    """在所有 Skill 的 SKILL.md 正文里搜关键词。

    返回命中的 id 列表 + 每个命中处周围的片段（给界面显示"为什么命中"）。
    正文索引在 Library 里缓存，任何写操作后自动重建。

    搜索词按空格拆成多个词，要求**全部出现**（AND），
    这样搜"内网 提权"比搜"内网提权"更符合预期。
    """
    query = (query or "").strip().lower()
    if not query:
        return {"ids": [], "snippets": {}, "words": [], "scanned": 0}
    words = [w for w in query.split() if w]

    index = lib.body_index()
    hits: dict[str, list[int]] = {}
    for skill_id, plain in index.items():
        positions: list[int] = []
        for word in words:
            spot = plain.find(word)
            if spot < 0:
                positions = []
                break
            positions.append(spot)
        if positions:
            hits[skill_id] = positions

    # 排序：命中词少的（更精确）排前面；命中位置靠前的排前面
    ordered = sorted(hits.items(), key=lambda item: (len(item[1]), min(item[1])))[:limit]

    snippets = {}
    for skill_id, positions in ordered:
        plain = index[skill_id]
        spot = min(positions)
        start = max(0, spot - 40)
        piece = plain[start:start + 160]
        snippets[skill_id] = ("…" if start > 0 else "") + piece.strip() + (
            "…" if start + 160 < len(plain) else "")
    return {"ids": [item[0] for item in ordered], "snippets": snippets,
            "words": words, "scanned": len(index)}


def api_state(lib: Library) -> dict:
    """给前端的一次性全量状态。几十个 Skill 的规模，直接全发不做分页。"""
    skills = []
    for record in lib.catalog["skills"].values():
        folder = lib.skill_dir(record["id"])
        total = 0
        if folder.is_dir():
            for item in folder.rglob("*"):
                if item.is_file():
                    total += item.stat().st_size
        skills.append({
            "id": record["id"],
            "name": record.get("name") or record["id"],
            "summary": record.get("summary") or "",
            "tags": record.get("tags") or [],
            "category": record.get("category") or UNCATEGORIZED,
            "starred": bool(record.get("starred")),
            "usedBy": record.get("usedBy") or [],
            "sourceType": record.get("source", {}).get("type", "-"),
            "sourcePath": record.get("source", {}).get("path", ""),
            "addedAt": record.get("source", {}).get("addedAt", ""),
            "fileCount": record.get("source", {}).get("files", 0),
            "bytes": total,
        })
    skills.sort(key=lambda item: (not item["starred"], item["id"].lower()))
    return {
        "skills": skills,
        "categories": lib.categories.get("categories", []),
        "counts": lib.count_by_category(rollup=True),
        "leafCounts": lib.count_by_category(rollup=False),
        "dataDir": str(lib.dir),
        "version": VERSION,
    }


def api_skill_detail(lib: Library, skill_id: str) -> dict:
    record = lib.get(skill_id)
    skill_md = lib.skill_md_path(skill_id)
    raw = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
    _, body = split_frontmatter(raw)
    files = []
    folder = lib.skill_dir(skill_id)
    if folder.is_dir():
        for item in sorted(folder.rglob("*")):
            if item.is_file():
                files.append({"name": item.relative_to(folder).as_posix(),
                              "bytes": item.stat().st_size})
    return {"record": record, "markdown": body, "html": render_markdown(body),
            "rawBytes": len(raw.encode("utf-8")), "files": files}


def api_projects(lib: Library) -> dict:
    """项目清单 + 每个项目里现在装了什么（网页界面「复制到项目」用）。"""
    out = []
    for name, entry in sorted(lib.projects.get("projects", {}).items()):
        root = Path(entry.get("path", "")) / PROJECT_SUBDIR
        installed = []
        if root.is_dir():
            installed = sorted(p.name for p in root.iterdir()
                               if p.is_dir() and (p / SKILL_MD).is_file())
        out.append({"name": name, "path": entry.get("path", ""),
                    "exists": Path(entry.get("path", "")).is_dir(),
                    "root": str(root), "installed": installed,
                    "addedAt": entry.get("addedAt", "")})
    return {"projects": out}


def update_skill(lib: Library, skill_id: str, patch: dict) -> dict:
    """按前端传来的补丁更新一个 Skill。

    所有写入都走 Library 的方法，保证 catalog.json 与 skill.json 同步落盘。
    """
    lib.get(skill_id)

    if "category" in patch:
        category = str(patch["category"] or UNCATEGORIZED)
        create = bool(patch.get("createCategory"))
        lib.set_category([skill_id], category, create=create)

    record = lib.get(skill_id)  # set_category 会重写记录，重新取一次

    if "tags" in patch:
        tags = [str(item).strip() for item in (patch["tags"] or []) if str(item).strip()]
        lib.set_tags(skill_id, tags, replace=True)

    if "starred" in patch:
        record["starred"] = bool(patch["starred"])
    for key in ("name", "summary"):
        if key in patch:
            value = str(patch[key] or "").strip()
            if value:
                record[key] = value

    record["updatedAt"] = today()
    write_json(lib.skill_dir(skill_id) / META_FILE, record)
    lib.save_all()
    return record


def create_from_text(lib: Library, payload: dict) -> dict:
    """网页上的「+新建」：粘贴文本当场生成一个 Skill（对应 CLI 的 skm add --text）。"""
    name = str(payload.get("name") or "").strip()
    body = str(payload.get("content") or "")
    if not name:
        raise SkmError("请填技能名称")
    if not body.strip():
        raise SkmError("内容是空的，没有生成任何东西")
    if not body.lstrip().startswith("#"):
        body = f"# {name}\n\n{body}"
    summary = str(payload.get("summary") or "").strip() or first_paragraph(body) or name

    parsed = analyze_skill_md(body)
    parsed["name"] = name
    parsed["summary"] = summary
    if not parsed["tags"]:
        parsed["tags"] = guess_tags(name, summary, body)

    skill_id = slugify(name)
    if skill_id in lib.catalog["skills"]:
        skill_id = unique_id(skill_id, set(lib.catalog["skills"].keys()))

    folder = lib.skill_dir(skill_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SKILL_MD).write_text(body.rstrip() + "\n", encoding="utf-8", newline="\n")

    record = lib._build_record(skill_id, parsed, {
        "type": "text", "path": "skm ui → 新建", "addedAt": today(),
        "contentHash": parsed["hash"], "files": 1,
    })
    category = str(payload.get("category") or "").strip()
    if category:
        try:
            record["category"] = lib.resolve_category(category)
        except SkmError:
            lib.ensure_category_exists(category)
            record["category"] = category
    lib.catalog["skills"][skill_id] = record
    write_json(folder / META_FILE, record)
    lib.save_all()
    return record


def trash_skill(lib: Library, skill_id: str) -> dict:
    """软删除：整个文件夹移进 _trash，catalog 摘掉条目。第五步的 skm restore 从 _trash 找回。"""
    record = lib.get(skill_id)
    folder = lib.skill_dir(skill_id)
    lib.trash_dir.mkdir(parents=True, exist_ok=True)
    target = lib.trash_dir / f"{skill_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if folder.is_dir():
        shutil.move(str(folder), str(target))
    (lib.trash_dir / f"{skill_id}.json").write_text(
        json.dumps({"id": skill_id, "record": record, "trashedAt": now_iso(),
                    "folder": target.name}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    del lib.catalog["skills"][skill_id]
    lib.save_all()
    return {"id": skill_id, "trashedAs": target.name}


def collect_paths(lib: Library, paths: list) -> dict:
    """网页「+收录」确认后按路径批量收录（复用 CLI 的 collect，含查重）。"""
    added, skipped, failed = [], [], []
    for raw in paths or []:
        try:
            record = collect(lib, Path(os.path.expanduser(str(raw))), "scan", assume_yes=True)
        except SkmError as exc:
            failed.append({"path": str(raw), "error": str(exc)})
            continue
        if record is None:
            skipped.append(str(raw))
        else:
            added.append({"id": record["id"], "name": record["name"]})
    lib.save_all()
    return {"added": added, "skipped": skipped, "failed": failed}


class SkmServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 两个降噪处理。

    daemon_threads=True：Ctrl+C 关闭时不等残留连接。
    handle_error：浏览器刷新页面时会直接断开连接，默认行为是往 stderr 打一整页
    堆栈（ConnectionResetError / BrokenPipeError），对本地工具来说是纯噪音，
    只有真正的服务端错误才值得打印。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return          # 客户端自己断开的，忽略
        super().handle_error(request, client_address)


class SkmHandler(BaseHTTPRequestHandler):
    server_version = f"skm/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def lib(self) -> Library:
        return self.server.lib  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # ---------- 安全与基础工具 ----------

    def _host_ok(self) -> bool:
        """Host 头必须是 127.0.0.1 / localhost，防 DNS rebinding。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name.strip("[]") in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self, writing: bool) -> bool:
        if not writing:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return (urlparse(origin).hostname or "") in ("127.0.0.1", "localhost", "::1")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _read_json(self, required: bool = True) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise SkmError("Content-Length 不合法")
        if length <= 0:
            if required:
                raise SkmError("请求体不能为空")
            return {}          # DELETE 这类操作没有请求体是正常的
        if length > 8 * 1024 * 1024:
            raise SkmError("请求体过大")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise SkmError("请求必须是 application/json")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkmError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise SkmError("请求体必须是 JSON 对象")
        return data

    def _guard(self, writing: bool) -> bool:
        if not self._host_ok():
            self._fail("非法的 Host 头（只接受 127.0.0.1 / localhost）", 403)
            return False
        if not self._origin_ok(writing):
            self._fail("不接受跨来源的写请求", 403)
            return False
        return True

    # ---------- 路由 ----------

    def do_GET(self) -> None:
        if not self._guard(writing=False):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, load_ui_html().encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/skmtest.js":
                # 只有带 ?skmtest=1 打开页面时才会来要这个文件（见 ui.html 末尾）。
                # 测试脚本放在 tests\ 里，不进发布物；文件不在就 404。
                hook = BASE_DIR / "tests" / "skmtest.js"
                if not hook.is_file():
                    self._fail("没有这个接口", 404)
                    return
                self._send(200, read_text_smart(hook).encode("utf-8"),
                           "application/javascript; charset=utf-8")
            elif path == "/api/state":
                self._json(api_state(self.lib))
            elif path.startswith("/api/skill/"):
                self._json(api_skill_detail(self.lib, unquote(path[len("/api/skill/"):])))
            elif path == "/api/scan/preview":
                result = scan_candidates(self.lib, resolve_scan_roots(self.lib))
                self._json({"pending": result["pending"],
                            "alreadyCount": len(result["already"]),
                            "fileTotal": result["fileTotal"],
                            "dupCount": result["dupCount"],
                            "missingRoots": result["missingRoots"],
                            "errors": result["errors"]})
            elif path == "/api/config":
                # 注意：ai.apiKey 一定要抹掉再下发——这个接口是 GET，
                # 能访问本机服务的东西都能拿到响应体。
                cfg = dict(self.lib.config)
                if isinstance(cfg.get("ai"), dict):
                    cfg["ai"] = ai_public_config(ai_config(self.lib),
                                                 from_env=not _ai_key_in_config(self.lib))
                    cfg["ai"].pop("apiKey", None)
                self._json({"config": cfg, "dataDir": str(self.lib.dir),
                            "ai": ai_public_config(ai_config(self.lib),
                                                   from_env=not _ai_key_in_config(self.lib))})
            elif path == "/api/ai":
                self._json({"ai": ai_public_config(ai_config(self.lib),
                                                   from_env=not _ai_key_in_config(self.lib)),
                            "uncategorized": len(uncategorized_ids(self.lib))})
            elif path == "/api/projects":
                self._json(api_projects(self.lib))
            elif path == "/api/doctor":
                with WRITE_LOCK:
                    self._json(doctor(self.lib, fix=False))
            elif path == "/api/check":
                with WRITE_LOCK:
                    self._json(check_library(self.lib))
            elif path == "/api/trash":
                with WRITE_LOCK:
                    self._json({"items": trash_list(self.lib)})
            elif path == "/api/backup.zip":
                # 直接把 zip 吐给浏览器下载：用完即删，不在磁盘上留中间文件
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path_ in sorted(self.lib.dir.rglob("*")):
                        rel = path_.relative_to(self.lib.dir)
                        if any(part in BACKUP_SKIP_DIRS for part in rel.parts):
                            continue
                        if path_.is_dir() or path_.suffix in (".pyc", ".tmp"):
                            continue
                        zf.write(path_, rel.as_posix())
                    zf.writestr(BACKUP_MANIFEST, json.dumps(
                        {"app": APP_NAME, "version": VERSION, "schema": SCHEMA_VERSION,
                         "createdAt": now_iso(), "skills": len(self.lib.catalog["skills"])},
                        ensure_ascii=False, indent=2))
                raw = buf.getvalue()
                self._send(200, raw, "application/zip",
                           {"Content-Disposition":
                            f'attachment; filename="skills-{today()}.zip"'})
            elif path == "/api/project/status":
                name = (query.get("project") or [""])[0]
                self._json(project_status(self.lib, name))
            elif path == "/api/send/preview":
                name = (query.get("project") or [""])[0]
                ids = [x for x in (query.get("ids") or [""])[0].split(",") if x]
                mode = (query.get("mode") or ["copy"])[0]
                with WRITE_LOCK:
                    plan = plan_send(self.lib, name, ids, mode)
                self._json({"project": name, "mode": mode, "plan": plan,
                            "root": str(project_dir(self.lib, name))})
            elif path == "/api/search":
                started = time.perf_counter()
                result = search_bodies(self.lib, (query.get("q") or [""])[0])
                result["ms"] = round((time.perf_counter() - started) * 1000, 1)
                self._json(result)
            elif path == "/api/file":
                self._read_library_file(query.get("path", [""])[0])
            else:
                self._fail("没有这个接口", 404)
        except SkmError as exc:
            self._fail(str(exc), 404)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"服务端出错：{exc}", 500)

    def _read_library_file(self, rel: str) -> None:
        """只允许读技能库内的文件，挡掉 .. 穿越。"""
        base = self.lib.dir.resolve()
        target = (base / unquote(rel)).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise SkmError("路径越界，只允许读技能库内的文件")
        if not target.is_file():
            raise SkmError("文件不存在")
        self._send(200, target.read_bytes(), "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self._write_request("POST")

    def do_PUT(self) -> None:
        self._write_request("PUT")

    def do_DELETE(self) -> None:
        self._write_request("DELETE")

    def _write_request(self, method: str) -> None:
        if not self._guard(writing=True):
            return
        path = urlparse(self.path).path
        try:
            # DELETE 没有请求体是正常的，其余都要 JSON
            payload = self._read_json(required=(method != "DELETE"))
            # 整个写流程串行化：Library 是共享的可变对象，
            # 批量操作会同时发好几个 PUT/DELETE，并发改会互相覆盖。
            with WRITE_LOCK:
                if method == "PUT" and path.startswith("/api/skill/"):
                    skill_id = unquote(path[len("/api/skill/"):])
                    self._json({"ok": True, "record": update_skill(self.lib, skill_id, payload)})
                elif method == "POST" and path == "/api/skill/text":
                    self._json({"ok": True, "record": create_from_text(self.lib, payload)})
                elif method == "DELETE" and path.startswith("/api/skill/"):
                    skill_id = unquote(path[len("/api/skill/"):])
                    self._json({"ok": True, **trash_skill(self.lib, skill_id)})
                elif method == "POST" and path == "/api/collect":
                    self._json({"ok": True,
                                **collect_paths(self.lib, payload.get("paths") or [])})
                elif method == "POST" and path == "/api/category":
                    name = str(payload.get("name") or "").strip()
                    parent = str(payload.get("parent") or "").strip()
                    if not name:
                        raise SkmError("请填分类名")
                    full = f"{parent}/{name}" if parent else name
                    self.lib.ensure_category_exists(full)
                    self.lib.save_all()
                    self._json({"ok": True, "created": full,
                                "categories": self.lib.categories.get("categories", [])})
                elif method == "POST" and path == "/api/config/roots":
                    self._update_roots(payload)
                    self._json({"ok": True, "config": self.lib.config})
                elif method == "POST" and path == "/api/project":
                    name = str(payload.get("name") or "").strip()
                    folder = str(payload.get("path") or "").strip()
                    if not name or not folder:
                        raise SkmError("项目名和路径都要填")
                    if name in self.lib.projects.get("projects", {}):
                        raise SkmError(f"项目「{name}」已登记")
                    target = Path(os.path.expanduser(folder))
                    if not target.is_dir():
                        raise SkmError(f"文件夹不存在：{target}")
                    self.lib.projects.setdefault("projects", {})[name] = {
                        "path": str(target.resolve()), "addedAt": today()}
                    self.lib.save_all()
                    self._json({"ok": True, "projects": api_projects(self.lib)["projects"]})
                elif method == "DELETE" and path.startswith("/api/project/"):
                    name = unquote(path[len("/api/project/"):])
                    self.lib.projects.get("projects", {}).pop(name, None)
                    for record in self.lib.catalog["skills"].values():
                        if name in record.get("usedBy", []):
                            record["usedBy"] = [x for x in record["usedBy"] if x != name]
                            write_json(self.lib.skill_dir(record["id"]) / META_FILE, record)
                    self.lib.save_all()
                    self._json({"ok": True, "projects": api_projects(self.lib)["projects"]})
                elif method == "POST" and path == "/api/ai":
                    ai = self.lib.config.setdefault("ai", {})
                    for key in ("baseUrl", "model", "temperature", "bodyChars",
                                "createCategory", "batchSize"):
                        if key in payload:
                            ai[key] = payload[key]
                    if payload.get("apiKey"):
                        ai["apiKey"] = str(payload["apiKey"]).strip()   # 空字符串=保持原样
                    if payload.get("clearKey"):
                        ai["apiKey"] = ""
                    ai.setdefault("baseUrl", AI_DEFAULTS["baseUrl"])
                    ai.setdefault("model", AI_DEFAULTS["model"])
                    self.lib.save_all()
                    self._json({"ok": True,
                                "ai": ai_public_config(ai_config(self.lib),
                                                       from_env=not _ai_key_in_config(self.lib))})
                elif method == "POST" and path == "/api/ai/test":
                    cfg = ai_config(self.lib)
                    answer = ai_chat(cfg, [{"role": "user", "content": "只回复两个字：可用"}])
                    self._json({"ok": True, "answer": answer.strip()[:120]})
                elif method == "POST" and path == "/api/classify/preview":
                    ids = [str(x) for x in (payload.get("ids") or []) if str(x).strip()]
                    if not ids:
                        ids = uncategorized_ids(self.lib)
                    if payload.get("all"):
                        ids = sorted(self.lib.catalog["skills"])
                    limit = int(payload.get("limit") or 0)
                    if limit:
                        ids = ids[:limit]
                    if not ids:
                        self._json({"ok": True, "items": [], "message": "没有待分类的 Skill"})
                        return
                    cfg = ai_config(self.lib)
                    items = propose_categories(self.lib, ids, cfg)
                    self._json({"ok": True, "items": items,
                                "model": cfg.get("model"),
                                "created": now_iso()})
                elif method == "POST" and path == "/api/classify/apply":
                    decisions = payload.get("items") or []
                    if not isinstance(decisions, list) or not decisions:
                        raise SkmError("没有要应用的建议")
                    result = apply_categories(self.lib, decisions, create_missing=True)
                    self._json({"ok": True, **result})
                elif method == "POST" and path == "/api/doctor":
                    self._json({"ok": True, **doctor(self.lib, fix=bool(payload.get("fix")))})
                elif method == "POST" and path == "/api/restore":
                    token = str(payload.get("id") or "").strip()
                    if not token:
                        raise SkmError("要指定找回哪个 Skill")
                    self._json({"ok": True, **restore_skill(self.lib, token)})
                elif method == "POST" and path == "/api/send":
                    name = str(payload.get("project") or "").strip()
                    ids = [str(x) for x in (payload.get("ids") or []) if str(x).strip()]
                    mode = str(payload.get("mode") or "copy")
                    if not name or not ids:
                        raise SkmError("要选项目和 Skill")
                    self._json({"ok": True, **send_skills(
                        self.lib, name, ids, mode,
                        overwrite=str(payload.get("overwrite") or "backup"),
                        assume_yes=True)})
                elif method == "POST" and path == "/api/project/remove":
                    name = str(payload.get("project") or "").strip()
                    ids = [str(x) for x in (payload.get("ids") or []) if str(x).strip()]
                    if not name or not ids:
                        raise SkmError("要选项目和 Skill")
                    self._json({"ok": True, **remove_from_project(self.lib, name, ids)})
                else:
                    self._fail("没有这个接口", 404)
        except SkmError as exc:
            self._fail(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"服务端出错：{exc}", 500)

    def _update_roots(self, payload: dict) -> None:
        """设置面板：改扫描位置的启用状态 / 新增。"""
        config = self.lib.config
        for entry in payload.get("roots") or []:
            raw = str(entry.get("path") or "").strip()
            if not raw:
                continue
            enabled = bool(entry.get("enabled", True))
            for bucket in ("scanRoots", "customRoots"):
                for item in config.get(bucket, []):
                    item_path = item.get("path") if isinstance(item, dict) else str(item)
                    if str(item_path).lower() == raw.lower():
                        if isinstance(item, dict):
                            item["enabled"] = enabled
                        return
            if payload.get("add"):
                config.setdefault("customRoots", []).append(
                    {"path": os.path.expanduser(raw), "label": "自定义", "enabled": enabled})
                return
            raise SkmError(f"位置不存在：{raw}")


def run_ui(lib: Library, host: str = "127.0.0.1", port: int = 8765,
           open_browser: bool = True, verbose: bool = False) -> int:
    """起本地服务。端口被占用就自动往后找，最多试 20 个。"""
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + 21):
        try:
            server = SkmServer((host, candidate), SkmHandler)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise SkmError(f"{host} 上 {port}~{port + 20} 端口都占用了：{last_error}")

    server.lib = lib          # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    address = f"http://{host}:{server.server_address[1]}"
    if server.server_address[1] != port:
        warn(f"端口 {port} 被占用，改用 {server.server_address[1]}")

    header("网页界面已启动", paint(f"共 {len(lib.catalog['skills'])} 个 Skill", C.GREY))
    print()
    ok(f"浏览器访问：{paint(address, C.CYAN, C.BOLD)}")
    info(f"技能库：{lib.dir}")
    info("关掉这个命令行窗口或按 Ctrl+C 停止服务。")
    print()

    if open_browser:
        try:
            import webbrowser
            from threading import Timer
            Timer(0.5, lambda: webbrowser.open(address)).start()
        except Exception:
            info(paint("没能自动打开浏览器，请手动访问上面的地址", C.GREY))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        info("收到 Ctrl+C，正在关闭…")
    finally:
        server.shutdown()
        server.server_close()
    ok("服务已停止。")
    return 0


def cmd_ui(args, lib: Library) -> int:
    return run_ui(lib, port=args.port, open_browser=not args.no_browser,
                  verbose=args.verbose)


UI_HTML_PATH = BASE_DIR / "ui.html"


def load_ui_html() -> str:
    """读取前端页面。

    plan 第六节写的是「一个 Python 单文件程序 + 一个网页界面」，
    所以前端单独放 ui.html（好维护、好改样式），不内嵌进 .py。
    找不到文件时给一段能看懂的提示，而不是直接崩。
    """
    if not UI_HTML_PATH.is_file():
        return ("<meta charset='utf-8'><body style='font:14px/1.8 system-ui;padding:40px'>"
                "<h2>找不到前端文件 ui.html</h2>"
                f"<p>应该在的位置：<code>{UI_HTML_PATH}</code></p>"
                "<p>命令行功能不受影响，继续用 <code>python skm.py list</code> 就行。</p>")
    return read_text_smart(UI_HTML_PATH)


def cmd_export_html(args, lib: Library) -> int:
    """skm export --html <输出.html>：单文件离线页面，双击就能看，可以发给同事。"""
    out = Path(os.path.expanduser(args.html))
    state = api_state(lib)
    details = {skill["id"]: api_skill_detail(lib, skill["id"]) for skill in state["skills"]}

    # 关键：payload 里含 SKILL.md 正文，万一出现 </script> 会提前闭合 script 标签，
    # 把后面的内容当 HTML 执行。转义 < 之后 JSON 依然合法，JS 读出来也还原成原文。
    payload = json.dumps({"state": state, "details": details},
                         ensure_ascii=False).replace("<", "\\u003c")

    template = load_ui_html()
    marker = "/*__SKM_BOOTSTRAP__*/null"
    if marker not in template:
        raise SkmError(f"{UI_HTML_PATH.name} 里找不到 {marker} 占位符，无法导出离线页面")
    html = template.replace(marker, payload)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    ok(f"已导出单文件页面：{out}")
    info(f"{human_size(out.stat().st_size)}，双击即可打开，不需要装本工具。")
    info(f"含 {len(state['skills'])} 个 Skill 的完整介绍与 SKILL.md 正文。")
    return 0


# ══════════════════════════════════════════════════════════════════════
#  命令行入口
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  分发到项目（plan 第四节）
#
#  三条铁律：
#  1. 只复制/链接，不改技能库里的任何文件（Skill 本体永远是库里的那份）
#  2. 项目里已有同名不同内容的，**绝不静默覆盖**，一定要用户拍板
#  3. 每次分发写一份记录到 data\_projects\<项目名>\records.json，撤销靠它
# ══════════════════════════════════════════════════════════════════════

PROJECT_SUBDIR = Path(".opencode") / "skills"
OVERWRITE_CHOICES = ["备份后覆盖", "直接覆盖", "跳过"]


def project_entry(lib: Library, name: str) -> dict:
    projects = lib.projects.get("projects", {})
    if name not in projects:
        known = "、".join(sorted(projects)) or "（一个都没登记）"
        raise SkmError(f"没有登记过项目「{name}」。已登记：{known}\n"
                       f"    先跑：skm project add {name} <项目文件夹>")
    return projects[name]


def project_dir(lib: Library, name: str) -> Path:
    """Skill 在项目里的落点：<项目>\\.opencode\\skills\\"""
    return Path(project_entry(lib, name)["path"]) / PROJECT_SUBDIR


def records_file(lib: Library, name: str) -> Path:
    return lib.records_dir / name / "records.json"


def load_records(lib: Library, name: str) -> dict:
    data = read_json(records_file(lib, name),
                     {"version": SCHEMA_VERSION, "project": name, "records": {}})
    data.setdefault("records", {})
    data["project"] = name
    data["projectPath"] = project_entry(lib, name)["path"]
    return data


def save_records(lib: Library, name: str, data: dict) -> None:
    data["updatedAt"] = now_iso()
    write_json(records_file(lib, name), data)


def skill_depends(lib: Library, skill_id: str) -> list[str]:
    record = lib.catalog["skills"].get(skill_id) or {}
    return [str(x).strip() for x in record.get("dependsOn") or [] if str(x).strip()]


def skill_requires(lib: Library, skill_id: str) -> list[str]:
    """Skill 声明自己需要的外部命令/文件（如 python、openpyxl）。"""
    record = lib.catalog["skills"].get(skill_id) or {}
    return [str(x).strip() for x in record.get("requires") or [] if str(x).strip()]


def missing_commands(commands: list[str]) -> list[str]:
    out = []
    for cmd in commands:
        if shutil.which(cmd) is None and not Path(cmd).exists():
            out.append(cmd)
    return out


def expand_dependencies(lib: Library, skill_ids: list[str]) -> tuple[list[str], dict[str, str]]:
    """把「顺带依赖」也带进来（广度优先）。返回 (顺序列表, 被谁带进来的)。"""
    order: list[str] = []
    via: dict[str, str] = {}
    queue = list(skill_ids)
    while queue:
        skill_id = queue.pop(0)
        if skill_id in order:
            continue
        lib.get(skill_id)          # 不在库里就直接报错，别静默跳过
        order.append(skill_id)
        for dep in skill_depends(lib, skill_id):
            if dep not in order:
                via.setdefault(dep, skill_id)
                if dep not in queue:
                    queue.append(dep)
    return order, via


def installed_hash(folder: Path) -> str | None:
    path = folder / SKILL_MD
    if not path.is_file():
        return None
    try:
        return content_hash(read_text_smart(path))
    except OSError:
        return None


def plan_send(lib: Library, project: str, skill_ids: list[str],
              mode: str = "copy") -> list[dict]:
    """分发前的预演：不写任何东西，只说清楚每个 Skill 会怎么处理。

    网页界面拿它渲染确认清单，命令行也用它打印计划——两边看到的必须一致。
    """
    root = project_dir(lib, project)          # 先校验项目：项目没登记比技能名写错更常见
    order, via = expand_dependencies(lib, skill_ids)
    plan = []
    for skill_id in order:
        record = lib.get(skill_id)
        target = root / skill_id
        digest = record.get("source", {}).get("contentHash")
        on_disk = installed_hash(target)
        if not target.exists():
            action = "add"
        elif on_disk == digest:
            action = "same"
        elif mode == "link":
            action = "relink"
        else:
            action = "overwrite"
        plan.append({
            "id": skill_id,
            "name": record.get("name") or skill_id,
            "category": record.get("category") or UNCATEGORIZED,
            "action": action,
            "target": str(target),
            "hash": digest,
            "installedHash": on_disk,
            "via": via.get(skill_id),
            "missing": missing_commands(skill_requires(lib, skill_id)),
        })
    return plan


def mark_used(lib: Library, skill_id: str, project: str, on: bool = True) -> None:
    """catalog.json 与 skill.json 两边同步更新 usedBy（测试盯着这两个字段一致）。"""
    record = lib.get(skill_id)
    used = [x for x in record.get("usedBy") or [] if x != project]
    if on:
        used.append(project)
    record["usedBy"] = used
    write_json(lib.skill_dir(skill_id) / META_FILE, record)


def make_link(src: Path, dst: Path) -> str:
    """软链接；Windows 普通用户没权限就退回目录联接（mklink /J）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    if dst.exists():
        raise SkmError(f"{dst} 已存在")
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SkmError("建链接失败：" + (proc.stderr or proc.stdout or "").strip()
                       + "\n    可以改用 --mode copy（普通权限就能用）")
    return "junction"


def _remove_path(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if getattr(path, "is_junction", lambda: False)():
        path.rmdir()          # 目录联接删的是链接本身，不能把源目录删了
        return
    shutil.rmtree(path)


def backup_existing(lib: Library, project: str, skill_id: str, target: Path) -> str | None:
    """覆盖前把项目里那份挪到 data\\_projects\\<项目>\\backup\\ 下。

    两个细节都是踩过的坑：
    - 目录名带秒级时间戳，但一秒钟内可能备份两次（自动化测试跑得很快），撞名要自动加后缀；
    - 用 shutil.move 而不是 os.replace——项目在 D 盘、库在 C 盘时 replace 会报 EXDEV。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = lib.records_dir / project / "backup"
    dest = base / f"{skill_id}-{stamp}"
    extra = 1
    while dest.exists():
        extra += 1
        dest = base / f"{skill_id}-{stamp}-{extra}"
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        return str(dest)
    except (OSError, shutil.Error) as exc:
        raise SkmError(f"备份旧版本失败：{exc}") from exc


def send_skills(lib: Library, project: str, skill_ids: list[str], mode: str = "copy",
                overwrite: str = "backup", assume_yes: bool = False) -> dict:
    """真的分发。overwrite: backup（默认，先备份）/ replace（直接覆盖）/ skip（跳过旧的）。"""
    root = project_dir(lib, project)
    root.mkdir(parents=True, exist_ok=True)
    plan = plan_send(lib, project, skill_ids, mode)
    data = load_records(lib, project)
    records = data["records"]
    result = {"project": project, "root": str(root), "mode": mode, "plan": plan,
              "added": [], "updated": [], "skipped": [], "failed": [],
              "warnings": [], "relinked": []}

    for item in plan:
        skill_id = item["id"]
        if item["missing"]:
            result["warnings"].append(
                f"{skill_id} 需要 {'、'.join(item['missing'])}，当前环境没有（不影响分发）")
        if item["action"] == "same":
            result["skipped"].append({"id": skill_id, "why": "项目里已经是同一份"})
            records.setdefault(skill_id, {})
            records[skill_id].update({"mode": records[skill_id].get("mode", mode),
                                      "hash": item["hash"], "checkedAt": today()})
            continue

        target = Path(item["target"])
        backup = None
        entry = records.setdefault(skill_id, {})
        try:
            if target.exists() or target.is_symlink():
                if assume_yes:
                    choice = _auto_choice(overwrite)
                elif not is_tty():
                    choice = OVERWRITE_CHOICES[0]     # 管道里没 --overwrite：按默认备份后覆盖
                else:
                    choice = ask_choice(
                        f"  {skill_id} 项目里已有{'不同版本' if item['action'] == 'overwrite' else '链接'}：",
                        OVERWRITE_CHOICES, 1)
                if choice == OVERWRITE_CHOICES[2]:
                    result["skipped"].append({"id": skill_id, "why": "项目里已有，按你的选择跳过"})
                    continue
                # 先备份再删：反过来的话备份的就是「已经不存在的东西」
                if choice == OVERWRITE_CHOICES[0]:
                    backup = backup_existing(lib, project, skill_id, target)
                _remove_path(target)

            if mode == "link":
                how = make_link(lib.skill_dir(skill_id), target)
                (result["relinked"] if item["action"] == "relink"
                 else result["added"]).append(skill_id)
                entry["mode"] = how
            else:
                copy_skill_tree(lib.skill_dir(skill_id), target)
                (result["updated"] if item["action"] in ("overwrite", "relink")
                 else result["added"]).append(skill_id)
                entry["mode"] = "copy"
            entry.update({
                "hash": item["hash"], "target": str(target),
                "sentAt": today(), "viaDependency": item["via"] or None,
                "backup": backup,
            })
            mark_used(lib, skill_id, project, True)
        except SkmError as exc:
            result["failed"].append({"id": skill_id, "why": str(exc)})
        except OSError as exc:
            result["failed"].append({"id": skill_id, "why": f"复制失败：{exc}"})

    save_records(lib, project, data)
    lib.save_all()
    return result


def _auto_choice(overwrite: str) -> str:
    """非交互时按 --overwrite 决定：backup=备份后覆盖 / replace=直接覆盖 / skip=跳过。"""
    return {"backup": OVERWRITE_CHOICES[0],
            "replace": OVERWRITE_CHOICES[1],
            "skip": OVERWRITE_CHOICES[2]}.get(overwrite, OVERWRITE_CHOICES[0])


def remove_from_project(lib: Library, project: str, skill_ids: list[str]) -> dict:
    root = project_dir(lib, project)
    data = load_records(lib, project)
    records = data["records"]
    result = {"project": project, "removed": [], "skipped": [], "failed": []}
    for skill_id in skill_ids:
        target = root / skill_id
        if not (target.exists() or target.is_symlink()):
            result["skipped"].append({"id": skill_id, "why": "项目里本来就没有"})
            continue
        try:
            _remove_path(target)
            records.pop(skill_id, None)
            mark_used(lib, skill_id, project, False)
            result["removed"].append(skill_id)
        except OSError as exc:
            result["failed"].append({"id": skill_id, "why": str(exc)})
    save_records(lib, project, data)
    lib.save_all()
    return result


def project_status(lib: Library, project: str) -> dict:
    root = project_dir(lib, project)
    data = load_records(lib, project)
    records = data["records"]
    rows = []
    for skill_id, rec in records.items():
        target = Path(rec.get("target") or (root / skill_id))
        on_disk = installed_hash(target)
        lib_rec = lib.catalog["skills"].get(skill_id)
        gone = not (target.exists() or target.is_symlink()) or (
            on_disk is None and not target.is_symlink())
        if lib_rec is None:
            state = "orphan"          # 项目里有，库里已经没了
        elif gone:
            state = "missing"         # 记录里有，实际文件没了（或 SKILL.md 被删了）
        elif on_disk == lib_rec.get("source", {}).get("contentHash"):
            state = "ok"
        else:
            state = "stale"
        rows.append({"id": skill_id, "state": state,
                     "libHash": (lib_rec or {}).get("source", {}).get("contentHash"),
                     "diskHash": on_disk,
                     "libUpdated": (lib_rec or {}).get("updatedAt"),
                     "mode": rec.get("mode"), "sentAt": rec.get("sentAt"),
                     "viaDependency": rec.get("viaDependency"),
                     "target": str(target)})
    # 项目里有、但记录里没有的（用户自己手工放进去的）
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name in records:
                continue
            if (folder / SKILL_MD).is_file():
                rows.append({"id": folder.name, "state": "manual",
                             "libHash": (lib.catalog["skills"].get(folder.name) or {})
                             .get("source", {}).get("contentHash"),
                             "diskHash": installed_hash(folder),
                             "mode": "手工放入", "target": str(folder)})
    return {"project": project, "path": project_entry(lib, name=project)["path"],
            "root": str(root), "rows": rows,
            "counts": {state: sum(1 for r in rows if r["state"] == state)
                       for state in ("ok", "stale", "missing", "orphan", "manual")}}


# ───────────────────────────── 命令行 ─────────────────────────────

def cmd_project(args, lib: Library) -> int:
    action = args.action or "list"
    projects = lib.projects.setdefault("projects", {})
    if action == "list":
        header("已登记的项目", paint(f"共 {len(projects)} 个", C.GREY))
        if not projects:
            print()
            warn("还没有登记任何项目。")
            info(paint("  skm project add myapp D:\\code\\myapp", C.CYAN))
            return 0
        print()
        for name, entry in projects.items():
            path = Path(entry.get("path", ""))
            mark = paint("在", C.GREEN) if path.is_dir() else paint("路径不存在", C.RED)
            root = path / PROJECT_SUBDIR
            count = len([p for p in root.iterdir() if p.is_dir()]) if root.is_dir() else 0
            print(f"  {paint(pad(name, 14), C.BOLD)} {mark} {trunc(str(path), max(20, term_width() - 34))}"
                  f"   {paint(f'已装 {count} 个', C.GREY)}")
        print()
        return 0

    if action == "add":
        if not args.name or not args.path:
            raise SkmError("用法：skm project add <项目名> <项目文件夹路径>")
        path = Path(os.path.expanduser(args.path))
        if not path.is_dir():
            raise SkmError(f"文件夹不存在：{path}")
        if args.name in projects:
            if not args.force:
                raise SkmError(f"项目「{args.name}」已登记（{projects[args.name]['path']}）。"
                               f"要改路径就加 --force")
        projects[args.name] = {"path": str(path.resolve()),
                               "addedAt": projects.get(args.name, {}).get("addedAt") or today()}
        lib.save_all()
        print()
        ok(f"已登记项目 {paint(args.name, C.BOLD)} → {path.resolve()}")
        info(f"    分发落点：{path.resolve() / PROJECT_SUBDIR}")
        return 0

    if action == "remove":
        entry = project_entry(lib, args.name)
        del projects[args.name]
        for record in lib.catalog["skills"].values():
            if args.name in record.get("usedBy", []):
                record["usedBy"] = [x for x in record["usedBy"] if x != args.name]
                write_json(lib.skill_dir(record["id"]) / META_FILE, record)
        lib.save_all()
        print()
        ok(f"已从清单移除项目 {paint(args.name, C.BOLD)}（{entry['path']}）")
        info(paint(f"    项目里的文件没动，记录还在 {records_file(lib, args.name)}", C.GREY))
        return 0

    raise SkmError(f"看不懂的项目操作：{action}")


def _resolve_send_ids(lib: Library, args) -> list[str]:
    ids: list[str] = []
    for token in _split_tokens(getattr(args, "id_list", None) or []) + _split_tokens(args.items or []):
        sid = lib.resolve_selector(token)
        if sid not in ids:
            ids.append(sid)
    if getattr(args, "category", None):
        category = lib.resolve_category(args.category)
        for record in lib.catalog["skills"].values():
            one = record.get("category") or UNCATEGORIZED
            if one == category or one.startswith(category + "/"):
                if record["id"] not in ids:
                    ids.append(record["id"])
    if getattr(args, "all", False):
        for sid in lib.catalog["skills"]:
            if sid not in ids:
                ids.append(sid)
    if not ids:
        raise SkmError("没指定要分发哪些 Skill。用 --ids、--category 或 --all")
    return ids


ACTION_LABEL = {"add": "新增", "same": "已一致", "overwrite": "覆盖旧版",
                "relink": "重建链接"}


def cmd_send(args, lib: Library) -> int:
    project = args.project
    project_entry(lib, project)                     # 先校验项目登记过
    ids = _resolve_send_ids(lib, args)
    mode = getattr(args, "mode", "copy") or "copy"
    if mode not in ("copy", "link"):
        raise SkmError("--mode 只能是 copy 或 link")

    plan = plan_send(lib, project, ids, mode)
    header(f"准备分发到 {project}",
           paint(f"{project_entry(lib, project)['path']}（{mode}）", C.GREY))
    print()
    width = max([dw(item["id"]) for item in plan] + [10])
    for item in plan:
        note = ""
        if item["via"]:
            note = paint(f"  （因依赖 {item['via']} 被带进来）", C.GREY)
        elif item["missing"]:
            note = paint(f"  ⚠ 缺 {'、'.join(item['missing'])}", C.YELLOW)
        print(f"  {paint(ACTION_LABEL[item['action']], C.BOLD, C.GREEN if item['action'] == 'add' else C.YELLOW)}"
              f" {pad(trunc(item['id'], width), width + 1)}{note}")
    print()

    result = send_skills(lib, project, ids, mode,
                         overwrite=getattr(args, "overwrite", "backup") or "backup",
                         assume_yes=bool(getattr(args, "yes", False)))
    for line in result["warnings"]:
        warn(line)
    rule()
    print(f"完成：新增 {paint(str(len(result['added'])), C.BOLD)}"
          + (f"，覆盖 {len(result['updated'])}" if result["updated"] else "")
          + (f"，重建链接 {len(result['relinked'])}" if result["relinked"] else "")
          + f"，跳过 {len(result['skipped'])}"
          + (f"，失败 {paint(str(len(result['failed'])), C.RED)}" if result["failed"] else ""))
    for item in result["skipped"]:
        info(paint(f"  跳过 {item['id']}：{item['why']}", C.GREY))
    for item in result["failed"]:
        fail(f"{item['id']}：{item['why']}")
    print()
    info(paint(f"记录：{records_file(lib, project)}", C.GREY))
    info(paint(f"撤销：skm remove {project} --ids <id>", C.GREY))
    return 1 if result["failed"] else 0


def cmd_status(args, lib: Library) -> int:
    info_data = project_status(lib, args.project)
    header(f"{args.project} 里的 Skill", paint(info_data["path"], C.GREY))
    print()
    if not info_data["rows"]:
        warn("这个项目里还没有分发过 Skill。")
        info(paint(f"  skm send --project {args.project} --all", C.CYAN))
        return 0
    width = max([dw(row["id"]) for row in info_data["rows"]] + [10])
    marks = {"ok": paint("一致", C.GREEN), "stale": paint("过期", C.YELLOW),
             "missing": paint("文件没了", C.RED), "orphan": paint("库里已删除", C.RED),
             "manual": paint("手工放入", C.GREY)}
    for row in info_data["rows"]:
        note = paint("  （随依赖带入）", C.GREY) if row.get("viaDependency") else ""
        print(f"  {pad(trunc(row['id'], width), width + 1)}{marks.get(row['state'], '?')}"
              f"   {paint(str(row.get('mode') or ''), C.GREY)}"
              f"   {paint(str(row.get('sentAt') or '-'), C.GREY)}{note}")
    print()
    counts = info_data["counts"]
    info(f"共 {len(info_data['rows'])} 个：一致 {counts['ok']}，过期 {counts['stale']}，"
         f"文件没了 {counts['missing']}，库里已删 {counts['orphan']}，手工放入 {counts['manual']}")
    if counts["stale"] or counts["missing"]:
        info(paint(f"  对齐：skm sync {args.project}", C.CYAN))
    return 0


def cmd_sync(args, lib: Library) -> int:
    info_data = project_status(lib, args.project)
    todo = [row["id"] for row in info_data["rows"]
            if row["state"] in ("stale", "missing") and row["id"] in lib.catalog["skills"]]
    orphans = [row["id"] for row in info_data["rows"]
               if row["state"] == "orphan"]
    header(f"对齐 {args.project}", paint(f"待补 {len(todo)} 个", C.GREY))
    print()
    if not todo and not orphans:
        ok("项目和技能库已经一致，不用动。")
        return 0
    for skill_id in todo:
        warn(f"{skill_id}：{'项目里文件没了' if info_data['counts']['missing'] else '与库里不一致'}，重新分发")
    for skill_id in orphans:
        warn(f"{skill_id}：项目里有，但技能库里已经没有了（要清掉请跑 skm remove {args.project} --ids {skill_id}）")
    print()
    if not args.yes:
        if is_tty() and ask("继续重新分发？", "y") not in ("y", "yes", "是"):
            print(paint("已取消。", C.GREY))
            return 0
    if not todo:
        return 0
    result = send_skills(lib, args.project, todo, mode="copy", overwrite="backup",
                         assume_yes=True)
    rule()
    ok(f"已重新分发 {paint(str(len(result['added']) + len(result['updated'])), C.BOLD)} 个")
    for item in result["failed"]:
        fail(f"{item['id']}：{item['why']}")
    return 1 if result["failed"] else 0


def cmd_remove(args, lib: Library) -> int:
    project = getattr(args, "project_opt", None) or args.project_pos
    if not project:
        raise SkmError("用法：skm remove <项目名> --ids <技能id…>")
    project_entry(lib, project)
    ids = _resolve_send_ids(lib, args)
    result = remove_from_project(lib, project, ids)
    header(f"从 {project} 移除", paint(f"{len(ids)} 个", C.GREY))
    print()
    for skill_id in result["removed"]:
        ok(f"已移除 {skill_id}（技能库里的还在）")
    for item in result["skipped"]:
        info(paint(f"  跳过 {item['id']}：{item['why']}", C.GREY))
    for item in result["failed"]:
        fail(f"{item['id']}：{item['why']}")
    return 1 if result["failed"] else 0


def cmd_dep(args, lib: Library) -> int:
    """声明依赖：Skill 之间（dependsOn）和外部命令（requires）。"""
    if not args.items:
        raise SkmError("用法：skm dep <技能id> [依赖的技能id…] / --cmd python,openpyxl")
    skill_id = lib.resolve_selector(args.items[0])
    record = lib.get(skill_id)
    deps = [x for x in record.get("dependsOn") or []]
    if args.clear:
        deps = []
    for token in args.items[1:] + _split_tokens(args.ids or []):
        dep = lib.resolve_selector(token)
        if dep == skill_id:
            raise SkmError("不能依赖自己")
        if dep not in deps:
            deps.append(dep)
    record["dependsOn"] = deps

    needs = [x for x in record.get("requires") or []]
    if args.clear_needs:
        needs = []
    needs += [x for x in _split_tokens(args.cmd or []) if x not in needs]
    record["requires"] = needs
    write_json(lib.skill_dir(skill_id) / META_FILE, record)
    lib.save_all()
    print()
    ok(f"{paint(skill_id, C.BOLD)} 的依赖：{('、'.join(deps) or '（无）')}")
    info(f"    需要的环境：{'、'.join(needs) or '（无）'}")
    if needs:
        gone = missing_commands(needs)
        info(paint(f"    当前环境缺：{'、'.join(gone) if gone else '不缺'}",
                   C.YELLOW if gone else C.GREY))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  备份 / 恢复 / 体检（plan 第五节）
#
#  备份 = 把整个 data\ 打包成一个 zip（含清单），换台机器 import 就能用。
#  体检 = 只报告不改数据；--fix 才动，而且每一条修完都会再校验一遍。
# ══════════════════════════════════════════════════════════════════════

BACKUP_MANIFEST = "skm-backup.json"
BACKUP_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


def export_zip(lib: Library, out: Path) -> dict:
    """把整个技能库打成 zip。

    刻意**连 _trash 一起打**：回收站里的东西在用户心里可能比某些在册的还重要，
    真删之前先备份一次比较稳妥（体积也就几百 KB）。
    跳过 __pycache__ / *.pyc / *.tmp 这类垃圾。
    """
    out = Path(out)
    if out.is_dir():
        out = out / f"skills-{today()}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    skill_count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(lib.dir.rglob("*")):
            rel = path.relative_to(lib.dir)
            if any(part in BACKUP_SKIP_DIRS for part in rel.parts):
                continue
            if path.suffix in (".pyc", ".tmp") or path.name.endswith(".tmp"):
                continue
            if path.is_dir():
                continue
            zf.write(path, rel.as_posix())
            if rel.parts and rel.parts[0] == "skills" and len(rel.parts) > 1:
                skill_count += 1
        manifest = {
            "app": APP_NAME, "version": VERSION, "schema": SCHEMA_VERSION,
            "createdAt": now_iso(), "dataDir": str(lib.dir),
            "skills": len(lib.catalog["skills"]),
            "projects": list(lib.projects.get("projects", {})),
        }
        zf.writestr(BACKUP_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"path": str(out), "bytes": out.stat().st_size,
            "skills": len(lib.catalog["skills"]), "files": skill_count,
            "manifest": manifest}


def _zip_read(zf: zipfile.ZipFile, name: str):
    try:
        return zf.read(name)
    except KeyError:
        return None


def import_zip(lib: Library, zip_path: Path, with_categories: bool = True,
               with_projects: bool = False, with_config: bool = False,
               assume_yes: bool = False) -> dict:
    """从 zip 导入技能。

    - 内容哈希一样 → 跳过（不制造重复）
    - id 撞了 → 自动加 -2 / -3 后缀
    - 分类树默认**合并**（只补没有的节点，绝不删你已有的分类）
    - 项目与扫描位置是**这台机器特有的**，默认不导入，要导得显式说
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise SkmError(f"文件不存在：{zip_path}")
    result = {"source": str(zip_path), "added": [], "skipped": [],
              "failed": [], "renamed": [], "categories": 0, "projects": 0, "roots": 0}
    with zipfile.ZipFile(zip_path) as zf:
        raw = _zip_read(zf, "catalog.json")
        if raw is None:
            raise SkmError("这个 zip 里没有 catalog.json，不是技能管家导出的备份")
        try:
            catalog = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkmError(f"备份里的 catalog.json 读不出来：{exc}") from exc

        # 判重**看文件实际内容**，不信 skill.json 里记的哈希：
        # 有人拿记事本手工改过 SKILL.md 而没更新哈希时，按记录判会误判成「已有」，
        # 结果备份里那份原版就被无声跳过了——这正是最不该发生的事。
        known_hash = set()
        for sid in lib.catalog["skills"]:
            actual = installed_hash(lib.skill_dir(sid))
            known_hash.add(actual or (lib.catalog["skills"][sid].get("source") or {})
                           .get("contentHash"))
        known_hash.discard(None)
        taken = set(lib.catalog["skills"])

        for skill_id, record in (catalog.get("skills") or {}).items():
            digest = (record.get("source") or {}).get("contentHash")
            if digest and digest in known_hash:
                result["skipped"].append({"id": skill_id, "why": "库里已有同样内容的"})
                continue
            folder = f"skills/{skill_id}/SKILL.md"
            if _zip_read(zf, folder) is None:
                result["failed"].append({"id": skill_id, "why": "zip 里没有 SKILL.md"})
                continue
            new_id = skill_id
            if new_id in taken:
                new_id = unique_id(skill_id, taken)
                result["renamed"].append({"from": skill_id, "to": new_id})
            target_dir = lib.skill_dir(new_id)
            try:
                for name in zf.namelist():
                    if not name.startswith(f"skills/{skill_id}/"):
                        continue
                    rel = name[len(f"skills/{skill_id}/"):]
                    if not rel or ".." in rel.split("/"):
                        continue
                    dest = target_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
            except (OSError, zipfile.BadZipFile) as exc:
                result["failed"].append({"id": skill_id, "why": f"解压失败：{exc}"})
                shutil.rmtree(target_dir, ignore_errors=True)
                continue
            new_record = dict(record)
            new_record["id"] = new_id
            new_record["usedBy"] = []      # 项目是本机的，导入的 Skill 一律先算「未使用」
            if not with_projects:
                new_record["source"] = dict(new_record.get("source") or {},
                                            type=str((new_record.get("source") or {}).get("type") or "import"))
            write_json(target_dir / META_FILE, new_record)
            lib.catalog["skills"][new_id] = new_record
            taken.add(new_id)
            if digest:
                known_hash.add(digest)
            result["added"].append(new_id)

        if with_categories:
            raw_cat = _zip_read(zf, "categories.json")
            if raw_cat:
                try:
                    incoming = json.loads(raw_cat.decode("utf-8-sig"))
                    result["categories"] = merge_categories(lib, incoming)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        if with_projects:
            raw_proj = _zip_read(zf, "projects.json")
            if raw_proj:
                try:
                    incoming = json.loads(raw_proj.decode("utf-8-sig"))
                    for name, entry in (incoming.get("projects") or {}).items():
                        folder = Path(str(entry.get("path", "")))
                        if not folder.is_dir():
                            continue        # 这台机器上没有这个路径，导了也是死的
                        lib.projects.setdefault("projects", {}).setdefault(name, entry)
                        result["projects"] += 1
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        if with_config:
            raw_cfg = _zip_read(zf, "config.json")
            if raw_cfg:
                try:
                    incoming = json.loads(raw_cfg.decode("utf-8-sig"))
                    have = {r.get("path") for r in lib.config.get("customRoots", [])}
                    for entry in incoming.get("customRoots") or []:
                        if entry.get("path") not in have:
                            lib.config.setdefault("customRoots", []).append(entry)
                            result["roots"] += 1
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass

    # 导入的分类可能在这台机器的树里不存在，统一校验一遍，别写幽灵分类
    valid = set(lib.category_paths())
    for record in lib.catalog["skills"].values():
        if (record.get("category") or UNCATEGORIZED) not in valid:
            record["category"] = UNCATEGORIZED
    lib.save_all()
    return result


def merge_categories(lib: Library, incoming: dict) -> int:
    """把备份里的分类节点补进当前分类树：只加没有的，不动已有的（更不删）。"""
    added = 0
    by_name = {cat.get("name"): cat for cat in lib.categories.setdefault("categories", [])}
    for cat in incoming.get("categories") or []:
        name = str(cat.get("name") or "").strip()
        if not name:
            continue
        if name not in by_name:
            lib.categories["categories"].append(
                {"name": name, "desc": cat.get("desc", ""), "children": []})
            by_name[name] = lib.categories["categories"][-1]
            added += 1
        target = by_name[name]
        have = {str(k.get("name")) for k in target.get("children") or []}
        for child in cat.get("children") or []:
            cname = str(child.get("name") or "").strip()
            if cname and cname not in have:
                target.setdefault("children", []).append(
                    {"name": cname, "desc": child.get("desc", "")})
                have.add(cname)
                added += 1
    return added


def doctor(lib: Library, fix: bool = False) -> dict:
    """体检：清单与磁盘对不上、幽灵分类、哈希漂移、项目失效…

    只报告不改数据；fix=True 才动手，且每条都记下修了什么。
    """
    issues: list[dict] = []

    def add(level: str, code: str, detail: str, fixable: bool = False,
            skill_id: str = "", action: str = "") -> None:
        issues.append({"level": level, "code": code, "detail": detail,
                       "fixable": fixable, "id": skill_id, "fixed": False,
                       "action": action})

    valid_cats = set(lib.category_paths())

    for skill_id, record in list(lib.catalog["skills"].items()):
        folder = lib.skill_dir(skill_id)
        if not folder.is_dir():
            add("错误", "catalog-无文件夹",
                f"清单里有「{skill_id}」，但 {folder} 不见了", True, skill_id,
                "从清单里摘掉")
            continue
        md = folder / SKILL_MD
        if not md.is_file():
            add("错误", "catalog-缺 SKILL.md",
                f"{skill_id} 文件夹里没有 {SKILL_MD}", False, skill_id)
            continue
        meta_path = folder / META_FILE
        meta = None
        if not meta_path.is_file():
            add("警告", "缺 skill.json",
                f"{skill_id} 没有 {META_FILE}（分类/标签会丢）", True, skill_id,
                "按清单记录重建")
        else:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                add("错误", "skill.json 读不出来", f"{skill_id}：{exc}", True, skill_id,
                    "按清单记录重建")
        if meta is not None:
            diff = [key for key in ("category", "tags", "starred", "usedBy")
                    if meta.get(key) != record.get(key)]
            if diff:
                add("警告", "两侧不一致",
                    f"{skill_id}：catalog 与 skill.json 的 {'、'.join(diff)} 不一样", True,
                    skill_id, "以 catalog 为准重写 skill.json")
        category = record.get("category") or UNCATEGORIZED
        if category not in valid_cats:
            add("警告", "幽灵分类",
                f"{skill_id} 的分类「{category}」在分类树里不存在", True, skill_id,
                "清回「未分类」")
        try:
            actual = content_hash(read_text_smart(md))
        except OSError as exc:
            add("错误", "SKILL.md 读不出来", f"{skill_id}：{exc}", False, skill_id)
            continue
        if actual != (record.get("source") or {}).get("contentHash"):
            add("提示", "内容哈希漂移",
                f"{skill_id} 的 SKILL.md 被手工改过（库里记的 "
                f"{(record.get('source') or {}).get('contentHash')}，实际 {actual}）", True,
                skill_id, "重算哈希")
            if fix:
                record.setdefault("source", {})["contentHash"] = actual

    for folder in sorted(lib.skills_dir.glob("*")) if lib.skills_dir.is_dir() else []:
        if folder.is_dir() and folder.name not in lib.catalog["skills"]:
            add("提示", "磁盘上有、清单里没有",
                f"{folder.name} 文件夹存在但清单里没有条目", False, folder.name)

    for name, entry in (lib.projects.get("projects") or {}).items():
        if not Path(entry.get("path", "")).is_dir():
            add("警告", "项目路径不存在",
                f"项目「{name}」的路径 {entry.get('path')} 不在了", False, name)
        rec_file = records_file(lib, name)
        if not rec_file.is_file():
            continue
        try:
            records = json.loads(rec_file.read_text(encoding="utf-8")).get("records", {})
        except (OSError, json.JSONDecodeError):
            add("错误", "分发记录读不出来", f"项目「{name}」的 records.json 坏了", False, name)
            continue
        dead = []
        for skill_id, rec in records.items():
            if skill_id not in lib.catalog["skills"]:
                dead.append(skill_id)
                add("警告", "记录里的技能库里已没有",
                    f"项目「{name}」记着 {skill_id}，但技能库里已经删了", False, name, skill_id)
                continue
            target = Path(rec.get("target") or "")
            if not (target.exists() or target.is_symlink()):
                dead.append(skill_id)
                add("警告", "项目里的文件没了",
                    f"项目「{name}」里 {skill_id} 的文件不见了（{target}）", False, name, skill_id)
        if dead and fix:
            data = json.loads(rec_file.read_text(encoding="utf-8"))
            for skill_id in dead:
                data["records"].pop(skill_id, None)
            save_records(lib, name, data)
            for item in issues:
                if item.get("action") in dead and item["code"] in (
                        "记录里的技能库里已没有", "项目里的文件没了"):
                    item["fixed"] = True

    fixed = 0
    if fix:
        for item in issues:
            if not item["fixable"] or item["fixed"]:
                continue
            skill_id = item["id"]
            if item["code"] == "catalog-无文件夹":
                lib.catalog["skills"].pop(skill_id, None)
            elif item["code"] == "缺 skill.json" or item["code"] == "skill.json 读不出来":
                write_json(lib.skill_dir(skill_id) / META_FILE,
                           lib.catalog["skills"][skill_id])
            elif item["code"] == "两侧不一致":
                write_json(lib.skill_dir(skill_id) / META_FILE,
                           lib.catalog["skills"][skill_id])
            elif item["code"] == "幽灵分类":
                lib.catalog["skills"][skill_id]["category"] = UNCATEGORIZED
                write_json(lib.skill_dir(skill_id) / META_FILE,
                           lib.catalog["skills"][skill_id])
            elif item["code"] == "内容哈希漂移":
                write_json(lib.skill_dir(skill_id) / META_FILE,
                           lib.catalog["skills"][skill_id])
            else:
                continue
            item["fixed"] = True
            fixed += 1
        lib.save_all()

    errors = sum(1 for i in issues if i["level"] == "错误")
    return {"issues": issues, "fixed": fixed, "errors": errors,
            "warnings": sum(1 for i in issues if i["level"] == "警告"),
            "hints": sum(1 for i in issues if i["level"] == "提示"),
            "ok": not errors and not any(i["level"] == "警告" for i in issues)}


def check_library(lib: Library, unused_days: int = 30) -> dict:
    """全库扫一遍：依赖缺失、依赖指向不存在、长期未用、空分类、未分类堆积。"""
    now = datetime.now()
    needs, dangling, unused = [], [], []
    for skill_id, record in lib.catalog["skills"].items():
        gone = missing_commands(skill_requires(lib, skill_id))
        if gone:
            needs.append({"id": skill_id, "missing": gone})
        for dep in skill_depends(lib, skill_id):
            if dep not in lib.catalog["skills"]:
                dangling.append({"id": skill_id, "dependsOn": dep})
        used = record.get("usedBy") or []
        added = str(record.get("source", {}).get("addedAt") or "")
        if not used and added:
            try:
                days = (now - datetime.strptime(added, "%Y-%m-%d")).days
            except ValueError:
                days = 0
            if days >= unused_days:
                unused.append({"id": skill_id, "days": days})

    empty = []
    for path in sorted(lib.category_paths()):
        if not any((rec.get("category") or UNCATEGORIZED) == path
                   for rec in lib.catalog["skills"].values()):
            empty.append(path)
    uncategorized = sum(1 for rec in lib.catalog["skills"].values()
                        if (rec.get("category") or UNCATEGORIZED) == UNCATEGORIZED)
    return {"needs": needs, "dangling": dangling, "unused": unused,
            "emptyCategories": empty, "uncategorized": uncategorized,
            "skills": len(lib.catalog["skills"]), "unusedDays": unused_days}


def trash_list(lib: Library) -> list[dict]:
    """回收站里还剩什么（软删除的东西连同当时的记录）。"""
    out = []
    if not lib.trash_dir.is_dir():
        return out
    for meta in sorted(lib.trash_dir.glob("*.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        folder = lib.trash_dir / str(data.get("folder") or "")
        out.append({"id": data.get("id"), "name": (data.get("record") or {}).get("name"),
                    "trashedAt": data.get("trashedAt"), "folder": folder.name,
                    "exists": folder.is_dir(),
                    "category": (data.get("record") or {}).get("category"),
                    "size": sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
                    if folder.is_dir() else 0})
    out.sort(key=lambda x: str(x.get("trashedAt") or ""), reverse=True)
    return out


def restore_skill(lib: Library, token: str) -> dict:
    """从回收站找回。token 可以是 id，也可以是回收站里的文件夹名。"""
    entries = trash_list(lib)
    hit = next((e for e in entries if e["folder"] == token), None) or \
        next((e for e in entries if e["id"] == token), None)
    if hit is None:
        if not entries:
            raise SkmError("回收站是空的，没有可恢复的东西")
        known = "、".join(e["id"] for e in entries[:8])
        raise SkmError(f"回收站里没有「{token}」。里面有：{known}")
    if hit["id"] in lib.catalog["skills"]:
        raise SkmError(f"「{hit['id']}」已经在库里了，先删掉才能恢复")
    src = lib.trash_dir / hit["folder"]
    if not src.is_dir():
        raise SkmError(f"回收站里的文件夹不见了：{src}")
    new_id = unique_id(hit["id"], set(lib.catalog["skills"]))
    shutil.move(str(src), str(lib.skill_dir(new_id)))
    meta = lib.skill_dir(new_id) / META_FILE
    record = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
    record["id"] = new_id
    record["usedBy"] = []
    record["restoredAt"] = now_iso()
    valid = set(lib.category_paths())
    if (record.get("category") or UNCATEGORIZED) not in valid:
        record["category"] = UNCATEGORIZED
    write_json(meta, record)
    lib.catalog["skills"][new_id] = record
    (lib.trash_dir / f"{hit['id']}.json").unlink(missing_ok=True)
    lib.save_all()
    return {"id": new_id, "from": hit["folder"]}


# ───────────────────────────── 命令行 ─────────────────────────────

def cmd_export(args, lib: Library) -> int:
    """`skm export --html` 和 `--zip` 合到一个命令里，两个都不给就报错。"""
    if not args.html and not args.zip:
        raise SkmError("要指定导出什么：--html <路径>（离线页面）或 --zip <路径>（技能库备份）")
    if args.zip:
        return cmd_export_zip(args, lib)
    args.html_out = args.html
    return cmd_export_html(args, lib)


def cmd_export_zip(args, lib: Library) -> int:
    out = Path(os.path.expanduser(args.zip))
    info_data = export_zip(lib, out)
    print()
    ok(f"已打包：{info_data['path']}")
    info(f"    {human_size(info_data['bytes'])}，"
         f"{info_data['skills']} 个 Skill（含清单、分类树、分发记录、回收站）")
    info(paint(f"    换台机器：skm import \"{info_data['path']}\"", C.CYAN))
    return 0


def cmd_import(args, lib: Library) -> int:
    before = len(lib.catalog["skills"])
    result = import_zip(lib, Path(os.path.expanduser(args.zip)),
                        with_categories=not args.no_categories,
                        with_projects=args.with_projects,
                        with_config=args.with_config,
                        assume_yes=args.yes)
    print()
    ok(f"导入完成：新增 {paint(str(len(result['added'])), C.BOLD)} 个，"
       f"跳过 {len(result['skipped'])} 个"
       + (f"，分类节点补了 {result['categories']} 个" if result["categories"] else "")
       + (f"，项目 {result['projects']} 个" if result["projects"] else "")
       + (f"，扫描位置 {result['roots']} 个" if result["roots"] else ""))
    for item in result["renamed"]:
        info(paint(f"  重名改叫：{item['from']} → {item['to']}", C.YELLOW))
    for item in result["skipped"]:
        info(paint(f"  跳过 {item['id']}：{item['why']}", C.GREY))
    for item in result["failed"]:
        fail(f"{item['id']}：{item['why']}")
    info(f"库里现在共 {len(lib.catalog['skills'])} 个（原来 {before} 个）")
    if not args.with_projects:
        info(paint("    项目和扫描位置是本机的，没导入（要导加 --with-projects / --with-config）",
                   C.GREY))
    return 1 if result["failed"] else 0


LEVEL_STYLE = {"错误": ("XX", C.RED), "警告": ("!!", C.YELLOW), "提示": ("..", C.GREY)}


def cmd_doctor(args, lib: Library) -> int:
    report = doctor(lib, fix=args.fix)
    header("技能库体检" + ("（--fix 已自动修）" if args.fix else ""),
           paint(f"{report['errors']} 个错误 / {report['warnings']} 个警告", C.GREY))
    print()
    if not report["issues"]:
        ok("一切正常：清单、文件、分类树、分发记录都对得上。")
        return 0
    for item in report["issues"]:
        mark, colour = LEVEL_STYLE.get(item["level"], ("..", C.GREY))
        tail = paint(f"  → {item['action']}", C.CYAN) if item["fixable"] and not item["fixed"] else ""
        done = paint("（已修）", C.GREEN) if item["fixed"] else ""
        print(f"  {paint(mark, colour, C.BOLD)} [{item['level']}] "
              f"{paint(item['code'], colour)}{item['detail']}{tail}{done}")
    print()
    fixable = sum(1 for i in report["issues"] if i["fixable"] and not i["fixed"])
    if fixable:
        info(paint(f"其中 {fixable} 条可以自动修：skm doctor --fix", C.CYAN))
    if report["errors"]:
        fail(f"{report['errors']} 个错误需要人工处理")
        return 1
    return 0


def cmd_check(args, lib: Library) -> int:
    report = check_library(lib, unused_days=args.days)
    header("全库检查", paint(f"{report['skills']} 个 Skill", C.GREY))
    print()

    print(paint("依赖缺失（不影响分发，只是提醒）", C.BOLD))
    if report["needs"]:
        for item in report["needs"]:
            warn(f"{item['id']} 需要 {'、'.join(item['missing'])}，当前环境没有")
    else:
        info(paint("  没有 Skill 声明的外部依赖是缺的", C.GREY))
    print()

    print(paint("依赖指向了不存在的 Skill（这个是真问题）", C.BOLD))
    if report["dangling"]:
        for item in report["dangling"]:
            fail(f"{item['id']} 依赖 {item['dependsOn']}，但库里没有它")
    else:
        info(paint("  依赖关系都能对上", C.GREY))
    print()

    print(paint(f"长期未用（超过 {report['unusedDays']} 天、没被任何项目用）", C.BOLD))
    if report["unused"]:
        for item in sorted(report["unused"], key=lambda x: -x["days"]):
            info(f"  {pad(item['id'], 34)} {item['days']} 天没动过")
    else:
        info(paint("  没有长期未用的 Skill", C.GREY))
    print()

    print(paint("空分类（有节点但一个 Skill 都没有）", C.BOLD))
    if report["emptyCategories"]:
        for path in report["emptyCategories"]:
            info(paint(f"  {path}", C.GREY))
    else:
        info(paint("  没有空分类", C.GREY))
    print()
    if report["uncategorized"]:
        warn(f"「未分类」里堆了 {report['uncategorized']} 个，用界面拖一拖或 skm cat 归类")
    return 1 if report["dangling"] else 0


def cmd_restore(args, lib: Library) -> int:
    if args.list or not args.id:
        items = trash_list(lib)
        header("回收站", paint(f"{len(items)} 个", C.GREY))
        print()
        if not items:
            info("回收站是空的。")
            return 0
        for item in items:
            mark = "" if item["exists"] else paint("（文件夹不见了）", C.RED)
            print(f"  {pad(item['id'], 30)} {pad(str(item['category'] or ''), 18)}"
                  f"{pad(str(item['trashedAt'] or ''), 22)}{human_size(item['size']):>10}{mark}")
        print()
        info(paint("  skm restore <id>  找回", C.CYAN))
        return 0
    result = restore_skill(lib, args.id)
    print()
    ok(f"已从回收站找回：{result['id']}"
       + (f"（原名 {result['from']}）" if result["id"] != args.id else ""))
    return 0


# ══════════════════════════════════════════════════════════════════════
#  AI 自动分类
#
#  设计上守死三条：
#  1. **只用标准库**（urllib 调 OpenAI 兼容接口），不引第三方依赖（plan 第六节）
#  2. **模型不直接改数据**——它只给建议，落盘必须经过人点确认（和 send 一个思路）
#  3. API Key 不外泄：只在本机 config.json / 环境变量里，绝不下发给前端
# ══════════════════════════════════════════════════════════════════════

AI_DEFAULTS = {
    "enabled": True,
    "baseUrl": "",                 # 例如 https://api.opencode.ai/v1 或 https://api.openai.com/v1
    "model": "",                   # 例如 gpt-4o-mini / qwen-plus
    "apiKey": "",                  # 也可以不写这里，改用环境变量 SKM_AI_API_KEY
    "temperature": 0,
    "timeout": 60,
    "bodyChars": 1200,             # 每个 Skill 最多送多少字符正文进提示词
    "createCategory": True,        # 允许模型提出新的一级/二级分类
    "batchSize": 1,                # 一次问几个（1 = 一个 Skill 一次请求，最好排查）
}

CLASSIFY_SYSTEM = (
    "你是技能库（Claude Skill / opencode Skill）的分类助手。"
    "用户会把现有分类树和若干 Skill 的信息给你，你为每个 Skill 指定最贴切的一个分类。\n"
    "规则：\n"
    "1. 优先从给出的分类树里选一个完整路径（如「信息对抗/侦察测绘」）；\n"
    "2. 现有分类都不合适时才允许提出新分类，路径用「/」分隔，一级或一级/二级都可以；\n"
    "3. 只输出 JSON，不要任何解释文字、不要 markdown 代码块；\n"
    "4. 格式：[{\"id\":\"<技能id>\",\"category\":\"<分类路径>\",\"newCategory\":false,\"reason\":\"<10字内理由>\"}]")


def ai_config(lib: Library) -> dict:
    """取 AI 配置并补齐默认值（老 config.json 没有这一节也能用）。"""
    cfg = dict(AI_DEFAULTS)
    cfg.update(lib.config.get("ai") or {})
    if not cfg.get("apiKey"):
        cfg["apiKey"] = os.environ.get("SKM_AI_API_KEY", "")   # 环境变量优先，免得一机多份密钥
    return cfg


def ai_public_config(cfg: dict, from_env: bool = False) -> dict:
    """给前端的配置：**去掉 key**，只告诉用户「配了没有」和后四位。

    这条很关键——/api/config 是 GET 接口，任何能访问本机这个服务的东西都能拿到响应体，
    API Key 绝不能出现在里面。
    """
    key = str(cfg.get("apiKey") or "")
    return {"enabled": bool(cfg.get("enabled", True)),
            "baseUrl": cfg.get("baseUrl", ""), "model": cfg.get("model", ""),
            "hasKey": bool(key),
            "keyHint": ("…" + key[-4:]) if len(key) >= 4 else ("已设置" if key else ""),
            "fromEnv": bool(from_env),
            "temperature": cfg.get("temperature", 0),
            "bodyChars": cfg.get("bodyChars", 1200),
            "createCategory": bool(cfg.get("createCategory", True)),
            "ready": bool(cfg.get("baseUrl") and cfg.get("model") and key)}


def ai_chat(cfg: dict, messages: list[dict], timeout: int | None = None) -> str:
    """调一次 OpenAI 兼容的 /chat/completions，返回文本内容。

    故意不做重试：批量分类时"失败就报出来"比"悄悄重试三次然后超时"更好排查。
    """
    base = str(cfg.get("baseUrl") or "").strip().rstrip("/")
    model = str(cfg.get("model") or "").strip()
    key = str(cfg.get("apiKey") or "").strip()
    if not base:
        raise SkmError("还没配 AI 接口地址（设置里填「接口地址 / 模型 / API Key」）")
    if not model:
        raise SkmError("还没配 AI 模型名")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {"model": model, "messages": messages,
               "temperature": float(cfg.get("temperature") or 0)}
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout or int(cfg.get("timeout") or 60)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise SkmError(f"AI 接口返回 {exc.code}"
                       + (f"：{detail}" if detail else "")
                       + ("（多半是 API Key 或模型名不对）" if exc.code in (401, 403) else "")) from exc
    except urllib.error.URLError as exc:
        raise SkmError(f"连不上 AI 接口：{url}\n    {exc.reason}") from exc
    except TimeoutError as exc:
        raise SkmError(f"AI 接口超时（{timeout or cfg.get('timeout')} 秒）") from exc
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SkmError(f"AI 接口返回的不是 JSON：{raw[:120]}") from exc
    return _ai_text(data)


def _ai_text(data: dict) -> str:
    """从各种兼容返回里挖出文本（OpenAI / 部分代理的字段名不统一）。"""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):       # 有些接口把 content 拆成片段
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        if content:
            return str(content)
        if choices[0].get("text"):
            return str(choices[0]["text"])
    for key in ("output_text", "text", "content"):
        if isinstance(data.get(key), str):
            return data[key]
    raise SkmError("AI 返回里找不到内容字段（这个接口可能不是 OpenAI 兼容格式）")


def parse_ai_json(text: str) -> list:
    """从模型输出里抠出 JSON。

    模型很爱在 JSON 外面包 ```json，也爱在后面加一句解释，所以：
    先整体试，失败就在第一个 [ 与最后一个 ] 之间截，失败再在 { } 之间截。
    """
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for candidate in (text, _slice_between(text, "[", "]"), _slice_between(text, "{", "}")):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("skills", "result", "results", "items"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
            return [parsed]
    raise SkmError("模型没给出可解析的 JSON（原文前 120 字：" + text[:120] + "）")


def _slice_between(text: str, left: str, right: str) -> str:
    start = text.find(left)
    end = text.rfind(right)
    return text[start:end + 1] if start >= 0 and end > start else ""


def category_tree_text(lib: Library) -> str:
    lines = []
    for cat in lib.categories.get("categories", []):
        lines.append(f"- {cat.get('name', '')}"
                     + (f"：{cat['desc']}" if cat.get("desc") else ""))
        for child in cat.get("children") or []:
            lines.append(f"  - {cat.get('name', '')}/{child.get('name', '')}"
                         + (f"：{child['desc']}" if child.get("desc") else ""))
    return "\n".join(lines) or "（现在一个分类都没有）"


def skill_digest(lib: Library, record: dict, body_chars: int) -> str:
    """给模型看的 Skill 摘要。正文超长就截断——发全文既慢又贵。"""
    parts = [f"id: {record['id']}", f"名称: {record.get('name') or '-'}",
             f"一句话介绍: {record.get('summary') or '-'}"]
    tags = record.get("tags") or []
    if tags:
        parts.append("标签: " + "、".join(tags))
    path = lib.skill_md_path(record["id"])
    body = ""
    if path.is_file():
        try:
            _, body = split_frontmatter(read_text_smart(path))
            body = markdown_to_text(body, limit=body_chars, keep_code=False)
        except OSError:
            body = ""
    if body:
        parts.append("正文节选: " + " ".join(body.split())[:body_chars])
    return "\n".join(parts)


def ai_require_config(cfg: dict) -> None:
    """没配齐就早点报错。

    不然 propose_categories 会给每个 Skill 都报一遍同样的「没配地址」，
    30 个未分类就是 30 行一样的废话——真正该说的是「你没配」。
    """
    missing = []
    if not str(cfg.get("baseUrl") or "").strip():
        missing.append("接口地址")
    if not str(cfg.get("model") or "").strip():
        missing.append("模型名")
    if not str(cfg.get("apiKey") or "").strip():
        missing.append("API Key（或环境变量 SKM_AI_API_KEY）")
    if missing:
        raise SkmError("AI 配置还缺：" + "、".join(missing)
                       + "\n    网页界面右上角「⚙ 设置」里填，或直接改 data\\config.json 的 ai 一节"
                       + "\n    配完可以先跑 skm ai --test 试连通")


def propose_categories(lib: Library, skill_ids: list[str], cfg: dict) -> list[dict]:
    """问模型这批 Skill 该放哪。**只读**，一个都不改。

    返回每个 Skill 一条：{id, category, newCategory, reason, error}
    出错的 Skill 也会占一条（error 说明原因），这样界面能显示"3 个成功 2 个失败"。
    """
    ai_require_config(cfg)
    tree = category_tree_text(lib)
    valid = set(lib.category_paths())
    body_chars = int(cfg.get("bodyChars") or 1200)
    allow_new = bool(cfg.get("createCategory", True))
    batch = max(1, int(cfg.get("batchSize") or 1))
    results: list[dict] = []

    for start in range(0, len(skill_ids), batch):
        chunk = skill_ids[start:start + batch]
        blocks = [skill_digest(lib, lib.get(sid), body_chars) for sid in chunk]
        prompt = (f"现有分类树：\n{tree}\n\n"
                  + ("允许提出新的分类（一级或一级/二级）。\n\n" if allow_new
                     else "只能从上面的分类树里选，不要提出新分类。\n\n")
                  + "待分类 Skill：\n\n" + "\n\n".join(blocks)
                  + f"\n\n请为上面 {len(chunk)} 个 Skill 各给一个分类，按要求输出 JSON 数组。")
        try:
            raw = ai_chat(cfg, [{"role": "system", "content": CLASSIFY_SYSTEM},
                                {"role": "user", "content": prompt}])
            parsed = parse_ai_json(raw)
        except SkmError as exc:
            for sid in chunk:
                results.append({"id": sid, "category": "", "newCategory": False,
                                "reason": "", "error": str(exc)})
            continue

        by_id = {}
        for item in parsed:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
        for sid in chunk:
            item = by_id.get(sid)
            if item is None:
                results.append({"id": sid, "category": "", "newCategory": False,
                                "reason": "", "error": "模型没提到这个 Skill"})
                continue
            category = str(item.get("category") or "").strip().strip("/")
            is_new = bool(item.get("newCategory")) or category not in valid
            if not category:
                results.append({"id": sid, "category": "", "newCategory": False,
                                "reason": "", "error": "模型没给分类"})
                continue
            if not allow_new and category not in valid:
                results.append({"id": sid, "category": "", "newCategory": False,
                                "reason": "", "error": f"「{category}」不在分类树里，且未允许新建"})
                continue
            results.append({"id": sid, "category": category, "newCategory": is_new,
                            "reason": str(item.get("reason") or "")[:40], "error": ""})
    return results


def apply_categories(lib: Library, decisions: list[dict],
                     create_missing: bool = True) -> dict:
    """按确认过的建议落盘。走的还是 set_category，catalog 与 skill.json 自动同步。

    `create_missing` 单独拎出来当参数，而不是靠 `is_tty()` 猜：
    「要不要顺手建新分类」是**调用方**的策略决定——
    界面上用户已经在确认框里看过要建什么了（直接 True），
    命令行非交互又没显式同意时必须 False（宁可失败也不要偷偷建分类）。
    """
    applied, failed, created = [], [], []
    pending = [d for d in decisions if str(d.get("category") or "").strip()]
    for item in pending:
        skill_id = str(item.get("id") or "").strip()
        category = str(item.get("category") or "").strip()
        try:
            lib.get(skill_id)
        except SkmError as exc:
            failed.append({"id": skill_id, "why": str(exc)})
            continue
        try:
            if lib.resolve_or_none(category) is None:
                if not create_missing:
                    raise SkmError(f"「{category}」在分类树里不存在（不自动新建）")
                lib.ensure_category_exists(category)
                created.append(category)
            lib.set_category([skill_id], category, create=False)
            applied.append({"id": skill_id, "category": category})
        except SkmError as exc:
            failed.append({"id": skill_id, "why": str(exc)})
    lib.save_all()
    return {"applied": applied, "failed": failed, "created": sorted(set(created))}


def _write_ai_report(lib: Library, report: dict) -> str:
    path = lib.dir / "_ai" / f"classify-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, report)
    return str(path)


def uncategorized_ids(lib: Library, only: str = "") -> list[str]:
    out = []
    for skill_id, record in lib.catalog["skills"].items():
        category = record.get("category") or UNCATEGORIZED
        if category == UNCATEGORIZED or (only and category == only):
            out.append(skill_id)
    return sorted(out)


# ───────────────────────────── 命令行 ─────────────────────────────

def cmd_ai(args, lib: Library) -> int:
    cfg = ai_config(lib)
    header("AI 配置")
    print()
    info(f"  接口地址  {cfg.get('baseUrl') or paint('（没配）', C.RED)}")
    info(f"  模型      {cfg.get('model') or paint('（没配）', C.RED)}")
    key = str(cfg.get("apiKey") or "")
    info(f"  API Key   {(paint(key[:4] + '…' + key[-4:], C.GREEN) if key else paint('（没配）', C.RED))}"
         + paint("（来自环境变量 SKM_AI_API_KEY）" if key and not _ai_key_in_config(lib) else "", C.GREY))
    info(f"  新建分类  {'允许' if cfg.get('createCategory', True) else '不允许'}"
         f"　正文节选 {cfg.get('bodyChars')} 字　批量 {cfg.get('batchSize')}")
    print()
    try:
        ai_require_config(cfg)
    except SkmError as exc:
        warn(str(exc))
        return 1
    if args.test:
        info("正在测试连接…")
        try:
            answer = ai_chat(cfg, [{"role": "user", "content": "只回复两个字：可用"}])
        except SkmError as exc:
            fail(str(exc))
            return 1
        ok(f"连接正常，模型回了：{answer.strip()[:60]}")
    return 0


def _ai_key_in_config(lib: Library) -> bool:
    return bool((lib.config.get("ai") or {}).get("apiKey"))


def cmd_classify(args, lib: Library) -> int:
    cfg = ai_config(lib)
    ids = _split_tokens(args.ids or [])
    if args.all:
        ids = sorted(lib.catalog["skills"])
    elif not ids:
        ids = uncategorized_ids(lib, only="")
    else:
        ids = [lib.resolve_selector(x) for x in ids]
    if args.only_category:
        ids = [sid for sid in ids
               if (lib.catalog["skills"][sid].get("category") or UNCATEGORIZED) == args.only_category]
    if args.limit:
        ids = ids[:args.limit]
    if not ids:
        ok("没有待分类的 Skill（未分类的是空的）。")
        return 0

    header("AI 自动分类", paint(f"{len(ids)} 个待分类", C.GREY))
    print()
    try:
        ai_require_config(cfg)
    except SkmError as exc:
        warn(str(exc))
        return 1
    info(f"模型 {cfg.get('model') or '（没配）'}，允许新建分类："
         + ("是" if cfg.get("createCategory", True) else "否"))
    print()
    proposals = propose_categories(lib, ids, cfg)
    ok_count = sum(1 for p in proposals if not p["error"])
    for item in proposals:
        mark = paint("？", C.YELLOW) if item["error"] else paint("✓", C.GREEN)
        target = item["error"] or (item["category"] + (paint("（新）", C.CYAN) if item["newCategory"] else ""))
        note = paint(f"  {item['reason']}", C.GREY) if item.get("reason") else ""
        print(f"  {mark} {pad(item['id'], 32)} {target}{note}")
    print()
    if ok_count == 0:
        fail("一个都没成功，先用 skm ai --test 看看接口通不通。")
        return 1

    report = {"at": now_iso(), "model": cfg.get("model"), "items": proposals}
    _write_ai_report(lib, report)
    if args.dry_run:
        info(paint("（--dry-run：只看不改）", C.GREY))
        return 0
    usable = [p for p in proposals if not p["error"]]
    new_paths = sorted({p["category"] for p in usable if p["newCategory"]})
    create_missing = True
    if not args.yes and is_tty():
        rule()
        answer = ask(f"把上面 {len(usable)} 个按建议归类？", "y")
        if answer.strip().lower() not in ("y", "yes", "是"):
            print(paint("已取消，什么都没改。", C.GREY))
            return 0
        if new_paths:
            print()
            info("其中这些分类树里还没有，需要新建：")
            for path in new_paths:
                info(paint(f"  {path}", C.CYAN))
            again = ask("要创建吗？", "y")
            create_missing = again.strip().lower() in ("y", "yes", "是")
    elif new_paths and not args.yes:
        create_missing = False     # 非交互又没显式同意 → 不偷偷建分类
    result = apply_categories(lib, usable, create_missing=create_missing)
    rule()
    ok(f"已归类 {paint(str(len(result['applied'])), C.BOLD)} 个"
       + (f"，新建分类 {len(result['created'])} 个" if result["created"] else "")
       + (f"，失败 {len(result['failed'])} 个" if result["failed"] else ""))
    for item in result["created"]:
        info(paint(f"  新建分类：{item}", C.CYAN))
    for item in result["failed"]:
        fail(f"{item['id']}：{item['why']}")
    info(paint(f"记录：{lib.dir / '_ai'}", C.GREY))
    return 1 if result["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_TITLE} —— 收集 → 分类 → 分发（当前阶段：收集）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用示例：
  skm scan                          扫描本机已有 Skill，勾选收录
  skm scan --path D:\\share\\skills  额外扫一个位置
  skm scan --add-root D:\\share      把这个位置记进配置，以后每次都扫
  skm scan --yes                    不交互，全部收录
  skm add <文件或文件夹>            手动收录
  skm add --text                    粘贴文本新建一个 Skill
  skm list                          看已收录的清单
  skm list --category PPT/排版      按分类过滤
  skm show <id>                     看某个 Skill 的完整内容
  skm cat --list                    看分类树和各分类数量
  skm cat <id> PPT/排版             改分类
  skm tag <id> ppt 排版 母版        改标签
""")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--data-dir", help="技能库数据目录（默认：程序目录下的 data）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="初始化/修复技能库目录与清单文件")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="扫描本机已有 Skill")
    p.add_argument("path", nargs="*", help="额外扫描的位置")
    p.add_argument("--path", dest="paths", action="append", metavar="路径",
                   help="额外扫描的位置（可重复）")
    p.add_argument("--add-root", action="append", metavar="路径",
                   help="把这个位置记进配置，以后每次都扫（可重复）")
    p.add_argument("-y", "--yes", action="store_true", help="不交互，全部收录；冲突一律跳过")
    p.add_argument("--select", metavar="编号",
                   help="直接指定要收录的编号，如 1,3,5-8 或 all（不进入交互提问）")
    p.add_argument("--category", help="收录后直接归到某分类（不存在会报错）")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("add", help="手动收录：指定路径 / 粘贴新建")
    p.add_argument("target", nargs="*", help="SKILL.md 文件或 Skill 文件夹")
    p.add_argument("--text", action="store_true", help="粘贴文本，当场生成一个 Skill")
    p.add_argument("--name", help="配合 --text：技能名称")
    p.add_argument("--summary", help="配合 --text：一句话介绍")
    p.add_argument("--file", help="配合 --text：从文件读内容")
    p.add_argument("--category", help="收录后直接归到某分类")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，冲突时自动处理")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="看已收录的清单")
    p.add_argument("-a", "--all", action="store_true", help="不把收藏项排前面")
    p.add_argument("-c", "--category", help="按分类过滤（支持「PPT」匹配整个大类）")
    p.add_argument("-t", "--tag", help="按标签过滤")
    p.add_argument("-n", "--name", help="按 id / 名称模糊过滤")
    p.add_argument("--starred", action="store_true", help="只看收藏")
    p.add_argument("--unused", action="store_true", help="只看没被任何项目用过的")
    p.add_argument("--source", help="按来源过滤：scan/local/text")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看某个 Skill 的完整内容")
    p.add_argument("id")
    p.add_argument("--lines", type=int, default=40, help="预览行数，0 表示全文")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("cat", help="查看/修改分类",
                       epilog="示例：\n"
                              "  skm cat --list                        看分类树\n"
                              "  skm cat ppt-layout PPT/排版            改单个\n"
                              "  skm cat a,b,c PPT/内容生成             批量改\n"
                              "  skm cat --category 表格 办公与自动化    整类搬家\n"
                              "  skm cat PPT/排版                       只看这个分类下有哪些\n")
    p.add_argument("items", nargs="*", help="技能 id…和目标分类（最后一个参数是分类）")
    p.add_argument("--category", help="源分类：把该分类下全部 Skill 移到目标分类")
    p.add_argument("--ids", dest="id_list", help="逗号分隔的技能 id")
    p.add_argument("--list", action="store_true", help="打印分类树和各分类数量")
    p.add_argument("--create", action="store_true", help="分类不存在时自动创建")
    p.set_defaults(func=cmd_cat)

    p = sub.add_parser("tag", help="查看/修改标签",
                       epilog="示例：\n"
                              "  skm tag ppt-layout              只看标签\n"
                              "  skm tag ppt-layout ppt 排版 母版  追加标签\n"
                              "  skm tag ppt-layout --replace a b  替换标签\n")
    p.add_argument("items", nargs="+", help="第一个是技能 id，后面是标签")
    p.add_argument("--replace", action="store_true", help="替换而不是追加")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("star", help="收藏/取消收藏")
    p.add_argument("id", nargs="+")
    p.add_argument("--off", action="store_true", help="取消收藏")
    p.set_defaults(func=cmd_star)

    p = sub.add_parser("info", help="技能库信息与统计")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("roots", help="查看扫描位置")
    p.set_defaults(func=cmd_roots)

    p = sub.add_parser("ui", help="启动网页界面（分类树 + 卡片墙 + 详情抽屉）",
                       epilog="示例：\n"
                              "  skm ui                 打开 http://127.0.0.1:8765\n"
                              "  skm ui --port 9000     换个端口（被占用时会自动往后找）\n"
                              "  skm ui --no-browser    不自动开浏览器\n")
    p.add_argument("--port", type=int, default=8765, help="起始端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--verbose", action="store_true", help="打印每条 HTTP 请求")
    p.set_defaults(func=cmd_ui)

    # ── 第四步：分发到项目 ──

    p = sub.add_parser("project", help="登记/查看项目（分发的前提）",
                       epilog="示例：\n"
                              "  skm project list\n"
                              "  skm project add myapp D:\\code\\myapp\n"
                              "  skm project remove myapp\n")
    p.add_argument("action", nargs="?", choices=("list", "add", "remove"),
                   default="list", help="list=看清单（默认） add=登记 remove=从清单移除")
    p.add_argument("name", nargs="?", help="项目名")
    p.add_argument("path", nargs="?", help="项目文件夹路径（add 时必填）")
    p.add_argument("--force", action="store_true", help="已存在时覆盖登记信息")
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("send", help="把选中的 Skill 分发到项目",
                       epilog="示例：\n"
                              "  skm send --project myapp --ids ppt-layout,ppt-content\n"
                              "  skm send --project myapp --category 信息对抗\n"
                              "  skm send --project myapp --all --mode link\n")
    p.add_argument("--project", required=True, help="项目名（先 skm project add）")
    p.add_argument("items", nargs="*", help="技能 id…（也可以用 --ids）")
    p.add_argument("--ids", dest="id_list", help="逗号分隔的技能 id")
    p.add_argument("--category", help="分发整个分类（支持一级大类）")
    p.add_argument("--all", action="store_true", help="分发全部 Skill")
    p.add_argument("--mode", choices=("copy", "link"), default="copy",
                   help="copy=复制一份（默认）；link=软链接/目录联接，改库立刻生效")
    p.add_argument("--overwrite", choices=("backup", "replace", "skip"), default="backup",
                   help="项目里已有同名不同内容时怎么办：backup=备份后覆盖（默认）"
                        " replace=直接覆盖 skip=跳过")
    p.add_argument("-y", "--yes", action="store_true", help="不交互，按 --overwrite 执行")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("status", help="看某个项目里现在有哪些 Skill、哪些和库里不一致")
    p.add_argument("project")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="按记录对齐项目：补上缺的、重发过期的")
    p.add_argument("project")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接重发")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("remove", help="从项目里移除 Skill（技能库里的还在）",
                       epilog="示例：\n"
                              "  skm remove myapp --ids ppt-visual\n"
                              "  skm remove --project myapp --category PPT\n")
    p.add_argument("project_pos", nargs="?", metavar="项目名", help="项目名（等价于 --project）")
    p.add_argument("--project", dest="project_opt", help="项目名")
    p.add_argument("items", nargs="*", help="技能 id…")
    p.add_argument("--ids", dest="id_list", help="逗号分隔的技能 id")
    p.add_argument("--category", help="移除整个分类")
    p.add_argument("--all", action="store_true", help="移除全部（库里不动）")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("dep", help="声明依赖：Skill 之间 / 需要的外部命令",
                       epilog="示例：\n"
                              "  skm dep ocr-fallback pdf-form-filler      # ocr 依赖 pdf\n"
                              "  skm dep excel-clean --cmd python,openpyxl  # 需要哪些命令\n"
                              "  skm dep ocr-fallback --clear              # 清空依赖\n")
    p.add_argument("items", nargs="+", help="第一个是技能 id，后面是它依赖的技能 id")
    p.add_argument("--ids", help="逗号分隔的依赖 id")
    p.add_argument("--cmd", help="逗号分隔的外部命令，如 python,openpyxl")
    p.add_argument("--clear", action="store_true", help="清空 Skill 之间的依赖")
    p.add_argument("--clear-needs", action="store_true", help="清空外部命令依赖")
    p.set_defaults(func=cmd_dep)

    # ── 第五步：备份 / 体检 ──

    p = sub.add_parser("export", help="导出：单文件离线 HTML / 整个技能库 zip",
                       epilog="示例：\n"
                              "  skm export --html D:\\tmp\\skills.html\n"
                              "  skm export --zip D:\\bak\\skills-2026-10.zip\n")
    p.add_argument("--html", metavar="输出路径", help="单文件离线页面（双击能看）")
    p.add_argument("--zip", metavar="输出路径", help="整个技能库打包（可再 import 回来）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="从 zip 备份导入技能",
                       epilog="示例：\n"
                              "  skm import D:\\bak\\skills.zip\n"
                              "  skm import D:\\bak\\skills.zip --with-projects\n")
    p.add_argument("zip", help="skm export --zip 导出的那个 zip")
    p.add_argument("--no-categories", action="store_true", help="不合并备份里的分类树")
    p.add_argument("--with-projects", action="store_true",
                   help="连项目登记一起导（路径得在这台机器上存在）")
    p.add_argument("--with-config", action="store_true", help="连自定义扫描位置一起导")
    p.add_argument("-y", "--yes", action="store_true", help="不交互")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("doctor", help="体检：清单/文件/分类/分发记录对不对得上",
                       epilog="示例：\n"
                              "  skm doctor          只报告，不改任何数据\n"
                              "  skm doctor --fix    能自动修的顺手修掉\n")
    p.add_argument("--fix", action="store_true", help="自动修能修的（会写盘）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("check", help="全库扫一遍：依赖缺失、长期未用、空分类")
    p.add_argument("--days", type=int, default=30, help="多少天没被用过算「长期未用」（默认 30）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("restore", help="从回收站找回删掉的 Skill",
                       epilog="示例：\n"
                              "  skm restore            看回收站里有什么\n"
                              "  skm restore ocr-fallback\n")
    p.add_argument("id", nargs="?", help="技能 id（或回收站里的文件夹名）")
    p.add_argument("--list", action="store_true", help="只看回收站清单")
    p.set_defaults(func=cmd_restore)

    # ── AI 自动分类 ──

    p = sub.add_parser("ai", help="查看/测试 AI 接口配置（自动分类用）",
                       epilog="示例：\n"
                              "  skm ai            看当前配置（key 只显示后四位）\n"
                              "  skm ai --test     发一句话测连通性\n")
    p.add_argument("--test", action="store_true", help="发一句话测试接口通不通")
    p.set_defaults(func=cmd_ai)

    p = sub.add_parser("classify", help="用 AI 给「未分类」的 Skill 提分类建议",
                       epilog="示例：\n"
                              "  skm classify --dry-run      只看建议，不改任何数据\n"
                              "  skm classify                确认后落盘\n"
                              "  skm classify --ids a,b      只给指定的几个提建议\n"
                              "  skm classify --all -y       全部（连已归类的也重提）\n")
    p.add_argument("items", nargs="*", help="技能 id…")
    p.add_argument("--ids", help="逗号分隔的技能 id")
    p.add_argument("--all", action="store_true", help="连已经归好类的也一起重新提建议")
    p.add_argument("--only-category", help="只处理某个分类下的（配合 --all）")
    p.add_argument("--limit", type=int, help="最多处理几个（防止一次烧太多 token）")
    p.add_argument("--dry-run", action="store_true", help="只打印建议，不落盘")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接按建议归类")
    p.set_defaults(func=cmd_classify)

    return parser


def main(argv=None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        print()
        info(paint(f"数据目录：{DEFAULT_DATA_DIR}", C.GREY))
        return 0

    try:
        lib = Library(Path(os.path.expanduser(args.data_dir)) if args.data_dir else None).ensure()
        return args.func(args, lib)
    except Abort:
        print(paint("\n已取消。", C.GREY))
        return 130
    except SkmError as exc:
        print()
        fail(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        print(paint("\n已取消。", C.GREY))
        return 130


if __name__ == "__main__":
    sys.exit(main())
