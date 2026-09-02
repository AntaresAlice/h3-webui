"""MiniMax H3 Ref2VA (r2v) 冒烟测试: 多图参考 -> 核心节点链 -> SaveVideo 出片。

验证链路: UNETLoader(ref2va) -> [ChunkFeedForward/LowVRAMAttention] -> [Turbo LoRA]
          -> MiniMaxH3SigmaShift(12/3) -> MiniMaxH3ReferenceToVideo(挂 ref_images)
          -> res_multistep/simple 采样 -> VAEDecode/VAEDecodeAudio -> CreateVideo -> SaveVideo

用法 (需先启动 ComfyUI 在 127.0.0.1:8188, 且 ref2va 权重已放入 models/diffusion_models):
  python h3_r2v_smoke.py                    # 2 张自动生成的测试图 + 20 步 (非 turbo)
  python h3_r2v_smoke.py --img1 a.png --img2 b.png
  python h3_r2v_smoke.py --steps 8 --turbo  # 8 步 turbo LoRA 路径 (SigmaShift 必需)
  python h3_r2v_smoke.py --no-lowvram       # 关闭低显存补丁链 (16G 卡默认开启)
"""
import json, sys, time, uuid, argparse, math, struct, zlib

API = "http://127.0.0.1:8188"

# ---- 极简 PNG 生成 (无第三方依赖, 仅用于冒烟参考图) ----
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

def make_png(w: int, h: int, rgb: tuple) -> bytes:
    """生成纯色 PNG (RGB, 8bit)。"""
    row = b"\x00" + bytes(rgb) * w
    raw = row * h
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw)) + _png_chunk(b"IEND", b""))

