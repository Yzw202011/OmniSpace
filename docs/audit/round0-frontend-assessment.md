# OmniSpace AI v2.3.1 前端零信任评估报告（Round 0）

- **评估日期**：2026-08-07
- **评估对象**：`e:\OmniSpace\frontend\`（约 1.3 万行 TS/TSX/CSS，React 19 + Vite 6 + Tailwind 4 + Zustand 5 + Three.js r170）
- **评估性质**：零信任评估 —— 不预设任何实现合规，一切结论以需求文档与代码实际交叉验证为准
- **评估人视角**：专家级前端 + UI/UX

---

## 1. 评估范围与方法

### 1.1 评估范围

**代码侧（全量通读）**：

| 目录/文件 | 内容 |
|---|---|
| `src/App.tsx` / `main.tsx` / `router.tsx` | 应用外壳、入口、Hash 路由（8 路由） |
| `src/components/common/`（7 个） | Button / Input / Modal / Progress / Slider / Toast / Tooltip |
| `src/components/dialog/`（4 个） | DialogPage / DialogView / MessageBubble / SessionList |
| `src/components/paint/`（4 个） | PaintPage / PaintView / PaintParams / ImageGrid |
| `src/components/manga/`（8 个） | MangaPage / StoryboardTable / DirectorStage / AssetPanel / CameraManager / PanoramaViewer / ScreenshotGrid / VoiceBinder |
| `src/components/learning/`（9 个）+ `learn/`（1 个） | LearningPage 及 8 个子组件 / LearnView |
| `src/components/model/` / `style/` / `help/` / `layout/` / `Home.tsx` / `Settings.tsx` | ModelManager / StylePage / HelpPage / RightPanel / BottomStatusBar / Home / Settings |
| `src/hooks/`（2 个） | useWebSocket / useHardwarePoll |
| `src/services/`（11 个） | api / ws / dialogApi / paintApi / mangaApi / modelApi / styleApi / systemApi / hardwareApi / learnApi / learningApi |
| `src/stores/`（10 个） | useAppStore / useDialogStore / usePaintStore / useMangaStore / useModelStore / useStyleStore / useHardwareStore / useTaskStore / useLearnStore / useLearningStore |
| `src/styles/`（4 个 CSS） | sakura.css（1261 行）/ sakura-theme.css（83 行）/ tokens.css（158 行）/ app.css（770 行） |
| `src/three/`（6 个） | SceneManager / MultiCameraRenderer / CubeCameraPanorama / SkeletonAnimator / GizmoController / RaycastSelector |
| `src/types/index.ts`（617 行）/ `src/utils/format.ts` | 类型契约 / 格式化工具 |
| `index.html` / `package.json` / `tsconfig.json` | 入口 HTML / 依赖清单 / TS 配置 |

**文档侧（对照依据）**：

| 文档 | 代号 | 角色 |
|---|---|---|
| 《布局视觉与规范（修正版D）》 | 文档 D | 布局视觉权威：五层布局、断点、CSS 变量体系、ApiResponse\<T\> |
| 《极致细粒度全量文档（修正版E）》 | 文档 E | 3514 行组件级规格（§6 前端专项） |
| 《中文版（修正版B）》 | 文档 B | 【可信任】权威文档（§6.1 布局、§6.3 设计系统、§9.1 接口） |
| 《编程语言规范（修改版C）》 | 文档 C | TypeScript 编码铁律 |
| 《v2.3.1（修正版A）》 | 文档 A | 开发计划（前端任务验收标准） |

### 1.2 评估方法

1. **静态全量通读**：上述代码文件逐一 Read，无抽样跳过。
2. **交叉比对**：七大维度（布局合规 / 视觉规范 / 模块完整性 / 类型与 API 契约 / TS 编码规范 / 历史残留冗余 / 可访问性交互）逐项与文档条款核对，记录文件:行号双端证据。
3. **死代码检测**：对每个组件/hooks/样式区块做全仓引用反查（import 链 + className 使用链）。
4. **虚假实现识别**：追踪"UI 操作 → store → service → 后端端点"全链路，凡链路中断且以本地状态伪造效果的，标记为"虚假实现"。
5. **文档冲突登记**：文档间条款互斥时（如文档 D 与文档 B/E 对顶栏、亮色主题的规定不一致），如实登记为"待裁决"，不擅自选边。

---

## 2. 总体结论

### 2.1 一句话结论

前端骨架与数据层质量**高于半成品预期**（服务层/状态层/WS 层全部真实对接后端，无 mock 数据、无 `any`、有诚实门控注释），但存在 **1 处整块虚假实现（训练任务）、1 项系统性视觉割裂（三大模块浅色硬编码 vs 暗色主题）、1 项 API 契约文档冲突、以及成规模的历史残留死代码（约 2000+ 行）**，距文档 D/B/E 的完整规格尚有明确差距。

### 2.2 各模块完成度评估

| 模块 | 完成度 | 说明 |
|---|---|---|
| 应用外壳（布局） | **70%** | 四层落地（侧栏/主区/右面板/状态栏），顶栏缺失（文档 D 要求）；侧栏尺寸 200/56 与三文档 260/64 不符；响应式仅实现 <768px 部分行为 |
| AI 对话 | **75%** | 会话 CRUD + WS 流式 + 引用/复制/重新生成真实可用；缺代码高亮、完整 Markdown、评分/收藏/导出/纠正/图片上传 UI（后端端点已在 dialogApi 封装但未接 UI） |
| AI 绘画 | **80%** | 文生图/图生图/历史画廊/参数面板真实可用（usePaintStore↔draw 端点）；LoRA/ControlNet/IP-Adapter 面板缺失（paintApi 诚实标注"后端暂无端点，UI 不展示"） |
| 漫剧创作 | **60%** | 分镜表（50 行上限/AI 切分）+ 3D 导演台（拖拽/旋转/截图）真实；但 5 个子组件（资产/机位/截图网格/音色/全景）零接线，音色绑定与视频四工序生成 UI 缺失，无 FPS 自证 |
| 知识学习 | **80%** | 8 个区块中 7 个真实（仪表盘/主题/浏览器/行为/知识库/图谱/导入/设置）；**训练任务区块（LearnView）为虚假实现，功能不可用** |
| 模型管理 | **85%** | 列表/分组/选择/校验/卸载真实；6 处原生 alert 破坏交互体系；VRAM 条真实取自硬件遥测 |
| 视频风格 | **80%** | 上传/参数/训练/进度/版本列表真实；版本回滚诚实降级（后端未就绪） |
| 设置 / 帮助 | **90%** | 设置读写后端真实；帮助静态完整但版本号 v2.3.0 过时、广告的快捷键未实现 |
| 首页 | **0%（未接入）** | Home.tsx 存在但无路由、无引用、无样式（`.home-*` 类在全部样式表中不存在），且内部导航卡仍用 v2.1 旧路由名 |

### 2.3 严重度分布总览

| 严重度 | 数量 | 含义 |
|---|---|---|
| **P0** | 5 | 阻断性：虚假实现 / 契约冲突 / 系统性视觉违规 / 权威布局层缺失 |
| **P1** | 18 | 重大：规格明确不符或功能缺失，影响验收 |
| **P2** | 20 | 一般：偏差、冗余、体验不一致 |
| **P3** | 3 | 提示：文档同步类 |
| **合计** | **46** | |

---

## 3. 问题清单

> 类别：L=布局合规 V=视觉规范 M=模块完整性 T=类型与API契约 C=TS编码规范 R=历史残留/冗余 A=可访问性/交互

| 编号 | 严重度 | 类别 | 文件:行号 | 问题描述 | 文档依据 | 修复建议 |
|---|---|---|---|---|---|---|
| FE-001 | P0 | L | `App.tsx:173-240`；`styles/app.css:5-6` | **顶层导航栏（48px）整层缺失**。文档 D 五层布局首层要求 48px 顶栏（Logo/全局搜索/通知中心/用户头像/主题切换），实现无顶栏且 app.css 头注明示"无顶部导航栏"。注意：文档 B §6.1.1/文档 E §6.1.1 的 ASCII 图亦无应用内顶栏（仅窗口标题栏）——**文档 D 与 B/E 冲突，需裁决** | 文档 D §1.1.1 | 裁决后二选一：补 48px 顶栏组件（含搜索/通知/主题切换），或在文档 D 中删除顶栏层并与 B/E 对齐 |
| FE-002 | P1 | L | `styles/tokens.css:142`；`App.tsx:176` | 侧栏展开宽 **200px ≠ 规格 260px**。代码注释引"§6.1.2"为依据，但文档 B §6.1.2、文档 E §6.1.2（含 ADR-07"最终确定"）、文档 D §1.1.3 三处均为 260px——引用失实 | 文档 B §6.1.2 / E §6.1.2+ADR-07 / D §1.1.3 | `--sidebar-width` 改 260px，同步 app.css §2 与注释 |
| FE-003 | P1 | L | `styles/tokens.css:143`；`App.tsx:158-159` | 侧栏收起宽 **56px ≠ 规格 64px**（同上，三文档一致 64px） | 同上 | `--sidebar-width-collapsed` 改 64px |
| FE-004 | P1 | L | `styles/app.css:719-757` | **Laptop 断点（1280-1439px）行为未实现**：文档 D 要求右侧面板折叠为 64px 图标态，实现仅收窄对话/绘画内栏（240px），右面板保持 320px 不变；且断点档（1536/1280/1024/768/640）与文档 D（1440/1280/1024/768）不一致 | 文档 D §1.1.2 | 按文档 D 断点档实现右面板 64px 图标折叠态（RightPanel 已有折叠机制，仅需断点驱动默认值） |
| FE-005 | P1 | L | `styles/app.css:738-757` | **Tablet 断点（768-1023px）行为未实现**：文档 D 要求侧栏默认 64px 图标态 + 右面板完全隐藏 + 浮动按钮唤出；实现右面板仅 <768px 隐藏，无浮动唤出按钮 | 文档 D §1.1.2 | 1023px 档隐藏右面板并新增浮动唤出按钮 |
| FE-006 | P1 | L | `styles/app.css:738-757` | **Mobile 断点（<768px）行为不符**：文档 D 要求侧栏转全屏覆盖层（Overlay）、状态栏隐藏并由底部导航栏替代；实现侧栏仅缩 56px 常驻、状态栏横向滚动、无底部导航 | 文档 D §1.1.2 | 实现侧栏 Overlay 模式 + 底部导航组件（或裁决桌面应用不做 Mobile 档并修订文档 D） |
| FE-007 | P2 | L | `App.tsx:225-227` | 主内容区**多标签页（Multi-Tab）未实现**：文档 D 要求"每个 Tab 对应一个独立路由或视图组件"，实现为单 `<Outlet />` | 文档 D §1.1.1 | 裁决是否需要多 Tab；若需要则加 TabBar 层 |
| FE-008 | P2 | L | `App.tsx:182-205` | 侧栏内容简陋：文档 D 要求树形菜单（Tree Menu）+ 空间切换器（Space Switcher）+ 底部收藏标记（Favorites），实现为扁平 8 项导航 + 折叠按钮 | 文档 D §1.1.1 | 裁决后补树形菜单/收藏位（B/E 均无此要求，疑为 D 过度规格） |
| FE-009 | P2 | L | `components/layout/RightPanel.tsx:57-110` | 右面板缺"操作历史记录"与"AI 对话侧边栏"内容（文档 D §1.1.1），仅实现上下文属性面板（与文档 E §6.1.3 一致） | 文档 D §1.1.1 vs E §6.1.3 | 以 E 为准则可关闭此项；以 D 为准则需补两个面板页签 |
| FE-010 | P0 | V | `dialog/DialogView.tsx:152,205,259`；`dialog/MessageBubble.tsx:63,103,136,146,153,161-166,174-194,206-226`；`dialog/SessionList.tsx`；`paint/PaintView.tsx:79`；`paint/PaintParams.tsx:132-376`；`paint/ImageGrid.tsx:116,128`；`manga/MangaPage.tsx:85-122` | **三大核心模块（对话/绘画/漫剧）共 94 处硬编码浅色 hex**（`#fdfbfb/#f0e6e9/#e2d4d7/#2d2527/#6e6064/#a69498/#c0668a/#f5edef` + `bg-white`），完全绕过 CSS 变量体系，在暗色 Sakura 外壳（`--color-bg:#1A1A2E`）内渲染白底页面，视觉割裂严重；tokens.css 头注明示"粉白浅色主题已废弃"，此批代码即为废弃主题残留 | 文档 D §1.1.3"全局样式必须通过 CSS Custom Properties 定义"；文档 B §6.3.1 Sakura 暗色；tokens.css:1-7 | 全量迁移至 tokens 令牌（`var(--color-card)/var(--color-text-primary)/...`），或裁决"内容区允许浅色"并文档化 + 令牌化（禁止裸 hex） |
| FE-011 | P1 | V | `dialog/MessageBubble.tsx:136,166` | AI 气泡底色 `#E3F2FD` 违反本仓令牌迁移决议：tokens.css:55-56 明示"浅色 #E3F2FD 在暗色底上刺眼，等效替换为薄荷绿低透明铺底（--color-ai-generated）"，MessageBubble 仍硬编码旧色且头注自引"约束 §14" | tokens.css:55-56；文档 B §14 | 改用 `var(--color-ai-generated)` / `bg-[var(--color-ai-bg)]` |
| FE-012 | P1 | V | `styles/tokens.css:11-21` | **CSS 变量命名体系与文档 D 不对齐**：`--color-bg`（D 为 `--color-bg-primary`）、`--color-card`（D 为 `--color-bg-secondary`，且 D 的 `--color-bg-card` 是另一值 #1E1E35）、`--color-accent`=#4ECDC4（D 的 `--color-accent`=#FFB3C6 浅樱粉，#4ECDC4 在 D 中名 `--color-secondary`）——同名异义/同义异名并存，跨文档协作必踩坑 | 文档 D §1.1.3 | 建立 tokens↔文档 D 变量映射表，裁决唯一命名（建议以本仓 tokens 为准反向修订文档 D，因实现已全链路自洽） |
| FE-013 | P1 | V | `styles/tokens.css:17-20` | 语义色值与文档 D 不符：success `#6BCB77`（D `#5BA87A`）、warning `#FFD93D`（D `#E8A84C`）、error `#FF6B6B`（D `#E05555`）、info `#4ECDC4`（D `#6BA5D6`）；实现与文档 B §6.3.1 一致 | 文档 D §1.1.3 vs 文档 B §6.3.1 | 文档 B/D 冲突登记；以 B 为准则修订 D 色值表 |
| FE-014 | P1 | V | `styles/tokens.css:5-6`；`components/Settings.tsx:76-84`；`main.tsx:12` | **亮色主题未实现**：文档 D §1.1.3 给出 `[data-theme="light"]` 全量覆盖变量、文档 E U-15 要求"深色默认，亮色可选切换"；实现 tokens.css 声明浅色废弃、无 data-theme 机制、设置页无切换器（"Sakura 唯一主题"） | 文档 D §1.1.3 / E U-15 vs 文档 B §6.3.1（唯一主题） | 文档冲突裁决：若遵 D/E 需补亮色令牌组+切换器；若遵 B 需修订 D/E |
| FE-015 | P2 | V | `styles/tokens.css:106` | 圆角 `--radius-sm: 6px` ≠ 文档 D `--border-radius-sm: 4px`（实现与文档 B §6.3.1 输入框 6px 一致） | 文档 D §1.1.3 vs B §6.3.1 | 文档冲突登记，建议以 B/实现为准 |
| FE-016 | P2 | V | `components/Settings.tsx:20-24`；`styles/tokens.css:69`；`styles/sakura.css:33-40` | 字号档位标注失实：Settings 标称 md=16px（默认），实际令牌 `--font-size-base: 14px`；sm 档 data-font-size 亦设 14px——sm 与 md 同值，档位失效 | 文档 B §6.3.1（正文 14px 基准） | 修正档位映射（sm 13/md 14/lg 16 或修订 Settings 文案为实际值） |
| FE-017 | P2 | V | `styles/tokens.css:87-103` | 间距量表命名与文档 D 不一致（`--space-1~16` vs `--spacing-xs/s/m/l/xl`），且粒度哲学不同（2px 步进 vs 语义档） | 文档 D §1.1.3 | 映射表文档化即可，不必改实现 |
| FE-018 | P3 | V | `styles/tokens.css:126-139` | 过渡变量命名与文档 D 不一致（`--duration-*` vs `--transition-fast/normal/slow`），量值体系亦不同 | 文档 D §1.1.3 | 文档化映射 |
| FE-019 | **P0** | M | `components/learn/LearnView.tsx:103-162`；`components/learning/LearningPage.tsx:40-44` | **训练任务区块为虚假实现（Fake UI）**：LearningPage 以零 props 渲染 `<LearnView />`，导致 ①`onCreateTask/onPauseTask/onResumeTask/onCancelTask` 全为 `undefined`，"开始训练"按钮提交后仅清空表单，后端零调用；②任务列表恒为空（`initialTasks=[]` 且无数据源）；③暂停/恢复/取消仅篡改本地 state 伪造状态流转；④`(LearnView as unknown as {...}).updateTasks = updateTasks` 向组件函数挂静态属性，无任何调用方，属反模式死挂钩；⑤校验反馈用原生 `alert`。而 `useLearnStore`（createTrain/cancelTask/reorderTasks→learnApi 真实端点）已存在却未接线 | 文档 A TASK-LEARN 系；文档 E §6.2.4；"诚实门控"仓约（RightPanel.tsx:12） | 立即接线：LearnView 改读 useLearnStore（tasks 列表/创建/暂停/取消全链路），删除静态属性挂钩与 alert；或整块下线 |
| FE-020 | P1 | M | `components/dialog/MessageBubble.tsx:53-124`；`package.json:12-19` | **代码高亮缺失 + Markdown 能力过窄**：文档 A TASK-CHAT-002 验收"流式显示，Markdown渲染，代码高亮"；实现为手写轻量解析，仅支持代码块/粗体/行内代码，代码块无语法高亮（依赖中无 highlight.js/prism/react-markdown），不支持标题/列表/链接/表格/嵌套 | 文档 A TASK-CHAT-002 | 引入 react-markdown + remark-gfm + highlight.js（离线包需入 pydeps 等效仓），或扩写解析器并接入高亮 |
| FE-021 | P1 | M | `services/dialogApi.ts:99-172`；`components/dialog/MessageBubble.tsx:198-232`；`DialogView.tsx` | 对话高级功能 UI 缺失：评分（赞/踩）、收藏、收藏夹、导出 Markdown/TXT、纠正入库、多模态图片上传——dialogApi 已全部封装（rateMessage/toggleFavorite/listFavorites/exportSession/submitCorrection/images 字段），UI 仅暴露复制/引用/重新生成 | 文档 E §6.2.1 组件树（消息操作含评分/收藏）；types/index.ts:254-260 | 在 MessageBubble 操作条补评分/收藏，SessionList 补收藏夹入口，输入区补图片上传与导出 |
| FE-022 | P1 | M/R | `components/manga/` 下 `AssetPanel.tsx`、`CameraManager.tsx`、`ScreenshotGrid.tsx`、`VoiceBinder.tsx`、`PanoramaViewer.tsx` | **漫剧 5 个子组件零接线（全仓无 import）**：MangaPage 仅使用 StoryboardTable + DirectorStage（MangaPage.tsx:14-15）；其中 ScreenshotGrid 的 4 合 1 截图展示、PanoramaViewer 的全景查看、CameraManager 的机位管理 UI 在页面上均不可达（能力仅内联在 DirectorStage/three 层），AssetPanel 资产管理与 VoiceBinder 音色绑定功能整体缺失。死代码约 1500+ 行 | 文档 E §6.2.3/§9.3；文档 A TASK-COMIC-002 | 二选一：接线进 MangaPage（导演台侧栏/页签），或删除归档；禁止继续闲置 |
| FE-023 | P1 | M | `components/manga/VoiceBinder.tsx`（死代码）；`services/mangaApi.ts:39-47` | 音色绑定功能缺失：VOI-001~008 端点已封装（/voices 列表/克隆/试听/绑定/情感标签），页面无入口 | 文档 E §6.2.3 / mangaApi 五节 | 接线 VoiceBinder 或在 StoryboardTable 行内补音色选择 |
| FE-024 | P1 | M | `services/mangaApi.ts:34-37`；`stores/useMangaStore.ts`（videoTasks）；`components/manga/MangaPage.tsx:82-144` | 视频生成四工序（text2img→img2video→tts→package）无发起 UI：`/manga/video/generate` 端点与 useMangaStore.videoTasks 存在且 RightPanel 只读展示任务，但 MangaPage 无"生成视频"入口/工序进度视图 | 文档 A TASK-COMIC 系；文档 E §6.2.3 | 分镜表行/导演台补生成入口与四工序进度条 |
| FE-025 | P1 | M/R | `components/Home.tsx:20-27`；`router.tsx:51-69` | **Home 为孤儿组件**：无路由注册、无 import；其导航卡使用 v2.1 旧路由名（dialog/manga/learn），点击将全部重定向 /chat；且 `.home-*` 全部样式类在 4 个样式表中不存在——即便挂载也是无样式裸奔。属 v2.1 残留 | 文档 B/E §6.1.2（导航无首页，实现与之相符）；评估任务要求核查"首页是否有" | 删除 Home.tsx，或裁决恢复首页则重写路由名与样式 |
| FE-026 | P2 | M | `services/paintApi.ts:12`；`components/paint/PaintParams.tsx` | 绘画 LoRA 选择/ControlNet/IP-Adapter 面板缺失（文档 E §6.2.2 参数面板规格）；paintApi 头注诚实标注"后端暂无 LoRA 列表/图片上传/收藏端点，相关 UI 不展示"——实现选择诚实降级，与文档规格差距属实 | 文档 E §6.2.2 | 后端端点就绪后补 UI；当前保持关闭，不算虚假实现 |
| FE-027 | P2 | M | `components/paint/PaintView.tsx:79` | 绘画页布局与文档 E §6.2.2"预览区 60% + 参数区 40%"不符：实现为左侧固定 w-80（320px）参数栏 + 主结果区，比例随窗口浮动而非 6:4 | 文档 E §6.2.2 | 裁决比例约束是否强制；若是改 grid 6:4 |
| FE-028 | P2 | M/R | `components/learn/` vs `components/learning/`；`services/learnApi.ts` vs `learningApi.ts`；`stores/useLearnStore.ts` vs `useLearningStore.ts` | 学习域**双体系并存**：v2.1 learn 三件套与新 learning 九组件并立，注释自承"零回归嵌入"；训练能力实际断链（见 FE-019），双 store/API 增加维护与理解成本 | 文档 A 任务演进 | 合并为单一 learning 域：LearnView 功能迁入 useLearnStore 驱动的新组件，旧文件归档 |
| FE-029 | P2 | M/A | `components/model/ModelManager.tsx:89,95,103,114,120,134` | 模型管理 6 处原生 `alert()` 反馈（未就绪/不支持/失败），绕过既有 Toast 体系，阻断式弹窗与全局交互语言不一致 | 文档 B §6.3 组件体系；仓内 Toast 惯例 | 一律改 `useAppStore.showToast` |
| FE-030 | P2 | M | `components/manga/DirectorStage.tsx`；`three/SceneManager.ts:201-219` | 3D 导演台无 FPS 监控/性能自证：文档 A TASK-COMIC-002 验收"Three.js>30fps"，代码无帧率统计（全仓 grep 无 fps 计数器） | 文档 A TASK-COMIC-002 | SceneManager 加可选 stats 回调（delta 滑动平均），导演台角落显示 FPS |
| FE-031 | **P0** | T | `types/index.ts:12-23`；`services/api.ts:169-190` | **ApiResponse 信封与文档 D/B 全面冲突**：实现为 `{code:number, message, data, request_id?, detail?}`（code===0 成功）；文档 D §4.1.1 与文档 B §9.1.1 均为 `{success:boolean, data, error:{code:string,message,detail,suggestion?}|null, meta:{request_id,timestamp,duration_ms}}`。前端全链路（api.ts 解包、全部 store）按数字信封实现，与在跑后端一致，但与两份权威文档均不符——**必须裁决唯一契约** | 文档 D §4.2.5/§4.1.1；文档 B §9.1.1 | 裁决：①以实现为准 → 修订文档 B/D §9.1.1/§4.1.1 为 `{code,message,data,request_id?}`；②以文档为准 → api.ts 增加适配层并在 meta 落地 duration_ms。无论何选，文档与代码必须收敛 |
| FE-032 | P0 | T | `services/api.ts:9-10`；`types/index.ts:26-35` | **错误码体系冲突**：实现为数字业务码（40001 VRAM不足/40002 模型未就绪/40004/40005/40009/-1/-2/-3）；文档 D 与文档 B §9.1.2 均为字符串语义码（MODEL_NOT_LOADED/VRAM_INSUFFICIENT...，按模块 1xxx-6xxx 分类），且文档 B 要求"前端使用 Zod 枚举校验" | 文档 D 错误码节；文档 B §9.1.2 | 与 FE-031 一并裁决；若保留数字码需修订两份文档的错误码表 |
| FE-033 | P1 | T/C | `package.json:12-30`；全部 services | **Zod 未引入**：文档 B §9.1.2"前端使用 Zod 枚举校验（错误码）"、文档 C 铁律示例要求 `Schema.parse(response)` 运行时校验；依赖清单无 zod，全部 API 响应为纯 TS 断言（`as ApiResponse<T>`），无运行时防护 | 文档 B §9.1.2；文档 C TS 规范 | 引入 zod（离线依赖需同步入仓），对 WS 消息与关键 REST 响应做 parse；或裁决降级为手写 type-guard 并修订文档 |
| FE-034 | P2 | T | `services/api.ts:16` | API 基地址与文档 B §9.1 不符：实现 `http://127.0.0.1:5800/v1`，文档 `http://localhost:8000/api/v1`（前缀亦不同 `/v1` vs `/api/v1`）；实现与在跑后端一致，文档过时 | 文档 B §9.1 | 修订文档 B |
| FE-035 | P2 | T | `types/index.ts:281-287` | 分页契约 `Paginated<T>` 的 `page/page_size` 为可选，文档 D 分页响应格式中二者必含；宽松定义掩盖后端缺字段 | 文档 D §4.2.5 分页响应 | 与后端核对后改必填或修订文档 |
| FE-036 | P2 | T | `services/ws.ts:132-143` | WS 消息未按文档 B §9.1.3 `{type:progress\|log\|status\|notification, module, data}` 收敛：实现分发任意 type、无 module 字段、无订阅过滤 | 文档 B §9.1.3 | 裁决文档或加 module 路由层 |
| FE-037 | P2 | C | `three/SceneManager.ts:186,266`；`learn/LearnView.tsx:162`；`types/index.ts`（`scenes?: unknown[]` 等） | `as unknown as {...}` 双重断言 3 处（向 this/组件函数塞私有属性），属文档 C 不鼓励模式；无 `any`（全仓 grep 零命中）值得肯定 | 文档 C TS 铁律 | SceneManager 改正式私有字段；LearnView 随 FE-019 重写消除 |
| FE-038 | P2 | C | `tsconfig.json:15-16` | `noUnusedLocals/noUnusedParameters` 关闭；若开启可即刻暴露本报告全部死代码（FE-022/025/041/042） | 文档 C 严格模式导向 | 开启二项并清理报错 |
| FE-039 | P2 | C | `three/MultiCameraRenderer.ts:49,133`；`manga/ScreenshotGrid.tsx:53` | `getContext('2d')!` 非空断言若于异常环境返回 null 将运行期崩溃；建议判空抛中文错 | 文档 C 错误处理导向 | 判空 + throw |
| FE-040 | P3 | C | `services/api.ts:2`（v2.1）；`stores/useStyleStore.ts:2`（v2.3）；`App.tsx:2`（v2.3.1） | 文件头版本标注三代并存，仓级版本叙事不一致 | 文档 A 版本 v2.3.1 | 统一头注版本或移除版本号 |
| FE-041 | P1 | R | `styles/app.css:536-660`（chat/image 页样式区块）+ `722-757`（其响应式规则） | **约 200 行死 CSS**：`.chat-layout/.chat-sidebar/.chat-main/.msg-row/.msg-body/.image-layout/.image-history/.history-grid/.upload-zone/.gen-result-img` 等类无任何 TSX 引用（对话/绘画页实际用 Tailwind 工具类另写了一套）——即同一页面存在"暗色 CSS 类版"与"浅色 Tailwind 版"两套实现，CSS 版整体废弃 | 零信任冗余检测 | 删除死区块（含其媒体查询分支），或反向启用并删除 Tailwind 浅色版（推荐前者，配合 FE-010 重做） |
| FE-042 | P1 | R | `hooks/useWebSocket.ts`（全 140+ 行）；`hooks/useHardwarePoll.ts`（全 140+ 行） | 两个 hooks 零引用：WS 能力已被 `services/ws.ts`（WsConnection 池）取代，硬件轮询已在 `useHardwareStore` 内联——纯历史残留 | 零信任冗余检测 | 删除或归档 |
| FE-043 | P2 | R | `App.tsx:107-121`；`router.tsx:41-48` | PageStub"该页面组件尚未合入"文案已过时（8 页面全部接入），现仅充当 Suspense 加载兜底；保留无害但文案误导 | 仓内一致性 | 改文案为纯加载态或内联 Spinner |
| FE-044 | P2 | R | `styles/sakura.css:22-30`；`styles/tokens.css:58-60` | 双层兼容别名并存：sakura.css 定义 7 个旧名别名（--color-text/--color-primary-dark/--radius/--blur 等），tokens.css 另定义 surface 系别名；别名层叠增加令牌溯源成本 | 令牌单一来源原则（tokens.css 自称"唯一权威来源"） | 收敛别名：引用点改新名后删除别名 |
| FE-045 | P3 | R | `components/help/HelpPage.tsx:170` | 帮助页关于卡版本号 **v2.3.0** ≠ package.json **v2.3.1** | 文档 A 版本 | 改读 `/system/version` 或常量统一 |
| FE-046 | P2 | A | `components/help/HelpPage.tsx:31-38` | **帮助页广告的快捷键多数未实现**：Ctrl+N（新建会话）、Ctrl+K（知识搜索聚焦）、Ctrl+S（保存设置/分镜表）、F5（刷新硬件）均无全局监听；仅 Ctrl+Enter（DialogView textarea Enter 发送）与 Esc（Modal 关闭）真实存在——对用户构成功能承诺落空 | 文档 E 快捷键节；HelpPage 自述 | 实现全局快捷键层（keydown 总线 + 路由级分发），或删除帮助页未实现条目 |
| （并） | P2 | A | `learn/LearnView.tsx:122,126`；`manga/StoryboardTable.tsx:145,314`；`learning/TopicManager.tsx:96`；`learning/KnowledgeBrowser.tsx:136`；死代码 `manga/CameraManager.tsx:136-176`、`manga/VoiceBinder.tsx:153,174` | 原生 alert/confirm 共 17 处（含 FE-029 的 6 处），全部应迁 Toast/Modal 体系 | 文档 B §6.3 | 统一替换 |
| （并） | P2 | A | `components/layout/BottomStatusBar.tsx:177` | 状态栏 AV1 字段恒为 `--`（后端无遥测的诚实占位，有 title 说明）——合规但见 FE 报告外提示：文档 B §6.1.4 状态栏十字段含"AV1:?"，若长期无数据建议移除该字段 | 文档 B §6.1.4 | 后端支持前隐藏字段 |
| （并） | P2 | A | `components/layout/BottomStatusBar.tsx` | 状态栏无**当前版本号**与显式 **API 连接状态**字段（文档 D §1.1.1 要求含版本号 v2.3.1；WS 灯可视为连接态但不等价 API 健康） | 文档 D §1.1.1 | 裁决后补版本号字段 |

