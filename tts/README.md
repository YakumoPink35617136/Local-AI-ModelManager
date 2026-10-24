# TTS installation guide

**English** · [简体中文](README.zh.md) · [日本語](README.ja.md)

Small wrappers for two local TTS engines.

| Component | Provided by | Port | Notes |
|---|---|---|---|
| `server.py` | **this repo** | 9880 | Genie TTS server; needs the [genie-tts](https://github.com/High-Logic/Genie) package |
| `proxy.py` | **this repo** | 9881 | OpenAI-compatible proxy for Genie |
| `api_v2.py` | GPT-SoVITS upstream | 9883 | **not bundled**; install GPT-SoVITS separately |
| `start-gsv-api.bat` | **this repo** | 9883 | Launches GPT-SoVITS from `tts/GPT-SoVITS/` |
| `gsv_proxy.py` | **this repo** | 9882 | OpenAI-compatible proxy for GPT-SoVITS |

> **Models are not included.** Voice models are large and carry their own terms.
> This repository ships only the wrappers; you provide the models and reference
> audio yourself.

---

## 1. Install dependencies

```bat
pip install -r requirements-tts.txt
```

Tested with Python 3.13. `genie-tts` is MIT-licensed.

---

## 2. Provide a model

`server.py` expects this layout:

```
tts/
├── GenieData/                                # shared runtime data
└── CharacterModels/v2ProPlus/<character>/
    ├── tts_models/                           # ONNX model files
    └── prompt_wav/
        └── ref.wav                           # reference audio
```

Set the character name:

```bat
set GENIE_CHARACTER=default
```

Optional environment variables:

| Variable | Meaning |
|---|---|
| `GENIE_DATA_DIR` | `GenieData` folder |
| `GENIE_CHARACTER` | folder name under `CharacterModels/v2ProPlus` |
| `GENIE_LANGUAGE` | `zh` / `ja` / `en` |
| `GENIE_REF_AUDIO` | reference wav, relative to `server.py` or absolute |
| `GENIE_REF_TEXT` | transcript of the reference wav |
| `GENIE_HOST` / `GENIE_PORT` | default `127.0.0.1:9880` |
| `GENIE_WORKERS` | default `1` |

---

## 3. Optional voice library for the proxy

`proxy.py` reads `VoiceLibrary/voices.json`. Copy the example first:

```bat
copy voices.example.json VoiceLibrary\voices.json
```

Structure:

```json
{
  "default": "default",
  "genie_default": "default",
  "models": {
    "default": {
      "character_name": "default",
      "model_dir": "genie_models/default/tts_models",
      "language": "zh"
    }
  },
  "voices": {
    "default": {
      "lang": "zh",
      "ref": "refs/default/ref.wav",
      "prompt": "Transcript of the reference audio.",
      "genie_model": "default",
      "aliases": ["default"]
    }
  }
}
```

All paths are resolved relative to `VoiceLibrary/`.

---

## 4. Run

```bat
server.bat          :: Genie TTS server, port 9880
start-proxy.bat     :: Genie proxy, port 9881

start-gsv-proxy.bat :: GPT-SoVITS proxy, port 9882
```

The model manager detects this folder automatically and shows the matching
cards. You can also start them by hand.

---

## 5. GPT-SoVITS

Put the whole GPT-SoVITS folder next to this README:

```
tts/
└── GPT-SoVITS/          (or GPT-SoVITS-<version>/)
    ├── api_v2.py
    ├── runtime/
    └── GPT_SoVITS/
```

Start the API server from this folder:

```bat
start-gsv-api.bat      :: 9883
```

It finds `api_v2.py` and `runtime\python.exe` automatically. If nothing is
found, it prints where to put the folder.

Then start the proxy:

```bat
start-gsv-proxy.bat    :: 9882
```

| Variable | Default | Meaning |
|---|---|---|
| `GSV_TTS_URL` | `http://127.0.0.1:9883/tts` | GPT-SoVITS API endpoint |
| `VOICE_LIBRARY` | `tts/VoiceLibrary` | folder containing `voices.json` |

The proxy uses the same `voices.json` schema as the Genie proxy. Voices whose
`genie_model` is missing are treated as GPT-SoVITS-only and are skipped by the
Genie proxy.

The GPT-SoVITS engine itself is **not included**. Install it from
[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) (MIT).

---

## API

The proxy exposes an OpenAI-compatible speech endpoint:

```http
POST http://127.0.0.1:9881/v1/audio/speech
{
  "model": "tts-1",
  "input": "Hello.",
  "voice": "default"
}
```

---

## Licensing

- Wrapper code in this folder: same license as the parent repository (MIT).
- `genie-tts`: MIT.
- Voice models and reference audio: **not included**; check the terms of
  whatever model you download. Do not redistribute voices you do not own.

---

Wrappers written by **DeepSeek** (AI assistant); debugging and modifications by
**YakumoPink35617136**.
