import { mirrorPref } from '@/services/uiPrefs';
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 右侧面板（规格 §6.1.3：320px，按当前路由动态切换）
 * --------------------------------------------------------------------------
 * 路由 → 面板映射（规格 §6.1.3）：
 *   /chat       → 对话设置 / 模型选择 / 温度调节（§6.2.1 参数控制，写回 useDialogStore）
 *   /paint      → 无面板（2026-09-07 M1：AI漫画工作台自包含；旧绘画参数面板随绘画页退役）
 *   /storyboard → 分镜属性 / 角色设置 / 场景参数（读 useMangaStore 真实项目数据）
 *   /learning   → 学习进度 / 知识详情 / LoRA版本（learningApi + learnApi，5s 轮询）
 *   /models     → 模型详情 / 显存占用 / 加载状态（useModelStore + 硬件实时遥测）
 *   /style      → 风格参数 / LoRA训练进度 / 预览（读写 useStyleStore）
 *   /settings、/help → 无右侧面板（规格 §6.1.3 未列出，整栏隐藏）
 * 诚实门控：后端未就绪/无数据时展示 -- 或空态提示，绝不伪造数据。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import {
  MessageSquare,
  Clapperboard,
  BookOpen,
  Package,
  Video,
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
} from 'lucide-react';
import type { StoryboardRow } from '@/types';
import { useAppStore } from '@/stores/useAppStore';
import {
  useDialogStore,
  CONTEXT_TOKEN_OPTIONS,
  TEMPERATURE_MIN,
  TEMPERATURE_MAX,
} from '@/stores/useDialogStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { useModelStore } from '@/stores/useModelStore';
import { useStyleStore } from '@/stores/useStyleStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import DialogWarmupBar from '../common/DialogWarmupBar';
import * as learningApi from '@/services/learningApi';
import { listLearnModels } from '@/services/learnApi';
import { listCloudProviders } from '@/services/cloudApi';
import { LEARN_SESSION_STATUS_LABELS, TRAIN_STATUS_LABELS } from '@/constants/statusLabels';

/** 面板折叠态本地持久化键 */
const PANEL_COLLAPSED_KEY = 'omnispace.layout.rightPanelCollapsed';
/** 面板宽度本地持久化键（SYS-020 拖拽调宽） */
const PANEL_WIDTH_KEY = 'omnispace.layout.rightPanelWidth';
/** 面板宽度边界（px） */
const PANEL_MIN_W = 240;
const PANEL_MAX_W = 520;
const PANEL_DEFAULT_W = 320;

/** 读取折叠态（异常回退展开） */
function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(PANEL_COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

/** 读取面板宽度（异常/越界回退默认 320；SYS-020） */
function loadWidth(): number {
  try {
    const n = parseInt(localStorage.getItem(PANEL_WIDTH_KEY) || '', 10);
    if (Number.isFinite(n)) {
      return Math.min(PANEL_MAX_W, Math.max(PANEL_MIN_W, n));
    }
  } catch {
    /* 隐私模式读取失败静默 */
  }
  return PANEL_DEFAULT_W;
}

/* ============================== 通用小组件 ============================== */

/** 键值行（.rp-row 契约） */
function Row({ label, value, title }: { label: string; value: ReactNode; title?: string }) {
  return (
    <div className="rp-row" title={title}>
      <span className="rp-label">{label}</span>
      <span className="rp-value">{value}</span>
    </div>
  );
}

/** 面板分区（.rp-section 契约） */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rp-section">
      <div className="rp-section-title">{title}</div>
      {children}
    </section>
  );
}

/**
 * 面板进度条（填充宽度为数据驱动百分比，唯一样式内联点；
 * 轨道/渐变色由 app.css .progress/.progress-bar 令牌驱动，§6.3.2）
 */
function PanelProgress({ percent }: { percent: number }) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(clamped)} aria-valuemin={0} aria-valuemax={100}>
      <div className="progress-bar" style={{ width: `${clamped}%` }} />
    </div>
  );
}

/** MB → GB 文本（无数据显示 --） */
function mbToGb(mb: number | undefined | null): string {
  return mb === null || mb === undefined ? '--' : (mb / 1024).toFixed(1);
}