> 注：末 3 行"（并）"为合并呈报项，计数分别并入 FE-029（alert 类）、A 类提示；全表按 46 项计。

---

## 4. 样式体系冗余/冲突专项分析

### 4.1 文件链路与定位（健康部分）

```
main.tsx → sakura.css（编排入口）
              ├─ @import 'tailwindcss'
              ├─ @import './tokens.css'        （:root 令牌：颜色/字阶/间距/圆角/阴影/动效/尺寸/z-index）
              ├─ @import './sakura-theme.css'  （@theme 注册 sakura-*/mint-* 工具色阶 + 滑块/keyframes）
              └─ @import './app.css'           （外壳布局 + 通用组件类 + 响应式）
```

四个文件**均在用**，无死文件；级联顺序正确；sakura-theme.css 的 sakura-500 `#ff6b9d` 与 tokens.css `--color-primary-500` 取值一致（双写但同步，属 Tailwind 4 @theme 机制的必要重复）。此部分架构合理。

### 4.2 冗余点清单

| # | 冗余/冲突 | 位置 | 量级 | 处置建议 |
|---|---|---|---|---|
| S-1 | **死 CSS 区块**：chat/image 页整套暗色类实现无调用方（对应 TSX 用 Tailwind 浅色类另写一套） | app.css:536-660 + 722-757 响应式分支 | ≈200 行 | 删除（与 FE-010/FE-041 联动） |
| S-2 | **双层别名**：sakura.css :root 别名 7 个 + tokens.css surface 别名 2 个 | sakura.css:22-30；tokens.css:58-60 | 9 条 | 引用点改新名后清零 |
| S-3 | **色阶双写**：tokens `--color-primary-50~700` 与 @theme `--color-sakura-50~700` 同值并存 | tokens.css:28-40 vs sakura-theme.css:13-27 | 14 条 | 机制必要，但需加"值必须同步"注释守卫或构建期断言 |
| S-4 | **三轨并行的样式范式**：①app.css 语义类（外壳/学习/模型页）②Tailwind 工具类+任意值（对话/绘画/漫剧页）③内联 `style={{...var(--x)}}`（learning 9 组件普遍） | 全仓 | 范式级 | 约定主范式（建议 Tailwind + 令牌语义类），内联 var() 仅限动态值 |
| S-5 | **命名体系与文档 D 系统性错位**（详见 FE-012/013/015/017/018） | tokens.css 全文 | 体系级 | 裁决唯一命名源 |

