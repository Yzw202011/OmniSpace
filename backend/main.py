"""OmniSpace AI v2.3.1 后端主入口（文档 §7.1 后端架构 / §6.4 启动时序）。

启动时序：端口绑定 → 数据库初始化 → 文件存储/缓存 → 调度引擎 → WebSocket → 就绪。
文档引用：§7.1 路由层 BASE_URL=/api/v1（ADR-03 由 /v1 迁移）, §8.4 模块间协调, §14 约束2: 绑定127.0.0.1:5800
"""
from __future__ import annotations

import asyncio
import importlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount

from . import config
from .data.database import get_db
from .middleware.cors import setup_cors
from .middleware.error_handler import ApiError, error, ok
from .middleware.logger import setup_logging
from .middleware.rate_limit import setup_rate_limit

# ── 日志初始化 ──────────────────────────────────────────────────
log = setup_logging()
_boot_ts = time.time()

# ── v2.1 调度引擎可用性探测 ──────────────────────────────────────
_SCHEDULER_AVAILABLE = False
try:
    from .services.scheduler import get_scheduler as _get_scheduler
    _SCHEDULER_AVAILABLE = True
except Exception:
    _get_scheduler = None  # type: ignore


# ═══════════════════════════════════════════════════════════════════
#  Lifespan
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 60)
    log.info("OmniSpace AI v2.3.1 后端启动 (%s:%s)", config.HOST, config.PORT)
    log.info("版本: %s", config.APP_VERSION)
    log.info("=" * 60)
    # 审计 R3-BE3：非回环绑定醒目告警（API 无认证体系，规格 §14 约束2 要求 127.0.0.1）
    if config.HOST not in ("127.0.0.1", "localhost"):
        log.warning("!" * 60)
        log.warning("安全告警：当前绑定非回环地址 %s，API 无认证体系，"
                    "存在局域网暴露风险（规格 §14 约束2 要求 127.0.0.1）",
                    config.HOST)
        log.warning("!" * 60)

    # T+0s: 数据库
    try:
        db = get_db()
        db.query_one("SELECT 1 AS one")
        log.info("T+0s 数据库初始化完成: %s", config.DB_PATH)
    except Exception as exc:
        log.error("数据库初始化失败: %s", exc)
        raise

    # T+3s: 文件存储与缓存
    try:
        from .data.cache import get_cache
        from .data.file_store import get_file_store
        get_file_store()
        get_cache()
        log.info("T+3s 文件存储与缓存初始化完成")
    except Exception as exc:
        log.warning("文件存储/缓存初始化异常: %s", exc)

    # T+4s: 预加载嵌入模型（显存布局关键优化，勿后移）
    # bge-large-zh 是长驻 CUDA 的小模型（~1.3GB）。若等到首次 RAG 才加载，
    # 其权重会落在对话/绘画大模型之后分配的高位显存段；大模型卸载时这些
    # 被小权重"钉住"的段无法归还驱动（PyTorch 分配器 inactive_split），
    # 实测 16GB 卡上卸载对话模型后空闲仅 5.1GB（应为 13.4GB），导致后续
    # 所有大模型加载全部报"显存不足"。启动时最先加载嵌入模型，让其权重
    # 独占低位段，大模型段即可整体释放（实测卸载后空闲回到 13.4GB）。
    # 附带收益：首次 RAG 检索无模型加载冷延迟（曾测得 2825ms 超标）。
    try:
        from .data.vector_db import get_vector_db
        get_vector_db().warmup()  # 触发嵌入模型加载+前向推理（CUDA 长驻低位显存段）
        log.info("T+4s 嵌入模型预加载完成（CUDA 长驻）")
    except Exception as exc:
        log.warning("嵌入模型预加载失败（降级运行）: %s", exc)

    # T+5s: 调度引擎（硬件监控 / 模型调度，可选）
    if _SCHEDULER_AVAILABLE:
        try:
            scheduler = _get_scheduler()
            await scheduler.start()
            log.info("T+5s 调度引擎已启动")
        except Exception as exc:
            log.warning("调度引擎启动失败（降级运行）: %s", exc)
    else:
        log.warning("调度引擎模块不可用，跳过启动（降级运行）")

    # T+5.5s: 浏览器进程池预热（TASK-054：学习会话 acquire <1s）
    # 后台守护线程执行 Chromium 启动（~2-15s），不阻塞就绪时序；
    # playwright 不可用时自动跳过（懒初始化降级，行为与 v2.3 一致）。
    try:
        from .services.browser_pool import get_browser_pool
        get_browser_pool().warmup(headless=True, background=True)
        log.info("T+5.5s 浏览器进程池预热已调度（后台）")
    except Exception as exc:
        log.warning("浏览器进程池预热调度失败（降级懒初始化）: %s", exc)

    # T+6s: WebSocket 消息中枢（规格 §2.2 /ws 协议）
    # 绑定事件循环、启动遥测推送，并向绘画/LoRA训练/浏览器Agent 注入广播器
    try:
        from .api import draw as _draw_api
        from .services import browser_agent_service as _agent_svc
        from .services import lora_training_service as _lora_svc
        from .services.ws_hub import get_ws_hub
        hub = get_ws_hub()
        hub.bind_loop(asyncio.get_running_loop())
        hub.start_telemetry()
        _draw_api.set_ws_broadcaster(hub.broadcast)
        _lora_svc.set_ws_broadcaster(hub.broadcast)
        _agent_svc.set_ws_broadcaster(hub.broadcast)
        log.info("T+6s WebSocket 消息中枢已启动（/ws），广播器已注入 paint/learn/agent")
    except Exception as exc:
        log.warning("WebSocket 消息中枢启动失败（降级运行）: %s", exc)

    log.info("T+10s 后端就绪，等待请求")
    log.info("=" * 60)
    yield

    # 关闭
    log.info("OmniSpace AI 后端关闭中...")
    try:
        from .services.browser_pool import get_browser_pool
        get_browser_pool().shutdown()
    except Exception:
        pass
    try:
        from .services.ws_hub import get_ws_hub
        await get_ws_hub().stop_telemetry()
    except Exception:
        pass
    if _SCHEDULER_AVAILABLE:
        try:
            scheduler = _get_scheduler()
            if hasattr(scheduler, "stop"):
                await scheduler.stop()
        except Exception:
            pass
    log.info("后端已停止")


