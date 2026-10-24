# -*- coding: utf-8 -*-
"""Genie TTS backend (port 9880).

All paths are resolved relative to this file, so the whole folder can be moved
or copied to another machine. Optional environment variables:

    GENIE_PYTHON       python.exe to use (handled by server.bat)
    GENIE_DATA_DIR     GenieData folder
    GENIE_CHARACTER    character folder name (default: default)
    GENIE_LANGUAGE     zh / ja / en (default: zh)
    GENIE_REF_AUDIO    reference wav path, relative to this file or absolute
    GENIE_REF_TEXT     transcript of the reference wav
    GENIE_HOST         default 127.0.0.1
    GENIE_PORT         default 9880
    GENIE_WORKERS      default 1
"""

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("GENIE_DATA_DIR", BASE_DIR / "GenieData"))
if not DATA_DIR.is_absolute():
    DATA_DIR = BASE_DIR / DATA_DIR

CHARACTER = os.environ.get("GENIE_CHARACTER", "default")
LANGUAGE = os.environ.get("GENIE_LANGUAGE", "zh")
CHAR_DIR = BASE_DIR / "CharacterModels" / "v2ProPlus" / CHARACTER
MODEL_DIR = CHAR_DIR / "tts_models"

ref_env = os.environ.get("GENIE_REF_AUDIO")
text_env = os.environ.get("GENIE_REF_TEXT")
auto_text = ""
REF_AUDIO = Path()

if ref_env:
    REF_AUDIO = Path(ref_env)
    if not REF_AUDIO.is_absolute():
        REF_AUDIO = BASE_DIR / REF_AUDIO
else:
    # Prefer prompt_wav.json, which carries both the wav name and transcript.
    meta_path = CHAR_DIR / "prompt_wav.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            first = next(iter(meta.values()))
            wav_name = str(first.get("wav") or "")
            auto_text = str(first.get("text") or "")
            for candidate in (CHAR_DIR / "prompt_wav" / wav_name,
                              CHAR_DIR / wav_name):
                if candidate.is_file():
                    REF_AUDIO = candidate
                    break
            if not REF_AUDIO.is_file() and wav_name:
                matches = sorted(CHAR_DIR.rglob(wav_name))
                if matches:
                    REF_AUDIO = matches[0]
        except Exception:
            auto_text = ""
    if not REF_AUDIO.is_file():
        candidates = sorted((CHAR_DIR / "prompt_wav").glob("*.wav"))
        if not candidates:
            candidates = sorted(CHAR_DIR.rglob("*.wav"))
        if candidates:
            REF_AUDIO = candidates[0]

REF_TEXT = text_env if text_env is not None else auto_text
HOST = os.environ.get("GENIE_HOST", "127.0.0.1")
PORT = int(os.environ.get("GENIE_PORT", "9880"))
WORKERS = int(os.environ.get("GENIE_WORKERS", "1"))

if not DATA_DIR.is_dir():
    print(f"[ERROR] GenieData folder not found:\n  {DATA_DIR}", flush=True)
    sys.exit(1)
if not MODEL_DIR.is_dir():
    print(f"[ERROR] TTS model folder not found for character '{CHARACTER}':\n"
          f"  {MODEL_DIR}\n"
          f"Set GENIE_CHARACTER to a folder name under CharacterModels/v2ProPlus.",
          flush=True)
    sys.exit(1)
if not REF_AUDIO.is_file():
    print(f"[ERROR] Reference audio not found:\n  {REF_AUDIO}\n"
          f"Set GENIE_REF_AUDIO to a .wav inside the character folder.",
          flush=True)
    sys.exit(1)
if not REF_TEXT:
    print("[WARN] No reference transcript found; set GENIE_REF_TEXT if TTS "
          "quality is poor.", flush=True)

os.environ["GENIE_DATA_DIR"] = str(DATA_DIR)
# 默认只缓存一个角色模型，切换角色时重新加载，避免同时占两份内存。
os.environ.setdefault(
    "Max_Cached_Character_Models",
    os.environ.get("GENIE_MODEL_CACHE", "1"),
)
import genie_tts as genie  # noqa: E402

print(f">>> character: {CHARACTER}", flush=True)
print(f">>> model:     {MODEL_DIR}", flush=True)
print(f">>> reference: {REF_AUDIO}", flush=True)

genie.load_character(
    character_name=CHARACTER,
    onnx_model_dir=str(MODEL_DIR),
    language=LANGUAGE,
)
print(">>> load_character done", flush=True)

genie.set_reference_audio(
    character_name=CHARACTER,
    audio_path=str(REF_AUDIO),
    audio_text=REF_TEXT,
)
print(">>> set_reference_audio done", flush=True)

genie.start_server(host=HOST, port=PORT, workers=WORKERS)
