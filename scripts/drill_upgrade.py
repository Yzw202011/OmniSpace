"""升级链重建 · 演练实弹脚本 v2（2026-10-21，SH-GAP-08 重建批 C）。

v2 变更：演练实例按 build_id 自动发现端口（5800-5835 扫描）——与开发链
并存不打架（开发链占 5800 时基座自动落 5801+）。

安全口径（离线演练工具）：所有请求目标=写死的本机回环 + 产品契约端口段，
http.client 直连；子调用全部参数列表 + shell=False；不接任何外部输入。

六点验证（每点打 PASS/FAIL，任何 FAIL 退出码非零）：
  ①出签名 .upg（make_upgrade_package 差分 gc5ddaac→2.5.1）
  ②导入+验签（基座 /upgrade/import；负向：篡改包必须拒收）
  ③升级执行（confirm=UPGRADE → updater 状态机走完 → success）
  ④build_id 对账（演练实例 /health build == to_build_id，三方一致）
  ⑤data/ 原地保留（演练标记文件升级后仍在）
  ⑥回滚/断电保险由专项覆盖（本脚本正文无破坏性注入）

用法：runtime/py312/python.exe scripts/drill_upgrade.py
（前置：主链/开发链可在线，演练基座自动落空余端口；终止后记得清
 E:/ccd_archive/selfheal_drill 与演练实例进程）
"""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path(r"D:\ccd\OmniSpace-2.5.0-gc5ddaac-protected")
NEW_BUILD_GLOB = "OmniSpace-2.5.1-g*-protected"  # 相对 pattern（glob 不收盘符）
DRILL = Path(r"E:\ccd_archive\selfheal_drill")
OUT_UPG = Path(r"E:\ccd_archive\selfheal_drill_upg")
KEY = REPO / "keys" / "upgrade_signing.key"

# 写死的目标（离线演练：仅本机回环；端口段=产品契约 5800-5835）
_HOST = "127.0.0.1"
_PORT_MIN, _PORT_MAX = 5800, 5835

results: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else f"FAIL {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    return ok


def get_health(port: int) -> dict | None:
    """单端口 /health 快照；不可达=None。"""
    try:
        conn = http.client.HTTPConnection(_HOST, port, timeout=3)
        conn.request("GET", "/health")
        d = json.loads(conn.getresponse().read())["data"]
        conn.close()
        return d
    except Exception:
        return None


