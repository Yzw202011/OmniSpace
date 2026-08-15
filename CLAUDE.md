# OmniSpace AI v2.3.1 — AI开发记忆文件

> **此文件为AI编程助手的强制上下文。每次开发会话开始时必须完整读取。**
> **任何与此文件冲突的技术决策均无效。此文件 = 开发铁律。**
> **最后更新：2026-08-12 | 前端框架：React 19.2（无约束选型最终裁定）**

---

## 0. 会话启动检查清单

每次开始编码前，AI必须确认以下全部为YES：

- [ ] 我已读取此文件全部内容
- [ ] 我知道前端用React 19.2，不是Vue3，不是Svelte，不是SolidJS
- [ ] 我知道状态管理用Zustand，不是Pinia，不是Redux
- [ ] 我知道包管理用pnpm，不是npm，不是yarn
- [ ] 我知道3D渲染用React Three Fiber，不是原生Three.js
- [ ] 我知道Python包管理用uv，不是pip
- [ ] 我知道后端用FastAPI + Pydantic v2，不是Django/Flask
- [ ] 我知道桌面壳层用Tauri 2.x (Rust)，不是Electron
- [ ] 我知道数据库用SQLite + ChromaDB，不是PostgreSQL/MongoDB
- [ ] 我知道所有CSS颜色必须用var(--color-*)，禁止硬编码

**如有任何一项为NO，立即停止编码并重新读取此文件。**

---

## 1. 技术栈锁定表（LOCKED — 禁止变更）

| 层级 | 锁定技术 | 禁止使用 | 锁定方式 |
|------|---------|---------|---------|
| 前端框架 | **React 19.2** (Function Component + Hooks + Compiler 1.0) | Vue3, Svelte, SolidJS, Angular | package.json |
| 编译优化 | **React Compiler 1.0** | 手动useMemo/useCallback（99%场景不需要） | babel.config.js |
| 前端包管理 | **pnpm 9+** | npm, yarn | pnpm-lock.yaml |
| 状态管理 | **Zustand 5** (slice模式) | Pinia, Redux, MobX, Recoil | package.json |
| 前端路由 | **React Router 7** | Vue Router, TanStack Router | package.json |
| 虚拟列表 | **@tanstack/react-virtual** | vue-virtual, react-window | package.json |
| 3D渲染 | **React Three Fiber + @react-three/drei** | 原生Three.js, TresJS, Babylon.js | package.json |
| 组件库基座 | **shadcn/ui (Base UI)** | Element Plus, Ant Design, MUI | components/ui/ |
| 拖拽交互 | **dnd-kit** | react-dnd, vue-draggable | package.json |
| AI流式 | **Vercel AI SDK** (useChat/useCompletion) | 手写SSE解析 | package.json |
| 错误边界 | **React Error Boundaries + Suspense** | Vue errorHandler | - |
| 类型系统 | **TypeScript 5.x** (strict: true) | JavaScript, CoffeeScript | tsconfig.json |
| 运行时校验 | **Zod 4.x** | io-ts, yup, joi | package.json |
| CSS框架 | **Tailwind CSS 4** | Styled Components, CSS-in-JS | - |
| 图标库 | **lucide-react** | lucide-vue-next, heroicons, font-awesome | - |
| 图表库 | **ECharts** (echarts-for-react) | Chart.js, D3(仅知识图谱) | - |
| 构建工具 | **Vite 6+** | Webpack, Rollup, esbuild直接使用 | vite.config.ts |
| 后端语言 | **Python 3.12** | Node.js, Go, Java | - |
| 后端框架 | **FastAPI** | Django, Flask, Tornado | - |
| Python包管理 | **uv** | pip, poetry, conda | requirements.txt |
| AI推理 | **PyTorch 2.8+** | TensorFlow, JAX | - |
| 数据校验 | **Pydantic v2** | dataclasses(仅简单场景), marshmallow | - |
| 关系数据库 | **SQLite** (WAL模式 + FTS5) | PostgreSQL, MySQL | - |
| 向量数据库 | **ChromaDB** | Pinecone, Weaviate, Milvus | - |
| 任务队列 | **Celery + Redis** | RQ, Huey, Dramatiq | - |
| 桌面壳层 | **Tauri 2.x** (Rust) | Electron, NW.js | - |
| C/C++构建 | **CMake 3.25+ + vcpkg** | Make, Bazel | - |
| 浏览器内核 | **CEF 120+** | WebView2(仅Windows回退) | - |
| 视频编码 | **FFmpeg 7.x** | libav直接使用 | - |

---

## 2. 前端编码铁律（React 19.2）

