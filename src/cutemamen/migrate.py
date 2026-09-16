"""migrate-v1-to-v2: CuteMamen v1 插件包 → v2 标准自动迁移工具

规范 §2.2 标准演进规则: 允许破坏性变更, 解码器吸收迁移成本,
本工具把 v1 插件包原地 (或到输出目录) 迁移到 v2:
    - manifest.json: standard_version 1.x → 2.0.0
    - manifest.json: model_type → base_model (重命名)
    - manifest.json: legacy_mode 删除 (废弃)
    - lifecycle: on_init → on_load (钩子重命名)
    - lifecycle: 补缺失的 on_unload 空实现
    - 无法映射的 v1 字段 (如 pipeline_config): 跳过并警告
      (--strict 下报错, 需人工迁移)

用法:
    migrate-v1-to-v2 ./my-plugin.CuteMamen
    migrate-v1-to-v2 ./plugins/ --recursive
    migrate-v1-to-v2 ./my-plugin.CuteMamen --output ./migrated/ --backup

退出码: 0 全部成功 / 1 部分需人工迁移 / 2 严重错误
"""

import argparse
import io
import json
import os
import sys
import tarfile
import time
from typing import Dict, List, Optional, Tuple

from .pkg import (CUTEMAMEN_SUFFIX, MANIFEST_PATH, V1_RENAMED_FIELDS,
                  V1_REMOVED_FIELDS, V1_UNMAPPABLE_FIELDS,
                  V1_RENAMED_HOOKS, REQUIRED_HOOKS, _version_tuple)

EXIT_OK = 0
EXIT_MANUAL = 1
EXIT_SEVERE = 2


def migrate_manifest(raw: Dict, to_version: str = "2.0.0",
                     strict: bool = False) -> Tuple[Dict, List[str], List[str]]:
    """v1 清单 → v2 清单, 返回 (新清单, 成功变更, 需人工项)"""
    changes: List[str] = []
    manual: List[str] = []
    manifest = dict(raw)  # 未知字段保留 (前向兼容)

    for old, new in V1_RENAMED_FIELDS.items():
        if old in manifest and new not in manifest:
            manifest[new] = manifest.pop(old)
            changes.append(f'manifest.json: renamed field "{old}" → "{new}"')

    for field in V1_REMOVED_FIELDS:
        if field in manifest:
            manifest.pop(field)
            changes.append(f'manifest.json: removed deprecated field "{field}"')

    lifecycle = manifest.get("lifecycle")
    if isinstance(lifecycle, dict):
        for old, new in V1_RENAMED_HOOKS.items():
            if old in lifecycle and new not in lifecycle:
                lifecycle[new] = lifecycle.pop(old)
                changes.append(f'lifecycle: renamed hook "{old}" → "{new}"')
        if "on_unload" not in lifecycle:
            lifecycle["on_unload"] = "stub"
            changes.append('lifecycle: added missing hook "on_unload" (stub)')

    for field in V1_UNMAPPABLE_FIELDS:
        if field in manifest:
            manual.append(f'manifest.json: field "{field}" has no v2 equivalent')
            if strict:
                break

    std = str(manifest.get("standard_version", "1.0.0"))
    manifest["standard_version"] = to_version
    changes.append(f'manifest.json: standard_version {std} → {to_version}')
    return manifest, changes, manual


