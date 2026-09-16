"""backend/ 兼容 shim（2026-09-16，src 扁平化重构后）。

仓库外冻结进程（打包台/发码台等，代码冻结、永不入库）硬编码
`uvicorn backend.main:app` 启动。本包不做任何逻辑，仅 re-export
src.main:app，保证旧启动命令在新布局下继续工作。

仓库内代码一律直接用 src.*，禁止新增对本包的引用。
"""