### 4.3 冲突点：暗色令牌 vs 浅色实现（最严重影响面）

tokens.css 已全量暗色化并明示"粉白浅色主题已废弃"，但对话/绘画/漫剧三模块的 TSX 层仍整体停留在废弃浅色主题（94 处裸 hex + `bg-white`），且这些裸 hex **不在任何令牌中存在**（`#fdfbfb/#f0e6e9/#2d2527/...` 全部查无令牌对应）——意味着这批页面既不是暗色体系、也不再是可切换的浅色体系，而是**第三套野生色板**。这是当前视觉层面唯一达到 P0 的系统性问题，其根因与 FE-041 互为表里：仓内曾存在暗色 CSS 类版对话/绘画页（.chat-*/.image-*），后被浅色 Tailwind 版替代但旧 CSS 未删、新版未令牌化，形成"双尸同棺"。

### 4.4 结论

样式文件层面无死文件、级联正确；问题集中在**三个断层**：令牌命名与文档 D 断层（S-5）、TSX 实现与令牌断层（§4.3）、历史 CSS 与现行实现断层（S-1）。修复顺序建议：先删死 CSS（S-1）→ 三模块令牌化重做（§4.3）→ 别名清零（S-2）→ 命名映射文档化（S-5）。

---

## 5. 已验证合规部分（简要）

