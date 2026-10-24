# -*- coding: utf-8 -*-
"""
Local Model Manager
===================

A small, dependency-free desktop manager for llama.cpp.

Features
--------
* Batch-import many GGUF models into named profiles.
* Router mode: one llama-server, several model aliases. The model is loaded
  on demand from the ``model`` field of each API request. Chat and agent
  clients can therefore share one endpoint and switch automatically.
* Single-model mode for machines that only run one model at a time.
* Portable JSON config: relative paths keep working after moving the folder.
* Optional service shortcuts (SillyTavern, TTS, anything with a command).

Only the Python standard library is used.

Run:  python model_manager.pyw
Test: python model_manager.pyw --selftest
"""

from __future__ import annotations

import configparser
import csv
import ctypes
import io
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
import tkinter as tk

APP_NAME = "Local Model Manager"
APP_VERSION = "2.1.0"
IS_WINDOWS = os.name == "nt"

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LMM_CONFIG", APP_DIR / "config.json")).expanduser()
BASE_DIR = CONFIG_PATH.parent
MANAGER_LOG = BASE_DIR / "manager.log"

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
CREATE_NEW_CONSOLE = 0x00000010 if IS_WINDOWS else 0
GIB = 1024 ** 3
VRAM_RESERVE_GB = 0.95
VRAM_SAFETY_GB = 0.3

# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

THEME_KEYS = ("bg", "card", "field", "fg", "muted", "accent",
              "danger", "success", "border", "log_bg", "log_fg")

BUILTIN_THEMES = {
    "暖夜": {
        "name": "暖夜", "dark": True,
        "bg": "#241b20", "card": "#30252b", "field": "#2b2026",
        "fg": "#f3e9e9", "muted": "#c3b0b5", "accent": "#d9a3af",
        "danger": "#d99292", "success": "#91c9ae", "border": "#554249",
        "log_bg": "#171013", "log_fg": "#d8c8cb",
    },
    "雾灰": {
        "name": "雾灰", "dark": True,
        "bg": "#1f2326", "card": "#2a2f33", "field": "#252a2e",
        "fg": "#e6ebee", "muted": "#a9b5bc", "accent": "#a8c3d4",
        "danger": "#c99b9b", "success": "#9cc7b1", "border": "#3d464d",
        "log_bg": "#141719", "log_fg": "#cfd8dd",
    },
    "青黛": {
        "name": "青黛", "dark": True,
        "bg": "#1d2223", "card": "#262d2e", "field": "#222829",
        "fg": "#e5eceb", "muted": "#a6b6b4", "accent": "#93bfb4",
        "danger": "#c59a9a", "success": "#a6cdbb", "border": "#384443",
        "log_bg": "#121617", "log_fg": "#cdd9d7",
    },
    "米灰": {
        "name": "米灰", "dark": False,
        "bg": "#e9e3da", "card": "#f2ede6", "field": "#f7f3ed",
        "fg": "#3c3732", "muted": "#7a7066", "accent": "#b08968",
        "danger": "#b57e7a", "success": "#7f9d86", "border": "#cfc4b7",
        "log_bg": "#f2ede6", "log_fg": "#3c3732",
    },
}

THEME_DARK = True
BG = BUILTIN_THEMES["暖夜"]["bg"]
CARD = BUILTIN_THEMES["暖夜"]["card"]
FIELD = BUILTIN_THEMES["暖夜"]["field"]
FG = BUILTIN_THEMES["暖夜"]["fg"]
MUTED = BUILTIN_THEMES["暖夜"]["muted"]
ACCENT = BUILTIN_THEMES["暖夜"]["accent"]
GREEN = BUILTIN_THEMES["暖夜"]["success"]
RED = BUILTIN_THEMES["暖夜"]["danger"]
BORDER = BUILTIN_THEMES["暖夜"]["border"]
LOG_BG = BUILTIN_THEMES["暖夜"]["log_bg"]
LOG_FG = BUILTIN_THEMES["暖夜"]["log_fg"]


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(x))) for x in rgb)


def blend(color_a: str, color_b: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    a = _hex_rgb(color_a)
    b = _hex_rgb(color_b)
    return _rgb_hex(tuple(a[i] * (1 - ratio) + b[i] * ratio for i in range(3)))


def _luminance(color: str) -> float:
    try:
        r, g, b = _hex_rgb(color)
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    except Exception:
        return 0.0


def _is_color(value) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", str(value).strip()))


def read_theme_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    elif suffix in (".ini", ".cfg"):
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        section = "theme" if parser.has_section("theme") else parser.default_section
        if parser.has_section(section):
            return dict(parser[section])
    raise ValueError(f"unsupported theme file: {path}")


def normalize_theme(data: dict, base: dict | None = None) -> dict:
    theme = dict(base or BUILTIN_THEMES["暖夜"])
    if not isinstance(data, dict):
        data = {}
    name = str(data.get("name") or theme.get("name") or "Custom").strip()
    theme["name"] = name[:40] or "Custom"
    for key in THEME_KEYS:
        value = data.get(key)
        if _is_color(value):
            theme[key] = str(value).strip()
    dark = data.get("dark")
    if dark is None:
        theme["dark"] = _luminance(theme["bg"]) < 0.5
    else:
        theme["dark"] = str(dark).strip().lower() in (
            "1", "true", "yes", "on", "dark")
    return theme


def load_themes(*bases: Path) -> dict[str, dict]:
    themes = {theme["name"]: dict(theme) for theme in BUILTIN_THEMES.values()}
    seen = set()
    for base in bases:
        folder = Path(base) / "themes"
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            suffix = path.suffix.lower()
            if suffix not in (".json", ".ini", ".cfg"):
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                data = read_theme_file(path)
                parent = themes.get(str(data.get("base") or ""))
                theme = normalize_theme(data, parent)
                themes[theme["name"]] = theme
            except Exception:
                log(f"failed to load theme {path}:\n" + traceback.format_exc())
    return themes


def apply_theme(theme: dict) -> None:
    global BG, CARD, FIELD, FG, MUTED, ACCENT, GREEN, RED, BORDER
    global LOG_BG, LOG_FG, THEME_DARK
    BG = theme["bg"]
    CARD = theme["card"]
    FIELD = theme["field"]
    FG = theme["fg"]
    MUTED = theme["muted"]
    ACCENT = theme["accent"]
    GREEN = theme["success"]
    RED = theme["danger"]
    BORDER = theme["border"]
    LOG_BG = theme["log_bg"]
    LOG_FG = theme["log_fg"]
    THEME_DARK = bool(theme.get("dark", True))

FONT_UI = "Microsoft YaHei UI" if IS_WINDOWS else "DejaVu Sans"
FONT_MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"

CTX_PRESETS = ["4096", "8192", "16384", "24576", "32768", "49152", "57344",
               "65536", "98304", "131072", "163840", "196608", "262144"]

# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

_LOCALES: dict[str, dict] = {"zh": {"name": "中文", "strings": {}}}
_LANG = "zh"


def load_locales(*bases: Path) -> dict[str, dict]:
    """Load built-in Chinese plus every locales/<code>.json.

    A locale file is:
        {"language_name": "English", "strings": {"中文原文": "translation"}}
    Files are merged in order, so a user copy can override a bundled one.
    """
    global _LOCALES
    locales: dict[str, dict] = {"zh": {"name": "中文", "strings": {}}}
    seen = set()
    for base in bases:
        folder = Path(base) / "locales"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            code = path.stem.lower()
            key = (folder.resolve(), code)
            if key in seen:
                continue
            seen.add(key)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                strings = data.get("strings")
                if not isinstance(strings, dict):
                    strings = {}
                locales.setdefault(code, {"name": code, "strings": {}})
                locales[code]["name"] = str(
                    data.get("language_name") or locales[code]["name"] or code)
                locales[code]["strings"].update(
                    {str(k): str(v) for k, v in strings.items()})
            except Exception:
                log(f"failed to load locale {path}:\n" + traceback.format_exc())
    _LOCALES = locales
    return locales


def set_language(code: str) -> str:
    global _LANG
    _LANG = code if code in _LOCALES else "zh"
    return _LANG


def current_language() -> str:
    return _LANG


def language_choices() -> list[tuple[str, str]]:
    return [(info.get("name", code), code) for code, info in _LOCALES.items()]


def T(text: str, **kwargs) -> str:
    """Translate a Chinese source string with optional format arguments."""
    translated = _LOCALES.get(_LANG, {}).get("strings", {}).get(text)
    if translated is None:
        translated = text
    if kwargs:
        try:
            return translated.format(**kwargs)
        except Exception:
            return translated
    return translated


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with MANAGER_LOG.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except Exception:
        pass


def default_config() -> dict:
    return {
        "version": 2,
        "server_path": "",
        "server_builds": [],
        "scan_dirs": [],
        "mode": "router",
        "host": "127.0.0.1",
        "port": 8081,
        "models_max": 1,
        "expose_assigned_only": True,
        "api_key": "",
        "vram_gb": 0,
        "vram_reserve_gb": VRAM_RESERVE_GB,
        "defaults": {
            "ctx": 32768,
            "kvk": "f16",
            "kvv": "f16",
            "ngl": 999,
            "batch": 2048,
            "ubatch": 512,
            "parallel": 1,
            "vision_cpu": True,
            "mtmd_batch": 256,
            "extra": "",
            "mtp": False,
            "mtp_enabled": True,
            "mtp_draft_n": 2,
            "load_mode": "auto",
        },
        "language": "zh",
        "last_profile": "",
        "ui": {"theme": "暖夜", "colors": {}},
        "profiles": {},
        "clients": [
            {"name": "Chat", "profile": ""},
        ],
        "services": [],
        "dismissed_plugins": [],
        "logs_dir": "logs",
        "router_preset": "router-presets.ini",
    }


def deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def autodetect_server(base: Path) -> Path | None:
    env = os.environ.get("LLAMA_SERVER")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    names = ["llama-server.exe", "llama-server"] if IS_WINDOWS else ["llama-server"]
    for name in names:
        candidates += [base / name, base / "bin" / name]
    for pattern in ("llama.cpp*/llama-server.exe", "llama.cpp*/llama-server"):
        candidates += sorted(base.glob(pattern))
        candidates += sorted(base.parent.glob(pattern))
    found = shutil.which("llama-server")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            pass
    return None


def server_build_label(path: Path) -> str:
    folder = path.parent
    try:
        files = [item.name.lower() for item in folder.iterdir() if item.is_file()]
    except Exception:
        files = []
    joined = " ".join(files)
    if ("ggml-cuda" in joined or "cudart" in joined or "cublas" in joined
            or "cuda" in folder.name.lower()):
        if "cudart64_13" in joined or "cublas64_13" in joined:
            backend = "CUDA 13"
        elif "cudart64_12" in joined or "cublas64_12" in joined:
            backend = "CUDA 12"
        else:
            backend = "CUDA"
    elif "ggml-vulkan" in joined or "vulkan" in folder.name.lower():
        backend = "Vulkan"
    elif "ggml-cpu" in joined:
        backend = "CPU"
    else:
        backend = ""
    return f"{backend} · {folder.name}" if backend else folder.name


def detect_server_builds(base: Path, current: Path | None = None,
                         extra: list[str] | None = None) -> list[tuple[str, Path]]:
    paths: dict[str, Path] = {}
    for root in (base, base.parent):
        try:
            candidates = list(root.glob("llama.cpp*/llama-server*"))
            candidates += list(root.glob("llama.cpp*/llama-server.exe"))
            candidates += [root / "llama-server.exe", root / "llama-server"]
        except Exception:
            candidates = []
        for candidate in candidates:
            try:
                if candidate.name.lower() not in ("llama-server.exe", "llama-server"):
                    continue
                if candidate.is_file():
                    paths.setdefault(str(candidate.resolve()).lower(),
                                     candidate.resolve())
            except Exception:
                pass
    for value in extra or []:
        try:
            candidate = base / Path(value) if not Path(value).is_absolute() else Path(value)
            if candidate.is_file():
                paths.setdefault(str(candidate.resolve()).lower(),
                                 candidate.resolve())
        except Exception:
            pass
    if current is not None:
        try:
            if current.is_file():
                paths.setdefault(str(current.resolve()).lower(), current.resolve())
        except Exception:
            pass
    items: list[tuple[str, Path]] = []
    used: set[str] = set()
    for path in sorted(paths.values(), key=lambda p: (p.parent.name.lower(), str(p))):
        label = server_build_label(path)
        if label in used:
            label = f"{label} [{path.parent}]"
        used.add(label)
        items.append((label, path))
    return items


SERVICE_PLUGIN_INFO = {
    "sillytavern": {
        "title": "SillyTavern",
        "desc": "角色卡聊天前端",
        "icon": "ST",
        "options": {"open_after_start": True},
    },
    "genie-tts": {
        "title": "Genie TTS",
        "desc": "本地语音合成，CPU 运行",
        "icon": "TTS",
        "options": {"model_cache": 1, "preload": "", "auto_proxy": True},
    },
    "genie-tts-proxy": {
        "title": "TTS Proxy",
        "desc": "把 TTS 接到酒馆 / 其他客户端",
        "icon": "→",
        "options": {},
    },
    "gpt-sovits": {
        "title": "GPT-SoVITS",
        "desc": "本地语音合成，CUDA 加速",
        "icon": "GSV",
        "options": {"auto_proxy": True},
    },
    "gpt-sovits-proxy": {
        "title": "GSV Proxy",
        "desc": "OpenAI 兼容接口，供酒馆调用",
        "icon": "→",
        "options": {},
    },
    "custom": {
        "title": "自定义服务",
        "desc": "任意启动命令或本地程序",
        "icon": "＋",
        "options": {},
    },
}


AUTO_PROXY_PLUGINS = {
    "genie-tts": "genie-tts-proxy",
    "gpt-sovits": "gpt-sovits-proxy",
}


def service_plugin_id(service: dict) -> str:
    """Return the built-in plugin id for a service record."""
    plugin = str(service.get("plugin") or "").strip()
    if plugin in SERVICE_PLUGIN_INFO and plugin != "custom":
        return plugin
    command = str(service.get("command") or "").lower().replace("\\", "/")
    name = str(service.get("name") or "").lower()
    if ("gpt-sovits" in command or "gpt sovits" in name
            or "gsv" in command or "gsv" in name):
        if "proxy" in command or "proxy" in name:
            return "gpt-sovits-proxy"
        return "gpt-sovits"
    if "start-proxy" in command or "tts proxy" in name or "tts-proxy" in name:
        return "genie-tts-proxy"
    if "sillytavern" in command or "sillytavern" in name or "酒馆" in name:
        return "sillytavern"
    if "genie-tts" in command or "genie tts" in name or "tts" in name:
        return "genie-tts"
    return "custom"


def service_plugin_info(service: dict) -> dict:
    return SERVICE_PLUGIN_INFO[service_plugin_id(service)]


def autodetect_services(base: Path) -> list[dict]:
    services = []
    roots = (base, base.parent, base.parent.parent)
    st = None
    for name in ("Start-Portable.bat", "Start.bat"):
        st = next((root / "SillyTavern" / name for root in roots
                   if (root / "SillyTavern" / name).exists()), None)
        if st:
            break
    if st:
        services.append({
            "name": "SillyTavern", "port": 8000, "command": str(st),
            "url": "http://127.0.0.1:8000", "process": "node", "stop": "",
            "plugin": "sillytavern",
            "options": {"open_after_start": True},
        })
    tts = None
    for root in roots:
        for rel in ("Genie-TTS GUI/server.bat", "tts/server.bat"):
            if (root / rel).exists():
                tts = root / rel
                break
        if tts:
            break
    if tts:
        services.append({
            "name": "Genie TTS", "port": 9880, "command": str(tts),
            "url": "", "process": "python", "stop": "",
            "plugin": "genie-tts",
            "options": {"model_cache": 1, "preload": "", "auto_proxy": True},
        })
    proxy = None
    for root in roots:
        for rel in ("Genie-TTS GUI/start-proxy.bat", "tts/start-proxy.bat"):
            if (root / rel).exists():
                proxy = root / rel
                break
        if proxy:
            break
    if proxy:
        services.append({
            "name": "TTS Proxy", "port": 9881, "command": str(proxy),
            "url": "", "process": "python", "stop": "",
            "plugin": "genie-tts-proxy",
            "options": {},
        })
    gsv_api = gsv_proxy = None
    for root in roots:
        if gsv_api is None:
            direct_api = root / "tts" / "start-gsv-api.bat"
            if direct_api.exists():
                gsv_api = direct_api
            else:
                gsv_api = next(iter(sorted(root.glob("GPT-SoVITS*/start-api-9883.bat"))), None)
        if gsv_proxy is None:
            direct = root / "tts" / "start-gsv-proxy.bat"
            if direct.exists():
                gsv_proxy = direct
            else:
                gsv_proxy = next(iter(sorted(root.glob("GPT-SoVITS*/start-gsv-proxy.bat"))), None)
    if gsv_api:
        services.append({
            "name": "GPT-SoVITS", "port": 9883, "command": str(gsv_api),
            "url": "", "process": "python", "stop": "",
            "plugin": "gpt-sovits",
            "options": {"auto_proxy": True},
        })
    if gsv_proxy:
        services.append({
            "name": "GSV Proxy", "port": 9882, "command": str(gsv_proxy),
            "url": "", "process": "python", "stop": "",
            "plugin": "gpt-sovits-proxy",
            "options": {},
        })
    return services


def service_environment(service: dict) -> dict:
    """Build the environment for a service, including TTS options."""
    env = os.environ.copy()
    plugin = service_plugin_id(service)
    if plugin != "genie-tts":
        return env
    options = service.get("options") or {}
    try:
        cache = int(options.get("model_cache", 1))
    except Exception:
        cache = 1
    env["GENIE_MODEL_CACHE"] = str(max(1, min(8, cache)))
    preload = str(options.get("preload") or "").strip()
    if preload:
        env["GENIE_PRELOAD"] = preload
    else:
        env.pop("GENIE_PRELOAD", None)
    return env


def migrate_v1(raw: dict, base: Path) -> dict:
    """Turn the old single-profile config into the v2 profile format."""
    data = dict(raw)

    if raw.get("llama_server") and not data.get("server_path"):
        data["server_path"] = raw["llama_server"]
    if raw.get("vram_gb") and not data.get("vram_gb"):
        data["vram_gb"] = raw["vram_gb"]

    last = raw.get("last")
    if isinstance(last, dict) and last.get("model") and not raw.get("profiles"):
        alias = last.get("alias") or Path(last["model"]).stem
        name = unique_name(re.sub(r"[\[\]\r\n;#=]", "-", str(alias)).strip(), {})
        data["profiles"] = {
            name: {
                "model": last.get("model", ""),
                "mmproj": last.get("mmproj", ""),
                "ctx": last.get("ctx", 32768),
                "kvk": last.get("kvk", "f16"),
                "kvv": last.get("kvv", "f16"),
                "ngl": last.get("ngl", 999),
                "batch": last.get("batch", 2048),
                "ubatch": last.get("ubatch", 512),
                "parallel": last.get("parallel", 1),
                "vision_cpu": bool(last.get("vision_cpu", True)),
                "mtmd_batch": last.get("mtmd_batch", 256),
                "extra": last.get("extra", ""),
                "mtp": bool(last.get("mtp", False)),
                "mtp_enabled": True,
                "mtp_draft_n": 2,
            }
        }
        roles = dict(data.get("roles") or {})
        roles.setdefault("chat", name)
        data["roles"] = roles
        data.pop("last", None)
    elif "last" in data:
        data.pop("last", None)

    return data


class Config:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.base = self.path.parent
        self.data = default_config()
        self.load()

    def load(self) -> None:
        raw: dict = {}
        if self.path.exists():
            try:
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed
            except Exception:
                log("读取配置失败:\n" + traceback.format_exc())

        migrated = migrate_v1(raw, self.base)
        self.data = deep_merge(default_config(), migrated)
        self._migrate_clients(migrated)
        self._fill_defaults()
        self.save()

    def _migrate_clients(self, raw: dict) -> None:
        data = self.data
        if not isinstance(raw.get("clients"), list):
            roles = raw.get("roles") or {}
            data["clients"] = [
                {"name": "Chat", "profile": roles.get("chat", "")},
                {"name": "Agent", "profile": roles.get("agent", "")},
            ]
        cleaned = []
        for index, client in enumerate(data.get("clients") or []):
            if not isinstance(client, dict):
                continue
            cleaned.append({
                "name": str(client.get("name") or f"Client {index + 1}"),
                "profile": str(client.get("profile") or ""),
            })
        data["clients"] = cleaned or [
            {"name": "Chat", "profile": ""},
            {"name": "Agent", "profile": ""},
        ]
        data.pop("roles", None)

    def _fill_defaults(self) -> None:
        data = self.data
        if not data.get("server_path"):
            server = autodetect_server(self.base)
            data["server_path"] = self.portable(server) if server else ""
        if not data.get("scan_dirs"):
            dirs = []
            for candidate in (self.base, self.base / "models"):
                if candidate.is_dir():
                    dirs.append(self.portable(candidate))
            data["scan_dirs"] = dirs or [self.portable(self.base)]
        services = data.get("services")
        if not isinstance(services, list):
            services = []
            data["services"] = services

        # Recognise old service records and give each one a plugin id.
        for service in services:
            if not isinstance(service, dict):
                continue
            plugin = service_plugin_id(service)
            service["plugin"] = plugin
            options = service.get("options")
            if not isinstance(options, dict):
                options = {}
                service["options"] = options
            for key, value in (SERVICE_PLUGIN_INFO.get(plugin, {})
                               .get("options") or {}).items():
                options.setdefault(key, value)

        dismissed = data.get("dismissed_plugins")
        if not isinstance(dismissed, list):
            dismissed = []
            data["dismissed_plugins"] = dismissed
        existing = {service_plugin_id(service): service
                    for service in services if isinstance(service, dict)}
        for detected in autodetect_services(self.base):
            plugin = detected["plugin"]
            detected["command"] = self.portable(Path(detected["command"]))
            current = existing.get(plugin)
            if current is None:
                if plugin not in dismissed:
                    services.append(detected)
                continue
            # Keep user edits, but repair a command path that no longer exists.
            old_path = self.resolve(current.get("command"))
            new_path = self.resolve(detected.get("command"))
            if not old_path.is_file() and new_path.is_file():
                current["command"] = detected["command"]
            options = current.setdefault("options", {})
            for key, value in (detected.get("options") or {}).items():
                options.setdefault(key, value)
        if not data.get("vram_gb"):
            _, vram = detect_gpu()
            if vram:
                data["vram_gb"] = round(vram, 1)
        for profile in data.get("profiles", {}).values():
            if isinstance(profile, dict):
                profile.setdefault("load_mode", "auto")
                profile.setdefault("mtp_enabled", True)
                profile.setdefault("mtp_draft_n", 2)
        self._normalize_paths()

    def _normalize_paths(self) -> None:
        """Store paths relative to the config folder whenever reasonable."""
        data = self.data
        if data.get("server_path"):
            data["server_path"] = self.portable(self.resolve(data["server_path"]))
        data["server_builds"] = [self.portable(self.resolve(item))
                                 for item in data.get("server_builds", [])
                                 if item]
        data["scan_dirs"] = [self.portable(self.resolve(item))
                             for item in data.get("scan_dirs", [])]
        for profile in data.get("profiles", {}).values():
            for key in ("model", "mmproj"):
                if profile.get(key):
                    profile[key] = self.portable(self.resolve(profile[key]))
        for service in data.get("services", []):
            for key in ("command", "stop"):
                value = service.get(key)
                if value and Path(value).is_absolute():
                    service[key] = self.portable(Path(value))

    def save(self) -> None:
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            temp.replace(self.path)
        except Exception:
            log("保存配置失败:\n" + traceback.format_exc())

    def resolve(self, value) -> Path:
        if value in (None, ""):
            return Path()
        text = os.path.expandvars(os.path.expanduser(str(value)))
        path = Path(text)
        if not path.is_absolute():
            path = self.base / path
        return path.resolve()

    def portable(self, value) -> str:
        if value in (None, ""):
            return ""
        path = Path(value)
        if not path.is_absolute():
            return path.as_posix()
        try:
            path = path.resolve()
            relative = os.path.relpath(str(path), str(self.base))
        except Exception:
            return str(path)
        if sum(1 for part in Path(relative).parts if part == "..") > 2:
            return str(path)
        return Path(relative).as_posix()

    def log_dir(self) -> Path:
        path = self.resolve(self.data.get("logs_dir") or "logs")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def server_log(self) -> Path:
        return self.log_dir() / "llama-server.log"

    def preset_path(self) -> Path:
        return self.resolve(self.data.get("router_preset") or "router-presets.ini")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TB"


def is_port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False


def find_listener_pid(port: int) -> int | None:
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"], stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW, text=True, errors="ignore")
            pattern = re.compile(
                rf"^\s*TCP\s+\S+:{int(port)}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)
            for line in out.splitlines():
                match = pattern.search(line)
                if match:
                    return int(match.group(1))
        else:
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{int(port)}", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL, text=True, timeout=3)
            first = out.strip().splitlines()
            if first:
                return int(first[0])
    except Exception:
        pass
    return None


def process_name(pid: int) -> str:
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                text=True, errors="ignore")
            row = next(csv.reader(io.StringIO(out)), [])
            return row[0] if row else ""
        return Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def terminate_pid(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def http_json(url: str, timeout: float = 1.5, api_key: str = ""):
    try:
        request = urllib.request.Request(url)
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8", "replace"))
        except Exception:
            return None
    except Exception:
        return None


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def tail_file(path: Path, max_lines: int = 220) -> str:
    try:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 128 * 1024))
            data = handle.read().decode("utf-8", "replace")
        return "\n".join(strip_ansi(data).splitlines()[-max_lines:])
    except Exception:
        return ""


