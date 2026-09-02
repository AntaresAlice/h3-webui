"""h3-webui r2v 端到端 API 测试 (走 WebUI 8080, 非直连 ComfyUI):
创建临时工作区 -> multipart 上传 2 张图 -> 提交 r2v 生成 -> 轮询 generations
直到有结果 -> 校验记录 refs/task_type/video 文件 -> 复用删除清理。

用法: python e2e_r2v_test.py [--steps 20] [--ws r2v_e2e]
"""
import json, sys, time, uuid, io, argparse
import requests
from PIL import Image

API = "http://127.0.0.1:8080"

SIX = """subject_definitions:
<Picture 1> is the reference for <Subject 1> (a person).

summary:
Generate one coherent audiovisual shot.

retention_analysis:
<Subject 1> must preserve the same facial identity, outfit and colors as <Picture 1>.

detailed_description:
[Shot 1] A stable medium shot of <Subject 1> slowly turning to face the camera. Cinematic, natural light. No text or watermarks.

overall_soundscape:
Soft ambience.

non_diegetic_music:
N/A"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="r2v_e2e_" + uuid.uuid4().hex[:6])
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--duration", type=int, default=2)
    ap.add_argument("--w", type=int, default=640)
    ap.add_argument("--h", type=int, default=640)
    ap.add_argument("--turbo", action="store_true")
    args = ap.parse_args()

    s = requests.Session()
    # 1. 工作区
    r = s.post(f"{API}/api/workspaces", json={"name": args.ws}, timeout=10)
    print("[1] workspace:", r.status_code, r.json() if r.ok else r.text[:300])
    assert r.ok

    # 2. 上传 2 张参考图 (multipart /upload-media)
    names = []
    for k, color in (("a", (90, 140, 210)), ("b", (205, 120, 60))):
        buf = io.BytesIO()
        Image.new("RGB", (512, 512), color).save(buf, format="PNG")
        buf.seek(0)
        files = {"file": (f"e2e_ref{k}.png", buf, "image/png")}
        r = s.post(f"{API}/api/workspaces/{args.ws}/upload-media",
                   data={"type": "image"}, files=files, timeout=30)
        print(f"[2] upload {k}:", r.status_code, r.json() if r.ok else r.text[:300])
        assert r.ok
        names.append(r.json()["filename"])

    # 3. 提交 r2v 生成
    params = {"task_type": "r2v", "model": "ref2va", "steps": args.steps,
              "res_mode": "custom", "custom_w": args.w, "custom_h": args.h,
              "duration": args.duration, "seed": 123, "low_vram": True,
              "sage": False, "turbo_lora": args.turbo}
    body = {"prompt": SIX, "params": params, "refs": {"images": names}}
    r = s.post(f"{API}/api/workspaces/{args.ws}/generate", json=body, timeout=40)
    print("[3] generate:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300] if r.ok else r.text[:500])
    assert r.ok
    job = r.json()

    # 4. 轮询 generations 直到记录出现且非 running
    t0 = time.time()
    rec = None
    while time.time() - t0 < 1800:
        time.sleep(10)
        gens = s.get(f"{API}/api/workspaces/{args.ws}/generations", timeout=10).json()
        hit = [g for g in gens if g.get("id") == job["gen_id"]]
        if hit:
            rec = hit[0]
            st = rec.get("status") or "done"
            print(f"[4] elapsed {time.time()-t0:.0f}s | status={st} | video={rec.get('video')} | error={str(rec.get('error'))[:200]}")
            if st != "running" and not st.startswith("tmp"):
                break
    assert rec, "超时无记录"

    # 5. 校验记录字段
    print("[5] task_type:", rec.get("task_type"), "| refs.images:", (rec.get("refs") or {}).get("images"))
    print("    video:", rec.get("video"), "| params model/steps:", rec.get("params", {}).get("model"), rec.get("params", {}).get("steps"))
    assert rec.get("task_type") == "r2v"
    assert rec.get("video"), "r2v 未产出视频"
    # 6. 清理
    r = s.delete(f"{API}/api/workspaces/{args.ws}", timeout=10)
    print("[6] cleanup workspace:", r.status_code)
    print("E2E PASS" if r.ok else "E2E cleanup issue (video file kept?)")


if __name__ == "__main__":
    main()