### 2.1 组件规范

```
组件文件扩展名：.tsx（禁止.vue, .jsx）
组件命名：PascalCase（OmniButton.tsx）
页面组件：XxxPage.tsx
全局组件前缀：Omni（OmniButton, OmniModal, OmniDrawer）
组件结构：Function Component + Hooks（禁止Class Component）
```

### 2.2 组件模板

```tsx
// 标准组件写法 — 严格遵守
import { useState, useEffect, useMemo, useCallback } from 'react';

interface OmniButtonProps {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';
  children: React.ReactNode;
  onClick?: () => void;
}

export function OmniButton({ variant = 'primary', size = 'md', children, onClick }: OmniButtonProps) {
  // 1. State
  // 2. Effects (MUST have cleanup return)
  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws');
    return () => ws.close(); // MUST cleanup
  }, []);
  // 3. Memoized values (仅性能瓶颈处使用，Compiler自动处理大部分)
  // 4. Event handlers
  // 5. Render
  return <button className={`btn btn-${variant} btn-${size}`} onClick={onClick}>{children}</button>;
}
```

### 2.3 前端绝对禁止

| 禁止 | 正确替代 |
|------|---------|
| `any` 类型 | `unknown` + 类型守卫 / Zod schema |
| 手动useMemo/useCallback（99%场景） | React Compiler自动处理 |
| 硬编码颜色 `#3b82f6` | `var(--color-primary)` |
| `!important` | 提升选择器特异性 |
| 全量引入UI库 | Tree-shaking按需引入 |
| useEffect无cleanup | MUST return cleanup函数 |
| 嵌套回调>2层 | async/await |
| class组件 | Function Component + Hooks |
| Redux/Pinia/MobX | Zustand 5 |
| 直接操作数据库 | 调后端API |
| WebSocket传AI token | SSE（Server-Sent Events） |
| 动画 width/height/top/left/margin | transform/opacity |
| 超过100条列表不虚拟化 | @tanstack/react-virtual |

### 2.4 状态管理（Zustand）

```tsx
// 10个store: useAppStore, useChatStore, usePaintStore, useStoryboardStore,
// useLearningStore, useModelStore, useStyleStore, useSettingsStore,
// useModalStore, useWebSocketStore
import { create } from 'zustand';

interface AppState {
  activeModule: string;
  setActiveModule: (module: string) => void;
}

export const useAppStore = create<AppState>((set) => ({
  activeModule: 'chat',
  setActiveModule: (module) => set({ activeModule: module }),
}));
```

### 2.5 路由（React Router 7）

8个页面路由：
```
/chat     → ChatPage
/paint    → PaintPage
/comic    → ComicPage（含3D导演台子路由）
/learn    → LearnPage
/models   → ModelsPage
/style    → StylePage
/settings → SettingsPage
/about    → AboutPage
```

### 2.6 3D导演台（React Three Fiber）

```tsx
// 3D导演台MUST用R3F，禁止原生Three.js API
import { Canvas } from '@react-three/fiber';
import { OrbitControls, Environment, Grid } from '@react-three/drei';

function DirectorStage() {
  return (
    <Canvas camera={{ position: [5, 5, 5], fov: 50 }}>
      <ambientLight intensity={0.5} />
      <Environment preset="studio" />
      <Grid args={[20, 20]} />
      <OrbitControls />
      {/* 3D资产 */}
    </Canvas>
  );
}
```

### 2.7 CSS变量体系

```css
/* 禁止硬编码，MUST使用变量 */
--color-primary: #3b82f6;
--color-bg-base: #0f172a;
--color-text-primary: #f1f5f9;
--sidebar-width: 64px;
--header-height: 48px;
--content-max-width: 1280px;
--radius-sm: 6px;
--radius-md: 8px;
--radius-lg: 12px;
--transition-fast: 150ms ease;
--transition-normal: 250ms ease;
```

---

## 3. 后端编码铁律（Python 3.12）

### 3.1 绝对规则

| 规则 | 说明 |
|------|------|
| 全部函数MUST写type hints | `def foo(x: int) -> str:` |
| 全部数据模型MUST用Pydantic v2 | 禁止裸dataclasses用于API |
| asyncio循环中禁止同步阻塞 | 所有IO用async/await，数据库用aiosqlite |
| 循环>10万次MUST向量化 | NumPy/Pandas，禁止纯Python for循环 |
| CPU密集任务MUST用ProcessPool | 禁止threading做CPU计算（GIL） |
| 大模型卸载MUST三件套 | `del model; torch.cuda.empty_cache(); gc.collect()` |
| 禁止循环中重复加载模型 | 模型加载一次，复用实例 |
| 包管理MUST用uv | 禁止pip install不指定版本 |
| mypy --strict MUST通过 | 静态类型检查 |