def sanitize_alias(name: str) -> str:
    lower = name.lower()
    match = re.search(r"(qwen[\d.]+)[-_ ]?(\d+(?:\.\d+)?b)", lower, re.I)
    if match:
        family = match.group(1).replace(".", "").replace("-", "")
        base = f"{family}-{match.group(2).lower()}"
        extras = []
        for token in ("turbo", "mtp", "ud"):
            if token in lower:
                extras.append(token)
        quant = re.search(r"(iq\d+_[a-z0-9]+|q\d+_k_[a-z]+|q\d+_k|q\d+_\d)",
                          lower)
        if quant:
            extras.append(quant.group(1).replace("_", ""))
        suffix = "-".join(dict.fromkeys(extras))
        return (f"{base}-{suffix}" if suffix else base)[:64]
    alias = re.sub(r"[^\w.\-]+", "-", name, flags=re.UNICODE).strip("-._").lower()
    alias = re.sub(r"-{2,}", "-", alias)
    return (alias or "local-model")[:48]


def unique_name(name: str, existing) -> str:
    name = re.sub(r"[\[\]\r\n;#=]", "-", (name or "profile").strip()) or "profile"
    candidate = name
    index = 2
    while candidate in existing:
        candidate = f"{name}-{index}"
        index += 1
    return candidate


def split_extra(extra: str) -> list[str]:
    if not extra.strip():
        return []
    try:
        return shlex.split(extra, posix=not IS_WINDOWS)
    except Exception:
        return extra.split()


def extra_to_ini(extra: str) -> list[str]:
    """Convert common CLI flags to preset keys (best effort)."""
    parts = split_extra(extra)
    lines = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token.startswith("--"):
            key = token[2:]
            value = "true"
            if "=" in key:
                key, value = key.split("=", 1)
            elif index + 1 < len(parts) and not parts[index + 1].startswith("-"):
                value = parts[index + 1]
                index += 1
            lines.append(f"{key} = {value}")
        index += 1
    return lines


# ---------------------------------------------------------------------------
# GGUF metadata (header only, never tensor data)
# ---------------------------------------------------------------------------

GGUF_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_KV_CACHE: dict[str, tuple[float, int, dict]] = {}


class GGUFReader:
    def __init__(self, file_obj):
        self.f = file_obj

    def read_exact(self, size: int) -> bytes:
        data = self.f.read(size)
        if len(data) != size:
            raise EOFError("incomplete GGUF metadata")
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self.read_exact(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read_exact(8))[0]

    def string(self) -> str:
        length = self.u64()
        if length > 100 * 1024 * 1024:
            raise ValueError("GGUF string too long")
        return self.read_exact(length).decode("utf-8", "replace")

    def skip_array(self) -> None:
        element_type = self.u32()
        count = self.u64()
        if element_type in GGUF_FIXED:
            self.f.seek(count * GGUF_FIXED[element_type], 1)
        elif element_type == 8:
            for _ in range(count):
                self.f.seek(self.u64(), 1)
        elif element_type == 9:
            for _ in range(count):
                self.skip_array()
        else:
            raise ValueError(f"unknown GGUF array type {element_type}")

    def scalar(self, value_type: int):
        if value_type == 8:
            return self.string()
        if value_type == 7:
            return self.read_exact(1)[0] != 0
        fmt = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
               6: "f", 10: "Q", 11: "q", 12: "d"}
        if value_type not in fmt:
            raise ValueError(f"unknown GGUF value type {value_type}")
        code = fmt[value_type]
        return struct.unpack("<" + code, self.read_exact(struct.calcsize(code)))[0]

    def metadata(self) -> dict:
        if self.read_exact(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        self.u32()
        self.u64()
        kv_count = self.u64()
        result = {}
        for _ in range(kv_count):
            key = self.string()
            value_type = self.u32()
            if value_type == 9:
                self.skip_array()
            else:
                result[key] = self.scalar(value_type)
        return result


def gguf_kv_info(path: Path) -> dict:
    with path.open("rb") as handle:
        meta = GGUFReader(handle).metadata()

    def pick(suffix: str):
        for key, value in meta.items():
            if key.endswith(suffix):
                return value
        return None

    block_count = int(pick(".block_count") or 0)
    interval = int(pick(".full_attention_interval") or 1)
    kv_heads = pick(".attention.head_count_kv")
    if isinstance(kv_heads, list):
        kv_heads = max(kv_heads) if kv_heads else 0
    kv_heads = int(kv_heads or 0)
    heads = int(pick(".attention.head_count") or 0)
    embedding = int(pick(".embedding_length") or 0)
    key_len = int(pick(".attention.key_length") or 0)
    value_len = int(pick(".attention.value_length") or 0)
    context_length = int(pick(".context_length") or 0)
    nextn_layers = int(pick(".nextn_predict_layers") or 0)
    if not key_len and heads and embedding:
        key_len = embedding // heads
    if not value_len:
        value_len = key_len
    if block_count <= 0 or kv_heads <= 0 or key_len <= 0 or value_len <= 0:
        raise ValueError("GGUF has no attention parameters")
    # Several hybrid models put the last full-attention block at the end
    # (for example 65 blocks with every 4th + the final block = 17 layers).
    # Integer division misses that final block.
    full_layers = max(1, (block_count + max(1, interval) - 1)
                      // max(1, interval))
    return {
        "block_count": block_count,
        "interval": interval,
        "full_layers": full_layers,
        "kv_heads": kv_heads,
        "key_len": key_len,
        "value_len": value_len,
        "context_length": context_length,
        "nextn_layers": nextn_layers,
        "has_mtp": nextn_layers > 0,
        "source": "GGUF metadata",
    }


def fallback_kv_info(model: Path) -> dict:
    name = model.name.lower()
    if "27b" in name:
        return {"block_count": 65, "interval": 4, "full_layers": 17, "kv_heads": 4,
                "key_len": 256, "value_len": 256, "context_length": 262144,
                "nextn_layers": 0, "has_mtp": False, "source": "filename estimate"}
    if "9b" in name:
        return {"block_count": 32, "interval": 4, "full_layers": 8, "kv_heads": 4,
                "key_len": 256, "value_len": 256, "context_length": 262144,
                "nextn_layers": 0, "has_mtp": False, "source": "filename estimate"}
    return {"block_count": 32, "interval": 1, "full_layers": 32, "kv_heads": 8,
            "key_len": 128, "value_len": 128, "context_length": 0,
            "nextn_layers": 0, "has_mtp": False, "source": "generic estimate"}


def get_kv_info(model: Path) -> dict:
    try:
        stat = model.stat()
    except Exception:
        return fallback_kv_info(model)
    key = str(model)
    cached = _KV_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        info = gguf_kv_info(model)
    except Exception as exc:
        log(f"failed to read metadata for {model.name}: {exc}")
        info = fallback_kv_info(model)
    _KV_CACHE[key] = (stat.st_mtime, stat.st_size, info)
    return info


def kv_bytes_per_token(info: dict, key_precision: str, value_precision: str) -> float:
    ratios = {"f16": 1.0, "q8_0": (34 / 32) / 2, "q4_0": (18 / 32) / 2}
    key_ratio = ratios.get(key_precision, 1.0)
    value_ratio = ratios.get(value_precision, 1.0)
    per_layer = info["kv_heads"]
    key_bytes = info["full_layers"] * per_layer * info["key_len"] * 2 * key_ratio
    value_bytes = info["full_layers"] * per_layer * info["value_len"] * 2 * value_ratio
    return key_bytes + value_bytes


def kv_cache_gb(info: dict, ctx: int, key_precision: str, value_precision: str) -> float:
    return ctx * kv_bytes_per_token(info, key_precision, value_precision) / GIB


def profile_has_mtp(profile: dict, base: Path) -> bool:
    """Whether the GGUF contains an MTP/nextn head."""
    model = resolve_profile_path(profile.get("model"), base)
    if model.is_file():
        try:
            return bool(get_kv_info(model).get("has_mtp"))
        except Exception:
            pass
    return bool(profile.get("mtp"))


def profile_mtp_enabled(profile: dict, base: Path) -> bool:
    """MTP is used only when the GGUF supports it and the user enabled it."""
    return profile_has_mtp(profile, base) and bool(profile.get("mtp_enabled", True))


def profile_mtp_draft_n(profile: dict) -> int:
    try:
        return max(1, min(8, int(profile.get("mtp_draft_n", 2))))
    except Exception:
        return 2


def estimate_profile(profile: dict, base: Path,
                     reserve_gb: float = VRAM_RESERVE_GB) -> dict | None:
    try:
        model = resolve_profile_path(profile.get("model"), base)
        if not model.is_file():
            return None
        info = get_kv_info(model)
        model_gb = model.stat().st_size / GIB
        kv_gb = kv_cache_gb(info, int(profile.get("ctx", 32768)),
                            profile.get("kvk", "f16"), profile.get("kvv", "f16"))
        vision_gb = 0.0
        mmproj = resolve_profile_path(profile.get("mmproj"), base)
        if mmproj.is_file() and not profile.get("vision_cpu", True):
            # The projector itself plus its vision compute buffer.
            vision_gb = mmproj.stat().st_size / GIB + 0.2

        # llama.cpp needs more than weights + KV: CUDA context, graph
        # buffers and the batch/ubatch scratch space.  The old fixed 0.8 GB
        # underestimated a 27B model by about 1.1 GB.
        try:
            ubatch = max(1, int(profile.get("ubatch", 512)))
        except Exception:
            ubatch = 512
        try:
            batch = max(1, int(profile.get("batch", 2048)))
        except Exception:
            batch = 2048
        ubatch_factor = min(2.0, max(0.25, ubatch / 512.0))
        batch_factor = 0.7 + 0.3 * min(2.0, max(0.0, batch / 2048.0))
        compute_gb = 0.5 + 0.10 * model_gb * ubatch_factor * batch_factor
        mtp_gb = 0.2 if profile_mtp_enabled(profile, base) else 0.0
        try:
            reserve = max(0.0, float(reserve_gb))
        except Exception:
            reserve = VRAM_RESERVE_GB
        total = (model_gb + kv_gb + vision_gb + compute_gb
                 + mtp_gb + reserve)
        return {"total": total, "model": model_gb, "kv": kv_gb,
                "vision": vision_gb, "compute": compute_gb,
                "mtp": mtp_gb, "reserve": reserve, "info": info}
    except Exception:
        return None


def resolve_profile_path(value, base: Path) -> Path:
    if value in (None, ""):
        return Path()
    text = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


# ---------------------------------------------------------------------------
# Model scanning
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", ".cache", "node_modules", "__pycache__", ".venv", "venv"}


def scan_models(scan_dirs) -> list[Path]:
    found: dict[str, Path] = {}
    for root in scan_dirs:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIRS
                           and not d.lower().startswith("_archive")]
            for name in filenames:
                lower = name.lower()
                if not lower.endswith(".gguf") or "mmproj" in lower:
                    continue
                path = Path(dirpath) / name
                try:
                    key = str(path.resolve()).lower()
                except Exception:
                    key = str(path).lower()
                found.setdefault(key, path)
    models = list(found.values())
    try:
        models.sort(key=lambda p: (p.stat().st_size, p.name.lower()), reverse=True)
    except Exception:
        models.sort(key=lambda p: p.name.lower())
    return models


def scan_mmproj(scan_dirs) -> list[Path]:
    found: dict[str, Path] = {}
    for root in scan_dirs:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIRS
                           and not d.lower().startswith("_archive")]
            for name in filenames:
                if not name.lower().endswith(".gguf") or "mmproj" not in name.lower():
                    continue
                path = Path(dirpath) / name
                try:
                    key = str(path.resolve()).lower()
                except Exception:
                    key = str(path).lower()
                found.setdefault(key, path)
    result = list(found.values())
    result.sort(key=lambda p: (len(p.name), p.name.lower()))
    return result


def auto_match_mmproj(model: Path, mmprojs: list[Path]) -> Path | None:
    same_dir = [p for p in mmprojs if p.parent == model.parent]
    if same_dir:
        return sorted(same_dir, key=lambda p: len(p.name))[0]
    match = re.search(r"(\d+(?:\.\d+)?B)", model.name, re.I)
    if match:
        token = match.group(1).lower()
        token_match = [p for p in mmprojs if token in p.name.lower()]
        if token_match:
            return sorted(token_match, key=lambda p: len(p.name))[0]
    return mmprojs[0] if len(mmprojs) == 1 else None


# ---------------------------------------------------------------------------
# GPU helpers
# ---------------------------------------------------------------------------

