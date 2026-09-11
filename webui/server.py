"""
MiniMax-H3 视频生成 WebUI 后端 (aiohttp)
- 代理 ComfyUI (http://127.0.0.1:8188): 提交 prompt 图、WebSocket 拿真实步数进度、SSE 推给前端
- 工作区管理: 每个工作区一个目录, generations.json + media/ (输入图 + 输出视频)
- 视频按 Range 流式播放 (支持拖动进度条)
运行: 使用 ComfyUI 自带的 python (含 aiohttp/requests/PIL), 例如:
  <ComfyUI根目录>/python_embeded/python.exe server.py
"""
import os, sys, json, time, uuid, base64, shutil, asyncio, mimetypes, io, math, random, re
from pathlib import Path

from aiohttp import web, ClientSession, WSMsgType
from PIL import Image

# ==================== 配置 ====================
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")


def _default_comfy_dirs():
    """从当前 python 可执行文件推导 ComfyUI 目录 (python_embeded/python.exe -> 安装根/ComfyUI/).
    也可用环境变量 COMFYUI_INPUT / COMFYUI_OUTPUT 显式指定。"""
    exe = Path(sys.executable).resolve()
    for cand in (exe.parent.parent, exe.parent):
        root = cand / "ComfyUI"
        if (root / "input").is_dir() and (root / "output").is_dir():
            return root / "input", root / "output"
    return Path("ComfyUI") / "input", Path("ComfyUI") / "output"


_COMFY_IN, _COMFY_OUT = _default_comfy_dirs()
COMFYUI_INPUT = Path(os.environ.get("COMFYUI_INPUT", str(_COMFY_IN)))
COMFYUI_OUTPUT = Path(os.environ.get("COMFYUI_OUTPUT", str(_COMFY_OUT)))
PORT = int(os.environ.get("H3WEBUI_PORT", "8080"))
HOST = os.environ.get("H3WEBUI_HOST", "127.0.0.1")


def _comfy_models_dirs() -> list:
    """ComfyUI 权重目录候选 (diffusion_models / unet), 用于探测模型文件是否存在。"""
    out = []
    seen = set()
    for base in (COMFYUI_INPUT.parent, COMFYUI_OUTPUT.parent):
        m = base / "models"
        for sub in ("diffusion_models", "unet"):
            d = m / sub
            if d.is_dir() and str(d) not in seen:
                seen.add(str(d))
                out.append(d)
    return out


def find_ref2va_model() -> str:
    """在 ComfyUI models 目录动态发现 ref2va 权重 (兼容 int8_convrot / fp8_scaled 命名).
    未找到返回 MODELS['ref2va'] 占位名; 前端可据此提示用户下载权重。"""
    for d in _comfy_models_dirs():
        try:
            hits = sorted(p.name for p in d.glob("minimax_h3_ref2va*.safetensors"))
        except OSError:
            continue
        if hits:
            return hits[0]
    return MODELS["ref2va"]


def model_file_present(fname: str) -> bool:
    """某权重文件名是否已存在于 ComfyUI models 目录。"""
    if not fname:
        return False
    for d in _comfy_models_dirs():
        try:
            if (d / fname).is_file():
                return True
        except OSError:
            continue
    return False

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
WORKSPACES_DIR = BASE / "workspaces"
WORKSPACES_DIR.mkdir(exist_ok=True)

# ComfyUI 客户端 id (ws 与 /prompt 共用, 这样进度消息回到我们的 ws)
CLIENT_ID = "h3webui-" + uuid.uuid4().hex[:12]

MODELS = {
    "pruned": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "full":   "minimax_h3_fl2va_int8_convrot.safetensors",
    # ref2va: 文件名是占位默认值, 实际以 models/diffusion_models 下的真实文件为准 (见 find_ref2va_model)
    "ref2va": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
}

# Ref2VA (r2v) 限制 (官方规格, 见调研报告第十六章)
R2V_MAX_IMAGES = 9        # 参考图 1..9
R2V_MAX_AUDIOS = 3        # 参考音频 <=3 段 (P2)
R2V_MAX_VIDEOS = 3        # 参考视频 <=3 段 (P3)
R2V_AUDIO_MIN_S = 2.0     # 单段音频最短 2s
R2V_AUDIO_MAX_S = 15.0    # 单段音频最长 15s
R2V_AUDIO_TOTAL_MAX_S = 15.0  # 音频总时长 <=15s
R2V_VIDEO_MIN_S = 2.0     # 单段视频最短 2s (P3, 官方规格)
R2V_VIDEO_MAX_S = 15.0    # 单段视频最长 15s (P3)
R2V_VIDEO_TOTAL_MAX_S = 15.0  # 视频总时长 <=15s (P3)
R2V_REF_TOTAL_MAX = 12    # 混合参考文件总数上限 (图+视频+音频, 官方规格)
R2V_DEFAULT_W, R2V_DEFAULT_H = 864, 480  # r2v 无 native 语义时的默认画布
# H3 约束 (来自 custom_nodes core.py): 画布必须 32 倍数, 面积上限 1920*1088
CANVAS_MULT = 32
MAX_PIXELS = 1920 * 1088
FPS = 24
# 时长档位 (秒) -> 实际向上取整到 17n+5 帧
DURATIONS = [2, 5, 10, 15]
# 自定义分辨率预设 (32 倍数, 面积 <= MAX_PIXELS=2088960)
RES_PRESETS = [
    # --- 16:9 ---
    {"label": "1920×1088 (16:9 最大)", "w": 1920, "h": 1088},
    {"label": "1280×736 (16:9)", "w": 1280, "h": 736},
    {"label": "864×480 (16:9 快)", "w": 864, "h": 480},
    # --- 9:16 竖屏 ---
    {"label": "1088×1920 (9:16 竖屏最大)", "w": 1088, "h": 1920},
    {"label": "736×1280 (9:16 竖屏)", "w": 736, "h": 1280},
    {"label": "480×864 (9:16 快)", "w": 480, "h": 864},
    # --- 1:1 ---
    {"label": "1440×1440 (1:1)", "w": 1440, "h": 1440},
    {"label": "1024×1024 (1:1)", "w": 1024, "h": 1024},
    {"label": "640×640 (1:1 快)", "w": 640, "h": 640},
    # --- 4:3 ---
    {"label": "1280×960 (4:3)", "w": 1280, "h": 960},
    {"label": "960×736 (4:3 快)", "w": 960, "h": 736},
    # --- 3:4 ---
    {"label": "960×1280 (3:4 竖屏)", "w": 960, "h": 1280},
    # --- 21:9 电影宽 ---
    {"label": "1920×832 (21:9 宽)", "w": 1920, "h": 832},
    {"label": "1280×544 (21:9 快)", "w": 1280, "h": 544},
    # --- 2:3 ---
    {"label": "768×1152 (2:3 竖屏)", "w": 768, "h": 1152},
]
TURBO_LORAS = {
    4: "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    8: "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}
DEFAULT_LORA = TURBO_LORAS[8]

# ==================== 任务管理 ====================
# prompt_id -> {queue, ws, gen_id, params, prompt, image, status, video, audio, error, t0}
JOBS = {}


def ws_dir(name: str) -> Path:
    d = WORKSPACES_DIR / safe_name(name)
    (d / "media").mkdir(parents=True, exist_ok=True)
    return d


def safe_name(n: str) -> str:
    keep = "".join(c for c in n if c.isalnum() or c in "-_")
    return keep or "workspace"


def load_gens(name: str) -> list:
    f = ws_dir(name) / "generations.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_gens(name: str, gens: list):
    (ws_dir(name) / "generations.json").write_text(
        json.dumps(gens, ensure_ascii=False, indent=2), encoding="utf-8")


