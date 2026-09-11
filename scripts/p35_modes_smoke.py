"""P3.5 生成模式冒烟 (进程内, 不依赖运行中的 ComfyUI):
1) build_graph 接线 (纯函数): 仅首 I2VA / 无图 T2VA / 双帧 FL2VA / 仅尾 L2VA
2) 生成校验 (HTTP): t2v 无图放行 / l2v 单图作尾帧放行 / fl2v 双图放行 / 默认 i2v 不回归 ——
   "放行"以 '连接 ComfyUI 失败' 判定 (校验全过, 只差 ComfyUI 本体)
3) 越界拒绝: t2v 带图 / fl2v 缺尾帧 / frame_mode 非法 / image_last 脱离 both
用法: qrhead python scripts/p35_modes_smoke.py
"""
import asyncio, io, os, pathlib, sys, tempfile

root = pathlib.Path(__file__).resolve().parent.parent
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="p35_smoke_"))
(_tmp / "input").mkdir()
(_tmp / "output").mkdir()
os.environ["COMFYUI_INPUT"] = str(_tmp / "input")
os.environ["COMFYUI_OUTPUT"] = str(_tmp / "output")
sys.path.insert(0, str(root / "webui"))
import server as S  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

WS = "p35_smoke"
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


async def upload_image(client, data, fname):
    import aiohttp
    fd = aiohttp.FormData()
    fd.add_field("type", "image")
    fd.add_field("file", data, filename=fname)
    r = await client.post(f"/api/workspaces/{WS}/upload-media", data=fd)
    body = await r.json()
    assert r.status == 200, f"上传失败: {body}"
    return body["filename"]


async def gen(client, params, extra):
    r = await client.post(f"/api/workspaces/{WS}/generate",
                          json={"prompt": "a cat walks", "params": params, **extra})
    return r.status, await r.text()


def _err(body):
    import json
    try:
        return str(json.loads(body).get("error", ""))
    except Exception:
        return body


def expect_reject(status, body, tag):
    assert status == 400, f"{tag}: 期望 400, 实际 {status}: {body[:200]}"
    assert "连接 ComfyUI 失败" not in _err(body), f"{tag}: 应在业务校验处被拒, 却到了 ComfyUI 连接"


def expect_pass_to_comfy(status, body, tag):
    """校验通过 -> 走到 ComfyUI 连接 (测试环境无 ComfyUI) -> 400 连接失败。"""
    assert status == 400 and "连接 ComfyUI 失败" in _err(body), \
        f"{tag}: 期望校验通过后停在 ComfyUI 连接, 实际 {status}: {body[:200]}"


def graph_case(name, first, last, want_tt, want_first, want_last):
    g = S.build_graph("p", {"steps": 4, "seed": 1}, first, 512, 512, 124, last_image_name=last)
    n7 = g["7"]["inputs"]
    assert n7["task_type"] == want_tt, f"{name}: task_type={n7['task_type']} != {want_tt}"
    assert ("first_frame" in n7) == want_first, f"{name}: first_frame 接线不符"
    assert ("last_frame" in n7) == want_last, f"{name}: last_frame 接线不符"
    assert ("6" in g) == want_first, f"{name}: LoadImage(首) 节点不符"
    assert ("6b" in g) == want_last, f"{name}: LoadImage(尾) 节点不符"
    if want_first:
        assert n7["first_frame"] == ["6", 0]
    if want_last:
        assert n7["last_frame"] == ["6b", 0]
    ok(name)


async def main():
    client = TestClient(TestServer(S.make_app()))
    await client.start_server()
    try:
        await client.post("/api/workspaces", json={"name": WS})

        # ---- 1) 构图接线 (纯函数) ----
        graph_case("构图: i2v 仅首帧", "a.png", None, "I2VA", True, False)
        graph_case("构图: t2v 无图", None, None, "T2VA", False, False)
        graph_case("构图: fl2v 首尾双帧", "a.png", "b.png", "FL2VA", True, True)
        graph_case("构图: l2v 仅尾帧", None, "b.png", "L2VA", False, True)

        # ---- 2) HTTP 生成 ----
        img1 = await upload_image(client, make_png(), "img1.png")
        img2 = await upload_image(client, make_png(), "img2.png")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5}, {"image": img1})
        expect_pass_to_comfy(s, b, "i2v 默认 first 不回归")
        ok("生成: i2v 默认 first 不回归")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5}, {"image": img1, "frame_mode": "last"})
        expect_pass_to_comfy(s, b, "l2v 单图作尾帧放行")
        ok("生成: l2v 单图作尾帧放行")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5},
                         {"image": img1, "image_last": img2, "frame_mode": "both"})
        expect_pass_to_comfy(s, b, "fl2v 首尾双帧放行")
        ok("生成: fl2v 首尾双帧放行")

        s, b = await gen(client, {"task_type": "t2v", "res_mode": "custom",
                                  "custom_w": 1024, "custom_h": 576, "duration": 5}, {})
        expect_pass_to_comfy(s, b, "t2v 无图放行")
        ok("生成: t2v 无图放行")

        # ---- 3) 越界拒绝 ----
        s, b = await gen(client, {"task_type": "t2v", "res_mode": "custom"}, {"image": img1})
        expect_reject(s, b, "t2v 带参考图 拒")
        ok("生成: t2v 带参考图 拒")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5}, {"image": img1, "frame_mode": "both"})
        expect_reject(s, b, "fl2v 缺尾帧图 拒")
        ok("生成: fl2v 缺尾帧图 拒")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5},
                         {"image": img1, "frame_mode": "side"})
        expect_reject(s, b, "frame_mode 非法 拒")
        ok("生成: frame_mode 非法 拒")

        s, b = await gen(client, {"task_type": "i2v", "duration": 5},
                         {"image": img1, "image_last": img2})
        expect_reject(s, b, "image_last 脱离 both 拒")
        ok("生成: image_last 脱离 both 拒")

    finally:
        await client.close()
    print(f"\nALL {len(PASSED)} PASS")


if __name__ == "__main__":
    asyncio.run(main())
