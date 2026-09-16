"""枚举后端全部真实路由（含 _IncludedRouter 包装），输出 METHOD PATH 清单。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.main as m  # noqa: E402

app = m.app
out = []
for r in app.routes:
    if type(r).__name__ == "_IncludedRouter":
        orig = getattr(r, "original_router", None)
        ctx = getattr(r, "include_context", None)
        prefix = getattr(ctx, "prefix", "") if ctx is not None else ""
        for sub in (getattr(orig, "routes", None) or []):
            methods = ",".join(sorted(getattr(sub, "methods", None) or ["WS"]))
            out.append(f"{methods} {prefix}{sub.path}")
    elif hasattr(r, "methods"):
        out.append(f"{','.join(sorted(r.methods))} {r.path}")
print("TOTAL_ENDPOINTS", len(out))
print("\n".join(sorted(out)))