def list_workspaces() -> list:
    out = []
    for d in sorted(WORKSPACES_DIR.iterdir()):
        if d.is_dir():
            gens = load_gens(d.name)
            out.append({
                "name": d.name,
                "count": len(gens),
                "updated": int((d / "generations.json").stat().st_mtime) if (d / "generations.json").exists() else 0,
            })
    return out


# ==================== 图像预处理 + 时长映射 ====================
def snap32(x):
    return max(CANVAS_MULT, (round(x / CANVAS_MULT)) * CANVAS_MULT)


def compute_native_target(iw, ih, scale=1.0):
    """图像原生比例下, 32 倍数且面积 <= MAX_PIXELS 的分辨率。
    scale (0.1~1.0) 按比例缩小最终分辨率 (保持比例, 仍 snap 到 32 倍数)。"""
    aspect = iw / ih
    max_pixels = MAX_PIXELS * max(0.05, min(scale, 1.0))
    h = snap32(math.sqrt(max_pixels / aspect))
    w = snap32(h * aspect)
    while w * h > max_pixels:
        h -= CANVAS_MULT
        w = snap32(h * aspect)
    return w, h


def cover_crop_resize(img: Image.Image, tw: int, th: int) -> Image.Image:
    """cover 裁剪到 tw:th 比例, 再缩放到 (tw, th)。"""
    sw, sh = img.size
    ta, sa = tw / th, sw / sh
    if sa > ta:
        nw = sh * ta
        left = (sw - nw) / 2
        img = img.crop((left, 0, left + nw, sh))
    else:
        nh = sw / ta
        top = (sh - nh) / 2
        img = img.crop((0, top, sw, top + nh))
    return img.resize((tw, th), Image.LANCZOS)


