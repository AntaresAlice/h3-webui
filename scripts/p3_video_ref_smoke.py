"""P3 视频参考冒烟 (进程内, 不依赖运行中的 ComfyUI):
1) build_ref2va_graph 视频接线: LoadVideo -> GetVideoComponents -> ref_videos / ref_video_audios
   (音轨仅对 has_audio 视频接线; 无视频时不产生视频节点)
2) 上传校验: 1s 视频拒(400) / 20s 视频拒(400) / 2.5s 带音轨过(has_audio=true) / 3s 无音轨过(has_audio=false)
3) 生成校验: <Video 2> 越界拒 / 3 段共 18s 总时长拒 / 混合 13 个拒 /
   <Audio 2>(音轨占位+独立音频)放行 / 纯视频无图放行 —— "放行"以
   '连接 ComfyUI 失败' 判定 (校验全过, 只差 ComfyUI 本体)
用法: qrhead python scripts/p3_video_ref_smoke.py
"""
import asyncio, io, math, os, pathlib, struct, sys, tempfile, wave

root = pathlib.Path(__file__).resolve().parent.parent
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="p3_smoke_"))
(_tmp / "input").mkdir()
(_tmp / "output").mkdir()
os.environ["COMFYUI_INPUT"] = str(_tmp / "input")
os.environ["COMFYUI_OUTPUT"] = str(_tmp / "output")
sys.path.insert(0, str(root / "webui"))
import server as S  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

WS = "p3_smoke"
PASSED = []


def ok(name):
    PASSED.append(name)
    print(f"[{len(PASSED)}] {name}  OK")


