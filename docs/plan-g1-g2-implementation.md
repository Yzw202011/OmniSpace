# OmniSpace AI — G1/G2 实施计划

> 目标：对齐竞品漫剧编辑器的核心差距（P0 级别）
> - G1：解说漫剧 6 步模式（作品类型选择 + 差异化工序流）
> - G2：工序确认弹窗增强（全部/缺失生成 + 模型选择 + 分辨率 + 预估耗时）
> 定位：本地离线/无积分/模型自托管，竞品"积分"替换为"预计耗时"，"渠道/成功率"替换为"本地模型可用状态"。

---

## 一、G1 — 解说漫剧 6 步模式

### 1.1 需求概述

| 维度 | 普通漫剧（5步） | 解说漫剧 COMM_MD（6步） |
|------|----------------|------------------------|
| 适用场景 | 角色对话式漫剧 | 旁白解说式漫剧 |
| 工序流 | ①剧本导入→②角色资产→③分镜生词→④分镜生图→⑤生成视频 | ①剧本导入→②角色资产→③**故事生词**→④**故事生图**→⑤**视频生词**→⑥生成视频 |
| 关键差异 | 分镜级别生词/生图 | **故事级别**生词/生图（跨分镜聚合），新增**视频生词**（为视频生成写专属描述词） |

> 故事生词/生图：以整个故事线为单位，AI 生成长篇连贯描述词和跨分镜一致的风格图，避免分镜间风格漂移。
> 视频生词：为视频生成步骤前置的专属描述词生成，优化运镜和动态描述。

### 1.2 数据库变更

**文件**：`src/data/database.py`

```python
# 在 projects 表新增字段
"work_mode": "TEXT DEFAULT 'regular'",  # 'regular' | 'narrative'
```

- 已有项目默认 `'regular'`（向后兼容）
- 新创建项目根据用户选择写入 `'narrative'`

### 1.3 后端数据模型变更

**文件**：`src/data/models.py`

```python
class WorkMode(str, Enum):
    REGULAR = "regular"
    NARRATIVE = "narrative"

# ProjectCreate / ProjectResponse 新增
work_mode: WorkMode = Field(default=WorkMode.REGULAR)

# 新增：故事生词请求
class StoryNarrativeRequest(BaseModel):
    project_id: str
    row_ids: list[str]           # 要处理的分镜行（空=全部）
    scope: str = "all"           # 'all' | 'missing'（全部/缺失）
    model_override: str | None = None
    prompt_prefix: str | None = None

# 新增：故事生图请求
class StoryKeyframeRequest(BaseModel):
    project_id: str
    row_ids: list[str]
    scope: str = "all"
    model_override: str | None = None
    resolution: str = "1024x1024"  # 1024x1024 | 1024x576 | 576x1024

# 新增：视频生词请求
class VideoNarrativeRequest(BaseModel):
    project_id: str
    row_ids: list[str]
    scope: str = "all"
    model_override: str | None = None
    prompt_prefix: str | None = None
```

### 1.4 后端 API 端点新增

**文件**：`src/api/manga.py`

| 方法 | 路径 | 函数名 | 说明 |
|------|------|--------|------|
| POST | `/manga/story/narrative` | `story_narrative_generate` | 故事生词（跨分镜聚合生成长描述词） |
| POST | `/manga/story/keyframe` | `story_keyframe_generate` | 故事生图（跨分镜一致性风格图） |
| POST | `/manga/video/narrative` | `video_narrative_generate` | 视频生词（为视频生成写专属描述词） |
| GET | `/manga/models/available` | `list_available_models` | 获取当前任务类型可用的本地模型列表及状态 |

**详细实现**：

#### ① POST /manga/story/narrative（故事生词）

```python
@router.post("/manga/story/narrative")
async def story_narrative_generate(req: StoryNarrativeRequest):
    """
    解说漫剧第 3 步：故事生词。
    将选中分镜行的 original_dialogue 聚合为故事线，调对话引擎生成连贯的长篇描述词，
    结果回写每行 description 字段（统一风格，减少分镜间漂移）。
    """
    # 1. 校验项目存在且 work_mode='narrative'
    # 2. 按 row_ids 取行，按 sort_index 排序聚合为 story_context
    # 3. 调 dialog_engine.generate_story_description(story_context, prompt_prefix)
    # 4. 将生成的长描述词拆分为每行的 description（按行内容比例或AI分段）
    # 5. 批量 UPDATE storyboard_rows 的 description
    # 6. 返回 { done: int, skipped: int, failed: int, rows: [...] }
```

#### ② POST /manga/story/keyframe（故事生图）

