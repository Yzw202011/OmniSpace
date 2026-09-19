"""客户端激活门禁——公开仓库占位壳（2026-09-19 用户令：真实锁芯不入库）。

大白话：本文件是开源仓库里的「壳」。真实锁芯（v2 双格式验签/时限码到期
硬拦/时钟回拨守卫/解绑墓碑/公钥双藏/错峰巡检等，出包时被 Cython 编译成
二进制）只存在于开发与出包机上：src/license_gate_impl.py，已被
.gitignore 排除——克隆公开仓库的人拿不到验签与加固逻辑。防破解三层：
发码台私钥（保险库）+ 编译二进制 + 本策略。

- 开发机 / 出包机：impl 在场 → 下方显式转发真身全部公开接口（单测直连
  src.license_gate_impl，不经本壳）。
- 公开仓库克隆者：impl 不存在 → 开发态占位（gate_enabled=False，门禁
  完全旁路——与历史开发模式行为一致，项目可正常跑）。
- 接口面（GateError/gate_enabled/is_activated/activate/unbind/status/
  collect_fingerprints/deactivate/start_sweep/PUBKEY_HEX/PUBKEY_HEX_X）
  两态等价；make_dist --pubkey 注入目标=license_gate_impl.py。
"""
from __future__ import annotations

try:
    from . import license_gate_impl as _impl
except ImportError:  # 公开仓库克隆者：无真身，走占位
    _impl = None  # type: ignore[assignment]

if _impl is not None:  # 开发/出包机：真身全量接管（显式逐名转发）
    GateError: type[Exception] = _impl.GateError
    PUBKEY_HEX = _impl.PUBKEY_HEX
    gate_enabled = _impl.gate_enabled
    collect_fingerprints = _impl.collect_fingerprints
    is_activated = _impl.is_activated
    activate = _impl.activate
    unbind = _impl.unbind
    deactivate = _impl.deactivate
    start_sweep = _impl.start_sweep
    status = _impl.status
else:
    PUBKEY_HEX = ""
    PUBKEY_HEX_X = ""

    class _StubGateError(Exception):
        """激活失败（占位实现：本构建未启用门禁）。"""

    GateError = _StubGateError  # type: ignore[assignment]  # 两分支异类同签名

    def _stub_gate_enabled() -> bool:
        return False

    def _stub_collect_fingerprints(force: bool = False) -> list[str]:
        """占位：公开仓库不携带指纹采集逻辑（返回空，状态页如实显示）。"""
        return []

    def _stub_is_activated() -> tuple[bool, str]:
        return True, ""

    def _stub_activate(code: str) -> dict:
        raise GateError("当前构建未启用激活门禁（公开仓库占位层）")

    def _stub_unbind() -> str:
        raise GateError("当前构建未启用激活门禁（公开仓库占位层）")

    def _stub_deactivate() -> None:
        return None

    def _stub_start_sweep() -> None:
        return None

    def _stub_status() -> dict:
        return {"gate_enabled": False, "activated": True, "reason": "",
                "fingerprints": [], "license": None,
                "license_file": "data/license.bin"}

    gate_enabled = _stub_gate_enabled
    collect_fingerprints = _stub_collect_fingerprints
    is_activated = _stub_is_activated
    activate = _stub_activate
    unbind = _stub_unbind
    deactivate = _stub_deactivate
    start_sweep = _stub_start_sweep
    status = _stub_status
# 本项目仅供学习使用，商业授权请+Q 3553191368
