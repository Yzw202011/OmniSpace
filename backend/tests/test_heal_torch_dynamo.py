"""torch dynamo 编译缓存注册表自愈测试（2026-08-22 对话模型加载失败修复）。

复现生产损坏：torch._dynamo 导入链半途失败后 Python 清理半成品模块、
torch.compiler._cache 注册表残留 → 二次导入必炸 AssertionError。
验证 _heal_torch_dynamo 能恢复注册表并让 import 重新可用。
"""
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="需与后端一致的运行时")


def _corrupt_dynamo_registry(strip_username: bool = False) -> None:
    """模拟真实损坏：删除 dynamo 模块群（保留 torch.compiler._cache 注册表）。

    strip_username=True 时同时剥掉用户身份环境变量，完整复现 launcher
    白名单环境下的原始故障（getpass.getuser() → import pwd → Windows 炸）。
    """
    import torch._dynamo  # 确保健康导入过一次
    for name in [n for n in sys.modules
                 if n == "torch._dynamo" or n.startswith("torch._dynamo.")]:
        del sys.modules[name]
    if strip_username:
        for k in ("USERNAME", "USER", "LOGNAME", "LNAME"):
            os.environ.pop(k, None)


def test_heal_recovers_from_corrupted_registry():
    """损坏后二次导入必炸 → 自愈 → 导入恢复正常。"""
    from backend.services.inference.backends.transformers_backend import (
        _heal_torch_dynamo,
    )
    _corrupt_dynamo_registry()

    # 1) 损坏确认：二次导入在重复注册处抛 AssertionError
    with pytest.raises(AssertionError, match="already registered"):
        import torch._dynamo  # noqa: F401

    # 2) 自愈成功
    assert _heal_torch_dynamo() is True

    # 3) 导入链恢复且注册表条目完整（pgo/precompile 重新注册）
    import torch._dynamo  # noqa: F401
    from torch.compiler._cache import CacheArtifactFactory
    assert "pgo" in CacheArtifactFactory._artifact_types
    assert "precompile" in CacheArtifactFactory._artifact_types


def test_heal_recovers_without_username_env():
    """真实根因场景：无任何用户身份变量（launcher 白名单环境）也能自愈。

    dynamo 缓存目录解析经 getpass.getuser()，无身份变量时 fallback 到
    Unix-only 的 pwd 模块 → Windows 上 ModuleNotFoundError → 注册表残留。
    自愈函数内置 USERNAME 兜底，应能完整恢复。
    """
    from backend.services.inference.backends.transformers_backend import (
        _heal_torch_dynamo,
    )
    _corrupt_dynamo_registry(strip_username=True)
    assert "USERNAME" not in os.environ

    assert _heal_torch_dynamo() is True
    import torch._dynamo  # noqa: F401
    from torch.compiler._cache import CacheArtifactFactory
    assert "precompile" in CacheArtifactFactory._artifact_types


def test_heal_is_idempotent_on_healthy_state():
    """健康状态下调用自愈应无副作用（幂等）。"""
    from backend.services.inference.backends.transformers_backend import (
        _heal_torch_dynamo,
    )
    import torch._dynamo  # noqa: F401
    assert _heal_torch_dynamo() is True
    import torch._dynamo  # noqa: F401  重导入后仍可用