```python
@router.post("/manga/story/keyframe")
async def story_keyframe_generate(req: StoryKeyframeRequest):
    """
    解说漫剧第 4 步：故事生图。
    以故事线为单位生图，确保跨分镜风格一致性。
    生成第 1 张图后，后续分镜以第 1 张为参考（img2img 或 IP-Adapter 风格锁定），
    产出 keyframes 并标记 is_current。
    """
    # 1. 校验 narrative 模式
    # 2. 按 sort_index 排序取行
    # 3. 第 1 行：paint_engine.generate(desc, resolution) → base ref image
    # 4. 第 2~N 行：paint_engine.generate_with_reference(desc, ref_image, resolution)
    #    当前 IP-Adapter 未随包 → 降级为统一 seed + 统一 prompt 前缀 保证近似一致性，
    #    响应带 degraded 标记诚实标注
    # 5. 登记 keyframes，设 is_current=true
    # 6. 返回 { succeeded: [...], failed: [...], success_count, total, degraded? }
```

#### ③ POST /manga/video/narrative（视频生词）

```python
@router.post("/manga/video/narrative")
async def video_narrative_generate(req: VideoNarrativeRequest):
    """
    解说漫剧第 5 步：视频生词。
    在已有分镜图基础上，AI 生成优化视频动态的描述词（含运镜、动作、转场建议），
    回写到行的 video_description 字段（新增列或复用 description 扩展）。
    """
    # 1. 校验 narrative 模式
    # 2. 取有 keyframe 的行
    # 3. 调 dialog_engine.generate_video_description(row.description, row.keyframe_prompt)
    # 4. 回写 video_description（或扩展 description 追加视频段落）
    # 5. 返回 { done, skipped, failed }
```

#### ④ GET /manga/models/available

```python
@router.get("/manga/models/available")
async def list_available_models(task_type: str):
    """
    返回指定任务类型可用的本地模型列表及状态。
    task_type: 'dialog' | 'paint' | 'video'
    响应: { items: [{ id, name, status, vram_gb, speed_label, notes }] }
    status: 'ready' | 'loading' | 'not_installed' | 'offload'
    speed_label: '极速' | '快速' | '标准' | '慢速'（基于硬件等级和模型大小估算）
    """
    # 查 model_manager.get_models_by_category(task_type)
    # 结合当前 hardware_profile 过滤 vram 够用的模型
    # 正在 loaded 的模型 status='ready'，未加载但已安装='not_installed'（可热加载）
```

### 1.5 后端服务层新增

**文件**：`src/services/inference/dialog_engine.py`（新增方法）

```python
async def generate_story_description(
    self,
    story_context: str,           # 聚合的多行台词/剧情文本
    prompt_prefix: str | None = None,
    model_id: str | None = None,
) -> str:
    """
    故事级描述词生成：输入多行聚合文本，输出连贯的、跨分镜风格统一的长描述词。
    提示词模板："以下是一段漫画剧情，请生成连贯的画面描述词，保持角色和场景一致性..."
    """

async def generate_video_description(
    self,
    shot_description: str,        # 分镜描述词
    keyframe_prompt: str | None = None,
    model_id: str | None = None,
) -> str:
    """
    视频级描述词生成：在分镜图描述基础上，追加运镜和动态描述。
    输出格式："[画面]... [运镜]... [动作]... [转场]..."
    """
```

**文件**：`src/services/inference/paint_engine.py`（新增方法）

```python
async def generate_with_reference(
    self,
    prompt: str,
    reference_image_path: str,    # 参考图路径（第 1 张故事图）
    resolution: str = "1024x1024",
    model_id: str | None = None,
) -> dict:
    """
    带参考图的风格一致性生图。
    当前 IP-Adapter 未随包 → 降级方案：统一 seed + 统一风格前缀词，
    诚实标记 degraded=True，degrade_reason="IP-Adapter 未安装，使用 seed 一致性回退"。
    """
```

### 1.6 前端类型定义变更

**文件**：`frontend/src/types/index.ts`

```typescript
// 在 ComicProject 接口新增
export interface ComicProject {
  // ...existing fields
  work_mode: 'regular' | 'narrative';
}

// 新增：工序步骤定义（支持动态步骤数）
export type WorkflowStep = {
  key: string;
  label: string;
  done: boolean;
  action: () => void;
};

// 新增：模型配置（G2 复用）
export interface ModelConfig {
  dialog_model?: string;      // 推理模型 ID
  paint_model?: string;       // 生图模型 ID
  video_model?: string;       // 视频模型 ID
  resolution?: string;        // 分辨率
  duration_seconds?: number;  // 视频时长
}

// 新增：可用模型信息
export interface AvailableModel {
  id: string;
  name: string;
  status: 'ready' | 'loading' | 'not_installed' | 'offload';
  vram_gb: number;
  speed_label: string;
  notes?: string;
}
```

