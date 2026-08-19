"""同步阻塞计算统一调度入口（TASK-P1-06，审计 P08）。

后端铁律：async 侧调用任何重计算同步函数（模型推理 / 权重加载 /
COM 调用 / 大文件哈希）必须经 ``run_blocking``——这是唯一入口。

结构动机（取代旧式注释契约——靠 docstring 提醒调用方自行卸载线程的
口头约定）：
  - 旧模式漏一次就卡死事件循环（审计 P08）；
  - 新模式统一入口 + 自调度 async 包装：异步函数天然不可同步误用
    （漏 await 只得到 coroutine，不会阻塞循环），入口唯一可 grep、
    可在测试中锁定。

分层约定：
  - 引擎层（services/inference/*）方法全部是同步核心，自身不感知
    调度；异步侧只经本入口触达；
  - 轻量文件 IO / 遥测读取（毫秒级）可直接 asyncio.to_thread，不
    强制走本入口；秒级以上一律走本入口。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def run_blocking(func: Callable[..., Any], /, *args: Any,
                       **kwargs: Any) -> Any:
    """唯一同步推理入口：调度同步阻塞函数到线程池并等待结果。

    语义等价 ``asyncio.to_thread(func, *args, **kwargs)``；收敛为
    单一入口是为了可 grep、可在测试中锁定"推理必经此路"。
    """
    return await asyncio.to_thread(func, *args, **kwargs)


def sync_core(func: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """把同步核心包装成自调度 async 函数（装饰器形态）。

    用于 API 层的私有辅助（如被动补全搜索/重推理）：包装后调用方
    ``await`` 即可，结构上不存在"忘放线程池"的误用面。
    """

    async def _wrapper(*a: Any, **kw: Any) -> Any:
        return await asyncio.to_thread(func, *a, **kw)

    _wrapper.__name__ = func.__name__
    _wrapper.__doc__ = (func.__doc__ or "") + "\n\n（自调度：经 run_blocking 卸载执行）"
    return _wrapper
