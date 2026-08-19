/**
 * MessageBubble 消息气泡
 * OmniSpace AI v2.3.1 — Sakura 主题（暗色默认 / 亮色可选，全令牌化）
 * --------------------------------------------------------------------------
 * 用户 / 助手不同样式；助手消息采用 AI 生成内容标记底色（--color-ai-bg，约束 §14，
 * 由 tokens.css 等效替换浅色 #E3F2FD，随主题切换）。
 * 显示模型名与时间；Markdown 由 react-markdown + remark-gfm（表格/删除线/任务列表）
 * + rehype-highlight（代码高亮，hljs-* 令牌化样式见 app.css）渲染。
 * 流式输出期间渲染纯文本 + 光标（避免逐 token 全量解析抖动），完成后切换 Markdown。
 * 规格 §9.2 对话模块。
 */
import { memo } from 'react';
import { User, Flower2, AlertCircle } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

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
  /** 错误文案 */
  errorText?: string;
  /** 引用消息 */
  quote?: { id: string; content: string } | null;
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
}

/** 格式化时间 HH:MM */
function formatTime(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts);
  const p = (n: number) => (n < 10 ? `0${n}` : `${n}`);
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * 审计 R3-FE1：React.memo 包裹（具名函数便于 devtools），阻断流式期间
 * store 每 token 重建 messages 数组导致的全列表重渲染（全量重跑 Markdown 解析）。
 * 自定义比较：message 按字段值比较（流式 token 仅触发内容变化的那条重渲染），
 * 回调按引用比较（父级须以 useCallback 稳定传入，见 DialogPage）。
 */
export const MessageBubble = memo(function MessageBubble({
  message,
  onQuote,
  onCopy,
  onRegenerate,
}: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isError = !!message.errorText;

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
        {/* 模型名 / 时间 */}
        <div className="flex items-center gap-2 mb-1 text-xs text-[var(--color-text-tertiary)]">
          {!isUser && message.model ? <span className="font-medium">{message.model}</span> : null}
          <span>{formatTime(message.timestamp)}</span>
        </div>

        {/* 引用块 */}
        {message.quote ? (
          <blockquote className="mb-1 pl-2 border-l-2 border-sakura-300 text-xs text-[var(--color-text-secondary)] truncate max-w-full">
            {message.quote.content}
          </blockquote>
        ) : null}

        {/* 气泡 */}
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
          ) : message.content ? (
            message.streaming ? (
              /* 流式期间：纯文本 + 光标，避免逐 token 全量 Markdown 解析 */
              <span className="leading-relaxed whitespace-pre-wrap break-words">
                {message.content}
                <span className="inline-block w-1.5 h-4 ml-0.5 bg-[var(--color-primary)] animate-pulse align-middle" />
              </span>
            ) : (
              /* 完成态：完整 Markdown（GFM 表格/删除线/任务列表 + 代码高亮） */
              <div className="md-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                  {message.content}
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
        </div>
      </div>
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
      a.errorText === b.errorText &&
      a.quote === b.quote);
  return (
    sameMessage &&
    prev.onQuote === next.onQuote &&
    prev.onCopy === next.onCopy &&
    prev.onRegenerate === next.onRegenerate
  );
}

export default MessageBubble;
