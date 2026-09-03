"""P2 音频参考冒烟: 上传 1 图 + 1 音频(3s) -> r2v 生成 (4 步) -> 验证入库/视频;
负例: 20s 音频上传被拒(400), <Audio 2> 引用越界被拒(400)。
用法: D:\\SoftwareStation\\ComfyUI\\python_embeded\\python.exe p2_audio_smoke.py
"""
import asyncio
import io
import json
import math
import struct
import time
import wave
from pathlib import Path
import aiohttp

BASE = "http://127.0.0.1:8080"
WS_NAME = "p2_audio_smoke"
COMFY_INPUT = Path(r"D:\SoftwareStation\ComfyUI\ComfyUI\input")


def make_png() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (512, 512))
    d = ImageDraw.Draw(img)
    for y in range(512):
        d.line([(0, y), (511, y)],
               fill=(int(30 + y * 0.3), int(60 + y * 0.2), int(120 + y * 0.25)))
    d.ellipse([160, 140, 352, 380], fill=(240, 220, 190))      # 脸
    d.rectangle([150, 380, 362, 460], fill=(60, 90, 160))      # 衣服
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def make_wav(seconds: float) -> bytes:
    rate = 22050
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(8000 * math.sin(2 * math.pi * 220 * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


async def upload(session, kind, data, fname):
    fd = aiohttp.FormData()
    fd.add_field("type", kind)
    fd.add_field("file", data, filename=fname)
    async with session.post(f"{BASE}/api/workspaces/{WS_NAME}/upload-media", data=fd) as r:
        body = await r.json()
        print(f"upload {kind} {fname}: http {r.status} -> {body}")
        return r.status, body


PROMPT = """subject_definitions:
<Picture 1> is the reference for <Subject 1>.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).

summary:
<Subject 1> (S1) speaks a short greeting.

retention_analysis:
<Subject 1> must keep identity from <Picture 1>; voice timbre like <Audio 1> without copying its wording.

detailed_description:
[Shot 1] static front medium shot. <Subject 1> (S1) says <d>[Chinese] 你好，这是音频参考测试。</d>

overall_soundscape:
Clean voice, quiet room.

non_diegetic_music:
N/A"""

PARAMS = {"task_type": "r2v", "model": "ref2va", "steps": 4, "duration": 2,
          "res_mode": "custom", "custom_w": 640, "custom_h": 480, "seed": 1234,
          "low_vram": True, "sage": False, "turbo_lora": False}


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.post(BASE + "/api/workspaces", json={"name": WS_NAME}) as r:
            print("create workspace:", r.status)

        st_i, img = await upload(s, "image", make_png(), "ref_face.png")
        st_a, aud = await upload(s, "audio", make_wav(3.0), "voice_3s.wav")
        st_l, _ = await upload(s, "audio", make_wav(20.0), "voice_20s.wav")  # 期望 400
        print(">>> 负例1 20s 音频: 期望 400, 实际", st_l)

        refs = {"images": [img["filename"]], "audios": [aud["filename"]]}
        async with s.post(f"{BASE}/api/workspaces/{WS_NAME}/generate",
                          json={"prompt": PROMPT.replace("<Audio 1>", "<Audio 2>", 1),
                                "params": PARAMS, "refs": refs}) as r:
            txt = (await r.text())[:200]
            print(">>> 负例2 <Audio 2> 越界: 期望 400, 实际", r.status, txt)

        async with s.post(f"{BASE}/api/workspaces/{WS_NAME}/generate",
                          json={"prompt": PROMPT, "params": PARAMS, "refs": refs}) as r:
            body = await r.json()
            print("generate:", r.status, body)
            if r.status != 200:
                return
            jid = body["job_id"]

        t0 = time.time()
        async with s.get(f"{BASE}/api/jobs/{jid}/events") as r:
            async for raw in r.content:
                if not raw.startswith(b"data: "):
                    continue
                ev = json.loads(raw[6:])
                if ev.get("type") == "progress":
                    print(f"  step {ev.get('value')}/{ev.get('max')}  ({time.time()-t0:.0f}s)")
                elif ev.get("type") == "done":
                    print("done in", round(time.time() - t0, 1), "s")
                    break
                elif ev.get("type") == "error":
                    print("JOB ERROR:", ev.get("message")); return

        async with s.get(f"{BASE}/api/workspaces/{WS_NAME}/generations") as r:
            gens = await r.json()
        rec = next((g for g in gens if g.get("job_id") == jid), None) or gens[0]
        print("入库 refs.audios:", rec.get("refs", {}).get("audios"))
        print("入库 video:", rec.get("video"))
        if rec.get("video"):
            async with s.get(f"{BASE}/api/workspaces/{WS_NAME}/media/{rec['video']}") as r:
                head = await r.read()
            print("video 头字节:", head[:12].hex(), "大小:", len(head))

        # 清理: 删除上传文件在 ComfyUI/input 的副本
        for f in ([img["filename"], aud["filename"]] if st_i == 200 else []):
            p = COMFY_INPUT / f
            if p.exists():
                p.unlink()
                print("cleaned input copy:", f)


asyncio.run(main())