### 1.7 前端 API 层变更

**文件**：`frontend/src/services/mangaApi.ts`

```typescript
// 新增：故事生词
export async function generateStoryNarrative(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  promptPrefix?: string,
): Promise<{ done: number; skipped: number; failed: number; rows: StoryboardRow[] }> {
  return post('/manga/story/narrative', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    prompt_prefix: promptPrefix,
  });
}

// 新增：故事生图
export async function generateStoryKeyframe(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  resolution?: string,
): Promise<{ succeeded: KeyframeItem[]; failed: { row_id: string; message: string }[]; success_count: number; total: number; degraded?: boolean }> {
  return post('/manga/story/keyframe', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    resolution,
  });
}

// 新增：视频生词
export async function generateVideoNarrative(
  projectId: string,
  rowIds: string[],
  scope: 'all' | 'missing' = 'all',
  modelOverride?: string,
  promptPrefix?: string,
): Promise<{ done: number; skipped: number; failed: number }> {
  return post('/manga/video/narrative', {
    project_id: projectId,
    row_ids: rowIds,
    scope,
    model_override: modelOverride,
    prompt_prefix: promptPrefix,
  });
}

// 新增：获取可用模型列表
export async function listAvailableModels(taskType: 'dialog' | 'paint' | 'video'): Promise<{ items: AvailableModel[] }> {
  return get('/manga/models/available', { task_type: taskType });
}
```

### 1.8 前端状态管理变更

**文件**：`frontend/src/stores/useMangaStore.ts`

```typescript
interface MangaState {
  // ...existing state
  
  // G1: 当前项目工作模式
  workMode: 'regular' | 'narrative';
  
  // G1+G2: 模型配置（按项目持久化）
  modelConfig: ModelConfig;
  
  // G2: 可用模型缓存
  availableModels: Record<string, AvailableModel[]>;  // key: task_type
  
  // G1: 批量工序类型扩展
  // confirmBatch 现支持 'describe' | 'keyframe' | 'story_narrative' | 'story_keyframe' | 'video_narrative'
  
  // Actions
  setWorkMode: (mode: 'regular' | 'narrative') => void;
  updateModelConfig: (config: Partial<ModelConfig>) => void;
  fetchAvailableModels: (taskType: string) => Promise<void>;
  
  // G1: 故事级批量执行
  batchStoryNarrative: (rowIds: string[], scope: string, cfg?: ModelConfig) => Promise<BatchResult>;
  batchStoryKeyframe: (rowIds: string[], scope: string, cfg?: ModelConfig) => Promise<BatchResult>;
  batchVideoNarrative: (rowIds: string[], scope: string, cfg?: ModelConfig) => Promise<BatchResult>;
}
```

### 1.9 前端组件变更

#### ① MangaLibrary.tsx — 创建项目弹窗增加模式选择

**文件**：`frontend/src/components/manga/MangaLibrary.tsx`

```typescript
// 状态新增
const [workMode, setWorkMode] = useState<'regular' | 'narrative'>('regular');

// 创建弹窗 UI 新增
<div className="manga-form-group">
  <label className="manga-form-label">作品类型</label>
  <div className="manga-mode-switch">
    <button
      type="button"
      className={workMode === 'regular' ? 'active' : ''}
      onClick={() => setWorkMode('regular')}
    >
      <span className="mode-title">普通漫剧</span>
      <span className="mode-desc">5 步流程，适合角色对话式作品</span>
    </button>
    <button
      type="button"
      className={workMode === 'narrative' ? 'active' : ''}
      onClick={() => setWorkMode('narrative')}
    >
      <span className="mode-title">解说漫剧</span>
      <span className="mode-desc">6 步流程，适合旁白解说式作品</span>
    </button>
  </div>
</div>

// createProject 调用传入 workMode
createProject(name, useTemplate ? 'comic_drama' : undefined, workMode)
```

#### ② MangaWorkspace.tsx — 工序条动态化

**文件**：`frontend/src/components/manga/MangaWorkspace.tsx`

