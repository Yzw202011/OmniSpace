# tests/ —— E2E 流程编排脚本（定位声明）

> TASK-P2-09（审计 P24）裁定：本目录**不是** pytest 测试目录，是「需要活后端」的实机/流程编排脚本集合。
> pytest 的家在 `tests/unit/`（`pytest.ini` 的 `testpaths=tests/unit` 即此收口，本目录脚本因命名（无 `test_` 前缀）与依赖（活后端/GPU/Playwright）设计为不被 pytest 收集）。

## 三层测试体系与唯一入口

| 层 | 位置 | 依赖 | 触发方式 |
| --- | --- | --- | --- |
| L1 后端 pytest | `tests/unit/` | 离线（不依赖活后端） | `runtime\py310\python.exe tools\run_tests.py`（默认含） |
| L2 前端 vitest | `frontend/src/**/*.test.ts` | node 环境 | 同上（默认含） |
| L3 E2E 流程编排 | 本目录 | **活后端** 127.0.0.1:5800（含真实模型推理） | 同上加 `--e2e` |

统一入口：`tools/run_tests.py`（`--smoke` 仅提交前最小集；`--e2e` 追加实机层）。
本目录任何脚本都**不**进提交钩子（`.githooks/` 只跑 `pytest -m smoke`）。

## 目录清单

### flow/ —— 流程编排主套件（L3 正主）

九大业务域端到端用例（sys/chat/paint/comic/learn/model/style/set/cross），HTTP 直打运行中后端，结果写 `flow/results/*.json` 并汇总 `summary.json`。

```bash
runtime\py310\python.exe -m tests.flow.run all        # 全部 9 模块
runtime\py310\python.exe -m tests.flow.run chat,comic # 按模块选跑
```

- `harness.py` —— Client/用例注册/结果落盘基础设施
- `run.py` —— 执行入口（含 `/health` 预检）
- `report.py` —— 结果报告生成
- `cases_*.py` —— 各域用例定义
- `results/` —— 历史运行产物（入库作为回归证据快照）

### 实机/严格复测脚本（发版前手动跑）

| 脚本 | 内容 |
| --- | --- |
| `e2e_realmachine.py` | 真机集成测试（TC-I-001~010 + 性能/安全抽样，真实模型推理级，输出 `e2e_results.json`） |
| `strict_test.py` | 极严格复测：并发压力/边界输入/协议鲁棒/注入安全/限流验证（输出 `strict_results.json`） |

### 历史 UI 冒烟批次（Playwright，需活后端）

`ui_batch_chat.py` / `ui_batch_comic_a.py` / `ui_batch_comic_b.py` / `ui_batch_sys.py` —— 对话/漫剧/系统页 UI 用例；`ui_*_retest.py` / `ui_fix_verify.py` —— 缺陷修复定点回归。结果落 `ui_results/`。

### 历史 HTTP 冒烟批次（已被 tests/unit pytest 冒烟取代，保留为对照）

`smoke_batch1~6.py` —— 直打 5800 端口的分批冒烟（P0-03 之前的形态；现行冒烟真源为 `tests/unit/test_api_smoke.py`，勿在此续写新用例）。

### 工具与调试探针（一次性）

`_cleanup_smoke.py`（清理冒烟残留项目）、`_dump_routes.py`（路由清单导出）、`_probe_*.py` / `_tsr_selftest.py` / `flow/_probe_*.py` / `flow/_triage.py`（问题复现与定位探针，无断言语义，不构成测试资产）。

## 新用例往哪写？

- **离线可判定的逻辑**（解析/校验/状态机/纯函数）→ `tests/unit/test_*.py`（pytest，打标记）
- **前端逻辑** → `frontend/src/**/__tests__` 同级 `.test.ts`（vitest）
- **跨端点真实链路**（必须活后端）→ `tests/flow/cases_<域>.py`（注册进 harness）
- 禁止再新增根目录散装 `*_batch.py` / `_probe_*.py` 风格脚本——探针用完即删，勿入库。
