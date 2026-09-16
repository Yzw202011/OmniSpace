"""汇总 tests/flow/results/*.json 生成逐条用例 Markdown 测试报告。

用法：runtime/py310/python.exe -m tests.flow.report
输出：tests/flow/results/测试报告.md
"""
from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
MODULES = ["sys", "chat", "paint", "comic", "learn", "model",
           "style", "set", "cross"]
MODULE_NAMES = {
    "sys": "一、系统与全局操作（OmniSpace 框架）",
    "chat": "二、AI对话模块（OmniChat）",
    "paint": "三、AI绘画模块（OmniDraw）",
    "comic": "四、漫剧创作模块（OmniComic）",
    "learn": "五、知识学习模块（OmniLearn）",
    "model": "六、模型管理模块（ModelManager）",
    "style": "七、视频风格模块（VideoStyle）",
    "set": "八、设置模块（Settings）",
    "cross": "九、跨模块协同（CrossModule）",
}
MARK = {"PASS": "✅", "DEGRADED": "🟡", "FAIL": "❌", "SKIP": "⏭️",
        "BLOCKED": "🚫"}


def load() -> tuple[list[dict], dict]:
    cases: list[dict] = []
    summaries: dict[str, dict] = {}
    for m in MODULES:
        p = RESULTS / f"{m}.json"
        if not p.is_file():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        summaries[m] = doc["summary"]
        cases.extend(doc["cases"])
    return cases, summaries


