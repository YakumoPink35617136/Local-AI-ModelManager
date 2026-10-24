# Local Model Manager

A small, dependency-free desktop manager for [llama.cpp](https://github.com/ggml-org/llama.cpp).

It lets you keep several GGUF model profiles and switch between them automatically:
SillyTavern can use one alias while an agent/coding client uses another, both through
the same llama.cpp **router** endpoint.

界面使用中文，代码和配置字段使用英文，方便直接开源和二次修改。

> This is the full English reference. Concise versions:
> [English](../README.md) · [简体中文](../README.zh.md) · [日本語](../README.ja.md).

---

## Features

- **Batch import** many GGUF files at once, or import a whole directory.
- **Named profiles**: model, vision projector, context size, KV cache type, GPU layers,
  batch sizes, extra CLI arguments, optional MTP with a configurable draft count, and an
  optional `load_mode` (`auto` / `none` / `dio`) for avoiding the GGUF file cache in RAM.
- **Client slots**: create any number of named endpoints, each mapped to a profile.
- **Router mode**: one `llama-server` process exposes all profiles as model aliases.
  A request for `chat` loads the chat profile; a request for `agent` loads the agent
  profile. Only the requested model is kept in memory (`models-max` configurable).
- **Chinese / English / Japanese UI**, switchable live. More languages can be added
  with a JSON file, without touching the code.
- **Four soft built-in themes** (Warm Night, Mist Grey, Indigo, Cream), full color
  customization, and theme import/export from JSON or INI.
- **Single mode** for machines that only want one model at a time.
- **Switch llama.cpp builds** (Vulkan / CUDA) from the model tab; the manager
  detects `llama.cpp*/llama-server.exe` folders automatically.
- **Portable config**: paths are stored relative to `config.json` whenever possible,
  so moving the whole folder keeps working.
- **Service plugin cards**: auto-detects SillyTavern and TTS, shows them as
  clickable cards with live status, and exposes TTS voice-cache scheduling.
  Any command can be added as a custom service.
- **VRAM estimate** from GGUF metadata (model + KV cache + compute buffers + CUDA
  context + a configurable desktop reserve), plus live GPU memory readings where
  available. Hybrid attention/SSM models use only their full-attention layers for KV.
- **No third-party Python packages.**

---

## Requirements

- Python **3.10+** with Tkinter (the official Windows installer includes it).
- A recent **llama.cpp** build that supports router mode:
  `llama-server --models-preset`, `--models-max`, `--models-autoload`.
  Tested with build **b11712**.
- GGUF model files. Vision models additionally need an `mmproj-*.gguf`.
- CUDA and Vulkan builds are both supported.
- Tested on RX 6750 GRE 12G, RTX 2080 Ti 22G, and RTX 4060 Ti 8G.

---

## Quick start

```text
local-model-manager/
├─ model_manager.pyw      # the application
├─ config.json            # created on first run (git-ignored)
├─ models/                # put your GGUF files here (optional)
└─ README.md
```

1. Put models in `models/`, or keep them anywhere and add the folder in **设置 → 扫描目录**.
2. Start the manager:

   Windows:
   ```bat
   启动管理器.bat
   ```
   or
   ```bat
   python model_manager.pyw
   ```

3. Open the **模型** tab and click **批量导入文件…** (or **导入整个目录…**).
4. Edit a profile to set context size, KV cache type, vision projector, etc.
5. Add client slots in **客户端连接** and select the model each slot should call.
6. Pick the UI language in the header and a theme in **外观** if needed.
7. Click **启动模型**.

On first run the manager searches for `llama-server` next to itself and in
`llama.cpp*/` folders. The build can live anywhere; if it is not detected, pick
it manually with **浏览…** in **设置**. CUDA and Vulkan builds both work.

### Linux / macOS

Not supported. This project is only tested on Windows 10/11.

---

## Router mode: automatic switching

Start the router once:

```
llama-server --models-preset router-presets.ini --models-max 1 --models-autoload ...
```

The manager generates `router-presets.ini` from your profiles. Every profile name is
also its API model name.

By default, **only profiles assigned to at least one client slot are written to the
preset**. An unassigned profile does not appear in `/v1/models` and cannot be loaded by
the router. This makes the client-slot list an expose allowlist. Turn off
**只暴露客户端选中的模型** in the client section to expose every profile (the old
behaviour). If no slot has a profile yet, the manager falls back to exposing all
profiles so a fresh setup still works.

Each client slot has an editable name and a profile. Add as many slots as you need.
Point every client at the same base URL, and use its slot's profile name as the model:

| Slot | Base URL | Model |
|---|---|---|
| SillyTavern | `http://127.0.0.1:8081/v1` | `chat` profile name |
| Agent / coding tool | `http://127.0.0.1:8081/v1` | `agent` profile name |
| Anything else | `http://127.0.0.1:8081/v1` | another profile name |

When the requested alias is not loaded, llama.cpp loads it on demand. With
`models-max = 1`, the previous model is unloaded first, which is the right choice
for a 12–16 GB GPU. If your VRAM can hold both models, set `models-max = 2` to avoid
reloading on every switch.

> **This is an expose allowlist, not per-client authentication.** If two slots use two
> different profiles, both clients still see both profiles in `/v1/models` and can call
> either one. Strict per-client isolation requires a small API-key gateway in front of
> the router.

> Router mode creates a child `llama-server` process per loaded model.
> The first request after a switch can take several seconds while the model loads.

### Why not two separate ports?

Two independent servers would keep both models in VRAM permanently. Router mode
keeps one process and one port, and only loads what the current request needs.

---

## Switching Vulkan / CUDA builds

Put each llama.cpp build in its own folder, for example:

```text
llama.cpp-vulkan/llama-server.exe
llama.cpp-cuda13/llama-server.exe
```

The **服务器版本 / Server build** dropdown on the model tab lists every
`llama.cpp*/llama-server.exe` found next to the manager. Selecting one updates
`server_path`; press **重启** to apply it. Custom paths can be added with
**浏览…** and are remembered in `server_builds`.

For a Windows CUDA build, download both the `llama-*-bin-win-cuda-*.zip` and the
matching `cudart-llama-bin-win-cuda-*.zip`, then extract both into the same folder.
Vulkan builds need only their own zip.

### GPU compatibility

| Build | Works with |
|---|---|
| Vulkan | AMD Radeon (RX 5000/6000/7000 and newer); NVIDIA as a fallback |
| CUDA 13 | NVIDIA RTX 20 / 30 / 40 / 50 (Turing and newer) |


- RTX 50 series **requires CUDA 13**; older CUDA builds do not target Blackwell.
- RTX 20/30/40/50 can all use CUDA 13. GTX 10 (Pascal) and older use Vulkan;
  CUDA 13 dropped Pascal support. A CUDA 12 build can be added manually if you
  need an old driver, but it is not bundled.
- Install the latest NVIDIA driver before switching to a CUDA build.
- 8 GB cards (e.g. RTX 5060) should use the bundled `qwen35-4b-q8` profile
  (4B Q8, 16K context, about 5.2 GB) or a 9B Q4/Q5 profile at 8–16K context.
- 12 GB cards (e.g. RTX 4070) can run the 9B Q6 profile at 32–57K.
- The 27B profiles target 22 GB and should not be loaded on 8/12 GB cards.
- Only the Vulkan build was physically tested on the current AMD machine. NVIDIA
  builds were validated as executables (`--version`); test with a small model first.

---

## Single mode

Choose **单模型** in the 模型 tab, select a profile in the table, and press
**启动模型**. The selected profile is launched with the usual llama.cpp arguments.
This is useful for debugging one model or for GPUs that cannot run the router build.

---

### Scan folders

The manager recursively scans the configured folders for non-mmproj `.gguf` models
and `mmproj` files. It only reads file names, sizes and GGUF header metadata; it
never loads model tensors, moves files, or deletes anything. Add folders in
**设置 → 扫描目录**, then press **重新扫描** or `F5`.

---

### VRAM estimate

The estimate includes model weights, KV cache for the full-attention layers, vision
projector (when offloaded), llama.cpp compute buffers, CUDA context, MTP overhead and a
configurable desktop reserve (`vram_reserve_gb`, default `0.95`). Hybrid attention/SSM
models only allocate KV for their full-attention blocks, including the final block.
Lower `batch` / `ubatch` to shrink the compute-buffer part; the desktop reserve can be
changed in **设置 → 系统预留 GB**.

---

## Configuration

`config.json` is created automatically. All paths can be absolute or relative to the
folder containing `config.json`. Use **设置 → 导出配置 / 导入配置** to share a setup.

```json
{
  "server_path": "../llama.cpp/llama-server.exe",
  "server_builds": [
    "../llama.cpp-vulkan/llama-server.exe",
    "../llama.cpp-cuda13/llama-server.exe"
  ],
  "scan_dirs": ["..", "models"],
  "mode": "router",
  "host": "127.0.0.1",
  "port": 8081,
  "models_max": 1,
  "expose_assigned_only": true,
  "api_key": "",
  "vram_gb": 0,
  "vram_reserve_gb": 0.95,
  "language": "zh",
  "ui": {"theme": "暖夜", "colors": {}},
  "defaults": {
    "ctx": 32768,
    "kvk": "f16",
    "kvv": "f16",
    "ngl": 999,
    "batch": 2048,
    "ubatch": 512,
    "parallel": 1,
    "vision_cpu": true,
    "mtmd_batch": 256,
    "extra": "",
    "mtp": false,
    "mtp_enabled": true,
    "mtp_draft_n": 2,
    "load_mode": "auto"
  },
  "profiles": {},
  "clients": [
    {"name": "Chat", "profile": "chat"}
  ],
  "services": [],
  "dismissed_plugins": [],
}
```

`clients` is an ordered list of slots. Names are free-form and can be duplicated;
each `profile` must match a key in `profiles` or be `""`.

A profile:

```json
{
  "model": "../models/Qwen3.5-9B-Q6_K.gguf",
  "mmproj": "../models/mmproj-Qwen3.5-9B-BF16.gguf",
  "ctx": 32768,
  "kvk": "q8_0",
  "kvv": "q8_0",
  "ngl": 999,
  "batch": 2048,
  "ubatch": 512,
  "parallel": 1,
  "vision_cpu": true,
  "mtmd_batch": 256,
  "extra": "",
  "mtp": false,
  "mtp_enabled": true,
  "mtp_draft_n": 2,
  "load_mode": "auto"
}
```

| Field | Meaning |
|---|---|
| `ctx` | `--ctx-size` |
| `kvk`, `kvv` | `--cache-type-k` / `--cache-type-v`: `f16`, `q8_0`, `q4_0` |
| `ngl` | `--n-gpu-layers` (`999` = all layers) |
| `vision_cpu` | add `--no-mmproj-offload` so the vision encoder runs on CPU |
| `mtp` | detected automatically from the GGUF `nextn` head; stored for reference |
| `mtp_enabled` | use MTP when the GGUF supports it (default `true`) |
| `mtp_draft_n` | `--spec-draft-n-max`, number of draft tokens (default `2`) |
| `extra` | extra llama.cpp flags, appended verbatim |
| `load_mode` | `auto` (mmap), `none` (no file cache), or `dio` (DirectIO). `none`/`dio` can reduce the RAM used by the memory-mapped GGUF, at the cost of slower model loading. |

`extra` flags are also converted into router preset keys on a best-effort basis.

---

## Localization and editing the UI

The language selector in the header switches the whole UI immediately and stores the
choice in `config.json` (`"language": "zh" | "en" | "ja"`).

Adding a language requires no code changes:

1. Copy `locales/en.json` to `locales/xx.json`.
2. Set `"language_name"` to the name shown in the selector (for example `"Français"`).
3. Translate the values in `"strings"`. Keys are the Chinese source strings; missing
   keys fall back to Chinese.

Bundled Chinese is built into the program. User locale files in the folder next to
`config.json` override bundled files with the same language code.

Other extension points:

- Add or edit model profiles in the GUI; no config editing required.
- Add any command as a service (start/stop/open) in the **插件** tab.
- Use **额外参数** for new llama.cpp flags without changing the app.
- Relative paths keep a copied folder working; **导出配置 / 导入配置** moves a setup.

---

## Appearance and themes

The **外观** tab provides four soft built-in themes:

| Theme | Style |
|---|---|
| 暖夜 / Warm Night | warm dark rose |
| 雾灰 / Mist Grey | muted slate |
| 青黛 / Indigo | soft teal |
| 米灰 / Cream | low-glare warm light |

Every color is editable: background, card, input field, text, muted text, accent,
danger, success, border, log background and log text. **应用颜色** saves the palette
in `config.json` under `"ui"`; **保存主题…** writes a reusable theme file.

Theme files may be JSON or INI. Import them from **导入主题…** or drop them into the
`themes/` folder next to `config.json`:

```ini
[theme]
name = My Theme
base = 暖夜
dark = true
accent = #88aa99
bg = #202020
```

```json
{
  "name": "My Theme",
  "base": "暖夜",
  "dark": true,
  "bg": "#202020",
  "accent": "#88aa99"
}
```

`base` is optional; omitted colors inherit from that theme. Colors must be `#rrggbb`.
Imported files are normalized and stored as JSON in `themes/`.

---

## Plugins and services

The **插件** tab turns local services into clickable cards. On first run the manager
detects `SillyTavern/Start-Portable.bat`, `Genie-TTS GUI/server.bat`,
`Genie-TTS GUI/start-proxy.bat` and `GPT-SoVITS*/start-api-*.bat` next to the
manager or its parent folders. Each card shows its port, address, live status and
start/stop/edit buttons. Clicking the card body starts it (or opens it if it is
already running).

The built-in TTS cards also expose scheduling options:

| Option | Meaning |
|---|---|
| `model_cache` | How many character voice models stay in RAM at once (`GENIE_MODEL_CACHE`, default `1`) |
| `preload` | Comma-separated voices to preload (`GENIE_PRELOAD`, optional) |
| `auto_proxy` | Start/stop the TTS proxy together with the TTS server |
| `open_after_start` | SillyTavern: open the browser after launch |

Options are saved in `config.json` and are passed to the service as environment
variables when it starts. Changes to the TTS cache take effect after restarting TTS.
**重新检测** adds newly installed plugins without creating duplicates.

Any other program can still be added as a custom service:

```json
{
  "name": "My tool",
  "port": 9000,
  "command": "C:/tools/start.bat",
  "url": "http://127.0.0.1:9000",
  "process": "python",
  "stop": "",
  "plugin": "custom",
  "options": {}
}
```

- **启动/打开** launches the command (or opens the URL if the port is already open).
- **停止** runs `stop` if provided; otherwise it only kills a process whose name
  contains `process`. This prevents accidentally killing an unrelated program.
- Deleting a built-in card remembers the choice in `dismissed_plugins`; **重新检测**
  clears that list.
- On Linux/macOS use a `.sh` command or a full command line.

---

## Security

- The default host is `127.0.0.1`.
- Router mode prints a warning because an unauthenticated router must not be exposed
  to the internet. If you bind to a LAN address, set an **API Key** in 设置.
- The manager never kills a process on a port unless the process name matches
  `llama-*` (model server) or the configured service keyword.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Port 8081 is used by ...` | Stop the old server, or change the port in 设置. |
| Router starts but the model never loads | Check the log tab. Wrong model path is the usual cause. |
| First request is very slow | Expected: the model is being loaded. Set `models-max = 2` if VRAM allows. |
| Image encoding crashes the GPU driver | Enable **Vision encoder on CPU**. |
| MTP status shows “not supported” | The GGUF has no `nextn` tensors, so MTP cannot be enabled. |
| MTP is supported but slower | Uncheck **Enable MTP** or lower **Draft tokens** in the profile; acceptance depends on the model and context. |
| Stats show “未运行” although a process exists | An API key may be set; the manager sends it automatically. Ensure it matches. |

---

## Development

```bash
python model_manager.pyw --selftest
```

The self-test prints detected paths, models, profiles, the generated router preset
and GPU information without starting a server.

The code is a single file on purpose: easy to copy, easy to audit, no packaging step.
Pull requests that keep it dependency-free are welcome.

---

## License

MIT. See [LICENSE](../LICENSE).

Not affiliated with llama.cpp, SillyTavern, or any model provider.
