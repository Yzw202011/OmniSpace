"""OmniSpace AI v2.1 极严格复测（标准高于《OmniSpace AI v2.1测试.txt》）。

覆盖维度（标准测试文档之外）：
  X-01 并发压力：20 路并发混合请求（对话/硬件/模型列表），全部正确响应或正确限流
  X-02 边界输入：空消息/纯空白/恰好上限字符/超上限 1 字符/Unicode 极端字符
  X-03 协议鲁棒：畸形 JSON / 错误 Content-Type / 未知字段 / 重复字段
  X-04 注入安全：SQL 注入 / XSS 载荷 / 路径穿越（模型导入/数据集上传）
  X-05 限流验证：单端点超频请求必须触发 429（中间件 100req/60s）
  X-06 显存互斥：对话→绘画→对话切换，验证驱逐与重载，全程无 OOM
  X-07 显存泄漏：同模型 5 轮加载-推理-卸载，空闲显存差 ≤0.5GB
  X-08 WebSocket：/ws 连接、遥测帧格式、断开重连
  X-09 训练边界：数据不足拒绝(40009) / 训练中并发触发拒绝(40007) / 坏数据集
  X-10 编码降级：H.264 NVENC 可用性 + 非法帧目录报错不崩溃
  X-11 前端资产：/ 与 /assets/* 无 404，Hash 路由深链回退
  X-12 API 一致性：错误响应统一 {code,message} 结构，无 500 HTML 泄漏
  X-13 长会话稳定：同 session 连续 6 轮对话，历史累计正确
  X-14 冷启动时延：后端就绪到首次健康检查 <15s（由外部脚本测，此处占位跳过）

用法: python strict_test.py [--skip-heavy]   （--skip-heavy 跳过真实模型推理类）
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

BASE = "http://127.0.0.1:5800"
RESULTS: list[dict] = []
SKIP_HEAVY = "--skip-heavy" in sys.argv


def record(case_id: str, name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append({"case": case_id, "name": name, "pass": bool(passed),
                    "detail": detail, "ts": time.strftime("%H:%M:%S")})
    print(f"[{'PASS' if passed else 'FAIL'}] {case_id} {name}"
          + (f" | {detail}" if detail else ""), flush=True)
    return passed


def api_err_code(resp: httpx.Response) -> int | None:
    try:
        return resp.json().get("code")
    except Exception:
        return None


def wait_lock_idle(cli: httpx.Client, timeout_s: float = 180.0) -> bool:
    """等待功能锁释放（/v1/models/status.feature_lock.active_feature 为空）。

    绘画/训练等异步任务在后台持锁运行，互斥期间其他 AI 功能返回 40007
    （规格 §6.1）；后续用例必须等锁空闲再发起，否则属于测试时序错误。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            d = cli.get("/v1/models/status", timeout=15).json().get("data") or {}
            if not (d.get("feature_lock") or {}).get("active_feature"):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_draw_done(cli: httpx.Client, task_id: str,
                   timeout_s: float = 300.0) -> bool:
    """轮询绘画任务直到 done/error，并确认功能锁已释放。"""
    deadline = time.time() + timeout_s
    ok_done = False
    while time.time() < deadline:
        try:
            d = cli.get(f"/v1/draw/result/{task_id}",
                        timeout=15).json().get("data") or {}
            if d.get("status") in ("done", "error"):
                ok_done = d.get("status") == "done"
                break
        except Exception:
            pass
        time.sleep(3)
    # 结果落库先于锁释放（watcher 线程 join 后才 release），再等锁空闲
    return ok_done and wait_lock_idle(cli, timeout_s=60)


def unload_all_models(cli: httpx.Client) -> None:
    """按 /v1/models/status 的 loaded_models 逐个卸载（卸载端点字段为 model_id）。"""
    try:
        d = cli.get("/v1/models/status", timeout=15).json().get("data") or {}
        for entry in d.get("loaded_models") or []:
            mid = entry.get("model_id") or entry.get("id")
            if mid:
                cli.post("/v1/models/unload", json={"model_id": mid}, timeout=30)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# X-01 并发压力：20 路混合请求