以下为经零信任核查**确认真实且合规**的部分，供后续轮次免查：

1. **数据链路真实**：11 个 services 全部对齐后端实际路由，头注如实标注已移除的悬空端点（modelApi:14-17、systemApi:14-16）；全仓 grep 无 mock/TODO/假数据标记。
2. **WS 层工程质量高**：`services/ws.ts` 连接池单例、指数退避重连（上限 30s）、待发送队列、状态机完备；对话流式（`ws/v1/dialog/stream/{id}` token/meta/error/done 事件）与硬件遥测双通道真实。
3. **状态管理分层清晰**：10 个 Zustand store 职责单一，UI 组件纯度良好，无 store 内嵌 JSX、无组件内裸 fetch。
4. **TS 严格度**：`strict: true`；**全仓零 `any`**（grep `\bany\b` 无命中）；types/index.ts 617 行集中定义跨模块契约。
5. **诚实门控文化**：RightPanel 头注"绝不伪造数据"且践行（无数据显示 `--`）；BottomStatusBar AV1 字段显式注明后端未提供；paintApi 对后端缺失端点选择不展示 UI 而非伪造。
6. **可访问性基础**：`aria-label/aria-expanded/aria-live/role=progressbar` 在导航/折叠钮/Toast/进度条正确出现；`:focus-visible` 全局光环；`prefers-reduced-motion` 降级媒体查询；Modal Esc 关闭。
7. **Three.js 层扎实**：SceneManager 完整生命周期（ResizeObserver/dispose/材质遍历释放）、WebGL2 强制 + 中文报错、4 合 1 截图渲染器、CubeCamera 720° 全景、15 种骨骼姿态预设、Gizmo 拖拽/旋转真实可用。
8. **外壳关键尺寸**：右面板 320px、状态栏 28px 与三文档一致；Hash 路由离线可用；侧栏折叠态 localStorage 持久化 + 收起 tooltip 符合 §6.1.2 悬停行。
9. **错误边界**：ErrorBoundary 包裹全树，渲染异常不白屏（COM-002）。
10. **全中文界面**：UI 文案、报错、空态全部中文（COM-016）。