```typescript
// 工序定义改为从 workMode 推导
const stages = useMemo(() => {
  const describeDone = rows.length > 0 && rows.every((r) => r.description.trim());
  const keyframeDone = rows.length > 0 && rows.every((r) => (keyframesMap[r.id] ?? []).some((k) => k.is_current));
  const videoDone = rows.length > 0 && rows.every((r) => r.generation_status === 'done' || r.generation_status === 'skipped');
  
  // G1: 解说漫剧新增 video_description 判定
  const videoDescDone = rows.length > 0 && rows.every((r) => (r as any).video_description?.trim());
  
  const isNarrative = currentProject?.work_mode === 'narrative';
  
  let list: { label: string; done: boolean; action: () => void }[];
  
  if (isNarrative) {
    // 解说漫剧 6 步
    list = [
      { label: '剧本导入', done: rows.length > 0, action: () => toggleDrawer('import') },
      { label: '角色资产', done: assets.length > 0, action: () => { setDrawer(''); setInspectRowId(null); setSelectedAsset(null); } },
      { label: '故事生词', done: describeDone, action: () => setConfirmBatch('story_narrative') },
      { label: '故事生图', done: keyframeDone, action: () => setConfirmBatch('story_keyframe') },
      { label: '视频生词', done: videoDescDone, action: () => setConfirmBatch('video_narrative') },
      { label: '生成视频', done: videoDone, action: () => toggleDrawer('video') },
    ];
  } else {
    // 普通漫剧 5 步（保持现有逻辑）
    list = [
      { label: '剧本导入', done: rows.length > 0, action: () => toggleDrawer('import') },
      { label: '角色资产', done: assets.length > 0, action: () => { setDrawer(''); setInspectRowId(null); setSelectedAsset(null); } },
      { label: '分镜生词', done: describeDone, action: () => setConfirmBatch('describe') },
      { label: '分镜生图', done: keyframeDone, action: () => setConfirmBatch('keyframe') },
      { label: '生成视频', done: videoDone, action: () => toggleDrawer('video') },
    ];
  }
  
  const currentIndex = list.findIndex((s) => !s.done);
  return { list, currentIndex: currentIndex === -1 ? list.length - 1 : currentIndex };
}, [rows, assets.length, keyframesMap, currentProject?.work_mode, toggleDrawer, setSelectedAsset]);

// confirmBatch 类型扩展
const [confirmBatch, setConfirmBatch] = useState<'' | 'describe' | 'keyframe' | 'story_narrative' | 'story_keyframe' | 'video_narrative'>('');
```

#### ③ BatchConfirmModal.tsx — 支持新的工序类型 + G2 增强

见 G2 章节，此处确认 kind 类型扩展：

```typescript
export interface BatchConfirmModalProps {
  kind: 'describe' | 'keyframe' | 'story_narrative' | 'story_keyframe' | 'video_narrative';
  onClose: () => void;
}
```

#### ④ batchOps.ts — 新增故事级/视频生词执行器

**文件**：`frontend/src/components/manga/editor/batchOps.ts`

```typescript
// 新增：故事生词批量执行
export async function batchStoryNarrative(
  targets: StoryboardRow[],
  projectId: string,
  onProgress: (done: number) => void,
  config?: ModelConfig,
): Promise<BatchResult> { ... }

// 新增：故事生图批量执行
export async function batchStoryKeyframe(
  targets: StoryboardRow[],
  projectId: string,
  onProgress: (done: number) => void,
  config?: ModelConfig,
): Promise<BatchResult> { ... }

// 新增：视频生词批量执行
export async function batchVideoNarrative(
  targets: StoryboardRow[],
  projectId: string,
  onProgress: (done: number) => void,
  config?: ModelConfig,
): Promise<BatchResult> { ... }
```

### 1.10 前端样式变更

**文件**：`frontend/src/styles/app.css`

```css
/* 作品类型选择开关 */
.manga-mode-switch {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.manga-mode-switch > button {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 16px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition-fast);
}
.manga-mode-switch > button.active {
  border-color: var(--color-primary);
  background: rgba(59, 130, 246, 0.08);
  box-shadow: 0 0 12px rgba(59, 130, 246, 0.15);
}
.manga-mode-switch .mode-title {
  font-weight: 600;
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}
.manga-mode-switch .mode-desc {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
```

---

## 二、G2 — 工序确认弹窗增强

### 2.1 需求概述

| 功能 | 竞品实现 | OmniSpace 本地化适配 |
|------|---------|---------------------|
| 全部/缺失生成 | 下拉选择 `isAll`（全部生成 / 缺失生成） | 同竞品，但某些状态隐藏"缺失生成"（如首次执行无缺失时） |
| 积分预估 | `calculateDedPoints` API 返回积分消耗 | **替换为"预计耗时"**：基于模型速度标签 + 行数计算 |
| 模型选择 | 推理/图片/视频模型 + 渠道 + 成功率 | **替换为"本地模型"**：显示已安装模型 + 加载状态 + VRAM 需求 |
| 分辨率/时长 | 图片分辨率、视频时长/分辨率 | 同竞品，使用本地配置 |
| 成功率展示 | "xx%成功率 / 渠道不可用" | **替换为"可用状态"**：ready（绿色）/ not_installed（黄色，可热加载）/ offload（灰色，需卸载其他模型） |

### 2.2 前端组件变更

#### ① BatchConfirmModal.tsx — 全面重构

