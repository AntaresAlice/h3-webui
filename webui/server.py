"""
MiniMax-H3 视频生成 WebUI 后端 (aiohttp)
- 代理 ComfyUI (http://127.0.0.1:8188): 提交 prompt 图、WebSocket 拿真实步数进度、SSE 推给前端
- 工作区管理: 每个工作区一个目录, generations.json + media/ (输入图 + 输出视频)
- 视频按 Range 流式播放 (支持拖动进度条)
运行: 使用 ComfyUI 自带的 python (含 aiohttp/requests/PIL), 例如:
  <ComfyUI根目录>\python_embeded\python.exe server.py
"""
import os, sys, json, time, uuid, base64, shutil, asyncio, mimetypes, io, math, random
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

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
WORKSPACES_DIR = BASE / "workspaces"
WORKSPACES_DIR.mkdir(exist_ok=True)

# ComfyUI 客户端 id (ws 与 /prompt 共用, 这样进度消息回到我们的 ws)
CLIENT_ID = "h3webui-" + uuid.uuid4().hex[:12]

MODELS = {
    "pruned": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "full":   "minimax_h3_fl2va_int8_convrot.safetensors",
}
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
def build_graph(prompt: str, params: dict, image_name: str, width: int, height: int, length: int) -> dict:
    model = MODELS.get(params.get("model", "pruned"), MODELS["pruned"])
    steps = int(params.get("steps", 4))
    seed = int(params.get("seed", 0))
    prefix = f"H3_{params.get('model','pruned')}_{steps}step_{width}x{height}_{length}f{'_sage' if params.get('sage') else ''}{'_lo' if params.get('low_vram', True) else ''}"
    g = {
        "1":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "2":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "6":  {"class_type": "LoadImage",  "inputs": {"image": image_name, "upload": "image"}},
        "7":  {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
            "prompt": prompt, "width": width, "height": height, "length": length,
            "task_type": "I2VA", "audio_mode": "native", "audio_denoise_strength": 1.0,
            "add_source_as_reference": True, "prompt_primary_audio_ordinal": 0,
            "strict_prompt_tags": True, "ref_image_size": "match",
            "reference_video_policy": "official_2_to_15s",
            "clip": ["3", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0],
            "first_frame": ["6", 0]}},
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


# ==================== ComfyUI WebSocket 监听 ====================
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
        await job["queue"].put({"type": "progress", "value": d.get("value", 0), "max": d.get("max", 0),
                                 "node": d.get("node", "")})
    elif t == "executing" and job:
        node = d.get("node")
        if node:
            await job["queue"].put({"type": "status", "text": f"执行节点 {node}"})
    elif t == "execution_success" and job:
        await finish_job(job)
    elif t == "execution_error" and job:
        msg = str(d.get("exception_message") or d.get("node_type") or "执行出错")
        tb = d.get("traceback") or d.get("exception_traceback") or ""
        job["error"] = msg
        await job["queue"].put({"type": "error", "message": msg, "traceback": tb})
    elif t == "execution_interrupted" and job:
        await job["queue"].put({"type": "error", "message": "已中断"})
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
        for nid, out in outputs.items():
            for img in (out.get("images") or out.get("gifs") or []):
                fn = img.get("filename")
                if fn and fn.lower().endswith((".mp4", ".webm", ".gif", ".mov")):
                    if not video:
                        video = (fn, img.get("subfolder", ""), img.get("type", "output"))
            for au in (out.get("audio") or []):
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
                shutil.copy2(src, wd / "media" / vdest)
        if audio:
            src = comfy_out_path(*audio)
            if src and src.exists():
                adest = f"{job['gen_id']}_audio{src.suffix}"
                shutil.copy2(src, wd / "media" / adest)
        job["video"] = vdest
        job["audio"] = adest
        job["status"] = "done"
        # 写历史
        gens = load_gens(ws_name)
        rec = {
            "id": job["gen_id"], "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": job["prompt"], "params": job["params"], "image": job["image"],
            "video": vdest, "audio": adest, "duration": dur, "seed": job["params"].get("seed"),
        }
        gens.insert(0, rec)
        save_gens(ws_name, gens)
        await job["queue"].put({"type": "done", "video": vdest, "audio": adest, "duration": dur, "gen_id": job["gen_id"]})
    except Exception as e:
        import traceback as _tb
        job["error"] = f"收尾失败: {e}"
        await job["queue"].put({"type": "error", "message": job["error"], "traceback": _tb.format_exc()})


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
            for k in ("video", "audio", "image"):
                fn = g.get(k)
                if fn and "/" not in fn and "\\" not in fn:
                    p = wd / "media" / fn
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


