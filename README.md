# 🎬 MiniMax-H3 WebUI — 视频生成工作台

> 一个基于 **ComfyUI + MiniMax-H3** 的自托管视频生成 Web 界面。三视图合一：对话流 / 总览 / Studio，支持工作区、历史管理、真实步数进度、参考图复用与视频续写。

**🌐 语言 / Language：中文 | [English](README.en.md)**

![UI theme](https://img.shields.io/badge/UI-暗色%20Slate%20Indigo-6366f1)
![Backend](https://img.shields.io/badge/backend-aiohttp-blue)
![License](https://img.shields.io/badge/license-MIT-green)

![WebUI 主界面](docs/screenshots/ui-screenshot.png)

> 暗色主题三视图一体界面（对话流 / 总览 / Studio，顶部切换）。

![r2v 提示词编辑器](docs/screenshots/composer-editor.png)

> r2v 全屏提示词编辑器示例：六段分栏 + 整段逐 token 标红，chat / Studio 均可唤起。

---

## ✨ 功能特性

| 特性 | 说明 |
| --- | --- |
| 🗂 **三视图合一** | 对话流（聊天式生成）、总览（作品网格）、Studio（工作台）三个视图合并为一个页面，顶部一键切换 |
| 📁 **工作区管理** | 每个工作区独立目录（`generations.json` + `media/`），可创建/切换/删除 |
| 📜 **历史记录** | 每个工作区完整保留生成记录（提示词、参数、参考图/音频、视频、时长），支持搜索 |
| ⚡ **真实进度** | WebSocket 桥接 ComfyUI，SSE 推送**步数级**实时进度（如 3/8 步），非定时器假进度 |
| 🖼 **参考图复用** | 一键复用历史参数，参考图/音频同步恢复；支持更换图片后自动重新上传 |
| ⏩ **视频续写** | 把已有视频的**最后一帧**抽取为下一段的首帧参考图，实现连续叙事 |
| 📐 **分辨率控制** | 15 种 32 倍数预设（16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / 2:3），或 **native 模式**按原图比例 + 缩放滑条（10–100%） |
| 🔧 **Turbo LoRA 自动匹配** | 按步数自动选 4-step / 8-step turbo LoRA，也可指定自定义 LoRA 文件 |
| 🔊 **原生音频** | 直接输出带声音的视频（H3 原生 audio），浏览器内可拖动进度条（Range 流式播放） |
| 🚀 **历史懒加载** | Studio 左侧历史列表分批渲染（40 条/批）+ 缩略图 IntersectionObserver 懒加载，几百条历史也流畅 |
| 🎯 **Ref2VA 多素材参考** | 任务类型可切换 `i2v 单图 / r2v 多素材`。r2v 可混用 **最多 9 张参考图、3 段参考视频、3 段参考音频**（三种加起来不超过 12 个），在提示词里用 `<Picture N>` / `<Video N>` / `<Audio N>` 按位置引用；只传视频不传图也能用（续写 / 剪辑场景），视频原声音轨默认接入、可一键关闭；内置**六段式提示词编辑器**（全屏整段编辑 + 六段分栏，正文逐 token 标红越界引用、快捷插入素材 token 与骨架，chat / Studio 均可打开，示例库会按你的素材组合自动推荐示例），走核心节点链（SigmaShift 12/3 + res_multistep/simple + SaveVideo），可复用 Turbo LoRA |
| 💾 **数据零依赖** | 前端纯原生 JS（无框架、无构建），后端仅依赖 aiohttp / Pillow / PyAV（ComfyUI 自带） |

---

## 🧱 架构

```
┌─────────────────────────────┐
│  浏览器 (index.html)          │  原生 JS + FontAwesome CDN
│  对话流 / 总览 / Studio        │
└──────────────┬──────────────┘
               │ HTTP / SSE
┌──────────────▼──────────────┐
│  WebUI 后端 (webui/server.py) │  aiohttp @ 127.0.0.1:8080
│  · 工作区 / 历史 / 媒体管理      │
│  · 分辨率预处理 (32倍数裁剪缩放)  │
│  · PyAV 截帧 (续写首帧)         │
│  · WebSocket 桥接 ComfyUI 进度  │
└──────────────┬──────────────┘
               │ HTTP + WS
┌──────────────▼──────────────┐
│  ComfyUI @ 127.0.0.1:8188    │
│  MiniMax-H3 custom nodes     │
│  (T8 conditioning / DualClock│
│   sampler / AV decode / VHS) │
└─────────────────────────────┘
```

关键设计：

- **步数真实生效**：DualClock sampler 的 `steps` 直接映射 `samplerMax`（4→4 步、8→8 步、12→12 步），非 UI 假参数。
- **H3 画布约束**：宽高强制 32 倍数（`CANVAS_MULT=32`），面积上限 `1920×1088`（`MAX_PIXELS`），时长向上取整到 `17n+5` 帧（24fps）。
- **零后端状态丢失**：SSE 事件流 `{type: progress|status|done|error}`，断线自动重连 keepalive。

---

## 📋 环境要求

| 依赖 | 说明 |
| --- | --- |
| **ComfyUI** | 任意较新版本（Windows 桌面版含 `python_embeded`） |
| **MiniMax-H3 自定义节点** | `ComfyUI/custom_nodes/` 中安装 H3 官方节点 |
| **模型文件** | 见下方模型清单，放入对应 `ComfyUI/models/` 目录 |
| **Python 库** | 使用 ComfyUI 自带 python（含 aiohttp / Pillow / PyAV / requests），**无需单独装环境** |

### 模型清单

| 用途 | 文件 | 目录 |
| --- | --- | --- |
| DiT 主模型（二选一） | `minimax_h3_fl2va_pruned_int8_convrot.safetensors`（剪枝版，快）<br>`minimax_h3_fl2va_int8_convrot.safetensors`（完整版，质量） | `models/unet/` |
| 视频 VAE | `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` |
| 音频 VAE | `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` |
| 文本编码器 | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/clip/` |
| Turbo LoRA（可选） | `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors`（4 步）<br>`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`（8 步） | `models/loras/` |
| **Ref2VA 主模型**（r2v 多素材，可选） | `minimax_h3_ref2va_pruned_int8_convrot.safetensors`（或 fp8_scaled 等变体；服务器启动时自动发现 `models/diffusion_models/` 下任意 `minimax_h3_ref2va*.safetensors`） | `models/diffusion_models/` |

模型与节点安装详见 MiniMax-H3 官方仓库：**[MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)**（请遵守其模型许可）。

---

## 🚀 快速开始（Windows）

### 方式一：一键启动（推荐）

```bat
run_webui.bat            :: 使用默认 ComfyUI 根目录
run_webui.bat D:\ComfyUI :: 指定你的 ComfyUI 根目录
```

脚本自动完成：**先挑选可用端口**（默认 8080；被系统保留或已占用时自动顺延到 8081、8082…）→ 该端口上已有 WebUI 则直接开浏览器 → 检查 8188（未跑则后台拉起 ComfyUI 并等待就绪）→ 启动 WebUI → 打开浏览器。**每一步都会打印日志**（端口挑选过程、ComfyUI 检查、启动命令与端口），不会静默等待。

> ComfyUI 根目录也可用环境变量 `H3_COMFY_ROOT` 指定；默认 `D:\ComfyUI`。
>
> Windows 上 Hyper-V / WSL / winnat 会预留“排除端口段”，落在其中的端口（本机常见 8080）即使没人监听也绑不上，报 `winerror 10013`。启动脚本用真实 bind 探测并自动换端口；也可 `set H3WEBUI_PORT=8081` 固定端口。

### 方式二：手动启动

```bat
:: 终端 1：启动 ComfyUI（你的正常方式）
cd /d D:\ComfyUI
python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build

:: 终端 2：启动 WebUI
cd /d <本仓库目录>
D:\ComfyUI\python_embeded\python.exe webui\server.py
```

浏览器打开 **http://127.0.0.1:8080**（或上一步脚本实际选中的端口）即可使用。

### 使用流程

1. 顶部选择/新建 **工作区**（每个工作区历史独立）。
2. 上传 **参考图**（拖拽或点击；r2v 多素材时还可追加参考音频与参考视频）。
3. 填写 **提示词**（支持中文），选择 模型 / 步数 / 时长 / 分辨率模式。r2v 可打开**全屏编辑器**（整段或六段分栏，正文直接插入 `<Picture N>` / `<Video N>` / `<Audio N>` 素材 token）。
4. 点击生成，右侧实时显示 **步数进度**，完成后自动入库并播放。
5. 在 **Studio** 左侧历史中点选旧作品 → **⏩ 续写**（末帧转首帧）或 **♻️ 复用参数**（含参考图/音频）。

---

## 🚀 快速开始（Linux / macOS）

### 方式一：一键启动（推荐）

```bash
./run_webui.sh                       # 使用默认 ComfyUI 根目录 (~/ComfyUI)
./run_webui.sh /path/to/ComfyUI      # 指定 ComfyUI 根目录
./run_webui.sh --port 9000           # 指定 WebUI 端口
```

脚本自动完成：检查 8080（已在跑则直接开浏览器）→ 检查 8188（未跑则后台拉起 ComfyUI 并等待就绪）→ 启动 WebUI → 打开浏览器 `http://127.0.0.1:8080`。

> ComfyUI 根目录也可用环境变量 `H3_COMFY_ROOT` 指定；默认 `~/ComfyUI`。若 ComfyUI 用 conda 环境运行，请先 `conda activate <env>`，或设置 `H3_PYTHON=/path/to/env/bin/python`。

停止 / 查看状态 / 日志：

```bash
./run_webui.sh --stop      # 同时停止 WebUI 与 ComfyUI
./run_webui.sh --status    # 查看运行状态
./run_webui.sh --logs      # 查看日志
```

### 方式二：手动启动

```bash
# 终端 1：启动 ComfyUI（你的正常方式）
cd /path/to/ComfyUI
/path/to/python main.py --listen 127.0.0.1 --port 8188

# 终端 2：启动 WebUI
cd <本仓库目录>
COMFYUI_URL=http://127.0.0.1:8188 \
COMFYUI_INPUT=/path/to/ComfyUI/input \
COMFYUI_OUTPUT=/path/to/ComfyUI/output \
/path/to/python webui/server.py
```

浏览器打开 **http://127.0.0.1:8080** 即可使用。

> Linux 下后端无法从 Python 路径自动反推 ComfyUI 目录，手动启动时建议显式设置 `COMFYUI_INPUT` / `COMFYUI_OUTPUT`。

---

## ⚙️ 配置

全部通过**环境变量**覆盖，无配置文件：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `COMFYUI_INPUT` | 自动探测（`<python>/../../ComfyUI/input`） | ComfyUI input 目录（上传图/截帧会复制到这里） |
| `COMFYUI_OUTPUT` | 自动探测（`<python>/../../ComfyUI/output`） | ComfyUI output 目录 |
| `H3WEBUI_HOST` | `127.0.0.1` | WebUI 监听地址 |
| `H3WEBUI_PORT` | `8080` | WebUI 监听端口；`run_webui.bat` 会从该端口起自动找可绑定的端口 |
| `H3_COMFY_ROOT` | `D:\ComfyUI` | （仅 `run_webui.bat` 用）ComfyUI 根目录 |
| `H3_NO_BROWSER` | 未设置 | （仅 `run_webui.bat` 用）设为 `1` 时不自动打开浏览器，只在控制台打印 URL |

---

## 🔌 API 一览

`/api` 前缀，全部 JSON：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/workspaces` | 列出工作区（含生成数） |
| POST | `/api/workspaces` | 创建工作区 `{name}` |
| DELETE | `/api/workspaces/{name}` | 删除工作区 |
| GET | `/api/workspaces/{name}/generations` | 历史列表（新→旧） |
| DELETE | `/api/workspaces/{name}/generations/{gid}` | 删除一条记录及其媒体 |
| POST | `/api/workspaces/{name}/upload-image` | 上传参考图（base64 data-url）→ 返回文件名（i2v） |
| POST | `/api/workspaces/{name}/upload-media` | multipart 上传参考素材 `{type: image\|audio\|video, file}` → 返回 `{filename, kind, duration_s, width, height}`（r2v；图片 PIL 校验，音视频 PyAV 校验；音频/视频单段限 2–15s，视频额外返回 `has_audio`） |
| POST | `/api/workspaces/{name}/probe-media` | PyAV 探测已上传媒体 `{filename}` → `{kind, duration_s, width, height, ok}` |
| POST | `/api/workspaces/{name}/preview-resolution` | 预览后端实际分辨率 `{image, res_mode, custom_w, custom_h, native_scale}` |
| POST | `/api/workspaces/{name}/extract-frame` | 截帧 `{video, position: "first"\|"last"\|0~1}` → 返回 PNG 文件名（续写用） |
| POST | `/api/workspaces/{name}/generate` | 提交生成 `{prompt, params, image}`（i2v）或 `{prompt, params, refs:{images, videos, audios, use_video_audio}}`（r2v）→ `{job_id, gen_id, width, height, length}` |
| GET | `/api/jobs/{job_id}/events` | SSE 进度流：`progress`（value/max/node）/ `status` / `done`（video/audio/duration）/ `error` |
| POST | `/api/interrupt` | 中断任务 `{job_id}` |
| GET | `/api/workspaces/{name}/media/{file}` | 流式媒体（支持 Range） |
| GET | `/api/comfyui/status` | ComfyUI 状态 + 预设信息（durations / res_presets / max_pixels / ref2va_model / ref2va_present） |

---

## 📁 项目结构

```
.
├── run_webui.bat            # 一键启动脚本（自动拉起 ComfyUI + WebUI）
├── webui/
│   ├── server.py            # aiohttp 后端（代理 ComfyUI、工作区、SSE、截帧）
│   ├── static/
│   │   └── index.html       # 前端单文件（三视图合一，原生 JS，无构建）
│   └── workspaces/          # 运行期生成，不入库（.gitignore）
│       └── <工作区>/generations.json + media/
└── scripts/
    ├── pick_port.ps1        # Windows：为 run_webui.bat 挑选可绑定的 WebUI 端口
    ├── h3_i2v_smoke.py      # 冒烟测试：i2v（直连 ComfyUI API 验证端到端链路，可选）
    ├── h3_r2v_smoke.py      # 冒烟测试：r2v 多图参考核心链（--input-dir 可自动生成测试图）
    ├── p2_audio_smoke.py    # 冒烟测试：r2v 音频参考（上传/校验/生成，含负例）
    ├── e2e_r2v_test.py      # 端到端测试：r2v 完整出片
    ├── sse_progress_check.py  # SSE 进度核对脚本
    ├── sse_latch_selftest.py  # SSE 收尾/闩锁自测
    └── i18n_coverage_check.js # 前端 i18n 文案覆盖率检查（zh/en 键对齐）
```

> 前端无任何构建步骤 —— `index.html` 即全部，改完刷新即生效。

---

## ❓ 常见问题

**Q: 需要单独安装 Python 环境吗？**
不需要。直接复用 ComfyUI 自带 `python_embeded\python.exe`（已含 aiohttp / Pillow / PyAV）。

**Q: 修改了 `index.html` 不生效？**
后端对首页设置了 no-cache，请按 **Ctrl+Shift+R** 强制刷新。

**Q: 显示"ComfyUI 拒绝"？**
多为模型文件未放入对应 `ComfyUI/models/` 目录（错误信息会附节点级提示），或节点参数无效。

**Q: 启动 WebUI 报 `winerror 10013`（无法监听 8080）？**
8080 被 Windows 的“排除端口段”预留了（Hyper-V / WSL / winnat），此时用 `netstat` 看不到任何占用，所以容易被误判成权限问题。`run_webui.bat` 会用真实 bind 探测并自动顺延到下一个可用端口；手动启动可 `set H3WEBUI_PORT=8081`。查看系统预留：`netsh interface ipv4 show excludedportrange protocol=tcp`。

**Q: 分辨率为什么不完全是自定义的值？**
H3 要求宽高为 32 倍数且面积 ≤ 1920×1088，后端会按模式自动裁剪/缩放（native 模式另有缩放滑条）。

**Q: 生成参数很多，如何复用？**
Studio 或总览卡片上的 **♻️ 复用** 一键把参数 + 参考图带回左侧面板；**⏩ 续写** 额外把该视频末帧作为新首帧。

**Q: r2v 里 `<Audio N>` 的编号为什么和音频上传顺序对不上？**
因为参考视频的原声音轨也会占用编号：音轨按视频顺序排在独立音频前面。比如传了 1 段带声音的视频和 1 段独立音频，视频原声是 `<Audio 1>`，独立音频才是 `<Audio 2>`；关掉"使用视频原声"后，编号就会从独立音频重新算起。

---

## 🙏 致谢与许可

- 模型、节点与官方工作流：**[MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)**（请遵守其模型许可条款）
- 本仓库代码（后端 + 前端）：MIT License，见 [LICENSE](LICENSE)

> ⚠️ 本仓库仅包含 **WebUI 层代码**，不含模型权重。模型需自行按官方指引下载。