**文件**：`frontend/src/components/manga/editor/BatchConfirmModal.tsx`

**状态扩展**：

```typescript
type Scope = 'all' | 'missing';

export default function BatchConfirmModal({ kind, onClose }: BatchConfirmModalProps) {
  // ...existing state
  
  // G2 新增状态
  const [scope, setScope] = useState<Scope>('missing');      // 默认"缺失生成"（安全）
  const [modelId, setModelId] = useState<string>('');         // 选中的模型 ID
  const [resolution, setResolution] = useState<string>('1024x1024');
  const [duration, setDuration] = useState<number>(5);        // 视频专属
  const [modelsLoading, setModelsLoading] = useState(false);
  const [availableModels, setAvailableModels] = useState<AvailableModel[]>([]);
  
  // 预估耗时（本地计算，非 API）
  const estimatedTime = useMemo(() => {
    const model = availableModels.find((m) => m.id === modelId);
    if (!model || runnableCount === 0) return null;
    // 速度标签 → 基准秒/行
    const baseSecPerRow = { '极速': 8, '快速': 15, '标准': 30, '慢速': 60 }[model.speed_label] || 30;
    const totalSec = baseSecPerRow * runnableCount;
    if (totalSec < 60) return `约 ${totalSec} 秒`;
    if (totalSec < 3600) return `约 ${Math.ceil(totalSec / 60)} 分钟`;
    return `约 ${(totalSec / 3600).toFixed(1)} 小时`;
  }, [availableModels, modelId, runnableCount]);
  
  // 根据 kind 确定任务类型和默认模型
  const taskType = useMemo(() => {
    if (kind === 'describe' || kind === 'story_narrative' || kind === 'video_narrative') return 'dialog';
    if (kind === 'keyframe' || kind === 'story_keyframe') return 'paint';
    return 'dialog';
  }, [kind]);
  
  // 加载可用模型列表
  useEffect(() => {
    setModelsLoading(true);
    listAvailableModels(taskType as any)
      .then((res) => {
        setAvailableModels(res.items);
        // 默认选中第一个 ready 的模型
        const ready = res.items.find((m) => m.status === 'ready');
        if (ready) setModelId(ready.id);
        else if (res.items[0]) setModelId(res.items[0].id);
      })
      .catch(() => showToast('模型列表加载失败', 'error'))
      .finally(() => setModelsLoading(false));
  }, [taskType]);
  
  // 根据 scope 过滤目标行
  const filteredTargets = useMemo(() => {
    if (scope === 'all') {
      // "全部生成" = 所有行（含已有）
      return rows.filter((r) => {
        if (r.is_locked) return false;
        if (kind === 'describe' || kind === 'story_narrative' || kind === 'video_narrative') {
          return r.original_dialogue.trim().length > 0;
        }
        return r.description.trim().length > 0;
      });
    }
    // "缺失生成" = 原有逻辑（缺描述词/缺分镜图）
    return targets;
  }, [scope, rows, targets, kind]);
  
  // 重新计算 filtered 后的统计
  const filteredLockedCount = useMemo(() => filteredTargets.filter((r) => r.is_locked).length, [filteredTargets]);
  const filteredNoInputCount = useMemo(() => /* ... */, [filteredTargets, kind]);
  const filteredRunnableCount = filteredTargets.length - filteredLockedCount - filteredNoInputCount;
}
```

**UI 重构**（Modal 内容区）：

