# TTS 安装指南

[English](README.md) · **简体中文** · [日本語](README.ja.md)

本目录包含两个本地 TTS 引擎的包装脚本。先弄清楚哪些是仓库自带的、哪些要自己装：

| 组件 | 谁提供 | 端口 | 说明 |
|---|---|---|---|
| `server.py` | **本仓库** | 9880 | Genie TTS 服务端，依赖 [genie-tts](https://github.com/High-Logic/Genie) 包 |
| `proxy.py` | **本仓库** | 9881 | Genie 的 OpenAI 兼容代理 |
| `api_v2.py` | GPT-SoVITS 官方 | 9883 | **不在仓库里**，需自行安装 |
| `start-gsv-api.bat` | **本仓库** | 9883 | 从 `tts/GPT-SoVITS/` 启动 GPT-SoVITS |
| `gsv_proxy.py` | **本仓库** | 9882 | GPT-SoVITS 的 OpenAI 兼容代理 |

**模型和参考音频都不包含在仓库里。** 两个引擎都需要你自己准备模型。

---

## 方案 A：Genie TTS

### 1. 安装 Python 依赖

需要 Python 3.10+：

```bat
pip install -r requirements-tts.txt
```

### 2. 准备模型

`server.py` 期望这样的目录结构：

```
tts/
├── GenieData/                              # 运行时数据
└── CharacterModels/v2ProPlus/<角色名>/
    ├── tts_models/                         # ONNX 模型文件
    └── prompt_wav/
        └── ref.wav                         # 参考音频
```

把你的角色模型放进 `CharacterModels/v2ProPlus/` 下，目录名可以自定义。

### 3. 配置音色库

```bat
mkdir VoiceLibrary
copy voices.example.json VoiceLibrary\voices.json
```

编辑 `VoiceLibrary/voices.json`，把 `model_dir` 和 `ref` 指向你的实际文件。
路径都相对于 `VoiceLibrary/`。

最小示例：

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
      "prompt": "参考音频对应的文字",
      "genie_model": "myvoice",
      "aliases": ["我的音色"]
    }
  }
}
```

> `model_dir` 和 `ref` 都是相对于 `VoiceLibrary/` 的路径。
> 如果想直接复用 `server.py` 用的模型，可以写成
> `"model_dir": "../CharacterModels/v2ProPlus/myvoice/tts_models"`。

### 4. 设置角色名

`server.py` 默认角色名是 `default`，需要改成你的目录名：

```bat
set GENIE_CHARACTER=myvoice
server.bat
```

其他可选环境变量：

| 变量 | 说明 |
|---|---|
| `GENIE_DATA_DIR` | `GenieData` 目录 |
| `GENIE_CHARACTER` | `CharacterModels/v2ProPlus` 下的目录名 |
| `GENIE_LANGUAGE` | `zh` / `ja` / `en` |
| `GENIE_REF_AUDIO` | 参考音频路径 |
| `GENIE_REF_TEXT` | 参考音频的文字 |
| `GENIE_HOST` / `GENIE_PORT` | 默认 `127.0.0.1:9880` |

### 5. 启动

```bat
server.bat           :: 9880，Genie 服务
start-proxy.bat      :: 9881，OpenAI 代理
```

### 6. 测试

```bat
curl http://127.0.0.1:9881/v1/audio/speech ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"tts-1\",\"input\":\"你好，这是一段测试。\",\"voice\":\"myvoice\"}" ^
  --output test.wav
```

### 7. 在管理器里使用

管理器会自动探测 `tts/server.bat` 和 `tts/start-proxy.bat`，
在 **插件** 页显示为两张卡（9880 / 9881）。

---

## 方案 B：GPT-SoVITS

### 1. 安装 GPT-SoVITS

`api_v2.py` 是 GPT-SoVITS 自带的，不在本仓库里：

- 官方仓库：<https://github.com/RVC-Boss/GPT-SoVITS>（MIT）
- 按它的文档安装，或者使用便携整合包

### 2. 放置引擎并启动

把整个 GPT-SoVITS 文件夹放到 `tts/GPT-SoVITS/`（或 `tts/GPT-SoVITS-版本号/`）：

```
tts/
└── GPT-SoVITS/
    ├── api_v2.py
    ├── runtime/
    └── GPT_SoVITS/
```

然后运行：

```bat
start-gsv-api.bat      :: 9883
```

它会自动找 `api_v2.py` 和 `runtime\python.exe`。找不到时会提示你放在哪里。
看到 API 就绪后再进行下一步。

### 3. 安装代理依赖

代理只需要 `fastapi`、`uvicorn`、`requests`：

```bat
pip install fastapi uvicorn requests
```

（`requirements-tts.txt` 里已经包含这些。）

### 4. 配置音色

GPT-SoVITS 使用 `gpt_ref` 和 `gpt_prompt` 字段，和 Genie 共用同一个 `voices.json`：

```json
{
  "default": "gsv_a",
  "voices": {
    "gsv_a": {
      "lang": "zh",
      "gpt_ref": "refs/gsv_a/ref.wav",
      "gpt_prompt": "参考音频对应的文字",
      "aliases": ["gsv a"]
    }
  }
}
```

一个音色同时有 `genie_model` 和 `gpt_ref` 时，两个引擎都能用它；
只有 `gpt_ref` 的音色会被 Genie 代理跳过。

### 5. 启动代理

```bat
start-gsv-proxy.bat     :: 9882
```

如果 GPT-SoVITS 不在本机，或者端口不是 9883：

```bat
set GSV_TTS_URL=http://127.0.0.1:9883/tts
```

### 6. 测试

```bat
curl http://127.0.0.1:9882/v1/audio/speech ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"tts-1\",\"input\":\"你好。\",\"voice\":\"gsv_a\"}" ^
  --output test.wav
```

### 7. 在管理器里使用

管理器会探测 `tts/start-gsv-proxy.bat`（9882），以及旁边
`GPT-SoVITS*/start-api-9883.bat`（9883），显示为两张卡。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `GenieData folder not found` | 没放 `GenieData` | 放到 `tts/GenieData/` |
| `TTS model folder not found` | 角色目录不对 | 检查 `GENIE_CHARACTER` |
| `Reference audio not found` | 没有 `ref.wav` | 放到 `prompt_wav/` 或设 `GENIE_REF_AUDIO` |
| 代理返回 502 | 后端没起来 | 先启动 9880 或 9883 |
| 管理器没显示卡片 | 路径不对 | 确保 `tts/` 在管理器目录下 |
| 声音不对 | `voices.json` 没匹配上 | 检查 `aliases` 和 `default` |

---

## 许可证

- 本目录的包装代码：与仓库一致，MIT。
- `genie-tts`：MIT。
- GPT-SoVITS：MIT。
- **模型和参考音频不在仓库里**，请自行确认其授权，不要分发你没有权利分发的音色。

---

包装代码由 **DeepSeek**（AI 助手）编写；调试与修改由 **YakumoPink35617136** 完成。