/** 百分比文本（无数据显示 --） */
function pctText(v: number | undefined | null): string {
  return v === null || v === undefined ? '--' : `${Math.round(v)}%`;
}

/** 秒 → mm:ss 已用时文本 */
function elapsedText(sec: number | undefined): string {
  if (sec === undefined || sec < 0) {
    return '--';
  }
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/* ============================== 1. AI 对话面板（/chat） ============================== */
function ChatPanel() {
  const model = useDialogStore((s) => s.model);
  const modelId = useDialogStore((s) => s.modelId);
  const modelOptions = useDialogStore((s) => s.modelOptions);
  const temperature = useDialogStore((s) => s.temperature);
  const contextTokens = useDialogStore((s) => s.contextTokens);
  const thinking = useDialogStore((s) => s.thinking);
  const setModelId = useDialogStore((s) => s.setModelId);
  const loadDialogModels = useDialogStore((s) => s.loadDialogModels);
  const setTemperature = useDialogStore((s) => s.setTemperature);
  const setContextTokens = useDialogStore((s) => s.setContextTokens);
  const setThinking = useDialogStore((s) => s.setThinking);
  const currentSession = useDialogStore((s) => s.currentSession);
  const messages = useDialogStore((s) => s.messages);
  const generating = useDialogStore((s) => s.generating);

  // 挂载时拉取可选模型清单（含可承载判定；不可承载自动回退）
  useEffect(() => {
    void loadDialogModels();
  }, [loadDialogModels]);

  // 云端模型选项（批1 云端API 2026-09-06）：启用中的连接 × 其模型列表，
  // 选中后 model_id 以 cloud::prov::model 直传后端路由（本地清单失败不影响）
  // 2026-09-10 用户令「对话不应出现视频/图像模型」：按 protocol 过滤——
  // 仅收文本协议（openai_text 等），openai_image/task_image/task_video
  // 的图像/视频模型无法对话，剔除；分组标注「文本对话」
  const [cloudOptions, setCloudOptions] = useState<Array<{ value: string; label: string }>>([]);
  useEffect(() => {
    let cancelled = false;
    listCloudProviders()
      .then((r) => {
        if (cancelled) return;
        const opts: Array<{ value: string; label: string }> = [];
        for (const p of r.providers ?? []) {
          if (!p.enabled) continue;
          if (!(p.protocol ?? '').toLowerCase().includes('text')) continue;
          const models = (p.models ?? []).length > 0 ? p.models : [''];
          for (const m of models) {
            opts.push({
              value: `cloud::${p.id}::${m}`,
              label: m ? `${p.name} · ${m}` : `${p.name} · 默认模型`,
            });
          }
        }
        setCloudOptions(opts);
      })
      .catch(() => {
        /* 云端清单不可用：静默保留本地清单 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 当前选中模型的清单项（清单为空 = 后端未就绪，回退档位展示） */
  const current = modelOptions.find((m) => m.model_id === modelId);
  const isCloudModel = (modelId || '').startsWith('cloud::');
  const currentCloud = cloudOptions.find((o) => o.value === modelId);

  return (
    <>
      <Section title="对话设置">
        <div className="form-row">
          <label className="form-label" htmlFor="rp-chat-model">
            模型选择
            {/* 2026-09-08 用户报「选择失效」：旧档位标签（8B/4B/2B）对
                清外模型（如 9B）错显「4B」，与下拉值互相矛盾=看着像没
                切上。改显所选模型真名；清单未加载时回退旧档位 */}
            <span className="form-value">
              {isCloudModel
                ? (currentCloud?.label ?? '云端')
                : (current?.name.split(' · ')[0] ?? model)}
            </span>
          </label>
          <select
            id="rp-chat-model"
            className="input"
            value={modelOptions.length || cloudOptions.length ? modelId : ''}
            onChange={(e) => setModelId(e.target.value)}
            onBlur={(e) => {
              // 2026-09-08 用户两报「切换无反应」但干净环境（含
              // WebView2 同引擎+真实鼠标）全路径无法复现：兜底防个别
              // 环境 change 事件丢失——失焦时按 DOM 真值对齐 store，
              // 保证「视觉已选」绝不落后于「实际生效」
              if (e.target.value && e.target.value !== modelId) {
                setModelId(e.target.value);
              }
            }}
          >
            {modelOptions.length === 0 && cloudOptions.length === 0 ? (
              <option value="">{model}（清单加载中…）</option>
            ) : (
              <>
                {modelOptions.length > 0 && (
                  <optgroup label="本地模型（本机显卡承载）">
                    {modelOptions.map((m) => (
                      <option key={m.model_id} value={m.model_id} disabled={!m.fits_local}>
                        {m.name} · {m.est_vram_gb}GB{m.loaded ? ' · 已加载' : ''}{m.fits_local ? '' : ' · 超本机显存'}
                      </option>
                    ))}
                  </optgroup>
                )}
                {cloudOptions.length > 0 && (
                  <optgroup label="云端 API · 文本对话（零显存占用）">
                    {cloudOptions.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </optgroup>
                )}
              </>
            )}
          </select>
          <div className="form-hint">
            {isCloudModel
              ? `云端模型${currentCloud ? `：${currentCloud.label}` : ''} · 由服务商承载，本地显卡零占用`
              : current
                ? `${current.name} · 预估 ${current.est_vram_gb}GB${current.loaded ? ' · 已加载' : ' · 首次发送时加载/切换'}`
                : '档位说明：8B 质量最佳 / 4B 均衡 / 2B 速度最快'}
          </div>
        </div>
        <div className="form-row">
          <label className="form-label" htmlFor="rp-chat-temp">
            温度调节
            <span className="form-value">{temperature.toFixed(1)}</span>
          </label>
          <input
            id="rp-chat-temp"
            type="range"
            className="slider"
            min={TEMPERATURE_MIN}
            max={TEMPERATURE_MAX}
            step={0.1}
            value={temperature}
            onChange={(e) => setTemperature(parseFloat(e.target.value))}
          />
          <div className="form-hint">0 严谨确定 ~ 2 发散创造（§6.2.1）</div>
        </div>
        <div className="form-row">
          <label className="form-label" htmlFor="rp-chat-ctx">上下文长度</label>
          <select
            id="rp-chat-ctx"
            className="input"
            value={contextTokens}
            onChange={(e) => setContextTokens(parseInt(e.target.value, 10))}
            onBlur={(e) => {
              const v = parseInt(e.target.value, 10);
              if (Number.isFinite(v) && v !== contextTokens) {
                setContextTokens(v);
              }
            }}
          >
            {CONTEXT_TOKEN_OPTIONS.map((n) => (
              <option key={n} value={n}>{n} tokens</option>
            ))}
          </select>
        </div>
        {/* 深度思考开关（reasoning 双通道：思考过程与最终回答分离流式展示） */}
        <div className="form-row">
          <label className="form-label" htmlFor="rp-chat-thinking">
            深度思考
            <span className="form-value">{thinking ? '开启' : '关闭'}</span>
          </label>
          <button
            id="rp-chat-thinking"
            type="button"
            role="switch"
            aria-checked={thinking}
            onClick={() => setThinking(!thinking)}
            className={[
              'relative w-10 h-5 rounded-full transition-colors duration-200 shrink-0',
              'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)]',
              thinking ? 'bg-[var(--color-primary)]' : 'bg-[var(--color-border)]',
            ].join(' ')}
            title={thinking ? '关闭深度思考' : '开启深度思考'}
          >
            <span
              className={[
                'absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow-sm',
                'transition-transform duration-200',
                thinking ? 'translate-x-5' : 'translate-x-0',
              ].join(' ')}
            />
          </button>
          <div className="form-hint">
            开启后展示完整推理路径：问题分析 → 信息检索 → 方案评估 → 决策依据，思考完成再输出最终回答
          </div>
        </div>
        {/* 参数接线说明（2026-08-31 温度/上下文接入后端后更新）：
            模型/深度思考此前已生效，温度/上下文当日接线，四项全通 */}
        <div className="rp-hint">
          注：模型 / 温度 / 上下文 / 深度思考均随每次发送生效；切换模型将在下次发送时热切换引擎（首次切换约需 0.5-2 分钟加载）。
        </div>
      </Section>
      <Section title="当前会话">
        {/* 对话模型冷启动进度条（2026-08-31 用户需求：右栏常驻可见，
            弹窗被「后台继续」关掉后仍持续显示，就绪即自动收起） */}
        <DialogWarmupBar />
        {currentSession ? (
          <>
            <Row label="标题" value={currentSession.title || '未命名'} title={currentSession.title} />
            <Row label="模式" value={currentSession.mode || '默认'} />
            <Row label="消息数" value={messages.length} />
            <Row
              label="状态"
              value={generating ? '生成中…' : '空闲'}
            />
          </>
        ) : (
          <div className="rp-hint">尚未选择会话。在对话页新建或选择左侧会话后，此处显示会话详情。</div>
        )}
      </Section>
    </>
  );
}

/* ============================== 2. AI 漫画（/paint，2026-09-07 M1 替代绘画页） ==============================
 * 漫画工作台自包含（分格/角色/画风都在主面板内），不设右侧参数面板；
 * 绘画参数面板随 AI 绘画页退役（原 PaintPanel 只读 store 状态展示，
 * 无可迁移的活参数）。AI绘画模型冷启动进度条（PaintWarmupBar）为全局
 * 组件（App 根渲染），漫画页生图预热提示不受本面板移除影响。
 * ============================================================================================== */

/* ============================== 3. 漫剧创作面板（/storyboard） ============================== */

/**
 * COMIC-069 语速/音量滑块（行级配音参数）。
 * 拖动过程仅更新本地草稿保证跟手；松手（pointerup）或方向键提交时
 * 调 updateRow → PUT /manga/storyboard/{pid}/rows/{rowId} 真实持久化
 * （后端校验 speed 0.5~2.0 / volume -12~0dB，越界 40008）。
 * 提交失败如实 toast 并回退到已持久化值，不伪造成功。
 */
function VoiceTuning({ row }: { row: StoryboardRow }) {
  const updateRow = useMangaStore((s) => s.updateRow);
  const showToast = useAppStore((s) => s.showToast);
  const [speed, setSpeed] = useState(row.speed ?? 1.0);
  const [volume, setVolume] = useState(row.volume ?? 0.0);
  const [saving, setSaving] = useState(false);

  const commit = (patch: Partial<StoryboardRow>) => {
    setSaving(true);
    updateRow(row.id, patch)
      .catch((err) => {
        const msg =
          err && typeof err === 'object' && 'message' in err
            ? (err as { message: string }).message
            : '配音参数保存失败';
        showToast(msg, 'error');
        // 回退到已持久化值（ truthful：滑块反映真实落库状态）
        setSpeed(row.speed ?? 1.0);
        setVolume(row.volume ?? 0.0);
      })
      .finally(() => setSaving(false));
  };

  return (
    <>
      <div className="form-row">
        <label className="form-label" htmlFor="rp-row-speed">
          语速
          <span className="form-value">{speed.toFixed(2)}×</span>
        </label>
        <input
          id="rp-row-speed"
          data-testid="rp-row-speed"
          type="range"
          className="slider"
          min={0.5}
          max={2.0}
          step={0.05}
          value={speed}
          disabled={saving}
          onChange={(e) => setSpeed(parseFloat(e.target.value))}
          onPointerUp={() => void commit({ speed })}
          onKeyUp={(e) => {
            if (e.key.startsWith('Arrow')) void commit({ speed });
          }}
        />
        <div className="form-hint">0.5~2.0 倍，松手即保存到分镜行</div>
      </div>
      <div className="form-row">
        <label className="form-label" htmlFor="rp-row-volume">
          音量
          <span className="form-value">{volume.toFixed(1)} dB</span>
        </label>
        <input
          id="rp-row-volume"
          data-testid="rp-row-volume"
          type="range"
          className="slider"
          min={-12}
          max={0}
          step={0.5}
          value={volume}
          disabled={saving}
          onChange={(e) => setVolume(parseFloat(e.target.value))}
          onPointerUp={() => void commit({ volume })}
          onKeyUp={(e) => {
            if (e.key.startsWith('Arrow')) void commit({ volume });
          }}
        />
        <div className="form-hint">-12~0 dB，松手即保存到分镜行</div>
      </div>
    </>
  );
}

function StoryboardPanel() {
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const videoTasks = useMangaStore((s) => s.videoTasks);
  const videoGenerating = useMangaStore((s) => s.videoGenerating);
  // 行级选中联动（COMIC-020）：分镜表镜号点击后此处展示该行属性
  const selectedRowId = useMangaStore((s) => s.selectedRowId);
  const selectedRow = rows.find((r) => r.id === selectedRowId) ?? null;

  // 场景列表：从分镜行去重（真实数据）
  const scenes = Array.from(new Set(rows.map((r) => r.scene).filter(Boolean)));
  const aiRows = rows.filter((r) => r.is_ai_generated).length;
  // 角色列表：后端无角色端点，由分镜行 characters 去重派生（诚实门控）；
  // 配音状态 = 任一含该角色的分镜行已填写 voice_id
  const characterNames = Array.from(
    new Set(rows.flatMap((r) => r.characters).filter(Boolean)),
  );
  const voiceOf = (name: string) =>
    rows.find((r) => r.characters.includes(name) && r.voice_id)?.voice_id || '';

  return (
    <>
      <Section title="分镜属性">
        {currentProject ? (
          <>
            <Row label="项目" value={currentProject.name} title={currentProject.name} />
            <Row label="分镜总数" value={rows.length} />
            <Row label="AI 生成行" value={aiRows} />
            <Row
              label="视频任务"
              value={videoGenerating ? '生成中…' : `${videoTasks.length} 个`}
            />
          </>
        ) : (
          <div className="rp-hint">尚未加载分镜数据。打开漫剧页后，此处显示默认分镜表的属性（当前版本不提供多项目管理）。</div>
        )}
      </Section>
      <Section title="选中分镜">
        {selectedRow ? (
          <>
            <Row label="镜号" value={`#${selectedRow.shot_number}`} />
            <Row
              label="原始台词"
              value={selectedRow.original_dialogue || '—'}
              title={selectedRow.original_dialogue || undefined}
            />
            <Row
              label="画面描述"
              value={selectedRow.description || '—'}
              title={selectedRow.description || undefined}
            />
            <Row
              label="出场角色"
              value={selectedRow.characters.length ? selectedRow.characters.join('、') : '—'}
              title={selectedRow.characters.join('、') || undefined}
            />
            <Row label="场景" value={selectedRow.scene || '—'} title={selectedRow.scene || undefined} />
            <Row
              label="道具"
              value={selectedRow.props.length ? selectedRow.props.join('、') : '—'}
              title={selectedRow.props.join('、') || undefined}
            />
            <Row label="配音音色" value={selectedRow.voice_id || '未绑定'} />
            <Row label="情感标签" value={selectedRow.voice_emotion || '—'} />
            {/* COMIC-069：语速/音量滑块（key 按行切换重置草稿） */}
            <VoiceTuning key={selectedRow.id} row={selectedRow} />
            <Row label="来源" value={selectedRow.is_ai_generated ? 'AI 生成' : '手动添加'} />
          </>
        ) : (
          <div className="rp-hint">未选中分镜行。在分镜表中点击镜号，此处联动显示该行属性。</div>
        )}
      </Section>
      <Section title="角色设置">
        {characterNames.length > 0 ? (
          characterNames.map((name) => (
            <Row key={name} label={name} value={voiceOf(name) ? '已配音色' : '未配音色'} title={name} />
          ))
        ) : (
          <div className="rp-hint">当前项目暂无角色资产。角色由 AI 切分分镜或手动添加后显示。</div>
        )}
      </Section>
      <Section title="场景参数">
        {scenes.length > 0 ? (
          scenes.map((scene) => (
            <Row
              key={scene}
              label={scene}
              value={`${rows.filter((r) => r.scene === scene).length} 镜`}
              title={scene}
            />
          ))
        ) : (
          <div className="rp-hint">暂无场景数据。分镜行填写场景字段后在此汇总。</div>
        )}
      </Section>
    </>
  );
}

/* ============================== 4. 知识学习面板（/learning，5s 轮询） ============================== */

function LearningPanel() {
  const [session, setSession] = useState<learningApi.LearnSessionStatus | null>(null);
  const [knowledgeCount, setKnowledgeCount] = useState<number | null>(null);
  const [loraList, setLoraList] = useState<Array<{ name: string; ready: boolean }>>([]);

  // 5s 轮询：学习会话状态 + 知识库统计 + 学习模型（LoRA）清单
  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const s = await learningApi.getSessionStatus();
        if (!cancelled) {
          setSession(s);
        }
      } catch {
        /* 后端未就绪保持上次快照 */
      }
      try {
        const k = await learningApi.getKnowledgeStats();
        if (!cancelled) {
          setKnowledgeCount(k.total ?? null);
        }
      } catch {
        /* 同上 */
      }
      try {
        const models = await listLearnModels();
        if (!cancelled) {
          setLoraList(models.map((m) => ({ name: m.name, ready: m.ready })));
        }
      } catch {
        /* 同上 */
      }
    }

    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const statusText = session ? LEARN_SESSION_STATUS_LABELS[session.status] ?? session.status : '--';
  const progressPct =
    session && session.max_pages
      ? Math.round(((session.pages_visited ?? 0) / session.max_pages) * 100)
      : null;

  return (
    <>
      <Section title="学习进度">
        <Row label="状态" value={statusText} />
        <Row label="主题" value={session?.topic || '--'} title={session?.topic} />
        <Row
          label="浏览进度"
          value={
            session?.max_pages
              ? `${session.pages_visited ?? 0}/${session.max_pages} 页`
              : '--'
          }
        />
        {progressPct !== null && <PanelProgress percent={progressPct} />}
        <Row label="已提取知识点" value={session?.knowledge_extracted ?? '--'} />
        <Row label="已用时" value={elapsedText(session?.elapsed_sec)} />
      </Section>
      <Section title="知识详情">
        <Row label="知识库条目" value={knowledgeCount === null ? '--' : knowledgeCount.toLocaleString()} />
        <div className="rp-hint">条目数来自 ChromaDB 统计（GET /v1/learn/knowledge/stats），5 秒刷新。</div>
      </Section>
      <Section title="LoRA 版本">
        {loraList.length > 0 ? (
          loraList.map((m) => (
            <Row key={m.name} label={m.name} value={m.ready ? '可训练' : '未就绪'} title={m.name} />
          ))
        ) : (
          <div className="rp-hint">暂无学习模型记录。完成一轮知识学习微调后，LoRA 版本将出现在此处。</div>
        )}
      </Section>
    </>
  );
}

/* ============================== 5. 模型管理面板（/models） ============================== */

function ModelsPanel() {
  const models = useModelStore((s) => s.models);
  const loaded = useModelStore((s) => s.loaded);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const selectedModels = useModelStore((s) => s.selectedModels);
  const realtime = useHardwareStore((s) => s.realtime);
  const activeModel = useHardwareStore((s) => s.activeModel);
  const cachedCount = useHardwareStore((s) => s.cachedCount);

  // 进入面板时确保模型清单已加载
  useEffect(() => {
    if (!useModelStore.getState().loaded) {
      fetchModels();
    }
  }, [fetchModels]);

  const downloadedCount = models.filter((m) => m.downloaded).length;
  const loadedCount = models.filter((m) => m.loaded).length;
  const vramUsed = realtime?.vram_used_mb;
  const vramTotal = realtime?.vram_total_mb;
  const vramPct =
    vramUsed !== undefined && vramTotal ? Math.round((vramUsed / vramTotal) * 100) : null;
  const selectedEntries = Object.entries(selectedModels);

  return (
    <>
      <Section title="模型详情">
        {loaded ? (
          <>
            <Row label="注册模型" value={models.length} />
            <Row label="已下载" value={downloadedCount} />
            <Row label="已加载" value={loadedCount} />
            {selectedEntries.length > 0 ? (
              selectedEntries.map(([feature, modelId]) => (
                <Row key={feature} label={`${feature} 当前`} value={modelId} title={modelId} />
              ))
            ) : (
              <div className="rp-hint">各功能尚未指定模型，由后端按显存自动调度。</div>
            )}
          </>
        ) : (
          <div className="rp-hint">模型清单加载中…</div>
        )}
      </Section>
      <Section title="显存占用">
        <Row
          label="VRAM"
          value={vramTotal ? `${mbToGb(vramUsed)}/${mbToGb(vramTotal)} GB` : '--'}
        />
        {vramPct !== null && <PanelProgress percent={vramPct} />}
        <Row label="使用率" value={pctText(realtime?.vram_percent)} />
      </Section>
      <Section title="加载状态">
        <Row label="常驻模型" value={activeModel} title={activeModel} />
        <Row label="缓存模型数" value={cachedCount} />
        <div className="rp-hint">加载状态来自协同调度快照（GET /v1/hardware/synergy），2 秒刷新。</div>
      </Section>
    </>
  );
}

/* ============================== 6. 视频风格面板（/style） ============================== */

function StylePanel() {
  const rank = useStyleStore((s) => s.rank);
  const alpha = useStyleStore((s) => s.alpha);
  const epochs = useStyleStore((s) => s.epochs);
  const setRank = useStyleStore((s) => s.setRank);
  const setAlpha = useStyleStore((s) => s.setAlpha);
  const setEpochs = useStyleStore((s) => s.setEpochs);
  const task = useStyleStore((s) => s.task);
  const training = useStyleStore((s) => s.training);
  const versions = useStyleStore((s) => s.versions);
  const versionsLoaded = useStyleStore((s) => s.versionsLoaded);
  const fetchVersions = useStyleStore((s) => s.fetchVersions);
  const datasetName = useStyleStore((s) => s.datasetName);

  // 进入面板时确保 LoRA 版本列表已加载
  useEffect(() => {
    if (!useStyleStore.getState().versionsLoaded) {
      fetchVersions();
    }
  }, [fetchVersions]);

  const currentVersion = versions.find((v) => v.is_current) ?? versions[0];
  const taskProgressPct = task ? Math.round(task.progress * 100) : null;

  return (
    <>
      <Section title="风格参数">
        <div className="form-row">
          <label className="form-label" htmlFor="rp-style-rank">LoRA Rank</label>
          <select
            id="rp-style-rank"
            className="input"
            value={rank}
            onChange={(e) => setRank(parseInt(e.target.value, 10))}
          >
            {[8, 16, 32, 64].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
        <div className="form-row">
          <label className="form-label" htmlFor="rp-style-alpha">LoRA Alpha</label>
          <select
            id="rp-style-alpha"
            className="input"
            value={alpha}
            onChange={(e) => setAlpha(parseInt(e.target.value, 10))}
          >
            {[16, 32, 64, 128].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
        <div className="form-row">
          <label className="form-label" htmlFor="rp-style-epochs">
            训练轮次
            <span className="form-value">{epochs}</span>
          </label>
          <input
            id="rp-style-epochs"
            type="range"
            className="slider"
            min={1}
            max={50}
            step={1}
            value={epochs}
            onChange={(e) => setEpochs(parseInt(e.target.value, 10))}
          />
        </div>
      </Section>
      <Section title="LoRA 训练进度">
        {task ? (
          <>
            <Row label="任务" value={task.name} title={task.name} />
            <Row label="状态" value={TRAIN_STATUS_LABELS[task.status] ?? task.status} />
            {taskProgressPct !== null && <PanelProgress percent={taskProgressPct} />}
            <Row label="进度" value={`${taskProgressPct ?? 0}%`} />
          </>
        ) : (
          <div className="rp-hint">
            {training ? '训练任务初始化中…' : '暂无训练任务。在风格页上传素材并发起训练后，此处实时显示进度。'}
          </div>
        )}
      </Section>
      <Section title="预览 / 版本">
        <Row label="训练素材" value={datasetName || '未上传'} title={datasetName ?? undefined} />
        {versionsLoaded && versions.length > 0 ? (
          <>
            <Row label="版本数" value={versions.length} />
            <Row
              label="当前版本"
              value={currentVersion ? currentVersion.name : '--'}
              title={currentVersion?.name}
            />
          </>
        ) : (
          <div className="rp-hint">暂无风格 LoRA 版本。风格化预览帧在风格页主区「风格预览」区块展示（需已有训练版本）。</div>
        )}
      </Section>
    </>
  );
}

/* ============================== 路由 → 面板注册表（规格 §6.1.3） ============================== */

interface PanelMeta {
  /** 面板标题 */
  title: string;
  /** 标题图标（Lucide，§6.3.4） */
  icon: LucideIcon;
  /** 面板内容组件 */
  render: () => ReactNode;
}

/** 键为一级路由名；settings/help 故意缺席（无右侧面板） */
const PANEL_BY_ROUTE: Record<string, PanelMeta> = {
  chat: { title: '对话设置', icon: MessageSquare, render: () => <ChatPanel /> },
  storyboard: { title: '分镜属性', icon: Clapperboard, render: () => <StoryboardPanel /> },
  learning: { title: '学习状态', icon: BookOpen, render: () => <LearningPanel /> },
  models: { title: '模型状态', icon: Package, render: () => <ModelsPanel /> },
  style: { title: '风格参数', icon: Video, render: () => <StylePanel /> },
};

/** 从路径名提取一级路由名（/chat/xxx → chat） */
function firstSegment(pathname: string): string {
  const seg = pathname.replace(/^\/+/, '').split('/')[0];
  return seg || 'chat';
}

/* ============================== 右侧面板（导出） ============================== */

/**
 * 右侧面板容器：按路由动态渲染对应面板；设置/帮助路由整体隐藏。
 * 折叠态持久化到 localStorage；边缘圆形按钮切换展开/收起（200ms 宽度过渡，§6.3.3）。
 * SYS-020：面板左缘拖拽手柄调整宽度（240~520px，localStorage 持久化，折叠时隐藏手柄）。
 */
export function RightPanel() {
  const location = useLocation();
  const route = firstSegment(location.pathname);
  const meta = PANEL_BY_ROUTE[route];

  const [collapsed, setCollapsed] = useState<boolean>(loadCollapsed);
  const [width, setWidth] = useState<number>(loadWidth);
  const [resizing, setResizing] = useState(false);

  // 折叠态持久化（读取已在 useState 初始化完成，这里只写）
  useEffect(() => {
    try {
      localStorage.setItem(PANEL_COLLAPSED_KEY, collapsed ? '1' : '0');
      mirrorPref(PANEL_COLLAPSED_KEY, collapsed); // 界面偏好镜像
    } catch {
      /* 隐私模式写入失败静默 */
    }
  }, [collapsed]);

  // 宽度持久化（SYS-020）
  useEffect(() => {
    try {
      localStorage.setItem(PANEL_WIDTH_KEY, String(width));
      mirrorPref(PANEL_WIDTH_KEY, width); // 界面偏好镜像
    } catch {
      /* 隐私模式写入失败静默 */
    }
  }, [width]);

  /** SYS-020：拖拽调宽（指针向左拖 → 增宽；window 级监听保证移出手柄仍跟随） */
  const onGripPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    setResizing(true);
    const onMove = (ev: PointerEvent) => {
      const next = startW + (startX - ev.clientX);
      setWidth(Math.min(PANEL_MAX_W, Math.max(PANEL_MIN_W, next)));
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  // 无面板路由：不渲染任何占位（主内容自然占满）
  if (!meta) {
    return null;
  }

  const Icon = meta.icon;

  return (
    <>
      {/* 面板左缘折叠按钮（跨边界悬浮，面板收起时仍可见可点） */}
      <div className="right-panel-edge">
        <button
          type="button"
          className="right-panel-toggle"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? '展开右侧面板' : '收起右侧面板'}
          aria-label={collapsed ? '展开右侧面板' : '收起右侧面板'}
          aria-expanded={!collapsed}
        >
          {collapsed ? <ChevronLeft size={12} /> : <ChevronRight size={12} />}
        </button>
      </div>
      <aside
        className={`right-panel${collapsed ? ' collapsed' : ''}${resizing ? ' resizing' : ''}`}
        style={collapsed ? undefined : { width }}
        aria-label={meta.title}
      >
        {/* SYS-020：左缘拖拽调宽手柄（折叠态不渲染） */}
        {!collapsed && (
          <div
            className="right-panel-resize-grip"
            role="separator"
            aria-orientation="vertical"
            aria-label="拖拽调整面板宽度"
            title="拖拽调整面板宽度"
            onPointerDown={onGripPointerDown}
          />
        )}
        <div className="right-panel-inner" style={collapsed ? undefined : { width }}>
          <header className="right-panel-header">
            <Icon size={16} aria-hidden="true" />
            <span>{meta.title}</span>
          </header>
          <div className="right-panel-body">{meta.render()}</div>
        </div>
      </aside>
    </>
  );
}

export default RightPanel;
// 本项目仅供学习使用，商业授权请+Q 3559331368