```tsx
<Modal width={560} /* 加宽以容纳模型选择 */>
  <div className="flex flex-col gap-4">
    
    {/* ① 范围选择：全部/缺失 */}
    <div className="manga-batch-scope">
      <label className="manga-form-label">生成范围</label>
      <select
        className="manga-select"
        value={scope}
        onChange={(e) => setScope(e.target.value as Scope)}
      >
        <option value="missing">缺失生成（仅处理未完成的 {targets.length} 行）</option>
        <option value="all">全部生成（重新处理所有符合条件的行）</option>
      </select>
    </div>
    
    {/* ② 统计卡片（基于 filteredTargets） */}
    <div className="manga-batch-stats">
      {/* 同现有，但数据源改为 filteredTargets */}
    </div>
    
    {/* ③ 模型选择（按任务类型） */}
    <div className="manga-batch-model">
      <label className="manga-form-label">
        {taskType === 'dialog' ? '推理模型' : taskType === 'paint' ? '生图模型' : '视频模型'}
      </label>
      {modelsLoading ? (
        <span className="text-tertiary text-xs">加载中…</span>
      ) : (
        <div className="manga-model-list">
          {availableModels.map((m) => (
            <button
              key={m.id}
              type="button"
              className={`manga-model-card${modelId === m.id ? ' active' : ''}${m.status !== 'ready' ? ' disabled' : ''}`}
              onClick={() => m.status === 'ready' && setModelId(m.id)}
              disabled={m.status !== 'ready'}
              title={m.notes}
            >
              <span className="model-name">{m.name}</span>
              <span className={`model-status ${m.status}`}>
                {m.status === 'ready' ? '可用' : m.status === 'not_installed' ? '未加载' : '需卸载'}
              </span>
              <span className="model-meta">{m.vram_gb}GB · {m.speed_label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
    
    {/* ④ 分辨率/时长（图片/视频任务） */}
    {(kind === 'keyframe' || kind === 'story_keyframe') && (
      <div className="manga-batch-res">
        <label className="manga-form-label">分辨率</label>
        <div className="manga-seg">
          {['1024x1024', '1024x576', '576x1024'].map((r) => (
            <button
              key={r}
              type="button"
              className={resolution === r ? 'active' : ''}
              onClick={() => setResolution(r)}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
    )}
    
    {kind === 'video' && (
      <div className="manga-batch-duration">
        <label className="manga-form-label">视频时长</label>
        <input
          type="range"
          min={1}
          max={20}
          value={duration}
          onChange={(e) => setDuration(Number(e.target.value))}
        />
        <span>{duration} 秒</span>
      </div>
    )}
    
    {/* ⑤ 预计耗时（替代积分） */}
    {estimatedTime && (
      <div className="manga-batch-estimate">
        <span className="estimate-icon">⏱</span>
        <span className="estimate-label">预计耗时</span>
        <span className="estimate-value">{estimatedTime}</span>
        <span className="estimate-hint">基于 {filteredRunnableCount} 行 × {availableModels.find(m => m.id === modelId)?.speed_label || '标准'} 速度估算</span>
      </div>
    )}
    
    {/* ⑥ 提示词设置入口（生词类） */}
    {isDescribe && /* ... 同现有 ... */}
    
    {/* ⑦ 进度与结果（同现有） */}
  </div>
</Modal>
```

**执行逻辑变更**：

```typescript
const handleRun = () => {
  if (!currentProject || filteredRunnableCount === 0 || phase === 'running') return;
  setPhase('running');
  setProcessed(0);
  
  const onProgress = (done: number) => setProcessed(done);
  const config: ModelConfig = {
    [taskType === 'dialog' ? 'dialog_model' : taskType === 'paint' ? 'paint_model' : 'video_model']: modelId,
    resolution,
    duration_seconds: duration,
  };
  
  // 根据 kind 路由到不同的 batchOps 函数，传入 config
  const call = (() => {
    switch (kind) {
      case 'describe': return batchDescribe(filteredTargets, currentProject.id, onProgress, config);
      case 'keyframe': return batchKeyframes(filteredTargets, currentProject.id, onProgress, config);
      case 'story_narrative': return batchStoryNarrative(filteredTargets, currentProject.id, onProgress, config);
      case 'story_keyframe': return batchStoryKeyframe(filteredTargets, currentProject.id, onProgress, config);
      case 'video_narrative': return batchVideoNarrative(filteredTargets, currentProject.id, onProgress, config);
      default: return batchDescribe(filteredTargets, currentProject.id, onProgress, config);
    }
  })();
  
  call.then((res) => { /* ... 同现有 ... */ });
};
```

#### ② batchOps.ts — 所有执行器支持 ModelConfig 覆盖

**文件**：`frontend/src/components/manga/editor/batchOps.ts`

```typescript
// 现有函数签名增加可选 config 参数
export async function batchDescribe(
  targets: StoryboardRow[],
  projectId: string,
  onProgress: (done: number) => void,
  config?: ModelConfig,  // G2 新增
): Promise<BatchResult> {
  // 调用 aiDescribe 时传入 model_override 和 prompt_prefix
  for (const row of runnable) {
    const desc = await aiDescribe(row.id, projectId, readPromptCfg().custom, config?.dialog_model);
    // ...
  }
}

export async function batchKeyframes(
  targets: StoryboardRow[],
  projectId: string,
  onProgress: (done: number) => void,
  config?: ModelConfig,  // G2 新增
): Promise<BatchResult> {
  // 调用 batchGenerateKeyframes 或 generateKeyframe 时传入 model_override 和 resolution
  const res = await batchGenerateKeyframes(
    runnable.map((r) => r.id),
    projectId,
    config?.resolution,
    config?.paint_model,
  );
}

// 新增执行器同理，均接受 config 参数
```

### 2.3 后端 API 变更

**文件**：`src/api/manga.py`

现有端点扩展以接受 `model_override` 和 `resolution`：

