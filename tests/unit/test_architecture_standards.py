"""架构标准哨兵（2026-09-21 标准引擎核心）。

大白话：把 CLAUDE.md 里的"必须/禁止"从文字建议变成机器检查。
每个测试函数守一条规则，新违规即红，存量违规记在基线里只减不增。

设计原则（沿用 test_error_code_registry 模式）：
① 源码扫描（grep/正则），不跑真实业务逻辑
② 扫描器自检（scanner_self_check 防扫描器静默失效）
③ 基线预算（architecture_baseline.json，同 mypy_baseline 模式）

基线文件：tools/architecture_baseline.json
  原则：新违规=红（零容忍）；存量=记基线只减不增
  再生成：手动编辑 json（每次修复后更新数字，不提供自动 regen——
  防止 AI 会话顺手 regen 把新增违规"合法化"）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FRONTEND_SRC = ROOT / "frontend" / "src"
BASELINE_FILE = ROOT / "tools" / "architecture_baseline.json"

# ── 基线加载 ────────────────────────────────────────────────────


def _baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {}
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def _allowed(section: str, key: str = "current", default: int = 0) -> int:
    return int(_baseline().get(section, {}).get(key, default))


# ── ① 文件大小硬限 ──────────────────────────────────────────────


def _py_line_counts() -> list[tuple[str, int]]:
    result = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        n = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
        result.append((p.relative_to(ROOT).as_posix(), n))
    return result


def test_file_size_hard_limit() -> None:
    """src/ 下 .py 文件大小纪律：新文件 ≤2000 行；基线内巨石只减不增。

    存量基线：7 个 >2000 行文件记入 baseline，不许增长超 50 行。
    新文件：硬限 2000 行，超过即红。
    """
    baseline = _baseline().get("max_file_lines", {})
    recorded = baseline.get("recorded_lines", {})
    files = _py_line_counts()

    for f, n in files:
        if f in recorded:
            # 基线内文件：不许增长超 50 行
            assert n <= recorded[f] + 50, (
                f"{f} 从基线 {recorded[f]} 行增长到 {n} 行——"
                f"巨石文件只许拆不许涨（tools/architecture_baseline.json）"
            )
        else:
            # 基线外文件：硬限 2000 行（不受基线最大值影响）
            assert n <= 2000, (
                f"{f} 有 {n} 行超过硬限 2000——"
                f"新文件超过 2000 行必须先拆再提交"
            )


# ── ② KV 单源 ───────────────────────────────────────────────────

_KV_SQL_RE = re.compile(r'(?:FROM|INTO|UPDATE)\s+system_settings', re.I)


def test_kv_single_source() -> None:
    """全库 system_settings SQL 只允许出现在 settings_kv.py。

    B1 根修防回归：KV 已从 12 种读法收敛到 1 种（settings_kv），
    此测试防止任何新会话绕过单源直接写 SQL。
    """
    allowed_files = {"src/data/settings_kv.py"}
    violations = []
    for p in SRC.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in allowed_files:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if _KV_SQL_RE.search(text):
            violations.append(rel)
    assert not violations, (
        f"system_settings SQL 出现在 {violations}——"
        f"必须走 src/data/settings_kv.py 单源（B1 根修防回归）"
    )


# ── ③ CSS 变量纪律 ──────────────────────────────────────────────

_CSS_HEX_RE = re.compile(r'#[0-9a-fA-F]{4,8}\b')
# 主题族定义文件豁免（CLAUDE.md §2.5 用户拍板）
_CSS_EXEMPT_STEMS = {
    "tokens", "tech-theme", "sakura-theme", "sakura",
    "noir", "dazzle", "tech-fx", "dali-theme", "app",
}


def test_css_variable_discipline_in_tsx() -> None:
    """frontend tsx 不得硬编码 hex 颜色（必须 var(--color-*)）。

    CLAUDE.md §2.5 铁律的机器执行。仅扫 .tsx（app.css 存量另计）。
    """
    allowed = _allowed("css_tsx_hardcoded", "current", 3)
    violations = []
    for p in FRONTEND_SRC.rglob("*.tsx"):
        stem = p.stem
        # 豁免：主题定义文件
        if stem in _CSS_EXEMPT_STEMS:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        # 只检查 style={{ }} 内的（内联样式区域）
        for m in re.finditer(r'style=\{\{[^}]+\}\}', text):
            block = m.group()
            if _CSS_HEX_RE.search(block):
                line = text[:m.start()].count("\n") + 1
                violations.append(f"{p.relative_to(ROOT)}:{line}")
                break  # 每文件只报一次
    assert len(violations) <= allowed, (
        f"以下 tsx 含硬编码颜色（CSS 铁律：必须 var(--color-*)）："
        f"{violations[:5]} 共 {len(violations)} 文件（基线 {allowed}）"
    )


# ── ④ 配置端口单源 ──────────────────────────────────────────────

_PORT_8189_RE = re.compile(r'["\']8189["\']')
_KNOWN_PORT_FILES = {
    "src/services/inference/comfy_paint_engine.py",
    "src/services/inference/comfy_proc.py",
    "src/services/inference/h3_engine.py",
}


def test_config_port_8189_no_new_hardcode() -> None:
    """8189 端口不得在新的文件中出现硬编码（B2 根修防回归）。

    存量 3 个 src/ 文件 + launcher/boot.py + launcher/stop.py 记入
    基线豁免，新增即红。
    """
    violations = []
    for p in SRC.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in _KNOWN_PORT_FILES:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if _PORT_8189_RE.search(text):
            violations.append(rel)
    assert not violations, (
        f"8189 端口硬编码出现在 {violations}——"
        f"应从 config 层读取（B2 根修防回归，豁免={_KNOWN_PORT_FILES}）"
    )


# ── ⑤ 孤儿文件防回归 ────────────────────────────────────────────

_KNOWN_ORPHANS = {"src/services/lora_converter.py"}


def test_no_new_orphan_files() -> None:
    """src/ 下零引用 .py 文件不得超过已知孤儿数（死代码防回归）。

    孤儿=模块名在 src/tests/tools 全量代码中零引用。
    已知孤儿记入基线豁免，新增即红。
    """
    # 收集全部文本（用于引用检测）
    all_text_parts = []
    for p in SRC.rglob("*.py"):
        if "__pycache__" not in str(p):
            all_text_parts.append(
                p.read_text(encoding="utf-8", errors="ignore"))
    for p in (ROOT / "tests").rglob("*.py"):
        if "__pycache__" not in str(p):
            all_text_parts.append(
                p.read_text(encoding="utf-8", errors="ignore"))
    all_text = "\n".join(all_text_parts)

    orphans = []
    for p in SRC.rglob("*.py"):
        if "__pycache__" in str(p) or p.name == "__init__.py":
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in _KNOWN_ORPHANS:
            continue
        stem = p.stem
        # 模块名在代码中有引用（import/from/字符串）
        if f" {stem}" not in all_text and f".{stem}" not in all_text \
                and f"import {stem}" not in all_text:
            orphans.append(rel)
    assert not orphans, (
        f"以下文件可能是新孤儿（零引用）：{orphans}——"
        f"请删除或添加引用（已知孤儿豁免={_KNOWN_ORPHANS}）"
    )


# ── ⑥ 手搓校验比例锁定 ──────────────────────────────────────────

_BODY_DICT_RE = re.compile(r'body:\s*dict\s*=\s*Body\(')


def test_body_dict_count_not_worsening() -> None:
    """API 层 body:dict 手搓校验总数不得超过基线（防恶化）。

    第四轮审计：99→165 处恶化。此测试锁定当前值，新增即红——
    新端点必须用 Pydantic 模型（CLAUDE.md §3.1）。
    """
    allowed = _allowed("body_dict_count", "current", 100)
    count = 0
    for p in (SRC / "api").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        count += len(_BODY_DICT_RE.findall(text))
    assert count <= allowed, (
        f"body:dict 手搓校验 {count} 处超过基线 {allowed}——"
        f"新端点必须用 Pydantic 模型（CLAUDE.md §3.1）"
    )


# ── ⑦ 扫描器自检 ────────────────────────────────────────────────

def test_scanner_self_check() -> None:
    """哨兵自检：确保扫描器能找到已知目标（防静默失效）。"""
    # ① 能找到 src 目录
    assert SRC.exists() and (SRC / "main.py").exists(), \
        "src/main.py 不存在——扫描路径损坏"
    # ② 能找到 settings_kv（KV 单源的锚点）
    assert (SRC / "data" / "settings_kv.py").exists(), \
        "settings_kv.py 不存在——KV 单源锚点丢失"
    # ③ 能找到 frontend（CSS 检查的锚点）
    assert FRONTEND_SRC.exists(), "frontend/src 不存在——CSS 检查路径损坏"
    # ④ 文件大小扫描器能读到足够多文件
    files = _py_line_counts()
    assert len(files) >= 150, \
        f"src/ 只扫到 {len(files)} 个文件（应 190+）——扫描器可能损坏"
    # ⑤ 基线文件存在且可解析
    bl = _baseline()
    assert isinstance(bl, dict), "architecture_baseline.json 解析失败"
    assert "max_file_lines" in bl, "baseline 缺 max_file_lines 节"