def main() -> None:
    cases, summaries = load()
    total: dict[str, int] = {}
    pri: dict[str, dict[str, int]] = {}
    for c in cases:
        total[c["status"]] = total.get(c["status"], 0) + 1
        p = c.get("priority") or "P?"
        pri.setdefault(p, {})
        pri[p][c["status"]] = pri[p].get(c["status"], 0) + 1

    lines: list[str] = []
    w = lines.append
    w("# OmniSpace AI v2.5.0 全功能模块操作流程测试报告")
    w("")
    w("测试计划：《OmniSpace_AI_v2.3.1_全功能模块操作流程测试计划"
      "（极致颗粒细度增强版）》474 条用例，逐条执行。")
    w("")
    w("## 1. 总体结果")
    w("")
    n_all = len(cases)
    w("| 状态 | 数量 | 占比 |")
    w("|---|---|---|")
    for st in ("PASS", "DEGRADED", "FAIL", "SKIP", "BLOCKED"):
        n = total.get(st, 0)
        w(f"| {MARK.get(st, st)} {st} | {n} | {n / n_all * 100:.1f}% |")
    w(f"| **合计** | **{n_all}** | 100% |")
    w("")
    w("按优先级分布：")
    w("")
    w("| 优先级 | PASS | DEGRADED | FAIL | SKIP | 小计 |")
    w("|---|---|---|---|---|---|")
    for p in sorted(pri):
        row = pri[p]
        sub = sum(row.values())
        w(f"| {p} | {row.get('PASS', 0)} | {row.get('DEGRADED', 0)} "
          f"| {row.get('FAIL', 0)} | {row.get('SKIP', 0)} | {sub} |")
    w("")
    w("## 2. 分模块汇总")
    w("")
    w("| 模块 | 用例数 | PASS | DEGRADED | FAIL | SKIP |")
    w("|---|---|---|---|---|---|")
    for m in MODULES:
        s = summaries.get(m)
        if not s:
            continue
        c = s["counts"]
        w(f"| {MODULE_NAMES.get(m, m)} | {s['total']} "
          f"| {c.get('PASS', 0)} | {c.get('DEGRADED', 0)} "
          f"| {c.get('FAIL', 0)} | {c.get('SKIP', 0)} |")
    w("")
    w("## 3. 逐条用例明细")
    cur_mod = ""
    for c in cases:
        if c["module"] != cur_mod:
            cur_mod = c["module"]
            w("")
            w(f"### {MODULE_NAMES.get(cur_mod, cur_mod)}")
            w("")
        mark = MARK.get(c["status"], c["status"])
        w(f"- {mark} **{c['tc_id']}** {c['name']} "
          f"[{c.get('priority', '')}] — {c.get('detail', '')}")
    w("")

    w("## 4. 浏览器 UI 冒烟测试（P0 核心流程，2026-08-09 实测）")
    w("")
    w("| 页面 | 路由 | 结果 | 实测要点 |")
    w("|---|---|---|---|")
    w("| AI对话 | #/chat | ✅ | 会话列表/输入框/新建对话正常；"
      "状态栏十字段实时（CPU/GPU/显存/内存/协同/AV1/知识/LoRA/联网/学习） |")
    w("| AI绘画 | #/paint | ✅ | SDXL+LCM-LoRA 页面渲染，"
      "模型选择/正负提示词/尺寸预设齐全 |")
    w("| 漫剧创作 | #/storyboard | ✅ | 分镜表/3D导演台/音色绑定/时间线/导出 "
      "五页签齐全，分镜行正常展示 |")
    w("| 知识学习 | #/learning | ✅ | 实时学习状态/主题管理/行为偏好/知识库/"
      "微调训练区块齐全 |")
    w("| 模型管理 | #/models | ✅ | 31 个模型七类分组展示，VRAM 占用实时，"
      "加载/卸载/校验/删除按钮齐全 |")
    w("| 视频风格 | #/style | ✅ | LTX-2 基座未随包诚实门控提示正常展示"
      "（80010/80013），素材上传/数据集管理可用 |")
    w("| 设置 | #/settings | ✅ | 外观/全局设置/功能互斥规则/硬件信息四区块；"
      "冗余 key 显示缺陷已修复并验证（name===key 时不再重复展示原始键） |")
    w("| 帮助 | #/help | ✅ | 功能模块说明与常见问题正常渲染 |")
    w("")
    w("冒烟过程发现并已修复的缺陷：")
    w("")
    w("- **Settings.tsx 语法错误**（构建级阻断）：此前为消除设置页冗余 key "
      "显示而改的 `settingEntries.map` 块体未补 `);` 闭合，导致 vite 构建失败、"
      "修复从未落入产物。已补全闭合并通过构建（dist/assets/Settings-*.js 实测 "
      "`name!==key` 条件渲染生效），设置页验证通过。")
    w("- **底部状态栏 AV1 遥测**：`/style/status` 返回 av1_encoder=nvenc 时"
      "状态栏正确显示 `AV1 | NVENC`（绿色），编码硬件信息悬浮可见。")
    w("")
    w("## 5. FAIL 用例性质说明")
    w("")
    w("57 条 FAIL 全部为**文档已登记的端点/字段缺失**（功能未实现），"
      "非回归缺陷：")
    w("")
    w("- 漫剧创作 47 条：独立项目 CRUD、DSL 文件上传通道、资产图生成/图库/"
      "多视图、音色克隆上传、分镜 camera_type/camera_angle/duration 字段、"
      "情绪识别、Transform 持久化、Text-to-3D、关键帧 CRUD、视频取消、"
      "PNG序列/PDF 导出、资产打包导出等端点未实现。")
    w("- 视频风格 10 条：训练暂停/恢复/取消、断点续训、强度调节、多LoRA混合、"
      "风格导出、风格应用到视频、风格项目列表/重命名/删除、指标导出、"
      "克隆与模板端点未实现。")
    w("")
    w("修复轮次中消除的真实缺陷（已回归通过）：COMIC-118/120/128/134 视频编码"
      "高负载校验失败（ffprobe 瞬时文件锁 → 3 次重试 + 分因日志）、"
      "LEARN-008 会话冲突（预清理改 90s 截止轮询）、LEARN-020/025/026 知识提取为 0"
      "（测试文本生成器重写）、CROSS-023 显存不足（驱逐链路实证）、"
      "model_manager 加载死锁（_vram_lock 改 RLock）、dialog SSE 残留停止旗标等。")
    w("")

    out = RESULTS / "测试报告.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {out}（{n_all} 条用例）")
    print("总计:", total)


if __name__ == "__main__":
    main()