```python
# POST /storyboard/ai-describe 扩展
class AiDescribeRequest(BaseModel):
    row_id: str
    project_id: str | None = None
    prompt_prefix: str | None = None
    model_override: str | None = None  # G2 新增

# POST /manga/keyframe/batch 扩展
class BatchKeyframeRequest(BaseModel):
    row_ids: list[str]
    project_id: str | None = None
    model_override: str | None = None  # G2 新增
    resolution: str = "1024x1024"      # G2 新增
```

### 2.4 前端样式变更

**文件**：`frontend/src/styles/app.css`

```css
/* 模型选择卡片列表 */
.manga-model-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}
.manga-model-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition-fast);
}
.manga-model-card.active {
  border-color: var(--color-primary);
  background: rgba(59, 130, 246, 0.08);
}
.manga-model-card.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.manga-model-card .model-name {
  font-weight: 500;
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}
.manga-model-card .model-status {
  font-size: var(--font-size-xs);
  padding: 1px 6px;
  border-radius: 999px;
  width: fit-content;
}
.manga-model-card .model-status.ready {
  background: rgba(34, 197, 94, 0.12);
  color: var(--color-success);
}
.manga-model-card .model-status.not_installed {
  background: rgba(234, 179, 8, 0.12);
  color: var(--color-warning);
}
.manga-model-card .model-meta {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}

/* 预计耗时 */
.manga-batch-estimate {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-elevated);
  border: 1px dashed var(--color-border);
}
.manga-batch-estimate .estimate-icon { font-size: 16px; }
.manga-batch-estimate .estimate-label { font-size: var(--font-size-sm); color: var(--color-text-secondary); }
.manga-batch-estimate .estimate-value { font-weight: 600; color: var(--color-primary); margin-left: auto; }
.manga-batch-estimate .estimate-hint { font-size: var(--font-size-xs); color: var(--color-text-tertiary); width: 100%; }

/* 范围选择 */
.manga-batch-scope .manga-select {
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-input-bg);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
}

/* 分段选择器（分辨率） */
.manga-seg {
  display: flex;
  gap: 4px;
}
.manga-seg > button {
  flex: 1;
  padding: 6px 0;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.manga-seg > button.active {
  border-color: var(--color-primary);
  background: rgba(59, 130, 246, 0.08);
  color: var(--color-primary);
}
```

---

## 三、实施顺序与依赖关系

```
第一阶段：基础设施（G1+G2 共用）
  ├─ ① database.py: projects 表加 work_mode 列
  ├─ ② models.py: WorkMode 枚举 + 新增 Request 模型
  ├─ ③ types/index.ts: 前端类型新增（ComicProject.work_mode, ModelConfig, AvailableModel）
  └─ ④ mangaApi.ts: 新增 API 函数（故事/视频生词/生图 + 可用模型列表）

第二阶段：G2 工序弹窗增强（先做，因为 G1 的 batchOps 复用）
  ├─ ⑤ src/api/manga.py: GET /manga/models/available + 现有端点扩展 model_override
  ├─ ⑥ src/services/model_manager.py: 新增 get_models_by_category 方法
  ├─ ⑦ BatchConfirmModal.tsx: 全面重构（范围选择 + 模型选择 + 分辨率 + 预计耗时）
  ├─ ⑧ batchOps.ts: 所有执行器增加 ModelConfig 参数
  └─ ⑨ app.css: 新增弹窗相关样式

第三阶段：G1 解说漫剧模式
  ├─ ⑩ src/api/manga.py: 新增 story/narrative, story/keyframe, video/narrative 端点
  ├─ ⑪ dialog_engine.py: 新增 generate_story_description, generate_video_description
  ├─ ⑫ paint_engine.py: 新增 generate_with_reference
  ├─ ⑬ MangaLibrary.tsx: 创建弹窗增加 work_mode 选择
  ├─ ⑭ MangaWorkspace.tsx: 工序条根据 work_mode 动态渲染
  ├─ ⑮ batchOps.ts: 新增 batchStoryNarrative / batchStoryKeyframe / batchVideoNarrative
  ├─ ⑯ useMangaStore.ts: 新增 workMode / modelConfig / fetchAvailableModels
  └─ ⑰ app.css: 新增作品类型选择样式

第四阶段：验证
  ├─ ⑱ py_compile 后端文件
  ├─ ⑲ tsc --noEmit
  ├─ ⑳ vite build
  └─ ㉑ 浏览器实测（普通漫剧 5 步 + 解说漫剧 6 步 + 弹窗模型选择）
```

---

## 四、文件修改清单汇总

### 后端文件（7 个）