# ═══════════════════════════════════════════════════════════════════
#  应用工厂
# ═══════════════════════════════════════════════════════════════════

# v2.1 API 路由模块（规格 §4 API接口完整定义）
_API_MODULES = [
    "dialog",    # §4.2 对话API
    "draw",      # §4.3 绘画API
    "manga",     # §4.4 漫剧API
    "learn",     # 知识学习API（训练任务/数据集）
    "learning",  # v2.3 学习API（主题/会话/调度配额）
    "knowledge", # v2.3 知识库+行为学习API
    "browser",   # v2.3 浏览器Agent API
    "models",    # §4.5 模型管理API
    "style",     # 文档 §7.1.4 视频风格API（/style/train, /preview, /versions）
    "vision_tools",  # 文档E 附录B /art：TripoSR/SAM/MiDaS/YOLOv8 随包模型接线（F-01~F-04）
    "voice",     # 语音API（/voice/transcribe Whisper ASR + /voice/synthesize TTS 自动装载链）
    "hardware",  # §4.6 硬件API
    "system",    # §4.7 系统API
]


def _register_routers(app: FastAPI) -> None:
    """注册 v2.1 API 路由模块。"""
    registered = 0
    for name in _API_MODULES:
        try:
            mod = importlib.import_module(f".api.{name}", package=__package__)
            if hasattr(mod, "router"):
                app.include_router(mod.router, prefix=config.API_PREFIX)
                registered += 1
                log.info("已注册 v2.1 路由: api.%s", name)
        except ImportError as exc:
            log.warning("导入 api.%s 失败: %s", name, exc)
        except Exception as exc:
            log.warning("注册 api.%s 失败: %s", name, exc)
    log.info("路由注册完成: %d 个模块", registered)


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title="OmniSpace AI",
        version=config.APP_VERSION,
        description="OmniSpace AI v2.3.1 — 全模态创作工作站",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # 中间件
    setup_cors(app)        # §6.1 L4: 仅本地
    setup_rate_limit(app)  # §7: 100 req/min
    # 请求上下文（文档D meta.request_id/duration_ms 支撑）
    # 审计 R3-BE6：最后注册 = 最外层用户中间件（Starlette insert(0) 语义），
    # 使限流/CORS 拒绝的响应也带 X-Request-ID
    from .middleware.request_context import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware)
    # 审计 R3-SEC：Host 头校验（防 DNS 重绑定攻击）。最后注册 = 最外层
    # 用户中间件，非法 Host 在路由/其他中间件之前即被 400 拦截。
    # 已核实 Starlette 1.3.1 实现：仅取 Host 头 hostname 部分比对
    # （split(":")[0]），任意端口均放行，不影响 127.0.0.1:5800 正常访问。
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
    )

    # 异常处理（§6.1 / §8）
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        log.warning("ApiError: code=%s msg=%s", exc.code, exc.message)
        return error(exc.code, exc.message, exc.detail, suggestion=exc.suggestion)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        fields = [{"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg", "")}
                  for e in exc.errors()[:5]]
        detail_str = "; ".join(f"{'.'.join(f['loc'])} {f['msg']}" for f in fields)
        log.warning("参数校验失败: %s", detail_str)
        return error("SYSTEM_PARAM_INVALID", "参数校验失败：" + detail_str,
                     {"fields": fields})

    # 审计 P1-1：StarletteHTTPException 统一包装（404 路由不存在 / 405 方法
    # 不允许等框架级异常），避免泄漏 FastAPI 默认 {"detail": "Not Found"}
    # 结构——前端只需面对统一 {success,data,error,meta} 信封契约（审计 R1-07）。
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            log.info("路由不存在: %s", getattr(_, "url", "").path if _ else "")
            return error("SYSTEM_RESOURCE_NOT_FOUND", "资源不存在",
                         {"path": str(_.url.path) if _ else "",
                          "http_status": 404})
        if exc.status_code == 405:
            return error("SYSTEM_PARAM_INVALID", "请求方法不允许",
                         {"path": str(_.url.path) if _ else "",
                          "method": getattr(_, "method", ""),
                          "http_status": 405})
        log.warning("HTTP 异常: status=%s detail=%s",
                    exc.status_code, exc.detail)
        return error("SYSTEM_INTERNAL_ERROR",
                     f"HTTP 错误 {exc.status_code}: {exc.detail}",
                     {"http_status": exc.status_code})

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception):
        log.exception("未捕获异常: %s", exc)
        return error("SYSTEM_INTERNAL_ERROR", "内部错误，请查看服务端日志",
                     {"type": type(exc).__name__})

    # 路由注册
    _register_routers(app)

    # 健康检查
    @app.get("/health", include_in_schema=False)
    async def health():
        db_status = "ok"
        try:
            get_db().query_one("SELECT 1 AS one")
        except Exception:
            db_status = "error"
        return ok({"status": "healthy", "version": config.APP_VERSION,
                    "uptime_s": round(time.time() - _boot_ts, 1), "db": db_status})

    # WebSocket: 对话流式（§4.2 ws://127.0.0.1:5800/api/v1/dialog/stream/{session_id}）
    # DEPRECATED（F-011）：新前端改用 POST /api/v1/chat/stream（SSE）；
    # 本 WS 端点仅为兼容现有前端 ws.ts 保留。
    # 真实引擎推理实现在 api.dialog.handle_dialog_stream（协议与前端 ws.ts 契约对齐）
    @app.websocket(f"{config.API_PREFIX}/dialog/stream/{{session_id}}")
    async def dialog_stream(websocket: WebSocket, session_id: str):
        from .middleware.cors import ws_origin_guard
        if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
            return
        from .api.dialog import handle_dialog_stream
        await handle_dialog_stream(websocket, session_id)

    # WebSocket: 硬件实时监控（§4.6 ws://127.0.0.1:5800/api/v1/hardware/realtime）
    # 注意：hardware.py 中已通过 router 注册了 /hardware/realtime WebSocket 端点，
    # 经 prefix=/api/v1 挂载后即可访问，此处无需重复定义。

    # WebSocket: 通用消息中枢（§2.2 ws://127.0.0.1:5800/ws）
    # 前端 useWebSocket 默认连接此端点；支持 ping/pong 与 subscribe_system。
    @app.websocket("/ws")
    async def ws_hub_endpoint(websocket: WebSocket):
        from .middleware.cors import ws_origin_guard
        if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
            return
        from .services.ws_hub import get_ws_hub
        await get_ws_hub().handle_connection(websocket)

    # 前端静态资源（§14约束2：离线本地前端）
    class NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    # 优先使用构建后的 dist 目录
    dist_dir = config.FRONTEND_DIR / "dist"
    static_dir = dist_dir if dist_dir.exists() else config.FRONTEND_DIR
    # favicon 必须在根路径挂载之前注册，否则被 Mount("/") 吞掉
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return JSONResponse(status_code=204, content=None)
    if static_dir.exists():
        # Vite 构建产物引用 /assets/...（base=/），挂载在根路径使其可直接访问；
        # html=True 使 GET / 返回 index.html（前端使用 Hash 路由，无需 SPA fallback）。
        # API（/api/v1）、/health、WebSocket 路由均先于本挂载注册，优先匹配不受影响。
        # 注意：Mount("/") 会全量匹配——用 _ApiAwareMount 排除 API 命名空间，
        # 使 GET 打到 POST 端点能回退到路由部分匹配返回 405（而非静态 404），
        # 未知 /api/v1 路径返回 JSON 404（而非 HTML）。
        class _ApiAwareMount(Mount):
            _API_PREFIXES = ("/api/v1", "/health", "/ws", "/favicon.ico")

            def matches(self, scope):  # type: ignore[override]
                path = scope.get("path", "")
                for p in self._API_PREFIXES:
                    if path == p or path.startswith(p + "/"):
                        return Match.NONE, {}
                return super().matches(scope)

        static_app = NoCacheStaticFiles(directory=str(static_dir), html=True)
        app.router.routes.append(
            _ApiAwareMount("/", app=static_app, name="static"))

    return app


# ── 应用实例（uvicorn 直接引用）──────────────────────────────────
app = create_app()


def main() -> None:
    """启动 uvicorn 服务器。"""
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
