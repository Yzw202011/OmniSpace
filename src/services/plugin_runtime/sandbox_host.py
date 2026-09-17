# 本项目仅供学习使用，商业授权请+Q 3559331368
"""沙箱子进程入口（插件安全模型终态=A，2026-09-17 用户拍板）。

user_source（用户·含源码）信任档的执行体：与后端同一运行时、
**独立 OS 进程**——插件代码崩了/超时/内存爆了都不连坐后端，父进程
可对超时与内存超限直接 kill（进程级硬杀，进程内线程杀不掉的固有限
制就此消除）。

协议（单行 JSON 进、单行 JSON 出）：
  stdin  : {"root", "name", "source_path", "pkg_path", "event"}
  stdout : {"ok": true, "result": <on_think 返回>}
           {"ok": false, "error": <回溯>}（异常/崩溃兜底）
  退出码 : 0=正常；非 0=异常（父侧按 rc 判定）

沙箱内的能力降级（诚实文档化，非缺陷）：
  - PluginContext.emit 为 no-op（插件事件不跨进程进 WS）；
  - 事件与结果只走 JSON（ndarray 不直传——帧类插件属出厂档不受影响）；
  - 权重/记忆由子进程从 .CuteMamen 包自读（复用产品 loader 同一套
    安全校验：条目白名单/尺寸上限/allow_pickle=False）。

用法（仅由 sandbox.py 父侧调用，不进 API 面）：
  python sandbox_host.py < stdin 单行 job
"""
from __future__ import annotations

import contextlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def _run_job(job: dict[str, Any]) -> Any:
    root = Path(job["root"])
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # 复用产品 loader：同一套装包校验 + omnispace.plugin 注入 +
    # 独立命名空间 exec + ExpertPlugin 子类发现
    from src.cutemamen.plugin import PluginContext
    from src.services.plugin_runtime.loader import (
        find_plugin_classes,
        load_plugin_module,
        read_cutemamen_pkg,
    )

    module = load_plugin_module(Path(job["source_path"]))
    classes = find_plugin_classes(module)
    if not classes:
        raise RuntimeError("模块内无 ExpertPlugin 子类")
    cls = classes[0]
    try:
        instance = cls()  # type: ignore[call-arg]  # 同 registry：无参优先
    except TypeError:
        instance = cls(name=job["name"])

    pkg_path = job.get("pkg_path")
    if pkg_path:
        pkg = read_cutemamen_pkg(Path(pkg_path))
        if hasattr(instance, "load_weights"):
            instance.load_weights(pkg.weights, pkg.manifest)
        instance.memory.restore(pkg.memory)

    # 沙箱档 ctx：emit 降级 no-op（事件不跨进程），其余契约不变
    ctx = PluginContext(emit_fn=lambda topic, payload: None,
                        plugin_name=job["name"])
    instance.on_load(ctx)
    return instance.on_think(job.get("event") or {}, ctx)


def main() -> int:
    try:
        job = json.loads(sys.stdin.readline())
    except Exception as exc:  # noqa: BLE001 - 协议层兜底
        print(json.dumps({"ok": False, "error": f"job 解析失败: {exc}"},
                         ensure_ascii=False))
        return 1
    # 插件自身的 print 全部改道 stderr——stdout 只许协议 JSON 走
    with contextlib.redirect_stdout(sys.stderr):
        try:
            result = _run_job(job)
        except Exception:  # noqa: BLE001 - 子进程全量兜底
            print(json.dumps(
                {"ok": False, "error": traceback.format_exc(limit=8)},
                ensure_ascii=False))
            return 1
    print(json.dumps({"ok": True, "result": result},
                     ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