def preprocess_image(img_bytes: bytes, mode: str, cw=None, ch=None, native_scale=1.0):
    """按分辨率模式裁剪/缩放到 32 倍数。返回 (处理后 PNG bytes, 目标宽, 目标高)。"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    iw, ih = img.size
    if mode == "custom" and cw and ch:
        tw, th = snap32(cw), snap32(ch)
        if tw * th > MAX_PIXELS:        # 超限则按该比例降到上限内
            tw, th = compute_native_target(tw, th)
    else:
        tw, th = compute_native_target(iw, ih, native_scale)
    img = cover_crop_resize(img, tw, th)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), tw, th


def duration_to_frames(seconds: int) -> int:
    """秒 -> 向上取整到 17n+5 网格的帧数。"""
    target = seconds * FPS
    n = max(0, math.ceil((target - 5) / 17))
    return 5 + 17 * n


# ==================== ComfyUI prompt 图构建 ====================
def build_graph(prompt: str, params: dict, image_name: str, width: int, height: int, length: int,
                last_image_name: str = None) -> dict:
    """T8 链构图。image_name=首帧 (None=纯文字); last_image_name=尾帧 (首尾帧模式)。
    task_type 按接线推导: 首尾 FL2VA / 仅尾 L2VA / 仅首 I2VA / 都无 T2VA
    (T8 resolve_task_type 校验接线与 task_type 必须一致)。"""
    model = MODELS.get(params.get("model", "pruned"), MODELS["pruned"])
    steps = int(params.get("steps", 4))
    seed = int(params.get("seed", 0))
    prefix = f"H3_{params.get('model','pruned')}_{steps}step_{width}x{height}_{length}f{'_sage' if params.get('sage') else ''}{'_lo' if params.get('low_vram', True) else ''}"
    g = {
        "1":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "2":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "8":  {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {
            "steps": steps, "shift_video": 12.0, "shift_audio": 3.0,
            "model": ["4", 0], "av_latent": ["7", 1]}},
        "9":  {"class_type": "RandomNoise",          "inputs": {"noise_seed": seed}},
        "10": {"class_type": "BasicGuider",          "inputs": {"model": ["8", 0], "conditioning": ["7", 0]}},
        "11": {"class_type": "SamplerCustomAdvanced","inputs": {
            "noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 1],
            "sigmas": ["8", 2], "latent_image": ["7", 1]}},
        "12": {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {"av_latent": ["11", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]}},
        "13": {"class_type": "VHS_VideoCombine", "inputs": {
            "frame_rate": FPS, "loop_count": 0, "filename_prefix": prefix,
            "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19,
            "save_metadata": True, "trim_to_audio": False, "pingpong": False,
            "save_output": True, "images": ["12", 0], "audio": ["12", 1]}},
    }
    if image_name:
        g["6"] = {"class_type": "LoadImage", "inputs": {"image": image_name, "upload": "image"}}
    if last_image_name:
        g["6b"] = {"class_type": "LoadImage", "inputs": {"image": last_image_name, "upload": "image"}}
    task_tt = ("FL2VA" if (image_name and last_image_name) else
               "L2VA" if last_image_name else
               "I2VA" if image_name else "T2VA")
    g["7"] = {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
        "prompt": prompt, "width": width, "height": height, "length": length,
        "task_type": task_tt, "audio_mode": "native", "audio_denoise_strength": 1.0,
        "add_source_as_reference": True, "prompt_primary_audio_ordinal": 0,
        "strict_prompt_tags": True, "ref_image_size": "match",
        "reference_video_policy": "official_2_to_15s",
        "clip": ["3", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0],
        **({"first_frame": ["6", 0]} if image_name else {}),
        **({"last_frame": ["6b", 0]} if last_image_name else {})}}
    model_src = "4"
    # 低显存优化 (精度无损, 16G 跑 47G 模型靠它避免 ComfyUI 默认 offload 降精度)
    # 顺序: UNETLoader -> ChunkFeedForward -> LowVRAMAttention -> [Sage] -> [Turbo LoRA] -> Sampler
    if params.get("low_vram", True):
        g["4a"] = {"class_type": "MiniMaxChunkFeedForward", "inputs": {"model": [model_src, 0], "chunks": 2, "seq_threshold": 4096}}
        model_src = "4a"
        g["4b"] = {"class_type": "MiniMaxLowVRAMAttention", "inputs": {"model": [model_src, 0], "head_chunks": 4}}
        model_src = "4b"
    if params.get("sage"):
        g["4c"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": [model_src, 0]}}
        model_src = "4c"
    if params.get("turbo_lora"):
        lora_name = params.get("lora_name") or DEFAULT_LORA
        g["5"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": lora_name, "strength_model": 1.0, "model": [model_src, 0]}}
        model_src = "5"
    g["8"]["inputs"]["model"] = [model_src, 0]
    return g


# ==================== Ref2VA (r2v) 核心节点链 ====================
def build_ref2va_graph(prompt: str, params: dict, ref_images: list, width: int, height: int, length: int,
                       ref_audios: list = None, ref_videos: list = None,
                       video_audio_flags: list = None) -> dict:
    """Ref2VA 多素材参考构建 (核心节点链, T8 无多参考输入故不走 T8)。

    链: UNETLoader -> [ChunkFeedForward/LowVRAMAttention] -> [Sage] -> [Turbo LoRA]
        -> MiniMaxH3SigmaShift(12/3) -> BasicScheduler/BasicGuider
    参考图: LoadImage xN -> MiniMaxH3ReferenceToVideo.ref_images.ref_image_0..N
    参考音频(P2): LoadAudio xM -> 同节点 ref_audios.ref_audio_0..M (官方 ≤3 段)
    参考视频(P3): LoadVideo xK -> GetVideoComponents -> ref_videos.ref_video_0..K (官方 ≤3 段)
        原声音轨: GetVideoComponents 输出槽1 -> ref_video_audios.ref_video_audio_j
        (与 ref_video_j 按尾号配对; 仅对实际含音轨的视频接线, 由 video_audio_flags 对齐)
        官方 conditioning 顺序: 图 -> 视频(音轨标签先于视频标签) -> 独立音频,
        故启用音轨时前端 <Audio N> 编号须把音轨排在独立音频之前 (音轨占 base 个序号)。
    出片: SamplerCustomAdvanced -> VAEDecode/VAEDecodeAudio -> CreateVideo -> SaveVideo
    注: onigirikiller/SekiyoKana 生产链已证实核心 SaveVideo 的输出同样出现在
        /history 的 images 数组 (.mp4), 与现有 finish_job 扫描兼容。
    """
    unet_name = find_ref2va_model()
    steps = int(params.get("steps", 20))
    seed = int(params.get("seed", 0))
    prefix = f"H3_R2V_{steps}step_{width}x{height}_{length}f{'_turbo' if params.get('turbo_lora') else ''}{'_sage' if params.get('sage') else ''}{'_lo' if params.get('low_vram', True) else ''}"

    g = {
        "1":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "2":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": "default"}},
        # 参考图 LoadImage 挂到 node6.ref_images.ref_image_{i} (i=0..N-1), 从 id 20 起
    }
    model_src = "4"
    if params.get("low_vram", True):
        g["4a"] = {"class_type": "MiniMaxChunkFeedForward", "inputs": {"model": [model_src, 0], "chunks": 2, "seq_threshold": 4096}}
        model_src = "4a"
        g["4b"] = {"class_type": "MiniMaxLowVRAMAttention", "inputs": {"model": [model_src, 0], "head_chunks": 4}}
        model_src = "4b"
    if params.get("sage"):
        g["4c"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": [model_src, 0]}}
        model_src = "4c"
    if params.get("turbo_lora"):
        lora_name = params.get("lora_name") or DEFAULT_LORA
        g["5"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": lora_name, "strength_model": 1.0, "model": [model_src, 0]}}
        model_src = "5"
    # SigmaShift: 核心链的 shift 载体 (T8 链由 DualClockSamplerT8 内置 shift 12/3)
    g["5s"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": [model_src, 0], "shift_video": 12.0, "shift_audio": 3.0}}

    node6 = {
        "clip": ["3", 0], "vae": ["1", 0], "audio_vae": ["2", 0],
        "prompt": prompt, "width": width, "height": height, "length": length,
        "ref_image_size": "match",   # 节点内部处理多参考图缩放, 不做后端 cover
    }
    nid = 20
    for i, fname in enumerate(ref_images[:R2V_MAX_IMAGES]):
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": fname}}
        node6[f"ref_images.ref_image_{i}"] = [str(nid), 0]
        nid += 1
    # P2: 参考音频 (官方 ≤3 段, 每段 2–15s, 总时长 ≤15s; 上传/生成时均已校验)
    for j, afname in enumerate((ref_audios or [])[:R2V_MAX_AUDIOS]):
        aid = 30 + j
        g[str(aid)] = {"class_type": "LoadAudio", "inputs": {"audio": afname}}
        node6[f"ref_audios.ref_audio_{j}"] = [str(aid), 0]
    # P3: 参考视频 (官方 ≤3 段, 每段 2–15s): LoadVideo(40+j) -> GetVideoComponents(44+j)
    #     槽0 帧序列 -> ref_videos.ref_video_{j}; 槽1 原声 -> ref_video_audios.ref_video_audio_{j} (有音轨才接)
    vid_flags = list(video_audio_flags or [])
    for j, vfname in enumerate((ref_videos or [])[:R2V_MAX_VIDEOS]):
        lid, gid = 40 + j, 44 + j
        g[str(lid)] = {"class_type": "LoadVideo", "inputs": {"file": vfname}}
        g[str(gid)] = {"class_type": "GetVideoComponents", "inputs": {"video": [str(lid), 0]}}
        node6[f"ref_videos.ref_video_{j}"] = [str(gid), 0]
        if j < len(vid_flags) and vid_flags[j]:
            node6[f"ref_video_audios.ref_video_audio_{j}"] = [str(gid), 1]

    g["6"] =  {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": node6}
    g["7"] =  {"class_type": "RandomNoise",            "inputs": {"noise_seed": seed}}
    g["8"] =  {"class_type": "KSamplerSelect",         "inputs": {"sampler_name": "res_multistep"}}
    g["9"] =  {"class_type": "BasicScheduler",         "inputs": {"model": ["5s", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}}
    g["10"] = {"class_type": "BasicGuider",            "inputs": {"model": ["5s", 0], "conditioning": ["6", 0]}}
    g["11"] = {"class_type": "SamplerCustomAdvanced",  "inputs": {
        "noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
        "sigmas": ["9", 0], "latent_image": ["6", 1]}}
    g["12"] = {"class_type": "VAEDecode",       "inputs": {"samples": ["11", 0], "vae": ["1", 0]}}
    g["13"] = {"class_type": "VAEDecodeAudio",  "inputs": {"samples": ["11", 0], "vae": ["2", 0]}}
    g["14"] = {"class_type": "CreateVideo",     "inputs": {"images": ["12", 0], "audio": ["13", 0], "fps": 24.0, "bit_depth": 8}}
    g["15"] = {"class_type": "SaveVideo",       "inputs": {
        "video": ["14", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"}}
    return g


# ==================== ComfyUI WebSocket 监听 ====================
def push_event(job: dict, ev: dict):
    """统一事件出口: 终态事件(done/error)闩锁到 job["final"], 供 SSE 断线重连兜底重放。
    事件附单调递增 seq, 供重连方跳过已被 last 重放覆盖的旧事件。
    队列为无界, put_nowait 永不阻塞, 同步/异步上下文均可安全调用。"""
    job["seq"] = job.get("seq", 0) + 1
    ev = dict(ev, seq=job["seq"])
    job["last"] = ev
    if ev.get("type") in ("done", "error"):
        job["final"] = ev
    job["queue"].put_nowait(ev)


async def comfy_ws_loop(app):
    session: ClientSession = app["session"]
    ws_url = COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={CLIENT_ID}"
    backoff = 1
    while not app.get("stop"):
        try:
            async with session.ws_connect(ws_url, heartbeat=20) as ws:
                print(f"[ws] connected to ComfyUI {ws_url}", flush=True)
                backoff = 1
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            await route_comfy_msg(json.loads(msg.data))
                        except Exception as e:
                            print("[ws] route err", e, flush=True)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
        except Exception as e:
            print(f"[ws] disconnect/err: {e}; reconnect in {backoff}s", flush=True)
        if app.get("stop"):
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 10)


async def route_comfy_msg(data: dict):
    t = data.get("type")
    d = data.get("data", {}) or {}
    pid = d.get("prompt_id")
    job = JOBS.get(pid) if pid else None
    if t == "progress" and job:
        push_event(job, {"type": "progress", "value": d.get("value", 0), "max": d.get("max", 0),
                         "node": d.get("node", "")})
    elif t == "executing" and job:
        node = d.get("node")
        if node:
            push_event(job, {"type": "status", "text": f"执行节点 {node}"})
    elif t == "execution_success" and job:
        # 收尾(轮询 history + 拷贝大视频)放后台任务, 不阻塞 WS 循环处理其它 job 的进度
        job["fin_task"] = asyncio.create_task(finish_job(job))
    elif t == "execution_error" and job:
        msg = str(d.get("exception_message") or d.get("node_type") or "执行出错")
        tb = d.get("traceback") or d.get("exception_traceback") or ""
        job["error"] = msg
        push_event(job, {"type": "error", "message": msg, "traceback": tb})
    elif t == "execution_interrupted" and job:
        push_event(job, {"type": "error", "message": "已中断"})
    elif t == "execution_cached" and job:
        pass


async def finish_job(job: dict):
    """从 /history 取输出文件, 拷到工作区, 写历史, 推 done。"""
    session: ClientSession = job["session"]
    pid = job["pid"]
    try:
        # 轮询 /history 直到出现
        hist = {}
        for _ in range(40):
            async with session.get(f"{COMFYUI_URL}/history/{pid}", timeout=10) as r:
                hist = await r.json()
            if pid in hist:
                break
            await asyncio.sleep(1)
        outputs = (hist.get(pid) or {}).get("outputs", {})
        video, audio = None, None

        def _iter_files(out: dict, *keys):
            """history 输出里按多个候选字段名取文件条目 (兼容 list / dict 形态)。"""
            for k in keys:
                v = out.get(k)
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and it.get("filename"):
                            yield it
                elif isinstance(v, dict) and v.get("filename"):
                    yield v

        for nid, out in outputs.items():
            # 核心 SaveVideo / VHS 都把成片放在 images/gifs/video 字段 (已验证: SaveVideo -> images)
            for img in _iter_files(out, "images", "gifs", "video"):
                fn = img.get("filename")
                if fn and fn.lower().endswith((".mp4", ".webm", ".gif", ".mov")):
                    if not video:
                        video = (fn, img.get("subfolder", ""), img.get("type", "output"))
            for au in _iter_files(out, "audio", "audios"):
                fn = au.get("filename")
                if fn and not audio:
                    audio = (fn, au.get("subfolder", ""), au.get("type", "output"))
        dur = round(time.time() - job["t0"], 1)
        ws_name = job["ws"]
        wd = ws_dir(ws_name)
        vdest, adest = None, None
        if video:
            src = comfy_out_path(*video)
            if src and src.exists():
                vdest = f"{job['gen_id']}_video{src.suffix}"
                await asyncio.to_thread(shutil.copy2, src, wd / "media" / vdest)
        if audio:
            src = comfy_out_path(*audio)
            if src and src.exists():
                adest = f"{job['gen_id']}_audio{src.suffix}"
                await asyncio.to_thread(shutil.copy2, src, wd / "media" / adest)
        job["video"] = vdest
        job["audio"] = adest
        job["status"] = "done"
        # 写历史
        gens = load_gens(ws_name)
        rec = {
            "id": job["gen_id"], "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": job["prompt"], "params": job["params"], "image": job["image"],
            "image_last": job.get("image_last"),
            "video": vdest, "audio": adest, "duration": dur, "seed": job["params"].get("seed"),
            "refs": job.get("refs"), "task_type": job.get("task_type") or job["params"].get("task_type") or "i2v",
        }
        gens.insert(0, rec)
        save_gens(ws_name, gens)
        push_event(job, {"type": "done", "video": vdest, "audio": adest, "duration": dur, "gen_id": job["gen_id"]})
    except Exception as e:
        import traceback as _tb
        job["error"] = f"收尾失败: {e}"
        push_event(job, {"type": "error", "message": job["error"], "traceback": _tb.format_exc()})


def comfy_out_path(filename: str, subfolder: str, ftype: str):
    if not filename:
        return None
    base = COMFYUI_OUTPUT if ftype != "temp" else COMFYUI_OUTPUT.parent / "temp"
    p = base / subfolder / filename if subfolder else base / filename
    return p


# ==================== HTTP 路由 ====================
async def index(request):
    resp = web.FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def favicon(request):
    return web.Response(status=204)


async def get_workspaces(request):
    return web.json_response(list_workspaces())


async def create_workspace(request):
    name = safe_name((await request.json()).get("name", "").strip())
    if not name:
        raise web.HTTPBadRequest(text="需要 name")
    ws_dir(name)
    return web.json_response({"name": name})


async def delete_workspace(request):
    name = safe_name(request.match_info["name"])
    d = WORKSPACES_DIR / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return web.json_response({"ok": True})


async def get_generations(request):
    name = safe_name(request.match_info["name"])
    return web.json_response(load_gens(name))


async def delete_generation(request):
    name = safe_name(request.match_info["name"])
    gid = request.match_info["gid"]
    gens = load_gens(name)
    wd = ws_dir(name)
    for g in gens:
        if g.get("id") == gid:
            # 只删产出文件 (video/audio); image 若为 i2v 私有预处理产物 (proc_/frame_/tmp)
            # 也删, 但上传素材 (ws_/au_/vd_...) 永不因删除记录而删 — 可能被多条记录复用
            for k in ("video", "audio"):
                fn = g.get(k)
                if fn and "/" not in fn and "\\" not in fn:
                    p = wd / "media" / fn
                    if p.exists():
                        try: p.unlink()
                        except Exception: pass
            img = g.get("image")
            if img and (img.startswith("proc_") or img.startswith("frame_")):
                p = wd / "media" / img
                if p.exists():
                    try: p.unlink()
                    except Exception: pass
    gens = [g for g in gens if g.get("id") != gid]
    save_gens(name, gens)
    return web.json_response({"ok": True})


async def upload_image(request):
    """前端上传参考图 (base64 data url), 存工作区 + ComfyUI/input/。返回文件名。"""
    name = safe_name(request.match_info["name"])
    try:
        data = await request.json()
    except Exception as e:
        raise web.HTTPBadRequest(text=f"无效的 JSON 请求体: {e}（请按 Ctrl+Shift+R 硬刷新页面清除缓存后重试）")
    b64 = data.get("image", "")
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    img_bytes = base64.b64decode(b64)
    ext = ".png"
    mime = (data.get("mime") or "image/png").lower()
    if "jpeg" in mime or "jpg" in mime:
        ext = ".jpg"
    elif "webp" in mime:
        ext = ".webp"
    fname = f"ws_{uuid.uuid4().hex[:10]}{ext}"
    wd = ws_dir(name)
    (wd / "media" / fname).write_bytes(img_bytes)
    # 拷到 ComfyUI/input 让 LoadImage 能读到
    try:
        shutil.copy2(wd / "media" / fname, COMFYUI_INPUT / fname)
    except Exception as e:
        print("[upload] copy to ComfyUI/input failed:", e, flush=True)
    return web.json_response({"filename": fname})


# ---- 参考素材 (r2v 多图/音频/视频): multipart 上传 + PyAV 探测 ----
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


def _probe_media(p: Path) -> dict:
    """PyAV 探测媒体文件: 返回 {ok, kind, duration_s, width, height, has_video, has_audio, error}"""
    try:
        import av
        container = av.open(str(p))
        out = {"ok": True, "kind": None, "duration_s": None,
               "width": None, "height": None,
               "has_video": bool(container.streams.video),
               "has_audio": bool(container.streams.audio), "error": None}
        # duration: 视频/音频流各自时长 (优先取存在的流)
        for s in (container.streams.video or container.streams.audio or []):
            if s.duration:
                out["duration_s"] = round(float(s.duration * s.time_base), 2)
                break
        vs = container.streams.video
        if vs:
            st = vs[0]
            out["width"], out["height"] = st.codec_context.width, st.codec_context.height
        container.close()
        if out["has_video"]:
            out["kind"] = "video"
        elif out["has_audio"]:
            out["kind"] = "audio"
        else:
            out["ok"] = False
            out["error"] = "未识别到任何音视频流"
        return out
    except ImportError:
        return {"ok": False, "error": "服务器缺少 PyAV (av) 库", "kind": None}
    except Exception as e:
        return {"ok": False, "error": f"媒体解码失败: {e}", "kind": None}


async def upload_media(request):
    """r2v 参考素材上传 (multipart: type + file)。图片 PIL 校验; 音视频 PyAV 校验并返回时长/尺寸。
    存工作区 + 拷 ComfyUI/input。"""
    name = safe_name(request.match_info["name"])
    reader = await request.multipart()
    kind, fname_orig, data = None, "", b""
    async for part in reader:
        if part.name == "type":
            kind = (await part.read()).decode("utf-8", "replace").strip().lower()
        elif part.name == "file":
            fname_orig = part.filename or ""
            data = await part.read()
    if not data:
        raise web.HTTPBadRequest(text="未收到文件内容")
    ext = Path(fname_orig).suffix.lower() or ""
    # kind 缺省时按扩展名推断
    if not kind:
        if ext in _IMAGE_EXTS: kind = "image"
        elif ext in _AUDIO_EXTS: kind = "audio"
        elif ext in _VIDEO_EXTS: kind = "video"
    wd = ws_dir(name)

    if kind == "image":
        if ext not in _IMAGE_EXTS:
            raise web.HTTPBadRequest(text=f"不支持的图片格式: {ext or '未知'} (支持 png/jpg/jpeg/webp/bmp)")
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as e:
            raise web.HTTPBadRequest(text=f"图片无法解码: {e}")
        # 统一重编码为 PNG (保持像素/不缩放): LoadImage 兼容性最好, 顺带剥离 EXIF
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        data = buf.getvalue()
        ext = ".png"
        fname = f"ws_{uuid.uuid4().hex[:10]}{ext}"
        resp = {"filename": fname, "kind": "image", "width": img.width, "height": img.height, "duration_s": None}
    elif kind in ("audio", "video"):
        if kind == "audio" and ext not in _AUDIO_EXTS:
            raise web.HTTPBadRequest(text=f"不支持的音频格式: {ext or '未知'} (支持 mp3/wav/flac/m4a/aac/ogg/opus)")
        if kind == "video" and ext not in _VIDEO_EXTS:
            raise web.HTTPBadRequest(text=f"不支持的视频格式: {ext or '未知'} (支持 mp4/webm/mov/mkv/avi)")
        prefix = "au_" if kind == "audio" else "vd_"
        fname = f"{prefix}{uuid.uuid4().hex[:10]}{ext or ('.mp3' if kind == 'audio' else '.mp4')}"
        resp = {"filename": fname, "kind": kind}
    else:
        raise web.HTTPBadRequest(text="type 必须是 image / audio / video")

    (wd / "media" / fname).write_bytes(data)
    try:
        shutil.copy2(wd / "media" / fname, COMFYUI_INPUT / fname)
    except Exception as e:
        print("[upload_media] copy to ComfyUI/input failed:", e, flush=True)

    if kind in ("audio", "video"):
        def _reject(msg: str):
            # 失败路径: 同时清理工作区副本与 ComfyUI/input 副本, 避免孤立文件
            (wd / "media" / fname).unlink(missing_ok=True)
            (Path(COMFYUI_INPUT) / fname).unlink(missing_ok=True)
            raise web.HTTPBadRequest(text=msg)
        info = _probe_media(wd / "media" / fname)
        if not info["ok"]:
            _reject(info["error"])
        if info["kind"] != kind:
            # 名不符实: 例如标 audio 实际是视频
            _reject(f"文件实际是 {info['kind']}, 与上传类型 {kind} 不符")
        if info["duration_s"] is None:
            # 无法探测时长 -> 无法执行 2–15s 边界校验, 拒绝 (避免静默绕过)
            _reject(f"无法探测{'音频' if kind == 'audio' else '视频'}时长, 无法校验 2–15s 限制, 请换常见容器格式 (如 mp4/wav)")
        if kind == "audio":
            if info["duration_s"] < R2V_AUDIO_MIN_S or info["duration_s"] > R2V_AUDIO_MAX_S:
                _reject(f"参考音频需 {R2V_AUDIO_MIN_S:.0f}–{R2V_AUDIO_MAX_S:.0f}s (当前 {info['duration_s']}s)")
        if kind == "video":
            # P3: 参考视频单段 2–15s (官方规格; 总时长 ≤15s 在生成时校验)
            if info["duration_s"] < R2V_VIDEO_MIN_S or info["duration_s"] > R2V_VIDEO_MAX_S:
                _reject(f"参考视频需 {R2V_VIDEO_MIN_S:.0f}–{R2V_VIDEO_MAX_S:.0f}s (当前 {info['duration_s']}s)")
        resp.update({"duration_s": info["duration_s"], "width": info["width"], "height": info["height"]})
        if kind == "video":
            resp["has_audio"] = bool(info.get("has_audio"))  # P3: 原声音轨有无 (决定 <Audio N> 编号与接线)
    return web.json_response(resp)


async def probe_media(request):
    """对工作区已上传的媒体返回探测信息 (供前端展示/校验)。"""
    name = safe_name(request.match_info["name"])
    data = await request.json()
    fname = data.get("filename")
    if not fname or "/" in fname or "\\" in fname:
        raise web.HTTPBadRequest(text="缺少 filename")
    p = ws_dir(name) / "media" / fname
    if not p.exists():
        raise web.HTTPNotFound(text="文件不存在")
    info = _probe_media(p)
    info["filename"] = fname
    return web.json_response(info)


async def preview_resolution(request):
    """给定已上传图 + 分辨率模式, 返回后端将实际使用的 (w, h)。"""
    name = safe_name(request.match_info["name"])
    data = await request.json()
    image = data.get("image")
    mode = data.get("res_mode", "native")
    cw, ch = data.get("custom_w"), data.get("custom_h")
    native_scale = float(data.get("native_scale", 1.0))
    if not image:
        raise web.HTTPBadRequest(text="缺少 image")
    p = ws_dir(name) / "media" / image
    if not p.exists():
        raise web.HTTPBadRequest(text="图不存在, 请重新上传")
    _b, tw, th = preprocess_image(p.read_bytes(), mode, cw, ch, native_scale)
    return web.json_response({"width": tw, "height": th, "pixels": tw * th})


def _decode_frame_sync(vp: Path, position):
    """同步解码目标帧 (阻塞操作, 调用方经 asyncio.to_thread 放入线程池)。返回 PIL Image。"""
    import av  # PyAV (ComfyUI 依赖, 用于读视频帧)
    container = av.open(str(vp))
    try:
        stream = container.streams.video[0]
        if position == "first":
            target_ts = 0
        elif position == "last":
            # seek 到最后附近, 读最后一帧
            target_ts = float(stream.duration * stream.time_base) if stream.duration else 0
        else:
            # position 是 0~1 比例
            target_ts = float(position) * (float(stream.duration * stream.time_base) if stream.duration else 0)
        if target_ts:
            container.seek(int(target_ts / stream.time_base), stream=stream)
        last_frame = None
        for frame in container.decode(video=0):
            last_frame = frame
        if last_frame is None:
            raise ValueError("无法解码视频帧")
        return last_frame.to_image().convert("RGB")
    finally:
        container.close()


async def extract_frame(request):
    """从工作区视频截取一帧 (默认最后一帧), 保存为 PNG 并拷到 ComfyUI/input。
    返回 {filename} 可直接当作参考图。解码为阻塞操作, 放线程池执行以免卡事件循环。"""
    name = safe_name(request.match_info["name"])
    data = await request.json()
    video = data.get("video")
    position = data.get("position", "last")  # "last" | "first" | 0~1 float
    if not video:
        raise web.HTTPBadRequest(text="缺少 video")
    vp = ws_dir(name) / "media" / video
    if not vp.exists():
        raise web.HTTPBadRequest(text="视频不存在")
    try:
        img = await asyncio.to_thread(_decode_frame_sync, vp, position)
        fname = f"frame_{uuid.uuid4().hex[:10]}.png"
        buf = io.BytesIO(); img.save(buf, format="PNG")
        wd = ws_dir(name)
        (wd / "media" / fname).write_bytes(buf.getvalue())
        try:
            shutil.copy2(wd / "media" / fname, COMFYUI_INPUT / fname)
        except Exception as e:
            print("[extract_frame] copy to ComfyUI/input failed:", e, flush=True)
        return web.json_response({"filename": fname})
    except ImportError:
        raise web.HTTPBadRequest(text="服务器缺少 PyAV (av) 库, 无法截帧")
    except Exception as e:
        raise web.HTTPBadRequest(text=f"截帧失败: {e}")


async def generate(request):
    """提交生成任务。params.task_type 决定走哪条链:
      - i2v (默认): T8 自定义节点链, 单图预处理, build_graph()
      - t2v: 同链不接图 (纯文字 → 音视频)
      - r2v: 核心节点链 (多素材参考), build_ref2va_graph()
    i2v/t2v 家族另支持 frame_mode: first(首帧, 默认) / last(单图作尾帧) / both(首尾双帧)"""
    name = safe_name(request.match_info["name"])
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    params = data.get("params") or {}
    raw_image = data.get("image")  # i2v 参考图 (r2v 用 refs.images, 不走此字段)
    raw_image_last = data.get("image_last")  # 首尾帧模式的尾帧图
    task_type = str(params.get("task_type") or data.get("task_type") or "i2v").lower()
    if task_type not in ("i2v", "t2v", "r2v"):
        task_type = "i2v"
    frame_mode = str(data.get("frame_mode") or "first").lower()
    if frame_mode not in ("first", "last", "both"):
        raise web.HTTPBadRequest(text="frame_mode 必须是 first / last / both")
    if frame_mode != "both" and raw_image_last:
        raise web.HTTPBadRequest(text="image_last 仅在 frame_mode=both 时有效")
    if not prompt:
        raise web.HTTPBadRequest(text="请填写提示词")
    params["task_type"] = task_type
    params.setdefault("model", "pruned" if task_type != "r2v" else "ref2va")
    params.setdefault("steps", 4 if task_type != "r2v" else 20)
    params.setdefault("res_mode", "custom" if task_type == "r2v" else "native")
    params.setdefault("duration", 5)
    params.setdefault("seed", -1)
    params.setdefault("low_vram", True)
    # 解析种子 (-1 -> 随机)
    seed = int(params.get("seed", -1))
    if seed < 0:
        seed = random.randint(0, 2**31 - 1)
    params["seed"] = seed
    # turbo LoRA 自动按步数选 (4->4step, 8->8step), 用户自定义文件名优先
    if params.get("turbo_lora"):
        ln = (params.get("lora_name") or "").strip()
        known = set(TURBO_LORAS.values()) | {DEFAULT_LORA, "minimax_h3_turbo_4STEPS_comfyui.safetensors"}
        params["lora_name"] = ln if (ln and ln not in known) else TURBO_LORAS.get(int(params.get("steps", 8)), DEFAULT_LORA)

    if task_type == "r2v":
        return await _generate_r2v(request, name, data, prompt, params)

    # ==================== i2v / t2v (T8 链; frame_mode: first/last/both) ====================
    if task_type == "t2v":
        if raw_image or raw_image_last:
            raise web.HTTPBadRequest(text="t2v 文字模式不接受参考图")
        if params.get("res_mode", "custom") == "native":
            params["res_mode"] = "custom"  # 无来源图, native 无语义
    else:
        if frame_mode == "both" and not (raw_image and raw_image_last):
            raise web.HTTPBadRequest(text="首尾帧模式需要同时提供首帧图与尾帧图")
        if frame_mode != "both" and not raw_image:
            raise web.HTTPBadRequest(text="请上传参考图")
    params["frame_mode"] = frame_mode
    wd = ws_dir(name)

    def _load_proc(raw, canvas=None):
        """读取+预处理上传图 (canvas=(w,h) 时按 custom 强制对齐该画布)。返回 (proc_name, w, h)。"""
        p = ws_dir(name) / "media" / raw
        if not p.exists():
            raise web.HTTPBadRequest(text="参考图不存在, 请重新上传")
        if canvas:
            b, w, h = preprocess_image(p.read_bytes(), "custom", canvas[0], canvas[1], 1.0)
        else:
            b, w, h = preprocess_image(p.read_bytes(), params.get("res_mode", "native"),
                                       params.get("custom_w"), params.get("custom_h"),
                                       float(params.get("native_scale", 1.0)))
        pn = f"proc_{uuid.uuid4().hex[:10]}.png"
        (wd / "media" / pn).write_bytes(b)
        try:
            shutil.copy2(wd / "media" / pn, COMFYUI_INPUT / pn)
        except Exception as e:
            print("[generate] copy proc to ComfyUI/input failed:", e, flush=True)
        return pn, w, h

    # frame_mode 决定接线: first->首帧口; last->尾帧口; both->首+尾 (尾帧对齐首帧画布)
    proc_first = proc_last = None
    tw = th = None
    if frame_mode == "both":
        proc_first, tw, th = _load_proc(raw_image)
        proc_last, _, _ = _load_proc(raw_image_last, canvas=(tw, th))
    elif frame_mode == "last":
        proc_last, tw, th = _load_proc(raw_image)
    elif raw_image:
        proc_first, tw, th = _load_proc(raw_image)
    if proc_first is None and proc_last is None:  # t2v: 画布 = custom 尺寸 (snap32 + 面积上限, 同 r2v)
        tw = snap32(int(params.get("custom_w") or 1280))
        th = snap32(int(params.get("custom_h") or 720))
        if tw * th > MAX_PIXELS:
            tw, th = compute_native_target(tw, th)
    frames = duration_to_frames(int(params.get("duration", 5)))
    params["width"], params["height"], params["length"] = tw, th, frames
    g = build_graph(prompt, params, proc_first, tw, th, frames, last_image_name=proc_last)
    return await _launch_job(name, request.app["session"], params, prompt, g,
                             proc_first or proc_last, None, task_type, tw, th, frames, image_last=proc_last)


def _validate_ref_fname(fn):
    """参考素材文件名安全校验 + 须存在于工作区。"""
    if not fn or not isinstance(fn, str):
        return False
    if "/" in fn or "\\" in fn or fn.startswith("..") or fn.strip() != fn:
        return False
    return True


async def _generate_r2v(request, name, data, prompt, params):
    """r2v: 多素材参考 (核心节点链)。图/视频不预处理, 原样 + ref_image_size=match 交给节点。
    P3: 视频可单独作参考 (video editing/continuation), 故图允许 0 张。"""
    wd = ws_dir(name)
    refs = data.get("refs") or {}
    ref_images = [f for f in (refs.get("images") or []) if _validate_ref_fname(f)]
    ref_videos = [f for f in (refs.get("videos") or []) if _validate_ref_fname(f)]
    if len(ref_images) > R2V_MAX_IMAGES:
        raise web.HTTPBadRequest(text=f"r2v 参考图最多 {R2V_MAX_IMAGES} 张 (收到 {len(ref_images)})")
    if not ref_images and not ref_videos:
        raise web.HTTPBadRequest(text="r2v 需要至少 1 张参考图或 1 段参考视频")
    missing = [f for f in ref_images if not (wd / "media" / f).exists()]
    if missing:
        raise web.HTTPBadRequest(text=f"参考图不存在, 请重新上传: {', '.join(missing[:3])}")
    # <Picture N> 引用编号不得超过实际图片数 (官方位置引用语义)
    pic_nums = [int(m) for m in re.findall(r"<Picture\s+(\d+)\s*>", prompt)]
    if pic_nums and max(pic_nums) > len(ref_images):
        raise web.HTTPBadRequest(
            text=f"提示词引用了 <Picture {max(pic_nums)}>, 但只上传了 {len(ref_images)} 张参考图")
    # P2: 参考音频 (0..R2V_MAX_AUDIOS 段)。官方: 音频不能作为唯一参考 (图已强制 1..9, 天然满足)
    ref_audios = [f for f in (refs.get("audios") or []) if _validate_ref_fname(f)]
    if len(ref_audios) > R2V_MAX_AUDIOS:
        raise web.HTTPBadRequest(text=f"r2v 参考音频最多 {R2V_MAX_AUDIOS} 段 (收到 {len(ref_audios)})")
    missing_a = [f for f in ref_audios if not (wd / "media" / f).exists()]
    if missing_a:
        raise web.HTTPBadRequest(text=f"参考音频不存在, 请重新上传: {', '.join(missing_a[:3])}")
    # P3.5: 音频用途标注 (voice/copy/bgm) —— 纯提示词语义, 不影响构图; 仅校验枚举并入历史
    audio_roles = refs.get("audio_roles") or {}
    if not isinstance(audio_roles, dict) or any(
            not isinstance(v, str) or v not in ("voice", "copy", "bgm") for v in audio_roles.values()):
        raise web.HTTPBadRequest(text="audio_roles 必须是 {文件名: voice/copy/bgm}")
    audio_roles = {f: audio_roles[f] for f in ref_audios if f in audio_roles}
    total_audio_s = 0.0
    for af in ref_audios:  # 逐段 2–15s 上传时已校验, 这里兜底复核可解码 + 总时长 ≤15s
        info = _probe_media(wd / "media" / af)
        if not info.get("ok") or info.get("kind") != "audio":
            raise web.HTTPBadRequest(text=f"参考音频无法解码: {af}")
        total_audio_s += float(info.get("duration_s") or 0)
    if ref_audios and total_audio_s > R2V_AUDIO_TOTAL_MAX_S:
        raise web.HTTPBadRequest(
            text=f"参考音频总时长 {total_audio_s:.1f}s 超过 {R2V_AUDIO_TOTAL_MAX_S:.0f}s 上限")
    # P3: 参考视频 (0..R2V_MAX_VIDEOS 段)。逐段兜底复核 2–15s 与可解码性 (upload_media 已校验, 此处防绕过);
    #     总时长 ≤15s; 混合文件总数 (图+视频+音频) ≤12 (官方规格)
    if len(ref_videos) > R2V_MAX_VIDEOS:
        raise web.HTTPBadRequest(text=f"r2v 参考视频最多 {R2V_MAX_VIDEOS} 段 (收到 {len(ref_videos)})")
    missing_v = [f for f in ref_videos if not (wd / "media" / f).exists()]
    if missing_v:
        raise web.HTTPBadRequest(text=f"参考视频不存在, 请重新上传: {', '.join(missing_v[:3])}")
    use_video_audio = refs.get("use_video_audio", True)
    if not isinstance(use_video_audio, bool):
        raise web.HTTPBadRequest(text="use_video_audio 必须是布尔值 (true/false)")
    video_audio_flags = []
    total_video_s = 0.0
    for vf in ref_videos:
        info = _probe_media(wd / "media" / vf)
        if not info.get("ok") or info.get("kind") != "video":
            raise web.HTTPBadRequest(text=f"参考视频无法解码: {vf}")
        d = info.get("duration_s")
        if d is None or not (R2V_VIDEO_MIN_S <= d <= R2V_VIDEO_MAX_S):
            raise web.HTTPBadRequest(
                text=f"参考视频时长越界 (需 {R2V_VIDEO_MIN_S:.0f}–{R2V_VIDEO_MAX_S:.0f}s): {vf} ({'未知' if d is None else str(d)+'s'})")
        total_video_s += float(d)
        # 原声开关 + 实际含音轨才接线 (官方: 音轨独立占用 <Audio N> 编号, 先于独立音频)
        video_audio_flags.append(bool(use_video_audio and info.get("has_audio")))
    if ref_videos and total_video_s > R2V_VIDEO_TOTAL_MAX_S:
        raise web.HTTPBadRequest(
            text=f"参考视频总时长 {total_video_s:.1f}s 超过 {R2V_VIDEO_TOTAL_MAX_S:.0f}s 上限")
    if len(ref_images) + len(ref_videos) + len(ref_audios) > R2V_REF_TOTAL_MAX:
        raise web.HTTPBadRequest(
            text=f"参考素材总数 (图+视频+音频) 不能超过 {R2V_REF_TOTAL_MAX} 个 (当前 {len(ref_images) + len(ref_videos) + len(ref_audios)})")
    vid_nums = [int(m) for m in re.findall(r"<Video\s+(\d+)\s*>", prompt)]
    if vid_nums and max(vid_nums) > len(ref_videos):
        raise web.HTTPBadRequest(
            text=f"提示词引用了 <Video {max(vid_nums)}>, 但只上传了 {len(ref_videos)} 段参考视频")
    # <Audio N> 编号含视频原声占位: 音轨按视频顺序先于独立音频获得编号 (官方 conditioning 顺序)
    audio_base = sum(1 for f in video_audio_flags if f)
    aud_nums = [int(m) for m in re.findall(r"<Audio\s+(\d+)\s*>", prompt)]
    if aud_nums and max(aud_nums) > audio_base + len(ref_audios):
        raise web.HTTPBadRequest(
            text=f"提示词引用了 <Audio {max(aud_nums)}>, 但可用音频编号只有 {audio_base + len(ref_audios)} 个"
                 f" (视频原声占 {audio_base} 个 + 独立音频 {len(ref_audios)} 段)")
    # r2v 无 native 语义: 固定 custom 画布 (默认 864×480), 不做 cover 预处理
    cw = snap32(int(params.get("custom_w") or R2V_DEFAULT_W))
    ch = snap32(int(params.get("custom_h") or R2V_DEFAULT_H))
    if cw * ch > MAX_PIXELS:
        cw, ch = compute_native_target(cw, ch)
    params["res_mode"] = "custom"
    frames = duration_to_frames(int(params.get("duration", 5)))
    params["width"], params["height"], params["length"] = cw, ch, frames
    # r2v 权重缺失提示 (文件不存在时 ComfyUI 会拒绝, 提前告知)
    r2v_name = find_ref2va_model()
    if not model_file_present(r2v_name):
        print(f"[r2v] 警告: ref2va 权重未找到 ({r2v_name}), 提交将被 ComfyUI 拒绝", flush=True)
    g = build_ref2va_graph(prompt, params, ref_images, cw, ch, frames, ref_audios,
                           ref_videos, video_audio_flags)
    refs_full = {"images": ref_images,
                 "audios": ref_audios,
                 "videos": ref_videos,
                 "use_video_audio": use_video_audio,
                 "audio_roles": audio_roles,
                 "video_audios": [vf for vf, f in zip(ref_videos, video_audio_flags) if f]}
    return await _launch_job(name, request.app["session"], params, prompt, g,
                             ref_images[0] if ref_images else None, refs_full, "r2v", cw, ch, frames)


async def _launch_job(name, session, params, prompt, g, image_field, refs, task_type, tw, th, frames,
                      image_last=None):
    try:
        async with session.post(f"{COMFYUI_URL}/prompt",
                                json={"prompt": g, "client_id": CLIENT_ID}, timeout=30) as r:
            resp = await r.json()
    except Exception as e:
        raise web.HTTPBadRequest(text=f"连接 ComfyUI 失败: {e}")
    if "error" in resp or "prompt_id" not in resp:
        err = resp.get("error", resp) if isinstance(resp.get("error"), dict) else resp
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        node_errs = err.get("node_errors", {}) if isinstance(err, dict) else {}
        hints = []
        for nid, ne in node_errs.items():
            cls = ne.get("class_type", "")
            for ce in (ne.get("errors") or []):
                hints.append(f"[{nid} {cls}] {ce.get('message','') or ce}")
        detail = msg + ((" | " + " ; ".join(hints)) if hints else "")
        if "failed_validation" in str(err) or "not found" in str(err).lower():
            detail += "（常见原因：所选模型文件未放入 ComfyUI/models/，或节点参数无效）"
        raise web.HTTPBadRequest(text=f"ComfyUI 拒绝: {detail[:700]}")
    pid = resp["prompt_id"]
    gen_id = uuid.uuid4().hex[:12]
    JOBS[pid] = {
        "queue": asyncio.Queue(), "session": session, "pid": pid,
        "ws": name, "gen_id": gen_id, "params": dict(params), "prompt": prompt,
        "image": image_field, "image_last": image_last, "refs": refs, "task_type": task_type,
        "status": "running", "video": None, "audio": None,
        "error": None, "t0": time.time(),
    }
    return web.json_response({"job_id": pid, "gen_id": gen_id, "width": tw, "height": th, "length": frames})


async def job_events(request):
    job_id = request.match_info["job_id"]
    job = JOBS.get(job_id)
    if not job:
        raise web.HTTPNotFound(text="job 不存在")
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q: asyncio.Queue = job["queue"]

    def _sse(ev: dict) -> bytes:
        return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")

    try:
        fin = job.get("final")
        if fin:
            # 断线重连: 终态已闩锁, 直接重放并结束
            await resp.write(_sse(fin))
            return resp
        last = job.get("last")
        last_seq = 0
        if last:
            # 重连: 先补发最近一次事件 (progress/status), 前端即刻恢复显示
            last_seq = int(last.get("seq") or 0)
            await resp.write(_sse(last))
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=12.0)
                if int(ev.get("seq") or 0) <= last_seq:
                    continue  # 重连场景: 跳过已被 last 重放覆盖的堆积旧事件
                await resp.write(_sse(ev))
                if ev.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                fin = job.get("final")
                if fin:
                    # 兜底: 终态事件被已断开的旧连接消费时, 在此补发 (最多延迟一个 keepalive 周期)
                    await resp.write(_sse(fin))
                    break
                await resp.write(b": keepalive\n\n")
    except Exception as e:
        print("[sse] err", e, flush=True)
    return resp


async def media(request):
    name = safe_name(request.match_info["name"])
    fname = request.match_info["file"]
    # 防目录穿越
    if "/" in fname or "\\" in fname or ".." in fname:
        raise web.HTTPBadRequest()
    p = ws_dir(name) / "media" / fname
    if not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p)


async def comfy_status(request):
    session: ClientSession = request.app["session"]
    r2v_name = find_ref2va_model()
    base = {
        "comfyui_url": COMFYUI_URL,
        "models": list(MODELS.keys()), "max_pixels": MAX_PIXELS, "durations": DURATIONS, "res_presets": RES_PRESETS,
        # r2v 权重探测: 名称(发现到的或占位) + 是否已存在于 models 目录
        "ref2va_model": r2v_name, "ref2va_present": model_file_present(r2v_name),
    }
    try:
        async with session.get(f"{COMFYUI_URL}/system_stats", timeout=5) as r:
            st = await r.json()
        dev = (st.get("devices") or [{}])[0]
        return web.json_response({
            "up": True,
            "vram_total": dev.get("vram_total"), "vram_free": dev.get("vram_free"),
            "torch": st.get("system", {}).get("torch_version"),
            **base,
        })
    except Exception as e:
        return web.json_response({"up": False, "error": str(e), **base})


async def interrupt(request):
    session: ClientSession = request.app["session"]
    pid = (await request.json()).get("job_id")
    try:
        async with session.post(f"{COMFYUI_URL}/interrupt", json={"prompt_id": pid}, timeout=10) as r:
            await r.read()
    except Exception:
        pass
    if pid in JOBS:
        push_event(JOBS[pid], {"type": "error", "message": "已中断"})
    return web.json_response({"ok": True})


# ==================== 启动 ====================
async def on_startup(app):
    app["session"] = ClientSession()
    app["stop"] = False
    app["ws_task"] = asyncio.create_task(comfy_ws_loop(app))


async def on_cleanup(app):
    app["stop"] = True
    # 等待仍在进行的收尾任务 (轮询 history/拷贝视频/写历史) 落地, 再关 session
    tasks = [j["fin_task"] for j in JOBS.values()
             if j.get("fin_task") and not j["fin_task"].done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await app["session"].close()


@web.middleware
async def error_middleware(request, handler):
    """捕获所有异常, 统一返回 JSON {error, status, traceback} 而非 aiohttp 默认错误页。"""
    try:
        return await handler(request)
    except web.HTTPException as e:
        if e.status < 400:
            return e
        import traceback
        tb = traceback.format_exc() if e.status >= 500 else ""
        print(f"[warn] {request.method} {request.path} -> {e.status} {e.text}", flush=True)
        return web.json_response({"error": e.text or e.reason, "status": e.status, "traceback": tb}, status=e.status)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[error] {request.method} {request.path}: {e}\n{tb}", flush=True)
        return web.json_response({"error": f"{type(e).__name__}: {e}", "traceback": tb}, status=500)


def make_app():
    app = web.Application(middlewares=[error_middleware], client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    api = "/api"
    app.router.add_get(f"{api}/workspaces", get_workspaces)
    app.router.add_post(f"{api}/workspaces", create_workspace)
    app.router.add_delete(f"{api}/workspaces/{{name}}", delete_workspace)
    app.router.add_get(f"{api}/workspaces/{{name}}/generations", get_generations)
    app.router.add_delete(f"{api}/workspaces/{{name}}/generations/{{gid}}", delete_generation)
    app.router.add_post(f"{api}/workspaces/{{name}}/upload-image", upload_image)
    app.router.add_post(f"{api}/workspaces/{{name}}/upload-media", upload_media)
    app.router.add_post(f"{api}/workspaces/{{name}}/probe-media", probe_media)
    app.router.add_post(f"{api}/workspaces/{{name}}/preview-resolution", preview_resolution)
    app.router.add_post(f"{api}/workspaces/{{name}}/extract-frame", extract_frame)
    app.router.add_post(f"{api}/workspaces/{{name}}/generate", generate)
    app.router.add_get(f"{api}/jobs/{{job_id}}/events", job_events)
    app.router.add_post(f"{api}/interrupt", interrupt)
    app.router.add_get(f"{api}/workspaces/{{name}}/media/{{file}}", media)
    app.router.add_get(f"{api}/comfyui/status", comfy_status)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    print("=" * 60)
    print("MiniMax-H3 视频生成 WebUI")
    print(f"  ComfyUI: {COMFYUI_URL}")
    print(f"  工作区目录: {WORKSPACES_DIR}")
    print(f"  监听: http://{HOST}:{PORT}")
    print("=" * 60)
    web.run_app(make_app(), host=HOST, port=PORT)
