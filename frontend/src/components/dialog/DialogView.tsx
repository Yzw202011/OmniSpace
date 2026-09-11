/**
 * DialogView 对话主视图
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 左侧会话列表 + 右侧消息区与输入区；支持流式输出显示与图片附件上传。
 * 组件通过 props 注入数据与回调，与 stores/services 解耦（受控模式）：
 *   父级驱动 messages 变化以实现流式输出，本组件负责 UI 与交互编排。
 * 规格 §9.2 对话模块。
 */
import { useEffect, useRef, useState } from 'react';
import type {
  ChangeEvent,
  ClipboardEvent as ReactClipboardEvent,
  DragEvent as ReactDragEvent,
  KeyboardEvent,
} from 'react';
import { Button } from '../common/Button';
import { VirtualList } from '../common/VirtualList';
import { Paperclip, X, MessageSquare, Flower2, FileText, Loader2 } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import type { ChatMessage } from './MessageBubble';
import { SessionList } from './SessionList';
import type { ChatSession } from './SessionList';
import { useAppStore } from '@/stores/useAppStore';
import dialogApi from '@/services/dialogApi';

/** 附件（图片 = 多模态理解；文档 = 解析成文本随消息注入） */
export interface Attachment {
  id: string;
  name: string;
  /** 附件类型：image（默认，兼容存量）/ document（2026-09-07 按模型能力开放） */
  kind?: 'image' | 'document';
  /** 预览用 blob: 对象 URL（图片附件） */
  url?: string;
  /** 发送用 data:base64 字符串（后端多模态推理需要真实图片数据） */
  dataUrl?: string;
  /** 文档附件解析出的纯文本（后端 /dialog/parse-document 返回） */
  text?: string;
  /** 文档字符数（chip 展示） */
  chars?: number;
  /** 解析进行中占位标记（chip 渲染加载态，2026-09-08 反馈闭环） */
  parsing?: boolean;
  size?: number;
}

export interface DialogViewProps {
  /** 会话列表 */
  sessions: ChatSession[];
  /** 当前激活会话 ID */
  activeSessionId?: string | null;
  /** 当前会话的消息列表（流式输出由父级更新此数组驱动） */
  messages: ChatMessage[];
  /** 是否正在生成回复 */
  generating?: boolean;
  /** 会话列表加载中 */
  sessionsLoading?: boolean;
  /** 消息加载中 */
  messagesLoading?: boolean;
  /** 选择会话 */
  onSelectSession: (id: string) => void;
  /** 新建会话 */
  onCreateSession: () => void;
  /**
   * 确保存在活跃会话（2026-08-23 输入即自动创建对话）：
   * 无会话时自动创建并返回新会话 id，失败返回 null。
   * 由 DialogPage 提供防重入保证（并发触发共享同一创建 Promise）。
   */
  onEnsureSession?: () => Promise<string | null>;
  /** 删除会话 */
  onDeleteSession: (id: string) => void;
  /** 批量删除会话（SessionList 批量管理模式） */
  onBatchDeleteSessions?: (ids: string[]) => void;
  /** 重命名会话 */
  onRenameSession?: (id: string, title: string) => void;
  /** 置顶/取消置顶会话 */
  onTogglePinSession?: (id: string, pinned: boolean) => void;
  /** 会话搜索（关键词，空串为清除） */
  onSearchSessions?: (keyword: string) => void;
  /** 引用请求（父级点击消息「引用」时下发，seq 递增触发输入框回填） */
  quoteRequest?: { seq: number; message: ChatMessage } | null;
  /** 发送消息（父级据此发起流式请求并更新 messages） */
  /**
   * 发送消息（父级据此发起流式请求并更新 messages）。
   * 返回 false = 同步拒绝（互斥/无会话）——输入与附件保留待重发
   * （2026-09-07 二测缺陷修复：此前附件随发送即清空，被拒后重发
   * 不带附件，AI 只能瞎猜）。void/true 视为已受理。
   */
  onSend: (
    text: string,
    attachments?: Attachment[],
  ) => void | boolean | Promise<void | boolean>;
  /** 停止生成 */
  onStop?: () => void;
  /** 引用消息 */
  onQuote?: (message: ChatMessage) => void;
  /** 复制消息 */
  onCopy?: (message: ChatMessage) => void;
  /** 重新生成 */
  onRegenerate?: (message: ChatMessage) => void;
  /** 评分（批5 反馈闭环：踩→引用知识降权） */
  onRate?: (message: ChatMessage, rating: number) => void;
  /**
   * 当前选中模型是否支持图片理解（2026-09-07 按模型能力开放附件）：
   * false = 纯文本模型，图片上传按钮/拖拽/粘贴彻底关闭，仅开放文档。
   * 缺省 true（模型清单未加载完成前兼容存量行为）。
   */
  modelSupportsVision?: boolean;
}

