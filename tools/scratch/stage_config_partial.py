# -*- coding: utf-8 -*-
"""config.yaml 部分暂存：索引只含 turnaround 切换，dflash（并发会话）留工作区。"""
import subprocess
from pathlib import Path

F = "src/config.yaml"
head = subprocess.run(["git", "show", f"HEAD:{F}"], capture_output=True).stdout
work = Path(F).read_bytes()

eol = b"\r\n" if b"turnaround_engine: legacy\r\n" in head else b"\n"
legacy = b"  turnaround_engine: legacy" + eol
assert head.count(legacy) == 1, "legacy line not found in HEAD"

comment = ("  # Z2 2026-09-15：zviews=Z-Image 分张管线（逐视图+参考锚身份+代码贴字，"
           "语义/标注构造性正确）；回滚=改回 legacy。").encode("utf-8") + eol
zviews = b"  turnaround_engine: zviews" + eol
new2 = comment + zviews
staged = head.replace(legacy, new2)

# dflash 块（并发会话）按行精确重建（CRLF）
dflash_lines = [
    "  # DFlash 块扩散投机解码（P-5 2026-09-15 新技术加速方案；默认 false",
    "  # =A/B 未收口）：true=开（输出数学无损——草稿仅提议、目标验证全权",
    "  # 裁决，对话行为不变）。与 MTP 同开时 DFlash 优先。仅对 qwen35-9b",
    "  # 家族目标生效（草稿=z-lab/Qwen3.5-9B-DFlash 官方配对未消融版",
    "  # 2.58GB，跨家族配对反向劣化）。显存：草稿+图画像 ~4GB，16GB 卡",
    "  # + 9B 需近乎空卡，准入线诚实拒。重启后端生效。",
    "  dflash_speculative: false",
    "  # DFlash 草稿权重目录（项目相对路径，须含 model.safetensors）",
    '  dflash_draft_model: "models/dflash/qwen35-9b-draft"',
]
dflash = b"".join(l.encode("utf-8") + eol for l in dflash_lines)

pos = staged.find(new2) + len(new2)
work_new = staged[:pos] + dflash + staged[pos:]

# 1) 写 staged 内容 -> git add（索引=HEAD+我的改动）
Path(F).write_bytes(staged)
subprocess.run(["git", "add", F], check=True)
# 2) 还原工作副本（HEAD+我的改动+dflash）
Path(F).write_bytes(work_new)

r1 = subprocess.run(["git", "diff", "--cached", "--stat", "--", F],
                    capture_output=True, text=True)
r2 = subprocess.run(["git", "diff", "--ignore-cr-at-eol", "--", F],
                    capture_output=True, text=True)
print("cached:", r1.stdout.strip())
print("unstaged contains dflash:", "dflash" in r2.stdout)
print("unstaged contains turnaround:", "turnaround_engine" in r2.stdout)
