// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * MessageBubble 消息气泡
 * OmniSpace AI v2.5.0 — Sakura 主题（暗色默认 / 亮色可选，全令牌化）
 * --------------------------------------------------------------------------
 * 用户 / 助手不同样式；助手消息采用 AI 生成内容标记底色（--color-ai-bg，约束 §14，
 * 由 tokens.css 等效替换浅色 #E3F2FD，随主题切换）。
 * 显示模型名与时间；Markdown 由 react-markdown + remark-gfm（表格/删除线/任务列表）
 * + rehype-highlight（代码高亮，hljs-* 令牌化样式见 app.css）渲染。
 * 流式输出期间渲染纯文本 + 光标（避免逐 token 全量解析抖动），完成后切换 Markdown。
 * 规格 §9.2 对话模块。
 */
import { memo, useEffect, useRef, useState } from 'react';
import {
  User,
  Flower2,
  AlertCircle,
  Brain,
  ChevronDown,
  ChevronRight,
  Database,
  FileSearch,
  Globe,
  Lightbulb,
  Scale,
  type LucideIcon,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Modal } from '../common/Modal';
import type { WebRef } from '../../types';
import { useDialogStore } from '../../stores/useDialogStore';

/** 消息角色 */
export type MessageRole = 'user' | 'assistant' | 'system';

/** 对话消息 */
export interface ChatMessage {
  /** 消息 ID */
  id: string;
  /** 角色 */
  role: MessageRole;
  /** 消息内容 */
  content: string;
  /** 时间戳（毫秒） */
  timestamp?: number;
  /** 生成该消息的模型名 */
  model?: string;
  /** 是否正在流式输出 */
  streaming?: boolean;
  /** 深度思考过程（reasoning 双通道；与 content 分离渲染，四步框架分段展示） */
  reasoning?: string;
  /** 用户评分（1 赞 / -1 踩 / 0 未评；历史回放带出，批5 反馈闭环） */
  rating?: number;
  /** 思考耗时毫秒（store 首 token 帧定格；历史回放无） */
  reasoningMs?: number;
  /** 错误文案 */
  errorText?: string;
  /** 引用消息 */
  quote?: { id: string; content: string } | null;
  /** 用户消息附带图片（多模态，dataUrl 格式） */
  images?: string[];
  /** 联网搜索来源（web_refs 事件；回答带【n】引用标注，架构升级计划 B-阶段一） */
  web_refs?: WebRef[];
}

export interface MessageBubbleProps {
  /** 消息数据 */
  message: ChatMessage;
  /** 引用回调 */
  onQuote?: (message: ChatMessage) => void;
  /** 复制回调 */
  onCopy?: (message: ChatMessage) => void;
  /** 重新生成回调（仅助手消息） */
  onRegenerate?: (message: ChatMessage) => void;
  /** 评分回调（仅助手消息，1 赞 / -1 踩；批5 反馈闭环：踩→引用知识降权） */
  onRate?: (message: ChatMessage, rating: number) => void;
}

/** 格式化时间 HH:MM */
function formatTime(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts);
  const p = (n: number) => (n < 10 ? `0${n}` : `${n}`);
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 四步框架步骤名 → 语义图标（未知标记用 ChevronRight 兜底） */
const STEP_ICONS: Record<string, LucideIcon> = {
  问题分析: FileSearch,
  信息检索: Database,
  方案评估: Scale,
  决策依据: Lightbulb,
};

/** 思考步骤（【步骤名】标记解析产物；title 为空 = 无标记头部内容） */
interface ThinkingStep {
  title: string;
  body: string;
}

/** 【步骤名】标记 → 结构化步骤序列（结构容忍：无标记时整体作正文） */
function parseThinkingSteps(reasoning: string): ThinkingStep[] {
  const parts = reasoning.split(/(【[^】]*】)/).filter((s) => s.length > 0);
  const steps: ThinkingStep[] = [];
  let current: ThinkingStep | null = null;
  for (const part of parts) {
    if (part.startsWith('【') && part.endsWith('】')) {
      if (current) steps.push(current);
      current = { title: part.slice(1, -1), body: '' };
    } else {
      if (!current) current = { title: '', body: '' };
      current.body += part;
    }
  }
  if (current) steps.push(current);
  return steps;
}