/** 输入字数上限（对齐 DIALOG-016） */
const MAX_INPUT = 4096;
/** 单张图片大小上限 10MB（CHAT-012：base64 入 JSON 体膨胀 ~33%，过大拖垮多模态推理） */
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
/** 附件数量上限（对齐后端 dialog._decode_images images[:4]） */
const MAX_ATTACHMENTS = 4;

export function DialogView({
  sessions,
  activeSessionId,
  messages,
  generating = false,
  sessionsLoading = false,
  messagesLoading = false,
  onSelectSession,
  onCreateSession,
  onEnsureSession,
  onDeleteSession,
  onBatchDeleteSessions,
  onRenameSession,
  onTogglePinSession,
  onSearchSessions,
  quoteRequest,
  onSend,
  onStop,
  onQuote,
  onCopy,
  onRegenerate,
  onRate,
  modelSupportsVision = true,
}: DialogViewProps) {
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  /** 拖拽悬停视觉反馈（CHAT-008） */
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const prevGeneratingRef = useRef(generating);
  const prevSessionRef = useRef(activeSessionId);

  // 新消息到达自动滚动到底部（审计 R3-FE3：流式期间仅当用户位于底部附近时以
  // 'auto' 跟随，避免每 token 重启 smooth 动画；生成结束时一次性 smooth 滚到底）
  useEffect(() => {
    const container = scrollRef.current;
    const sessionChanged = prevSessionRef.current !== activeSessionId;
    prevSessionRef.current = activeSessionId;
    const justFinished = prevGeneratingRef.current && !generating;
    prevGeneratingRef.current = generating;
    if (!container) {
      return;
    }
    if (sessionChanged) {
      // 切换会话：直接定位到底部
      messagesEndRef.current?.scrollIntoView({ behavior: 'auto' });
      return;
    }
    if (justFinished) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
      return;
    }
    const nearBottom =
      container.scrollTop + container.clientHeight >= container.scrollHeight - 80;
    if (nearBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'auto' });
    }
  }, [messages, generating, activeSessionId]);

  // 引用请求：把被引用消息内容填入输入框并聚焦（Markdown 引用块格式）
  useEffect(() => {
    if (!quoteRequest) return;
    const quoted = quoteRequest.message.content.trim();
    if (!quoted) return;
    setInput((prev) => `> ${quoted.replace(/\n/g, '\n> ')}\n\n${prev}`);
    inputRef.current?.focus();
    // 输入即自动创建：引用回填也是输入行为
    autoEnsureSession();
  }, [quoteRequest]);

  // 审计 R3-FE4：组件卸载时释放未发送的附件 blob: URL（经 ref 读取最新附件列表）
  const attachmentsRef = useRef<Attachment[]>([]);
  attachmentsRef.current = attachments;
  useEffect(() => {
    return () => {
      attachmentsRef.current.forEach((a) => {
        if (a.url?.startsWith('blob:')) {
          URL.revokeObjectURL(a.url);
        }
      });
    };
  }, []);

  // ── 输入即自动创建会话（2026-08-23）────────────────────────────
  // 经 ref 读取最新回调，避免触发点依赖数组膨胀
  const ensureSessionRef = useRef(onEnsureSession);
  ensureSessionRef.current = onEnsureSession;
  /** 无会话时自动创建（fire-and-forget：不阻塞输入；失败由父级 toast） */
  function autoEnsureSession() {
    if (activeSessionId) return;
    ensureSessionRef.current?.();
  }

  async function send() {
    const text = input.trim();
    if (!text && attachments.length === 0) return;
    if (generating) return;
    // 2026-09-08 反馈闭环：文档解析中禁止发送——占位附件无 text，
    // 发送会被 docs 过滤静默丢掉（用户以为带了文档实际没带）
    const parsingDoc = attachments.find((a) => a.parsing);
    if (parsingDoc) {
      useAppStore.getState().showToast(
        `文档「${parsingDoc.name}」还在解析中，稍等 1 秒再发送`, 'warning');
      return;
    }
    // P2-2 修复（2026-09-02 实测复现）：浮层被 Esc 关闭/输入法组合态
    // 键序跳过 applyCommand 时，回车会把命令原文当消息发给模型
    // （「/清空输入不生效」观感来源）。全文等值命令键 → 直接执行。
    const directCmd = SLASH_COMMANDS.find((c) => c.key === text);
    if (directCmd) {
      directCmd.run();
      setCmdIdx(0);
      setCmdDismissed(false);
      inputRef.current?.focus();
      return;
    }
    // 兜底：输入后立刻发送而自动创建尚未完成（或未触发）时，
    // 等待会话就绪再发——输入内容保留（仅成功发送才清空）
    if (!activeSessionId) {
      const sid = await ensureSessionRef.current?.();
      if (!sid) return; // 创建失败已 toast，保留输入待重试
    }
    // 2026-09-07 文档附件：解析文本以标记块拼进消息正文（图片走
    // images 通道，文档走文本通道——纯文本模型同样可读，气泡侧对
    // 📎 块折叠渲染不刷屏）。单条上限对齐后端 DIALOG_MAX_INPUT_CHARS
    // （32768）：文档块按预算截断（超限整条消息会被后端拒绝）
    const DOC_PAYLOAD_BUDGET = 32000;
    const docs = attachments.filter((a) => a.kind === 'document' && a.text);
    let payload = text;
    if (docs.length > 0) {
      const perDoc = Math.max(
        1000, Math.floor((DOC_PAYLOAD_BUDGET - text.length) / docs.length));
      const docBlocks = docs
        .map((d) => {
          const clipped = d.text!.length > perDoc
            ? `${d.text!.slice(0, perDoc)}\n…（文档过长已截断，全文 ${d.chars ?? d.text!.length} 字）`
            : d.text!;
          return `[📎 文档 ${d.name}]\n${clipped}\n[/📎 文档 ${d.name}]`;
        })
        .join('\n\n');
      payload = text
        ? `${docBlocks}\n\n${text}`
        : `${docBlocks}\n\n请阅读以上文档内容。`;
    }
    const accepted = await onSend(
      payload, attachments.length > 0 ? attachments : undefined);
    // 同步拒绝（互斥/无会话等）：输入与附件全保留，用户处理后重发
    // 即可——不吞附件（2026-09-07 二测缺陷：被拒后重发不带附件）
    if (accepted === false) {
      return;
    }
    // 审计 R3-FE4：发送后释放附件 blob: 预览 URL，避免内存泄漏
    attachments.forEach((a) => {
      if (a.url?.startsWith('blob:')) {
        URL.revokeObjectURL(a.url);
      }
    });
    setInput('');
    setAttachments([]);
    // 重置输入框高度
    if (inputRef.current) inputRef.current.style.height = 'auto';
  }

  // ---- CHAT-046：斜杠命令补全（命令均映射真实动作，不伪造） ----
  const SLASH_COMMANDS = [
    {
      key: '/新建对话',
      desc: '创建一个新的对话会话',
      run: () => {
        setInput('');
        onCreateSession();
      },
    },
    {
      key: '/清空输入',
      desc: '清空当前输入框内容',
      run: () => setInput(''),
    },
  ];
  const slashActive = input.startsWith('/') && !input.includes('\n');
  const filteredCmds = slashActive
    ? SLASH_COMMANDS.filter((c) => c.key.startsWith(input.trim()))
    : [];
  const [cmdIdx, setCmdIdx] = useState(0);
  const [cmdDismissed, setCmdDismissed] = useState(false);
  const cmdVisible = slashActive && filteredCmds.length > 0 && !cmdDismissed;

  function applyCommand(cmd: (typeof SLASH_COMMANDS)[number]) {
    cmd.run();
    setCmdIdx(0);
    setCmdDismissed(false);
    inputRef.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 命令浮层打开时优先接管键盘（Enter 应用 / 方向键导航 / Esc 关闭）
    if (cmdVisible) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setCmdIdx((i) => (i + 1) % filteredCmds.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setCmdIdx((i) => (i - 1 + filteredCmds.length) % filteredCmds.length);
        return;
      }
      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
        e.preventDefault();
        applyCommand(filteredCmds[Math.min(cmdIdx, filteredCmds.length - 1)]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setCmdDismissed(true);
        return;
      }
    }
    // Enter 发送 / Shift+Enter 换行；兼容中文输入法 isComposing
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  }

  function onInputChange(e: ChangeEvent<HTMLTextAreaElement>) {
    let v = e.target.value;
    if (v.length > MAX_INPUT) v = v.slice(0, MAX_INPUT);
    setInput(v);
    setCmdIdx(0);
    setCmdDismissed(false); // 输入变化时重新允许浮层
    // 输入即自动创建会话：首字符触发（键盘输入/文本粘贴/语音转文字
    // 最终都汇聚到本收口）。IME 组合期（拼音未上屏）不触发，选定
    // 汉字的那次 onChange 才建；斜杠命令是系统指令而非对话内容，
    // 由 applyCommand 显式处理，不自动建会话
    if (
      !activeSessionId &&
      v.trim() &&
      !(e.nativeEvent as InputEvent).isComposing &&
      !v.startsWith('/')
    ) {
      autoEnsureSession();
    }
    // 自适应高度（上限 160px）
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(160, el.scrollHeight)}px`;
  }

  // 共享图片入列（文件选择 / 拖拽 / 粘贴三通道同一校验漏斗，CHAT-008/009/012）
  function addImageFiles(files: File[]) {
    const toast = useAppStore.getState().showToast;
    // 2026-09-07 按模型能力开放附件：纯文本模型彻底关闭图片通道
    // （按钮不渲染，拖拽/粘贴在此守卫拒绝——不给出「能传却被吞」的误导）
    if (!modelSupportsVision) {
      if (files.some((f) => f.type.startsWith('image/'))) {
        toast('当前模型不支持图片理解，请上传文档（txt/md/docx/pdf）或切换多模态模型', 'warning');
      }
      return;
    }
    // 输入即自动创建：图片入列也是输入行为（选文件/拖拽/粘贴图片）
    if (files.some((f) => f.type.startsWith('image/'))) {
      autoEnsureSession();
    }
    // 2026-09-08 txt 吞文件修复：非图片文件不再静默 continue（此前
    // 用户从回形针选 txt 被无声丢弃=「无法发送」的根因）——文档类
    // 自动路由到文档解析通道，未知类型如实提示带出路
    const docRouted: File[] = [];
    let docQuotaExceeded = false;
    for (const file of files) {
      if (!file.type.startsWith('image/')) {
        if (attachmentsRef.current.length + docRouted.length >= MAX_ATTACHMENTS) {
          docQuotaExceeded = true;
          continue;
        }
        docRouted.push(file);
        continue;
      }
      // CHAT-012：超大图片拒绝 + 如实提示（此前无限制，13MB+ 图片直接入列）
      if (file.size > MAX_IMAGE_BYTES) {
        toast(
          `图片「${file.name}」超过大小上限（${(file.size / 1024 / 1024).toFixed(1)}MB > ${MAX_IMAGE_BYTES / 1024 / 1024}MB），已跳过`,
          'warning',
        );
        continue;
      }
      if (attachmentsRef.current.length >= MAX_ATTACHMENTS) {
        toast(`最多添加 ${MAX_ATTACHMENTS} 张图片，已跳过「${file.name}」`, 'warning');
        break;
      }
      // blob: URL 仅供本地预览；发送需真实图片数据 → FileReader 转 base64
      const reader = new FileReader();
      reader.onload = () => {
        const att: Attachment = {
          id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          name: file.name,
          kind: 'image',
          url: URL.createObjectURL(file),
          dataUrl: String(reader.result || ''),
          size: file.size,
        };
        setAttachments((prev) => [...prev, att]);
      };
      reader.readAsDataURL(file);
    }
    if (docRouted.length > 0) {
      autoEnsureSession();
      void processDocFiles(docRouted);
    }
    if (docQuotaExceeded) {
      toast(`附件总数上限 ${MAX_ATTACHMENTS} 个：文档已超配额，请先移除部分附件`, 'warning');
    }
  }

  function onFilesChange(e: ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (!files) return;
    addImageFiles(Array.from(files));
    e.target.value = '';
  }

  // ---- 2026-09-07 文档附件（所有模型开放；纯文本模型的主要资料通道）----
  const docFileRef = useRef<HTMLInputElement>(null);
  /** 文档解析进行中（防重复点选；chip 区显示加载态） */
  const [docParsing, setDocParsing] = useState(false);

  async function onDocFilesChange(e: ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    // e.target.files 是活 FileList：先清 value 会把它原地清空（2026-09-08
    // 实弹抓出——按钮选文件零反应零报错的真凶），必须先快照再清
    const list = Array.from(files);
    e.target.value = '';
    await processDocFiles(list);
  }

  /** 文档附件处理核（2026-09-08 从 onDocFilesChange 抽出）：图片通道
   * 收到文档类文件时自动路由到此——用户不应需要分辨两个上传按钮。 */
  async function processDocFiles(docList: File[]) {
    const toast = useAppStore.getState().showToast;
    // 上限收纳（2026-09-07 二测反馈：整批全拒太生硬）：按剩余配额
    // 收前 N 个、多余的如实提示——而非一个都不收
    const list = docList;
    const quota = MAX_ATTACHMENTS - attachmentsRef.current.length;
    if (quota <= 0) {
      toast(`附件总数上限 ${MAX_ATTACHMENTS} 个，请先移除部分附件`, 'warning');
      return;
    }
    const accepted = list.slice(0, quota);
    const skipped = list.slice(quota);
    if (skipped.length > 0) {
      toast(`附件上限 ${MAX_ATTACHMENTS} 个：已收 ${accepted.length} 个，跳过 ${skipped.length} 个（${skipped.map((f) => f.name).join('、')}）`, 'warning');
    }
    autoEnsureSession();
    setDocParsing(true);
    for (const file of accepted) {
      // 2026-09-08 反馈闭环（用户实测「不知道有没有上传成功」）：
      // ①解析期间先挂「解析中…」占位 chip（docx/pdf 解析数秒，此前
      //   零反馈=用户以为点了没反应）；②成功弹 toast 确认（此前只有
      //   截断/失败才有提示，成功是静默的）；③失败移除占位+带出路
      //   toast。占位以 parsing 标记渲染，完成/失败原地替换/移除。
      const phId = `doc-ph-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setAttachments((prev) => [...prev, {
        id: phId, name: file.name, kind: 'document',
        text: '', size: file.size, parsing: true,
      }]);
      try {
        const res = await dialogApi.parseDocument(file);
        setAttachments((prev) => prev.map((a) => a.id === phId ? {
          ...a, name: res.name, text: res.text, chars: res.chars,
          size: file.size, parsing: false,
        } : a));
        toast(`文档「${res.name}」已附加（${res.chars} 字）${res.truncated ? '，过长已截断' : ''}`, res.truncated ? 'warning' : 'success');
      } catch (err) {
        setAttachments((prev) => prev.filter((a) => a.id !== phId));
          // api 层抛 ApiError 普通对象（非 Error 实例，含 message/
          // suggestion）——instanceof 恒 false 会吞掉后端出路提示
          //（二测实测：.doc 拒绝只显「文档解析失败」）。此处兼容
          // 两种形状并把 suggestion（如「请另存为 .docx」）带出来
          const e2 = err as { message?: unknown; suggestion?: unknown } | null;
          const msg = e2 && typeof e2 === 'object' && e2.message
            ? String(e2.message)
            : err instanceof Error ? err.message : '文档解析失败';
          const sug = e2 && typeof e2 === 'object' && e2.suggestion
            ? String(e2.suggestion)
            : '';
          toast(`「${file.name}」：${msg}${sug ? `（${sug}）` : ''}`, 'error');
      }
    }
    setDocParsing(false);
  }

  // ---- CHAT-008：拖拽图片上传 ----
  function onDragOver(e: ReactDragEvent<HTMLDivElement>) {
    if (e.dataTransfer.types.includes('Files')) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      setDragOver(true);
    }
  }

  function onDragLeave(e: ReactDragEvent<HTMLDivElement>) {
    // 仅当真正离开容器边界时复位（子元素间移动不闪烁）
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
      setDragOver(false);
    }
  }

  function onDrop(e: ReactDragEvent<HTMLDivElement>) {
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    // 2026-09-07 拖拽分流：文档扩展名走文档解析通道，其余走图片漏斗
    const DOC_EXTS = ['.txt', '.md', '.docx', '.pdf', '.doc'];
    const docFiles = files.filter((f) =>
      DOC_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext)));
    const imgFiles = files.filter((f) => !docFiles.includes(f));
    if (docFiles.length > 0) {
      const dt = new DataTransfer();
      docFiles.forEach((f) => dt.items.add(f));
      const evt = { target: { files: dt.files, value: '' } } as unknown as ChangeEvent<HTMLInputElement>;
      void onDocFilesChange(evt);
    }
    if (imgFiles.length > 0) {
      addImageFiles(imgFiles);
    }
  }

  // ---- CHAT-009：粘贴剪贴板图片 ----
  function onPaste(e: ReactClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files).filter((f) =>
      f.type.startsWith('image/'),
    );
    if (files.length > 0) {
      e.preventDefault(); // 阻止剪贴板文件名文本插入输入框
      addImageFiles(files);
    }
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target?.url?.startsWith('blob:')) URL.revokeObjectURL(target.url);
      return prev.filter((a) => a.id !== id);
    });
  }

  return (
    <div className="flex h-full bg-[var(--color-bg)]">
      {/* ============ 左侧：会话列表 ============ */}
      <div className="w-64 shrink-0 border-r border-[var(--color-border-light)] bg-[var(--color-card)]">
        <SessionList
          sessions={sessions}
          activeId={activeSessionId}
          onSelect={onSelectSession}
          onCreate={onCreateSession}
          onDelete={onDeleteSession}
          onBatchDelete={onBatchDeleteSessions}
          onRename={onRenameSession}
          onTogglePin={onTogglePinSession}
          onSearch={onSearchSessions}
          loading={sessionsLoading}
        />
      </div>

      {/* ============ 右侧：消息区 + 输入区 ============ */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* 消息区 */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-4 space-y-5">
          {!activeSessionId ? (
            <div className="h-full flex items-center justify-center text-center text-[var(--color-text-tertiary)]">
              <div>
                <div className="flex justify-center mb-3 text-[var(--color-primary)]"><MessageSquare size={36} strokeWidth={1.5} aria-hidden="true" /></div>
                <div className="text-base">开始一段新对话吧</div>
                <div className="text-sm mt-1">直接在下方输入内容即可自动创建对话，支持剧本讨论与图片理解。</div>
              </div>
            </div>
          ) : messagesLoading ? (
            <div className="h-full flex items-center justify-center text-[var(--color-text-tertiary)]">
              <span className="inline-block w-5 h-5 border-2 border-sakura-300 border-t-transparent rounded-full animate-spin mr-2" />
              加载消息中…
            </div>
          ) : messages.length === 0 ? (
            <div className="h-full flex items-center justify-center text-center text-[var(--color-text-tertiary)]">
              <div>
                <div className="flex justify-center mb-3 text-[var(--color-primary)]"><Flower2 size={36} strokeWidth={1.5} aria-hidden="true" /></div>
                <div className="text-base">我是 OmniSpace 助手</div>
                <div className="text-sm mt-1">可以和我讨论剧本、分析角色，或询问任何创作问题。</div>
              </div>
            </div>
          ) : (
            <>
              {/* 消息列表窗口化（P3-⑥）：超阈值(60条)线程按可视窗口渲染 +
                overscan 缓冲，行高实测校正；短会话走普通流式渲染 */}
              <VirtualList
                items={messages}
                scrollRef={scrollRef}
                estimate={80}
                gap={20}
                threshold={60}
                endRef={messagesEndRef}
                renderItem={(m: ChatMessage) => (
                  <MessageBubble
                    message={m}
                    onQuote={onQuote}
                    onCopy={onCopy}
                    onRegenerate={onRegenerate}
                    onRate={onRate}
                  />
                )}
              />
            </>
          )}
        </div>

        {/* 输入区（拖拽图片到此处上传，CHAT-008） */}
        <div
          className={`border-t border-[var(--color-border-light)] bg-[var(--color-card)] px-4 py-3${
            dragOver ? ' ring-2 ring-inset ring-sakura-300' : ''
          }`}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
        >
          {/* 附件预览（图片=缩略图方块；文档=文件名+字数 chip） */}
          {attachments.length > 0 ? (
            <div className="flex flex-wrap gap-2 mb-2">
              {attachments.map((a) =>
                (a.kind ?? 'image') === 'image' ? (
                <div
                  key={a.id}
                  className="relative w-16 h-16 rounded-lg overflow-hidden border border-[var(--color-input-border)]"
                >
                  <img src={a.url} alt={a.name} className="w-full h-full object-cover" />
                  <button
                    type="button"
                    onClick={() => removeAttachment(a.id)}
                    aria-label={`移除附件 ${a.name}`}
                    className="absolute top-0.5 right-0.5 w-5 h-5 rounded-full bg-black/50 text-white flex items-center justify-center hover:bg-black/70"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
                ) : (
                <div
                  key={a.id}
                  className="relative flex items-center gap-1.5 h-9 px-2.5 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-xs text-[var(--color-text-secondary)]"
                >
                  {a.parsing ? (
                    <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin text-sakura-500" />
                  ) : (
                    <FileText className="w-3.5 h-3.5 shrink-0 text-sakura-500" />
                  )}
                  <span className="max-w-40 truncate font-medium">{a.name}</span>
                  {a.parsing ? (
                    <span className="text-[10px] text-[var(--color-text-tertiary)]">解析中…</span>
                  ) : typeof a.chars === 'number' && (
                    <span className="text-[10px] text-[var(--color-text-tertiary)]">
                      {a.chars >= 1000 ? `${(a.chars / 1000).toFixed(1)}k` : a.chars}字
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => removeAttachment(a.id)}
                    aria-label={`移除文档 ${a.name}`}
                    className="w-4 h-4 rounded-full flex items-center justify-center text-[var(--color-text-tertiary)] hover:text-red-500"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
                ),
              )}
            </div>
          ) : null}

          {/* 斜杠命令补全浮层（CHAT-046） */}
          {cmdVisible && (
            <div
              role="listbox"
              aria-label="命令建议"
              className="mb-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] shadow-lg overflow-hidden"
            >
              {filteredCmds.map((c, i) => (
                <button
                  key={c.key}
                  type="button"
                  role="option"
                  aria-selected={i === cmdIdx}
                  onMouseDown={(e) => {
                    e.preventDefault(); // 保持输入框焦点
                    applyCommand(c);
                  }}
                  className={`w-full flex items-center gap-3 px-3 py-2 text-left text-sm transition-colors${
                    i === cmdIdx
                      ? ' bg-sakura-50 text-sakura-700'
                      : ' text-[var(--color-text-primary)] hover:bg-sakura-50'
                  }`}
                >
                  <span className="font-medium">{c.key}</span>
                  <span className="text-xs text-[var(--color-text-tertiary)]">{c.desc}</span>
                </button>
              ))}
            </div>
          )}

          <div className="flex items-end gap-2">
            {/* 图片上传按钮（2026-09-07 按模型能力开放：纯文本模型
                彻底不渲染，与拖拽/粘贴守卫同口径） */}
            {modelSupportsVision && (
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                aria-label="上传图片附件"
                title="上传图片"
                className="w-9 h-9 shrink-0 rounded-lg flex items-center justify-center text-[var(--color-text-secondary)] hover:bg-sakura-50 hover:text-sakura-600 transition-colors"
              >
                <Paperclip className="w-4 h-4" />
              </button>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={onFilesChange}
            />

            {/* 文档上传按钮（2026-09-07：所有模型开放——txt/md/docx/pdf
                解析成文本随消息注入，纯文本模型的主要资料通道） */}
            <button
              type="button"
              onClick={() => docFileRef.current?.click()}
              aria-label="上传文档附件"
              title="上传文档（txt / md / docx / pdf）"
              disabled={docParsing}
              className="w-9 h-9 shrink-0 rounded-lg flex items-center justify-center text-[var(--color-text-secondary)] hover:bg-sakura-50 hover:text-sakura-600 transition-colors disabled:opacity-50"
            >
              <FileText className={`w-4 h-4 ${docParsing ? 'animate-pulse' : ''}`} />
            </button>
            <input
              ref={docFileRef}
              type="file"
              accept=".txt,.md,.docx,.pdf"
              multiple
              className="hidden"
              onChange={(e) => void onDocFilesChange(e)}
            />

            {/* 输入框（输入即自动创建会话：无会话时不再禁用，
                输入/粘贴/语音首内容自动新建，2026-08-23） */}
            <textarea
              ref={inputRef}
              value={input}
              onChange={onInputChange}
              onKeyDown={onKeyDown}
              onPaste={onPaste}
              rows={1}
              placeholder={
                activeSessionId
                  ? '输入消息，Enter 发送，Shift+Enter 换行…'
                  : '输入内容将自动开始新对话…'
              }
              className="flex-1 min-h-9 max-h-40 px-3 py-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] resize-none focus:outline-none focus:ring-2 focus:ring-sakura-300 focus:border-sakura-400"
            />

            {/* 发送 / 停止（无会话时点击/回车发送：先自动建会话再发） */}
            {generating ? (
              <Button variant="danger" onClick={onStop} size="md">
                停止
              </Button>
            ) : (
              <Button
                onClick={send}
                disabled={!input.trim() && attachments.length === 0}
              >
                发送
              </Button>
            )}
          </div>

          {/* 输入元信息（按模型能力提示：纯文本模型只提文档通道） */}
          <div className="flex items-center justify-between mt-1.5 text-xs text-[var(--color-text-tertiary)]">
            <span>
              {generating
                ? '助手正在思考中…'
                : modelSupportsVision
                  ? '支持上传图片（多模态理解）与文档（txt/md/docx/pdf）'
                  : '当前模型为纯文本：支持上传文档（txt / md / docx / pdf）'}
            </span>
            <span className={input.length >= MAX_INPUT ? 'text-[var(--color-error)]' : ''}>
              {input.length}/{MAX_INPUT}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export default DialogView;
// 本项目仅供学习使用，商业授权请+Q 3559331368