---

## 6. 重构优先级建议

### 第一波：止血（P0，1-2 天）

1. **FE-019 训练任务接线**：LearnView 接通 useLearnStore（或整块下线）——当前页面存在功能性欺骗，最优先。
2. **FE-031/032 API 契约裁决**：召开文档-实现对齐裁决（建议：以在跑后端的 `{code,message,data}` 数字码为准，反向修订文档 B §9.1 与文档 D §4.x；同时删除文档中 Zod 强制条款或补依赖，二选一不要悬空）。
3. **FE-010/011 三模块主题令牌化**：对话/绘画/漫剧 94 处裸 hex 全量替换为 tokens 令牌；同步删除 app.css 死区块（FE-041），一次手术根治"双尸同棺"。
4. **FE-001 顶栏裁决**：产品层面决定遵 D（补 48px 顶栏）还是修 D（同 B/E 无顶栏）。

### 第二波：规格对齐（P1，3-5 天）

5. 侧栏 260/64px 尺寸修正（FE-002/003，令牌改两行 + 回归视觉）。
6. 响应式断点行为补齐或文档降级（FE-004/005/006；桌面应用建议明确"不做 Mobile 档"并修订文档 D）。
7. 漫剧 5 死组件接线或删除（FE-022），音色绑定与视频生成入口补齐（FE-023/024）。
8. 对话代码高亮 + 高级功能 UI（FE-020/021）。
9. 死代码清零：Home.tsx、2 个 hooks、死 CSS（FE-025/041/042），随后开启 `noUnusedLocals/noUnusedParameters` 防回潮（FE-038）。
10. 亮色主题/文档冲突裁决（FE-014）。