/** 耗时格式化：<60s 显秒，否则 M 分 N 秒 */
function formatThinkingDuration(ms: number): string {
  const s = Math.max(1, Math.round(ms / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

/**
 * 深度思考面板（reasoning 双通道渲染端）。
 * - 流式期间自动展开 + 尾部光标 + 自动跟随滚动（用户上滚暂停、滚回底部恢复）；
 *   正文开始输出或流式结束自动折叠一次，用户手动切换后不再干预（意图优先）
 * - 展开/折叠用 grid-template-rows 0fr↔1fr 过渡（动画铁律：禁 height/margin 动画）
 * - 【步骤名】解析为结构化步骤，竖向时间线渲染（节点图标 + 序号 + 连接线，
 *   进行中步骤呼吸高亮）；无标记时整体作为正文渲染
 * - 折叠态摘要：思考中显示步骤进度；完成显示 步数 · 耗时
 *   （耗时由 store 在首 token 帧定格写入消息；历史回放无耗时 → 字数兜底）
 */
function ThinkingPanel({ reasoning, streaming, hasContent, reasoningMs }: {
  reasoning: string;
  streaming: boolean;
  hasContent: boolean;
  reasoningMs?: number;
}) {
  const thinking = streaming && !hasContent;
  const [expanded, setExpanded] = useState(thinking);
  // 用户手动切换后，自动折叠不再干预（修复旧实现注释与行为不符）
  const userToggledRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

  // 思考完成（正文开始输出 或 流式结束）自动折叠一次
  useEffect(() => {
    if (!userToggledRef.current && !thinking) {
      setExpanded(false);
    }
  }, [thinking]);

  // 流式自动跟随滚动到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el && thinking && stickRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [reasoning, thinking]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // 距底 <32px 视为"贴底"，恢复跟随；上滚即暂停
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  };

  const steps = parseThinkingSteps(reasoning);
  const titledCount = steps.filter((s) => s.title).length;
  const activeStep = thinking ? steps[steps.length - 1] : null;
  const doneSummary = [
    titledCount > 0 ? `${titledCount} 步` : '',
    reasoningMs != null ? formatThinkingDuration(reasoningMs) : `${reasoning.length} 字`,
  ].filter(Boolean).join(' · ');

  return (
    <div className="w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-ai-bg)]/60 mb-1 overflow-hidden">
      <button
        type="button"
        onClick={() => {
          setExpanded((v) => !v);
          userToggledRef.current = true;
        }}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 w-full px-3 py-2 text-xs text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] transition-colors"
        title={expanded ? '收起思考过程' : '展开思考过程'}
      >
        <Brain size={13} aria-hidden="true" className="shrink-0 text-[var(--color-primary)]" />
        <span className="font-medium shrink-0">
          {thinking ? '深度思考中' : '已深度思考'}
        </span>
        <span className="min-w-0 flex-1 truncate text-left text-[var(--color-text-tertiary)]">
          {thinking && activeStep?.title
            ? `第 ${steps.length} 步 · ${activeStep.title}`
            : doneSummary}
        </span>
        {thinking ? (
          <span className="inline-flex gap-0.5 shrink-0" aria-hidden="true">
            <span className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce" style={{ animationDelay: '0ms' }} />
            <span className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce" style={{ animationDelay: '150ms' }} />
            <span className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce" style={{ animationDelay: '300ms' }} />
          </span>
        ) : null}
        <ChevronDown
          size={13}
          aria-hidden="true"
          className={[
            'shrink-0 transition-transform duration-200',
            expanded ? 'rotate-180' : '',
          ].join(' ')}
        />
      </button>
      <div
        className={[
          'grid transition-[grid-template-rows] duration-300 ease-in-out',
          expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
        ].join(' ')}
      >
        <div className="overflow-hidden">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="px-3 pb-2.5 pt-0.5 max-h-72 overflow-y-auto text-xs"
          >
            <ol className="m-0 p-0 list-none">
              {steps.map((step, i) => {
                const isLast = i === steps.length - 1;
                const active = thinking && isLast;
                const Icon = step.title ? STEP_ICONS[step.title] ?? ChevronRight : ChevronRight;
                return (
                  <li key={i} className="relative pl-6 pb-2.5 last:pb-0">
                    {/* 时间线连接线（非最后一步） */}
                    {!isLast ? (
                      <span
                        className="absolute left-2 top-5 bottom-0 w-px bg-[var(--color-border)]"
                        aria-hidden="true"
                      />
                    ) : null}
                    {/* 节点（进行中呼吸高亮） */}
                    <span
                      className={[
                        'absolute left-0 top-0.5 flex items-center justify-center w-4 h-4 rounded-full border bg-[var(--color-bg-base)]',
                        active
                          ? 'border-[var(--color-primary)] text-[var(--color-primary)] animate-pulse'
                          : 'border-[var(--color-border)] text-[var(--color-text-tertiary)]',
                      ].join(' ')}
                      aria-hidden="true"
                    >
                      <Icon size={9} />
                    </span>
                    {step.title ? (
                      <div className="flex items-center gap-1.5 font-medium text-[var(--color-text-secondary)]">
                        <span className="tabular-nums text-[var(--color-text-tertiary)]">
                          {String(i + 1).padStart(2, '0')}
                        </span>
                        <span>{step.title}</span>
                        {active ? (
                          <span
                            className="w-1 h-1 rounded-full bg-[var(--color-primary)] animate-pulse"
                            aria-hidden="true"
                          />
                        ) : null}
                      </div>
                    ) : null}
                    {step.body ? (
                      <div className="mt-0.5 text-[var(--color-text-secondary)] leading-relaxed whitespace-pre-wrap break-words">
                        {step.body}
                        {active ? (
                          <span className="inline-block w-1.5 h-3 ml-0.5 bg-[var(--color-primary)] animate-pulse align-middle" />
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 审计 R3-FE1：React.memo 包裹（具名函数便于 devtools），阻断流式期间
 * store 每 token 重建 messages 数组导致的全列表重渲染（全量重跑 Markdown 解析）。
 * 自定义比较：message 按字段值比较（流式 token 仅触发内容变化的那条重渲染），
 * 回调按引用比较（父级须以 useCallback 稳定传入，见 DialogPage）。
 */

/** 2026-09-07 文档附件标记块：[📎 文档 x]…[/📎 文档 x]（DialogView.send 拼装） */
const DOC_BLOCK_RE = /\[📎 文档 (.+?)\]\n([\s\S]*?)\[\/📎 文档 \1\]/g;

/** 提取用户消息里的文档块（name + 内容） */
function extractDocBlocks(content: string): Array<{ name: string; text: string }> {
  const out: Array<{ name: string; text: string }> = [];
  for (const m of content.matchAll(DOC_BLOCK_RE)) {
    out.push({ name: m[1], text: m[2] });
  }
  return out;
}

/** 剥掉文档块后的正文（无块时原样返回） */
function stripDocBlocks(content: string): string {
  return content.replace(DOC_BLOCK_RE, '').trim();
}

export const MessageBubble = memo(function MessageBubble({
  message,
  onQuote,
  onCopy,
  onRegenerate,
  onRate,
}: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isError = !!message.errorText;
  const [viewerIdx, setViewerIdx] = useState<number | null>(null);
  const images = isUser ? message.images ?? [] : [];
  // 2026-09-07 文档附件：用户消息中的 📎 标记块折叠展示（正文剥离后渲染）
  const docBlocks = isUser ? extractDocBlocks(message.content) : [];
  const bubbleContent = docBlocks.length > 0 ? stripDocBlocks(message.content) : message.content;
  // 批5 反馈闭环：本消息评分（1 赞 / -1 踩 / 0 未评），本地视觉态
  const [myRating, setMyRating] = useState<number>(message.rating ?? 0);
  // 2026-09-08 用户报「对话的 AI 跟模型选择的不符」：根因=选的模型
  // 显存装不下被自动降级，但只有气泡上一行灰色技术 id、零解释。
  // 模型真身（流内 meta.engine / 历史 model_used）改用友好名展示，
  // 与当前所选不一致时给警示色+降级说明——不让大家猜
  const selectedModelId = useDialogStore((s) => s.modelId);
  const modelOptions = useDialogStore((s) => s.modelOptions);
  const displayModel = !isUser && message.model
    ? modelOptions.find((o) => o.model_id === message.model)?.name ?? message.model
    : '';
  const modelMismatch = !isUser && !!message.model && message.model !== selectedModelId;

  const handleRate = (rating: number) => {
    const next = myRating === rating ? 0 : rating; // 重复点击=取消评分
    setMyRating(next);
    onRate?.(message, next);
  };

  return (
    <div className={['flex gap-3', isUser ? 'flex-row-reverse' : 'flex-row'].join(' ')}>
      {/* 头像 */}
      <div
        className={[
          'w-8 h-8 rounded-full flex items-center justify-center text-sm shrink-0',
          isUser ? 'bg-sakura-100' : 'bg-[var(--color-ai-bg)]',
        ].join(' ')}
        aria-hidden="true"
      >
        {isUser ? (
          <User size={16} aria-hidden="true" className="text-sakura-600" />
        ) : (
          <Flower2 size={16} aria-hidden="true" className="text-[var(--color-primary)]" />
        )}
      </div>

      {/* 气泡主体 */}
      <div className={['flex flex-col max-w-[80%]', isUser ? 'items-end' : 'items-start'].join(' ')}>
        {/* 模型名 / 时间（助手侧=实际答复模型真身；降级=警示色标注） */}
        <div className="flex items-center gap-2 mb-1 text-xs text-[var(--color-text-tertiary)]">
          {!isUser && displayModel ? (
            <span
              className={`font-medium max-w-64 truncate ${modelMismatch ? 'text-[var(--color-warning)]' : ''}`}
              title={modelMismatch
                ? `本条由「${displayModel}」答复：您选的模型在当前显存余量下装不下，已自动降级。关闭占用显存的应用后重新选择即可换回`
                : `答复模型：${displayModel}`}
            >
              {displayModel}{modelMismatch ? '（自动降级）' : ''}
            </span>
          ) : null}
          <span>{formatTime(message.timestamp)}</span>
        </div>

        {/* 引用块 */}
        {message.quote ? (
          <blockquote className="mb-1 pl-2 border-l-2 border-sakura-300 text-xs text-[var(--color-text-secondary)] truncate max-w-full">
            {message.quote.content}
          </blockquote>
        ) : null}

        {/* 深度思考面板（reasoning 双通道；仅助手消息且有思考内容时渲染） */}
        {!isUser && message.reasoning ? (
          <ThinkingPanel
            reasoning={message.reasoning}
            streaming={!!message.streaming}
            hasContent={!!message.content}
            reasoningMs={message.reasoningMs}
          />
        ) : null}

        {/* 附件图片缩略图（多模态，CHAT-008/009/012 上传链路的展示端） */}
        {images.length > 0 ? (
          <div className="flex flex-wrap gap-2 justify-end mb-1 max-w-full">
            {images.map((src, i) => (
              <button
                key={i}
                type="button"
                onClick={() => setViewerIdx(i)}
                className="shrink-0 rounded-xl overflow-hidden border border-[var(--color-border)] hover:opacity-90 focus-visible:opacity-90 transition-opacity"
                title={`查看图片 ${i + 1}/${images.length}`}
                aria-label={`查看附件图片 ${i + 1}`}
              >
                <img
                  src={src}
                  alt={`附件图片 ${i + 1}`}
                  className="w-28 h-28 object-cover"
                  loading="lazy"
                />
              </button>
            ))}
          </div>
        ) : null}

        {/* 文档附件折叠块（2026-09-07：📎 标记块折叠展示，点开看全文） */}
        {docBlocks.length > 0 ? (
          <div className="flex flex-col gap-1.5 mb-1 max-w-full items-end">
            {docBlocks.map((d, i) => (
              <details
                key={i}
                className="group w-full rounded-xl border border-sakura-300 bg-sakura-50/60 text-xs"
              >
                <summary className="flex items-center gap-1.5 px-2.5 py-1.5 cursor-pointer select-none text-[var(--color-text-secondary)]">
                  <span aria-hidden="true">📎</span>
                  <span className="font-medium truncate max-w-52">{d.name}</span>
                  <span className="text-[10px] text-[var(--color-text-tertiary)] shrink-0">
                    {d.text.length >= 1000 ? `${(d.text.length / 1000).toFixed(1)}k` : d.text.length}字文档
                  </span>
                  <span className="ml-auto text-[10px] text-sakura-500 shrink-0 group-open:hidden">展开</span>
                  <span className="ml-auto hidden text-[10px] text-sakura-500 shrink-0 group-open:inline">收起</span>
                </summary>
                <pre className="px-3 pb-2 pt-0.5 max-h-64 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-[var(--color-text-secondary)]">{d.text}</pre>
              </details>
            ))}
          </div>
        ) : null}

        {/* 气泡（纯图片消息不渲染空气泡；深度思考进行中由思考面板承担状态展示，
            不再渲染重复的"思考中"占位气泡） */}
        {bubbleContent || (message.streaming && !message.reasoning) || isError ? (
        <div
          className={[
            'px-4 py-2.5 rounded-2xl text-sm text-[var(--color-text-primary)] break-words',
            isError
              ? 'bg-[var(--color-error-bg)] text-[var(--color-error)]'
              : isUser
                ? 'bg-sakura-500 text-white rounded-tr-sm'
                : 'bg-[var(--color-ai-bg)] rounded-tl-sm', // 约束 §14：AI 生成内容标记底色（令牌化）
          ].join(' ')}
        >
          {isError ? (
            <span className="inline-flex items-center gap-1.5"><AlertCircle size={14} aria-hidden="true" /> {message.errorText}</span>
          ) : bubbleContent ? (
            message.streaming ? (
              /* 流式期间：纯文本 + 光标，避免逐 token 全量 Markdown 解析 */
              <span className="leading-relaxed whitespace-pre-wrap break-words">
                {bubbleContent}
                <span className="inline-block w-1.5 h-4 ml-0.5 bg-[var(--color-primary)] animate-pulse align-middle" />
              </span>
            ) : (
              /* 完成态：完整 Markdown（GFM 表格/删除线/任务列表 + 代码高亮） */
              <div className="md-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                  {bubbleContent}
                </ReactMarkdown>
              </div>
            )
          ) : message.streaming ? (
            <span className="inline-flex items-center gap-1 text-[var(--color-text-secondary)]">
              思考中
              <span className="inline-flex gap-0.5">
                <span
                  className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce"
                  style={{ animationDelay: '0ms' }}
                />
                <span
                  className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce"
                  style={{ animationDelay: '150ms' }}
                />
                <span
                  className="w-1 h-1 rounded-full bg-[var(--color-text-tertiary)] animate-bounce"
                  style={{ animationDelay: '300ms' }}
                />
              </span>
            </span>
          ) : null}
        </div>
        ) : null}

        {/* 联网来源卡片（web_refs 事件；仅助手消息，与回答中的【n】引用
            标注对应——点击打开原文，架构升级计划 B-阶段一） */}
        {!isUser && message.web_refs && message.web_refs.length > 0 ? (
          <div className="mt-1 w-full">
            <div className="flex items-center gap-1 mb-1 text-xs text-[var(--color-text-tertiary)]">
              <Globe size={12} aria-hidden="true" />
              联网来源（{message.web_refs.length}）
            </div>
            <div className="flex flex-col gap-1">
              {message.web_refs.slice(0, 5).map((r, i) => (
                <a
                  key={`${r.url}-${i}`}
                  href={r.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs px-2 py-1 rounded bg-[var(--color-bg-alt,var(--color-card))] border border-[var(--color-border-light)] hover:border-sakura-300 text-[var(--color-text-secondary)] hover:text-[var(--color-primary)] transition-colors truncate"
                  title={r.title || r.url}
                >
                  【{i + 1}】{r.title || r.url}
                </a>
              ))}
            </div>
          </div>
        ) : null}

        {/* 操作按钮 */}
        <div
          className={['flex items-center gap-1 mt-1', isUser ? 'flex-row-reverse' : 'flex-row'].join(' ')}
        >
          {onCopy ? (
            <button
              type="button"
              onClick={() => onCopy(message)}
              className="text-xs text-[var(--color-text-tertiary)] hover:text-sakura-600 px-1.5 py-0.5 rounded hover:bg-sakura-50 transition-colors"
              title="复制"
            >
              复制
            </button>
          ) : null}
          {onQuote ? (
            <button
              type="button"
              onClick={() => onQuote(message)}
              className="text-xs text-[var(--color-text-tertiary)] hover:text-sakura-600 px-1.5 py-0.5 rounded hover:bg-sakura-50 transition-colors"
              title="引用"
            >
              引用
            </button>
          ) : null}
          {!isUser && onRegenerate && !message.streaming ? (
            <button
              type="button"
              onClick={() => onRegenerate(message)}
              className="text-xs text-[var(--color-text-tertiary)] hover:text-sakura-600 px-1.5 py-0.5 rounded hover:bg-sakura-50 transition-colors"
              title="重新生成"
            >
              重新生成
            </button>
          ) : null}
          {/* 赞/踩（批5 反馈闭环：踩→本次引用的知识自动降权） */}
          {!isUser && onRate && !message.streaming ? (
            <>
              <button
                type="button"
                onClick={() => handleRate(1)}
                className={`text-xs px-1.5 py-0.5 rounded transition-colors ${
                  myRating === 1
                    ? 'text-[var(--color-success)] font-semibold'
                    : 'text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50'
                }`}
                title="赞（有用的回答）"
              >
                👍
              </button>
              <button
                type="button"
                onClick={() => handleRate(-1)}
                className={`text-xs px-1.5 py-0.5 rounded transition-colors ${
                  myRating === -1
                    ? 'text-[var(--color-error)] font-semibold'
                    : 'text-[var(--color-text-tertiary)] hover:text-sakura-600 hover:bg-sakura-50'
                }`}
                title="踩（回答质量差，将降低所引用知识的权重）"
              >
                👎
              </button>
            </>
          ) : null}
        </div>
      </div>

      {/* 图片放大预览（复用通用 Modal，遮罩/ESC 关闭） */}
      {viewerIdx !== null && images[viewerIdx] ? (
        <Modal
          onClose={() => setViewerIdx(null)}
          width={720}
          title={`附件图片 ${viewerIdx + 1} / ${images.length}`}
        >
          <img
            src={images[viewerIdx]}
            alt={`附件图片 ${viewerIdx + 1}`}
            className="w-full h-auto rounded-lg"
          />
        </Modal>
      ) : null}
    </div>
  );
}, messageBubblePropsEqual);

/** 审计 R3-FE1：props 相等比较（message 字段值 + 回调引用） */
function messageBubblePropsEqual(prev: MessageBubbleProps, next: MessageBubbleProps): boolean {
  const a = prev.message;
  const b = next.message;
  const sameMessage =
    a === b ||
    (a.id === b.id &&
      a.role === b.role &&
      a.content === b.content &&
      a.timestamp === b.timestamp &&
      a.model === b.model &&
      a.streaming === b.streaming &&
      a.reasoning === b.reasoning &&
      a.reasoningMs === b.reasoningMs &&
      a.errorText === b.errorText &&
      a.quote === b.quote &&
      a.images === b.images);
  return (
    sameMessage &&
    prev.onQuote === next.onQuote &&
    prev.onCopy === next.onCopy &&
    prev.onRegenerate === next.onRegenerate
  );
}

export default MessageBubble;
