from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
import requests
import re
import os
import json
import struct
import time
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class _Tee:
    """Write stdout both to the console and to a file."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


_runtime_log = open(BASE_DIR / "proxy.runtime.log", "a", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.stdout, _runtime_log)
print(f"\n===== proxy start {time.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GENIE = os.environ.get("GENIE_TTS_URL", "http://127.0.0.1:9880")
PROXY_HOST = os.environ.get("GENIE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("GENIE_PROXY_PORT", "9881"))
SAMPLE_RATE = 32000

# 音色库和 GPT-SoVITS 共用；加音色只改 voices.json。
LIBRARY = Path(os.environ.get("VOICE_LIBRARY", BASE_DIR / "VoiceLibrary")).expanduser()
VOICES_JSON = LIBRARY / "voices.json"


def load_library():
    """Load the shared voice library used by Genie and GPT-SoVITS."""
    if not VOICES_JSON.is_file():
        print(f">>> voice library not found: {VOICES_JSON}", flush=True)
        return {}, {}, [], "default"

    data = json.loads(VOICES_JSON.read_text(encoding="utf-8"))
    models = {}
    for key, item in (data.get("models") or {}).items():
        if not isinstance(item, dict):
            continue
        model_dir = (LIBRARY / str(item.get("model_dir") or "")).resolve()
        if not model_dir.is_dir():
            print(f">>> genie model missing: {key} -> {model_dir}", flush=True)
        models[key] = {
            "character_name": item.get("character_name") or key,
            "onnx_model_dir": str(model_dir),
            "language": item.get("language") or "zh",
        }

    index = {}
    order = []
    for name, item in (data.get("voices") or {}).items():
        if not isinstance(item, dict):
            continue
        model_key = str(item.get("genie_model") or "").strip()
        if not model_key or model_key not in models:
            # GPT-SoVITS-only voice; Genie cannot synthesize it.
            continue
        ref = (LIBRARY / str(item.get("ref") or "")).resolve()
        if not ref.is_file():
            print(f">>> genie ref missing: {name} -> {ref}", flush=True)
            continue
        entry = (model_key, name, str(ref), str(item.get("prompt") or ""))
        index[name.lower()] = entry
        for alias in item.get("aliases") or []:
            index[str(alias).lower()] = entry
        order.append(name)

    default_name = str(data.get("genie_default") or data.get("default") or "").strip()
    default_voice = (default_name if default_name.lower() in index
                     else (order[0] if order else "default"))
    print(f">>> library loaded: {len(order)} voices, "
          f"{len(models)} models from {VOICES_JSON}", flush=True)
    return models, index, order, default_voice


MODELS, VOICE_INDEX, VOICE_ORDER, DEFAULT_VOICE = load_library()
try:
    _LIBRARY_MTIME = VOICES_JSON.stat().st_mtime
except OSError:
    _LIBRARY_MTIME = None


def reload_library_if_changed():
    """Pick up voices.json edits without restarting the proxy."""
    global MODELS, VOICE_INDEX, VOICE_ORDER, DEFAULT_VOICE
    global current_model, current_voice, _LIBRARY_MTIME
    try:
        mtime = VOICES_JSON.stat().st_mtime
    except OSError:
        return
    if mtime == _LIBRARY_MTIME:
        return
    try:
        MODELS, VOICE_INDEX, VOICE_ORDER, DEFAULT_VOICE = load_library()
    except Exception as exc:
        print(f">>> library reload failed: {exc}", flush=True)
        # Keep the old tables and retry on the next request.
        _LIBRARY_MTIME = None
        return
    current_model = None
    current_voice = None
    _LIBRARY_MTIME = mtime
    print(">>> voice library reloaded", flush=True)

# 复用 TCP 连接，省掉每次请求的握手开销
session = requests.Session()
current_model = None
current_voice = None
loaded_models = set()
TTS_LOCK = threading.Lock()


def resolve_voice(voice_name):
    """把酒馆传来的 voice 名解析成模型和参考音频；未知名字回退到默认。"""
    key = str(voice_name or "").strip().lower()
    if key in VOICE_INDEX:
        return VOICE_INDEX[key]
    for model_key, model in MODELS.items():
        if key in (model_key.lower(), model["character_name"].lower()):
            for name in VOICE_ORDER:
                entry = VOICE_INDEX.get(name.lower())
                if entry and entry[0] == model_key:
                    return entry
    if DEFAULT_VOICE.lower() in VOICE_INDEX:
        return VOICE_INDEX[DEFAULT_VOICE.lower()]
    if VOICE_ORDER:
        return VOICE_INDEX[VOICE_ORDER[0].lower()]
    return None


def _post(path, payload, timeout=120):
    return session.post(f"{GENIE}{path}", json=payload, timeout=timeout)


def ensure_voice(voice_name):
    """确保 Genie 当前加载的模型和参考音频与请求的 voice 一致。"""
    global current_model, current_voice
    reload_library_if_changed()
    resolved = resolve_voice(voice_name)
    if resolved is None:
        print(f">>> no Genie voice available for {voice_name!r}", flush=True)
        return None
    model_key, display_voice, audio_path, ref_text = resolved
    model = MODELS[model_key]

    # 始终在模型切换时调用 load_character。
    # 缓存只有一个角色时，旧模型会被淘汰，下次切回来必须重新加载。
    if model_key != current_model:
        try:
            r = _post("/load_character", {
                "character_name": model["character_name"],
                "onnx_model_dir": model["onnx_model_dir"],
                "language": model["language"],
            }, timeout=120)
        except Exception as e:
            print(f">>> load model {model_key} failed: {e}", flush=True)
            return None
        if r.status_code != 200:
            print(f">>> load model {model_key} failed status={r.status_code} {r.text[:200]}", flush=True)
            return None
        loaded_models.add(model_key)
        current_model = model_key
        current_voice = None
        print(f">>> load model -> {model_key} ({model['language']}) ok", flush=True)

    if current_voice != display_voice:
        if not os.path.exists(audio_path):
            print(f">>> voice file missing: {audio_path}", flush=True)
            return None
        try:
            r = _post("/set_reference_audio", {
                "character_name": model["character_name"],
                "audio_path": audio_path,
                "audio_text": ref_text,
                "language": model["language"],
            }, timeout=60)
        except Exception as e:
            print(f">>> switch voice {display_voice} failed: {e}", flush=True)
            return None
        if r.status_code != 200:
            print(f">>> switch voice {display_voice} failed status={r.status_code} {r.text[:200]}", flush=True)
            return None
        current_voice = display_voice
        print(f">>> switch voice -> {display_voice} "
              f"({os.path.basename(audio_path)}) ok", flush=True)

    return {
        "model_key": model_key,
        "voice": display_voice,
        "language": model["language"],
        "character_name": model["character_name"],
    }


# ========= 文本清洗 =========
# 保留：中日汉字、平假名、片假名、英文、数字、中日标点、省略号。
# 去掉：括号里的动作/翻译、星号动作、引号本身。
_ALLOWED_CHARS = re.compile(
    r"[^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9"
    r"\uff0c\u3002\uff1f\uff01\u3001\u2026]"
)


def strip_symbols(text):
    """把标点也去掉，作为 TTS 失败时的兜底候选。"""
    return re.sub(
        r"[^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]",
        "",
        text,
    )


def clean_text(t):
    if not t:
        return "嗯"
    # 1. 去掉括号内容（动作描写、中文翻译）
    t = re.sub(r"\uff08[^\uff09]*\uff09", "", t)  # （）
    t = re.sub(r"\([^)]*\)", "", t)              # ()
    t = re.sub(r"\[[^\]]*\]", "", t)             # []
    t = re.sub(r"\u3010[^\u3011]*\u3011", "", t)  # 【】
    # 2. 去掉 *星号* 场景
    t = re.sub(r"\*[^*]*\*", "", t)
    # 3. 去掉各种引号符号本身（不动「」）
    for ch in ["\u201c", "\u201d", "\u2018", "\u2019", "\u0022", "\u0027"]:
        t = t.replace(ch, "")
    # 4. 只保留中日文/英文/数字/基本标点
    t = _ALLOWED_CHARS.sub("", t)
    t = t.strip()
    # 5. 折叠多余标点
    t = re.sub(r"[\uff0c\u3002]{2,}", "\u3002", t)
    if not t:
        return "嗯"
    return t


# ========= 分句 =========
# Genie 内部是一句一推理，整段丢进去要等全部合成完才有第一个字节。
_SENT_END = "。！？!?"
_SENT_SOFT = "，、；：,;:"


def _eff_len(s):
    """有效长度：标点不计入，CJK 算 2。"""
    return sum(1 if ord(c) < 128 else 2 for c in s if c not in _SENT_END + _SENT_SOFT)


def _soft_cut(s, max_len):
    """最靠后、且之前内容有效长度不超过 max_len 的软标点切点；没有则 -1。"""
    n, best = 0, -1
    for i, c in enumerate(s):
        if c in _SENT_END + _SENT_SOFT:
            if c in _SENT_SOFT and n <= max_len:
                best = i + 1
            continue
        n += 1 if ord(c) < 128 else 2
        if n > max_len:
            break
    return best


def _hard_cut(s, max_len):
    """没有标点可用时的兜底：返回有效长度刚好够 max_len 的切点。"""
    n = 0
    for i, c in enumerate(s):
        if c in _SENT_END + _SENT_SOFT:
            continue
        n += 1 if ord(c) < 128 else 2
        if n >= max_len:
            return max(i + 1, 1)
    return len(s)


def _cut_once(s, max_len):
    idx = _soft_cut(s, max_len)
    return idx if idx > 0 else _hard_cut(s, max_len)


def split_sentences(text, max_len=44, min_len=6):
    text = (text or "").strip()
    if not text:
        return []
    # 单句整段合成，不按逗号/省略号拆；只有多句才逐句。
    if len(re.findall(r"[。！？!?]", text)) <= 1:
        return [text]
    parts = re.findall(r"[^。！？!?]+[。！？!?]*", text)
    out, buf = [], ""
    for p in parts:
        buf += p
        if not buf:
            continue
        if buf[-1] in _SENT_END and _eff_len(buf) >= min_len:
            while _eff_len(buf) > max_len:
                cut = _cut_once(buf, max_len)
                out.append(buf[:cut].strip())
                buf = buf[cut:]
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        elif _eff_len(buf) >= max_len:
            cut = _cut_once(buf, max_len)
            out.append(buf[:cut].strip())
            buf = buf[cut:]
    while _eff_len(buf) > max_len:
        cut = _cut_once(buf, max_len)
        out.append(buf[:cut].strip())
        buf = buf[cut:]
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


# ========= 调 Genie 合成 =========
def genie_pcm(text, character_name, timeout=90):
    """成功返回裸 PCM(32k/mono/s16)，失败返回 None。"""
    try:
        with session.post(f"{GENIE}/tts", json={
            "character_name": character_name,
            "text": text,
        }, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                print(f">>> genie http {r.status_code}", flush=True)
                return None
            buf = bytearray()
            for chunk in r.iter_content(16384):
                if chunk:
                    buf.extend(chunk)
            if len(buf) < SAMPLE_RATE * 2 * 0.1:
                print(f">>> genie empty/short audio ({len(buf)} bytes): {text!r}", flush=True)
                return None
            return bytes(buf)
    except Exception as e:
        print(f">>> genie error: {e}", flush=True)
        return None


# Genie 对少数日文汉字/生僻字会返回 0 字节音频。
# 这些替换只用于 TTS 发音，不改聊天里显示的文字；只对中文模型生效。
TTS_REPLACEMENTS = {
    "\u963f\u71d0": "\u963f\u6797",
    "\u71d0": "\u6797",
    "\u54b2": "\u7b11",
    "\u970a": "\u7075",
    "\u6d29": "\u6cc4",
    "\u96db": "\u96cf",
    "\u8acf": "\u8bf9",
    "\u9234": "\u94c3",
    "\u9d7a": "\u591c",
    "\u5e7d\u3005\u5b50": "\u5e7d\u5e7d\u5b50",
}


def apply_tts_replacements(text, language):
    if language != "zh":
        return text
    for src, dst in TTS_REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text


def candidates(text, language):
    replaced = apply_tts_replacements(text, language)
    if replaced != text:
        yield replaced
        yield replaced.replace("\uff1f", "\u3002").replace("\uff01", "\u3002")
        yield strip_symbols(replaced)
        yield text
    else:
        yield text
        if len(text) <= 2:
            yield text * 2
            yield text + "\u3002"
        yield text.replace("\uff1f", "\u3002").replace("\uff01", "\u3002")
        yield strip_symbols(text)


def silence_pcm(n_chars):
    """按字数估算时长，静音顶上，保证节奏不乱。"""
    secs = min(max(n_chars * 0.16, 0.3), 3.0)
    return b"\x00\x00" * int(secs * SAMPLE_RATE)


def trim_silence(pcm, threshold=300, margin_ms=20):
    """去掉每句首尾的静音，避免逐句拼接后出现明显停顿。"""
    if not pcm:
        return pcm
    total = len(pcm) // 2
    first = 0
    while first < total:
        if abs(struct.unpack_from("<h", pcm, first * 2)[0]) > threshold:
            break
        first += 1
    last = total - 1
    while last >= first:
        if abs(struct.unpack_from("<h", pcm, last * 2)[0]) > threshold:
            break
        last -= 1
    if first > last:
        return b""
    margin = int(SAMPLE_RATE * margin_ms / 1000)
    start = max(0, first - margin)
    end = min(total, last + 1 + margin)
    return pcm[start * 2:end * 2]


def synth(text, cfg):
    """逐句合成，拼成一条 PCM。单句失败降级为等长静音，不影响其它句。"""
    pcm = bytearray()
    had_speech = False
    for sent in split_sentences(text):
        got = None
        for i, cand in enumerate(candidates(sent, cfg["language"])):
            if not cand:
                continue
            got = genie_pcm(cand, cfg["character_name"])
            if got:
                break
            print(f">>> fail try[{i}]: {cand!r}", flush=True)
        if got:
            if cand != sent:
                print(f">>> tts replacement: {sent!r} -> {cand!r}", flush=True)
            got = trim_silence(got)
            if got:
                pcm.extend(got)
                had_speech = True
            else:
                print(f">>> dropped -> silence after trim: {sent!r}", flush=True)
                pcm.extend(silence_pcm(len(sent)))
        else:
            print(f">>> dropped -> silence: {sent!r}", flush=True)
            pcm.extend(silence_pcm(len(sent)))
    return bytes(pcm), had_speech


def wav_bytes(pcm, rate=SAMPLE_RATE):
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def render(raw, voice):
    """串行处理一次 TTS，避免两个请求同时切换模型。"""
    with TTS_LOCK:
        cfg = ensure_voice(voice)
        if cfg is None:
            print(f">>> voice {voice!r} failed, fallback to {DEFAULT_VOICE}", flush=True)
            cfg = ensure_voice(DEFAULT_VOICE)
        if cfg is None:
            return None
        text = clean_text(raw)
        pcm, had_speech = synth(text, cfg)
        return pcm, had_speech, cfg, text


# ========= 启动加载模型 =========
def init_character():
    try:
        # 默认只预加载默认音色，其他音色第一次使用时再加载。
        # 需要预加载多个可以设置：
        #   set GENIE_PRELOAD=VoiceA,VoiceB
        preload = os.environ.get("GENIE_PRELOAD", DEFAULT_VOICE)
        voices = [v.strip() for v in preload.split(",") if v.strip()]
        for voice in voices:
            if ensure_voice(voice):
                print(f">>> preloaded {voice}", flush=True)
            else:
                print(f">>> preload {voice} failed", flush=True)
        ensure_voice(DEFAULT_VOICE)
        print(">>> models preloaded", flush=True)
    except Exception as e:
        print(f">>> init failed: {e}", flush=True)


@app.on_event("startup")
def startup():
    init_character()


# ========= 主接口 =========
def genie_alive():
    """Check that the Genie backend is reachable (any HTTP response counts)."""
    try:
        session.get(f"{GENIE}/", timeout=2)
        return True
    except Exception:
        return False


@app.post("/v1/audio/speech")
async def speech(req: Request):
    body = await req.json()
    raw = body.get("input", "")
    voice = body.get("voice", DEFAULT_VOICE)
    t0 = time.time()

    result = await run_in_threadpool(render, raw, voice)
    if result is None:
        return Response(content="TTS backend produced no audio", status_code=503, media_type="text/plain")

    pcm, had_speech, cfg, text = result

    if not had_speech:
        alive = await run_in_threadpool(genie_alive)
        if alive:
            print(f">>> no audio (backend alive), using silence: {text!r}", flush=True)
            pcm = silence_pcm(max(len(text), 1))
            dur = len(pcm) / 2 / SAMPLE_RATE
            print(
                f">>> {time.time()-t0:.2f}s voice={cfg['voice']} model={cfg['model_key']} "
                f"lang={cfg['language']} in={len(text)}字 out={dur:.2f}s (silence)",
                flush=True,
            )
            return Response(content=wav_bytes(pcm), media_type="audio/wav")
        print(f">>> no audio: voice={voice} in={len(text)}字; Genie backend 9880 unavailable", flush=True)
        return Response(content="TTS backend produced no audio", status_code=503, media_type="text/plain")

    dur = len(pcm) / 2 / SAMPLE_RATE
    print(
        f">>> {time.time()-t0:.2f}s voice={cfg['voice']} model={cfg['model_key']} "
        f"lang={cfg['language']} in={len(text)}字 out={dur:.2f}s | {text[:36]!r}",
        flush=True,
    )
    return Response(content=wav_bytes(pcm), media_type="audio/wav")


# ========= 音色列表 =========
@app.get("/v1/audio/voices")
def voices():
    return {"voices": VOICE_ORDER}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)
