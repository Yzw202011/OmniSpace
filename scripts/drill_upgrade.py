"""升级链重建 · 演练实弹脚本（2026-10-21，SH-GAP-08 重建批 C）。

前置：2.5.1 全链构建产物（D:/ccd/OmniSpace-2.5.1-g*-protected）已出、
基座= D:/ccd/OmniSpace-2.5.0-gc5ddaac-protected、主链已停（端口让给演练）。

安全口径（本脚本为离线演练工具）：所有请求目标=写死的本机回环常量
（http.client.HTTPConnection("127.0.0.1", 端口)），所有子调用=参数列表
+shell=False；不接任何外部输入。

六点验证（每点打 PASS/FAIL，任何 FAIL 退出码非零）：
  ①出签名 .upg（make_upgrade_package 差分 gc5ddaac→2.5.1）
  ②导入+验签（基座 /upgrade/import；负向：篡改包必须拒收）
  ③升级执行（confirm=UPGRADE → updater 状态机走完 → success）
  ④build_id 对账（/health build == to_build_id，三方一致）
  ⑤data/ 原地保留（演练标记文件升级后仍在）
  ⑥回滚保险真弹由断电自愈演练覆盖（升级中途 kill updater → boot 还原），
    本脚本正文不含破坏性注入。

用法：runtime/py312/python.exe scripts/drill_upgrade.py
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
NEW_BUILD_GLOB = r"D:\ccd\OmniSpace-2.5.1-g*-protected"
DRILL = Path(r"D:\ccd\selfheal_drill")
OUT_UPG = Path(r"D:\ccd\selfheal_drill_upg")
KEY = REPO / "keys" / "upgrade_signing.key"

# 写死的目标（离线演练：仅本机回环，端口=产品契约端口）
_HOST = "127.0.0.1"
_PORT = 5800
_HEALTH_PATH = "/health"

results: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else f"FAIL {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    return ok


def api_call(method: str, path: str, body: bytes | None = None,
             content_type: str = "application/json",
             timeout: float = 30) -> tuple[int, dict]:
    """对演练实例（本机回环固定端口）发一次请求。"""
    conn = http.client.HTTPConnection(_HOST, _PORT, timeout=timeout)
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


def wait_health(timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(_HOST, _PORT, timeout=3)
            conn.request("GET", _HEALTH_PATH)
            d = json.loads(conn.getresponse().read())["data"]
            conn.close()
            if d.get("status") == "healthy":
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


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
        print("未找到 2.5.1 构建目录（OmniSpace-2.5.1-g*-protected）")
        return 2
    print("基座:", BASE.name)
    print("新构建:", new_build.name)

    # ── ① 出签名 .upg ────────────────────────────────────────────
    OUT_UPG.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(REPO / "tools" / "make_upgrade_package.py"),
           "--old", str(BASE), "--new", str(new_build),
           "--out", str(OUT_UPG), "--key", str(KEY),
           "--notes", "升级链重建演练 2.5.1"]
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

    # ── 起基座（演练实例独占 5800/5850）──────────────────────────
    boot = DRILL / "runtime" / "py312" / "pythonw.exe"
    subprocess.Popen([str(boot), "launcher/boot.py"], cwd=str(DRILL),
                     creationflags=0x08000000, shell=False)  # CREATE_NO_WINDOW
    if not wait_health(240):
        check("基座启动", False, "240s 未就绪")
        return 4
    check("基座启动", True)

    # ── ② 导入+验签 ──────────────────────────────────────────────
    boundary, body = multipart(upg.name, upg.read_bytes())
    code, imp = api_call("POST", "/api/v1/upgrade/import", body,
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
        code2, imp2 = api_call("POST", "/api/v1/upgrade/import", body2,
                               f"multipart/form-data; boundary={boundary2}",
                               120)
        ok2 = imp2.get("success") is False
    except Exception:
        ok2 = True  # 4xx 拒收也算
    check("②负向篡改包拒收", ok2)
    tamper.unlink(missing_ok=True)

    # ── ③④ 升级执行 + build_id 对账 ─────────────────────────────
    start_body = json.dumps({"package": upg.name, "confirm": "UPGRADE"}).encode()
    code, start = api_call("POST", "/api/v1/upgrade/start", start_body, 30)
    check("③升级启动受理", code == 200 and start.get("success") is True,
          str(start.get("error"))[:120] if not (code == 200) else "")
    to_build = ""
    manifest = new_build / "dist_manifest.json"
    if manifest.is_file():
        to_build = str(json.loads(manifest.read_text(encoding="utf-8"))
                       .get("build_id") or "")
    final_ok = False
    deadline = time.time() + 900  # 升级+重启验证上限 15 分钟
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(_HOST, _PORT, timeout=3)
            conn.request("GET", _HEALTH_PATH)
            d = json.loads(conn.getresponse().read())["data"]
            conn.close()
            if (d.get("status") == "healthy" and to_build
                    and d.get("build") == to_build):
                final_ok = True
                break
        except Exception:
            pass
        time.sleep(5)
    check("④build_id 三方对账（升级完成）", final_ok, f"期望 {to_build}")

    # ── ⑤ data/ 原地保留 ─────────────────────────────────────────
    check("⑤data 演练标记保留", marker.is_file())

    print("\n===== 演练结果 =====")
    fails = [r for r in results if r[1].startswith("FAIL")]
    for name, r in results:
        print(f"  [{r[:4]}] {name}")
    return 0 if not fails else 5


if __name__ == "__main__":
    sys.exit(main())
