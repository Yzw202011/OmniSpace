# 本项目仅供学习使用，商业授权请+Q 3559331368
"""批3（2026-09-18）：数字错误码 → 语义码 全量替换工具。

277 处 `ApiError(<数字>, ...)` 按 _LEGACY_CODE_MAP 就地替换为语义串。
- AST 不必上：`ApiError(数字,` 前缀在行内唯一可辨（正则+行级复核足够，
  且要求替换后行内不再残留 `ApiError(数字`）
- 换行符保真（主导行尾回写，exc_info 批同款教训）
- 映射表真源 = error_handler._LEGACY_CODE_MAP 运行时导入（不复制第二份）
- 白名单补丁：20020 → VLLM_TERMINATE_FAILED（表外孤儿码，人工指派）

用法：runtime/py310/python.exe tools/replace_numeric_codes.py [--dry]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 表外孤儿码的人工指派（20020 唯一：vLLM 子进程终止失败）
MANUAL = {20020: "VLLM_TERMINATE_FAILED"}


def _code_map() -> dict[int, str]:
    from src.middleware.error_handler import _LEGACY_CODE_MAP
    m = dict(_LEGACY_CODE_MAP)
    m.update(MANUAL)
    # 0=OK 是信封语义不是错误码——不应出现在 raise 位；出现则报而不换
    m.pop(0, None)
    return m


def main() -> int:
    dry = "--dry" in sys.argv
    cmap = _code_map()
    pat = re.compile(r"ApiError\(\s*(\d+)\s*,")
    total = 0
    miss: list[int] = []
    for p in sorted(ROOT.glob("src/**/*.py")):
        if "__pycache__" in str(p):
            continue
        raw = p.read_bytes().decode("utf-8")
        hits = pat.findall(raw)
        if not hits:
            continue
        crlf = raw.count("\r\n")
        nl = "\r\n" if crlf >= (raw.count("\n") - crlf) else "\n"
        text = raw.replace("\r\n", "\n")

        def _sub(m: re.Match) -> str:
            n = int(m.group(1))
            sem = cmap.get(n)
            if sem is None:
                miss.append(n)
                return m.group(0)
            return f'ApiError("{sem}",'

        new = pat.sub(_sub, text)
        n = len(hits) - len([x for x in hits if int(x) in miss])
        if new != text and not dry:
            p.write_bytes(new.replace("\n", nl).encode("utf-8")
                          if nl == "\r\n" else new.encode("utf-8"))
        total += n
        print(f"{p.relative_to(ROOT)}: {len(hits)} 处")
    print(f"合计 {total} 处{'（dry）' if dry else ''}；未映射码: {sorted(set(miss)) or '无'}")
    return 1 if miss else 0


if __name__ == "__main__":
    raise SystemExit(main())
