"""验证 r2v 核心链的 SSE 进度是否步级准确 (value/max = steps)。"""
import io, json, time, requests
from PIL import Image

API = "http://127.0.0.1:8080"
s = requests.Session()
ws = "r2v_sse_check"
s.post(f"{API}/api/workspaces", json={"name": ws}, timeout=10)
buf = io.BytesIO(); Image.new("RGB", (512, 512), (120, 60, 200)).save(buf, format="PNG"); buf.seek(0)
fn = s.post(f"{API}/api/workspaces/{ws}/upload-media", data={"type": "image"},
            files={"file": ("s.png", buf, "image/png")}, timeout=20).json()["filename"]
body = {"prompt": "subject_definitions:\n<Picture 1> is <Subject 1>.\n\nsummary:\nshot.\n\nretention_analysis:\nkeep identity.\n\ndetailed_description:\n[Shot 1] stable shot.\n\noverall_soundscape:\nambience.\n\nnon_diegetic_music:\nN/A",
        "params": {"task_type": "r2v", "steps": 8, "res_mode": "custom", "custom_w": 640, "custom_h": 640,
                   "duration": 2, "seed": 7, "low_vram": True, "turbo_lora": True},
        "refs": {"images": [fn]}}
r = s.post(f"{API}/api/workspaces/{ws}/generate", json=body, timeout=30)
job = r.json(); print("submitted:", job["job_id"][:8], "steps=8 turbo")

prog = []
with s.get(f"{API}/api/jobs/{job['job_id']}/events", stream=True, timeout=300) as resp:
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        ev = json.loads(raw[6:])
        if ev["type"] == "progress":
            prog.append((ev.get("value"), ev.get("max")))
            if len(prog) in (1, 2, 8):
                print("  progress:", ev.get("value"), "/", ev.get("max"), "node:", ev.get("node"))
        elif ev["type"] == "done":
            print("done:", ev.get("video"), ev.get("duration"), "s")
            break
        elif ev["type"] == "error":
            print("ERROR:", ev.get("message")); break
maxes = {m for _, m in prog}
print("progress events:", len(prog), "| distinct max:", sorted(maxes))
print("step-accurate:", bool(prog) and all(m == 8 for m in maxes))
s.delete(f"{API}/api/workspaces/{ws}", timeout=10)
