# 附录B 漂移端点契约裁定（R2-B11 / F-09）

- 日期：2026-08-08
- 输入：round2-function-integrity.md §1.2（R2-B11 文档E 附录B 细粒度端点缺失合集）+ F-09 修复
- 裁定口径：已实现（本次）/ 保留缺口（理由）/ 文档废弃（有意省略）

## 一、/system/update 裁定

文档B §7.1.4「系统设置 /info, /hardware, /update」中的 **/system/update**：
RC 为免安装整体替换形态，在线更新端点**有意省略**；为消除契约 404 歧义，
本次在 `src/api/system.py` 新增 `GET /system/update`，如实返回
`{"supported": false, "channel": "manual-replace"}`——端点存在、语义诚实，
前端据此隐藏更新入口。裁定：**已实现（本次，语义化省略）**。

## 二、R2-B11 逐项裁定

| 端点 | 裁定 | 理由 / 位置 |
|---|---|---|
| chat /complete | 保留缺口 | 前端无消费方，待需求驱动 |
| chat /context（POST+DELETE） | 保留缺口 | 前端无消费方，待需求驱动 |
| chat /models | 保留缺口 | 前端无消费方，待需求驱动（/dialog/models 功能近义） |
| chat /usage | 保留缺口 | 前端无消费方，待需求驱动 |
| art /inpaint | 保留缺口 | 前端无消费方，待需求驱动（SAM 遮罩已具备前置能力） |
| art /ip-adapter | 保留缺口 | 前端无消费方，待需求驱动（IP-Adapter 权重未随包，见 F-14） |
| art /styles | 保留缺口 | 前端无消费方，待需求驱动 |
| art 任务取消 DELETE | 保留缺口 | 前端无消费方，待需求驱动 |
| art 历史删除 DELETE | 保留缺口 | 前端无消费方，待需求驱动 |
| art /segment | **已实现（本次）** | `src/api/vision_tools.py` POST /art/segment（F-02 SAM 接线） |
| art /depth | **已实现（本次）** | `src/api/vision_tools.py` POST /art/depth（F-03 MiDaS 接线） |
| art /detect | **已实现（本次）** | `src/api/vision_tools.py` POST /art/detect（F-04 YOLOv8 接线） |
| art /image-to-3d | **已实现（本次）** | `src/api/vision_tools.py` POST /art/image-to-3d（F-01 TripoSR 接线） |
| model /download、/download/{task_id} | 文档废弃 | **离线 RC 有意省略**：交付形态为模型随包/离线导入，在线下载与定位冲突（R2-B11 已认定合理） |
| model /config PUT | 保留缺口 | 前端无消费方，待需求驱动 |
| style /{id} DELETE | 保留缺口 | 前端无消费方，待需求驱动 |
| style /{id}/apply | 保留缺口 | 前端无消费方，待需求驱动 |
| style /{id}/metrics | 保留缺口 | 前端无消费方，待需求驱动 |
| style /{id}/merge | 保留缺口 | 前端无消费方，待需求驱动 |
| style /train/{id}/stop | 保留缺口 | 前端无消费方，待需求驱动 |
| system /metrics | 保留缺口 | 前端无消费方，待需求驱动（/hardware/realtime 提供实时遥测） |
| system /logs | 保留缺口 | 前端无消费方，待需求驱动 |
| system /restart | 保留缺口 | 前端无消费方，待需求驱动 |
| system /gpu | 保留缺口 | 前端无消费方，待需求驱动（/hardware/info 功能等价） |
| system /disk | 保留缺口 | 前端无消费方，待需求驱动（/system/diagnose 含磁盘探测） |
| system /config（GET/PUT） | 保留缺口 | 前端无消费方，待需求驱动（/system/settings 功能等价） |
| learn /knowledge/import/url | 保留缺口 | 前端无消费方，待需求驱动 |
| learn /lora/{id}/metrics | 保留缺口 | 前端无消费方，待需求驱动 |
| learn /lora/{id} DELETE | 保留缺口 | 前端无消费方，待需求驱动（/style/versions 回滚已覆盖版本管理主路径） |
| comic scene CRUD/reorder/render/export 系列 | 保留缺口 | 前端无消费方，待需求驱动（/director/panorama≈scene、/director/screenshot-4in1≈render，见 R2-B08） |

## 三、结论

- 已实现（本次）：5 项（/system/update 语义化省略 + art 视觉工具 4 端点）
- 文档废弃（有意省略）：1 项（model/download 系列，离线 RC 定位）
- 保留缺口：其余 24 项，统一理由"前端无消费方，待需求驱动"，
  前端出现真实消费方时按单项需求补齐，消除双文档漂移。
