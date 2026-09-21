# Hello Plugin —— 最简 OmniSpace 插件模板

> 20 行代码、3 步上手。复制这个目录、改 `plugin.py`、打包、导入。

## 快速开始

```bash
# ① 改 plugin.py（已给最简可运行模板）
# ② 打包（在 OmniSpace 仓库根目录跑）：
runtime/py312/python.exe tools/build_plugin.py examples/hello-plugin/plugin.py hello.CuteMamen --skills chat
# ③ 导入：设置 → 插件 → 导入 → 选 hello.CuteMamen → 确认
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `plugin.py` | 插件源码（你只需要改这个文件） |
| `README.md` | 本说明 |

## 插件能做什么

- **纯计算**：文本处理、数学、格式转换
- **记忆**：跨调用记住数据（`self.memory.set/get`）
- **能力限制**：不能访问网络/文件系统/子进程/LLM（安全沙箱隔离）

## 声明技能

`--skills chat` 让插件出现在 AI 对话页的"插件技能"面板。
可选值：`chat`（对话）/ `novel`（写作）/ `comic`（漫画）。

## 调试

设置 → 插件 → 你的插件旁的「试跑」按钮，输入 JSON 测试。