### 3.2 API响应格式（铁律）

```python
# 所有API响应MUST遵循此格式
class ApiResponse(BaseModel, Generic[T]):
    success: bool
    data: Optional[T]
    error: Optional[ErrorDetail]
    meta: ResponseMeta
```

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "meta": { "request_id": "uuid", "timestamp": "ISO8601", "duration_ms": 123 }
}
```

---

## 4. API路由表（LOCKED）

```
Base URL: http://localhost:8000/api/v1
├── /api/v1/chat          → AI对话
├── /api/v1/paint          → AI绘画（不是/art）
├── /api/v1/storyboard     → 漫剧分镜
├── /api/v1/director        → 3D导演台
├── /api/v1/video            → 视频生成
├── /api/v1/learn           → 知识学习
├── /api/v1/models          → 模型管理
├── /api/v1/style            → 视频风格
├── /api/v1/system          → 系统设置
└── /api/v1/browser         → 内置浏览器
```

> 注意：旧路由 `/api/v1/comic` 已拆分为 storyboard + director + video 三个路由。

---

## 5. 数据库规则

| 数据库 | 用途 | 规则 |
|--------|------|------|
| SQLite (WAL模式) | 元数据/配置/项目数据 | 8张表，FTS5全文检索，单写锁 |
| ChromaDB | 向量数据/知识库 | HNSW索引，BGE-large-zh嵌入 |
| Redis | 任务队列/缓存 | Celery broker |
| localStorage | 用户偏好 | 前端直接读写 |
| IndexedDB | 大型缓存 | 对话历史/绘画历史/分镜数据 |

SQLite 8张表：`projects, chat_sessions, chat_messages, paint_history, storyboard_projects, storyboard_frames, knowledge_files, model_configs`

**禁止前端直接操作SQLite文件。**

---

## 6. Rust/Tauri规则

| 规则 | 说明 |
|------|------|
| 禁止unwrap() | 用`?`操作符或`.unwrap_or()` |
| 禁止主线程阻塞IO | 用tokio异步 |
| IPC命名 | `{domain}_{action}_{resource}`（如`browser_acquire_instance`） |
| IPC大数据(>1MB) | 改用文件路径传递 |
| IPC异步超时 | 默认30s |
| TS/Rust类型对齐 | camelCase字段名一致 |

---

## 7. 安全策略（MUST实施）

### GPU温度保护

| 温度 | 动作 |
|------|------|
| >85°C | 降频，Batch Size减半，状态栏变橙 |
| >90°C | 强制暂停，全屏警告 |
| 连续3次高温 | 锁定GPU利用率至80% |

### 显存4级阈值

| 占用 | 动作 |
|------|------|
| <70% | 安全区，fp32/bf16 |
| 70-85% | 警戒区，降级fp16 |
| 85-95% | 危险区，降级int8，卸载LoRA |
| >95% | 溢出区，强制量化，CPU Offload |

### 其他安全

- 零信任：所有输入不可信，MUST校验
- CEF沙箱隔离
- 进程崩溃3秒内检测重启
- 进程间禁止共享内存

---

## 8. 性能指标（验收标准）

| 指标 | 标准 |
|------|------|
| 首字延迟（≤4K上下文） | <500ms |
| 首字延迟（8K满上下文） | <800ms |
| FLUX.1-dev 1024x1024 | <15s (4090) |
| 5s视频生成 | <60s |
| RAG检索延迟 | <100ms |
| 10万面3D资产加载 | <3s |
| 中文语义理解准确率 | >95% |
| Agent调用成功率 | >98% |
| ML预测准确率 | >85% |
| 10k条虚拟列表FPS | >55 |
| 24h内存增长 | <5% |
| CEF内存泄漏 | <50MB/h |

---

## 9. 模块互斥矩阵

6大模块按P0-P5优先级调度，同一GPU不可同时加载互斥模型：

| 模块 | 互斥模块 | 可共存模块 |
|------|---------|-----------|
| OmniChat (对话) | OmniDraw (绘画) | OmniLearn (知识) |
| OmniDraw (绘画) | OmniChat, VideoStyle | OmniComic (分镜) |
| OmniComic (漫剧) | VideoStyle | OmniDraw |
| VideoStyle (视频) | OmniDraw, OmniComic | OmniChat |
| OmniLearn (学习) | 无 | 全部 |
| ModelManager (管理) | 无 | 全部 |

---

## 10. 硬件基线

**项目硬件基线：RTX 3060 12GB + 32GB RAM（入门档位）**

所有功能MUST在入门档位可用。高端档位为增强体验，非必需。

---

## 11. 开发顺序（P0→P1→P2）

```
P0（核心基础）:
  TASK-SYS-001 项目脚手架 (React 19.2+Vite6+pnpm+Tailwind4+Compiler1.0)
  TASK-SYS-002 Tauri 2.x桌面壳层
  TASK-SYS-003 SQLite 8张表+WAL+FTS5
  TASK-SYS-004 ChromaDB+HNSW+BGE嵌入
  TASK-SYS-005 FastAPI基础框架
  TASK-LAY-001~004 布局+路由+Zustand+Tailwind
  TASK-COMP-001~003 30个Omni组件