def gpu_memory_usage(pid: int) -> tuple[float | None, float | None, str]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            text=True, timeout=4)
        process_mb = None
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] == str(pid):
                process_mb = float(parts[1])
                break
        total_mb = None
        try:
            total_out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                text=True, timeout=4)
            first = total_out.strip().splitlines()
            if first:
                total_mb = float(first[0].strip())
        except Exception:
            pass
        if process_mb is not None or total_mb is not None:
            return (process_mb / 1024 if process_mb is not None else None,
                    total_mb / 1024 if total_mb is not None else None, "nvidia-smi")
    except Exception:
        pass

    if not IS_WINDOWS:
        return None, None, ""

    script = (
        "$p=Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' -MaxSamples 1 "
        "-ErrorAction SilentlyContinue;"
        f"$proc=($p.CounterSamples | Where-Object {{ $_.InstanceName -like '*pid_{pid}*' }} "
        "| Measure-Object CookedValue -Sum).Sum;"
        "$a=Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' -MaxSamples 1 "
        "-ErrorAction SilentlyContinue;"
        "$total=($a.CounterSamples | Measure-Object CookedValue -Maximum).Maximum;"
        "Write-Output (\"$proc,$total\")"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            text=True, timeout=6).strip()
        parts = out.split(",")
        if len(parts) >= 2:
            proc = float(parts[0]) / GIB if parts[0].strip() not in ("", "0") else None
            total = float(parts[1]) / GIB if parts[1].strip() not in ("", "0") else None
            if proc is not None or total is not None:
                return proc, total, "Windows GPU counters"
    except Exception:
        pass
    return None, None, ""


_GPU_INFO: tuple[str, float | None] | None = None


def detect_gpu() -> tuple[str, float | None]:
    global _GPU_INFO
    if _GPU_INFO is not None:
        return _GPU_INFO
    _GPU_INFO = _detect_gpu_uncached()
    return _GPU_INFO


def _detect_gpu_uncached() -> tuple[str, float | None]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            text=True, timeout=3).strip().splitlines()
        if out:
            parts = [p.strip() for p in out[0].split(",")]
            if len(parts) >= 2:
                return parts[0], float(parts[1]) / 1024.0
    except Exception:
        pass
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name"],
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                text=True, timeout=5).strip()
            if out:
                return out, None
        except Exception:
            pass
    return "Unknown GPU", None


# ---------------------------------------------------------------------------
# Window frame
# ---------------------------------------------------------------------------

def colorref(hex_color: str) -> int:
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return red | (green << 8) | (blue << 16)


def apply_dark_window_frame(window: tk.Tk) -> None:
    if not IS_WINDOWS:
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)
        if not hwnd:
            hwnd = window.winfo_id()
        enabled = ctypes.c_int(1 if THEME_DARK else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        border = ctypes.c_uint(colorref(BG))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        for attribute, color in ((35, CARD), (36, FG)):
            value = ctypes.c_uint(colorref(color))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# llama.cpp process control
# ---------------------------------------------------------------------------

class ServerController:
    def __init__(self, config: Config):
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.last_cmd: list[str] = []

    def managed_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd: list[str], cwd: Path) -> int:
        log_path = self.config.server_log()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        console_path = log_path.with_name("llama-server-console.log")
        console = console_path.open("ab")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=console, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        finally:
            console.close()
        self.last_cmd = cmd
        log("start: " + " ".join(cmd))
        return int(self.proc.pid)

    def stop(self, port: int) -> tuple[bool, str]:
        pid = find_listener_pid(port)
        if pid:
            name = process_name(pid)
            if "llama" not in name.lower():
                return False, f"Port {port} is used by {name or 'unknown'} (PID {pid})."
            terminate_pid(pid)
            for _ in range(30):
                time.sleep(0.2)
                if not is_port_open(port):
                    break
            self.proc = None
            log(f"stop llama-server PID {pid}")
            return True, f"Stopped PID {pid}"
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.proc = None
            return True, "Stopped"
        return True, "Model server is not running."


def build_single_command(cfg: Config, name: str, profile: dict) -> list[str]:
    server = cfg.resolve(cfg.data.get("server_path"))
    model = cfg.resolve(profile.get("model"))
    cmd = [str(server), "-m", str(model), "--alias", name,
           "-c", str(profile.get("ctx", 32768)),
           "--ctx-checkpoints", "8",
           "-ngl", str(profile.get("ngl", 999)),
           "-fa", "on",
           "-b", str(profile.get("batch", 2048)),
           "-ub", str(profile.get("ubatch", 512)),
           "-np", str(profile.get("parallel", 1)),
           "--jinja", "--reasoning", "off", "--reasoning-budget", "0",
           "--host", str(cfg.data.get("host", "127.0.0.1")),
           "--port", str(cfg.data.get("port", 8081)),
           "--log-file", str(cfg.server_log()), "--slots"]
    load_mode = str(profile.get("load_mode") or "auto").strip()
    if load_mode in ("none", "mmap", "mlock", "mmap+mlock", "dio"):
        cmd += ["--load-mode", load_mode]
    mmproj = cfg.resolve(profile.get("mmproj"))
    if profile.get("mmproj") and mmproj.is_file():
        cmd += ["--mmproj", str(mmproj)]
        if profile.get("vision_cpu", True):
            cmd += ["--no-mmproj-offload",
                    "--mtmd-batch-max-tokens", str(profile.get("mtmd_batch", 256))]
    if profile.get("kvk", "f16") != "f16":
        cmd += ["-ctk", profile["kvk"]]
    if profile.get("kvv", "f16") != "f16":
        cmd += ["-ctv", profile["kvv"]]
    if profile_mtp_enabled(profile, cfg.base):
        cmd += ["--spec-type", "draft-mtp",
                "--spec-draft-n-max", str(profile_mtp_draft_n(profile))]
    cmd += split_extra(profile.get("extra", ""))
    return cmd


def router_preset_text(cfg: Config) -> tuple[str, list[str]]:
    defaults = cfg.data.get("defaults", {})
    lines = [
        "# Generated by Local Model Manager. Do not edit by hand.",
        "[*]",
        f"gpu-layers = {defaults.get('ngl', 999)}",
        "flash-attn = on",
        f"batch-size = {defaults.get('batch', 2048)}",
        f"ubatch-size = {defaults.get('ubatch', 512)}",
        f"parallel = {defaults.get('parallel', 1)}",
        "jinja = 1",
        "reasoning = off",
        "reasoning-budget = 0",
        "ctx-checkpoints = 4",
        "",
    ]
    all_profiles = cfg.data.get("profiles", {})
    clients = cfg.data.get("clients") or []
    assigned = {
        str(client.get("profile") or "").strip()
        for client in clients if isinstance(client, dict)
    }
    assigned.discard("")
    expose_only = bool(cfg.data.get("expose_assigned_only", True))
    if expose_only and assigned:
        profiles = {name: profile for name, profile in all_profiles.items()
                    if name in assigned}
        lines.append("# Only profiles assigned to client slots are exposed.")
    else:
        profiles = all_profiles
        if expose_only:
            lines.append("# No client slots have a profile; exposing all profiles.")
    missing = []
    for name, profile in profiles.items():
        model = cfg.resolve(profile.get("model"))
        if not model.is_file():
            missing.append(name)
            continue
        section = re.sub(r"[\[\]\r\n;#=]", "-", name).strip() or "profile"
        lines.append(f"[{section}]")
        lines.append(f"model = {model.as_posix()}")
        lines.append(f"ctx-size = {profile.get('ctx', 32768)}")
        lines.append(f"n-gpu-layers = {profile.get('ngl', 999)}")
        lines.append(f"batch-size = {profile.get('batch', 2048)}")
        lines.append(f"ubatch-size = {profile.get('ubatch', 512)}")
        lines.append(f"parallel = {profile.get('parallel', 1)}")
        load_mode = str(profile.get("load_mode") or "auto").strip()
        if load_mode in ("none", "mmap", "mlock", "mmap+mlock", "dio"):
            lines.append(f"load-mode = {load_mode}")
        if profile.get("kvk", "f16") != "f16":
            lines.append(f"cache-type-k = {profile['kvk']}")
        if profile.get("kvv", "f16") != "f16":
            lines.append(f"cache-type-v = {profile['kvv']}")
        mmproj = cfg.resolve(profile.get("mmproj"))
        if profile.get("mmproj") and mmproj.is_file():
            lines.append(f"mmproj = {mmproj.as_posix()}")
            if profile.get("vision_cpu", True):
                lines.append("mmproj-offload = false")
                lines.append(f"mtmd-batch-max-tokens = {profile.get('mtmd_batch', 256)}")
        if profile_mtp_enabled(profile, cfg.base):
            lines.append("spec-type = draft-mtp")
            lines.append(f"spec-draft-n-max = {profile_mtp_draft_n(profile)}")
        lines += extra_to_ini(profile.get("extra", ""))
        lines.append("")
    return "\n".join(lines) + "\n", missing


def build_router_command(cfg: Config, preset: Path) -> list[str]:
    server = cfg.resolve(cfg.data.get("server_path"))
    cmd = [
        str(server),
        "--models-preset", str(preset),
        "--models-max", str(cfg.data.get("models_max", 1)),
        "--models-autoload",
        "--host", str(cfg.data.get("host", "127.0.0.1")),
        "--port", str(cfg.data.get("port", 8081)),
        "--log-file", str(cfg.server_log()),
        "--log-colors", "off",
    ]
    api_key = str(cfg.data.get("api_key") or "").strip()
    if api_key:
        cmd += ["--api-key", api_key]
    return cmd


# ---------------------------------------------------------------------------
# Profile dialog
# ---------------------------------------------------------------------------

class ProfileDialog(tk.Toplevel):
    def __init__(self, app: "App", title: str, name: str, profile: dict):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self.result: tuple[str, dict] | None = None
        self.original_name = name
        self.model_map = dict(app.model_map)
        self.mmproj_map = dict(app.mmproj_map)

        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app)
        self.bind("<Escape>", lambda _e: self.destroy())

        self.no_vision = T("（不使用视觉）")
        self.name_var = tk.StringVar(value=name)
        self.model_var = tk.StringVar()
        self.mmproj_var = tk.StringVar(value=self.no_vision)
        self.ctx_var = tk.StringVar(value=str(profile.get("ctx", 32768)))
        self.kvk_var = tk.StringVar(value=profile.get("kvk", "f16"))
        self.kvv_var = tk.StringVar(value=profile.get("kvv", "f16"))
        self.ngl_var = tk.StringVar(value=str(profile.get("ngl", 999)))
        self.batch_var = tk.StringVar(value=str(profile.get("batch", 2048)))
        self.ubatch_var = tk.StringVar(value=str(profile.get("ubatch", 512)))
        self.parallel_var = tk.StringVar(value=str(profile.get("parallel", 1)))
        self.vision_cpu_var = tk.BooleanVar(value=bool(profile.get("vision_cpu", True)))
        self.mtmd_batch_var = tk.StringVar(value=str(profile.get("mtmd_batch", 256)))
        self.load_mode_choices = {
            T("自动（mmap）"): "auto",
            T("不保留文件缓存（none）"): "none",
            T("DirectIO（dio）"): "dio",
        }
        current_mode = str(profile.get("load_mode") or "auto").strip()
        self.load_mode_var = tk.StringVar(
            value=next((display for display, value in self.load_mode_choices.items()
                        if value == current_mode), T("自动（mmap）")))
        self.mtp_detected = bool(profile.get("mtp", False))
        self.mtp_enabled_var = tk.BooleanVar(
            value=bool(profile.get("mtp_enabled", True)))
        self.mtp_draft_var = tk.StringVar(
            value=str(profile.get("mtp_draft_n", 2)))
        self.mtp_status_var = tk.StringVar(value=T("未检测"))
        self.extra_var = tk.StringVar(value=profile.get("extra", ""))
        self.info_var = tk.StringVar(value="")
        self.estimate_var = tk.StringVar(value="")

        self._select_existing(profile)
        self._build()
        self._refresh_info()
        for var in (self.ctx_var, self.kvk_var, self.kvv_var, self.vision_cpu_var):
            var.trace_add("write", lambda *_: self._refresh_estimate())
        self.after(10, self._show)

    def _show(self) -> None:
        self.update_idletasks()
        self._center()
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass
        apply_dark_window_frame(self)
        self.deiconify()
        self.lift()
        self.grab_set()
        self.after(10, lambda: apply_dark_window_frame(self))
        self._fade(0)

    def _fade(self, step: int) -> None:
        try:
            alpha = min(1.0, step * 0.12)
            self.attributes("-alpha", alpha)
            if alpha < 1.0:
                self.after(16, lambda: self._fade(step + 1))
        except Exception:
            pass

    def _select_existing(self, profile: dict) -> None:
        model = resolve_profile_path(profile.get("model"), self.app.cfg.base)
        for display, path in self.model_map.items():
            if path == model:
                self.model_var.set(display)
                break
        else:
            if model.is_file():
                display = self.app.display_for(model)
                self.model_map[display] = model
                self.model_var.set(display)
        mmproj = resolve_profile_path(profile.get("mmproj"), self.app.cfg.base)
        for display, path in self.mmproj_map.items():
            if path == mmproj:
                self.mmproj_var.set(display)
                break

    def _center(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - width) // 2
        y = self.app.winfo_rooty() + (self.app.winfo_height() - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build(self) -> None:
        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)

        def label(text, row, col):
            ttk.Label(body, text=text).grid(row=row, column=col, sticky="w",
                                            padx=(0, 8), pady=5)

        label(T("配置名称"), 0, 0)
        ttk.Entry(body, textvariable=self.name_var, width=28).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=5)

        label(T("模型 GGUF"), 1, 0)
        model_combo = ttk.Combobox(body, textvariable=self.model_var,
                                   values=list(self.model_map), state="readonly")
        model_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        model_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_model_change())
        ttk.Button(body, text=T("浏览…"), command=self._browse_model).grid(
            row=1, column=3, sticky="e", padx=(6, 0), pady=5)

        label(T("视觉模块"), 2, 0)
        mmproj_combo = ttk.Combobox(body, textvariable=self.mmproj_var,
                                    state="readonly",
                                    values=[self.no_vision] + list(self.mmproj_map))
        mmproj_combo.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        mmproj_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_info())
        ttk.Button(body, text=T("自动匹配"), command=self._auto_vision).grid(
            row=2, column=3, sticky="e", padx=(6, 0), pady=5)

        ttk.Label(body, textvariable=self.info_var, style="Muted.TLabel",
                  wraplength=560, justify="left").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(0, 6))

        label(T("上下文"), 4, 0)
        ttk.Combobox(body, textvariable=self.ctx_var, values=CTX_PRESETS).grid(
            row=4, column=1, sticky="ew", pady=5)
        label("KV K", 4, 2)
        ttk.Combobox(body, textvariable=self.kvk_var, values=["f16", "q8_0", "q4_0"],
                     state="readonly", width=8).grid(row=4, column=3, sticky="w", pady=5)

        label(T("GPU 层数"), 5, 0)
        ttk.Entry(body, textvariable=self.ngl_var, width=10).grid(
            row=5, column=1, sticky="w", pady=5)
        label("KV V", 5, 2)
        ttk.Combobox(body, textvariable=self.kvv_var, values=["f16", "q8_0", "q4_0"],
                     state="readonly", width=8).grid(row=5, column=3, sticky="w", pady=5)

        label("batch", 6, 0)
        ttk.Entry(body, textvariable=self.batch_var, width=10).grid(
            row=6, column=1, sticky="w", pady=5)
        label("ubatch", 6, 2)
        ttk.Entry(body, textvariable=self.ubatch_var, width=10).grid(
            row=6, column=3, sticky="w", pady=5)

        label(T("并行槽"), 7, 0)
        ttk.Entry(body, textvariable=self.parallel_var, width=10).grid(
            row=7, column=1, sticky="w", pady=5)
        label(T("视觉批大小"), 7, 2)
        ttk.Entry(body, textvariable=self.mtmd_batch_var, width=10).grid(
            row=7, column=3, sticky="w", pady=5)

        label(T("加载模式"), 8, 0)
        ttk.Combobox(body, textvariable=self.load_mode_var,
                     values=list(self.load_mode_choices),
                     state="readonly").grid(row=8, column=1, columnspan=2,
                                             sticky="ew", pady=5)
        ttk.Label(body, text=T("none/dio 可减少内存缓存"),
                  style="Muted.TLabel").grid(row=8, column=3, sticky="w",
                                             padx=(6, 0), pady=5)

        check_row = ttk.Frame(body)
        check_row.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(5, 5))
        ttk.Checkbutton(check_row, text=T("视觉编码放 CPU"),
                        variable=self.vision_cpu_var).pack(side="left")
        self.mtp_check = ttk.Checkbutton(
            check_row, text=T("启用 MTP"), variable=self.mtp_enabled_var)
        self.mtp_check.pack(side="left", padx=(24, 6))
        ttk.Label(check_row, textvariable=self.mtp_status_var,
                  style="Muted.TLabel").pack(side="left")
        ttk.Label(check_row, text=T("草稿数")).pack(side="left", padx=(16, 4))
        self.mtp_draft_combo = ttk.Combobox(
            check_row, textvariable=self.mtp_draft_var, values=("1", "2", "3"),
            state="readonly", width=3)
        self.mtp_draft_combo.pack(side="left")

        label(T("额外参数"), 10, 0)
        ttk.Entry(body, textvariable=self.extra_var).grid(
            row=10, column=1, columnspan=3, sticky="ew", pady=5)

        tk.Label(body, textvariable=self.estimate_var, bg=BG, fg=MUTED,
                 font=(FONT_UI, 10), anchor="w", justify="left",
                 wraplength=560).grid(row=11, column=0, columnspan=4,
                                      sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=12, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text=T("取消"), command=self.destroy).pack(side="right")
        ttk.Button(buttons, text=T("保存"), style="Accent.TButton",
                   command=self._save).pack(side="right", padx=(0, 8))

    def _browse_model(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=T("选择 GGUF 模型"),
            filetypes=[("GGUF", "*.gguf"), ("All files", "*.*")])
        if not path:
            return
        model = Path(path)
        display = self.app.display_for(model)
        self.model_map[display] = model
        self.model_var.set(display)
        self._on_model_change()

    def _on_model_change(self) -> None:
        model = self.model_map.get(self.model_var.get())
        if model is None:
            return
        if not self.name_var.get().strip() or self.original_name == "":
            self.name_var.set(unique_name(sanitize_alias(model.stem),
                                          self.app.cfg.data.get("profiles", {})))
        candidate = auto_match_mmproj(model, list(self.mmproj_map.values()))
        if candidate:
            for display, path in self.mmproj_map.items():
                if path == candidate:
                    self.mmproj_var.set(display)
                    break
        self._refresh_info()

    def _auto_vision(self) -> None:
        model = self.model_map.get(self.model_var.get())
        if model is None:
            return
        candidate = auto_match_mmproj(model, list(self.mmproj_map.values()))
        if candidate is None:
            self.mmproj_var.set(self.no_vision)
        else:
            for display, path in self.mmproj_map.items():
                if path == candidate:
                    self.mmproj_var.set(display)
                    break
        self._refresh_info()

    def _current_profile(self) -> dict:
        def as_int(var, fallback):
            try:
                return int(str(var.get()).strip())
            except Exception:
                return fallback

        model = self.model_map.get(self.model_var.get())
        mmproj = self.mmproj_map.get(self.mmproj_var.get())
        return {
            "model": self.app.cfg.portable(model) if model else "",
            "mmproj": self.app.cfg.portable(mmproj) if mmproj else "",
            "ctx": as_int(self.ctx_var, 32768),
            "kvk": self.kvk_var.get(),
            "kvv": self.kvv_var.get(),
            "ngl": as_int(self.ngl_var, 999),
            "batch": as_int(self.batch_var, 2048),
            "ubatch": as_int(self.ubatch_var, 512),
            "parallel": as_int(self.parallel_var, 1),
            "vision_cpu": bool(self.vision_cpu_var.get()),
            "mtmd_batch": as_int(self.mtmd_batch_var, 256),
            "extra": self.extra_var.get().strip(),
            "mtp": bool(self.mtp_detected),
            "mtp_enabled": bool(self.mtp_enabled_var.get()),
            "mtp_draft_n": profile_mtp_draft_n(
                {"mtp_draft_n": self.mtp_draft_var.get()}),
            "load_mode": self.load_mode_choices.get(
                self.load_mode_var.get(), "auto"),
        }

    def _refresh_info(self) -> None:
        model = self.model_map.get(self.model_var.get())
        if model is None:
            self.mtp_detected = False
            self.mtp_status_var.set(T("未检测"))
            self.info_var.set(T("还没有选择模型。"))
            self._set_mtp_controls()
            return
        try:
            info = get_kv_info(model)
            self.mtp_detected = bool(info.get("has_mtp"))
            self.mtp_status_var.set(
                T("支持") if self.mtp_detected else T("不支持"))
            native = info.get("context_length") or "?"
            self.info_var.set(T(
                "{name} | {size} | 原生上下文 {native}",
                name=model.name, size=human_size(model.stat().st_size),
                native=native))
        except Exception as exc:
            self.mtp_detected = False
            self.mtp_status_var.set(T("未检测"))
            self.info_var.set(T("{model} | 读取元数据失败：{exc}", model=model, exc=exc))
        self._set_mtp_controls()
        self._refresh_estimate()

    def _set_mtp_controls(self) -> None:
        try:
            self.mtp_check.configure(
                state="normal" if self.mtp_detected else "disabled")
            self.mtp_draft_combo.configure(
                state="readonly" if self.mtp_detected else "disabled")
        except Exception:
            pass

    def _refresh_estimate(self) -> None:
        profile = self._current_profile()
        if not profile["model"]:
            self.estimate_var.set("")
            return
        reserve = float(self.app.cfg.data.get("vram_reserve_gb",
                                              VRAM_RESERVE_GB))
        estimate = estimate_profile(profile, self.app.cfg.base, reserve)
        if estimate is None:
            self.estimate_var.set(T("无法估算：模型文件不存在。"))
            return
        vram = float(self.app.cfg.data.get("vram_gb") or 0)
        warn = (T("   ⚠ 超过当前显存设置")
                if vram and estimate["total"] > max(0.0, vram - VRAM_SAFETY_GB)
                else "")
        mtp_text = (T(" + MTP {gb:.2f}", gb=estimate["mtp"])
                    if estimate.get("mtp") else "")
        self.estimate_var.set(
            T("预计显存 {total:.2f} GB（模型 {model:.2f} + KV {kv:.2f}"
              " + 视觉 {vision:.2f} + 计算 {compute:.2f}"
              " + 系统 {reserve:.2f}{mtp}）{warn}",
              total=estimate["total"], model=estimate["model"],
              kv=estimate["kv"], vision=estimate["vision"],
              compute=estimate.get("compute", 0.0),
              reserve=estimate.get("reserve", 0.0),
              mtp=mtp_text, warn=warn))

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(T("无法保存"), T("配置名称不能为空。"), parent=self)
            return
        if re.search(r"[\[\]\r\n;#=]", name):
            messagebox.showerror(T("无法保存"), T("配置名称包含不支持的字符。"),
                                 parent=self)
            return
        profiles = self.app.cfg.data.get("profiles", {})
        if name != self.original_name and name in profiles:
            messagebox.showerror(T("无法保存"), T("配置“{name}”已存在。", name=name), parent=self)
            return
        profile = self._current_profile()
        model = self.app.cfg.resolve(profile["model"]) if profile["model"] else None
        if model is None or not model.is_file():
            messagebox.showerror(T("无法保存"), T("请选择一个存在的 GGUF 模型。"), parent=self)
            return
        if profile["ctx"] < 512:
            messagebox.showerror(T("无法保存"), T("上下文至少 512。"), parent=self)
            return
        if not (1 <= self.app._port() <= 65535):
            messagebox.showerror(T("无法保存"), T("端口不合法。"), parent=self)
            return
        self.result = (name, profile)
        self.destroy()