| # | 文件 | 修改类型 | 修改内容 |
|---|------|---------|---------|
| 1 | `src/data/database.py` | 编辑 | projects 表加 `work_mode TEXT DEFAULT 'regular'` |
| 2 | `src/data/models.py` | 编辑 | 新增 `WorkMode` 枚举、`StoryNarrativeRequest`、`StoryKeyframeRequest`、`VideoNarrativeRequest` |
| 3 | `src/api/manga.py` | 编辑 | 新增 4 个端点 + 扩展现有端点 model_override/resolution |
| 4 | `src/services/inference/dialog_engine.py` | 编辑 | 新增 `generate_story_description`、`generate_video_description` |
| 5 | `src/services/inference/paint_engine.py` | 编辑 | 新增 `generate_with_reference`（降级回退） |
| 6 | `src/services/model_manager.py` | 编辑 | 新增 `get_models_by_category(task_type)` |
| 7 | `src/api/manga.py` | 编辑 | `ProjectCreate`/`ProjectResponse` 扩展 `work_mode` |

### 前端文件（8 个）

| # | 文件 | 修改类型 | 修改内容 |
|---|------|---------|---------|
| 8 | `frontend/src/types/index.ts` | 编辑 | `ComicProject` 加 `work_mode`；新增 `ModelConfig`、`AvailableModel` |
| 9 | `frontend/src/services/mangaApi.ts` | 编辑 | 新增 `generateStoryNarrative`、`generateStoryKeyframe`、`generateVideoNarrative`、`listAvailableModels` |
| 10 | `frontend/src/services/schema.ts` | 编辑 | 新增 Zod schema（故事/视频生词响应、可用模型列表） |
| 11 | `frontend/src/stores/useMangaStore.ts` | 编辑 | 新增 `workMode`、`modelConfig`、`availableModels`、`fetchAvailableModels`、批量执行 actions |
| 12 | `frontend/src/components/manga/MangaLibrary.tsx` | 编辑 | 创建弹窗增加 `work_mode` 选择开关 |
| 13 | `frontend/src/components/manga/MangaWorkspace.tsx` | 编辑 | 工序条 `stages` 根据 `work_mode` 动态渲染 5/6 步 |
| 14 | `frontend/src/components/manga/editor/BatchConfirmModal.tsx` | 编辑 | 全面重构：范围选择 + 模型选择 + 分辨率 + 预计耗时 |
| 15 | `frontend/src/components/manga/editor/batchOps.ts` | 编辑 | 所有执行器增加 `config?: ModelConfig`；新增 3 个故事级执行器 |
| 16 | `frontend/src/styles/app.css` | 编辑 | 新增模型卡片、预计耗时、范围选择、分段选择器、作品类型开关样式 |

---

## 五、风险与回退策略

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| `dialog_engine.generate_story_description` 效果不佳 | 故事级描述词质量低 | 降级为逐行调用 `aiDescribe`（退化为普通漫剧效果），诚实标记 degraded |
| `paint_engine.generate_with_reference` 无 IP-Adapter | 跨分镜风格不一致 | 已规划降级：统一 seed + 风格前缀，degraded 标记 |
| `work_mode` 数据库迁移 | 现有项目无该列 | `DEFAULT 'regular'` 保证向后兼容，无需迁移脚本 |
| 模型选择增加用户决策成本 | 新手不知选哪个 | 默认自动选中 `status='ready'` 的第一个模型，减少手动操作 |
| 弹窗宽度从 460→560 | 小屏笔记本可能溢出 | max-width: 95vw + 内部滚动，确保 1366px 屏可用 |

---

## 六、验收标准

### G1 验收

- [ ] 创建项目弹窗可选"普通漫剧"和"解说漫剧"
- [ ] 普通漫剧项目显示 5 步工序条（剧本导入→角色资产→分镜生词→分镜生图→生成视频）
- [ ] 解说漫剧项目显示 6 步工序条（剧本导入→角色资产→故事生词→故事生图→视频生词→生成视频）
- [ ] 解说漫剧第 3 步"故事生词"生成跨分镜连贯描述词
- [ ] 解说漫剧第 4 步"故事生图"生成风格一致的系列图
- [ ] 解说漫剧第 5 步"视频生词"生成视频专属描述词
- [ ] 后端 `work_mode` 持久化，刷新页面后保持模式

### G2 验收

- [ ] 批量确认弹窗显示"全部生成/缺失生成"下拉选择
- [ ] 弹窗内显示当前任务类型的可用模型列表（含状态、VRAM、速度标签）
- [ ] 默认自动选中已加载（ready）的模型
- [ ] 生图任务显示分辨率选择（1024x1024 / 1024x576 / 576x1024）
- [ ] 弹窗底部显示"预计耗时"（基于行数 × 模型速度估算）
- [ ] 选择"全部生成"时重新处理所有行，选择"缺失生成"时仅处理未完成的行
- [ ] 后端 `model_override` 参数生效，实际使用用户选择的模型