P1（核心功能）:
  TASK-CHAT-001~005 OmniChat对话模块
  TASK-PAINT-001~005 OmniDraw绘画模块
  TASK-COMIC-001~005 OmniComic漫剧模块(含3D导演台)
  TASK-MODEL-001~003 ModelManager模型管理

P2（增强功能）:
  TASK-LEARN-001~004 OmniLearn知识学习
  TASK-STYLE-001~003 VideoStyle视频风格
  TASK-ENG-001~005 5大智能引擎
  TASK-SYS-006~010 系统层(更新/日志/安全/硬件)
```

---

## 12. 跨语言禁止事项总表

| 编号 | 禁止 | 原因 |
|------|------|------|
| F-001 | Python threading做CPU计算 | GIL |
| F-002 | TS使用any | 消灭类型安全 |
| F-003 | Rust unwrap() | panic崩溃 |
| F-004 | C++裸new/delete | 内存泄漏 |
| F-005 | SQL字符串拼接 | SQL注入 |
| F-006 | 前端直接操作SQLite | 绕过权限 |
| F-007 | Rust主线程阻塞IO | UI冻结 |
| F-008 | asyncio中调同步函数 | 事件循环阻塞 |
| F-009 | IPC传>1MB JSON | 性能瓶颈 |
| F-010 | C++独立CUDA context | 显存翻倍 |
| F-011 | WebSocket传AI token | 用SSE |
| F-012 | Shell脚本>50行 | 改用Python |
| F-013 | JSON存大段文本 | 改用YAML |
| F-014 | 跨语言传pickle | 不安全 |
| F-015 | 多进程同写SQLite | 写锁冲突 |
| FE-001 | 使用Vue3/Svelte/SolidJS | 已选型React 19.2 |
| FE-002 | 使用npm/yarn | 已锁定pnpm |
| FE-003 | 使用Pinia/Redux | 已选型Zustand 5 |
| FE-004 | 使用Vue Router | 已选型React Router 7 |

---

## 13. Git提交规范

```
格式：type(scope): description
类型：feat / fix / docs / style / refactor / test / chore
示例：feat(chat): 实现Qwen3-VL多模态对话流式输出
```

---

## 14. 文档引用表（需要深入细节时查阅）

| 文档 | 位置 | 用途 |
|------|------|------|
| 约束.md | d:\Users\Desktop\123\约束.md | 完整开发约束（17章，v3.0） |
| 开发清单.txt | d:\Users\Desktop\123\开发清单.txt | 全量开发索引（16章，v3.0） |
| 修正版E | 附件 | 唯一权威全量技术文档（12章） |
| 修改版C | 附件 | 8种编程语言规范 |
| 修正版D | 附件 | 布局视觉规范（CSS变量/组件/尺寸） |
| 测试计划 | 附件 | 414条测试用例 |

**冲突裁决优先级**：此文件 > 约束.md > 修正版E > 其他文档

---

## 15. AI行为规则

1. **每次会话开始**：完整读取此文件，执行第0节检查清单
2. **编码前**：确认涉及的技术栈与此文件第1节一致
3. **编码中**：每写一个组件/函数，对照第2/3节铁律检查
4. **不确定时**：查阅文档引用表（第14节），不要猜测
5. **偏离检测**：如果发现自己使用了禁止的技术，立即停止并修正
6. **新文件命名**：.tsx（前端）/ .py（后端）/ .rs（Rust）
7. **绝不自创规范**：所有规范以本文档和约束.md为准
8. **组件开发**：基于shadcn/ui定制，不要从零构建UI组件
9. **3D开发**：用R3F声明式写法，不要用new THREE.Scene()命令式
10. **状态管理**：用Zustand create()创建store，不要用Context+useReducer

---

> **此文件是AI开发的行为宪法。违反此文件 = 引入Bug = 项目风险。**
> **当AI不确定时，此文件优先于一切。**