# ---------------------------------------------------------------------------
# Service dialog
# ---------------------------------------------------------------------------

class ServiceDialog(tk.Toplevel):
    def __init__(self, app: "App", title: str, service: dict | None):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self.result: dict | None = None
        self.title(title)
        self.configure(bg=BG)
        self.transient(app)
        self.resizable(False, False)
        self.bind("<Escape>", lambda _e: self.destroy())

        service = service or {}
        self.original = dict(service)
        self.name_var = tk.StringVar(value=service.get("name", ""))
        self.port_var = tk.StringVar(value=str(service.get("port", "")))
        self.command_var = tk.StringVar(value=service.get("command", ""))
        self.url_var = tk.StringVar(value=service.get("url", ""))
        self.process_var = tk.StringVar(value=service.get("process", ""))
        self.stop_var = tk.StringVar(value=service.get("stop", ""))

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        rows = [
            (T("名称"), self.name_var, None),
            (T("端口"), self.port_var, None),
            (T("启动命令/文件"), self.command_var, self._browse_command),
            (T("打开地址"), self.url_var, None),
            (T("进程关键字"), self.process_var, None),
            (T("停止命令（可选）"), self.stop_var, self._browse_stop),
        ]
        for row, (text, var, browse) in enumerate(rows):
            ttk.Label(body, text=text).grid(row=row, column=0, sticky="w",
                                            padx=(0, 8), pady=5)
            ttk.Entry(body, textvariable=var, width=52).grid(
                row=row, column=1, sticky="ew", pady=5)
            if browse:
                ttk.Button(body, text=T("浏览…"), command=browse).grid(
                    row=row, column=2, padx=(6, 0), pady=5)

        buttons = ttk.Frame(body)
        buttons.grid(row=len(rows), column=0, columnspan=3, sticky="e",
                     pady=(14, 0))
        ttk.Button(buttons, text=T("取消"), command=self.destroy).pack(side="right")
        ttk.Button(buttons, text=T("保存"), style="Accent.TButton",
                   command=self._save).pack(side="right", padx=(0, 8))
        self.after(10, self._show)

    def _show(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - width) // 2
        y = self.app.winfo_rooty() + (self.app.winfo_height() - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass
        apply_dark_window_frame(self)
        self.deiconify()
        self.lift()
        self.grab_set()
        self.after(10, lambda: apply_dark_window_frame(self))
        self._fade(0)

    def _fade(self, step: int) -> None:
        try:
            alpha = min(1.0, step * 0.12)
            self.attributes("-alpha", alpha)
            if alpha < 1.0:
                self.after(16, lambda: self._fade(step + 1))
        except Exception:
            pass

    def _browse_command(self) -> None:
        path = filedialog.askopenfilename(parent=self, title=T("选择启动程序"))
        if path:
            self.command_var.set(path)

    def _browse_stop(self) -> None:
        path = filedialog.askopenfilename(parent=self, title=T("选择停止程序"))
        if path:
            self.stop_var.set(path)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        command = self.command_var.get().strip()
        if not name or not command:
            messagebox.showerror(T("无法保存"), T("名称和启动命令不能为空。"), parent=self)
            return
        try:
            port = int(self.port_var.get().strip() or "0")
        except Exception:
            port = 0
        self.result = {
            "name": name,
            "port": port,
            "command": command,
            "url": self.url_var.get().strip(),
            "process": self.process_var.get().strip(),
            "stop": self.stop_var.get().strip(),
            "plugin": self.original.get("plugin") or "custom",
            "api_base": self.original.get("api_base", ""),
            "options": dict(self.original.get("options") or {}),
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, config_path: Path):
        super().__init__()
        self.cfg = Config(config_path)
        self.server = ServerController(self.cfg)
        self.themes = load_themes(APP_DIR, self.cfg.base)
        ui = self.cfg.data.setdefault("ui", {})
        self.theme_name = str(ui.get("theme") or "暖夜")
        if self.theme_name not in self.themes:
            self.theme_name = "暖夜"
        self.theme = dict(self.themes[self.theme_name])
        for key, value in (ui.get("colors") or {}).items():
            if key in THEME_KEYS and _is_color(value):
                self.theme[key] = value
        apply_theme(self.theme)
        self.title(APP_NAME)
        self.geometry("1180x900")
        self.minsize(1000, 720)
        self.configure(bg=BG, highlightthickness=0, bd=0)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.busy = False
        self.last_action = ""
        self.model_map: dict[str, Path] = {}
        self.mmproj_map: dict[str, Path] = {}
        self.status_busy = False
        self.status_queue: queue.Queue = queue.Queue()
        self.current_pid: int | None = None
        self.last_log_text = ""
        self.tts_starting = False
        self.service_starting: set[int] = set()
        self.gpu_name, gpu_vram = detect_gpu()
        if not self.cfg.data.get("vram_gb") and gpu_vram:
            self.cfg.data["vram_gb"] = round(gpu_vram, 1)
            self.cfg.save()

        self.locales = load_locales(APP_DIR, self.cfg.base)
        set_language(self.cfg.data.get("language", "zh"))
        self.title(T("本地模型管理器"))

        self._setup_style()
        self._build_vars()
        self._build_ui()
        self.bind("<F5>", lambda _e: self.refresh_models())
        self.refresh_models(initial=True)
        self.refresh_profiles()
        self.refresh_services()
        self.refresh_status()
        self.after(150, lambda: apply_dark_window_frame(self))

    # ---------- style ----------

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        hover = blend(CARD, FG, 0.10)
        pressed = blend(CARD, FG, 0.17)
        disabled_bg = blend(BG, CARD, 0.55)
        disabled_fg = blend(MUTED, BG, 0.45)
        accent_fg = blend(ACCENT, "#000000", 0.72)
        accent_hover = blend(ACCENT, FG, 0.15)
        accent_pressed = blend(ACCENT, "#000000", 0.12)
        danger_fg = blend(RED, "#000000", 0.70)
        danger_hover = blend(RED, FG, 0.15)
        danger_pressed = blend(RED, "#000000", 0.12)
        select_bg = blend(ACCENT, CARD, 0.55)

        # Native Tk popups and focus rings are the main source of stray white
        # edges in ttk. Configure them once for every child widget.
        self.option_add("*highlightThickness", 0)
        self.option_add("*highlightBackground", BG)
        self.option_add("*highlightColor", ACCENT)
        self.option_add("*selectBackground", ACCENT)
        self.option_add("*selectForeground", accent_fg)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", select_bg)
        self.option_add("*TCombobox*Listbox.selectForeground", FG)
        self.option_add("*TCombobox*Listbox.borderWidth", 0)

        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                        bordercolor=BORDER, font=(FONT_UI, 11))
        style.configure("TFrame", background=BG, borderwidth=0)
        style.configure("Card.TFrame", background=CARD, borderwidth=0)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                        font=(FONT_UI, 10))
        style.configure("Card.TLabel", background=CARD, foreground=FG)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED,
                        font=(FONT_UI, 10))
        style.configure("Title.TLabel", background=BG, foreground=ACCENT,
                        font=(FONT_UI, 20, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=(FONT_UI, 10))

        # Flat buttons whose hover/pressed states stay inside the palette.
        # lightcolor/darkcolor are the border elements clam would otherwise
        # turn white when the mouse enters the button.
        style.configure("TButton", background=CARD, foreground=FG, padding=(12, 7),
                        width=0, bordercolor=BORDER, lightcolor=CARD,
                        darkcolor=CARD, focuscolor=CARD, focusthickness=0,
                        relief="flat", borderwidth=0, font=(FONT_UI, 11))
        style.map(
            "TButton",
            background=[("pressed", pressed), ("active", hover),
                        ("disabled", disabled_bg)],
            foreground=[("disabled", disabled_fg)],
            lightcolor=[("pressed", pressed), ("active", hover),
                        ("disabled", disabled_bg)],
            darkcolor=[("pressed", pressed), ("active", hover),
                       ("disabled", disabled_bg)],
            bordercolor=[("pressed", BORDER), ("active", BORDER),
                         ("disabled", BORDER)],
            focuscolor=[("focus", CARD), ("active", CARD)])
        style.configure("Accent.TButton", background=ACCENT, foreground=accent_fg,
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        focuscolor=ACCENT, font=(FONT_UI, 11, "bold"))
        style.map(
            "Accent.TButton",
            background=[("pressed", accent_pressed), ("active", accent_hover),
                        ("disabled", blend(ACCENT, BG, 0.55))],
            foreground=[("disabled", blend(accent_fg, BG, 0.55))],
            lightcolor=[("pressed", accent_pressed), ("active", accent_hover),
                        ("disabled", blend(ACCENT, BG, 0.55))],
            darkcolor=[("pressed", accent_pressed), ("active", accent_hover),
                       ("disabled", blend(ACCENT, BG, 0.55))],
            bordercolor=[("pressed", ACCENT), ("active", ACCENT),
                         ("disabled", blend(ACCENT, BG, 0.55))])
        style.configure("Danger.TButton", background=RED, foreground=danger_fg,
                        bordercolor=RED, lightcolor=RED, darkcolor=RED,
                        focuscolor=RED)
        style.map(
            "Danger.TButton",
            background=[("pressed", danger_pressed), ("active", danger_hover),
                        ("disabled", blend(RED, BG, 0.55))],
            foreground=[("disabled", blend(danger_fg, BG, 0.55))],
            lightcolor=[("pressed", danger_pressed), ("active", danger_hover),
                        ("disabled", blend(RED, BG, 0.55))],
            darkcolor=[("pressed", danger_pressed), ("active", danger_hover),
                       ("disabled", blend(RED, BG, 0.55))],
            bordercolor=[("pressed", RED), ("active", RED),
                         ("disabled", blend(RED, BG, 0.55))])

        style.configure("TEntry", fieldbackground=FIELD, foreground=FG,
                        insertcolor=FG, bordercolor=BORDER, lightcolor=FIELD,
                        darkcolor=FIELD, padding=5)
        style.map("TEntry",
                  fieldbackground=[("disabled", disabled_bg)],
                  foreground=[("disabled", disabled_fg)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=FIELD, foreground=FG,
                        background=CARD, arrowcolor=FG, bordercolor=BORDER,
                        lightcolor=FIELD, darkcolor=FIELD,
                        selectbackground=select_bg, selectforeground=FG, padding=4)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD), ("disabled", disabled_bg)],
            foreground=[("readonly", FG), ("disabled", disabled_fg)],
            arrowcolor=[("disabled", disabled_fg), ("active", FG)],
            bordercolor=[("focus", ACCENT), ("active", BORDER)],
            lightcolor=[("focus", ACCENT), ("active", FIELD)],
            darkcolor=[("focus", ACCENT), ("active", FIELD)])

        style.configure("TLabelframe", background=BG, foreground=ACCENT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                        font=(FONT_UI, 11, "bold"))

        # Removing the notebook border avoids the thin light rectangle clam
        # normally draws around the page area.
        try:
            style.layout("TNotebook", [("TNotebook.client",
                                        {"sticky": "nswe"})])
        except Exception:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED,
                        padding=(18, 9), borderwidth=0, bordercolor=CARD,
                        lightcolor=CARD, darkcolor=CARD, focuscolor=CARD,
                        font=(FONT_UI, 11))
        style.map(
            "TNotebook.Tab",
            background=[("selected", hover), ("active", hover)],
            foreground=[("selected", FG), ("active", FG)],
            bordercolor=[("selected", hover), ("active", hover)],
            lightcolor=[("selected", hover), ("active", hover)],
            darkcolor=[("selected", hover), ("active", hover)])

        style.configure("TCheckbutton", background=BG, foreground=FG,
                        indicatorcolor=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, focuscolor=BG,
                        font=(FONT_UI, 11))
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  foreground=[("disabled", disabled_fg)],
                  indicatorcolor=[("selected", ACCENT), ("active", select_bg)])
        style.configure("Card.TCheckbutton", background=CARD, foreground=FG,
                        indicatorcolor=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, focuscolor=CARD,
                        font=(FONT_UI, 10))
        style.map("Card.TCheckbutton",
                  background=[("active", CARD)],
                  foreground=[("disabled", disabled_fg)],
                  indicatorcolor=[("selected", ACCENT), ("active", select_bg)])

        style.configure("TRadiobutton", background=BG, foreground=FG,
                        indicatorcolor=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, focuscolor=BG,
                        font=(FONT_UI, 11))
        style.map("TRadiobutton",
                  background=[("active", BG)],
                  foreground=[("disabled", disabled_fg)],
                  indicatorcolor=[("selected", ACCENT), ("active", select_bg)])

        style.configure("Treeview", background=FIELD, fieldbackground=FIELD,
                        foreground=FG, bordercolor=BORDER, rowheight=27,
                        lightcolor=FIELD, darkcolor=FIELD,
                        font=(FONT_UI, 10))
        style.configure("Treeview.Heading", background=CARD, foreground=ACCENT,
                        bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                        font=(FONT_UI, 10, "bold"))
        style.map("Treeview", background=[("selected", select_bg)],
                  foreground=[("selected", FG)])
        style.map("Treeview.Heading", background=[("active", hover)],
                  foreground=[("active", FG)])

        style.configure("TScrollbar", background=CARD, troughcolor=BG,
                        bordercolor=BORDER, arrowcolor=MUTED,
                        lightcolor=CARD, darkcolor=CARD)
        style.map("TScrollbar",
                  background=[("pressed", pressed), ("active", hover)],
                  arrowcolor=[("pressed", FG), ("active", FG)])
        style.configure("TProgressbar", background=ACCENT,
                        troughcolor=FIELD, bordercolor=BORDER)
        style.configure("TSeparator", background=BORDER)

    def _build_vars(self) -> None:
        data = self.cfg.data
        self.mode_var = tk.StringVar(value=data.get("mode", "router"))
        self.port_var = tk.StringVar(value=str(data.get("port", 8081)))
        self.models_max_var = tk.StringVar(value=str(data.get("models_max", 1)))
        self.expose_only_var = tk.BooleanVar(
            value=bool(data.get("expose_assigned_only", True)))
        self.server_path_var = tk.StringVar(value=str(self.cfg.resolve(data.get("server_path"))))
        self.server_build_var = tk.StringVar()
        self.build_map: dict[str, Path] = {}
        self.vram_var = tk.StringVar(value=str(data.get("vram_gb") or 0))
        self.vram_reserve_var = tk.StringVar(
            value=str(data.get("vram_reserve_gb", VRAM_RESERVE_GB)))
        self.status_var = tk.StringVar(value=T("检查中…"))
        self.detail_var = tk.StringVar(value="")
        self.connection_var = tk.StringVar(value="")
        choices = language_choices()
        active = current_language()
        self.language_var = tk.StringVar(
            value=next((display for display, code in choices if code == active),
                       choices[0][0] if choices else "中文"))
        self.language_map = {display: code for display, code in choices}
        self.no_role = T("（未设置）")
        self.client_rows: list[dict] = []
        self.theme_var = tk.StringVar(value=self._theme_label(self.theme_name))
        self.theme_display_map = {
            self._theme_label(name): name for name in self.themes}
        self.color_vars = {key: tk.StringVar(value=self.theme.get(key, ""))
                           for key in THEME_KEYS}
        self.color_swatches: dict[str, tk.Label] = {}

    # ---------- layout ----------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, bg=BG, padx=18, pady=14)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(header, text=T("本地模型管理器"), bg=BG, fg=ACCENT,
                 font=(FONT_UI, 20, "bold")).grid(row=0, column=0, rowspan=2,
                                                  sticky="w")

        language_box = tk.Frame(header, bg=BG)
        language_box.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 14))
        tk.Label(language_box, text=T("语言"), bg=BG, fg=MUTED,
                 font=(FONT_UI, 9)).pack(anchor="e")
        language_combo = ttk.Combobox(
            language_box, textvariable=self.language_var, state="readonly",
            values=[display for display, _code in language_choices()], width=10)
        language_combo.pack(anchor="e", pady=(2, 0))
        language_combo.bind("<<ComboboxSelected>>", self._change_language)

        status_box = tk.Frame(header, bg=CARD, padx=14, pady=9)
        status_box.grid(row=0, column=2, rowspan=2, sticky="e")
        self.status_dot = tk.Label(status_box, text="●", bg=CARD, fg=MUTED,
                                   font=(FONT_UI, 18))
        self.status_dot.grid(row=0, column=0, sticky="w")
        tk.Label(status_box, textvariable=self.status_var, bg=CARD, fg=FG,
                 font=(FONT_UI, 12, "bold")).grid(row=0, column=1, sticky="w",
                                                   padx=(7, 0))
        tk.Label(status_box, textvariable=self.detail_var, bg=CARD, fg=MUTED,
                 font=(FONT_UI, 9)).grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(2, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 8))
        self._build_model_tab()
        self._build_services_tab()
        self._build_appearance_tab()
        self._build_settings_tab()
        self._build_log_tab()


    def _build_model_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=T("模型"))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1, minsize=170)

        run = ttk.LabelFrame(tab, text=T("运行"), padding=10)
        run.grid(row=0, column=0, sticky="ew")
        for col in (1, 3, 5, 7):
            run.columnconfigure(col, weight=1)

        ttk.Label(run, text=T("模式")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Radiobutton(run, text=T("路由器（按请求自动切换）"), value="router",
                        variable=self.mode_var,
                        command=self._on_mode_change).grid(
            row=0, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(run, text=T("单模型"), value="single",
                        variable=self.mode_var,
                        command=self._on_mode_change).grid(
            row=0, column=3, sticky="w")

        ttk.Label(run, text=T("端口")).grid(row=0, column=4, sticky="e", padx=(12, 6))
        ttk.Entry(run, textvariable=self.port_var, width=8).grid(
            row=0, column=5, sticky="w")
        self.models_max_label = ttk.Label(run, text=T("同时加载"))
        self.models_max_label.grid(row=0, column=6, sticky="e", padx=(12, 6))
        self.models_max_spin = ttk.Spinbox(
            run, from_=1, to=8, textvariable=self.models_max_var, width=5)
        self.models_max_spin.grid(row=0, column=7, sticky="w")

        ttk.Label(run, text=T("服务器版本")).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.server_build_combo = ttk.Combobox(
            run, textvariable=self.server_build_var, state="readonly")
        self.server_build_combo.grid(
            row=1, column=1, columnspan=5, sticky="ew", pady=(8, 0))
        self.server_build_combo.bind("<<ComboboxSelected>>",
                                     self._on_build_selected)
        ttk.Button(run, text=T("浏览…"), command=self.browse_server).grid(
            row=1, column=6, columnspan=2, sticky="w", padx=(6, 0),
            pady=(8, 0))

        actions = ttk.Frame(run)
        actions.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        self.start_btn = ttk.Button(actions, text=T("启动模型"), style="Accent.TButton",
                                    command=self.start_server)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text=T("停止模型"), style="Danger.TButton",
                                   command=self.stop_server)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.restart_btn = ttk.Button(actions, text=T("重启"), command=self.restart_server)
        self.restart_btn.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text=T("打开日志"), command=self.open_log).pack(side="right")
        self.run_hint = ttk.Label(actions, text="", style="Muted.TLabel")
        self.run_hint.pack(side="right", padx=(0, 12))

        connect = ttk.LabelFrame(tab, text=T("客户端连接"), padding=10)
        connect.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        connect.columnconfigure(1, weight=1)
        ttk.Label(connect, text=T("接口地址")).grid(row=0, column=0, sticky="w",
                                                padx=(0, 8))
        ttk.Entry(connect, textvariable=self.connection_var, state="readonly").grid(
            row=0, column=1, sticky="ew", pady=3)
        copy_endpoint_btn = ttk.Button(connect, text=T("复制"))
        copy_endpoint_btn.configure(
            command=lambda b=copy_endpoint_btn: self.copy_text(
                self.connection_var.get(), b))
        copy_endpoint_btn.grid(row=0, column=2, padx=(6, 0))

        self.clients_canvas = tk.Canvas(connect, bg=BG, highlightthickness=0,
                                        height=96)
        self.clients_canvas.grid(row=1, column=0, columnspan=3, sticky="ew",
                                 pady=(8, 0))
        self.clients_scroll = ttk.Scrollbar(
            connect, orient="vertical", command=self.clients_canvas.yview)
        self.clients_scroll.grid(row=1, column=3, sticky="ns", pady=(8, 0))
        self.clients_frame = ttk.Frame(self.clients_canvas)
        self.clients_window = self.clients_canvas.create_window(
            (0, 0), window=self.clients_frame, anchor="nw")
        self.clients_frame.columnconfigure(1, weight=1)
        self.clients_frame.bind("<Configure>", self._sync_clients_scroll)
        self.clients_canvas.bind("<Configure>",
                                 self._resize_clients_window)
        self.clients_canvas.configure(yscrollcommand=self.clients_scroll.set)
        self._render_clients()

        ttk.Button(connect, text=T("添加槽位"), command=self.add_client).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            connect, text=T("只暴露客户端选中的模型"),
            variable=self.expose_only_var,
            command=self._save_expose_only).grid(
            row=2, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Label(connect, text=T("重启模型后生效"),
                  style="Muted.TLabel").grid(
            row=2, column=2, columnspan=2, sticky="w", padx=(6, 0),
            pady=(8, 0))

        profiles = ttk.LabelFrame(tab, text=T("模型配置"), padding=10)
        profiles.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        profiles.columnconfigure(0, weight=1)
        profiles.rowconfigure(0, weight=1)

        columns = ("name", "role", "model", "ctx", "kv", "vision",
                   "mtp", "vram")
        self.profile_tree = ttk.Treeview(profiles, columns=columns,
                                         show="headings", height=9)
        headings = {
            "name": T("配置名"), "role": T("角色"), "model": T("模型"),
            "ctx": T("上下文"), "kv": "KV", "vision": T("视觉"),
            "mtp": "MTP", "vram": T("预计显存"),
        }
        widths = {"name": 200, "role": 100, "model": 230, "ctx": 95,
                  "kv": 95, "vision": 60, "mtp": 55, "vram": 90}
        for column in columns:
            self.profile_tree.heading(column, text=headings[column])
            self.profile_tree.column(column, width=widths[column],
                                     anchor="w" if column != "vram" else "e",
                                     stretch=column == "model")
        self.profile_tree.grid(row=0, column=0, sticky="nsew")
        self.profile_tree.tag_configure("over", foreground=RED)
        self.profile_tree.bind("<Double-1>", lambda _e: self.edit_profile())
        self.profile_tree.bind("<<TreeviewSelect>>", self._on_profile_select)
        scrollbar = ttk.Scrollbar(profiles, orient="vertical",
                                  command=self.profile_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.profile_tree.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(profiles)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        import_row = ttk.Frame(buttons)
        import_row.pack(fill="x")
        ttk.Button(import_row, text=T("批量导入文件…"),
                   command=self.import_files).pack(side="left")
        ttk.Button(import_row, text=T("导入整个目录…"),
                   command=self.import_directory).pack(side="left", padx=(6, 0))
        ttk.Button(import_row, text=T("重新扫描"),
                   command=lambda: self.refresh_models()).pack(side="right")
        edit_row = ttk.Frame(buttons)
        edit_row.pack(fill="x", pady=(6, 0))
        ttk.Button(edit_row, text=T("新建…"), command=self.new_profile).pack(
            side="left")
        ttk.Button(edit_row, text=T("编辑…"), command=self.edit_profile).pack(
            side="left", padx=(6, 0))
        ttk.Button(edit_row, text=T("复制"), command=self.duplicate_profile).pack(
            side="left", padx=(6, 0))
        ttk.Button(edit_row, text=T("打开目录"),
                   command=self.open_model_dir).pack(side="left", padx=(6, 0))
        ttk.Button(edit_row, text=T("删除"), command=self.delete_profile).pack(
            side="left", padx=(6, 0))

        self._on_mode_change()
        self.refresh_server_builds()

    def _build_services_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=T("插件"))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.service_cards: list[dict] = []

        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        tk.Label(head, text=T("本机插件"), bg=BG, fg=ACCENT,
                 font=(FONT_UI, 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(head, text=T("检测到的本地服务，点击卡片即可启动"),
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w",
                                             pady=(2, 0))
        actions = ttk.Frame(head)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(actions, text=T("重新检测"),
                   command=self.rescan_services).pack(side="left")
        ttk.Button(actions, text=T("添加服务…"),
                   command=self.add_service).pack(side="left", padx=(6, 0))

        area = tk.Frame(tab, bg=BG)
        area.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.services_canvas = tk.Canvas(area, bg=BG, highlightthickness=0,
                                         bd=0)
        self.services_canvas.grid(row=0, column=0, sticky="nsew")
        self.services_scroll = ttk.Scrollbar(
            area, orient="vertical", command=self.services_canvas.yview)
        self.services_scroll.grid(row=0, column=1, sticky="ns")
        self.services_canvas.configure(
            yscrollcommand=self.services_scroll.set)
        self.services_frame = tk.Frame(self.services_canvas, bg=BG)
        self.services_frame.columnconfigure(0, weight=1, uniform="service")
        self.services_frame.columnconfigure(1, weight=1, uniform="service")
        self.services_window = self.services_canvas.create_window(
            (0, 0), window=self.services_frame, anchor="nw")
        self.services_frame.bind("<Configure>", self._sync_services_scroll)
        self.services_canvas.bind("<Configure>", self._resize_services_window)
        for widget in (self.services_canvas, self.services_frame):
            widget.bind("<MouseWheel>", self._scroll_services)

    def _sync_services_scroll(self, _event=None) -> None:
        bbox = self.services_canvas.bbox("all")
        self.services_canvas.configure(scrollregion=bbox)
        try:
            needed = bool(bbox) and bbox[3] > self.services_canvas.winfo_height()
            if needed:
                self.services_scroll.grid()
            else:
                self.services_scroll.grid_remove()
        except Exception:
            pass

    def _resize_services_window(self, event) -> None:
        self.services_canvas.itemconfigure(self.services_window, width=event.width)
        self._sync_services_scroll()

    def _scroll_services(self, event) -> str:
        try:
            self.services_canvas.yview_scroll(
                int(-event.delta / 120) or (-1 if event.delta > 0 else 1),
                "units")
        except Exception:
            pass
        return "break"

    def _bind_service_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_services)
        for child in widget.winfo_children():
            self._bind_service_wheel(child)

    def _theme_label(self, name: str) -> str:
        return T(name) if name in BUILTIN_THEMES else str(name)

    def _build_appearance_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=T("外观"))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        theme_frame = ttk.LabelFrame(tab, text=T("主题"), padding=10)
        theme_frame.grid(row=0, column=0, sticky="ew")
        theme_frame.columnconfigure(1, weight=1)
        ttk.Label(theme_frame, text=T("主题")).grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.theme_combo = ttk.Combobox(
            theme_frame, textvariable=self.theme_var, state="readonly",
            values=list(self.theme_display_map))
        self.theme_combo.grid(row=0, column=1, sticky="ew")
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)
        theme_buttons = ttk.Frame(theme_frame)
        theme_buttons.grid(row=1, column=0, columnspan=2, sticky="w",
                           pady=(8, 0))
        ttk.Button(theme_buttons, text=T("导入主题…"),
                   command=self.import_theme).pack(side="left")
        ttk.Button(theme_buttons, text=T("打开主题目录"),
                   command=self.open_theme_dir).pack(side="left", padx=(6, 0))
        ttk.Button(theme_buttons, text=T("保存主题…"),
                   command=self.save_theme).pack(side="left", padx=(6, 0))

        colors = ttk.LabelFrame(tab, text=T("颜色"), padding=10)
        colors.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        labels = {
            "bg": T("背景"), "card": T("卡片"), "field": T("输入框"),
            "fg": T("正文"), "muted": T("次要文字"), "accent": T("强调色"),
            "danger": T("警告色"), "success": T("成功色"),
            "border": T("边框"), "log_bg": T("日志背景"),
            "log_fg": T("日志文字"),
        }
        for index, key in enumerate(THEME_KEYS):
            block, row = divmod(index, 6)
            column = block * 4
            ttk.Label(colors, text=labels[key]).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(colors, textvariable=self.color_vars[key],
                      width=12).grid(row=row, column=column + 1,
                                     sticky="w", pady=3)
            swatch = tk.Label(colors, bg=self.color_vars[key].get() or FIELD,
                              width=3, relief="flat")
            swatch.grid(row=row, column=column + 2, padx=(8, 8))
            self.color_swatches[key] = swatch
            self.color_vars[key].trace_add(
                "write", lambda *_a, k=key: self._update_swatch(k))
            ttk.Button(colors, text=T("选择…"),
                       command=lambda k=key: self.choose_color(k)).grid(
                row=row, column=column + 3, sticky="w", pady=3)

        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text=T("应用颜色"), style="Accent.TButton",
                   command=self.apply_colors).pack(side="left")
        ttk.Button(actions, text=T("重置为当前主题"),
                   command=self.reset_colors).pack(side="left", padx=(8, 0))

    def _on_theme_selected(self, _event=None) -> None:
        name = self.theme_display_map.get(self.theme_var.get())
        if not name:
            return
        self.theme_name = name
        self.theme = dict(self.themes[name])
        ui = self.cfg.data.setdefault("ui", {})
        ui["theme"] = name
        ui["colors"] = {}
        self.cfg.save()
        self.after_idle(self._rebuild_ui)

    def _update_swatch(self, key: str) -> None:
        swatch = self.color_swatches.get(key)
        value = self.color_vars[key].get().strip()
        if swatch is not None and _is_color(value):
            swatch.configure(bg=value)

    def choose_color(self, key: str) -> None:
        _rgb, value = colorchooser.askcolor(
            color=self.color_vars[key].get() or FIELD,
            parent=self, title=T("选择颜色"))
        if value:
            self.color_vars[key].set(value)
            swatch = self.color_swatches.get(key)
            if swatch is not None:
                swatch.configure(bg=value)

    def _colors_from_ui(self) -> dict | None:
        colors = {}
        for key, var in self.color_vars.items():
            value = var.get().strip()
            if not _is_color(value):
                messagebox.showerror(T("无法保存"), T("颜色格式必须像 #aabbcc。"))
                return None
            colors[key] = value
        return colors

    def apply_colors(self) -> None:
        colors = self._colors_from_ui()
        if colors is None:
            return
        ui = self.cfg.data.setdefault("ui", {})
        ui["theme"] = self.theme_name
        ui["colors"] = colors
        self.cfg.save()
        self.theme = dict(self.themes.get(self.theme_name,
                                          BUILTIN_THEMES["暖夜"]))
        self.theme.update(colors)
        self.theme["dark"] = _luminance(colors["bg"]) < 0.5
        self.after_idle(self._rebuild_ui)

    def reset_colors(self) -> None:
        ui = self.cfg.data.setdefault("ui", {})
        ui["theme"] = self.theme_name
        ui["colors"] = {}
        self.cfg.save()
        self.theme = dict(self.themes.get(self.theme_name,
                                          BUILTIN_THEMES["暖夜"]))
        self.after_idle(self._rebuild_ui)

    def open_theme_dir(self) -> None:
        folder = self.cfg.base / "themes"
        folder.mkdir(parents=True, exist_ok=True)
        self.open_path(folder)

    def _write_theme(self, theme: dict) -> Path:
        folder = self.cfg.base / "themes"
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w\-]+", "-", theme["name"], flags=re.UNICODE).strip("-")
        path = folder / f"{stem or 'custom'}.json"
        payload = {key: theme[key] for key in
                   ("name", "dark") + THEME_KEYS}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def ask_theme_name(self) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.withdraw()
        dialog.title(T("保存主题"))
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=T("主题名称：")).pack(anchor="w")
        value = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=value, width=34)
        entry.pack(fill="x", pady=(6, 0))
        result = {"value": None}

        def accept():
            result["value"] = value.get().strip()
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text=T("取消"),
                   command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text=T("保存"), style="Accent.TButton",
                   command=accept).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", lambda _e: accept())
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            dialog.attributes("-alpha", 0.0)
        except Exception:
            pass
        apply_dark_window_frame(dialog)
        dialog.deiconify()
        dialog.lift()
        dialog.grab_set()
        entry.focus_set()

        def fade(step: int) -> None:
            try:
                alpha = min(1.0, step * 0.12)
                dialog.attributes("-alpha", alpha)
                if alpha < 1.0:
                    dialog.after(16, lambda: fade(step + 1))
            except Exception:
                pass

        dialog.after(16, lambda: fade(1))
        self.wait_window(dialog)
        return result["value"] or None

    def save_theme(self) -> None:
        name = self.ask_theme_name()
        if not name or not name.strip():
            return
        colors = self._colors_from_ui()
        if colors is None:
            return
        theme = dict(self.theme)
        theme.update(colors)
        theme["name"] = name.strip()
        theme["dark"] = _luminance(colors["bg"]) < 0.5
        try:
            self._write_theme(theme)
        except Exception as exc:
            messagebox.showerror(T("无法保存"), str(exc))
            return
        self.themes = load_themes(APP_DIR, self.cfg.base)
        self.theme_name = theme["name"]
        self.theme = dict(self.themes.get(self.theme_name, theme))
        ui = self.cfg.data.setdefault("ui", {})
        ui["theme"] = self.theme_name
        ui["colors"] = {}
        self.cfg.save()
        self.after_idle(self._rebuild_ui)

    def import_theme(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=T("导入主题"),
            filetypes=[("Theme files", "*.json *.ini *.cfg"),
                       ("JSON", "*.json"), ("INI", "*.ini *.cfg"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            theme = normalize_theme(read_theme_file(Path(path)))
            self._write_theme(theme)
        except Exception as exc:
            messagebox.showerror(T("导入失败"), str(exc))
            return
        self.themes = load_themes(APP_DIR, self.cfg.base)
        self.theme_name = theme["name"]
        self.theme = dict(self.themes.get(self.theme_name, theme))
        ui = self.cfg.data.setdefault("ui", {})
        ui["theme"] = self.theme_name
        ui["colors"] = {}
        self.cfg.save()
        self.after_idle(self._rebuild_ui)

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=T("设置"))
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="llama-server").grid(row=0, column=0, sticky="w",
                                                 padx=(0, 10), pady=5)
        ttk.Entry(tab, textvariable=self.server_path_var).grid(
            row=0, column=1, sticky="ew", pady=5)
        ttk.Button(tab, text=T("浏览…"), command=self.browse_server).grid(
            row=0, column=2, padx=(6, 0), pady=5)

        ttk.Label(tab, text=T("扫描目录")).grid(row=1, column=0, sticky="nw",
                                             padx=(0, 10), pady=5)
        self.scan_list = tk.Listbox(
            tab, bg=FIELD, fg=FG,
            selectbackground=blend(ACCENT, FIELD, 0.45),
            relief="flat", height=5, font=(FONT_MONO, 10))
        self.scan_list.grid(row=1, column=1, sticky="ew", pady=5)
        self._refresh_scan_list()
        scan_buttons = ttk.Frame(tab)
        scan_buttons.grid(row=1, column=2, sticky="n", padx=(6, 0), pady=5)
        ttk.Button(scan_buttons, text=T("添加…"), command=self.add_scan_dir).pack(
            fill="x")
        ttk.Button(scan_buttons, text=T("移除"), command=self.remove_scan_dir).pack(
            fill="x", pady=(6, 0))

        ttk.Label(tab, text=T("显存 GB")).grid(row=4, column=0, sticky="w",
                                            padx=(0, 10), pady=5)
        ttk.Entry(tab, textvariable=self.vram_var, width=10).grid(
            row=4, column=1, sticky="w", pady=5)

        ttk.Label(tab, text=T("系统预留 GB")).grid(row=5, column=0, sticky="w",
                                                padx=(0, 10), pady=5)
        ttk.Entry(tab, textvariable=self.vram_reserve_var, width=10).grid(
            row=5, column=1, sticky="w", pady=5)
        ttk.Label(tab, text=T("桌面/浏览器等占用，默认 0.95"),
                  style="Muted.TLabel").grid(
            row=5, column=2, sticky="w", padx=(6, 0), pady=5)

        ttk.Label(tab, text=T("API Key（可选）")).grid(row=6, column=0, sticky="w",
                                                    padx=(0, 10), pady=5)
        self.api_key_var = tk.StringVar(value=self.cfg.data.get("api_key", ""))
        ttk.Entry(tab, textvariable=self.api_key_var, show="*").grid(
            row=6, column=1, sticky="ew", pady=5)

        actions = ttk.Frame(tab)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        ttk.Button(actions, text=T("保存设置"), style="Accent.TButton",
                   command=self.save_settings).pack(side="left")
        ttk.Button(actions, text=T("打开配置目录"),
                   command=lambda: self.open_path(self.cfg.base)).pack(
            side="left", padx=(8, 0))
        ttk.Button(actions, text=T("导出配置…"), command=self.export_config).pack(
            side="left", padx=(8, 0))
        ttk.Button(actions, text=T("导入配置…"), command=self.import_config).pack(
            side="left", padx=(6, 0))

        self.config_path_label = ttk.Label(
            tab, text=f"{self.cfg.path}   |   {APP_VERSION}",
            style="Muted.TLabel")
        self.config_path_label.grid(row=8, column=0, columnspan=3, sticky="w",
                                    pady=(14, 0))

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=T("日志"))
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text=T("打开日志文件"), command=self.open_log).pack(side="left")
        ttk.Button(toolbar, text=T("清空显示"),
                   command=lambda: self.log_text.delete("1.0", "end")).pack(
            side="left", padx=(6, 0))
        self.log_text = tk.Text(tab, bg=LOG_BG, fg=LOG_FG,
                                insertbackground=FG,
                                selectbackground=blend(ACCENT, LOG_BG, 0.45),
                                relief="flat", wrap="none", font=(FONT_MONO, 10))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tab, orient="vertical",
                                  command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set, state="disabled")

    # ---------- helpers ----------

    def _port(self) -> int:
        try:
            return int(str(self.port_var.get()).strip())
        except Exception:
            return 8081

    def display_for(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.cfg.base)
        except Exception:
            relative = path
        try:
            size = human_size(path.stat().st_size)
        except Exception:
            size = "?"
        return f"{relative.as_posix()}  ({size})"

    def _refresh_scan_list(self) -> None:
        self.scan_list.delete(0, "end")
        for item in self.cfg.data.get("scan_dirs", []):
            self.scan_list.insert("end", item)

    def _browse_into(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            var.set(path)

    def _on_mode_change(self) -> None:
        router = self.mode_var.get() == "router"
        state = "normal" if router else "disabled"
        self.models_max_spin.configure(state=state)
        self.models_max_label.configure(foreground=FG if router else MUTED)
        self.run_hint.configure(
            text=T("启动路由器；模型按请求加载") if router else T("启动表格中选中的配置"))
        self._update_connection()

    def _update_connection(self) -> None:
        host = self.cfg.data.get("host", "127.0.0.1")
        self.connection_var.set(f"http://{host}:{self._port()}/v1")

    def _change_language(self, _event=None) -> None:
        if self.busy:
            return
        code = self.language_map.get(self.language_var.get(), "zh")
        self._collect_clients()
        if code == current_language():
            return
        self.cfg.data["language"] = code
        self.cfg.save()
        set_language(code)
        self.after_idle(self._rebuild_ui)

    def _rebuild_ui(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        self.status_busy = False
        self.last_log_text = ""
        self.client_rows = []
        apply_theme(self.theme)
        self.configure(bg=BG, highlightthickness=0, bd=0)
        self._setup_style()
        self._build_vars()
        self._build_ui()
        self.title(T("本地模型管理器"))
        self.refresh_models(initial=False)
        self.refresh_profiles()
        self.refresh_services()
        self.refresh_status(force=True)
        self.after(120, lambda: apply_dark_window_frame(self))

    def copy_text(self, text: str, button=None) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        if button is None:
            return
        try:
            original = button.cget("text")
            button.configure(text=T("已复制"))

            def restore():
                try:
                    if button.winfo_exists():
                        button.configure(text=original)
                except Exception:
                    pass

            self.after(900, restore)
        except Exception:
            pass

    def copy_endpoint(self) -> None:
        self.copy_text(self.connection_var.get())

    # ---------- model scanning / profiles ----------

    def refresh_models(self, initial: bool = False) -> None:
        dirs = [self.cfg.resolve(item) for item in self.cfg.data.get("scan_dirs", [])]
        models = scan_models(dirs)
        mmprojs = scan_mmproj(dirs)
        self.model_map = {self.display_for(path): path for path in models}
        self.mmproj_map = {self.display_for(path): path for path in mmprojs}
        if not initial:
            self.status_var.set(T("找到 {count} 个模型", count=len(models)))
            self.after(1500, lambda: self.refresh_status(force=True))
        self.refresh_profiles()
        self.refresh_services()
        self._update_connection()

    def sync_mtp_flags(self) -> None:
        """Keep the stored MTP flag in sync with the GGUF metadata."""
        changed = False
        for profile in self.cfg.data.get("profiles", {}).values():
            model = self.cfg.resolve(profile.get("model"))
            if not model.is_file():
                continue
            has_mtp = bool(get_kv_info(model).get("has_mtp"))
            if bool(profile.get("mtp")) != has_mtp:
                profile["mtp"] = has_mtp
                changed = True
            if "mtp_enabled" not in profile:
                profile["mtp_enabled"] = True
                changed = True
            if "mtp_draft_n" not in profile:
                profile["mtp_draft_n"] = 2
                changed = True
        if changed:
            self.cfg.save()

    def refresh_profiles(self) -> None:
        self.sync_mtp_flags()
        selected = self.selected_profile_name()
        for item in self.profile_tree.get_children():
            self.profile_tree.delete(item)
        profiles = self.cfg.data.get("profiles", {})
        clients = self._clients()
        vram = float(self.cfg.data.get("vram_gb") or 0)
        reserve = float(self.cfg.data.get("vram_reserve_gb",
                                           VRAM_RESERVE_GB))
        names = sorted(profiles.keys())
        for name in names:
            profile = profiles[name]
            role_names = [str(client.get("name", "")) for client in clients
                          if client.get("profile") == name]
            estimate = estimate_profile(profile, self.cfg.base, reserve)
            model = self.cfg.resolve(profile.get("model"))
            vision = T("有") if profile.get("mmproj") else T("无")
            if not profile_has_mtp(profile, self.cfg.base):
                mtp = T("无")
            elif profile_mtp_enabled(profile, self.cfg.base):
                mtp = T("开")
            else:
                mtp = T("关")
            if estimate:
                vram_text = f"{estimate['total']:.1f} GB"
                over = bool(vram and estimate["total"]
                            > max(0.0, vram - VRAM_SAFETY_GB))
            else:
                vram_text = T("文件缺失")
                over = True
            values = (name, " / ".join(role_names) or "-",
                      model.name if model.name else "-",
                      str(profile.get("ctx", 32768)),
                      f"{profile.get('kvk', 'f16')}/{profile.get('kvv', 'f16')}",
                      vision, mtp, vram_text)
            self.profile_tree.insert("", "end", iid=name, values=values,
                                     tags=("over",) if over else ())
        if selected and selected in profiles:
            self.profile_tree.selection_set(selected)
        elif names:
            last = str(self.cfg.data.get("last_profile") or "")
            self.profile_tree.selection_set(last if last in profiles else names[0])

        role_values = [self.no_role] + names
        for row in self.client_rows:
            row["combo"]["values"] = role_values
            if row["profile_var"].get() not in role_values:
                row["profile_var"].set(self.no_role)

    def selected_profile_name(self) -> str | None:
        selection = self.profile_tree.selection()
        return selection[0] if selection else None

    def _on_profile_select(self, _event=None) -> None:
        name = self.selected_profile_name()
        if name and self.cfg.data.get("last_profile") != name:
            self.cfg.data["last_profile"] = name
            self.cfg.save()

    def _profile(self, name: str) -> dict:
        return dict(self.cfg.data.get("profiles", {}).get(name, {}))

    def _clients(self) -> list[dict]:
        clients = self.cfg.data.setdefault("clients", [])
        if not clients:
            clients.extend([
                {"name": "Chat", "profile": ""},
                {"name": "Agent", "profile": ""},
            ])
        return clients

    def _collect_clients(self) -> None:
        clients = []
        for index, row in enumerate(self.client_rows):
            name = row["name_var"].get().strip() or f"Client {index + 1}"
            profile = row["profile_var"].get()
            clients.append({
                "name": name,
                "profile": "" if profile == self.no_role else profile,
            })
        if clients:
            self.cfg.data["clients"] = clients

    def _sync_clients_scroll(self, _event=None) -> None:
        self.clients_canvas.configure(scrollregion=self.clients_canvas.bbox("all"))

    def _resize_clients_window(self, event) -> None:
        self.clients_canvas.itemconfigure(self.clients_window, width=event.width)

    def _render_clients(self) -> None:
        for child in self.clients_frame.winfo_children():
            child.destroy()
        self.client_rows = []
        profiles = sorted(self.cfg.data.get("profiles", {}))
        for index, client in enumerate(self._clients()):
            name_var = tk.StringVar(value=client.get("name", ""))
            profile_var = tk.StringVar(
                value=client.get("profile") or self.no_role)
            name_entry = ttk.Entry(self.clients_frame, textvariable=name_var,
                                   width=22)
            name_entry.grid(row=index, column=0, sticky="ew", pady=2,
                            padx=(0, 6))
            combo = ttk.Combobox(self.clients_frame, textvariable=profile_var,
                                 state="readonly", values=[self.no_role] + profiles)
            combo.grid(row=index, column=1, sticky="ew", pady=2, padx=(0, 6))
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, i=index: self._client_profile_changed(i))
            copy_button = ttk.Button(self.clients_frame, text=T("复制模型名"))
            copy_button.configure(
                command=lambda v=profile_var, b=copy_button: self.copy_text(v.get(), b))
            copy_button.grid(row=index, column=2, padx=(0, 6), pady=2)
            ttk.Button(self.clients_frame, text=T("移除"),
                       command=lambda i=index: self.remove_client(i)).grid(
                row=index, column=3, pady=2)
            name_var.trace_add("write",
                               lambda *_a, i=index: self._client_name_changed(i))
            self.client_rows.append({
                "name_var": name_var,
                "profile_var": profile_var,
                "combo": combo,
            })
        self.clients_frame.update_idletasks()
        required = self.clients_frame.winfo_reqheight()
        maximum = 4 * 38 + 6
        self.clients_canvas.configure(height=max(40, min(required, maximum)))
        if required > maximum:
            self.clients_scroll.grid()
        else:
            self.clients_scroll.grid_remove()
        self._sync_clients_scroll()

    def _save_expose_only(self) -> None:
        self.cfg.data["expose_assigned_only"] = bool(self.expose_only_var.get())
        self.cfg.save()

    def _client_name_changed(self, index: int) -> None:
        clients = self._clients()
        if 0 <= index < len(clients):
            clients[index]["name"] = self.client_rows[index]["name_var"].get().strip()

    def _client_profile_changed(self, index: int) -> None:
        self._collect_clients()
        self.cfg.save()
        self.refresh_profiles()

    def add_client(self) -> None:
        self._collect_clients()
        clients = self._clients()
        number = len(clients) + 1
        clients.append({"name": f"{T('客户端')} {number}", "profile": ""})
        self.cfg.save()
        self._render_clients()
        self.refresh_profiles()

    def remove_client(self, index: int) -> None:
        self._collect_clients()
        clients = self._clients()
        if 0 <= index < len(clients):
            clients.pop(index)
        self.cfg.save()
        self._render_clients()
        self.refresh_profiles()

    def _profile_from_model(self, model: Path, mmprojs: list[Path]) -> dict:
        defaults = self.cfg.data.get("defaults", {})
        mmproj = auto_match_mmproj(model, mmprojs)
        return {
            "model": self.cfg.portable(model),
            "mmproj": self.cfg.portable(mmproj) if mmproj else "",
            "ctx": int(defaults.get("ctx", 32768)),
            "kvk": defaults.get("kvk", "f16"),
            "kvv": defaults.get("kvv", "f16"),
            "ngl": int(defaults.get("ngl", 999)),
            "batch": int(defaults.get("batch", 2048)),
            "ubatch": int(defaults.get("ubatch", 512)),
            "parallel": int(defaults.get("parallel", 1)),
            "vision_cpu": bool(defaults.get("vision_cpu", True)),
            "mtmd_batch": int(defaults.get("mtmd_batch", 256)),
            "extra": defaults.get("extra", ""),
            "mtp": bool(get_kv_info(model).get("has_mtp", False)),
            "mtp_enabled": True,
            "mtp_draft_n": 2,
            "load_mode": defaults.get("load_mode", "auto"),
        }

    def _import_models(self, models: list[Path]) -> None:
        profiles = self.cfg.data.setdefault("profiles", {})
        mmprojs = list(self.mmproj_map.values())
        added = 0
        names: list[str] = []
        for model in models:
            if model.suffix.lower() != ".gguf" or "mmproj" in model.name.lower():
                continue
            base_name = sanitize_alias(model.stem)
            name = unique_name(base_name, profiles)
            profiles[name] = self._profile_from_model(model, mmprojs)
            names.append(name)
            added += 1

        if not added:
            messagebox.showinfo(T("批量导入"), T("没有找到可导入的 GGUF 模型。"))
            return

        clients = self._clients()
        for name in names:
            for client in clients:
                if not client.get("profile"):
                    client["profile"] = name
                    break
        self.cfg.save()
        self.refresh_profiles()
        self._render_clients()
        self.refresh_models()
        messagebox.showinfo(
            T("批量导入完成"),
            T("新增 {count} 个配置。\n\n", count=added)
            + "\n".join(f"· {n}" for n in names[:12])
            + ("\n…" if len(names) > 12 else "")
            + T("\n\n路由器模式需要重启模型服务才会生效。"))

    def import_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title=T("批量选择 GGUF 模型（可多选）"),
            filetypes=[("GGUF", "*.gguf"), ("All files", "*.*")])
        if paths:
            self._import_models([Path(item) for item in paths])

    def import_directory(self) -> None:
        directory = filedialog.askdirectory(parent=self, title=T("选择包含 GGUF 的目录"))
        if not directory:
            return
        models = scan_models([Path(directory)])
        if not models:
            messagebox.showinfo(T("导入目录"), T("这个目录里没有找到非 mmproj 的 GGUF。"))
            return
        self._import_models(models)

    def new_profile(self) -> None:
        defaults = self.cfg.data.get("defaults", {})
        profile = {
            "model": "",
            "mmproj": "",
            "ctx": int(defaults.get("ctx", 32768)),
            "kvk": defaults.get("kvk", "f16"),
            "kvv": defaults.get("kvv", "f16"),
            "ngl": int(defaults.get("ngl", 999)),
            "batch": int(defaults.get("batch", 2048)),
            "ubatch": int(defaults.get("ubatch", 512)),
            "parallel": int(defaults.get("parallel", 1)),
            "vision_cpu": bool(defaults.get("vision_cpu", True)),
            "mtmd_batch": int(defaults.get("mtmd_batch", 256)),
            "extra": defaults.get("extra", ""),
            "mtp": False,
        }
        dialog = ProfileDialog(self, T("新建模型配置"), "", profile)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        name, saved = dialog.result
        profiles = self.cfg.data.setdefault("profiles", {})
        profiles[name] = saved
        self.cfg.save()
        self.refresh_profiles()
        self.profile_tree.selection_set(name)

    def edit_profile(self) -> None:
        name = self.selected_profile_name()
        if not name:
            return
        dialog = ProfileDialog(self, T("编辑模型配置"), name, self._profile(name))
        self.wait_window(dialog)
        if dialog.result is None:
            return
        new_name, profile = dialog.result
        profiles = self.cfg.data.setdefault("profiles", {})
        profiles.pop(name, None)
        profiles[new_name] = profile
        for client in self._clients():
            if client.get("profile") == name:
                client["profile"] = new_name
        self.cfg.save()
        self.refresh_profiles()
        self._render_clients()
        self.profile_tree.selection_set(new_name)

    def duplicate_profile(self) -> None:
        name = self.selected_profile_name()
        if not name:
            return
        profiles = self.cfg.data.setdefault("profiles", {})
        new_name = unique_name(f"{name}-copy", profiles)
        profiles[new_name] = dict(profiles[name])
        self.cfg.save()
        self.refresh_profiles()
        self.profile_tree.selection_set(new_name)

    def delete_profile(self) -> None:
        name = self.selected_profile_name()
        if not name:
            return
        if not messagebox.askyesno(T("删除配置"), T("确定删除“{name}”吗？\n不会删除模型文件。", name=name)):
            return
        self.cfg.data.get("profiles", {}).pop(name, None)
        for client in self._clients():
            if client.get("profile") == name:
                client["profile"] = ""
        self.cfg.save()
        self.refresh_profiles()
        self._render_clients()

    # ---------- settings ----------

    def refresh_server_builds(self) -> None:
        current_text = self.server_path_var.get().strip()
        current = self.cfg.resolve(current_text) if current_text else None
        items = detect_server_builds(
            self.cfg.base, current, self.cfg.data.get("server_builds", []))
        self.build_map = {label: path for label, path in items}
        if hasattr(self, "server_build_combo"):
            self.server_build_combo["values"] = list(self.build_map)
        selected = None
        for label, path in items:
            if current is not None and path == current:
                selected = label
                break
        if selected:
            self.server_build_var.set(selected)
        elif items:
            self.server_build_var.set(items[0][0])
        else:
            self.server_build_var.set("")

    def _on_build_selected(self, _event=None) -> None:
        path = self.build_map.get(self.server_build_var.get())
        if path is None:
            return
        portable = self.cfg.portable(path)
        self.server_path_var.set(str(path))
        builds = self.cfg.data.setdefault("server_builds", [])
        if portable not in builds:
            builds.append(portable)
        self.cfg.data["server_path"] = portable
        self.cfg.save()

    def browse_server(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=T("选择 llama-server"),
            filetypes=[("llama-server", "llama-server*"),
                       ("All files", "*.*")])
        if path:
            self.server_path_var.set(path)
            self.refresh_server_builds()

    def add_scan_dir(self) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            dirs = self.cfg.data.setdefault("scan_dirs", [])
            portable = self.cfg.portable(Path(path))
            if portable not in dirs:
                dirs.append(portable)
                self.cfg.save()
                self._refresh_scan_list()
                self.refresh_models()

    def remove_scan_dir(self) -> None:
        selection = self.scan_list.curselection()
        if not selection:
            return
        dirs = self.cfg.data.setdefault("scan_dirs", [])
        index = selection[0]
        if 0 <= index < len(dirs):
            dirs.pop(index)
            self.cfg.save()
            self._refresh_scan_list()
            self.refresh_models()

    def save_settings(self) -> None:
        server = Path(self.server_path_var.get().strip())
        if server and not server.is_file():
            messagebox.showerror(T("无法保存"), T("找不到 llama-server：\n{server}", server=server))
            return
        self.cfg.data["server_path"] = self.cfg.portable(server) if server else ""
        if server:
            portable_server = self.cfg.portable(server)
            builds = self.cfg.data.setdefault("server_builds", [])
            if portable_server not in builds:
                builds.append(portable_server)
        try:
            self.cfg.data["vram_gb"] = float(self.vram_var.get())
        except Exception:
            self.cfg.data["vram_gb"] = 0
        try:
            reserve = float(self.vram_reserve_var.get())
            self.cfg.data["vram_reserve_gb"] = max(0.0, min(4.0, reserve))
        except Exception:
            self.cfg.data["vram_reserve_gb"] = VRAM_RESERVE_GB
        self.cfg.data["api_key"] = self.api_key_var.get().strip()
        self.cfg.save()
        messagebox.showinfo(T("设置"), T("已保存。修改服务器程序或 API Key 后请重启模型服务。"))
        self.refresh_server_builds()
        self.refresh_profiles()

    def export_config(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title=T("导出配置"), defaultextension=".json",
            initialfile="model-manager-config.json",
            filetypes=[("JSON", "*.json")])
        if path:
            try:
                Path(path).write_text(
                    json.dumps(self.cfg.data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                messagebox.showinfo(T("导出成功"), path)
            except Exception as exc:
                messagebox.showerror(T("导出失败"), str(exc))

    def import_config(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=T("导入配置"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(T("配置必须是 JSON 对象。"))
        except Exception as exc:
            messagebox.showerror(T("导入失败"), str(exc))
            return
        if not messagebox.askyesno(T("导入配置"), T("导入会覆盖当前配置（不会删除模型文件）。继续吗？")):
            return
        self.cfg.data = deep_merge(default_config(), migrate_v1(data, self.cfg.base))
        self.cfg._fill_defaults()
        self.cfg.save()
        messagebox.showinfo(T("导入成功"), T("配置已导入，建议重新打开管理器让所有路径生效。"))
        self.refresh_profiles()
        self.refresh_services()

    # ---------- server control ----------

    def _profile_for_start(self) -> tuple[str, dict] | None:
        name = self.selected_profile_name()
        if not name:
            messagebox.showerror(T("无法启动"), T("请先在表格里选择一个配置。"))
            return None
        profile = self._profile(name)
        model = self.cfg.resolve(profile.get("model"))
        if not model.is_file():
            messagebox.showerror(T("无法启动"), T("模型文件不存在：\n{model}", model=model))
            return None
        return name, profile

    def _validate_server(self) -> bool:
        server = self.cfg.resolve(self.cfg.data.get("server_path"))
        if not server.is_file():
            messagebox.showerror(T("无法启动"),
                                 T("找不到 llama-server。\n请到“设置”页选择可执行文件。"))
            return False
        return True

    def start_server(self) -> None:
        if self.busy or not self._validate_server():
            return
        mode = self.mode_var.get()
        port = self._port()
        if not (1 <= port <= 65535):
            messagebox.showerror(T("无法启动"), T("端口必须在 1–65535 之间。"))
            return

        preset = None
        if mode == "router":
            if not self.cfg.data.get("profiles"):
                messagebox.showerror(T("无法启动"), T("还没有模型配置。请先批量导入。"))
                return
            preset = self.cfg.preset_path()
            text, missing = router_preset_text(self.cfg)
            preset.parent.mkdir(parents=True, exist_ok=True)
            preset.write_text(text, encoding="utf-8")
            if missing and len(missing) == len(self.cfg.data.get("profiles", {})):
                messagebox.showerror(T("无法启动"), T("所有配置的模型文件都不存在。"))
                return
            if missing and not messagebox.askyesno(
                    T("部分配置不可用"),
                    T("以下配置的模型文件不存在，会被跳过：\n\n{names}\n\n仍要启动吗？",
                      names="\n".join(missing))):
                return
            cmd = build_router_command(self.cfg, preset)
        else:
            result = self._profile_for_start()
            if result is None:
                return
            name, profile = result
            self._confirm_fit(name, profile)
            cmd = build_single_command(self.cfg, name, profile)

        listener = find_listener_pid(port)
        if listener:
            name = process_name(listener)
            if "llama" not in name.lower():
                messagebox.showerror(
                    T("端口被占用"),
                    T("端口 {port} 被 {name} (PID {pid}) 占用。\n"
                      "为避免误杀其他程序，管理器不会启动。",
                      port=port, name=name or T("未知进程"), pid=listener))
                return
            if not messagebox.askyesno(
                    T("已有模型服务"),
                    T("端口 {port} 上已有 llama-server（PID {pid}）。\n"
                      "先停止它，再启动当前配置吗？",
                      port=port, pid=listener)):
                return

        self._persist_runtime_settings()
        self._set_busy(True)
        self.status_var.set(T("启动中…"))
        self.status_dot.configure(fg=MUTED)

        def work():
            try:
                if find_listener_pid(port):
                    ok, message = self.server.stop(port)
                    if not ok:
                        return False, message
                    for _ in range(30):
                        if not is_port_open(port):
                            break
                        time.sleep(0.2)
                if is_port_open(port):
                    return False, T("端口 {port} 仍被占用，未启动。", port=port)
                pid = self.server.start(cmd, cwd=self.cfg.base)
                time.sleep(1.2)
                if self.server.proc is not None and self.server.proc.poll() is not None:
                    tail = tail_file(self.cfg.server_log(), 30)
                    return False, T("llama-server 启动后立即退出。\n\n{tail}", tail=tail)
                self.cfg.save()
                return True, T("已启动 PID {pid}，等待模型加载…", pid=pid)
            except Exception as exc:
                log("启动失败:\n" + traceback.format_exc())
                return False, str(exc)

        self.last_action = "start"
        self._run_worker(work)

    def _confirm_fit(self, name: str, profile: dict) -> bool:
        reserve = float(self.cfg.data.get("vram_reserve_gb",
                                           VRAM_RESERVE_GB))
        estimate = estimate_profile(profile, self.cfg.base, reserve)
        if estimate is None:
            return True
        vram = float(self.cfg.data.get("vram_gb") or 0)
        if vram and estimate["total"] > max(0.0, vram - VRAM_SAFETY_GB):
            return messagebox.askyesno(
                T("显存可能不够"),
                T("配置“{name}”预计占用 {total:.2f} GB，当前显存设置 {vram:.1f} GB。\n\n"
                  "继续启动可能触发 CPU offload，速度会明显变慢。仍要启动吗？",
                  name=name, total=estimate["total"], vram=vram))
        return True

    def _persist_runtime_settings(self) -> None:
        self._collect_clients()
        self.cfg.data["mode"] = self.mode_var.get()
        self.cfg.data["port"] = self._port()
        try:
            self.cfg.data["models_max"] = max(1, int(self.models_max_var.get()))
        except Exception:
            self.cfg.data["models_max"] = 1
        self.cfg.data["server_path"] = self.cfg.portable(
            Path(self.server_path_var.get().strip()))
        self.cfg.save()
        self._update_connection()

    def stop_server(self) -> None:
        if self.busy:
            return
        port = self._port()
        if not messagebox.askyesno(T("停止模型"), T("确定停止端口 {port} 上的模型服务吗？", port=port)):
            return
        self._set_busy(True)

        def work():
            try:
                return self.server.stop(port)
            except Exception as exc:
                log("停止失败:\n" + traceback.format_exc())
                return False, str(exc)

        self.last_action = "stop"
        self._run_worker(work)

    def restart_server(self) -> None:
        if self.busy or not self._validate_server():
            return
        mode = self.mode_var.get()
        port = self._port()
        if not (1 <= port <= 65535):
            messagebox.showerror(T("无法重启"), T("端口必须在 1–65535 之间。"))
            return
        if mode == "router":
            if not self.cfg.data.get("profiles"):
                messagebox.showerror(T("无法重启"), T("还没有模型配置。请先批量导入。"))
                return
            preset = self.cfg.preset_path()
            text, missing = router_preset_text(self.cfg)
            preset.parent.mkdir(parents=True, exist_ok=True)
            preset.write_text(text, encoding="utf-8")
            if missing and len(missing) == len(self.cfg.data.get("profiles", {})):
                messagebox.showerror(T("无法重启"), T("所有配置的模型文件都不存在。"))
                return
            if missing and not messagebox.askyesno(
                    T("部分配置不可用"),
                    T("以下配置的模型文件不存在，会被跳过：\n\n{names}\n\n仍要重启吗？",
                      names="\n".join(missing))):
                return
            cmd = build_router_command(self.cfg, preset)
        else:
            result = self._profile_for_start()
            if result is None:
                return
            name, profile = result
            if not self._confirm_fit(name, profile):
                return
            cmd = build_single_command(self.cfg, name, profile)

        self._persist_runtime_settings()
        self._set_busy(True)
        self.status_var.set(T("重启中…"))
        self.status_dot.configure(fg=MUTED)

        def work():
            try:
                ok, message = self.server.stop(port)
                if not ok:
                    return False, message
                for _ in range(30):
                    if not is_port_open(port):
                        break
                    time.sleep(0.2)
                if is_port_open(port):
                    return False, T("端口 {port} 仍被占用，未启动。", port=port)
                pid = self.server.start(cmd, cwd=self.cfg.base)
                time.sleep(1.2)
                if self.server.proc is not None and self.server.proc.poll() is not None:
                    tail = tail_file(self.cfg.server_log(), 30)
                    return False, T("llama-server 启动后立即退出。\n\n{tail}", tail=tail)
                return True, T("已重启 PID {pid}，等待模型加载…", pid=pid)
            except Exception as exc:
                log("重启失败:\n" + traceback.format_exc())
                return False, str(exc)

        self.last_action = "restart"
        self._run_worker(work)

    def _run_worker(self, fn) -> None:
        def runner():
            ok, message = fn()
            self.after(0, lambda: self._worker_done(ok, message))
        threading.Thread(target=runner, daemon=True).start()

    def _worker_done(self, ok: bool, message: str) -> None:
        self._set_busy(False)
        if ok:
            self.status_var.set(
                T("启动中…") if self.last_action in ("start", "restart")
                else T("已停止"))
            log(message)
        else:
            self.status_var.set(T("操作失败"))
            messagebox.showerror(T("操作失败"), message)
        self.refresh_status(force=True)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.start_btn, self.stop_btn, self.restart_btn):
            try:
                button.configure(state=state)
            except Exception:
                pass

    # ---------- status / log ----------

    def refresh_status(self, force: bool = False) -> None:
        try:
            while True:
                snapshot = self.status_queue.get_nowait()
                self._apply_status(snapshot)
        except queue.Empty:
            pass
        if not self.status_busy or force:
            self.status_busy = True
            port = self._port()

            def work():
                try:
                    pid = find_listener_pid(port)
                    name = process_name(pid) if pid else ""
                    api_key = str(self.cfg.data.get("api_key") or "")
                    health = http_json(f"http://127.0.0.1:{port}/health",
                                       timeout=1.2, api_key=api_key) if pid else None
                    models = http_json(f"http://127.0.0.1:{port}/v1/models",
                                       timeout=1.2, api_key=api_key) if pid else None
                    services = {}
                    service_status = []
                    for service in self.cfg.data.get("services") or []:
                        try:
                            service_port = int(service.get("port") or 0)
                        except Exception:
                            service_port = 0
                        open_now = is_port_open(service_port) if service_port else False
                        service_status.append(open_now)
                        name_key = service.get("name")
                        if name_key and name_key not in services:
                            services[name_key] = open_now
                    snapshot = {
                        "port": port, "pid": pid, "name": name,
                        "health": health, "models": models,
                        "services": services, "service_status": service_status,
                        "log": tail_file(self.cfg.server_log()),
                    }
                except Exception as exc:
                    snapshot = {"port": port, "pid": None, "name": "", "health": None,
                                "models": None, "services": {},
                                "service_status": [], "log": "",
                                "error": str(exc)}
                self.status_queue.put(snapshot)

            threading.Thread(target=work, daemon=True).start()
        self.after(1200, self.refresh_status)

    def _apply_status(self, snapshot: dict) -> None:
        self.status_busy = False
        if not snapshot:
            return
        port = snapshot.get("port", self._port())
        pid = snapshot.get("pid")
        self.current_pid = pid
        health = snapshot.get("health")
        if pid:
            name = snapshot.get("name") or "llama-server"
            if isinstance(health, dict) and health.get("status") == "ok":
                self.status_var.set(T("运行中"))
                self.status_dot.configure(fg=GREEN)
            elif isinstance(health, dict) and health.get("error"):
                self.status_var.set(T("加载中"))
                self.status_dot.configure(fg=MUTED)
            else:
                self.status_var.set(T("已监听"))
                self.status_dot.configure(fg=ACCENT)
            detail = T("端口 {port} | PID {pid} | {name}", port=port, pid=pid, name=name)
            models = snapshot.get("models")
            loaded = []
            if isinstance(models, dict):
                for item in models.get("data", []):
                    status = item.get("status") or {}
                    if isinstance(status, dict) and status.get("value") == "loaded":
                        loaded.append(str(item.get("id", "")))
            if loaded:
                detail += T(" | 已加载 {models}", models=", ".join(loaded))
            detail += f" | GPU {self.gpu_name}"
            self.detail_var.set(detail)
        elif self.server.managed_running():
            self.status_var.set(T("启动中…"))
            self.status_dot.configure(fg=MUTED)
            self.detail_var.set(T("端口 {port} | 进程 PID {pid} | 等待监听", port=port, pid=self.server.proc.pid))
        else:
            self.status_var.set(T("未运行"))
            self.status_dot.configure(fg=RED)
            self.detail_var.set(T("端口 {port} 空闲 | GPU {gpu}", port=port, gpu=self.gpu_name))
        statuses = snapshot.get("service_status")
        if not isinstance(statuses, list):
            statuses = [
                bool(snapshot.get("services", {}).get(service.get("name")))
                for service in self.cfg.data.get("services") or []
                if isinstance(service, dict)
            ]
        for card in getattr(self, "service_cards", []):
            index = card.get("index", -1)
            running = bool(statuses[index]) if 0 <= index < len(statuses) else False
            starting = index in self.service_starting
            if running:
                self.service_starting.discard(index)
                card["dot"].configure(fg=GREEN)
                card["status"].configure(text=T("运行中"), fg=MUTED)
                card["primary"].configure(text=T("打开"))
            elif starting:
                card["dot"].configure(fg=ACCENT)
                card["status"].configure(text=T("启动中…"), fg=MUTED)
                card["primary"].configure(text=T("启动"))
            else:
                card["dot"].configure(fg=MUTED)
                card["status"].configure(text=T("未运行"), fg=MUTED)
                card["primary"].configure(text=T("启动"))
        self._set_log_text(snapshot.get("log", ""))

    def _set_log_text(self, text: str) -> None:
        if text == self.last_log_text:
            return
        self.last_log_text = text
        at_bottom = True
        try:
            at_bottom = self.log_text.yview()[1] > 0.97
        except Exception:
            pass
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text or T("日志还没有生成。"))
        self.log_text.configure(state="disabled")
        if at_bottom:
            self.log_text.see("end")

    def open_log(self) -> None:
        path = self.cfg.server_log()
        if path.exists():
            self.open_path(path)
        else:
            messagebox.showinfo(T("日志"), T("日志文件还不存在：\n{path}", path=path))

    # ---------- service plugins ----------

    def _service_at(self, index: int | None) -> dict | None:
        services = self.cfg.data.get("services") or []
        if index is None or not (0 <= index < len(services)):
            return None
        service = services[index]
        return service if isinstance(service, dict) else None

    def _service_index_by_plugin(self, plugin: str) -> int | None:
        for index, service in enumerate(self.cfg.data.get("services") or []):
            if isinstance(service, dict) and service_plugin_id(service) == plugin:
                return index
        return None

    def refresh_services(self) -> None:
        if not hasattr(self, "services_frame"):
            return
        for child in self.services_frame.winfo_children():
            child.destroy()
        self.service_cards = []
        services = self.cfg.data.get("services") or []
        if not services:
            empty = tk.Frame(self.services_frame, bg=CARD, highlightthickness=1,
                             highlightbackground=BORDER, bd=0, padx=24, pady=30)
            empty.grid(row=0, column=0, columnspan=2, sticky="nsew",
                       pady=(0, 10))
            tk.Label(empty, text=T("未检测到本地插件"), bg=CARD, fg=FG,
                     font=(FONT_UI, 13, "bold")).pack(anchor="w")
            tk.Label(empty, text=T("把 SillyTavern 或 TTS 放到管理器同级目录，然后点“重新检测”。"),
                     bg=CARD, fg=MUTED, font=(FONT_UI, 10), justify="left",
                     wraplength=620).pack(anchor="w", pady=(6, 0))
        else:
            for index, service in enumerate(services):
                if not isinstance(service, dict):
                    continue
                row, column = divmod(index, 2)
                self._build_service_card(index, service, row, column)
        self.services_frame.update_idletasks()
        self._sync_services_scroll()
        self._bind_service_wheel(self.services_frame)

    def _build_service_card(self, index: int, service: dict,
                            row: int, column: int) -> None:
        info = service_plugin_info(service)
        plugin = service_plugin_id(service)
        options = service.setdefault("options", {})
        card = tk.Frame(self.services_frame, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER,
                        bd=0, padx=14, pady=12)
        card.grid(row=row, column=column, sticky="nsew",
                  padx=(0, 6) if column == 0 else (6, 0), pady=(0, 10))
        card.columnconfigure(1, weight=1)

        icon = tk.Label(card, text=info.get("icon", "•"),
                        bg=blend(CARD, ACCENT, 0.18), fg=ACCENT,
                        font=(FONT_UI, 12, "bold"), width=4, height=2,
                        bd=0, highlightthickness=0)
        icon.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 10))
        name = tk.Label(card, text=service.get("name") or info.get("title", ""),
                        bg=CARD, fg=FG, font=(FONT_UI, 13, "bold"),
                        anchor="w", bd=0, highlightthickness=0)
        name.grid(row=0, column=1, sticky="ew")

        status_box = tk.Frame(card, bg=CARD)
        status_box.grid(row=0, column=2, sticky="e", padx=(8, 0))
        dot = tk.Label(status_box, text="●", bg=CARD, fg=MUTED,
                       font=(FONT_UI, 11), bd=0, highlightthickness=0)
        dot.pack(side="left")
        status = tk.Label(status_box, text=T("检查中…"), bg=CARD, fg=MUTED,
                          font=(FONT_UI, 9), bd=0, highlightthickness=0)
        status.pack(side="left", padx=(4, 0))

        desc = tk.Label(card, text=T(info.get("desc", "")), bg=CARD, fg=MUTED,
                        font=(FONT_UI, 9), anchor="w", justify="left",
                        wraplength=370, bd=0, highlightthickness=0)
        desc.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(2, 0))

        address = service.get("url") or service.get("command") or ""
        try:
            port = int(service.get("port") or 0)
        except Exception:
            port = 0
        if port and address:
            address = T("端口 {port} · {address}", port=port, address=address)
        elif port:
            address = T("端口 {port}", port=port)
        api_base = service.get("api_base") or (service.get("options") or {}).get("api_base")
        if api_base:
            address += "\n" + T("Cherry API: {url}", url=api_base)
        detail = tk.Label(card, text=address, bg=CARD, fg=MUTED,
                          font=(FONT_UI, 9), anchor="w", justify="left",
                          wraplength=390, bd=0, highlightthickness=0)
        detail.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        next_row = 3
        cache_var = preload_var = auto_var = open_var = None
        model_var = None
        model_map: dict[str, str] = {}
        note = None
        note_saved = T("已保存，重启服务后生效。")

        def save_options(_event=None) -> None:
            opts = service.setdefault("options", {})
            if cache_var is not None:
                try:
                    opts["model_cache"] = max(1, min(8, int(cache_var.get())))
                except Exception:
                    opts["model_cache"] = 1
                opts["preload"] = preload_var.get().strip()
            if model_var is not None:
                selected = model_var.get()
                if selected in model_map:
                    opts["model"] = model_map[selected]
            if auto_var is not None:
                opts["auto_proxy"] = bool(auto_var.get())
            if open_var is not None:
                opts["open_after_start"] = bool(open_var.get())
            self.cfg.save()
            if note is not None:
                note.configure(text=note_saved, fg=GREEN)

        if plugin == "genie-tts":
            opt = tk.Frame(card, bg=CARD)
            opt.grid(row=next_row, column=0, columnspan=3, sticky="ew",
                     pady=(8, 0))
            next_row += 1
            tk.Label(opt, text=T("调度"), bg=CARD, fg=ACCENT,
                     font=(FONT_UI, 9, "bold")).pack(side="left")
            tk.Label(opt, text=T("模型缓存"), bg=CARD, fg=MUTED,
                     font=(FONT_UI, 9)).pack(side="left", padx=(10, 4))
            cache_var = tk.StringVar(value=str(options.get("model_cache", 1)))
            cache_box = ttk.Combobox(opt, textvariable=cache_var,
                                     state="readonly", width=3,
                                     values=("1", "2", "3", "4"))
            cache_box.pack(side="left")
            cache_box.bind("<<ComboboxSelected>>", save_options)
            tk.Label(opt, text=T("个角色"), bg=CARD, fg=MUTED,
                     font=(FONT_UI, 9)).pack(side="left", padx=(3, 10))
            tk.Label(opt, text=T("预加载"), bg=CARD, fg=MUTED,
                     font=(FONT_UI, 9)).pack(side="left")
            preload_var = tk.StringVar(value=str(options.get("preload") or ""))
            preload_entry = ttk.Entry(opt, textvariable=preload_var, width=11)
            preload_entry.pack(side="left", padx=(4, 0))
            preload_entry.bind("<Return>", save_options)
            preload_entry.bind("<FocusOut>", save_options)

            auto_var = tk.BooleanVar(value=bool(options.get("auto_proxy", True)))
            auto_check = tk.Checkbutton(
                card, text=T("同时启动代理"), variable=auto_var,
                command=save_options, bg=CARD, fg=FG, activebackground=CARD,
                activeforeground=FG, selectcolor=FIELD, bd=0,
                highlightthickness=0, font=(FONT_UI, 9))
            auto_check.grid(row=next_row, column=0, columnspan=3, sticky="w",
                            pady=(5, 0))
            next_row += 1
            note_saved = T("已保存，重启 TTS 后生效。")
            note = tk.Label(card, text=T("重启 TTS 后生效"), bg=CARD, fg=MUTED,
                            font=(FONT_UI, 9), anchor="w", bd=0,
                            highlightthickness=0)
            note.grid(row=next_row, column=0, columnspan=3, sticky="w",
                      pady=(3, 0))
            next_row += 1
        elif plugin == "gpt-sovits":
            opt = tk.Frame(card, bg=CARD)
            opt.grid(row=next_row, column=0, columnspan=3, sticky="ew",
                     pady=(8, 0))
            next_row += 1
            tk.Label(opt, text=T("CUDA"), bg=CARD, fg=ACCENT,
                     font=(FONT_UI, 9, "bold")).pack(side="left")
            tk.Label(opt, text=T("v2ProPlus · 零样本克隆 · 约 2.3–3 GB 显存"),
                     bg=CARD, fg=MUTED, font=(FONT_UI, 9)).pack(side="left",
                                                                padx=(8, 0))

            auto_var = tk.BooleanVar(value=bool(options.get("auto_proxy", True)))
            auto_check = tk.Checkbutton(
                card, text=T("同时启动代理"), variable=auto_var,
                command=save_options, bg=CARD, fg=FG, activebackground=CARD,
                activeforeground=FG, selectcolor=FIELD, bd=0,
                highlightthickness=0, font=(FONT_UI, 9))
            auto_check.grid(row=next_row, column=0, columnspan=3, sticky="w",
                            pady=(5, 0))
            next_row += 1
            note_saved = T("已保存，重启 GPT-SoVITS 后生效。")
            note = tk.Label(card, text=T("重启 GPT-SoVITS 后生效"), bg=CARD,
                            fg=MUTED, font=(FONT_UI, 9), anchor="w", bd=0,
                            highlightthickness=0)
            note.grid(row=next_row, column=0, columnspan=3, sticky="w",
                      pady=(3, 0))
            next_row += 1
        elif plugin == "sillytavern":
            open_var = tk.BooleanVar(
                value=bool(options.get("open_after_start", True)))
            open_check = tk.Checkbutton(
                card, text=T("启动后打开浏览器"), variable=open_var,
                command=save_options, bg=CARD, fg=FG, activebackground=CARD,
                activeforeground=FG, selectcolor=FIELD, bd=0,
                highlightthickness=0, font=(FONT_UI, 9))
            open_check.grid(row=next_row, column=0, columnspan=3, sticky="w",
                            pady=(8, 0))
            next_row += 1
        elif plugin == "genie-tts-proxy":
            hint = tk.Label(card, text=T("需要先启动 TTS 语音（9880）"),
                            bg=CARD, fg=MUTED, font=(FONT_UI, 9), anchor="w",
                            bd=0, highlightthickness=0)
            hint.grid(row=next_row, column=0, columnspan=3, sticky="w",
                      pady=(8, 0))
            next_row += 1

        actions = tk.Frame(card, bg=CARD)
        actions.grid(row=next_row, column=0, columnspan=3, sticky="ew",
                     pady=(10, 0))
        primary = ttk.Button(actions, text=T("启动"), style="Accent.TButton",
                             command=lambda i=index: self.start_service(i))
        primary.pack(side="left")
        stop = ttk.Button(actions, text=T("停止"), style="Danger.TButton",
                          command=lambda i=index: self.stop_service(i))
        stop.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text=T("编辑"),
                   command=lambda i=index: self.edit_service(i)).pack(side="right")
        ttk.Button(actions, text=T("删除"),
                   command=lambda i=index: self.delete_service(i)).pack(
            side="right", padx=(0, 6))

        clickable = [card, icon, name, status_box, dot, status, desc, detail]
        for widget in clickable:
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>",
                        lambda _e, i=index: self.start_service(i))
            widget.bind(
                "<Enter>",
                lambda _e, c=card: c.configure(highlightbackground=ACCENT))
            widget.bind(
                "<Leave>",
                lambda _e, c=card: self.services_frame.after_idle(
                    lambda: self._service_card_leave(c)))

        self.service_cards.append({
            "index": index, "service": service, "card": card,
            "dot": dot, "status": status, "primary": primary, "stop": stop,
        })

    def _service_card_leave(self, card) -> None:
        try:
            x, y = card.winfo_pointerxy()
            target = card.winfo_containing(x, y)
            while target is not None:
                if target is card:
                    return
                target = target.master
        except Exception:
            pass
        try:
            card.configure(highlightbackground=BORDER)
        except Exception:
            pass

    def _set_service_card_status(self, index: int, state: str) -> None:
        for card in self.service_cards:
            if card.get("index") != index:
                continue
            if state == "starting":
                card["dot"].configure(fg=ACCENT)
                card["status"].configure(text=T("启动中…"), fg=MUTED)
                card["primary"].configure(text=T("启动"))
            return

    def start_service(self, index: int | None = None) -> None:
        service = self._service_at(index)
        if not service:
            return
        self.service_starting.add(index)
        try:
            started = self._launch_service(service)
        except Exception as exc:
            self.service_starting.discard(index)
            messagebox.showerror(T("启动失败"), str(exc))
            return
        options = service.get("options") or {}
        if not started:
            self.service_starting.discard(index)
            proxy_plugin = AUTO_PROXY_PLUGINS.get(service_plugin_id(service))
            if proxy_plugin and options.get("auto_proxy", True):
                proxy_index = self._service_index_by_plugin(proxy_plugin)
                if proxy_index is not None:
                    proxy = self._service_at(proxy_index)
                    try:
                        proxy_port = int((proxy or {}).get("port") or 0)
                    except Exception:
                        proxy_port = 0
                    if proxy_port and not is_port_open(proxy_port):
                        self._start_service_quiet(proxy_index)
            self._open_service(service)
            return
        self._set_service_card_status(index, "starting")
        if (service_plugin_id(service) == "genie-tts"
                and options.get("auto_proxy", True)):
            proxy_index = self._service_index_by_plugin("genie-tts-proxy")
            if proxy_index is not None:
                self.after(2500,
                           lambda i=proxy_index: self._start_service_quiet(i))
        if service.get("url") and options.get("open_after_start", True):
            self.after(3000, lambda s=service: self._open_service(s))
        self.after(1200, lambda: self.refresh_status(force=True))

    def _start_service_quiet(self, index: int) -> None:
        service = self._service_at(index)
        if not service:
            return
        try:
            port = int(service.get("port") or 0)
        except Exception:
            port = 0
        if port and is_port_open(port):
            return
        try:
            self._launch_service(service)
            self.service_starting.add(index)
            self._set_service_card_status(index, "starting")
        except Exception:
            log("自动启动 TTS 代理失败:\n" + traceback.format_exc())

    def _service_env(self, service: dict) -> dict:
        """TTS scheduling lives on the TTS card, but the proxy needs it too."""
        env = service_environment(service)
        if service_plugin_id(service) == "genie-tts-proxy":
            tts = self._service_at(self._service_index_by_plugin("genie-tts"))
            if tts is not None:
                env = service_environment(tts)
        return env

    def _launch_service(self, service: dict) -> bool:
        try:
            port = int(service.get("port") or 0)
        except Exception:
            port = 0
        if port and is_port_open(port):
            return False
        command = str(service.get("command") or "")
        path = self.cfg.resolve(command)
        env = self._service_env(service)
        if path.is_file():
            self._launch_path(path, env=env)
        elif command:
            subprocess.Popen(command, shell=True, cwd=str(self.cfg.base),
                             env=env, creationflags=CREATE_NEW_CONSOLE)
        else:
            raise ValueError(T("这个服务没有配置启动命令。"))
        return True

    def _launch_path(self, path: Path, env: dict | None = None) -> None:
        suffix = path.suffix.lower()
        env = env if env is not None else os.environ.copy()
        if suffix in (".bat", ".cmd"):
            subprocess.Popen(["cmd", "/c", str(path)], cwd=str(path.parent),
                             env=env, creationflags=CREATE_NEW_CONSOLE)
        elif suffix in (".sh",):
            subprocess.Popen(["bash", str(path)], cwd=str(path.parent), env=env,
                             start_new_session=True)
        elif suffix in (".exe",):
            subprocess.Popen([str(path)], cwd=str(path.parent), env=env,
                             creationflags=CREATE_NEW_CONSOLE)
        elif IS_WINDOWS:
            os.startfile(str(path))
        else:
            subprocess.Popen([str(path)], cwd=str(path.parent), env=env,
                             start_new_session=True)

    def _open_service(self, service: dict) -> None:
        url = service.get("url")
        if not url and service_plugin_id(service) == "sillytavern":
            try:
                port = int(service.get("port") or 0)
            except Exception:
                port = 0
            if port:
                url = f"http://127.0.0.1:{port}"
        if url:
            webbrowser.open(url)
            return
        messagebox.showinfo(T("服务"),
                            T("{name} 正在运行。", name=service.get("name", "")))

    def _stop_one_service(self, service: dict, ask: bool = True) -> tuple[bool, str]:
        name = service.get("name") or T("服务")
        stop = service.get("stop", "")
        env = self._service_env(service)
        try:
            if stop:
                path = self.cfg.resolve(stop)
                if path.is_file():
                    self._launch_path(path, env=env)
                else:
                    subprocess.Popen(stop, shell=True, cwd=str(self.cfg.base),
                                     env=env, creationflags=CREATE_NEW_CONSOLE)
                return True, T("已执行停止命令：{command}", command=stop)
            try:
                port = int(service.get("port") or 0)
            except Exception:
                port = 0
            pid = find_listener_pid(port) if port else None
            if not pid:
                return False, T("{name} 没有在运行。", name=name)
            process = process_name(pid)
            keyword = str(service.get("process") or "").lower()
            if keyword and keyword not in process.lower():
                return False, T(
                    "端口 {port} 被 {name} (PID {pid}) 占用，不包含关键字“{keyword}”。"
                    "为安全起见未停止。",
                    port=port, name=process or T("未知进程"), pid=pid,
                    keyword=keyword)
            if ask and not messagebox.askyesno(
                    T("停止服务"), T("停止 {name}（PID {pid}）吗？",
                                     name=name, pid=pid)):
                return False, ""
            terminate_pid(pid)
            return True, T("已停止 {name}。", name=name)
        except Exception as exc:
            return False, str(exc)

    def stop_service(self, index: int | None = None) -> None:
        service = self._service_at(index)
        if not service:
            return
        if index is not None:
            self.service_starting.discard(index)
        messages: list[str] = []
        ok, message = self._stop_one_service(service, ask=True)
        if message == "":
            return
        if message:
            messages.append(message)
        proxy_plugin = AUTO_PROXY_PLUGINS.get(service_plugin_id(service))
        if (proxy_plugin
                and (service.get("options") or {}).get("auto_proxy", True)):
            proxy_index = self._service_index_by_plugin(proxy_plugin)
            proxy = self._service_at(proxy_index)
            if proxy is not None:
                try:
                    proxy_port = int(proxy.get("port") or 0)
                except Exception:
                    proxy_port = 0
                if proxy_port and is_port_open(proxy_port):
                    if proxy_index is not None:
                        self.service_starting.discard(proxy_index)
                    _ok, proxy_message = self._stop_one_service(proxy, ask=False)
                    if proxy_message:
                        messages.append(proxy_message)
        if messages:
            messagebox.showinfo(T("停止服务"), "\n".join(messages))
        self.refresh_status(force=True)

    def rescan_services(self) -> None:
        data = self.cfg.data
        data["dismissed_plugins"] = []
        services = data.get("services")
        if not isinstance(services, list):
            services = []
            data["services"] = services
        existing = {service_plugin_id(service): service
                    for service in services if isinstance(service, dict)}
        added: list[str] = []
        for detected in autodetect_services(self.cfg.base):
            plugin = detected["plugin"]
            detected["command"] = self.cfg.portable(Path(detected["command"]))
            current = existing.get(plugin)
            if current is None:
                services.append(detected)
                added.append(detected.get("name") or plugin)
                continue
            old_path = self.cfg.resolve(current.get("command"))
            new_path = self.cfg.resolve(detected.get("command"))
            if not old_path.is_file() and new_path.is_file():
                current["command"] = detected["command"]
            current.setdefault("plugin", plugin)
            options = current.setdefault("options", {})
            for key, value in (detected.get("options") or {}).items():
                options.setdefault(key, value)
        self.cfg.save()
        self.refresh_services()
        if added:
            messagebox.showinfo(
                T("插件"), T("重新检测到 {count} 个插件：{names}",
                             count=len(added), names=", ".join(added)))
        else:
            messagebox.showinfo(T("插件"), T("没有发现新的插件。"))

    def add_service(self) -> None:
        dialog = ServiceDialog(self, T("添加服务"), None)
        self.wait_window(dialog)
        if dialog.result:
            dialog.result["plugin"] = service_plugin_id(dialog.result)
            self.cfg.data.setdefault("services", []).append(dialog.result)
            self.cfg.save()
            self.refresh_services()

    def edit_service(self, index: int | None = None) -> None:
        service = self._service_at(index)
        if not service:
            return
        dialog = ServiceDialog(self, T("编辑服务"), service)
        self.wait_window(dialog)
        if dialog.result:
            services = self.cfg.data.setdefault("services", [])
            dialog.result["plugin"] = (dialog.result.get("plugin")
                                       or service_plugin_id(service))
            services[index] = dialog.result
            self.cfg.save()
            self.refresh_services()

    def delete_service(self, index: int | None = None) -> None:
        service = self._service_at(index)
        if not service:
            return
        if not messagebox.askyesno(
                T("删除服务"), T("删除“{name}”快捷方式吗？",
                                 name=service.get("name"))):
            return
        plugin = service_plugin_id(service)
        self.cfg.data.get("services", []).remove(service)
        if plugin != "custom":
            dismissed = self.cfg.data.setdefault("dismissed_plugins", [])
            if plugin not in dismissed:
                dismissed.append(plugin)
        self.cfg.save()
        self.refresh_services()

    # ---------- tools ----------

    def open_model_dir(self) -> None:
        name = self.selected_profile_name()
        if not name:
            return
        model = self.cfg.resolve(self._profile(name).get("model"))
        if model.exists():
            self.open_path(model.parent)
        else:
            messagebox.showinfo(T("路径不存在"), str(model))

    def open_path(self, path: Path) -> None:
        try:
            path = Path(path)
            if not path.exists():
                messagebox.showinfo(T("路径不存在"), str(path))
                return
            if IS_WINDOWS:
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror(T("打开失败"), str(exc))

    # ---------- close ----------

    def on_close(self) -> None:
        port = self._port()
        running = bool(find_listener_pid(port)) or self.server.managed_running()
        if running:
            answer = messagebox.askyesno(
                T("退出管理器"),
                T("模型服务还在运行。要同时停止它吗？\n\n"
                  "选择“否”会让模型继续在后台运行。"),
                default=messagebox.NO)
            if answer:
                self.server.stop(port)
        self._persist_runtime_settings()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def selftest() -> None:
    cfg = Config(CONFIG_PATH)
    dirs = [cfg.resolve(item) for item in cfg.data.get("scan_dirs", [])]
    models = scan_models(dirs)
    mmprojs = scan_mmproj(dirs)
    print("config:      ", cfg.path)
    print("base:        ", cfg.base)
    print("server:      ", cfg.resolve(cfg.data.get("server_path")))
    print("scan dirs:   ", [str(item) for item in dirs])
    print("models:      ", len(models))
    for path in models:
        print("   ", path, human_size(path.stat().st_size))
    print("mmproj:      ", len(mmprojs))
    for path in mmprojs:
        print("   ", path, human_size(path.stat().st_size))
    print("profiles:    ", ", ".join(cfg.data.get("profiles", {})) or "(none)")
    print("clients:     ", cfg.data.get("clients"))
    preset, missing = router_preset_text(cfg)
    print("preset:      ", cfg.preset_path(), f"({len(preset.splitlines())} lines)")
    if missing:
        print("missing:     ", ", ".join(missing))
    print("gpu:         ", detect_gpu())


def main() -> None:
    global CONFIG_PATH, BASE_DIR
    args = sys.argv[1:]
    if "--config" in args:
        index = args.index("--config")
        if index + 1 < len(args):
            CONFIG_PATH = Path(args[index + 1]).expanduser()
            BASE_DIR = CONFIG_PATH.parent
    if "--selftest" in args:
        selftest()
        return
    try:
        app = App(CONFIG_PATH)
        app.mainloop()
    except Exception:
        log("管理器崩溃:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