def build_graph(unet, steps, seed, w, h, length, imgs, prompt, prefix,
                low_vram=True, sage=False, turbo=False, lora=None):
    g = {
        "1":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "2":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "7":  {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8":  {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["1", 0]}},
        "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["2", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["12", 0], "audio": ["13", 0], "fps": 24.0, "bit_depth": 8}},
        "15": {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"}},
    }
    src = "4"
    if low_vram:
        g["4a"] = {"class_type": "MiniMaxChunkFeedForward", "inputs": {"model": [src, 0], "chunks": 2, "seq_threshold": 4096}}
        src = "4a"
        g["4b"] = {"class_type": "MiniMaxLowVRAMAttention", "inputs": {"model": [src, 0], "head_chunks": 4}}
        src = "4b"
    if sage:
        g["4c"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": [src, 0]}}
        src = "4c"
    if turbo:
        g["5"] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": [src, 0], "lora_name": lora or "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "strength_model": 1.0}}
        src = "5"
    # SigmaShift: 核心链的 shift 载体
    g["5s"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": [src, 0], "shift_video": 12.0, "shift_audio": 3.0}}

    node6 = {"clip": ["3", 0], "vae": ["1", 0], "audio_vae": ["2", 0],
             "prompt": prompt, "width": w, "height": h, "length": length,
             "ref_image_size": "match"}
    nid = 20
    for i, fn in enumerate(imgs[:9]):
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": fn}}
        node6[f"ref_images.ref_image_{i}"] = [str(nid), 0]
        nid += 1
    g["6"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": node6}
    g["9"]  = {"class_type": "BasicScheduler", "inputs": {"model": ["5s", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}}
    g["10"] = {"class_type": "BasicGuider", "inputs": {"model": ["5s", 0], "conditioning": ["6", 0]}}
    g["11"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
        "sigmas": ["9", 0], "latent_image": ["6", 1]}}
    return g

SIX_PROMPT = """subject_definitions:
<Picture 1> is the reference for <Subject 1> (a person).
<Picture 2> is the reference for <Subject 2> (an object).

summary:
Generate one coherent audiovisual shot where both referenced subjects appear naturally.

retention_analysis:
<Subject 1> must preserve the same facial identity, hair, outfit and colors as <Picture 1>.
<Subject 2> must preserve the same shape, texture and colors as <Picture 2>.

detailed_description:
[Shot 1] A stable medium shot. <Subject 1> stands on the left holding <Subject 2> with both hands, slowly turning to face the camera. Natural lighting, shallow depth of field, cinematic. No text, subtitles, logos or watermarks.

overall_soundscape:
Soft room ambience, subtle fabric rustle.

non_diegetic_music:
N/A"""

def main():
    ap = argparse.ArgumentParser(description="H3 Ref2VA 冒烟 (多图参考, 核心节点链)")
    ap.add_argument("--unet", default="minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                    help="ref2va 权重 (实际以 ComfyUI 目录为准)")
    ap.add_argument("--img1", default=None, help="参考图1 (ComfyUI/input 下)")
    ap.add_argument("--img2", default=None, help="参考图2")
    ap.add_argument("--input-dir", default=None, help="ComfyUI/input 目录 (缺省时自动生成测试图)")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=640)
    ap.add_argument("--length", type=int, default=56, help="帧数 17n+5: 22/39/56/73/...")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--turbo", action="store_true", help="8 步 turbo LoRA 路径")
    ap.add_argument("--no-lowvram", action="store_true", help="关闭低显存补丁链")
    ap.add_argument("--prompt", default=SIX_PROMPT)
    args = ap.parse_args()

    if args.turbo:
        args.steps = 8
    W, H, L = args.width, args.height, args.length
    prefix = f"H3_R2V_SMOKE_{args.steps}step_{W}x{H}_{L}f{'_turbo' if args.turbo else ''}"
    imgs = []
    for tag, f in (("1", args.img1), ("2", args.img2)):
        if f:
            imgs.append(f)
    if not imgs:
        import pathlib, urllib.request
        ind = pathlib.Path(args.input_dir) if args.input_dir else None
        if not ind or not ind.is_dir():
            print("需要参考图: 用 --img1/--img2 指定 ComfyUI/input 下的文件, 或 --input-dir 提供目录(自动生成测试图)")
            raise SystemExit(1)
        # 上传两张生成图 (经 ComfyUI /upload/image)
        for k, rgb in (("1", (70, 130, 220)), ("2", (200, 120, 60))):
            png = make_png(512, 512, rgb)
            fn = f"r2v_smoke_ref{k}.png"
            bound = b"--" + b"dshboundary" + b"\r\n"
            body = (bound + f'Content-Disposition: form-data; name="image"; filename="{fn}"\r\nContent-Type: image/png\r\n\r\n'.encode()
                    + png + b"\r\n" + bound + b"--\r\n")
            req = urllib.request.Request(f"{API}/upload/image", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=dshboundary"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            imgs.append(fn)
        print(">> 已生成并上传测试参考图:", imgs)

    g = build_graph(args.unet, args.steps, args.seed, W, H, L, imgs, args.prompt, prefix,
                    low_vram=not args.no_lowvram, turbo=args.turbo)
    client_id = str(uuid.uuid4()); t0 = time.time()
    print(f">> r2v | {W}x{H} | {args.steps}步 | {L}帧({L/24:.1f}s) | turbo={'ON' if args.turbo else 'OFF'} | 低显存={'ON' if not args.no_lowvram else 'OFF'} | 图={len(imgs)}张")
    try:
        r = __import__("requests").post(f"{API}/prompt", json={"prompt": g, "client_id": client_id}, timeout=30)
    except Exception as e:
        print(f"连接 ComfyUI 失败 ({API}): {e}"); raise SystemExit(1)
    if r.status_code != 200:
        print("提交失败:", r.text[:800]); raise SystemExit(1)
    pid = r.json().get("prompt_id"); print(f"已提交 prompt_id={pid}, 开始计时...")

    for i in range(240):
        time.sleep(15)
        h = __import__("requests").get(f"{API}/history/{pid}", timeout=10).json()
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("completed"):
                dur = time.time() - t0; print(f"OK 完成! {dur:.1f}s ({dur/60:.1f}min)")
                for nid, o in h[pid].get("outputs", {}).items():
                    for k, v in o.items():
                        if isinstance(v, list):
                            for it in v:
                                print("  OUT:", it.get("filename"), "(subfolder:", it.get("subfolder"), ")")
                return
            for tt, mm in st.get("messages", [])[-3:]:
                if tt != "execution_start": print(f"  [{tt}] {str(mm)[:160]}")
            errs = st.get("status_str")
            if errs and "error" in str(errs).lower(): print(f"  !! {errs}")
        else:
            if i % 4 == 0: print(f"[{i+1}] 生成中... {time.time()-t0:.0f}s")
    print("超时")

if __name__ == "__main__":
    main()