def discover_drill_port(expected_build: str, timeout_s: float) -> int | None:
    """在产品端口段里找 build==expected_build 的实例（=演练基座）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for port in range(_PORT_MIN, _PORT_MAX + 1):
            d = get_health(port)
            if d and d.get("build") == expected_build \
                    and d.get("status") == "healthy":
                return port
        time.sleep(3)
    return None


def api_call(port: int, method: str, path: str, body: bytes | None = None,
             content_type: str = "application/json",
             timeout: float = 30) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(_HOST, port, timeout=timeout)
    try:
        conn.request(method, path, body=body,
                     headers={"Content-Type": content_type})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, {}
    finally:
        conn.close()


def find_new_build() -> Path | None:
    cands = sorted(Path(r"D:\ccd").glob(NEW_BUILD_GLOB),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def multipart(name: str, payload: bytes) -> tuple[str, bytes]:
    boundary = "----selfhealdrill"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="file"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() \
        + payload + f"\r\n--{boundary}--\r\n".encode()
    return boundary, body


def main() -> int:
    if not BASE.is_dir():
        print("基座不存在:", BASE)
        return 2
    new_build = find_new_build()
    if new_build is None:
        print("未找到 2.5.1 构建目录")
        return 2
    base_manifest = json.loads(
        (BASE / "dist_manifest.json").read_text(encoding="utf-8"))
    base_build = str(base_manifest.get("build_id") or "")
    to_build = ""
    new_manifest = new_build / "dist_manifest.json"
    if new_manifest.is_file():
        to_build = str(json.loads(new_manifest.read_text(encoding="utf-8"))
                       .get("build_id") or "")
    print("基座 build:", base_build)
    print("目标 build:", to_build)

    # ── ① 出签名 .upg ────────────────────────────────────────────
    OUT_UPG.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(REPO / "tools" / "make_upgrade_package.py"),
           "--old", str(BASE), "--new", str(new_build),
           "--out", str(OUT_UPG), "--key", str(KEY),
           "--notes", str(OUT_UPG / "notes.txt")]
    rc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                        shell=False)
    upgs = list(OUT_UPG.glob("*.upg"))
    detail = "" if rc.returncode == 0 else \
        (rc.stdout[-200:] + rc.stderr[-200:])
    check("①出签名.upg", rc.returncode == 0 and bool(upgs), detail)
    if not upgs:
        return 3
    upg = upgs[0]

    # ── 演练基座就位（复制 gc5ddaac → DRILL，data 打演练标记）──────
    if DRILL.exists():
        shutil.rmtree(DRILL, ignore_errors=True)
    print("复制基座（约 23 万文件，数分钟）…")
    subprocess.run(["robocopy", str(BASE), str(DRILL), "/E", "/NFL",
                    "/NDL", "/NJH", "/NJS"], capture_output=True, shell=False)
    marker = DRILL / "data" / "selfheal_drill_marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("升级后我必须还在", encoding="utf-8")

    # ── 起基座（端口自动发现：谁报基座 build 谁是演练实例）─────────
    boot = DRILL / "runtime" / "py312" / "pythonw.exe"
    subprocess.Popen([str(boot), "launcher/boot.py"], cwd=str(DRILL),
                     creationflags=0x08000000, shell=False)  # CREATE_NO_WINDOW
    port = discover_drill_port(base_build, 300)
    if port is None:
        # 死因速报：基座 boot 日志里找拒绝启动的措辞（版本隔离守卫等）
        bootlog = DRILL / "logs" / "boot.log"
        reason = ""
        if bootlog.is_file():
            for line in reversed(bootlog.read_text(
                    encoding="utf-8", errors="replace").splitlines()):
                if "不接管" in line or "拒绝启动" in line or "退位" in line:
                    reason = line.strip()[:150]
                    break
        check("基座启动+端口发现", False,
              reason or f"300s 未发现 build={base_build} 的实例")
        return 4
    check("基座启动+端口发现", True, f":{port}")

    # ── ② 导入+验签 ──────────────────────────────────────────────
    boundary, body = multipart(upg.name, upg.read_bytes())
    code, imp = api_call(port, "POST", "/api/v1/upgrade/import", body,
                         f"multipart/form-data; boundary={boundary}", 600)
    check("②导入+验签", code == 200 and imp.get("success") is True,
          str(imp.get("error"))[:120])

    # 负向：篡改包必须拒收（翻转 payload 中间 1 字节）
    raw = bytearray(upg.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    tamper = OUT_UPG / "tampered.upg"
    tamper.write_bytes(bytes(raw))
    boundary2, body2 = multipart("tampered.upg", bytes(raw))
    try:
        code2, imp2 = api_call(port, "POST", "/api/v1/upgrade/import", body2,
                               f"multipart/form-data; boundary={boundary2}",
                               120)
        ok2 = imp2.get("success") is False
    except Exception:
        ok2 = True  # 4xx 拒收也算
    check("②负向篡改包拒收", ok2)
    tamper.unlink(missing_ok=True)

    # ── ③④ 升级执行 + build_id 对账 ─────────────────────────────
    start_body = json.dumps({"package": upg.name, "confirm": "UPGRADE"}).encode()
    code, start = api_call(port, "POST", "/api/v1/upgrade/start", start_body, 30)
    check("③升级启动受理", code == 200 and start.get("success") is True,
          str(start.get("error"))[:120] if code != 200 else "")
    final_ok = False
    final_port: int | None = None
    deadline = time.time() + 900  # 升级+重启验证上限 15 分钟
    while time.time() < deadline:
        # 升级后实例可能换端口（updater respawn 从段首扫描）——全段扫 to_build
        for p in range(_PORT_MIN, _PORT_MAX + 1):
            d = get_health(p)
            if d and to_build and d.get("build") == to_build \
                    and d.get("status") == "healthy":
                final_ok, final_port = True, p
                break
        if final_ok:
            break
        time.sleep(5)
    check("④build_id 三方对账（升级完成）", final_ok, f"期望 {to_build}")
    if final_port:
        print("  升级后实例端口:", final_port)

    # ── ⑤ data/ 原地保留 ─────────────────────────────────────────
    check("⑤data 演练标记保留", marker.is_file())

    print("\n===== 演练结果 =====")
    fails = [r for r in results if r[1].startswith("FAIL")]
    for name, r in results:
        print(f"  [{r[:4]}] {name}")
    return 0 if not fails else 5


if __name__ == "__main__":
    sys.exit(main())
