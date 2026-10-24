# TTS インストールガイド

[English](README.md) · [简体中文](README.zh.md) · **日本語**

このフォルダーには、2 つのローカル TTS エンジン用のラッパーが入っています。
まず、どれが同梱でどれが別途必要なのかを確認してください：

| コンポーネント | 提供元 | ポート | 説明 |
|---|---|---|---|
| `server.py` | **このリポジトリ** | 9880 | Genie TTS サーバー。`genie-tts` に依存 |
| `proxy.py` | **このリポジトリ** | 9881 | Genie 用 OpenAI 互換プロキシ |
| `api_v2.py` | GPT-SoVITS 公式 | 9883 | **同梱されていません**。別途インストール |
| `start-gsv-api.bat` | **このリポジトリ** | 9883 | `tts/GPT-SoVITS/` から GPT-SoVITS を起動 |
| `gsv_proxy.py` | **このリポジトリ** | 9882 | GPT-SoVITS 用 OpenAI 互換プロキシ |

**モデルと参照音声は同梱されていません。** どちらのエンジンも、モデルは自分で用意します。

---

## 方法 A：Genie TTS

### 1. Python の依存関係

Python 3.10 以上が必要です：

```bat
pip install -r requirements-tts.txt
```

### 2. モデルの配置

`server.py` は次の構成を想定しています：

```
tts/
├── GenieData/                              # 実行時データ
└── CharacterModels/v2ProPlus/<キャラ名>/
    ├── tts_models/                         # ONNX モデル
    └── prompt_wav/
        └── ref.wav                         # 参照音声
```

キャラクターモデルを `CharacterModels/v2ProPlus/` の下に置きます。
フォルダー名は自由です。

### 3. 音声ライブラリの設定

```bat
mkdir VoiceLibrary
copy voices.example.json VoiceLibrary\voices.json
```

`VoiceLibrary/voices.json` を編集し、`model_dir` と `ref` を実際のファイルに
合わせます。パスは `VoiceLibrary/` からの相対パスです。

最小例：

```json
{
  "default": "myvoice",
  "genie_default": "myvoice",
  "models": {
    "myvoice": {
      "character_name": "myvoice",
      "model_dir": "genie_models/myvoice/tts_models",
      "language": "zh"
    }
  },
  "voices": {
    "myvoice": {
      "lang": "zh",
      "ref": "refs/myvoice/ref.wav",
      "prompt": "参照音声の文字起こし",
      "genie_model": "myvoice",
      "aliases": ["マイボイス"]
    }
  }
}
```

> `model_dir` と `ref` は `VoiceLibrary/` からの相対パスです。
> `server.py` と同じモデルを再利用する場合は
> `"model_dir": "../CharacterModels/v2ProPlus/myvoice/tts_models"` と書けます。

### 4. キャラクター名の指定

`server.py` の既定値は `default` です。自分のフォルダー名に変更します：

```bat
set GENIE_CHARACTER=myvoice
server.bat
```

その他の環境変数：

| 変数 | 説明 |
|---|---|
| `GENIE_DATA_DIR` | `GenieData` フォルダー |
| `GENIE_CHARACTER` | `CharacterModels/v2ProPlus` 下のフォルダー名 |
| `GENIE_LANGUAGE` | `zh` / `ja` / `en` |
| `GENIE_REF_AUDIO` | 参照音声のパス |
| `GENIE_REF_TEXT` | 参照音声の文字起こし |
| `GENIE_HOST` / `GENIE_PORT` | 既定 `127.0.0.1:9880` |

### 5. 起動

```bat
server.bat           :: 9880、Genie サーバー
start-proxy.bat      :: 9881、OpenAI プロキシ
```

### 6. テスト

```bat
curl http://127.0.0.1:9881/v1/audio/speech ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"tts-1\",\"input\":\"こんにちは。\",\"voice\":\"myvoice\"}" ^
  --output test.wav
```

