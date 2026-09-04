"""进程内验证修复后的 SSE 逻辑 (不依赖 ComfyUI):
1) 未知 job -> 404
2) 终态闩锁: 断线后重连 -> 立即重放 done (且不重复)
3) 活流: 重连先补发最近 progress, 之后事件继续到达, done 收尾
4) push_event 终态闩锁语义 (last/final 字段)
"""
import asyncio, json, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "webui"))
import server as S  # noqa: E402
from aiohttp.test_utils import TestServer, TestClient  # noqa: E402


def _bare(evs):
    """去掉测试用的 seq 字段, 只比对业务字段。"""
    return [{k: v for k, v in e.items() if k != "seq"} for e in evs]


async def read_events(resp, n):
    """读 n 条 SSE data 行。"""
    evs = []
    while len(evs) < n:
        line = await asyncio.wait_for(resp.content.readline(), timeout=15)
        line = line.decode().strip()
        if line.startswith("data: "):
            evs.append(json.loads(line[6:]))
    return evs


async def main():
    ok = True
    client = TestClient(TestServer(S.make_app()))
    await client.start_server()
    try:
        # 1) 未知 job -> 404
        r = await client.get("/api/jobs/nope/events")
        assert r.status == 404, f"期望 404, 实际 {r.status}"
        await r.read()
        print("[1] 未知 job -> 404  OK")

        # 4) push_event 闩锁语义
        j = {"queue": asyncio.Queue()}
        S.push_event(j, {"type": "progress", "value": 3, "max": 8})
        S.push_event(j, {"type": "done", "video": "x.mp4", "gen_id": "g1"})
        assert j["last"]["type"] == "done" and j["final"]["type"] == "done", j
        print("[4] push_event 闩锁 last/final  OK")

        # 2) 终态闩锁重放: 模拟"断线后才重连"
        jid = "test-fin"
        S.JOBS[jid] = {"queue": asyncio.Queue()}
        S.push_event(S.JOBS[jid], {"type": "progress", "value": 3, "max": 8})
        S.push_event(S.JOBS[jid], {"type": "done", "video": "x.mp4", "gen_id": "g1"})
        r = await client.get(f"/api/jobs/{jid}/events")
        evs = []
        line = await asyncio.wait_for(r.content.readline(), timeout=5)
        evs.append(json.loads(line.decode()[6:]))
        assert len(evs) == 1 and evs[0]["type"] == "done", evs
        r.close()
        print("[2] 断线重连 -> 立即重放 done (无重复)  OK:", evs)

        # 3) 活流: 连接前有 last, 连接后继续推事件
        jid2 = "test-live"
        S.JOBS[jid2] = {"queue": asyncio.Queue()}
        S.push_event(S.JOBS[jid2], {"type": "progress", "value": 1, "max": 8})
        r = await client.get(f"/api/jobs/{jid2}/events")
        first = await read_events(r, 1)
        assert _bare(first) == [{"type": "progress", "value": 1, "max": 8}], first  # last 重放
        S.push_event(S.JOBS[jid2], {"type": "progress", "value": 2, "max": 8})
        S.push_event(S.JOBS[jid2], {"type": "done", "video": "y.mp4", "gen_id": "g2"})
        rest = await read_events(r, 2)
        assert _bare(rest) == [{"type": "progress", "value": 2, "max": 8}, {"type": "done", "video": "y.mp4", "gen_id": "g2"}], rest
        r.close()
        print("[3] 重连补发 last + 活流后续事件 + done 收尾  OK:", first + rest)
    finally:
        await client.close()

    print("ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
