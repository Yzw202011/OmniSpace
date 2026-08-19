#!/usr/bin/env python3
"""
安装包完整性校验工具（CLI）
- BLAKE3（优先）/ BLAKE2b-256（降级）对关键文件做全量哈希
- 支持生成 manifest 与比对 manifest 两种模式
用法:
  python tools/verify_integrity.py --write     生成 .manifest.json
  python tools/verify_integrity.py             与 .manifest.json 比对
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from launcher.launcher import EnvironmentChecker, LauncherConfig  # noqa: E402


def main() -> int:
    write = "--write" in sys.argv
    checker = EnvironmentChecker(LauncherConfig())
    manifest = PROJECT_ROOT / ".manifest.json"
    ok, msg = checker.blake3_quick_verify(
        manifest_path=manifest if manifest.exists() or write else None,
        write_manifest=write)
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