### 7. マネージャーでの使用

マネージャーは `tts/server.bat` と `tts/start-proxy.bat` を自動検出し、
**插件** タブに 2 枚のカード（9880 / 9881）として表示します。

---

## 方法 B：GPT-SoVITS

### 1. GPT-SoVITS のインストール

`api_v2.py` は GPT-SoVITS 本体に含まれるもので、このリポジトリにはありません：

- 公式リポジトリ：<https://github.com/RVC-Boss/GPT-SoVITS>（MIT）
- 公式の手順、またはポータブル版を使ってインストールします

### 2. エンジンの配置と起動

GPT-SoVITS のフォルダー全体を `tts/GPT-SoVITS/`（または
`tts/GPT-SoVITS-<バージョン>/`）に置きます：

```
tts/
└── GPT-SoVITS/
    ├── api_v2.py
    ├── runtime/
    └── GPT_SoVITS/
```

このフォルダーから起動します：

```bat
start-gsv-api.bat      :: 9883
```

`api_v2.py` と `runtime\python.exe` は自動で探します。見つからない場合は、
置く場所を表示します。API が起動してから次へ進みます。

### 3. プロキシの依存関係

プロキシに必要なのは `fastapi`、`uvicorn`、`requests` だけです：

```bat
pip install fastapi uvicorn requests
```

（`requirements-tts.txt` に含まれています。）

### 4. 音声の設定

GPT-SoVITS は `gpt_ref` と `gpt_prompt` を使います。`voices.json` は Genie と共通です：

```json
{
  "default": "gsv_a",
  "voices": {
    "gsv_a": {
      "lang": "zh",
      "gpt_ref": "refs/gsv_a/ref.wav",
      "gpt_prompt": "参照音声の文字起こし",
      "aliases": ["gsv a"]
    }
  }
}
```

`genie_model` と `gpt_ref` の両方を持つ音声は、両方のエンジンで使えます。
`gpt_ref` だけの音声は Genie プロキシではスキップされます。

### 5. プロキシの起動

```bat
start-gsv-proxy.bat     :: 9882
```

GPT-SoVITS が別のホストにある、またはポートが違う場合：

```bat
set GSV_TTS_URL=http://127.0.0.1:9883/tts
```

### 6. テスト

```bat
curl http://127.0.0.1:9882/v1/audio/speech ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"tts-1\",\"input\":\"こんにちは。\",\"voice\":\"gsv_a\"}" ^
  --output test.wav
```

### 7. マネージャーでの使用

マネージャーは `tts/start-gsv-proxy.bat`（9882）と、隣にある
`GPT-SoVITS*/start-api-9883.bat`（9883）を検出し、2 枚のカードとして表示します。

---

## よくある問題

| 症状 | 原因 | 対処 |
|---|---|---|
| `GenieData folder not found` | `GenieData` がない | `tts/GenieData/` に置く |
| `TTS model folder not found` | キャラクターフォルダーが違う | `GENIE_CHARACTER` を確認 |
| `Reference audio not found` | `ref.wav` がない | `prompt_wav/` に置くか `GENIE_REF_AUDIO` を設定 |
| プロキシが 502 を返す | バックエンドが起動していない | 先に 9880 または 9883 を起動 |
| カードが表示されない | パスが違う | `tts/` がマネージャーの下にあるか確認 |
| 声が違う | `voices.json` が一致していない | `aliases` と `default` を確認 |

---

## ライセンス

- このフォルダーのラッパーコード：リポジトリと同じ MIT。
- `genie-tts`：MIT。
- GPT-SoVITS：MIT。
- **モデルと参照音声は同梱されていません。** 各自のライセンスを確認し、
  再配布する権利のない音声を配布しないでください。

---

ラッパーは **DeepSeek**（AI アシスタント）が実装し、**YakumoPink35617136** が
デバッグと修正を行いました。
