# 🎬 MiniMax-H3 WebUI — Video Generation Workbench

> A self-hosted video-generation web UI built on **ComfyUI + MiniMax-H3**. Merges three views into one: Chat / Overview / Studio, with workspaces, history management, real step-level progress, reference reuse and video continuation.

**🌐 Language: [中文](README.md) | English**

![UI theme](https://img.shields.io/badge/UI-Dark%20Slate%20Indigo-6366f1)
![Backend](https://img.shields.io/badge/backend-aiohttp-blue)
![License](https://img.shields.io/badge/license-MIT-green)

![WebUI main UI](docs/screenshots/ui-screenshot.png)

> Dark-themed merged 3-view UI (Chat / Overview / Studio, switched from the top bar).

![r2v prompt editor](docs/screenshots/composer-editor.png)

> Fullscreen r2v prompt editor example: six-section split view plus per-token highlight in whole mode (out-of-range red / skipped-index amber), launchable from Chat or Studio.

---

## ✨ Features

| Feature | Description |
| --- | --- |
| 🗂 **Merged 3-view UI** | Chat (chat-style generation), Overview (works grid) and Studio (workbench) combined into one page, switched from the top bar |
| 📁 **Workspace management** | Each workspace has its own directory (`generations.json` + `media/`); create / switch / delete |
| 📜 **History records** | Every workspace keeps full generation records (prompt, params, reference images/audio, video, duration), with search |
| ⚡ **Real progress** | WebSocket bridging to ComfyUI; SSE pushes **step-level** progress (e.g. 3/8 steps) — no fake timer progress |
| 🖼 **Reference reuse** | One-click reuse of history params with reference images/audio restored; auto re-upload after replacing an image |
| ⏩ **Video continuation** | Extracts the **last frame** of an existing video as the first-frame reference of the next segment for continuous storytelling |
| 🔀 **Generation modes** | **i2v first frame** (default, one image as the first frame) / **t2v text-only** (no assets at all, pure text → video+audio) / **last frame** (l2v, one image as the video's ending frame) / **first + last** (fl2v, two images; the last frame is auto-aligned to the first frame's canvas) / **r2v multi-asset** — five modes sharing one parameter panel |
| 📐 **Resolution control** | 15 presets at 32-multiple steps (16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / 2:3), or **native mode** keeping source aspect + a scale slider (10–100%) |
| 🔧 **Turbo LoRA auto-matching** | Automatically picks the 4-step / 8-step turbo LoRA by step count; custom LoRA files also supported |
| 🔊 **Native audio** | Outputs videos with sound directly (H3 native audio); draggable progress bar in the browser (Range streaming) |
| 🎚 **Audio role labels** | Each reference audio can be tagged **voice / as-is / music**: voice = learn that timbre and speak new lines, as-is = reuse this exact audio, music = use as the background bed. Prompt semantics only — graph wiring is unchanged; tagging as-is/music triggers a reminder to add the `[audio reuse]` prefix |
| 🚀 **Lazy history** | Studio history list rendered in batches (40 per batch) + thumbnails lazy-loaded via IntersectionObserver; smooth even with hundreds of records |
| 🎯 **Ref2VA multi-asset reference** | Task type switchable `i2v single image / t2v text-only / r2v multi-asset` (the i2v family also has frame slots `first / last / first+last`). r2v accepts **up to 9 reference images, 3 reference videos and 3 reference audio clips** (12 in total across all kinds), referenced by position in the prompt via `<Picture N>` / `<Video N>` / `<Audio N>`; videos also work on their own (continuation / editing), and their original soundtracks are wired in by default with a one-click toggle; built-in **six-section prompt editor** (fullscreen whole-mode editing + six-section split, per-token out-of-range highlighting, quick insert of asset tokens & skeleton, openable from Chat / Studio; the example library suggests presets based on your asset mix), running the core node chain (SigmaShift 12/3 + res_multistep/simple + SaveVideo) with reusable Turbo LoRA |
| 💾 **Zero data dependencies** | Frontend is pure vanilla JS (no framework, no build); backend only needs aiohttp / Pillow / PyAV (bundled with ComfyUI) |

---

## 🧱 Architecture

```
┌─────────────────────────────┐
│  Browser (index.html)       │  vanilla JS + FontAwesome CDN
│  Chat / Overview / Studio    │
└──────────────┬──────────────┘
               │ HTTP / SSE
┌──────────────▼──────────────┐
│  WebUI backend (webui/server.py) │  aiohttp @ 127.0.0.1:8080
│  · workspace / history / media   │
│  · resolution preprocess (32-mult)│
│  · PyAV frame extraction        │
│  · WebSocket bridge to ComfyUI  │
└──────────────┬──────────────┘
               │ HTTP + WS
┌──────────────▼──────────────┐
│  ComfyUI @ 127.0.0.1:8188    │
│  MiniMax-H3 custom nodes     │
│  (T8 conditioning / DualClock│
│   sampler / AV decode / VHS) │
└─────────────────────────────┘
```

Key design points:

- **Steps really take effect**: the DualClock sampler maps `steps` directly to `samplerMax` (4→4, 8→8, 12→12 steps) — not a fake UI parameter.
- **H3 canvas constraints**: width/height are forced to 32-multiples (`CANVAS_MULT=32`), area capped at `1920×1088` (`MAX_PIXELS`), duration rounded up to the `17n+5` frame grid (24 fps).
- **No backend state loss**: SSE event stream `{type: progress|status|done|error}` with keepalive auto-reconnect on disconnect.

---

## 📋 Requirements

| Dependency | Notes |
| --- | --- |
| **ComfyUI** | Any recent version (Windows desktop build includes `python_embeded`) |
| **MiniMax-H3 custom nodes** | Install the official H3 nodes under `ComfyUI/custom_nodes/` |
| **Model files** | See the model list below; place them in the matching `ComfyUI/models/` directories |
| **Python libraries** | Uses ComfyUI's bundled python (includes aiohttp / Pillow / PyAV / requests) — **no separate environment needed** |

### Model list

| Purpose | File | Directory |
| --- | --- | --- |
| DiT main model (pick one) | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` (pruned, fast)<br>`minimax_h3_fl2va_int8_convrot.safetensors` (full, quality) | `models/unet/` |
| Video VAE | `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` |
| Audio VAE | `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` |
| Text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/clip/` |
| Turbo LoRA (optional) | `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` (4-step)<br>`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` (8-step) | `models/loras/` |
| **Ref2VA main model** (r2v multi-asset, optional) | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` (or fp8_scaled variants; auto-discovered at startup from any `minimax_h3_ref2va*.safetensors` under `models/diffusion_models/`) | `models/diffusion_models/` |

For model & node installation details, see the official MiniMax-H3 repository: **[MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)** (please comply with its model license).

---

## 🚀 Quick Start (Windows)

### Option 1: One-click start (recommended)

```bat
run_webui.bat            :: default ComfyUI root
run_webui.bat D:\ComfyUI :: specify your ComfyUI root
```

The script automatically: **picks a usable port first** (default 8080; if it is reserved by Windows or already taken it moves on to 8081, 8082, …) → opens the browser if a WebUI already answers on that port → checks 8188 (starts ComfyUI in the background and waits until ready if not running) → starts the WebUI → opens the browser. **Every step prints a log line** (port picking, ComfyUI check, start command and port), so it never waits silently.

> The ComfyUI root can also be set via the `H3_COMFY_ROOT` environment variable; default `D:\ComfyUI`.
>
> On Windows, Hyper-V / WSL / winnat reserve “excluded port ranges”; a port inside one (commonly 8080) cannot be bound even when nothing is listening, failing with `winerror 10013`. The launcher probes with a real bind and shifts to the next free port; you can also pin one with `set H3WEBUI_PORT=8081`.

### Option 2: Manual start

```bat
:: Terminal 1: start ComfyUI (your usual way)
cd /d D:\ComfyUI
python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build

:: Terminal 2: start the WebUI
cd /d <this repo>
D:\ComfyUI\python_embeded\python.exe webui\server.py
```

Open **http://127.0.0.1:8080** (or whichever port the launcher picked) in your browser.

### Usage flow

1. Select / create a **workspace** at the top (each workspace keeps independent history).
2. Upload a **reference image** (drag & drop or click; r2v can also add reference videos and audio clips).
3. Write the **prompt** (Chinese supported) and pick model / steps / duration / resolution mode. For r2v, open the **fullscreen editor** (whole or six-section split; insert `<Picture N>` / `<Video N>` / `<Audio N>` asset tokens directly in the text).
4. Click Generate; the right side shows **step progress** live, auto-saved and playable when done.
5. In the **Studio** history list, pick an old work → **⏩ Continue** (last frame → first frame) or **♻️ Reuse params** (reference images/audio included).

---

## 🚀 Quick Start (Linux / macOS)

### Option 1: One-click start (recommended)

```bash
./run_webui.sh                       # default ComfyUI root (~/ComfyUI)
./run_webui.sh /path/to/ComfyUI      # specify your ComfyUI root
./run_webui.sh --port 9000           # specify the WebUI port
```

The script automatically: checks 8080 (opens the browser if already running) → checks 8188 (starts ComfyUI in the background and waits until ready if not running) → starts the WebUI → opens `http://127.0.0.1:8080`.

> The ComfyUI root can also be set via the `H3_COMFY_ROOT` environment variable; default `~/ComfyUI`. If ComfyUI runs in a conda env, `conda activate <env>` first, or set `H3_PYTHON=/path/to/env/bin/python`.

Stop / status / logs:

```bash
./run_webui.sh --stop      # stop both WebUI and ComfyUI
./run_webui.sh --status    # show running status
./run_webui.sh --logs      # tail logs
```

### Option 2: Manual start

```bash
# Terminal 1: start ComfyUI (your usual way)
cd /path/to/ComfyUI
/path/to/python main.py --listen 127.0.0.1 --port 8188

# Terminal 2: start the WebUI
cd <this repo>
COMFYUI_URL=http://127.0.0.1:8188 \
COMFYUI_INPUT=/path/to/ComfyUI/input \
COMFYUI_OUTPUT=/path/to/ComfyUI/output \
/path/to/python webui/server.py
```

Open **http://127.0.0.1:8080** in your browser.

> On Linux the backend cannot auto-detect the ComfyUI dirs from the Python path, so set `COMFYUI_INPUT` / `COMFYUI_OUTPUT` explicitly when starting manually.

---

## ⚙️ Configuration

Everything is overridden by **environment variables** — no config file:

| Variable | Default | Description |
| --- | --- | --- |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI address |
| `COMFYUI_INPUT` | auto-detected (`<python>/../../ComfyUI/input`) | ComfyUI input dir (uploaded images / extracted frames are copied here) |
| `COMFYUI_OUTPUT` | auto-detected (`<python>/../../ComfyUI/output`) | ComfyUI output dir |
| `H3WEBUI_HOST` | `127.0.0.1` | WebUI listen host |
| `H3WEBUI_PORT` | `8080` | WebUI listen port; `run_webui.bat` starts here and shifts to the next bindable port if needed |
| `H3_COMFY_ROOT` | `D:\ComfyUI` | ComfyUI root (used only by `run_webui.bat`) |
| `H3_NO_BROWSER` | unset | (`run_webui.bat` only) when set to `1`, do not open a browser — just print the URL |

---

## 🔌 REST API

All under the `/api` prefix, JSON:

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/workspaces` | List workspaces (with generation counts) |
| POST | `/api/workspaces` | Create workspace `{name}` |
| DELETE | `/api/workspaces/{name}` | Delete workspace |
| GET | `/api/workspaces/{name}/generations` | History list (newest first) |
| DELETE | `/api/workspaces/{name}/generations/{gid}` | Delete one record and its media |
| POST | `/api/workspaces/{name}/upload-image` | Upload a reference image (base64 data-url) → filename (i2v) |
| POST | `/api/workspaces/{name}/upload-media` | Multipart upload of reference assets `{type: image\|audio\|video, file}` → `{filename, kind, duration_s, width, height}` (r2v; PIL check for images, PyAV check for audio/video; audio/video clips limited to 2–15s each, videos additionally return `has_audio`) |
| POST | `/api/workspaces/{name}/probe-media` | PyAV-probe an uploaded media `{filename}` → `{kind, duration_s, width, height, ok}` |
| POST | `/api/workspaces/{name}/preview-resolution` | Preview the backend's actual resolution `{image, res_mode, custom_w, custom_h, native_scale}` |
| POST | `/api/workspaces/{name}/extract-frame` | Extract a frame `{video, position: "first"\|"last"\|0~1}` → PNG filename (for continuation) |
| POST | `/api/workspaces/{name}/generate` | Submit generation. i2v/t2v family: `{prompt, params:{task_type:"i2v"\|"t2v"}, image, image_last, frame_mode}` (`frame_mode` = first/last/both, `image_last` only valid with both, t2v takes no reference image); r2v: `{prompt, params:{task_type:"r2v"}, refs:{images, videos, audios, use_video_audio}}` → `{job_id, gen_id, width, height, length}` |
| GET | `/api/jobs/{job_id}/events` | SSE progress stream: `progress` (value/max/node) / `status` / `done` (video/audio/duration) / `error` |
| POST | `/api/interrupt` | Interrupt a job `{job_id}` |
| GET | `/api/workspaces/{name}/media/{file}` | Stream media (Range supported) |
| GET | `/api/comfyui/status` | ComfyUI status + presets (durations / res_presets / max_pixels / ref2va_model / ref2va_present) |

---

## 📁 Project Structure

```
.
├── run_webui.bat            # one-click launcher (starts ComfyUI + WebUI)
├── webui/
│   ├── server.py            # aiohttp backend (proxies ComfyUI, workspaces, SSE, frame extraction)
│   ├── static/
│   │   └── index.html       # single-file frontend (merged 3-view UI, vanilla JS, no build)
│   └── workspaces/          # runtime-generated, not committed (.gitignore)
│       └── <workspace>/generations.json + media/
└── scripts/
    ├── pick_port.ps1        # Windows: pick a bindable WebUI port for run_webui.bat
    ├── h3_i2v_smoke.py      # smoke test: i2v (direct ComfyUI API end-to-end, optional)
    ├── h3_r2v_smoke.py      # smoke test: r2v multi-image reference core chain (--input-dir can auto-generate test images)
    ├── p2_audio_smoke.py    # smoke test: r2v audio refs (upload/validation/generation incl. negative cases)
    ├── p3_video_ref_smoke.py  # smoke test: r2v video refs (graph wiring / upload & generation boundaries)
    ├── p35_modes_smoke.py   # smoke test: generation mode extensions (t2v text-only / last frame / first+last)
    ├── e2e_r2v_test.py      # end-to-end test: full r2v generation
    ├── sse_progress_check.py  # SSE progress check script
    ├── sse_latch_selftest.py  # SSE finalize/latch self-test
    └── i18n_coverage_check.js # frontend i18n coverage check (zh/en key alignment)
```

> No frontend build step — `index.html` is everything; edit and refresh.

---

## ❓ FAQ

**Q: Do I need a separate Python environment?**
No. Reuse ComfyUI's bundled `python_embeded\python.exe` (already includes aiohttp / Pillow / PyAV).

**Q: Changes to `index.html` do not take effect?**
The backend disables caching for the homepage; press **Ctrl+Shift+R** to hard-refresh.

**Q: "ComfyUI rejected"?**
Usually a model file is missing from the matching `ComfyUI/models/` directory (the error includes node-level hints), or a node parameter is invalid.

**Q: Starting the WebUI fails with `winerror 10013` (cannot listen on 8080)?**
Port 8080 is inside a Windows “excluded port range” (Hyper-V / WSL / winnat); `netstat` shows nothing listening, which makes it look like a permission problem. `run_webui.bat` probes with a real bind and shifts to the next free port; for a manual start use `set H3WEBUI_PORT=8081`. Inspect the reservations with: `netsh interface ipv4 show excludedportrange protocol=tcp`.

**Q: Why isn't the resolution exactly my custom value?**
H3 requires width/height to be 32-multiples with area ≤ 1920×1088; the backend auto-crops/scales per mode (native mode has an extra scale slider).

**Q: Many generation params — how to reuse them?**
The **♻️ Reuse** button on Studio / Overview cards brings params + reference images back to the left panel; **⏩ Continue** additionally uses that video's last frame as the new first frame.

**Q: Why don't the `<Audio N>` numbers match my audio upload order?**
Because a reference video's soundtrack occupies an ordinal too, and soundtracks are numbered before standalone audio, in video order. For example, with one video that has sound plus one standalone clip, the video soundtrack is `<Audio 1>` and the standalone clip is `<Audio 2>`. Turning off "Use video soundtrack" renumbers the standalone clips from `<Audio 1>` again.

---

## 🙏 Credits & License

- Models, nodes and official workflows: **[MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)** (please comply with its model license terms)
- This repo's code (backend + frontend): MIT License, see [LICENSE](LICENSE)

> ⚠️ This repository contains only the **WebUI layer code**, no model weights. Download models yourself per the official instructions.
