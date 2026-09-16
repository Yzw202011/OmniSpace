"""backend.main 兼容 shim —— 真源 = src/main.py。

uvicorn backend.main:app 与 uvicorn src.main:app 等价；
新代码/新进程请直接使用 src.main:app。
"""
from src.main import app, lifespan  # noqa: F401  (re-export)

__all__ = ["app", "lifespan"]
