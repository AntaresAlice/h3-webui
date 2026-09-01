"""MiniMax H3 冒烟测试: v1 pruned, 绕过 turbo LoRA (v1 不用 turbo),
小尺寸 640x640 / 56帧(~2.3s, 17n+5 网格最小且>=2s), 验证端到端链路:
模型加载 -> T8 conditioning -> DualClockSampler -> AVDecode -> VHS 出片。

用法 (需先启动 ComfyUI 在 127.0.0.1:8188):
  python h3_i2v_smoke.py             # 普通冒烟 (不加 sage, 可托管启动 ComfyUI)
  python h3_i2v_smoke.py --sage      # sage 路径冒烟 (须手动启动 ComfyUI, 坑3)
"""
import json, requests, time, uuid, argparse

API = "http://127.0.0.1:8188"

def main():
    ap = argparse.ArgumentParser(description="H3 v1 pruned 冒烟")
    ap.add_argument("--sage", action="store_true", help="启用 SageAttention 路径 (须手动启动 ComfyUI)")
    ap.add_argument("--img", default="example.png", help="参考图 (ComfyUI/input/ 下)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--length", type=int, default=56, help="帧数 17n+5: 22/39/56/73/... 默认56(~2.3s)")
    ap.add_argument("--steps", type=int, default=4)
    args = ap.parse_args()

    W, H = 640, 640  # 1x1 画幅
    unet = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    prompt = "a quick smoke test: a person gently waving in a sunlit garden, soft natural daylight, cinematic"
    prefix = f"SMOKE_pruned_{args.steps}step_1x1{'_sage' if args.sage else ''}"

    g = {
        "1":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "2":  {"class_type": "VAELoader",  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "3":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "4":  {"class_type": "UNETLoader","inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "6":  {"class_type": "LoadImage", "inputs": {"image": args.img, "upload": "image"}},
        "7":  {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
            "prompt": prompt, "width": W, "height": H, "length": args.length,
            "task_type": "I2VA", "audio_mode": "native", "audio_denoise_strength": 1.0,
            "add_source_as_reference": True, "prompt_primary_audio_ordinal": 0,
            "strict_prompt_tags": True, "ref_image_size": "match",
            "reference_video_policy": "official_2_to_15s",
            "clip": ["3", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0],
            "first_frame": ["6", 0],
        }},
        # node 8 sampler model defaults to UNETLoader(4); rewired to 4a if --sage (见末尾)
        "8":  {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {
            "steps": args.steps, "shift_video": 12.0, "shift_audio": 3.0,
            "model": ["4", 0], "av_latent": ["7", 1],
        }},
        "9":  {"class_type": "RandomNoise",            "inputs": {"noise_seed": args.seed}},
        "10": {"class_type": "BasicGuider",            "inputs": {"model": ["8", 0], "conditioning": ["7", 0]}},
        "11": {"class_type": "SamplerCustomAdvanced",   "inputs": {
            "noise": ["9", 0], "guider": ["10", 0],
            "sampler": ["8", 1], "sigmas": ["8", 2], "latent_image": ["7", 1],
        }},
        "12": {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {"av_latent": ["11", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]}},
        "13": {"class_type": "VHS_VideoCombine", "inputs": {
            "frame_rate": 24, "loop_count": 0, "filename_prefix": prefix,
            "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 19,
            "save_metadata": True, "trim_to_audio": False, "pingpong": False,
            "save_output": True, "images": ["12", 0], "audio": ["12", 1],
        }},
    }
    if args.sage:
        g["4a"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["4", 0]}}
        g["8"]["inputs"]["model"] = ["4a", 0]

    client_id = str(uuid.uuid4()); t0 = time.time()
    print(f">> v1 pruned | {W}x{H} | {args.steps}步 | {args.length}帧({args.length/24:.1f}s) | sage={'ON' if args.sage else 'OFF'} | img={args.img}")
    try:
        r = requests.post(f"{API}/prompt", json={"prompt": g, "client_id": client_id}, timeout=30)
    except Exception as e:
        print(f"连接 ComfyUI 失败 ({API}): {e}"); raise SystemExit(1)
    if r.status_code != 200:
        print("提交失败:", r.text[:600]); raise SystemExit(1)
    pid = r.json().get("prompt_id"); print(f"已提交 prompt_id={pid}, 开始计时...")

    for i in range(120):
        time.sleep(15)
        h = requests.get(f"{API}/history/{pid}", timeout=10).json()
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("completed"):
                dur = time.time() - t0; print(f"OK 完成! {dur:.1f}s ({dur/60:.1f}min)")
                for nid, o in h[pid].get("outputs", {}).items():
                    for k, v in o.items():
                        if isinstance(v, list):
                            for it in v: print("  OUT:", it.get("filename", it.get("type", "?")))
                return
            for tt, mm in st.get("messages", [])[-3:]:
                if tt != "execution_start": print(f"  [{tt}] {str(mm)[:140]}")
            errs = st.get("status_str")
            if errs and "error" in str(errs).lower(): print(f"  !! {errs}")
        else:
            if i % 4 == 0: print(f"[{i+1}] 生成中... {time.time()-t0:.0f}s")
    print("超时")

if __name__ == "__main__":
    main()