async def extract_frame(request):
    """从工作区视频截取一帧 (默认最后一帧), 保存为 PNG 并拷到 ComfyUI/input。
    返回 {filename} 可直接当作参考图。"""
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
        import av  # PyAV (ComfyUI 依赖, 用于读视频帧)
        container = av.open(str(vp))
        stream = container.streams.video[0]
        total_frames = stream.frames or 0
        if position == "first":
            target_ts = 0
        elif position == "last":
            # seek 到最后附近, 读最后一帧
            target_ts = float(stream.duration * stream.time_base) if stream.duration else 0
        else:
            # position 是 0~1 比例
            target_ts = float(position) * (float(stream.duration * stream.time_base) if stream.duration else 0)
        container.seek(int(target_ts / stream.time_base), stream=stream) if target_ts else None
        last_frame = None
        for frame in container.decode(video=0):
            last_frame = frame
        container.close()
        if last_frame is None:
            raise web.HTTPBadRequest(text="无法解码视频帧")
        img = last_frame.to_image().convert("RGB")
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
    name = safe_name(request.match_info["name"])
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    params = data.get("params") or {}
    raw_image = data.get("image")  # 已上传的原始图文件名
    if not prompt:
        raise web.HTTPBadRequest(text="请填写提示词")
    if not raw_image:
        raise web.HTTPBadRequest(text="请上传参考图")
    params.setdefault("model", "pruned")
    params.setdefault("steps", 4)
    params.setdefault("res_mode", "native")
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
    # 图像预处理: 按分辨率模式裁剪/缩放到 32 倍数 (节点会把图拉伸到 w×h, 故先处理好)
    raw_path = ws_dir(name) / "media" / raw_image
    if not raw_path.exists():
        raise web.HTTPBadRequest(text="参考图不存在, 请重新上传")
    proc_bytes, tw, th = preprocess_image(raw_path.read_bytes(), params.get("res_mode", "native"),
                                          params.get("custom_w"), params.get("custom_h"),
                                          float(params.get("native_scale", 1.0)))
    frames = duration_to_frames(int(params.get("duration", 5)))
    proc_name = f"proc_{uuid.uuid4().hex[:10]}.png"
    wd = ws_dir(name)
    (wd / "media" / proc_name).write_bytes(proc_bytes)
    try:
        shutil.copy2(wd / "media" / proc_name, COMFYUI_INPUT / proc_name)
    except Exception as e:
        print("[generate] copy proc to ComfyUI/input failed:", e, flush=True)
    params["width"], params["height"], params["length"] = tw, th, frames
    g = build_graph(prompt, params, proc_name, tw, th, frames)
    session: ClientSession = request.app["session"]
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
        "image": proc_name, "status": "running", "video": None, "audio": None,
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
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=12.0)
                await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                if ev.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
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
    try:
        async with session.get(f"{COMFYUI_URL}/system_stats", timeout=5) as r:
            st = await r.json()
        dev = (st.get("devices") or [{}])[0]
        return web.json_response({
            "up": True, "comfyui_url": COMFYUI_URL,
            "vram_total": dev.get("vram_total"), "vram_free": dev.get("vram_free"),
            "torch": st.get("system", {}).get("torch_version"),
            "models": list(MODELS.keys()), "max_pixels": MAX_PIXELS, "durations": DURATIONS, "res_presets": RES_PRESETS,
        })
    except Exception as e:
        return web.json_response({"up": False, "error": str(e), "comfyui_url": COMFYUI_URL,
                                  "models": list(MODELS.keys()), "max_pixels": MAX_PIXELS, "durations": DURATIONS, "res_presets": RES_PRESETS})


async def interrupt(request):
    session: ClientSession = request.app["session"]
    pid = (await request.json()).get("job_id")
    try:
        async with session.post(f"{COMFYUI_URL}/interrupt", json={"prompt_id": pid}, timeout=10) as r:
            await r.read()
    except Exception:
        pass
    if pid in JOBS:
        await JOBS[pid]["queue"].put({"type": "error", "message": "已中断"})
    return web.json_response({"ok": True})


# ==================== 启动 ====================
async def on_startup(app):
    app["session"] = ClientSession()
    app["stop"] = False
    app["ws_task"] = asyncio.create_task(comfy_ws_loop(app))


async def on_cleanup(app):
    app["stop"] = True
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