def make_png(w=64, h=48):
    from PIL import Image
    img = Image.new("RGB", (w, h), (120, 140, 180))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_wav(seconds=2.5):
    rate = 16000
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(6000 * math.sin(2 * math.pi * 300 * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def make_mp4(seconds, with_audio):
    """极小 h264(+aac) mp4; 24fps 纯灰帧, 音频为静音 (时长由流元数据体现)。"""
    import av
    buf = io.BytesIO()
    c = av.open(buf, mode="w", format="mp4")
    vs = c.add_stream("libx264", rate=24)
    vs.width, vs.height, vs.pix_fmt = 64, 48, "yuv420p"
    vs.options = {"preset": "ultrafast", "crf": "30"}
    asc = None
    if with_audio:
        asc = c.add_stream("aac", rate=32000)
        asc.layout = "mono"
    n = int(24 * seconds)
    for i in range(n):
        frame = av.VideoFrame(64, 48, "yuv420p")
        frame.pts = i
        for pkt in vs.encode(frame):
            c.mux(pkt)
    for pkt in vs.encode(None):
        c.mux(pkt)
    if asc is not None:
        step, sent = 1024, 0
        total = int(32000 * seconds)
        while sent < total:
            fr = av.AudioFrame(format="s16", layout="mono", samples=step)
            fr.planes[0].update(bytes(step * 2))
            fr.sample_rate = 32000
            fr.pts = sent
            for pkt in asc.encode(fr):
                c.mux(pkt)
            sent += step
        for pkt in asc.encode(None):
            c.mux(pkt)
    c.close()
    return buf.getvalue()


async def upload(client, kind, data, fname):
    import aiohttp
    fd = aiohttp.FormData()
    fd.add_field("type", kind)
    fd.add_field("file", data, filename=fname)
    r = await client.post(f"/api/workspaces/{WS}/upload-media", data=fd)
    body = await r.json()
    return r.status, body


async def gen(client, prompt, refs):
    r = await client.post(f"/api/workspaces/{WS}/generate", json={
        "prompt": prompt, "params": {"task_type": "r2v", "duration": 5}, "refs": refs})
    body = await r.text()
    return r.status, body


def _err(body):
    try:
        return str(__import__("json").loads(body).get("error", ""))
    except Exception:
        return body


def expect_reject(status, body, tag):
    assert status == 400, f"{tag}: 期望 400, 实际 {status}: {body[:200]}"
    assert "连接 ComfyUI 失败" not in _err(body), f"{tag}: 应在业务校验处被拒, 却到了 ComfyUI 连接: {body[:200]}"


def expect_pass_to_comfy(status, body, tag):
    """校验通过 -> 走到 ComfyUI 连接 (测试环境无 ComfyUI) -> 400 连接失败。"""
    assert status == 400 and "连接 ComfyUI 失败" in _err(body), \
        f"{tag}: 期望校验通过后停在 ComfyUI 连接, 实际 {status}: {body[:200]}"


async def main():
    client = TestClient(TestServer(S.make_app()))
    await client.start_server()
    try:
        await client.post("/api/workspaces", json={"name": WS})

        # ---- 1) 构图接线 (纯函数, 不发请求) ----
        params = {"steps": 4, "seed": 1, "width": 864, "height": 480, "length": 124}
        g = S.build_ref2va_graph("p", dict(params), ["a.png"], 864, 480, 124,
                                 ["x.mp3"], ["v1.mp4", "v2.mp4"], [True, False])
        assert g["40"]["class_type"] == "LoadVideo" and g["40"]["inputs"]["file"] == "v1.mp4"
        assert g["41"]["inputs"]["file"] == "v2.mp4"
        assert g["44"]["class_type"] == "GetVideoComponents" and g["44"]["inputs"]["video"] == ["40", 0]
        n6 = g["6"]["inputs"]
        assert n6["ref_videos.ref_video_0"] == ["44", 0], n6.get("ref_videos.ref_video_0")
        assert n6["ref_videos.ref_video_1"] == ["45", 0]
        assert n6["ref_video_audios.ref_video_audio_0"] == ["44", 1]  # v1 有音轨 -> 接线
        assert "ref_video_audios.ref_video_audio_1" not in n6         # v2 无音轨 -> 不接
        assert n6["ref_audios.ref_audio_0"] == ["30", 0]
        g2 = S.build_ref2va_graph("p", dict(params), ["a.png"], 864, 480, 124, None, ["v1.mp4"], [False])
        assert "ref_video_audios.ref_video_audio_0" not in g2["6"]["inputs"]  # 原声关 -> 不接
        g3 = S.build_ref2va_graph("p", dict(params), ["a.png"], 864, 480, 124)  # 无视频 -> 无视频节点
        assert "40" not in g3 and not any(k.startswith("ref_videos") for k in g3["6"]["inputs"])
        ok("构图: LoadVideo->GVC->ref_videos / 音轨按 flags 接线")

        # ---- 2) 上传校验 ----
        s_img, img = await upload(client, "image", make_png(), "a.png")
        assert s_img == 200 and img.get("filename"), (s_img, img)
        r1, b1 = await upload(client, "video", make_mp4(1.0, False), "short.mp4")
        assert r1 == 400 and "2–15s" in b1.get("error", ""), (r1, b1)
        r2, b2 = await upload(client, "video", make_mp4(20.0, False), "long.mp4")
        assert r2 == 400 and "2–15s" in b2.get("error", ""), (r2, b2)
        r3, b3 = await upload(client, "video", make_mp4(2.5, True), "v_audio.mp4")
        assert r3 == 200 and b3.get("has_audio") is True, (r3, b3)
        r4, b4 = await upload(client, "video", make_mp4(3.0, False), "v_silent.mp4")
        assert r4 == 200 and b4.get("has_audio") is False, (r4, b4)
        ok("上传: 1s/20s 拒, 2.5s 带音轨/3s 无音轨 过 (has_audio 正确)")

        va, vs_ = b3["filename"], b4["filename"]
        wav_name = None
        rw, bw = await upload(client, "audio", make_wav(2.5), "a.wav")
        assert rw == 200, (rw, bw)
        wav_name = bw["filename"]

        # ---- 3) 生成校验 ----
        # 3a) <Video 2> 越界 (只传 2 段却引用 3)
        st, body = await gen(client, "ref <Video 3> here", {"images": [img["filename"]], "videos": [va, vs_]})
        expect_reject(st, body, "<Video 3> 越界")
        ok("生成: <Video N> 越界拒绝")

        # 3b) 总时长超限: 2.5+3.0=5.5 合法, 再传一段 12s 凑 17.5 -> 总时长拒
        r5, b5 = await upload(client, "video", make_mp4(12.0, False), "v12s.mp4")
        assert r5 == 200, (r5, b5)
        v12 = b5["filename"]
        st, body = await gen(client, "no refs to videos", {"images": [img["filename"]],
                                                           "videos": [va, vs_, v12]})
        expect_reject(st, body, "总时长 17.5s")
        ok("生成: 视频总时长 >15s 拒绝")

        # 3c) 混合总数 >12: 9 图 + 2 视频 + 2 音频 = 13
        imgs = [img["filename"]]
        for k in range(8):
            rk, bk = await upload(client, "image", make_png(), f"i{k}.png")
            assert rk == 200, (rk, bk)
            imgs.append(bk["filename"])
        ra2, ba2 = await upload(client, "audio", make_wav(2.2), "a2.wav")
        assert ra2 == 200, (ra2, ba2)
        st, body = await gen(client, "many refs", {"images": imgs, "videos": [va, vs_],
                                                   "audios": [wav_name, ba2["filename"]]})
        expect_reject(st, body, "混合 13 个")
        ok("生成: 混合素材总数 >12 拒绝")

        # 3d) 音轨占位编号: v1(有音轨) + v2(无) + 1 独立音频 -> <Audio 2> 可用 (占位 1 + 独立 1)
        st, body = await gen(client, "talk <Audio 2>", {"images": [img["filename"]],
                                                        "videos": [va, vs_], "audios": [wav_name]})
        expect_pass_to_comfy(st, body, "<Audio 2> 音轨占位放行")
        ok("生成: 音轨占位编号 (<Audio 2> = v1 原声 + 独立音频) 放行")

        # 3e) 纯视频无图 (video editing) 放行
        st, body = await gen(client, "edit <Video 1>", {"images": [], "videos": [va]})
        expect_pass_to_comfy(st, body, "纯视频放行")
        ok("生成: 纯视频无图 (video editing 场景) 放行")

        # 3f) 原声关闭 -> <Audio 2> 应拒 (无占位, 只有 1 段独立音频)
        st, body = await gen(client, "talk <Audio 2>",
                             {"images": [img["filename"]], "videos": [va], "audios": [wav_name],
                              "use_video_audio": False})
        expect_reject(st, body, "原声关闭后 <Audio 2> 越界")
        ok("生成: 关闭原声后音轨占位编号回收 (<Audio 2> 拒绝)")

        print(f"\nALL {len(PASSED)} PASS")
    finally:
        await client.close()
        try:
            S.JOBS.clear()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
