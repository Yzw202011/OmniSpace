# 前端错误呈现三分法（错误处理策略）

> 版本 v1.0 ｜ 制定于 2026-08-20（TASK-P2-08，对应审计 P16）｜ 代码载体：`frontend/src/utils/errors.ts` ｜ 防回归：`frontend/src/utils/errors.test.ts`
>
> 配套文档：[架构总览](architecture-overview.md)

## 1. 背景与问题

审计 P16 指出：前端错误处理口径混乱，同一个「失败」有的弹 toast、有的只写 console、有的彻底静默吞掉。典型事故是保存失败被 `.catch(() => undefined)` 吞掉——用户以为已保存，刷新页面后内容丢失，且无任何线索可排查。

本策略把错误呈现收敛为**三个等级 + 一个白名单**，并把「提取错误消息」收敛为全站唯一定义，消除 10 处各写一份的 `getErrMessage/errMsg` 副本。

## 2. 三分法

| 等级 | 判定标准 | 呈现方式 | 代码入口 |
|------|---------|---------|---------|
| **TOAST** | 用户主动发起的写操作失败：保存、生成、导入、上传、提交、取消、删除等。失败意味着用户的意图落空或数据可能丢失 | 右下角 toast（error 级），文案 = `err.message`（无则动作名兜底） | `reportActionError(err, '动作名')` |
| **CONSOLE** | 后台刷新/缓存回写失败：预取关键帧、行序回滚重拉、资产并行拉取、批量结果回写本地 store 等。主流程已有结果或已有兜底，失败只是「暂时不同步」 | `console.warn`，不打扰用户，留排查线索 | `reportBgError('作用域', err)` |
| **SILENT** | 仅限第 4 节白名单 | 无 | 无（结构性静默） |

判定口诀：**「用户刚点了什么」失败 → TOAST；「系统自己在补数据」失败 → CONSOLE；其余一律不允许静默。**

### 2.1 边界示例

- AI 生词成功但描述词落库失败 → **TOAST**。虽然主操作成功了，但落库失败会让紧随其后的「已生成」toast 变成误导，属于数据丢失风险（`InspectorPanel.handleDescribe`）。
- 拖拽重排失败后自动回滚重拉行序，重拉本身又失败 → **CONSOLE**。重排失败本身已由调用方 TOAST，重拉只是兜底同步（`rowsSlice.reorderRows`）。
- 视频行状态本地变更后的全量持久化失败 → **CONSOLE**。下次手动保存会收敛，不阻塞主流程（`videoSlice.syncRowGenerationStatus`）。

## 3. 代码载体（@/utils/errors）

```ts
getErrorMessage(err, fallback)  // 错误对象 → 用户可读文案（全站唯一定义）
reportActionError(err, action)  // 入口①：TOAST 级
reportBgError(scope, err)       // 入口②：CONSOLE 级
isBenignError(err)              // 白名单判定：AbortError 类主动取消
```

约定：

- 组件/store 内**禁止**再写 `.catch(() => undefined)`、空 catch 块，以及本地 `getErrMessage/errMsg` 副本；
- 每个 catch 必须落到两个入口之一，或显式注释白名单理由；
- `reportBgError` 的 scope 用 `文件名.函数名` 命名（如 `StoryboardTable.prefetchKeyframes`），保证 console 里可直接定位。

## 4. 静默白名单（仅此四种）

1. **localStorage 隐私模式写失败**——主题/配置持久化，浏览器层面拒绝写入且无恢复手段（`useAppStore.applyTheme`）；
2. **AbortError 类主动取消**——用户或竞态守护主动 abort，不是故障（`isBenignError`）；
3. **竞态守护丢弃过期响应**——`alive` 标记类：组件已卸载/弹窗已关闭，迟到响应按设计丢弃；
4. **下层已 toast 呈现后的控制流收敛**——store 层失败已 TOAST，上层 catch 只为把 rejection 转成返回值（如 `VideoConfirmModal` 的 `.catch(() => false)`），**必须带注释说明**，防止二次呈现或无声扩散。

## 5. 本次收敛清单（TASK-P2-08）

| 位置 | 原状 | 现状 |
|------|------|------|
| MangaWorkspace 资产预取 / 推理后刷新 | 吞掉 | CONSOLE |
| MangaWorkspace 导演台进度保存 | 吞掉 | TOAST |
| MangaWorkspace 取消/推理失败 | 内联消息提取 | 统一 `getErrorMessage` + TOAST |
| rowsSlice 重排回滚重拉 | 吞掉 | CONSOLE |
| projectSlice 资产并行拉取 | 吞掉（还置 assetsLoaded=true） | CONSOLE + 置位（保留原兜底语义） |
| videoSlice 行状态持久化 | 吞掉 | CONSOLE |
| InspectorPanel 关键帧预取 | 吞掉 | CONSOLE |
| InspectorPanel 描述词/情绪标签落库 | 吞掉 | TOAST |
| StoryboardTable 关键帧预取 | 吞掉 | CONSOLE |
| ImportDrawer / ScriptImport DSL 导入后刷新 | 吞掉 | CONSOLE |
| batchOps 批量回写 ×3 | 吞掉 | CONSOLE |
| VideoConfirmModal 逐行提交 | 无注释静默转 false | 白名单第 4 条 + 注释 |
| 10 处 `getErrMessage/errMsg` 本地副本 | 各自为政 | 统一 `@/utils/errors` |

注：StoryboardTable 的防抖全量保存（`persist`）在 P2-01 拆分时已带「分镜保存失败」TOAST + 顶栏保存状态点，本次核验无回归。

## 6. 防回归

`frontend/src/utils/errors.test.ts` 静态扫描 `src/` 全部 `.ts/.tsx`（排除测试自身）：

- 禁止出现 `.catch(() => undefined/null/void 0/{})` 与空 catch 块；
- 禁止再定义 `function getErrMessage` / `const errMsg =`（真源唯一）；
- 单测覆盖 `getErrorMessage` 提取语义、`isBenignError` 判定与 `reportBgError` 输出格式。

新增代码若违反本策略，`npm run test` 直接失败。

## 7. 决策流程（新增错误处理时）

```
catch 到错误
  ├─ 用户刚才主动发起的动作？ ──是──> reportActionError(err, '动作名')
  ├─ 系统后台补数据/回写？   ──是──> reportBgError('作用域', err)
  ├─ 命中第 4 节白名单四条之一？──是──> 静默 + 注释理由
  └─ 都不是 ──────────────────────> 按最靠近的等级从严处理（宁 TOAST 勿静默）
```