### 第三波：体验与工程硬化（P2，2-3 天）

11. 17 处 alert/confirm → Toast/Modal（FE-029 等）。
12. 全局快捷键层兑现帮助页承诺（FE-046），否则删减文案。
13. Zod 或 type-guard 运行时校验落地（FE-033，依第一波裁决）。
14. FPS 自证（FE-030）、字号档位修正（FE-016）、版本号统一（FE-045）、状态栏字段裁决。
15. 样式范式收敛与别名清零（S-2/S-4）。

### 裁决事项汇总（需产品/架构组拍板，前端不擅自选边）

| # | 事项 | 冲突双方 |
|---|---|---|
| 1 | 顶栏存废 | 文档 D（要 48px 顶栏）vs 文档 B/E + 实现（无顶栏） |
| 2 | API 信封与错误码 | 文档 B/D（success/error 信封 + 字符串码）vs 实现 + 在跑后端（code 信封 + 数字码） |
| 3 | 亮色主题 | 文档 D/E（可选切换）vs 文档 B + tokens.css（唯一暗色主题） |
| 4 | 内容区主题 | 三模块浅色现状 vs 暗色 Sakura 令牌 |
| 5 | Mobile 断点 | 文档 D（覆盖层 + 底部导航）vs 桌面应用实际形态 |
| 6 | Zod | 文档 B/C 强制 vs 零依赖离线仓现状 |

---

*报告完。评估基于 2026-08-07 代码快照；全部问题均可按"文件:行号"现场复核。*
