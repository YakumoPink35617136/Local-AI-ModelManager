# -*- coding: utf-8 -*-
"""OpenAI-compatible proxy for GPT-SoVITS api_v2 (port 9882).

Voices come from VoiceLibrary/voices.json, the same library used by the
Genie TTS proxy. Adding a voice there needs no code change here.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GSV_URL = os.environ.get("GSV_TTS_URL", "http://127.0.0.1:9883/tts")
LIBRARY_DIR = Path(os.environ.get("VOICE_LIBRARY", BASE_DIR / "VoiceLibrary")).expanduser()
VOICES_JSON = LIBRARY_DIR / "voices.json"

ACTION_RE = re.compile(r"（[^）]*）|\([^)]*\)|\[[^\]]*\]|【[^】]*】")
KANA_RE = re.compile(r"[\u3040-\u30ff]")

_LIB_LOCK = threading.Lock()
_TTS_LOCK = threading.Lock()
_LIB_CACHE = None
_LIB_MTIME = None
_WARMUP = {"ok": False, "message": "pending"}


def _load_library() -> dict:
    data = json.loads(VOICES_JSON.read_text(encoding="utf-8"))
    index: dict[str, dict] = {}
    order: list[str] = []
    for name, entry in (data.get("voices") or {}).items():
        if not isinstance(entry, dict):
            continue
        ref_name = entry.get("gpt_ref") or entry.get("ref") or ""
        ref = (LIBRARY_DIR / str(ref_name)).resolve()
        if not ref.is_file():
            print(f">>> voice ref missing: {name} -> {ref}", flush=True)
            continue
        item = {
            "name": name,
            "lang": str(entry.get("lang") or "zh"),
            "ref": str(ref),
            "prompt": str(entry.get("gpt_prompt") or entry.get("prompt") or ""),
        }
        index[name.lower()] = item
        for alias in entry.get("aliases") or []:
            index[str(alias).lower()] = item
        order.append(name)

    default_name = str(data.get("default") or "").strip()
    default = index.get(default_name.lower()) if default_name else None
    if default is None and order:
        default = index[order[0].lower()]
    return {"index": index, "order": order, "default": default, "data": data}


def library() -> dict:
    """Reload voices.json when its mtime changes, so adding a voice is live."""
    global _LIB_CACHE, _LIB_MTIME
    try:
        mtime = VOICES_JSON.stat().st_mtime
    except OSError:
        mtime = None
    with _LIB_LOCK:
        if _LIB_CACHE is None or mtime != _LIB_MTIME:
            try:
                _LIB_CACHE = _load_library()
                _LIB_MTIME = mtime
                print(f">>> library loaded: {len(_LIB_CACHE['order'])} voices "
                      f"from {VOICES_JSON}", flush=True)
            except Exception as exc:
                print(f">>> library load failed: {exc}", flush=True)
                if _LIB_CACHE is None:
                    _LIB_CACHE = {"index": {}, "order": [], "default": None,
                                  "data": {}}
                # Keep the old cache and retry on the next request.
                _LIB_MTIME = None
    return _LIB_CACHE


def resolve_voice(name: str) -> dict | None:
    lib = library()
    key = str(name or "").strip().lower()
    return lib["index"].get(key) or lib["default"]


def _warmup() -> None:
    """Warm CUDA kernels and the default reference after the API is ready."""
    cfg = library()["default"]
    if cfg is None:
        _WARMUP.update(ok=False, message="no default voice")
        return
    text = {"ja": "あ", "zh": "嗯", "en": "ah"}.get(cfg["lang"], "ah")
    payload = {
        "text": text,
        "text_lang": cfg["lang"],
        "ref_audio_path": cfg["ref"],
        "prompt_text": cfg["prompt"],
        "prompt_lang": cfg["lang"],
        "text_split_method": "cut0",
        "batch_size": 1,
        "speed_factor": 1.0,
        "media_type": "wav",
        "streaming_mode": 0,
        "parallel_infer": True,
        "repetition_penalty": 1.35,
    }
    for _ in range(60):
        try:
            with _TTS_LOCK:
                r = requests.post(GSV_URL, json=payload, timeout=20)
            if r.status_code == 200:
                _WARMUP.update(ok=True, message=f"ok ({cfg['name']})")
                print(f">>> warmup ok ({cfg['name']})", flush=True)
                return
            _WARMUP["message"] = f"http {r.status_code}"
        except Exception as exc:
            _WARMUP["message"] = str(exc)[:120]
        time.sleep(1)
    print(f">>> warmup skipped: {_WARMUP['message']}", flush=True)


def clean_text(text: str) -> str:
    text = ACTION_RE.sub("", text or "")
    text = text.replace("*", "")
    for quote in ("「", "」", "『", "』", "“", "”", "‘", "’"):
        text = text.replace(quote, "")
    return text.strip()


def detect_lang(text: str) -> str:
    if KANA_RE.search(text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


@app.on_event("startup")
def _start_warmup():
    threading.Thread(target=_warmup, daemon=True).start()


@app.get("/health")
def health():
    lib = library()
    return {
        "status": "ok",
        "voices": len(lib["order"]),
        "library": str(VOICES_JSON),
        "warmup": _WARMUP["ok"],
        "warmup_message": _WARMUP["message"],
    }


@app.get("/v1/audio/voices")
def voices():
    lib = library()
    details = []
    for name in lib["order"]:
        item = lib["index"].get(name.lower())
        details.append({
            "name": name,
            "lang": item["lang"] if item else "zh",
        })
    # Keep "voices" for older clients; "details" is used by SillyTavern.
    return {"voices": lib["order"], "details": details}


@app.post("/reload")
def reload_library():
    global _LIB_MTIME
    with _LIB_LOCK:
        _LIB_MTIME = None
    lib = library()
    return {"voices": lib["order"]}


@app.post("/v1/audio/speech")
async def speech(req: Request):
    body = await req.json()
    text = clean_text(body.get("input", ""))
    voice = str(body.get("voice", ""))
    cfg = resolve_voice(voice)
    if not text:
        return Response(content=b"", status_code=400, media_type="text/plain")
    if cfg is None:
        return Response(content="voice library is empty or unavailable",
                        status_code=503, media_type="text/plain")

    lang = detect_lang(text)
    try:
        speed = float(body.get("speed", 1.0) or 1.0)
    except Exception:
        speed = 1.0
    speed = min(max(speed, 0.5), 2.0)

    payload = {
        "text": text,
        "text_lang": lang,
        "ref_audio_path": cfg["ref"],
        "prompt_text": cfg["prompt"],
        "prompt_lang": cfg["lang"],
        "text_split_method": "cut0",
        "batch_size": 1,
        "speed_factor": speed,
        "media_type": "wav",
        "streaming_mode": 0,
        "parallel_infer": True,
        "repetition_penalty": 1.35,
    }

    t0 = time.time()
    try:
        with _TTS_LOCK:
            r = requests.post(GSV_URL, json=payload, timeout=180)
    except Exception as e:
        print(f">>> gsv error: {e}", flush=True)
        return Response(content=f"gpt-sovits error: {e}", status_code=502,
                        media_type="text/plain")

    if r.status_code != 200:
        print(f">>> gsv http {r.status_code}: {r.text[:200]}", flush=True)
        return Response(content=r.text[:500], status_code=502,
                        media_type="text/plain")

    print(f">>> {time.time()-t0:.2f}s gsv voice={cfg['name']} lang={lang} "
          f"in={len(text)} out={len(r.content)}B | {text[:36]!r}", flush=True)
    return Response(content=r.content, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9882)