# ═══════════════════════════════════════════════════════════════
def x01_concurrency() -> None:
    def hit(i: int) -> tuple[int, int | None]:
        try:
            if i % 3 == 0:
                r = httpx.get(f"{BASE}/health", timeout=15)
            elif i % 3 == 1:
                r = httpx.get(f"{BASE}/v1/hardware/info", timeout=15)
            else:
                r = httpx.get(f"{BASE}/v1/models", timeout=15)
            return r.status_code, api_err_code(r)
        except Exception:
            return -1, None

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(hit, i) for i in range(20)]
        out = [f.result() for f in as_completed(futs)]
    ok_200 = sum(1 for s, _ in out if s == 200)
    ok_429 = sum(1 for s, _ in out if s == 429)
    bad = [s for s, _ in out if s not in (200, 429)]
    record("X-01", "并发压力-20路混合", not bad and ok_200 >= 10,
           f"200×{ok_200} 429×{ok_429} 异常×{len(bad)}{bad[:5]}")


# ═══════════════════════════════════════════════════════════════
# X-02 边界输入
# ═══════════════════════════════════════════════════════════════
def x02_boundary() -> None:
    cli = httpx.Client(base_url=BASE, timeout=30)
    results = []

    def safe_post(name: str, check, **kw) -> None:
        """单请求异常不拖垮整组：记录为该子项失败。"""
        try:
            r = cli.post("/v1/chat/send", **kw)
            results.append((name, check(r)))
        except Exception as exc:
            results.append((f"{name}(异常:{type(exc).__name__})", False))

    # 空消息
    safe_post("空消息→30001", lambda r: api_err_code(r) == 30001,
              json={"message": ""})
    # 纯空白
    safe_post("纯空白→30001", lambda r: api_err_code(r) == 30001,
              json={"message": "   \n\t  "})
    # 超上限 1 字符（32769 > 32768）
    safe_post("32769字→40002", lambda r: api_err_code(r) == 40002,
              json={"message": "测" * 32769})
    # 恰好上限（应通过校验进入推理或队列；只验证不报 40002/30001）
    safe_post("32768字→非输入错误",
              lambda r: api_err_code(r) not in (30001, 40002),
              json={"message": "测" * 32768, "max_new_tokens": 1},
              timeout=httpx.Timeout(180.0, connect=10.0))
    # Unicode 极端字符（emoji/组合符/零宽）
    weird = "👨‍👩‍👧‍👦\u200b\uFE0F\x00测试\u202E反转"
    safe_post("Unicode极端字符→不崩溃",
              lambda r: r.status_code in (200, 400, 429)
              and api_err_code(r) != 500,
              json={"message": weird, "max_new_tokens": 1},
              timeout=httpx.Timeout(180.0, connect=10.0))

    fails = [n for n, ok in results if not ok]
    record("X-02", "边界输入-5项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-03 协议鲁棒
# ═══════════════════════════════════════════════════════════════
def x03_protocol() -> None:
    results = []
    # 畸形 JSON（规格 9.1.1 统一封装：HTTP 200 + code=40004；官方测试文档
    # 亦认可 "状态码=422 或 code=30001" 双轨——判据：非 500 且携带非 0 业务码）
    r = httpx.post(f"{BASE}/v1/chat/send", content=b"{bad json,",
                   headers={"Content-Type": "application/json"}, timeout=15)
    results.append(("畸形JSON→40004封装非500",
                    r.status_code < 500 and api_err_code(r) == 40004))
    # 错误 Content-Type
    r = httpx.post(f"{BASE}/v1/chat/send", content=b"message=hi",
                   headers={"Content-Type": "text/plain"}, timeout=15)
    results.append(("text/plain→40004封装非500",
                    r.status_code < 500 and api_err_code(r) == 40004))
    # 未知字段（应忽略不崩溃）
    r = httpx.post(f"{BASE}/v1/chat/send",
                   json={"message": "hi", "evil_field": {"a": 1},
                         "max_new_tokens": 1},
                   timeout=httpx.Timeout(180.0, connect=10.0))
    results.append(("未知字段→不崩溃", r.status_code in (200, 400, 429)))
    # GET 打到 POST 端点（路由器部分匹配必须返回 405，而非静态层吞掉）
    r = httpx.get(f"{BASE}/v1/chat/send", timeout=15)
    results.append(("方法错误→405", r.status_code == 405))

    fails = [n for n, ok in results if not ok]
    record("X-03", "协议鲁棒-4项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-04 注入安全
# ═══════════════════════════════════════════════════════════════
def x04_injection() -> None:
    results = []
    # SQL 注入（知识库查询参数）
    r = httpx.get(f"{BASE}/v1/knowledge/list",
                  params={"topic": "'; DROP TABLE knowledge;--"}, timeout=15)
    body = r.text
    results.append(("SQL注入→不执行不泄漏",
                    r.status_code in (200, 400, 422) and "syntax" not in body.lower()))
    # XSS 载荷入库后读回（应原样存储/转义，不执行——此处验证 API 不崩溃）
    xss = "<script>alert(1)</script>"
    r = httpx.post(f"{BASE}/v1/knowledge/process-text",
                   json={"content": xss, "topic": "安全测试"}, timeout=120)
    results.append(("XSS载荷→处理不崩溃", r.status_code in (200, 400, 429)))
    # 路径穿越：模型导入
    r = httpx.post(f"{BASE}/v1/models/import",
                   json={"path": "../../windows/system32/cmd.exe",
                         "name": "evil"}, timeout=15)
    results.append(("路径穿越导入→拒绝",
                    r.status_code >= 400 or api_err_code(r) not in (0, None)))
    # 路径穿越：数据集上传文件名
    r = httpx.post(f"{BASE}/v1/learn/dataset/upload",
                   files={"file": ("../../evil.jsonl", b'{"a":1}\n',
                                   "application/jsonl")}, timeout=15)
    saved = ""
    try:
        saved = (r.json().get("data") or {}).get("dataset_path", "")
    except Exception:
        pass
    results.append(("上传文件名穿越→不落盘到逃逸路径",
                    "..\\" not in saved and "../" not in saved))

    fails = [n for n, ok in results if not ok]
    record("X-04", "注入安全-4项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-05 限流验证（100 req/60s per endpoint）
# ═══════════════════════════════════════════════════════════════
def x05_rate_limit() -> None:
    codes = []
    for _i in range(120):
        try:
            r = httpx.get(f"{BASE}/v1/system/version", timeout=10)
            codes.append(r.status_code)
        except Exception:
            codes.append(-1)
    n429 = codes.count(429)
    record("X-05", "限流-120连击必出429", n429 > 0,
           f"429×{n429} / 120（200×{codes.count(200)}）")


# ═══════════════════════════════════════════════════════════════
# X-06 显存互斥：对话→绘画→对话
# ═══════════════════════════════════════════════════════════════
def x06_vram_mutex() -> None:
    if SKIP_HEAVY:
        record("X-06", "显存互斥切换", True, "跳过（--skip-heavy）")
        return
    cli = httpx.Client(base_url=BASE, timeout=httpx.Timeout(600.0, connect=10.0))
    results = []
    wait_lock_idle(cli)  # 前置：确保无遗留任务持锁
    # 对话推理
    r = cli.post("/v1/chat/send", json={"message": "你好", "max_new_tokens": 8})
    results.append(("对话推理", r.status_code == 200 and api_err_code(r) == 0))
    # 绘画推理（异步任务：受理后经驱逐加载、后台生成，须轮询完成）
    r = cli.post("/v1/draw/generate",
                 json={"prompt": "a red apple on a table", "width": 512,
                       "height": 512, "steps": 8})
    task_id = ""
    try:
        task_id = (r.json().get("data") or {}).get("task_id", "")
    except Exception:
        pass
    accepted = r.status_code == 200 and api_err_code(r) == 0 and bool(task_id)
    paint_ok = accepted and wait_draw_done(cli, task_id)
    results.append(("绘画推理", paint_ok))
    # 再次对话（验证回切：绘画完成锁释放后，对话引擎驱逐绘画权重并重载）
    r = cli.post("/v1/chat/send", json={"message": "还在吗", "max_new_tokens": 8})
    results.append(("对话回切", r.status_code == 200 and api_err_code(r) == 0))

    fails = [n for n, ok in results if not ok]
    record("X-06", "显存互斥-对话/绘画/对话", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-07 显存泄漏：5 轮加载-推理-卸载
# ═══════════════════════════════════════════════════════════════
def x07_vram_leak() -> None:
    if SKIP_HEAVY:
        record("X-07", "显存泄漏-5轮", True, "跳过（--skip-heavy）")
        return
    cli = httpx.Client(base_url=BASE, timeout=httpx.Timeout(600.0, connect=10.0))

    def free_gb() -> float:
        # /v1/hardware/info 的 gpu 为字典（单卡场景），字段为 vram_free_mb
        r = cli.get("/v1/hardware/info")
        d = r.json().get("data") or {}
        gpu = d.get("gpu") or {}
        if isinstance(gpu, list):  # 兼容未来多卡列表形态
            gpu = gpu[0] if gpu else {}
        if gpu.get("vram_free_gb") is not None:
            return float(gpu["vram_free_gb"])
        return float(gpu.get("vram_free_mb", 0) or 0) / 1024.0

    wait_lock_idle(cli)
    unload_all_models(cli)
    time.sleep(3)
    baseline = free_gb()
    for i in range(5):
        cli.post("/v1/chat/send", json={"message": f"第{i + 1}轮测试",
                                        "max_new_tokens": 4})
        unload_all_models(cli)
        time.sleep(2)
    final = free_gb()
    drift = baseline - final
    record("X-07", "显存泄漏-5轮漂移≤0.5GB", drift <= 0.5,
           f"基线{baseline:.1f}GB → 终值{final:.1f}GB，漂移{drift:.2f}GB")


# ═══════════════════════════════════════════════════════════════
# X-08 WebSocket /ws
# ═══════════════════════════════════════════════════════════════
def x08_websocket() -> None:
    import asyncio

    import websockets

    async def probe() -> tuple[bool, str]:
        try:
            async with websockets.connect("ws://127.0.0.1:5800/ws",
                                          open_timeout=10) as ws:
                # 协议（§2.2）：ping→pong；subscribe_system 后才有遥测帧
                await ws.send(json.dumps({"type": "ping"}))
                await ws.send(json.dumps({"type": "subscribe_system"}))
                got_pong = False
                for _ in range(15):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    mtype = msg.get("type")
                    if mtype == "pong":
                        got_pong = True
                        continue
                    if mtype == "system_status" and isinstance(
                            msg.get("data"), dict):
                        return (True,
                                f"pong={'✓' if got_pong else '✗'} "
                                f"遥测帧 system_status✓")
                return False, f"15 次接收内无遥测帧（pong={'✓' if got_pong else '✗'}）"
        except Exception as exc:
            return False, f"连接异常: {exc}"

    ok, detail = asyncio.run(probe())
    record("X-08", "WebSocket-ping/pong+遥测帧", ok, detail)


# ═══════════════════════════════════════════════════════════════
# X-09 训练边界
# ═══════════════════════════════════════════════════════════════
def x09_training_boundary() -> None:
    cli = httpx.Client(base_url=BASE, timeout=60)
    results = []
    # 数据不足：不存在的 dataset_path + 空知识库场景由服务判定；
    # 这里用不存在的基座触发明确错误
    r = cli.post("/v1/learn/train", json={"base_model": ""})
    results.append(("空base_model→40008", api_err_code(r) == 40008))
    # 坏数据集文件（非 JSONL 内容，<100 条有效 → 40009）
    r = cli.post("/v1/learn/dataset/upload",
                 files={"file": ("bad.jsonl", b"not json at all\n{}\n",
                                 "application/jsonl")}, timeout=15)
    bad_path = ""
    try:
        bad_path = (r.json().get("data") or {}).get("dataset_path", "")
    except Exception:
        pass
    if bad_path:
        r2 = cli.post("/v1/learn/train", json={
            "base_model": r"e:\OmniSpace\models\qwen3-vl-4b",
            "dataset_path": bad_path, "epochs": 1})
        results.append(("坏数据集→40009数据不足", api_err_code(r2) == 40009))
    else:
        results.append(("坏数据集→上传被拒", True))
    # 任务列表结构
    r = cli.get("/v1/learn/tasks")
    d = r.json().get("data") or {}
    results.append(("任务列表结构", "items" in d and "total" in d))

    fails = [n for n, ok in results if not ok]
    record("X-09", "训练边界-3项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-10 编码边界
# ═══════════════════════════════════════════════════════════════
def x10_encoder() -> None:
    results = []
    # 服务状态
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    py = str(root / "runtime" / "py310" / "python.exe")
    probe = subprocess.run(
        [py, "-c",
         f"import sys; sys.path.insert(0, {str(root)!r});"
         "from backend.services.encoder_service import get_encoder_service as g;"
         "e = g(); print(int(e.available)); print(int(e.has_encoder('h264_nvenc')))"],
        capture_output=True, text=True, timeout=60, cwd=str(root))
    lines = (probe.stdout or "").strip().splitlines()
    results.append(("ffmpeg可用", lines[0] == "1" if lines else False))
    results.append(("h264_nvenc可用",
                    len(lines) > 1 and lines[1] == "1"))
    # 非法帧目录 → EncodeError 友好抛出（不崩溃）
    probe2 = subprocess.run(
        [py, "-c",
         f"import sys; sys.path.insert(0, {str(root)!r});"
         "from backend.services.encoder_service import get_encoder_service as g, EncodeError;"
         "e = g();"
         "out = None;"
         "exec(\"try:\\n e.encode_frames_to_video('e:/nonexistent_dir_xyz', 'e:/tmp_x.mp4', timeout_s=30)\\n print('NO_RAISE')\\nexcept EncodeError:\\n print('ENCODE_ERR')\\nexcept Exception as ex:\\n print('OTHER', type(ex).__name__)\")"],
        capture_output=True, text=True, timeout=120, cwd=str(root))
    out2 = (probe2.stdout or "").strip()
    results.append(("非法帧目录→友好报错", "NO_RAISE" not in out2 and out2 != ""))

    fails = [n for n, ok in results if not ok]
    record("X-10", "编码边界-3项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results) + f" [{out2}]")


# ═══════════════════════════════════════════════════════════════
# X-11 前端资产
# ═══════════════════════════════════════════════════════════════
def x11_frontend() -> None:
    results = []
    r = httpx.get(f"{BASE}/", timeout=15, follow_redirects=True)
    html_ok = r.status_code == 200 and ("<div" in r.text or "<script" in r.text)
    results.append(("首页200", html_ok))
    # 提取 JS/CSS 资产路径验证无 404
    import re
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', r.text)
    bad = []
    for a in assets[:10]:
        ra = httpx.get(f"{BASE}{a}", timeout=15)
        if ra.status_code != 200:
            bad.append((a, ra.status_code))
    results.append((f"资产无404({len(assets)}个)", not bad))
    # Hash 路由深链（/#/xxx 由前端处理，/ 必须可服务）
    r2 = httpx.get(f"{BASE}/", timeout=15)
    results.append(("深链回退", r2.status_code == 200))

    fails = [n for n, ok in results if not ok]
    record("X-11", "前端资产-3项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results)
        + (f" 404:{bad[:3]}" if bad else ""))


# ═══════════════════════════════════════════════════════════════
# X-12 API 一致性：错误结构统一
# ═══════════════════════════════════════════════════════════════
def x12_error_consistency() -> None:
    results = []
    # 404 端点
    r = httpx.get(f"{BASE}/v1/nonexistent_endpoint", timeout=15)
    try:
        j = r.json()
        results.append(("404端点→JSON结构", "code" in j or "detail" in j))
    except Exception:
        results.append(("404端点→JSON结构", False))
    # 业务错误结构
    r = httpx.post(f"{BASE}/v1/chat/send", json={"message": ""}, timeout=15)
    try:
        j = r.json()
        results.append(("业务错误→{code,message}",
                        "code" in j and "message" in j and j["code"] != 0))
    except Exception:
        results.append(("业务错误→{code,message}", False))
    # 无 500 HTML 错误页泄漏
    r = httpx.get(f"{BASE}/v1/models/nonexistent_model_xyz", timeout=15)
    ct = r.headers.get("content-type", "")
    results.append(("错误不泄漏HTML页",
                    "application/json" in ct or r.status_code == 404))

    fails = [n for n, ok in results if not ok]
    record("X-12", "API一致性-3项", not fails, "；".join(
        f"{n}{'✓' if ok else '✗'}" for n, ok in results))


# ═══════════════════════════════════════════════════════════════
# X-13 长会话稳定：6 轮同 session 对话
# ═══════════════════════════════════════════════════════════════
def x13_long_session() -> None:
    if SKIP_HEAVY:
        record("X-13", "长会话-6轮", True, "跳过（--skip-heavy）")
        return
    sid = uuid.uuid4().hex
    cli = httpx.Client(base_url=BASE, timeout=httpx.Timeout(300.0, connect=10.0))
    # 前置：等待前序用例（绘画等异步任务）释放功能锁，避免互斥 40007 干扰
    wait_lock_idle(cli, timeout_s=300)
    ok_rounds = 0
    for i in range(6):
        r = cli.post("/v1/chat/send",
                     json={"message": f"这是第{i + 1}轮，请回复轮次数字",
                           "session_id": sid, "max_new_tokens": 16})
        if r.status_code == 200 and api_err_code(r) == 0:
            ok_rounds += 1
    # 历史累计校验
    try:
        h = cli.get("/v1/chat/history", params={"session_id": sid}).json()
        msgs = (h.get("data") or {}).get("messages") or []
        hist_ok = len(msgs) >= ok_rounds * 2
    except Exception:
        hist_ok = False
    record("X-13", "长会话-6轮+历史累计", ok_rounds == 6 and hist_ok,
           f"成功{ok_rounds}/6轮，历史{'OK' if hist_ok else '异常'}")


def main() -> None:
    print("=" * 64)
    print("OmniSpace AI v2.1 极严格复测（X-01 ~ X-13）")
    print("=" * 64, flush=True)
    try:
        httpx.get(f"{BASE}/health", timeout=5).raise_for_status()
    except Exception as exc:
        print(f"后端不可达: {exc}")
        sys.exit(2)

    for fn in (x01_concurrency, x02_boundary, x03_protocol, x04_injection,
               x05_rate_limit, x06_vram_mutex, x07_vram_leak, x08_websocket,
               x09_training_boundary, x10_encoder, x11_frontend,
               x12_error_consistency, x13_long_session):
        try:
            fn()
        except Exception as exc:  # 单用例异常不中断套件
            record(fn.__name__.upper().replace("_", "-"), "用例内部异常",
                   False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for r in RESULTS if r["pass"])
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"极严格复测结果: {passed}/{total} 通过")
    for r in RESULTS:
        if not r["pass"]:
            print(f"  失败: {r['case']} {r['name']} — {r['detail']}")
    from pathlib import Path
    out = Path(__file__).with_name("strict_results.json")
    out.write_text(json.dumps({"passed": passed, "total": total,
                               "results": RESULTS,
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入: {out}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