def migrate_pkg(path: str, output: Optional[str] = None,
                to_version: str = "2.0.0", dry_run: bool = False,
                verbose: bool = False, strict: bool = False,
                backup: bool = False) -> Tuple[bool, bool]:
    """迁移单个包, 返回 (是否成功, 是否需要人工)"""
    label = os.path.basename(path)
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = []
            for m in tar.getmembers():
                if m.isdir():
                    continue
                members.append((m.name, tar.extractfile(m).read()))
    except (tarfile.TarError, OSError) as exc:
        print(f"✗ {label}: 无法读取 ({exc})")
        return False, False

    raw_manifest = None
    for name, data in members:
        if name == MANIFEST_PATH or name.endswith("/" + MANIFEST_PATH):
            raw_manifest = json.loads(data.decode())
            break
    if raw_manifest is None:
        print(f"✗ {label}: 缺少 manifest.json, 无法迁移")
        return False, False

    std = str(raw_manifest.get("standard_version", "1.0.0"))
    if _version_tuple(std) >= (2,):
        print(f"✓ {label}: already v2 (standard_version {std}), skipped")
        return True, False

    manifest, changes, manual = migrate_manifest(raw_manifest, to_version, strict)
    for c in changes:
        print(f"  ✓ {c}" if verbose else f"  ✓ {label}: {c}")
    for w in manual:
        print(f"  ⚠ {w} — skipped (use --strict to fail)")
    if manual and strict:
        print(f"✗ {label}: {len(manual)} 个字段无法自动迁移, 需人工处理")
        return False, True

    if dry_run:
        print(f"  [DRY RUN] {label}: 不写入")
        return True, bool(manual)

    target = path
    if output:
        os.makedirs(output, exist_ok=True)
        target = os.path.join(output, label)
    if backup and os.path.exists(target):
        backup_path = target + ".bak"
        os.replace(target, backup_path)
        print(f"  ✓ backup: {os.path.basename(backup_path)}")

    try:
        with tarfile.open(target, "w:gz") as tar:
            for name, data in members:
                if name == MANIFEST_PATH or name.endswith("/" + MANIFEST_PATH):
                    data = json.dumps(manifest, ensure_ascii=False,
                                      indent=2).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
    except (tarfile.TarError, OSError) as exc:
        print(f"✗ {label}: 写入失败 ({exc})")
        return False, False
    print(f"  ✓ packaged: {target} (v{to_version})")
    return True, bool(manual)


def collect_targets(paths: List[str], recursive: bool) -> List[str]:
    targets: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        if f.endswith(CUTEMAMEN_SUFFIX):
                            targets.append(os.path.join(root, f))
            else:
                targets.extend(
                    os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.endswith(CUTEMAMEN_SUFFIX))
        elif os.path.isfile(p):
            targets.append(p)
        else:
            print(f"✗ 路径不存在: {p}")
    return targets


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate-v1-to-v2",
        description="CuteMamen v1 插件包 → v2 标准自动迁移工具")
    parser.add_argument("paths", nargs="+",
                        help=".CuteMamen 文件或目录")
    parser.add_argument("--output", "-o", default=None,
                        help="输出目录 (不指定则覆盖原文件)")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="递归处理目录下所有 .CuteMamen 文件")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="预览模式, 只报告变更, 不实际写入")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="显示详细迁移日志")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式: 无法自动迁移的字段直接报错退出")
    parser.add_argument("--backup", "-b", action="store_true",
                        help="迁移前备份原文件为 .CuteMamen.bak")
    parser.add_argument("--from", dest="from_version", default=None,
                        help="源版本号 (默认自动检测)")
    parser.add_argument("--to", dest="to_version", default="2.0.0",
                        help="目标版本号 (默认 2.0.0)")
    args = parser.parse_args(argv)

    if args.dry_run:
        print("[DRY RUN] No files will be modified.\n")

    targets = collect_targets(args.paths, args.recursive)
    if not targets:
        print("未找到可迁移的 .CuteMamen 插件包")
        return EXIT_SEVERE
    # 单文件 + dry-run 提示 (与 README 示例一致)
    if len(targets) == 1 and args.dry_run:
        print(f"Would apply the following changes to "
              f"{os.path.basename(targets[0])}:\n")

    n_ok = n_manual = n_fail = 0
    for i, path in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] Migrating: {path}")
        ok, needs_manual = migrate_pkg(
            path, output=args.output, to_version=args.to_version,
            dry_run=args.dry_run, verbose=args.verbose,
            strict=args.strict, backup=args.backup)
        if ok and not needs_manual:
            n_ok += 1
        elif needs_manual:
            n_manual += 1
        else:
            n_fail += 1

    print()
    if n_fail:
        print(f"Done. {n_ok} migrated, {n_manual + n_fail} failed/need manual.")
        return EXIT_SEVERE if n_ok == 0 else EXIT_MANUAL
    if n_manual:
        print(f"Done. {n_ok} migrated, {n_manual} needs manual intervention.")
        return EXIT_MANUAL
    print(f"Done. {n_ok} migrated.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
