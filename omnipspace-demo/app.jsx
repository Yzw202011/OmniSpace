/* ============================================================
 * OmniSpace AI 主应用（文档 7.3 前端架构：入口/路由/状态/组件/通信）
 * 页面：首页 / 漫剧创作(入口→M6) / AI对话 / AI绘画 / 知识学习 / 模型管理 / 设置
 * 二级：资产库管理
 * 通信：HTTP REST(/api/v1) + WebSocket(/ws) + CustomEvent 事件总线
 * ============================================================ */
(function () {
  const { useState, useEffect, useRef, useCallback, useMemo } = React;
  const CFG = window.OMNI_CONFIG || { apiPrefix: "/api/v1", wsUrl: "ws://" + location.host + "/ws" };

  /* ==================== 通信层 ==================== */
  // CustomEvent 事件总线（文档 5.4：conversations-updated / system-status-update /
  // task-progress-update / notification-show / snapshot-saved / queue-updated ...）
  const Bus = {
    on(name, fn) { const h = (e) => fn(e.detail); window.addEventListener(name, h); return () => window.removeEventListener(name, h); },
    emit(name, detail) { window.dispatchEvent(new CustomEvent(name, { detail })); },
  };

  function fileUrl(p) {
    if (!p) return "";
    if (p.startsWith("/files/") || p.startsWith("http") || p.startsWith("data:")) return p;
    const norm = String(p).replace(/\\/g, "/");
    const idx = norm.toLowerCase().indexOf("/data/");
    return "/files/" + (idx >= 0 ? norm.slice(idx + 6) : norm.replace(/^\/+/, ""));
  }

  class ApiError extends Error {
    constructor(code, message, detail) { super(message); this.code = code; this.detail = detail; }
  }

  async function api(path, { method = "GET", body, formData, timeout = 30000 } = {}) {
    const opt = { method, headers: {} };
    if (formData) { opt.body = formData; }
    else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    opt.signal = ctrl.signal;
    let resp;
    try { resp = await fetch(CFG.apiPrefix + path, opt); }
    catch (e) { clearTimeout(timer); throw new ApiError(-1, e.name === "AbortError" ? "请求超时" : "网络连接失败"); }
    clearTimeout(timer);
    let json = null;
    try { json = await resp.json(); } catch (e) { /* 非 JSON */ }
    if (!json) throw new ApiError(resp.status, "响应格式错误");
    if (json.code !== 0) throw new ApiError(json.code, json.message || "请求失败", json.detail);
    return json.data;
  }

  // SSE 流式对话（POST /chat/completions → data: {token} / [DONE]）
  async function apiSSE(path, body, { onToken, onMeta, signal } = {}) {
    const resp = await fetch(CFG.apiPrefix + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal,
    });
    if (!resp.ok || !resp.body) {
      let msg = "请求失败";
      try { const j = await resp.json(); msg = j.message || msg; } catch (e) {}
      throw new ApiError(resp.status, msg);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") return;
        try {
          const obj = JSON.parse(payload);
          if (obj.token !== undefined) onToken && onToken(obj.token, obj);
          else onMeta && onMeta(obj);
        } catch (e) { /* 忽略坏帧 */ }
      }
    }
  }

  // WebSocket 客户端：自动重连（≤30s），事件桥接到 Bus
  const WS = {
    _ws: null, _retry: 0, _timer: null, _closed: false,
    connect() {
      if (this._closed) return;
      try { this._ws = new WebSocket(CFG.wsUrl); } catch (e) { this._schedule(); return; }
      this._ws.onopen = () => { this._retry = 0; Bus.emit("ws-state", { connected: true }); };
      this._ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        const { type, payload } = msg || {};
        if (type === "task_progress" || type === "task_started" || type === "task_queued" ||
            type === "task_completed" || type === "task_failed" || type === "task_cancelled") {
          Bus.emit("task-progress-update", { type, task: payload });
        } else if (type === "system_status" || type === "vram_warning") {
          Bus.emit("system-status-update", payload);
        } else if (type === "queue_updated") {
          Bus.emit("queue-updated", payload);
        } else if (type === "sleep_detected") {
          Bus.emit("notification-show", { kind: "warning", text: "检测到系统休眠恢复，正在重载资源" });
        }
      };
      this._ws.onclose = () => { Bus.emit("ws-state", { connected: false }); this._schedule(); };
      this._ws.onerror = () => { try { this._ws.close(); } catch (e) {} };
    },
    _schedule() {
      if (this._closed || this._timer) return;
      const delay = Math.min(30000, 1000 * Math.pow(2, this._retry++));  // 指数退避封顶 30s（SUP-12）
      this._timer = setTimeout(() => { this._timer = null; this.connect(); }, delay);
    },
    send(type, payload) {
      if (this._ws && this._ws.readyState === 1) this._ws.send(JSON.stringify({ type, payload: payload || {} }));
    },
    close() { this._closed = true; if (this._ws) try { this._ws.close(); } catch (e) {} },
  };

  /* ==================== 通用组件 ==================== */
  function Icon({ name, size = 18, className = "" }) {
    const ref = useRef(null);
    useEffect(() => {
      if (ref.current && window.lucide && window.lucide.icons && window.lucide.icons[name]) {
        ref.current.innerHTML = "";
        const svg = window.lucide.icons[name].toSvg
          ? window.lucide.icons[name].toSvg({ width: size, height: size })
          : null;
        if (svg) { ref.current.innerHTML = svg; return; }
      }
      if (ref.current && window.lucide && window.lucide.createElement && window.lucide.icons[name]) {
        try {
          const el = window.lucide.createElement(window.lucide.icons[name]);
          el.setAttribute("width", size); el.setAttribute("height", size);
          ref.current.innerHTML = ""; ref.current.appendChild(el);
        } catch (e) {}
      }
    }, [name, size]);
    return <span ref={ref} className={"inline-flex items-center justify-center " + className}
                 style={{ width: size, height: size }} aria-hidden="true" />;
  }

  const Spinner = ({ size = 32 }) => (
    <span className="sakura-spinner" style={{ width: size, height: size }}>
      <span /><span /><span /><span /><span />
    </span>
  );

  const Progress = ({ value, indeterminate }) => (
    <div className={"progress " + (indeterminate ? "progress-indeterminate" : "")} role="progressbar"
         aria-valuenow={indeterminate ? undefined : Math.round((value || 0) * 100)} aria-valuemin="0" aria-valuemax="100">
      <div className="progress-fill" style={{ width: Math.round((value || 0) * 100) + "%" }} />
    </div>
  );

  const EmptyState = ({ art = "🌸", title, hint, action }) => (
    <div className="empty-state">
      <div className="empty-art" aria-hidden="true">{art}</div>
      <div style={{ fontSize: "var(--text-lg)", fontWeight: "var(--font-medium)", color: "var(--color-text-secondary)" }}>{title}</div>
      {hint && <div className="text-tertiary">{hint}</div>}
      {action}
    </div>
  );

  // Toast 宿主：监听 notification-show 事件（文档事件总线）
  function ToastHost() {
    const [items, setItems] = useState([]);
    useEffect(() => Bus.on("notification-show", ({ kind = "info", text, ms = 3600 }) => {
      const id = Math.random().toString(36).slice(2);
      setItems((xs) => [...xs, { id, kind, text }]);
      setTimeout(() => {
        setItems((xs) => xs.map((t) => t.id === id ? { ...t, leaving: true } : t));
        setTimeout(() => setItems((xs) => xs.filter((t) => t.id !== id)), 220);
      }, ms);
    }), []);
    return (
      <div className="toast-host" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={"toast toast-" + t.kind + (t.leaving ? " leaving" : "")} role="status">{t.text}</div>
        ))}
      </div>
    );
  }
  const toast = {
    ok: (text) => Bus.emit("notification-show", { kind: "success", text }),
    warn: (text) => Bus.emit("notification-show", { kind: "warning", text }),
    err: (text) => Bus.emit("notification-show", { kind: "error", text }),
    info: (text) => Bus.emit("notification-show", { kind: "info", text }),
  };

  function Modal({ open, onClose, title, children, width = 560, footer }) {
    if (!open) return null;
    return (
      <div className="modal-mask" onClick={onClose} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal" style={{ width }} onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-between mb-4">
            <h3 style={{ margin: 0, fontSize: "var(--text-xl)", fontWeight: "var(--font-semibold)" }}>{title}</h3>
            <button className="btn btn-icon" onClick={onClose} aria-label="关闭"><Icon name="X" /></button>
          </div>
          <div>{children}</div>
          {footer && <div className="flex gap-3 mt-6" style={{ justifyContent: "flex-end" }}>{footer}</div>}
        </div>
      </div>
    );
  }

  /* ==================== 路由 ==================== */
  function useHashRoute() {
    const parse = () => {
      const h = location.hash.replace(/^#\/?/, "");
      const [name, ...rest] = h.split("/");
      return { name: name || "home", param: decodeURIComponent(rest.join("/")) };
    };
    const [route, setRoute] = useState(parse);
    useEffect(() => {
      const fn = () => setRoute(parse());
      window.addEventListener("hashchange", fn);
      return () => window.removeEventListener("hashchange", fn);
    }, []);
    const nav = useCallback((name, param) => {
      location.hash = "#/" + name + (param ? "/" + encodeURIComponent(param) : "");
    }, []);
    return { route, nav };
  }

  const NAV_ITEMS = [
    { key: "home", label: "首页", icon: "Home" },
    { key: "manga", label: "漫剧创作", icon: "Clapperboard" },
    { key: "chat", label: "AI对话", icon: "MessageSquare" },
    { key: "image", label: "AI绘画", icon: "Palette" },
    { key: "knowledge", label: "知识学习", icon: "GraduationCap" },
    { key: "models", label: "模型管理", icon: "Boxes" },
    { key: "settings", label: "设置", icon: "Settings" },
  ];

  function Sidebar({ route, nav, wsOk, sysStatus }) {
    return (
      <aside className="sidebar" aria-label="主导航">
        <div className="flex items-center gap-2" style={{ padding: "0 var(--space-2)", marginBottom: "var(--space-6)" }}>
          <div style={{
            width: 36, height: 36, borderRadius: "var(--radius-md)", flexShrink: 0,
            background: "linear-gradient(135deg, var(--color-primary), var(--color-primary-deep))",
            display: "flex", alignItems: "center", justifyContent: "center", color: "#fff",
            fontSize: "var(--text-lg)", fontWeight: "var(--font-bold)",
          }}>桜</div>
          <div className="brand-text">
            <div style={{ fontWeight: "var(--font-bold)", fontSize: "var(--text-base)", lineHeight: "var(--leading-tight)" }}>OmniSpace</div>
            <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>AI 创作空间</div>
          </div>
        </div>
        <nav className="flex flex-col gap-1 flex-1">
          {NAV_ITEMS.map((it) => {
            const active = route.name === it.key || (it.key === "manga" && route.name === "assets");
            return (
              <button key={it.key} onClick={() => nav(it.key)} aria-current={active ? "page" : undefined}
                className="flex items-center gap-3"
                style={{
                  border: "none", cursor: "pointer", textAlign: "left",
                  padding: "10px var(--space-3)", borderRadius: "var(--radius-md)",
                  background: active ? "var(--color-primary-soft)" : "transparent",
                  color: active ? "var(--color-primary)" : "var(--color-text-secondary)",
                  fontWeight: active ? "var(--font-semibold)" : "var(--font-normal)",
                  fontSize: "var(--text-sm)", fontFamily: "var(--font-sans)",
                  transition: "all var(--dur-hover) var(--ease-out)",
                }}>
                <Icon name={it.icon} size={18} />
                <span className="nav-label">{it.label}</span>
              </button>
            );
          })}
        </nav>
        {/* 托盘状态指示（侧边栏底部，文档 2.1.1 / R18：CPU/内存/GPU/显存） */}
        <div className="tray-text" style={{ padding: "var(--space-3) var(--space-2)", borderTop: "1px solid var(--color-border)" }}>
          <div className="flex items-center gap-2" style={{ fontSize: "var(--text-xs)", color: "var(--color-text-tertiary)" }}>
            <span style={{
              width: 8, height: 8, borderRadius: "50%",
              background: wsOk ? "var(--color-success)" : "var(--color-error)",
              boxShadow: "0 0 6px " + (wsOk ? "var(--color-success)" : "var(--color-error)"),
            }} />
            <span>{wsOk ? "实时通道已连接" : "实时通道重连中"}</span>
          </div>
          {sysStatus && (
            <div className="mt-2 flex flex-col gap-1" style={{ fontSize: "var(--text-xs)", color: "var(--color-text-tertiary)" }}>
              {sysStatus.cpu_pct != null && (
                <div className="flex justify-between">
                  <span>CPU</span>
                  <span style={{ color: sysStatus.cpu_pct >= 85 ? "var(--color-error)" : sysStatus.cpu_pct >= 50 ? "var(--color-warning)" : "var(--color-success)" }}>
                    {Math.round(sysStatus.cpu_pct)}%
                  </span>
                </div>
              )}
              {sysStatus.ram && sysStatus.ram.pct != null && (
                <div className="flex justify-between">
                  <span>内存</span>
                  <span style={{ color: sysStatus.ram.pct >= 85 ? "var(--color-error)" : sysStatus.ram.pct >= 50 ? "var(--color-warning)" : "var(--color-success)" }}>
                    {Math.round(sysStatus.ram.pct)}%
                  </span>
                </div>
              )}
              {sysStatus.vram && sysStatus.vram.gpu_util_pct != null && (
                <div className="flex justify-between">
                  <span>GPU</span>
                  <span style={{ color: sysStatus.vram.gpu_util_pct >= 85 ? "var(--color-error)" : sysStatus.vram.gpu_util_pct >= 50 ? "var(--color-warning)" : "var(--color-success)" }}>
                    {Math.round(sysStatus.vram.gpu_util_pct)}%
                  </span>
                </div>
              )}
              {sysStatus.vram && sysStatus.vram.used_mb != null && (
                <div className="flex justify-between">
                  <span>显存</span>
                  <span style={{ color: sysStatus.vram.total_mb && (sysStatus.vram.used_mb / sysStatus.vram.total_mb) >= 0.85 ? "var(--color-error)" : "var(--color-text-tertiary)" }}>
                    {Math.round(sysStatus.vram.used_mb)}M{sysStatus.vram.total_mb ? "/" + Math.round(sysStatus.vram.total_mb) + "M" : ""}
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      </aside>
    );
  }

  /* ==================== 首页（文档 2.1.4：Bento 网格） ==================== */
  const PIPELINE_LABEL = {
    SCRIPT_INPUT: "剧本输入", STORYBOARD: "分镜", ASSET_PLAN: "资产规划",
    ASSET_GEN: "资产生成", DIRECTOR: "3D导演", VIDEO: "视频",
    CONFIRM: "确认", COMPLETE: "完成",
  };
  const MODE_LABEL = { auto: "自动", manual: "手动" };

  function HomePage({ nav }) {
    const [sys, setSys] = useState(null);
    const [vram, setVram] = useState(null);
    const [stats, setStats] = useState({ projects: 2, shots: 5, images: 2, knowledge: 128 });
    const [tasks, setTasks] = useState([]);

    useEffect(() => {
      api("/system/info").then(setSys).catch(() => {});
      api("/models/vram/status").then(setVram).catch(() => {});
      api("/tasks?state=running").then(d => setTasks(d.items || [])).catch(() => {});
    }, []);

    const gpuText = sys && sys.gpu && sys.gpu.available
      ? `${sys.gpu.devices[0].name}` : "NVIDIA GeForce RTX 4060";
    const vramRatio = vram && vram.capacity_mb ? Math.min(1, (vram.used_mb || 0) / vram.capacity_mb) : 0.15;

    const features = [
      { key: "manga", icon: "Clapperboard", title: "漫剧创作", desc: "剧本→分镜→资产→导演→视频", color: "var(--color-primary)" },
      { key: "chat", icon: "MessageSquare", title: "AI 对话", desc: "多轮对话·剧本创作·图片理解", color: "#6366f1" },
      { key: "image", icon: "Image", title: "AI 绘画", desc: "文生图·图生图·多图·首尾帧", color: "#f59e0b" },
      { key: "knowledge", icon: "GraduationCap", title: "知识学习", desc: "文本·URL·自动学习·训练面板", color: "#10b981" },
      { key: "models", icon: "Cpu", title: "模型管理", desc: "模型注册表·显存池·LoRA训练", color: "#8b5cf6" },
      { key: "settings", icon: "Settings", title: "系统设置", desc: "系统概览·任务队列·激活更新", color: "#64748b" },
    ];

    return (
      <div className="page">
        <header className="mb-6">
          <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)", lineHeight: "var(--leading-tight)" }}>
            工作台
          </h1>
          <p className="text-secondary mt-2" style={{ marginBottom: 0 }}>一站式 AI 创作平台 · 漫剧 · 对话 · 绘画 · 学习</p>
        </header>

        {/* 统计卡片 */}
        <div className="grid gap-4 mb-6" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
          {[
            { label: "漫剧项目", value: stats.projects, icon: "Film", color: "var(--color-primary)" },
            { label: "分镜总数", value: stats.shots, icon: "ListVideo", color: "#6366f1" },
            { label: "生成图片", value: stats.images, icon: "Image", color: "#f59e0b" },
            { label: "知识条目", value: stats.knowledge, icon: "BookOpen", color: "#10b981" },
          ].map((s, i) => (
            <div key={i} className="card" style={{ padding: "var(--space-4)" }}>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>{s.label}</div>
                  <div style={{ fontSize: "var(--text-2xl)", fontWeight: "var(--font-bold)", marginTop: 4 }}>{s.value}</div>
                </div>
                <div style={{ width: 40, height: 40, borderRadius: 10, background: s.color + "15", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <Icon name={s.icon} size={20} style={{ color: s.color }} />
                </div>
              </div>
            </div>
          ))}
        </div>

        <div className="grid gap-6" style={{ gridTemplateColumns: "2fr 1fr" }}>
          {/* 左 2/3：功能入口 */}
          <section aria-label="功能入口">
            <h2 className="section-title mb-4">功能模块</h2>
            <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
              {features.map((f) => (
                <div key={f.key} className="card card-hover" style={{ cursor: "pointer", padding: "var(--space-5)" }}
                     onClick={() => nav(f.key)} role="button" tabIndex="0"
                     onKeyDown={(e) => e.key === "Enter" && nav(f.key)}>
                  <div style={{ width: 44, height: 44, borderRadius: 12, background: f.color + "15", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: "var(--space-3)" }}>
                    <Icon name={f.icon} size={22} style={{ color: f.color }} />
                  </div>
                  <div style={{ fontWeight: "var(--font-semibold)", fontSize: "var(--text-base)" }}>{f.title}</div>
                  <div className="text-tertiary mt-1" style={{ fontSize: "var(--text-xs)", lineHeight: "var(--leading-relaxed)" }}>{f.desc}</div>
                </div>
              ))}
            </div>

            {/* 正在运行的任务 */}
            <h2 className="section-title mt-8 mb-4">正在运行</h2>
            {tasks.length === 0 ? (
              <div className="card" style={{ padding: "var(--space-6)", textAlign: "center" }}>
                <Icon name="CheckCircle" size={32} style={{ color: "var(--color-success)", marginBottom: 8 }} />
                <div className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>当前没有运行中的任务</div>
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {tasks.map((t) => (
                  <div key={t.id} className="card" style={{ padding: "var(--space-4)" }}>
                    <div className="flex items-center justify-between mb-2">
                      <span style={{ fontWeight: "var(--font-medium)" }}>{t.type}</span>
                      <span className="tag tag-warning">{t.status}</span>
                    </div>
                    <Progress value={(t.progress || 0) / 100} />
                    <div className="text-tertiary mt-2" style={{ fontSize: "var(--text-xs)" }}>{t.progress}% · {t.model}</div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* 右 1/3：今日灵感 + 系统状态速览 */}
          <aside className="flex flex-col gap-6">
            <div className="card" style={{
              background: "linear-gradient(160deg, var(--color-primary-grad-from), #fff)",
              borderLeft: "4px solid var(--color-primary)",
            }}>
              <div className="flex items-center gap-2 mb-3">
                <Icon name="Lightbulb" size={18} />
                <span style={{ fontWeight: "var(--font-semibold)" }}>今日灵感</span>
              </div>
              <p className="text-secondary" style={{ margin: 0, lineHeight: "var(--leading-relaxed)" }}>
                雨后的老街，撑红伞的少女回眸——侧逆光，浅景深，胶片颗粒质感。
              </p>
              <button className="btn btn-secondary mt-4" onClick={() => nav("image")}>
                <Icon name="Palette" size={15} /> 去画一张
              </button>
            </div>

            <div className="card">
              <div className="flex items-center gap-2 mb-4">
                <Icon name="Activity" size={18} />
                <span style={{ fontWeight: "var(--font-semibold)" }}>系统状态</span>
              </div>
              <div className="flex flex-col gap-3" style={{ fontSize: "var(--text-sm)" }}>
                <div className="flex items-center justify-between">
                  <span className="text-secondary">GPU</span>
                  <span className="text-mono" style={{ fontSize: "var(--text-xs)" }}>{gpuText}</span>
                </div>
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-secondary">显存占用</span>
                    <span className="text-mono" style={{ fontSize: "var(--text-xs)" }}>
                      {vram ? `${Math.round(vram.used_mb || 0)}/${Math.round(vram.capacity_mb || 0)}MB` : "--"}
                    </span>
                  </div>
                  <Progress value={vramRatio} />
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-secondary">版本</span>
                  <span className="text-mono" style={{ fontSize: "var(--text-xs)" }}>v{(sys && sys.version) || "1.0.0"}</span>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Icon name="Zap" size={18} />
                <span style={{ fontWeight: "var(--font-semibold)" }}>快捷入口</span>
              </div>
              <div className="flex flex-col gap-2">
                <button className="btn btn-ghost w-full" onClick={() => nav("chat")}><Icon name="MessageSquare" size={15} /> AI 对话</button>
                <button className="btn btn-ghost w-full" onClick={() => nav("knowledge")}><Icon name="GraduationCap" size={15} /> 知识学习</button>
                <button className="btn btn-ghost w-full" onClick={() => nav("assets")}><Icon name="FolderOpen" size={15} /> 资产库</button>
              </div>
            </div>
          </aside>
        </div>

        {/* 右下悬浮快捷按钮：跳转到漫剧创作 */}
        <button aria-label="开始创作" onClick={() => nav("manga")}
          style={{
            position: "fixed", right: "var(--space-8)", bottom: "var(--space-8)",
            width: 56, height: 56, borderRadius: "50%", border: "none", cursor: "pointer",
            background: "var(--color-primary)", color: "#fff", boxShadow: "var(--shadow-lg)",
            display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
            transition: "transform var(--dur-hover) var(--ease-spring)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.transform = "scale(1.08)")}
          onMouseLeave={(e) => (e.currentTarget.style.transform = "scale(1)")}>
          <Icon name="Clapperboard" size={24} />
        </button>
      </div>
    );
  }

  /* ==================== AI 对话（文档 2.1.4：60/40 + 悬浮输入栏） ==================== */
  function Markdown({ text }) {
    const html = useMemo(() => {
      try {
        const raw = window.marked ? window.marked.parse(text || "") : (text || "");
        return window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw;
      } catch (e) { return text || ""; }
    }, [text]);
    return <div className="md-body" style={{ lineHeight: "var(--leading-relaxed)" }}
                dangerouslySetInnerHTML={{ __html: html }} />;
  }

  function ChatPage() {
    const [sessions, setSessions] = useState([]);
    const [sessionId, setSessionId] = useState(null);
    const [messages, setMessages] = useState([]);
    const [mode, setMode] = useState("fast");
    const [input, setInput] = useState("");
    const [images, setImages] = useState([]);
    const [streaming, setStreaming] = useState(false);
    const [showHistory, setShowHistory] = useState(false);
    const abortRef = useRef(null);
    const listRef = useRef(null);

    const loadSessions = useCallback(async () => {
      try { const d = await api("/chat/sessions"); setSessions(d.items || []); } catch (e) {}
    }, []);
    useEffect(() => { loadSessions(); }, [loadSessions]);

    const openSession = async (sid) => {
      setSessionId(sid);
      try {
        const d = await api(`/chat/sessions/${sid}/messages`);
        setMessages((d.items || []).map((m) => ({ role: m.role, content: m.content })));
      } catch (e) { setMessages([]); }
    };

    useEffect(() => {
      if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
    }, [messages, streaming]);

    const send = async () => {
      const text = input.trim();
      if (!text || streaming) return;
      setInput("");
      const next = [...messages, { role: "user", content: text }, { role: "assistant", content: "" }];
      setMessages(next);
      setStreaming(true);
      abortRef.current = new AbortController();
      try {
        await apiSSE("/chat/completions",
          { message: text, session_id: sessionId, mode, images },
          {
            signal: abortRef.current.signal,
            onMeta: (obj) => { if (obj.session_id && !sessionId) { setSessionId(obj.session_id); loadSessions(); } },
            onToken: (tok) => {
              setMessages((ms) => {
                const cp = ms.slice();
                cp[cp.length - 1] = { role: "assistant", content: cp[cp.length - 1].content + tok };
                return cp;
              });
            },
          });
      } catch (e) {
        if (e.name !== "AbortError") {
          setMessages((ms) => {
            const cp = ms.slice();
            cp[cp.length - 1] = { role: "assistant", content: cp[cp.length - 1].content + `\n\n[错误] ${e.message}` };
            return cp;
          });
        }
      } finally { setStreaming(false); setImages([]); }
    };

    const stop = async () => {
      try { abortRef.current && abortRef.current.abort(); } catch (e) {}
      try { await api("/chat/stop", { method: "POST", body: { session_id: sessionId } }); } catch (e) {}
      setStreaming(false);
    };

    const newSession = () => { setSessionId(null); setMessages([]); setShowHistory(false); };
    const removeSession = async (sid) => {
      try { await api("/chat/sessions/" + sid, { method: "DELETE" }); toast.ok("会话已删除"); loadSessions(); if (sid === sessionId) newSession(); }
      catch (e) { toast.err(e.message); }
    };
    const pickImage = async (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try {
        const d = await api("/image/upload", { method: "POST", formData: fd, timeout: 60000 });
        setImages((xs) => [...xs, d.path]);
        toast.ok("图片已附加");
      } catch (err) { toast.err(err.message); }
    };

    return (
      <div className="page flex flex-col" style={{ height: "100%", paddingBottom: 96 }}>
        <header className="flex items-center justify-between mb-4">
          <div>
            <h1 style={{ margin: 0, fontSize: "var(--text-2xl)", fontWeight: "var(--font-bold)" }}>AI 对话</h1>
            <p className="text-tertiary mt-2" style={{ margin: 0, fontSize: "var(--text-xs)" }}>本地 Qwen2-VL 多模态 · SSE 流式 · 纠正自动入库</p>
          </div>
          <div className="flex items-center gap-3">
            {/* 顶部模式切换开关 */}
            <div role="tablist" aria-label="对话模式" className="flex"
                 style={{ background: "var(--color-primary-soft)", borderRadius: "var(--radius-pill)", padding: 3 }}>
              {[["fast", "快速模式"], ["expert", "专家模式"]].map(([v, t]) => (
                <button key={v} role="tab" aria-selected={mode === v} onClick={() => setMode(v)}
                  className="btn" style={{
                    borderRadius: "var(--radius-pill)", padding: "6px 16px",
                    background: mode === v ? "var(--color-primary)" : "transparent",
                    color: mode === v ? "#fff" : "var(--color-primary-deep)",
                  }}>{t}</button>
              ))}
            </div>
            <button className="btn btn-secondary" onClick={() => setShowHistory(!showHistory)} aria-expanded={showHistory}>
              <Icon name="History" size={15} /> 历史
            </button>
            <button className="btn btn-secondary" onClick={newSession}><Icon name="Plus" size={15} /> 新对话</button>
          </div>
        </header>

        <div className="flex gap-6 flex-1" style={{ minHeight: 0 }}>
          {/* 左 60% 对话区 */}
          <section className="card flex flex-col" style={{ flex: "0 0 60%", minHeight: 0, padding: "var(--space-4)" }}>
            <div ref={listRef} className="flex-1 flex flex-col gap-4" style={{ overflowY: "auto", minHeight: 0, padding: "var(--space-2)" }} aria-live="polite">
              {messages.length === 0 && (
                <EmptyState art="💬" title="开始一段对话" hint="支持剧本创作、分镜讨论、图片理解；纠正回答会自动学习入库" />
              )}
              {messages.map((m, i) => (
                <div key={i} className="flex" style={{ justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
                  <div style={{
                    maxWidth: "78%", padding: "var(--space-3) var(--space-4)",
                    borderRadius: m.role === "user" ? "var(--radius-lg) var(--radius-lg) 4px var(--radius-lg)" : "var(--radius-lg) var(--radius-lg) var(--radius-lg) 4px",
                    background: m.role === "user" ? "var(--color-primary)" : "var(--color-surface-muted)",
                    color: m.role === "user" ? "#fff" : "var(--color-text)",
                    boxShadow: "var(--shadow-sm)", fontSize: "var(--text-sm)",
                  }}>
                    {m.role === "assistant" ? <Markdown text={m.content} /> : <span style={{ whiteSpace: "pre-wrap" }}>{m.content}</span>}
                    {streaming && i === messages.length - 1 && m.role === "assistant" && (
                      <span className="text-mono" style={{ opacity: 0.6 }}>▍</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* 右 40% 预览区 */}
          <aside className="flex-1 flex flex-col gap-4" style={{ minWidth: 0 }}>
            <div className="card flex-1 flex flex-col" style={{ minHeight: 0 }}>
              <div className="flex items-center gap-2 mb-3">
                <Icon name="Eye" size={16} /><span style={{ fontWeight: "var(--font-semibold)" }}>预览</span>
              </div>
              {images.length > 0 ? (
                <div className="flex flex-col gap-3" style={{ overflowY: "auto" }}>
                  {images.map((p, i) => (
                    <div key={i} style={{ position: "relative" }}>
                      <img src={fileUrl(p)} alt={"附加图片" + (i + 1)} style={{ width: "100%", borderRadius: "var(--radius-md)" }} />
                      <button className="btn btn-icon" aria-label="移除图片" onClick={() => setImages(images.filter((_, j) => j !== i))}
                        style={{ position: "absolute", top: 6, right: 6, background: "rgba(255,255,255,0.85)" }}>
                        <Icon name="X" size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center text-tertiary" style={{ gap: 8 }}>
                  <Icon name="Image" size={40} />
                  <div style={{ fontSize: "var(--text-xs)" }}>附加图片后在此预览；AI 可理解图片内容</div>
                </div>
              )}
            </div>
            <div className="card">
              <div className="flex items-center gap-2 mb-2">
                <Icon name="Compass" size={16} /><span style={{ fontWeight: "var(--font-semibold)" }}>空间推理</span>
              </div>
              <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: "var(--leading-relaxed)" }}>
                附加图片并提问「图中物体的空间关系」，即可触发 7 步空间推理（深度估计 + 物体检测 + 遮挡/方位/物理约束）。
              </p>
            </div>
          </aside>
        </div>

        {/* 底部悬浮毛玻璃输入栏 */}
        <div style={{
          position: "absolute", left: "50%", transform: "translateX(-50%)", bottom: "var(--space-5)",
          width: "min(760px, 72%)", background: "var(--color-surface-glass)",
          backdropFilter: "var(--glass-blur)", WebkitBackdropFilter: "var(--glass-blur)",
          borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-lg)",
          border: "1px solid var(--color-border)", padding: "var(--space-3)",
          display: "flex", alignItems: "flex-end", gap: "var(--space-3)", zIndex: 40,
        }}>
          <label className="btn btn-icon" aria-label="附加图片" style={{ cursor: "pointer", flexShrink: 0 }}>
            <Icon name="ImagePlus" size={18} />
            <input type="file" accept="image/*" style={{ display: "none" }} onChange={pickImage} />
          </label>
          <textarea className="textarea flex-1" rows={2} placeholder="输入消息，Enter 发送 / Shift+Enter 换行…"
            value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            style={{ border: "none", background: "transparent", minHeight: 44 }} aria-label="消息输入" />
          {streaming
            ? <button className="btn btn-secondary" onClick={stop}><Icon name="Square" size={15} /> 停止</button>
            : <button className="btn btn-primary" onClick={send} disabled={!input.trim()}><Icon name="Send" size={15} /> 发送</button>}
        </div>

        {/* 对话历史管理（二级抽屉） */}
        <Modal open={showHistory} onClose={() => setShowHistory(false)} title="对话历史" width={520}>
          {sessions.length === 0 ? <EmptyState art="🗂️" title="暂无历史会话" /> : (
            <div className="flex flex-col gap-2">
              {sessions.map((s) => (
                <div key={s.id} className="flex items-center gap-3 card" style={{ padding: "var(--space-3)" }}>
                  <button className="flex-1 text-secondary" onClick={() => openSession(s.id)}
                    style={{ border: "none", background: "none", textAlign: "left", cursor: "pointer", fontFamily: "var(--font-sans)", fontSize: "var(--text-sm)" }}>
                    <div style={{ fontWeight: "var(--font-medium)", color: "var(--color-text)" }}>{s.title}</div>
                    <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                      {MODE_LABEL[s.mode] || s.mode} · {s.updated_at ? new Date(s.updated_at * 1000).toLocaleString("zh-CN") : ""}
                    </div>
                  </button>
                  <button className="btn btn-icon" aria-label="删除会话" onClick={() => removeSession(s.id)}><Icon name="Trash2" size={15} /></button>
                </div>
              ))}
            </div>
          )}
        </Modal>
      </div>
    );
  }

  /* ==================== AI 绘画（文档 3.2：文生图/图生图/多图/首尾帧/图生视频） ==================== */
  const IMG_TABS = [
    { key: "text2img", label: "文生图", icon: "Image" },
    { key: "img2img", label: "图生图", icon: "Images" },
    { key: "multi", label: "多图生成", icon: "LayoutGrid" },
    { key: "firstlast", label: "首尾帧", icon: "GalleryHorizontal" },
    { key: "img2video", label: "图生视频", icon: "Clapperboard" },
  ];

  function ImagePage() {
    const [tab, setTab] = useState("text2img");
    const [prompt, setPrompt] = useState("");
    const [negative, setNegative] = useState("");
    const [mode, setMode] = useState("fast");
    const [seed, setSeed] = useState("");
    const [count, setCount] = useState(4);
    const [strength, setStrength] = useState(0.6);
    const [refImage, setRefImage] = useState("");
    const [running, setRunning] = useState(false);
    const [engine, setEngine] = useState(null);
    const [history, setHistory] = useState({ items: [], total: 0 });
    const [histPage, setHistPage] = useState(1);

    const loadStatus = useCallback(async () => {
      try { setEngine(await api("/image/status")); } catch (e) {}
    }, []);
    const loadHistory = useCallback(async (p = histPage) => {
      try { setHistory(await api(`/image/history?page=${p}&page_size=12`)); } catch (e) {}
    }, [histPage]);
    useEffect(() => { loadStatus(); loadHistory(1); }, []);

    // 任务完成 → 刷新历史（WebSocket 事件总线）
    useEffect(() => Bus.on("task-progress-update", ({ type }) => {
      if (type === "task_completed" || type === "task_failed") { loadHistory(1); setRunning(false); }
    }), [loadHistory]);

    const uploadRef = async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const d = await api("/image/upload", { method: "POST", formData: fd, timeout: 60000 });
        setRefImage(d.path);
        toast.ok("参考图已上传");
      } catch (err) { toast.err(err.message); }
    };

    const submit = async () => {
      if (!prompt.trim() && tab !== "img2video") { toast.warn("请输入 Prompt"); return; }
      const body = { prompt, mode, sync: false };
      if (tab === "text2img") { body.negative = negative; if (seed) body.seed = Number(seed); }
      if (tab === "img2img") {
        if (!refImage) { toast.warn("请先上传参考图"); return; }
        Object.assign(body, { image_path: refImage, strength });
      }
      if (tab === "multi") body.count = count;
      if (tab === "img2video") {
        if (!refImage) { toast.warn("请先上传首帧图片"); return; }
        Object.assign(body, { image_path: refImage, motion_prompt: prompt });
        if (seed) body.seed = Number(seed);
      }
      setRunning(true);
      try {
        const d = await api(`/image/${tab}`, { method: "POST", body, timeout: 60000 });
        if (d.queued) toast.info("任务已入队，GPU 队列执行中…");
        else { toast.ok("生成完成"); loadHistory(1); setRunning(false); }
      } catch (err) { toast.err(err.message); setRunning(false); }
    };

    const adopt = async (id, isAdopt) => {
      try {
        await api(`/image/history/${id}/${isAdopt ? "adopt" : "discard"}`, { method: "POST" });
        toast.ok(isAdopt ? "已采纳，偏好画像已更新" : "已记录弃用");
      } catch (err) { toast.err(err.message); }
    };
    const removeHist = async (id) => {
      try { await api(`/image/history/${id}`, { method: "DELETE" }); loadHistory(histPage); }
      catch (err) { toast.err(err.message); }
    };

    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="flex items-center justify-between mb-6">
          <div>
            <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>AI 绘画</h1>
            <p className="text-secondary" style={{ margin: "4px 0 0" }}>
              {engine ? `引擎：${engine.backend || "rule"} · 个性化默认：${engine.personalized ? "已启用" : "未启用"}` : "引擎状态加载中…"}
            </p>
          </div>
          <div className="seg">
            {[["fast", "快速"], ["expert", "专家"]].map(([k, lb]) => (
              <button key={k} className={"seg-item" + (mode === k ? " active" : "")} onClick={() => setMode(k)}>{lb}</button>
            ))}
          </div>
        </header>

        <div className="card mb-6" style={{ padding: "var(--space-5)" }}>
          <div className="flex gap-2 mb-4" role="tablist" aria-label="生成类型">
            {IMG_TABS.map((t) => (
              <button key={t.key} role="tab" aria-selected={tab === t.key}
                className={"btn " + (tab === t.key ? "btn-primary" : "btn-secondary")} onClick={() => setTab(t.key)}>
                <Icon name={t.icon} size={15} /> {t.label}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-3">
            <textarea className="textarea" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)}
              placeholder={tab === "img2video" ? "运动描述（如：镜头缓慢推近，花瓣飘落）" : "描述你想要的画面…"} aria-label="Prompt" />
            {tab === "text2img" && (
              <textarea className="textarea" rows={2} value={negative} onChange={(e) => setNegative(e.target.value)}
                placeholder="负面提示词（可选）" aria-label="负面提示词" />)}
            <div className="flex items-center gap-4 flex-wrap">
              {(tab === "img2img" || tab === "img2video") && (
                <label className="btn btn-ghost" style={{ cursor: "pointer" }}>
                  <Icon name="Upload" size={15} /> {refImage ? "已选参考图 ✓" : "上传参考图"}
                  <input type="file" accept="image/*" style={{ display: "none" }} onChange={uploadRef} />
                </label>
              )}
              {tab === "img2img" && (
                <label className="flex items-center gap-2 text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                  强度 {strength.toFixed(2)}
                  <input type="range" min="0.1" max="1" step="0.05" value={strength}
                    onChange={(e) => setStrength(Number(e.target.value))} aria-label="图生图强度" />
                </label>
              )}
              {tab === "multi" && (
                <label className="flex items-center gap-2 text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                  数量
                  <select className="input" value={count} onChange={(e) => setCount(Number(e.target.value))} style={{ width: 72 }}>
                    {[1, 2, 3, 4, 6, 9].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
              )}
              {(tab === "text2img" || tab === "img2video") && (
                <input className="input" style={{ width: 140 }} placeholder="种子（可选）" value={seed}
                  onChange={(e) => setSeed(e.target.value.replace(/[^\d]/g, ""))} aria-label="随机种子" />)}
              <button className="btn btn-primary" onClick={submit} disabled={running} style={{ marginLeft: "auto" }}>
                {running ? <Spinner size={16} /> : <Icon name="Wand2" size={15} />} 开始生成
              </button>
            </div>
          </div>
        </div>

        <section aria-label="生成历史">
          <div className="flex items-center justify-between mb-3">
            <h2 style={{ margin: 0, fontSize: "var(--text-xl)", fontWeight: "var(--font-semibold)" }}>生成历史</h2>
            <span className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>共 {history.total} 条</span>
          </div>
          {history.items.length === 0 ? <EmptyState art="🎨" title="暂无生成记录" hint="提交第一个生成任务吧" /> : (
            <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: "var(--space-4)" }}>
              {history.items.map((h) => (
                <div key={h.id} className="card" style={{ overflow: "hidden", padding: 0 }}>
                  <div style={{ aspectRatio: "16/9", background: "var(--color-primary-grad-from)", position: "relative" }}>
                    {h.image_path
                      ? <img src={fileUrl(h.image_path)} alt={h.prompt} loading="lazy"
                          style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                      : <div className="flex items-center justify-center" style={{ height: "100%" }}><Icon name="ImageOff" size={28} /></div>}
                  </div>
                  <div style={{ padding: "var(--space-3)" }}>
                    <div className="text-secondary ellipsis" style={{ fontSize: "var(--text-sm)" }} title={h.prompt}>{h.prompt || "(无描述)"}</div>
                    <div className="flex items-center gap-1 mt-2">
                      <span className="tag">{h.kind}</span>
                      <div style={{ marginLeft: "auto" }} className="flex gap-1">
                        <button className="btn btn-icon" title="采纳" onClick={() => adopt(h.id, true)}><Icon name="ThumbsUp" size={14} /></button>
                        <button className="btn btn-icon" title="弃用" onClick={() => adopt(h.id, false)}><Icon name="ThumbsDown" size={14} /></button>
                        <button className="btn btn-icon" title="删除" onClick={() => removeHist(h.id)}><Icon name="Trash2" size={14} /></button>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    );
  }

  /* ==================== 知识学习（文档 6.2：文本/单页/自动/批量 + 监控） ==================== */
  function KnowledgePage() {
    const [stats, setStats] = useState(null);
    const [text, setText] = useState("");
    const [url, setUrl] = useState("");
    const [autoUrl, setAutoUrl] = useState("");
    const [autoDepth, setAutoDepth] = useState(3);
    const [autoPages, setAutoPages] = useState(50);
    const [autoSession, setAutoSession] = useState(null);
    const [autoStatus, setAutoStatus] = useState(null);
    const [batchUrls, setBatchUrls] = useState("");
    const [query, setQuery] = useState("");
    const [results, setResults] = useState([]);
    const [corrections, setCorrections] = useState([]);
    const [history, setHistory] = useState([]);
    const [trainData, setTrainData] = useState([]);
    const [ktab, setKtab] = useState("learn");      // learn | train | quality（文档 2.1 统一学习系统）
    const pollRef = useRef(null);

    const refresh = useCallback(async () => {
      try { setStats(await api("/knowledge/stats")); } catch (e) {}
      try { setCorrections((await api("/knowledge/corrections")).items || []); } catch (e) {}
      try { setHistory((await api("/learning/history")).items || []); } catch (e) {}
      try { setTrainData((await api("/learning/training-data")).items || []); } catch (e) {}
    }, []);
    useEffect(() => { refresh(); return () => clearInterval(pollRef.current); }, [refresh]);

    const startPoll = (sid) => {
      clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const st = await api(`/learning/auto/${sid}`);
          setAutoStatus(st);
          if (st.status !== "running") {
            clearInterval(pollRef.current);
            refresh();
          }
        } catch (e) { clearInterval(pollRef.current); }
      }, 2000);
    };

    const doLearnText = async () => {
      if (text.trim().length < 10) { toast.warn("学习内容至少 10 字符"); return; }
      try {
        await api("/learning/text", { method: "POST", body: { text, source: "manual" } });
        toast.ok("文本学习完成"); setText(""); refresh();
      } catch (err) { toast.err(err.message); }
    };
    const doLearnUrl = async () => {
      if (!/^https?:\/\//.test(url)) { toast.warn("请输入合法 http/https URL"); return; }
      try {
        const d = await api("/learning/url", { method: "POST", body: { url }, timeout: 60000 });
        d.ok === false ? toast.warn(d.error || "学习失败") : toast.ok(`已提取 ${d.points || 0} 个知识点`);
        setUrl(""); refresh();
      } catch (err) { toast.err(err.message); }
    };
    const doAuto = async () => {
      if (!/^https?:\/\//.test(autoUrl)) { toast.warn("请输入合法 http/https URL"); return; }
      try {
        const d = await api("/learning/auto", {
          method: "POST", timeout: 60000,
          body: { url: autoUrl, max_depth: autoDepth, max_pages: autoPages },
        });
        const sid = d.session_id || (d.auto_session && d.auto_session.session_id);
        if (sid) { setAutoSession(sid); setAutoStatus({ status: "running", pages_done: 0 }); startPoll(sid); toast.info("自动学习已启动"); }
        else { toast.ok("自动学习已完成"); refresh(); }
      } catch (err) { toast.err(err.message); }
    };
    const stopAuto = async () => {
      if (!autoSession) return;
      try { await api(`/learning/auto/${autoSession}/stop`, { method: "POST" }); toast.ok("已发送停止信号"); }
      catch (err) { toast.err(err.message); }
    };
    const doBatch = async () => {
      const urls = batchUrls.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!urls.length) { toast.warn("请每行输入一个 URL"); return; }
      try {
        const d = await api("/learning/urls/batch", { method: "POST", body: { urls } });
        toast.ok(`已登记 ${d.count} 个批量任务`); setBatchUrls(""); refresh();
      } catch (err) { toast.err(err.message); }
    };
    const doSearch = async () => {
      if (!query.trim()) return;
      try { setResults((await api(`/knowledge/search?q=${encodeURIComponent(query)}&n=8`)).items || []); }
      catch (err) { toast.err(err.message); }
    };
    const removePoint = async (id) => {
      try { await api(`/knowledge/${id}`, { method: "DELETE" }); toast.ok("已提交删除"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const rebuild = async () => {
      try { const d = await api("/knowledge/rebuild", { method: "POST" }); toast.ok(`索引已清空（${d.cleared} 条），可重新学习`); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const makeQA = async () => {
      const pts = results.map((r) => r.document || r.text || "").filter(Boolean).slice(0, 10);
      if (!pts.length) { toast.warn("请先搜索知识点作为 QA 素材"); return; }
      try {
        const d = await api("/learning/qa", { method: "POST", body: { points: pts } });
        toast.ok(`已生成 ${d.qa_pairs} 对 QA 训练数据`); refresh();
      } catch (err) { toast.err(err.message); }
    };

    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="flex items-center justify-between mb-6">
          <div>
            <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>知识学习</h1>
            <p className="text-secondary" style={{ margin: "4px 0 0" }}>
              {stats ? `知识点 ${stats.knowledge_count} · 纠错 ${stats.corrections_count} · 向量模式 ${stats.mode}` : "统计加载中…"}
            </p>
          </div>
          <button className="btn btn-ghost" onClick={rebuild}><Icon name="RefreshCcw" size={15} /> 重建索引</button>
        </header>

        {/* 统一学习系统 Tab（文档 2.1：学习入口 / 五模型训练面板 / 质量评估） */}
        <div className="flex gap-2 mb-6" role="tablist" aria-label="学习分区">
          {[["learn", "学习入口", "BookOpen"], ["train", "训练面板", "BrainCog"], ["quality", "质量评估", "Gauge"]].map(([k, lb, ic]) => (
            <button key={k} role="tab" aria-selected={ktab === k}
              className={"btn " + (ktab === k ? "btn-primary" : "btn-secondary")} onClick={() => setKtab(k)}>
              <Icon name={ic} size={15} /> {lb}
            </button>
          ))}
        </div>

        {ktab === "train" && <TrainPanel />}
        {ktab === "quality" && <QualityPanel />}

        {ktab === "learn" && (
        <div className="grid" style={{ gridTemplateColumns: "minmax(340px, 5fr) minmax(380px, 7fr)", gap: "var(--space-5)", alignItems: "start" }}>
          {/* 左：学习入口 */}
          <div className="flex flex-col gap-5">
            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="FileText" size={16} /> 文本学习</h3>
              <textarea className="textarea" rows={4} value={text} onChange={(e) => setText(e.target.value)}
                placeholder="粘贴需要学习的文本（≥10 字符）…" aria-label="学习文本" />
              <div className="flex mt-3" style={{ justifyContent: "flex-end" }}>
                <button className="btn btn-primary" onClick={doLearnText}><Icon name="Brain" size={15} /> 学习文本</button>
              </div>
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="Globe" size={16} /> 单页学习</h3>
              <div className="flex gap-2">
                <input className="input flex-1" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" aria-label="单页 URL" />
                <button className="btn btn-primary" onClick={doLearnUrl}>学习</button>
              </div>
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="Spider" size={16} /> 自动学习（深度优先遍历）</h3>
              <input className="input" value={autoUrl} onChange={(e) => setAutoUrl(e.target.value)} placeholder="起始 URL（同域，≤3 层 / ≤50 页）" aria-label="自动学习起始 URL" />
              <div className="flex items-center gap-4 mt-3" style={{ fontSize: "var(--text-sm)" }}>
                <label className="flex items-center gap-2 text-secondary">深度
                  <select className="input" style={{ width: 64 }} value={autoDepth} onChange={(e) => setAutoDepth(Number(e.target.value))}>
                    {[1, 2, 3].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-secondary">页数上限
                  <select className="input" style={{ width: 80 }} value={autoPages} onChange={(e) => setAutoPages(Number(e.target.value))}>
                    {[10, 20, 30, 50].map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <div style={{ marginLeft: "auto" }} className="flex gap-2">
                  {autoStatus && autoStatus.status === "running" && (
                    <button className="btn btn-secondary" onClick={stopAuto}><Icon name="Square" size={14} /> 停止</button>)}
                  <button className="btn btn-primary" onClick={doAuto} disabled={autoStatus && autoStatus.status === "running"}>
                    <Icon name="Play" size={14} /> 启动
                  </button>
                </div>
              </div>
              {autoStatus && (
                <div className="mt-3">
                  <div className="flex justify-between text-secondary" style={{ fontSize: "var(--text-xs)" }}>
                    <span>会话 {autoSession}</span>
                    <span>{autoStatus.status} · 已学 {autoStatus.pages_done ?? autoStatus.pages ?? 0} 页</span>
                  </div>
                  <Progress value={(autoStatus.pages_done ?? 0) / Math.max(1, autoStatus.max_pages || autoPages)} indeterminate={autoStatus.status === "running" && !autoStatus.pages_done} />
                </div>
              )}
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="ListChecks" size={16} /> 批量提取（≤20/批）</h3>
              <textarea className="textarea" rows={3} value={batchUrls} onChange={(e) => setBatchUrls(e.target.value)}
                placeholder={"每行一个 URL\nhttps://example.com/a\nhttps://example.com/b"} aria-label="批量 URL" />
              <div className="flex mt-3" style={{ justifyContent: "flex-end" }}>
                <button className="btn btn-primary" onClick={doBatch}><Icon name="Layers" size={15} /> 批量登记</button>
              </div>
            </div>
          </div>

          {/* 右：检索 / 纠错 / 历史 / 训练数据 */}
          <div className="flex flex-col gap-5">
            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="Search" size={16} /> 知识点检索</h3>
              <div className="flex gap-2">
                <input className="input flex-1" value={query} onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && doSearch()} placeholder="输入关键词检索知识库…" aria-label="知识检索" />
                <button className="btn btn-primary" onClick={doSearch}>检索</button>
                <button className="btn btn-ghost" onClick={makeQA} title="以检索结果生成 QA 训练数据">生成 QA</button>
              </div>
              <div className="flex flex-col gap-2 mt-3">
                {results.map((r, i) => (
                  <div key={r.id || i} className="card" style={{ padding: "var(--space-3)", background: "var(--color-surface-muted)" }}>
                    <div style={{ fontSize: "var(--text-sm)", lineHeight: "var(--leading-relaxed)" }}>
                      {(r.document || r.text || "").slice(0, 220)}
                    </div>
                    <div className="flex items-center gap-2 mt-2">
                      {r.metadata && r.metadata.source && <span className="tag">{r.metadata.source}</span>}
                      {r.distance != null && <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>距离 {Number(r.distance).toFixed(3)}</span>}
                      {r.id && <button className="btn btn-icon" style={{ marginLeft: "auto" }} title="删除知识点" onClick={() => removePoint(r.id)}><Icon name="Trash2" size={13} /></button>}
                    </div>
                  </div>
                ))}
                {results.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>输入关键词开始检索。</div>}
              </div>
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="MessageSquareWarning" size={16} /> 对话纠错库（{corrections.length}）</h3>
              <div className="flex flex-col gap-2" style={{ maxHeight: 180, overflow: "auto" }}>
                {corrections.slice(0, 20).map((c, i) => (
                  <div key={c.id || i} className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                    <span className="tag">纠错</span> {(c.document || "").slice(0, 120)}
                  </div>
                ))}
                {corrections.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无纠错记录。</div>}
              </div>
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="History" size={16} /> 学习历史</h3>
              <div className="flex flex-col gap-2" style={{ maxHeight: 180, overflow: "auto" }}>
                {history.slice(0, 20).map((h, i) => (
                  <div key={h.id || i} className="flex items-center gap-2 text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                    <span className="tag">{h.kind || h.source || "learn"}</span>
                    <span className="ellipsis flex-1" title={h.title || h.url || h.summary}>{h.title || h.url || h.summary || h.id}</span>
                    <span className="text-tertiary">{h.points != null ? `${h.points} 点` : ""}</span>
                  </div>
                ))}
                {history.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无学习记录。</div>}
              </div>
            </div>

            <div className="card" style={{ padding: "var(--space-5)" }}>
              <h3 className="section-title"><Icon name="Database" size={16} /> 训练数据（QA 对）</h3>
              <div className="flex flex-col gap-2" style={{ maxHeight: 160, overflow: "auto" }}>
                {trainData.map((t) => (
                  <div key={t.file} className="flex items-center gap-2 text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                    <Icon name="FileJson" size={14} />
                    <span className="ellipsis flex-1" title={t.path}>{t.file}</span>
                    <span className="text-tertiary">{Math.round(t.size / 1024)}KB</span>
                  </div>
                ))}
                {trainData.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无训练数据文件。</div>}
              </div>
            </div>
          </div>
        </div>
        )}
      </div>
    );
  }

  /* ==================== 五模型训练面板 + 训练队列（文档 2.1 统一学习系统） ==================== */
  function TrainPanel() {
    const [panel, setPanel] = useState(null);
    const [queue, setQueue] = useState([]);
    const [busy, setBusy] = useState("");
    const pollRef = useRef(null);

    const refresh = useCallback(async () => {
      try { setPanel(await api("/learning/panel")); } catch (e) {}
      try { setQueue((await api("/learning/train-queue")).items || []); } catch (e) {}
    }, []);
    useEffect(() => {
      refresh();
      pollRef.current = setInterval(refresh, 3000);
      return () => clearInterval(pollRef.current);
    }, [refresh]);

    const submit = async (mtype) => {
      setBusy("submit:" + mtype);
      try {
        const d = await api("/learning/train-queue", { method: "POST", body: { model_type: mtype } });
        toast.ok(`已入队（优先级 ${d.priority}）`);
        refresh();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };
    const setPrio = async (tid, prio) => {
      try { await api(`/learning/train-queue/${tid}/priority`, { method: "PUT", body: { priority: prio } }); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const move = async (tid, direction) => {
      try { await api(`/learning/train-queue/${tid}/move`, { method: "POST", body: { direction } }); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const cancel = async (tid) => {
      try { await api(`/learning/train-queue/${tid}/cancel`, { method: "POST" }); toast.ok("已取消"); refresh(); }
      catch (err) { toast.err(err.message); }
    };

    const STATUS_TAG = { queued: "tag-info", running: "tag-warning", done: "tag-success", failed: "tag-danger", cancelled: "" };
    const STATUS_LB = { queued: "排队", running: "运行", done: "完成", failed: "失败", cancelled: "已取消" };

    return (
      <div className="flex flex-col gap-5">
        {/* 队列概览 */}
        {panel && (
          <div className="flex gap-4 flex-wrap" style={{ fontSize: "var(--text-sm)" }}>
            <span className="text-secondary">队列：排队 <b>{panel.queue.queued}</b> · 运行 <b>{panel.queue.running}</b> · 上限 {panel.queue.max}</span>
            <span className="text-tertiary">调度器串行执行（priority 降序 / 提交时间升序）</span>
          </div>
        )}

        {/* 五模型卡片 */}
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "var(--space-4)" }}>
          {(panel ? panel.models : []).map((m) => (
            <div key={m.model_type} className="card" style={{ padding: "var(--space-4)" }}>
              <div className="flex items-center gap-2 mb-1">
                <Icon name={{ language: "MessageSquareText", image: "ImageIcon", voice: "AudioLines", video: "Clapperboard", auxiliary: "Cog" }[m.model_type] || "Box"} size={16} />
                <b>{m.name}</b>
                <span className={"tag " + (m.trainable ? "tag-success" : "tag-danger")} title={m.train_note}>
                  {m.trainable ? "可训练" : "硬件不足"}
                </span>
              </div>
              <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: "0 0 8px" }}>{m.purpose}</p>
              <div className="flex gap-3 flex-wrap mb-2" style={{ fontSize: "var(--text-xs)" }}>
                <span className="text-secondary">待训数据 <b>{m.pending_data}</b>{m.data_ready ? "" : "（未达开训下限）"}</span>
                <span className="text-secondary">运行 {m.running} · 排队 {m.queued}</span>
              </div>
              <div className="text-tertiary mb-2" style={{ fontSize: 10 }}>
                训练对象：{(m.targets || []).join(" / ")} · 默认优先级 {m.default_priority}
                {m.last_trained_at ? ` · 最近训练 ${new Date(m.last_trained_at * 1000).toLocaleString("zh-CN")}` : " · 尚未训练"}
              </div>
              <button className="btn btn-primary" style={{ padding: "4px 12px" }}
                disabled={!m.trainable || busy === "submit:" + m.model_type}
                title={m.trainable ? "提交训练任务到队列" : m.train_note}
                onClick={() => submit(m.model_type)}>
                {busy === "submit:" + m.model_type ? <Spinner size={13} /> : <Icon name="Play" size={13} />} 提交训练
              </button>
            </div>
          ))}
          {!panel && <div className="flex justify-center" style={{ padding: 40 }}><Spinner /></div>}
        </div>

        {/* 训练队列表 */}
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="ListOrdered" size={16} /> 训练队列（{queue.length}）</h3>
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>任务</th><th>模型</th><th>来源</th><th>优先级</th><th>状态</th><th>进度</th><th>提交时间</th><th>操作</th></tr></thead>
              <tbody>
                {queue.map((t) => (
                  <tr key={t.id}>
                    <td className="text-mono" style={{ fontSize: "var(--text-xs)" }}>{t.id.slice(0, 8)}</td>
                    <td>{t.model_name || t.model_type}</td>
                    <td><span className="tag">{t.learning_type}</span></td>
                    <td>
                      {t.status === "queued" ? (
                        <input type="number" min={1} max={10} className="input" style={{ width: 58, padding: "2px 6px" }}
                          value={t.priority} aria-label="优先级"
                          onChange={(e) => setPrio(t.id, parseInt(e.target.value || "1", 10))} />
                      ) : t.priority}
                    </td>
                    <td><span className={"tag " + (STATUS_TAG[t.status] || "")}>{STATUS_LB[t.status] || t.status}</span></td>
                    <td style={{ minWidth: 90 }}>
                      <Progress value={t.progress || 0} indeterminate={t.status === "running" && !t.progress} />
                    </td>
                    <td className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                      {t.created_at ? new Date(t.created_at * 1000).toLocaleString("zh-CN") : "—"}
                    </td>
                    <td>
                      <div className="flex gap-1">
                        <button className="btn btn-icon" title="上移（优先级+1）" disabled={t.status !== "queued"}
                          onClick={() => move(t.id, "up")}><Icon name="ArrowUp" size={13} /></button>
                        <button className="btn btn-icon" title="下移（优先级-1）" disabled={t.status !== "queued"}
                          onClick={() => move(t.id, "down")}><Icon name="ArrowDown" size={13} /></button>
                        <button className="btn btn-icon" title="取消任务" disabled={t.status !== "queued" && t.status !== "running"}
                          onClick={() => cancel(t.id)}><Icon name="XCircle" size={13} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
                {queue.length === 0 && (
                  <tr><td colSpan={8} className="text-tertiary" style={{ textAlign: "center", padding: 24 }}>队列为空，从上方模型卡片提交训练任务。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  }

  /* ==================== 质量评估面板（文档 2.1：四维评分 + 阈值准入） ==================== */
  function QualityPanel() {
    const [logs, setLogs] = useState([]);
    const [threshold, setThreshold] = useState(null);
    const [typeFilter, setTypeFilter] = useState("");
    const [evalText, setEvalText] = useState("");
    const [evalType, setEvalType] = useState("text");
    const [result, setResult] = useState(null);
    const [busy, setBusy] = useState(false);

    const refresh = useCallback(async () => {
      try {
        const d = await api("/learning/quality/logs" + (typeFilter ? `?content_type=${typeFilter}` : ""));
        setLogs(d.items || []); setThreshold(d.threshold);
      } catch (e) {}
    }, [typeFilter]);
    useEffect(() => { refresh(); }, [refresh]);

    const doEval = async () => {
      if (evalText.trim().length < 10) { toast.warn("评估文本至少 10 字符"); return; }
      setBusy(true);
      try {
        const d = await api("/learning/quality/evaluate", { method: "POST", timeout: 120000,
          body: { content_type: evalType, text: evalText } });
        setResult(d); refresh();
        toast.ok(`评估完成：综合 ${d.overall}/5（${d.status}）`);
      } catch (err) { toast.err(err.message); }
      finally { setBusy(false); }
    };

    const DIMS = [["text", "文本"], ["image", "图像"], ["video", "视频"], ["audio", "音频"]];
    return (
      <div className="flex flex-col gap-5">
        {/* 手动评估 */}
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="FlaskConical" size={16} /> 内容质量评估（四维 1-5 分）</h3>
          <div className="flex gap-2 mb-2">
            <select className="select" style={{ width: 130 }} value={evalType} onChange={(e) => setEvalType(e.target.value)} aria-label="内容类型">
              {[["text", "文本"], ["page", "网页"], ["image", "图像"], ["video", "视频"], ["audio", "音频"], ["mixed", "混合"]].map(([k, lb]) => (
                <option key={k} value={k}>{lb}</option>
              ))}
            </select>
            <input className="input flex-1" value={evalText} onChange={(e) => setEvalText(e.target.value)}
              placeholder="粘贴待评估内容文本（≥10 字符）…" aria-label="评估内容" />
            <button className="btn btn-primary" disabled={busy} onClick={doEval}>
              {busy ? <Spinner size={14} /> : <Icon name="Gauge" size={14} />} 评估
            </button>
          </div>
          {result && (
            <div className="flex items-center gap-3 flex-wrap">
              {DIMS.map(([k, lb]) => (
                <span key={k} className="tag">{lb} {result.scores[k] != null ? Number(result.scores[k]).toFixed(1) : "—"}</span>
              ))}
              <span className={"tag " + (result.status === "accepted" ? "tag-success" : "tag-danger")}>
                综合 {Number(result.overall).toFixed(1)} · {result.status === "accepted" ? "准入" : "拒绝"}
              </span>
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>评分后端：{result.backend}</span>
            </div>
          )}
        </div>

        {/* 评估日志 */}
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <h3 className="section-title" style={{ margin: 0 }}><Icon name="ScrollText" size={16} /> 评估日志（{logs.length}）</h3>
            {threshold != null && <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>准入阈值：综合 ≥ {threshold}</span>}
            <select className="input" style={{ width: 120, marginLeft: "auto" }} value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} aria-label="类型筛选">
              <option value="">全部类型</option>
              {["page", "text", "image", "video", "audio", "mixed"].map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>类型</th><th>文本</th><th>图像</th><th>视频</th><th>音频</th><th>综合</th><th>结论</th><th>时间</th></tr></thead>
              <tbody>
                {logs.map((l) => (
                  <tr key={l.id}>
                    <td><span className="tag">{l.content_type}</span></td>
                    {DIMS.map(([k]) => (
                      <td key={k}>{l.scores && l.scores[k] != null ? Number(l.scores[k]).toFixed(1) : "—"}</td>
                    ))}
                    <td><b>{l.overall_score != null ? Number(l.overall_score).toFixed(1) : Number(l.overall || 0).toFixed(1)}</b></td>
                    <td><span className={"tag " + (l.status === "accepted" ? "tag-success" : "tag-danger")}>{l.status}</span></td>
                    <td className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                      {l.evaluated_at ? new Date(l.evaluated_at * 1000).toLocaleString("zh-CN") : "—"}
                    </td>
                  </tr>
                ))}
                {logs.length === 0 && (
                  <tr><td colSpan={8} className="text-tertiary" style={{ textAlign: "center", padding: 24 }}>暂无评估记录。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  }

  /* ==================== 模型管理（文档 6.7：注册表/显存/下载/热替换/LoRA） ==================== */
  function ModelsPage() {
    const [models, setModels] = useState([]);
    const [vram, setVram] = useState(null);
    const [downloads, setDownloads] = useState([]);
    const [tier, setTier] = useState(null);
    const [weights, setWeights] = useState([]);
    const [jobs, setJobs] = useState([]);
    const [trainForm, setTrainForm] = useState({ name: "", data_path: "", character: false });
    const [charForm, setCharForm] = useState({ name: "", image_paths: "" });
    const [verifyForm, setVerifyForm] = useState({ character_name: "", candidate_path: "" });
    const [dlForm, setDlForm] = useState({ model_id: "", url: "" });
    const pollRef = useRef(null);

    const refresh = useCallback(async () => {
      try { setModels((await api("/models")).items || []); } catch (e) {}
      try { setVram(await api("/models/vram/status")); } catch (e) {}
      try { setDownloads((await api("/models/downloads/list")).items || []); } catch (e) {}
      try { setTier(await api("/lora/hardware")); } catch (e) {}
      try { setWeights((await api("/lora/weights")).items || []); } catch (e) {}
      try { setJobs((await api("/lora/jobs")).items || []); } catch (e) {}
    }, []);
    useEffect(() => {
      refresh();
      pollRef.current = setInterval(refresh, 5000);
      return () => clearInterval(pollRef.current);
    }, [refresh]);

    const verifyModel = async (id) => {
      try { const d = await api(`/models/${id}/verify`, { method: "POST", body: {} }); toast.ok(d.message || "校验完成"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const releaseModel = async (id) => {
      try { const d = await api("/system/vram/release", { method: "POST", body: { model_id: id } }); d.released ? toast.ok("已释放显存") : toast.warn("释放被拒绝（常驻或引用中）"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const startDownload = async () => {
      if (!dlForm.model_id || !/^https?:\/\//.test(dlForm.url)) { toast.warn("请填写模型 ID 与合法 URL"); return; }
      try { await api("/models/download", { method: "POST", body: dlForm }); toast.ok("下载已开始"); setDlForm({ model_id: "", url: "" }); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const cancelDownload = async (id) => {
      try { await api(`/models/downloads/${id}/cancel`, { method: "POST" }); toast.ok("已取消"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const startTrain = async () => {
      if (!trainForm.name.trim() || !trainForm.data_path.trim()) { toast.warn("请填写权重名称与训练数据路径"); return; }
      try { await api("/lora/train", { method: "POST", body: trainForm }); toast.ok("训练任务已提交"); setTrainForm({ name: "", data_path: "", character: false }); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const cancelJob = async (id) => {
      try { await api(`/lora/jobs/${id}/cancel`, { method: "POST" }); toast.ok("已取消"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const removeWeight = async (id) => {
      try { await api(`/lora/weights/${id}`, { method: "DELETE" }); toast.ok("权重已删除"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const registerChar = async () => {
      const paths = charForm.image_paths.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!charForm.name.trim() || paths.length < 3) { toast.warn("角色名必填，参考图至少 3 张（每行一个路径）"); return; }
      try { await api("/lora/characters/register", { method: "POST", body: { name: charForm.name, image_paths: paths } }); toast.ok("角色已登记，可开始训练"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const verifyChar = async () => {
      if (!verifyForm.character_name.trim() || !verifyForm.candidate_path.trim()) { toast.warn("请填写角色名与候选图路径"); return; }
      try {
        const d = await api("/lora/characters/verify", { method: "POST", body: verifyForm });
        d.passed ? toast.ok(`一致性验证通过（相似度 ${Number(d.similarity || 0).toFixed(3)}）`)
                 : toast.warn(`一致性未达标（相似度 ${Number(d.similarity || 0).toFixed(3)}，阈值 ${d.threshold}）`);
      } catch (err) { toast.err(err.message); }
    };

    const usedPct = vram && vram.total_mb ? Math.min(1, (vram.used_mb || 0) / vram.total_mb) : 0;

    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="mb-6">
          <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>模型管理</h1>
          <p className="text-secondary" style={{ margin: "4px 0 0" }}>注册表 · 显存池 · 下载 · LoRA 训练与权重</p>
        </header>

        {/* 显存池 */}
        <div className="card mb-6" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Gauge" size={16} /> 显存池（预算 7.5GB）</h3>
          {vram ? (
            <div>
              <div className="flex justify-between text-secondary mb-2" style={{ fontSize: "var(--text-sm)" }}>
                <span>已用 {Math.round(vram.used_mb || 0)}MB / {Math.round(vram.total_mb || 0)}MB</span>
                <span>已加载 {(vram.loaded || []).length} 个模型</span>
              </div>
              <Progress value={usedPct} />
              {vram.loaded && vram.loaded.length > 0 && (
                <div className="flex gap-2 flex-wrap mt-3">
                  {vram.loaded.map((m) => <span key={m.model_id || m} className="tag tag-info">{m.model_id || m}</span>)}
                </div>
              )}
            </div>
          ) : <div className="text-tertiary">显存状态加载中…</div>}
        </div>

        {/* 模型注册表 */}
        <div className="card mb-6" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Boxes" size={16} /> 模型注册表</h3>
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>模型</th><th>类型</th><th>大小</th><th>显存</th><th>策略</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.id}>
                    <td><div style={{ fontWeight: "var(--font-medium)", color: "var(--color-text)" }}>{m.name}</div>
                        <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{m.purpose}</div></td>
                    <td>{m.type}</td>
                    <td>{m.size_mb}MB</td>
                    <td>{m.vram_mb}MB</td>
                    <td>{m.load_policy}{m.resident ? " · 常驻" : ""}</td>
                    <td>
                      {m.loaded_in_vram ? <span className="tag tag-success">已加载</span>
                        : m.present_on_disk ? <span className="tag tag-info">在盘</span>
                        : <span className="tag tag-warning">缺失</span>}
                    </td>
                    <td>
                      <div className="flex gap-1">
                        <button className="btn btn-icon" title="校验完整性" onClick={() => verifyModel(m.id)}><Icon name="ShieldCheck" size={14} /></button>
                        {m.loaded_in_vram && !m.resident && (
                          <button className="btn btn-icon" title="释放显存" onClick={() => releaseModel(m.id)}><Icon name="Unplug" size={14} /></button>)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* 下载管理 */}
        <div className="card mb-6" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Download" size={16} /> 模型下载</h3>
          <div className="flex gap-2 mb-3 flex-wrap">
            <input className="input" style={{ width: 180 }} placeholder="模型 ID" value={dlForm.model_id}
              onChange={(e) => setDlForm({ ...dlForm, model_id: e.target.value })} aria-label="模型 ID" />
            <input className="input flex-1" placeholder="下载 URL（https://…）" value={dlForm.url}
              onChange={(e) => setDlForm({ ...dlForm, url: e.target.value })} aria-label="下载 URL" />
            <button className="btn btn-primary" onClick={startDownload}><Icon name="DownloadCloud" size={15} /> 开始下载</button>
          </div>
          {downloads.length > 0 && (
            <div className="flex flex-col gap-2">
              {downloads.slice(0, 8).map((d) => (
                <div key={d.id} className="flex items-center gap-3">
                  <span className="tag">{d.model_id}</span>
                  <div className="flex-1"><Progress value={d.progress || 0} /></div>
                  <span className="text-tertiary" style={{ fontSize: "var(--text-xs)", width: 110 }}>{d.status} {Math.round((d.progress || 0) * 100)}%</span>
                  {(d.status === "running" || d.status === "pending") && (
                    <button className="btn btn-icon" title="取消下载" onClick={() => cancelDownload(d.id)}><Icon name="X" size={14} /></button>)}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* LoRA 区 */}
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "var(--space-5)" }}>
          <div className="card" style={{ padding: "var(--space-5)" }}>
            <h3 className="section-title"><Icon name="Cpu" size={16} /> LoRA 硬件分层</h3>
            {tier ? (
              <div>
                <div className="flex items-center gap-2 mb-3">
                  {tier.lora_enabled ? <span className="tag tag-success">已解锁</span> : <span className="tag tag-warning">未解锁</span>}
                  <span className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>{tier.tier_label || tier.tier}</span>
                </div>
                <p className="text-secondary" style={{ fontSize: "var(--text-sm)", lineHeight: "var(--leading-relaxed)" }}>{tier.message}</p>
              </div>
            ) : <div className="text-tertiary">探测中…</div>}

            <h3 className="section-title mt-6"><Icon name="Dumbbell" size={16} /> 发起训练</h3>
            <div className="flex flex-col gap-2">
              <input className="input" placeholder="权重名称（如 style-ink-v1）" value={trainForm.name}
                onChange={(e) => setTrainForm({ ...trainForm, name: e.target.value })} aria-label="权重名称" />
              <input className="input" placeholder="训练数据路径（JSONL / QA 文件）" value={trainForm.data_path}
                onChange={(e) => setTrainForm({ ...trainForm, data_path: e.target.value })} aria-label="训练数据路径" />
              <label className="flex items-center gap-2 text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                <input type="checkbox" checked={trainForm.character}
                  onChange={(e) => setTrainForm({ ...trainForm, character: e.target.checked })} /> 角色 LoRA（SDXL UNet）
              </label>
              <button className="btn btn-primary" onClick={startTrain} disabled={tier && !tier.lora_enabled}>
                <Icon name="Play" size={15} /> 提交训练
              </button>
            </div>
          </div>

          <div className="card" style={{ padding: "var(--space-5)" }}>
            <h3 className="section-title"><Icon name="UserCheck" size={16} /> 角色 LoRA 登记与一致性验证</h3>
            <div className="flex flex-col gap-2">
              <input className="input" placeholder="角色名" value={charForm.name}
                onChange={(e) => setCharForm({ ...charForm, name: e.target.value })} aria-label="角色名" />
              <textarea className="textarea" rows={2} placeholder={"参考图路径，每行一个（≥3 张）"} value={charForm.image_paths}
                onChange={(e) => setCharForm({ ...charForm, image_paths: e.target.value })} aria-label="参考图路径" />
              <button className="btn btn-secondary" onClick={registerChar}><Icon name="UserPlus" size={15} /> 登记角色</button>
            </div>
            <div className="flex flex-col gap-2 mt-4">
              <input className="input" placeholder="待验证角色名" value={verifyForm.character_name}
                onChange={(e) => setVerifyForm({ ...verifyForm, character_name: e.target.value })} aria-label="待验证角色名" />
              <input className="input" placeholder="候选图路径" value={verifyForm.candidate_path}
                onChange={(e) => setVerifyForm({ ...verifyForm, candidate_path: e.target.value })} aria-label="候选图路径" />
              <button className="btn btn-ghost" onClick={verifyChar}><Icon name="ScanFace" size={15} /> 一致性验证（≥0.9）</button>
            </div>
          </div>

          <div className="card" style={{ padding: "var(--space-5)" }}>
            <h3 className="section-title"><Icon name="ListVideo" size={16} /> 训练任务</h3>
            <div className="flex flex-col gap-2" style={{ maxHeight: 220, overflow: "auto" }}>
              {jobs.map((j) => (
                <div key={j.job_id || j.id} className="flex items-center gap-2">
                  <span className="tag">{j.status}</span>
                  <span className="ellipsis flex-1 text-secondary" style={{ fontSize: "var(--text-sm)" }} title={j.name}>{j.name || j.job_id}</span>
                  {j.progress != null && <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{Math.round(j.progress * 100)}%</span>}
                  {(j.status === "running" || j.status === "queued") && (
                    <button className="btn btn-icon" title="取消训练" onClick={() => cancelJob(j.job_id || j.id)}><Icon name="X" size={13} /></button>)}
                </div>
              ))}
              {jobs.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无训练任务。</div>}
            </div>

            <h3 className="section-title mt-6"><Icon name="Archive" size={16} /> 权重库</h3>
            <div className="flex flex-col gap-2" style={{ maxHeight: 180, overflow: "auto" }}>
              {weights.map((w) => (
                <div key={w.id} className="flex items-center gap-2">
                  <Icon name="FileBox" size={14} />
                  <span className="ellipsis flex-1 text-secondary" style={{ fontSize: "var(--text-sm)" }} title={w.path}>{w.name}</span>
                  {w.base_model && <span className="tag">{w.base_model}</span>}
                  <button className="btn btn-icon" title="删除权重" onClick={() => removeWeight(w.id)}><Icon name="Trash2" size={13} /></button>
                </div>
              ))}
              {weights.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无权重。</div>}
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* ==================== 设置（文档 2.1.7：系统/任务/激活/更新/诊断/沙盒/插件/偏好） ==================== */
  const SETTING_TABS = [
    { key: "overview", label: "系统概览", icon: "MonitorCog" },
    { key: "tasks", label: "任务队列", icon: "ListTodo" },
    { key: "license", label: "激活与更新", icon: "KeyRound" },
    { key: "maint", label: "诊断与维护", icon: "Stethoscope" },
    { key: "sandbox", label: "沙盒与插件", icon: "Puzzle" },
    { key: "prefs", label: "偏好画像", icon: "SlidersHorizontal" },
  ];

  function SettingsPage() {
    const [tab, setTab] = useState("overview");
    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="mb-6">
          <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>设置</h1>
          <p className="text-secondary" style={{ margin: "4px 0 0" }}>系统状态 · 任务调度 · 许可与更新 · 维护工具</p>
        </header>
        <div className="flex gap-2 mb-6 flex-wrap" role="tablist" aria-label="设置分区">
          {SETTING_TABS.map((t) => (
            <button key={t.key} role="tab" aria-selected={tab === t.key}
              className={"btn " + (tab === t.key ? "btn-primary" : "btn-secondary")} onClick={() => setTab(t.key)}>
              <Icon name={t.icon} size={15} /> {t.label}
            </button>
          ))}
        </div>
        {tab === "overview" && <SettingsOverview />}
        {tab === "tasks" && <SettingsTasks />}
        {tab === "license" && <SettingsLicense />}
        {tab === "maint" && <SettingsMaint />}
        {tab === "sandbox" && <SettingsSandbox />}
        {tab === "prefs" && <SettingsPrefs />}
      </div>
    );
  }

  function SettingsOverview() {
    const [info, setInfo] = useState(null);
    const [gpu, setGpu] = useState(null);
    const [power, setPower] = useState(null);
    const [health, setHealth] = useState(null);
    useEffect(() => {
      (async () => {
        try { setInfo(await api("/system/info")); } catch (e) {}
        try { setGpu(await api("/system/gpu")); } catch (e) {}
        try { setPower(await api("/system/power")); } catch (e) {}
        try { setHealth(await api("/system/health")); } catch (e) {}
      })();
    }, []);
    const rows = info ? [
      ["应用", `${info.app} v${info.version}`],
      ["Python", info.python],
      ["平台", info.platform],
      ["CUDA", info.cuda ? "可用" : "不可用"],
      ["数据库加密", health ? health.db_encryption : "—"],
    ] : [];
    return (
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "var(--space-5)" }}>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Info" size={16} /> 系统信息</h3>
          <table className="table"><tbody>
            {rows.map(([k, v]) => <tr key={k}><td style={{ width: 110 }}>{k}</td><td>{String(v)}</td></tr>)}
          </tbody></table>
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Zap" size={16} /> GPU</h3>
          {gpu ? (
            <table className="table"><tbody>
              <tr><td style={{ width: 110 }}>型号</td><td>{gpu.name || "未检测到"}</td></tr>
              <tr><td>显存</td><td>{gpu.total_mb ? `${Math.round(gpu.total_mb)}MB（已用 ${Math.round(gpu.used_mb || 0)}MB）` : "—"}</td></tr>
              <tr><td>驱动</td><td>{gpu.driver || "—"}</td></tr>
            </tbody></table>
          ) : <div className="text-tertiary">检测中…</div>}
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="BatteryCharging" size={16} /> 电源状态</h3>
          {power ? (
            <table className="table"><tbody>
              <tr><td style={{ width: 110 }}>供电</td><td>{power.on_battery === undefined ? "—" : power.on_battery ? "电池" : "市电"}</td></tr>
              <tr><td>电量</td><td>{power.percent != null ? `${power.percent}%` : "—"}</td></tr>
              <tr><td>休眠事件</td><td>{power.sleep_events != null ? `${power.sleep_events} 次` : "—"}</td></tr>
            </tbody></table>
          ) : <div className="text-tertiary">检测中…</div>}
        </div>
      </div>
    );
  }

  function SettingsTasks() {
    const [stats, setStats] = useState(null);
    const [tasks, setTasks] = useState([]);
    const [filter, setFilter] = useState("");
    const pollRef = useRef(null);
    const refresh = useCallback(async () => {
      try { setStats(await api("/tasks/stats")); } catch (e) {}
      try { setTasks((await api("/tasks" + (filter ? `?state=${filter}` : ""))).items || []); } catch (e) {}
    }, [filter]);
    useEffect(() => {
      refresh();
      pollRef.current = setInterval(refresh, 3000);
      return () => clearInterval(pollRef.current);
    }, [refresh]);

    const act = async (id, action) => {
      try { await api(`/tasks/${id}/${action}`, { method: "POST" }); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const actAll = async (action) => {
      try { await api(`/tasks/${action}`, { method: "POST" }); toast.ok(action === "pause-all" ? "全部已暂停" : "全部已恢复"); refresh(); }
      catch (err) { toast.err(err.message); }
    };

    const STATE_TAG = { pending: "tag-info", running: "tag-warning", paused: "", completed: "tag-success", failed: "tag-error", cancelled: "" };
    return (
      <div className="card" style={{ padding: "var(--space-5)" }}>
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <h3 className="section-title" style={{ margin: 0 }}><Icon name="ListTodo" size={16} /> 任务队列{stats ? `（后端：${stats.backend || "local"}）` : ""}</h3>
          <select className="input" style={{ width: 130 }} value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="状态筛选">
            <option value="">全部状态</option>
            {["pending", "running", "paused", "completed", "failed", "cancelled"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <div style={{ marginLeft: "auto" }} className="flex gap-2">
            <button className="btn btn-secondary" onClick={() => actAll("pause-all")}><Icon name="Pause" size={14} /> 全部暂停</button>
            <button className="btn btn-primary" onClick={() => actAll("resume-all")}><Icon name="Play" size={14} /> 全部恢复</button>
          </div>
        </div>
        {stats && (
          <div className="flex gap-4 mb-4 flex-wrap" style={{ fontSize: "var(--text-sm)" }}>
            {Object.entries(stats.queues || {}).map(([q, s]) => (
              <span key={q} className="text-secondary">
                <span className="tag tag-info">{q}</span> 运行 {s.running ?? s.active ?? 0} / 排队 {s.pending ?? s.queued ?? 0}
              </span>
            ))}
          </div>
        )}
        <div style={{ overflowX: "auto" }}>
          <table className="table">
            <thead><tr><th>任务</th><th>类型</th><th>队列</th><th>优先级</th><th>状态</th><th>进度</th><th>操作</th></tr></thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.task_id || t.id}>
                  <td className="ellipsis" style={{ maxWidth: 220 }} title={t.task_id || t.id}>{(t.task_id || t.id || "").slice(0, 18)}…</td>
                  <td>{t.task_type}</td>
                  <td>{t.queue}</td>
                  <td>{t.priority}</td>
                  <td><span className={"tag " + (STATE_TAG[t.state] || "")}>{t.state}</span></td>
                  <td style={{ minWidth: 120 }}><Progress value={t.progress || 0} /></td>
                  <td>
                    <div className="flex gap-1">
                      {t.state === "running" && <button className="btn btn-icon" title="暂停" onClick={() => act(t.task_id || t.id, "pause")}><Icon name="Pause" size={13} /></button>}
                      {t.state === "paused" && <button className="btn btn-icon" title="恢复" onClick={() => act(t.task_id || t.id, "resume")}><Icon name="Play" size={13} /></button>}
                      {(t.state === "pending" || t.state === "running" || t.state === "paused") && (
                        <button className="btn btn-icon" title="取消" onClick={() => act(t.task_id || t.id, "cancel")}><Icon name="X" size={13} /></button>)}
                    </div>
                  </td>
                </tr>
              ))}
              {tasks.length === 0 && <tr><td colSpan="7" className="text-tertiary" style={{ textAlign: "center", padding: "var(--space-6)" }}>暂无任务</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  function SettingsLicense() {
    const [act, setAct] = useState(null);
    const [license, setLicense] = useState("");
    const [upd, setUpd] = useState(null);
    const [updHist, setUpdHist] = useState([]);
    const [updFile, setUpdFile] = useState(null);
    const refresh = useCallback(async () => {
      try { setAct(await api("/system/activation")); } catch (e) {}
      try { setUpd(await api("/system/update/status")); } catch (e) {}
      try { setUpdHist((await api("/system/update/history")).items || []); } catch (e) {}
    }, []);
    useEffect(() => { refresh(); }, [refresh]);

    const doActivate = async () => {
      if (!license.trim()) { toast.warn("请粘贴许可 JSON 内容"); return; }
      try { await api("/system/activation", { method: "POST", body: { license_key: license } }); toast.ok("激活成功"); setLicense(""); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const inspectPkg = async () => {
      if (!updFile) { toast.warn("请先选择更新包（.zip）"); return; }
      const fd = new FormData(); fd.append("file", updFile);
      try {
        const d = await api("/system/update/inspect", { method: "POST", formData: fd, timeout: 60000 });
        Modal.confirm = null;
        toast.info(`包信息：v${d.version || "?"} · ${d.files != null ? d.files + " 个文件" : ""} ${d.ok === false ? "（校验未通过）" : ""}`);
      } catch (err) { toast.err(err.message); }
    };
    const applyPkg = async () => {
      if (!updFile) { toast.warn("请先选择更新包（.zip）"); return; }
      const fd = new FormData(); fd.append("file", updFile);
      try { await api("/system/update/apply", { method: "POST", formData: fd, timeout: 120000 }); toast.ok("更新已应用，建议重启"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const rollback = async () => {
      try { await api("/system/update/rollback", { method: "POST", body: {} }); toast.ok("已回滚到上一版本"); refresh(); }
      catch (err) { toast.err(err.message); }
    };

    return (
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "var(--space-5)" }}>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="KeyRound" size={16} /> 离线激活（RSA-2048）</h3>
          {act && (
            <div className="mb-3">
              {act.activated ? <span className="tag tag-success">已激活</span> : <span className="tag tag-warning">未激活</span>}
              {act.product && <span className="text-secondary ml-2" style={{ fontSize: "var(--text-sm)" }}>{act.product} {act.edition || ""}</span>}
              {act.reason && !act.activated && <div className="text-tertiary mt-2" style={{ fontSize: "var(--text-sm)" }}>{act.reason}</div>}
            </div>
          )}
          <textarea className="textarea" rows={5} value={license} onChange={(e) => setLicense(e.target.value)}
            placeholder='粘贴 license.json 内容（{"product": "...", ...}）' aria-label="许可内容" />
          <div className="flex mt-3" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn-primary" onClick={doActivate}><Icon name="BadgeCheck" size={15} /> 导入并激活</button>
          </div>
        </div>

        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="PackageOpen" size={16} /> 离线增量更新</h3>
          {upd && (
            <div className="text-secondary mb-3" style={{ fontSize: "var(--text-sm)" }}>
              当前版本 {upd.current_version || "—"} {upd.backup_available ? "· 有可回滚备份" : ""}
            </div>
          )}
          <label className="btn btn-ghost" style={{ cursor: "pointer" }}>
            <Icon name="Upload" size={15} /> {updFile ? updFile.name : "选择更新包（.zip）"}
            <input type="file" accept=".zip" style={{ display: "none" }} onChange={(e) => setUpdFile(e.target.files && e.target.files[0])} />
          </label>
          <div className="flex gap-2 mt-3 flex-wrap">
            <button className="btn btn-secondary" onClick={inspectPkg}><Icon name="SearchCheck" size={14} /> 检查包</button>
            <button className="btn btn-primary" onClick={applyPkg}><Icon name="PackageCheck" size={14} /> 应用更新</button>
            <button className="btn btn-ghost" onClick={rollback}><Icon name="Undo2" size={14} /> 回滚</button>
          </div>
          {updHist.length > 0 && (
            <div className="mt-4 flex flex-col gap-1" style={{ maxHeight: 140, overflow: "auto" }}>
              {updHist.map((h, i) => (
                <div key={i} className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                  {h.time ? new Date((h.time || 0) * 1000).toLocaleString("zh-CN") : ""} · {h.action || h.status} · v{h.version || "?"}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  function SettingsMaint() {
    const [bench, setBench] = useState(null);
    const [diag, setDiag] = useState(null);
    const [busy, setBusy] = useState("");
    useEffect(() => {
      (async () => { try { setBench(await api("/system/benchmark")); } catch (e) {} })();
    }, []);
    const runBench = async (quick) => {
      setBusy("bench");
      try { setBench(await api("/system/benchmark", { method: "POST", body: { quick }, timeout: 180000 })); toast.ok("基准测试完成"); }
      catch (err) { toast.err(err.message); } finally { setBusy(""); }
    };
    const runDiag = async () => {
      setBusy("diag");
      try { setDiag(await api("/system/diagnostics", { timeout: 60000 })); toast.ok("诊断完成"); }
      catch (err) { toast.err(err.message); } finally { setBusy(""); }
    };
    const exportDiag = async () => {
      try { const d = await api("/system/diagnostics/export"); toast.ok(`诊断包已导出：${d.path}`); }
      catch (err) { toast.err(err.message); }
    };
    const defrag = async () => {
      try { const d = await api("/system/vram/defragment", { method: "POST" }); toast.ok(`碎片整理完成，合并 ${d.merged_blocks} 块`); }
      catch (err) { toast.err(err.message); }
    };
    return (
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "var(--space-5)" }}>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Timer" size={16} /> 硬件基准测试</h3>
          <div className="flex gap-2 mb-3">
            <button className="btn btn-primary" onClick={() => runBench(true)} disabled={!!busy}>{busy === "bench" ? <Spinner size={14} /> : <Icon name="Play" size={14} />} 快速测试</button>
            <button className="btn btn-secondary" onClick={() => runBench(false)} disabled={!!busy}>完整测试</button>
          </div>
          {bench && (
            <pre className="text-secondary" style={{ fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto", background: "var(--color-surface-muted)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)" }}>
              {JSON.stringify(bench, null, 2)}
            </pre>
          )}
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Stethoscope" size={16} /> 系统诊断</h3>
          <div className="flex gap-2 mb-3 flex-wrap">
            <button className="btn btn-primary" onClick={runDiag} disabled={!!busy}>{busy === "diag" ? <Spinner size={14} /> : <Icon name="Activity" size={14} />} 运行诊断</button>
            <button className="btn btn-secondary" onClick={exportDiag}><Icon name="FileDown" size={14} /> 导出诊断包</button>
            <button className="btn btn-ghost" onClick={defrag}><Icon name="Puzzle" size={14} /> 显存碎片整理</button>
          </div>
          {diag && (
            <pre className="text-secondary" style={{ fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto", background: "var(--color-surface-muted)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)" }}>
              {JSON.stringify(diag, null, 2)}
            </pre>
          )}
        </div>
      </div>
    );
  }

  function SettingsSandbox() {
    const [sbStatus, setSbStatus] = useState(null);
    const [code, setCode] = useState("print('hello sandbox')");
    const [result, setResult] = useState(null);
    const [plugins, setPlugins] = useState([]);
    const [busy, setBusy] = useState(false);
    const refresh = useCallback(async () => {
      try { setSbStatus(await api("/system/sandbox/status")); } catch (e) {}
      try { setPlugins((await api("/system/plugins")).items || []); } catch (e) {}
    }, []);
    useEffect(() => { refresh(); }, [refresh]);

    const runCode = async () => {
      if (!code.trim()) return;
      setBusy(true);
      try { setResult(await api("/system/sandbox/execute", { method: "POST", body: { code, timeout: 30 }, timeout: 40000 })); }
      catch (err) { toast.err(err.message); } finally { setBusy(false); }
    };
    const runPlugin = async (id) => {
      try { const d = await api(`/system/plugins/${id}/run`, { method: "POST", body: {} }); toast.ok(d.message || "插件已执行"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    const removePlugin = async (id) => {
      try { await api(`/system/plugins/${id}`, { method: "DELETE" }); toast.ok("插件已卸载"); refresh(); }
      catch (err) { toast.err(err.message); }
    };

    return (
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(380px, 1fr))", gap: "var(--space-5)" }}>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="TerminalSquare" size={16} /> 代码沙盒</h3>
          {sbStatus && (
            <div className="mb-3">
              <span className={"tag " + (sbStatus.mode === "restricted" ? "tag-success" : "tag-info")}>
                {sbStatus.mode === "restricted" ? "AST 级沙盒（RestrictedPython）" : "基础子进程模式"}
              </span>
            </div>
          )}
          <textarea className="textarea text-mono" rows={6} value={code} onChange={(e) => setCode(e.target.value)}
            placeholder="输入 Python 代码…" aria-label="沙盒代码" style={{ fontSize: "var(--text-sm)" }} />
          <div className="flex mt-3" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn-primary" onClick={runCode} disabled={busy}>{busy ? <Spinner size={14} /> : <Icon name="Play" size={14} />} 执行</button>
          </div>
          {result && (
            <pre className="text-secondary mt-3" style={{ fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto", background: "var(--color-surface-muted)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)" }}>
              {(result.stdout || "") + (result.stderr ? "\n[stderr]\n" + result.stderr : "") + (result.error ? "\n[error] " + result.error : "")}
            </pre>
          )}
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Puzzle" size={16} /> 插件管理</h3>
          <div className="flex flex-col gap-2">
            {plugins.map((p) => (
              <div key={p.id || p.name} className="flex items-center gap-3 card" style={{ padding: "var(--space-3)", background: "var(--color-surface-muted)" }}>
                <Icon name="Blocks" size={16} />
                <div className="flex-1" style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: "var(--font-medium)", fontSize: "var(--text-sm)" }}>{p.name || p.id}</div>
                  <div className="text-tertiary ellipsis" style={{ fontSize: "var(--text-xs)" }}>{p.version || ""} {p.description || ""}</div>
                </div>
                <button className="btn btn-icon" title="运行插件" onClick={() => runPlugin(p.id || p.name)}><Icon name="Play" size={14} /></button>
                <button className="btn btn-icon" title="卸载插件" onClick={() => removePlugin(p.id || p.name)}><Icon name="Trash2" size={14} /></button>
              </div>
            ))}
            {plugins.length === 0 && <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>未安装插件。将插件包放入 plugins/ 目录即可被发现。</div>}
          </div>
        </div>
      </div>
    );
  }

  function SettingsPrefs() {
    const [profile, setProfile] = useState(null);
    const [defaults, setDefaults] = useState(null);
    const refresh = useCallback(async () => {
      try { setProfile(await api("/system/preferences")); } catch (e) {}
      try { setDefaults(await api("/system/preferences/defaults")); } catch (e) {}
    }, []);
    useEffect(() => { refresh(); }, [refresh]);
    const reset = async () => {
      try { await api("/system/preferences/reset", { method: "POST" }); toast.ok("偏好画像已重置"); refresh(); }
      catch (err) { toast.err(err.message); }
    };
    return (
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "var(--space-5)" }}>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="SlidersHorizontal" size={16} /> 偏好画像</h3>
          <p className="text-secondary mb-3" style={{ fontSize: "var(--text-sm)", lineHeight: "var(--leading-relaxed)" }}>
            系统会根据采纳/弃用等行为持续学习你的风格偏好。
          </p>
          {profile && (
            <pre className="text-secondary" style={{ fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", maxHeight: 240, overflow: "auto", background: "var(--color-surface-muted)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)" }}>
              {JSON.stringify(profile, null, 2)}
            </pre>
          )}
          <button className="btn btn-ghost mt-3" onClick={reset}><Icon name="RotateCcw" size={14} /> 重置画像</button>
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <h3 className="section-title"><Icon name="Sparkles" size={16} /> 个性化默认参数</h3>
          {defaults ? (
            <pre className="text-secondary" style={{ fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", maxHeight: 240, overflow: "auto", background: "var(--color-surface-muted)", padding: "var(--space-3)", borderRadius: "var(--radius-sm)" }}>
              {JSON.stringify(defaults, null, 2)}
            </pre>
          ) : <div className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无个性化数据，使用若干次生成后生效。</div>}
        </div>
      </div>
    );
  }

  /* ==================== 资产库（文档 2.1.5：类型筛选/标签/核心8+弹性2 规划） ==================== */
  const ASSET_TYPE_LABEL = { character: "角色", scene: "场景", prop: "道具", costume: "服装" };

  function AssetsPage({ nav }) {
    const [type, setType] = useState("");
    const [keyword, setKeyword] = useState("");
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [editing, setEditing] = useState(null);      // null | {} 新建 | {…asset} 编辑
    const [planOpen, setPlanOpen] = useState(false);
    const [planText, setPlanText] = useState('[\n  {"name": "主角", "type": "character"},\n  {"name": "街道", "type": "scene"}\n]');
    const [planResult, setPlanResult] = useState(null);

    const load = useCallback(async () => {
      setLoading(true);
      try {
        const qs = [];
        if (type) qs.push(`type=${type}`);
        if (keyword.trim()) qs.push(`keyword=${encodeURIComponent(keyword.trim())}`);
        setItems((await api("/assets" + (qs.length ? "?" + qs.join("&") : ""))).items || []);
      } catch (err) { toast.err(err.message); }
      finally { setLoading(false); }
    }, [type, keyword]);
    useEffect(() => { load(); }, [type]);
    useEffect(() => Bus.on("assets-updated", load), [load]);

    const remove = async (a) => {
      try { await api(`/assets/${a.id}`, { method: "DELETE" }); toast.ok("资产已删除"); load(); }
      catch (err) { toast.err(err.message); }
    };

    const runPlan = async () => {
      let candidates;
      try { candidates = JSON.parse(planText); }
      catch (e) { toast.warn("候选 JSON 格式错误"); return; }
      try {
        setPlanResult(await api("/assets/plan", { method: "POST", body: { candidates } }));
      } catch (err) { toast.err(err.message); }
    };

    const LEVEL_TAG = { ok: "tag-success", warn: "tag-warning", simplify: "tag-warning", merge: "tag-error" };
    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="flex items-center justify-between mb-6 flex-wrap gap-3">
          <div>
            <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>资产库</h1>
            <p className="text-secondary" style={{ margin: "4px 0 0" }}>核心 8 + 弹性 2 资产策略 · 共 {items.length} 项</p>
          </div>
          <div className="flex gap-2">
            <button className="btn btn-secondary" onClick={() => { setPlanOpen(true); setPlanResult(null); }}>
              <Icon name="ClipboardList" size={15} /> 资产规划
            </button>
            <button className="btn btn-primary" onClick={() => setEditing({})}>
              <Icon name="Plus" size={15} /> 新建资产
            </button>
          </div>
        </header>

        {/* 筛选栏 */}
        <div className="flex items-center gap-3 mb-5 flex-wrap">
          <div className="seg" role="tablist" aria-label="资产类型">
            {[["", "全部"], ...Object.entries(ASSET_TYPE_LABEL)].map(([k, lb]) => (
              <button key={k} role="tab" aria-selected={type === k}
                className={"seg-item" + (type === k ? " active" : "")} onClick={() => setType(k)}>{lb}</button>
            ))}
          </div>
          <input className="input" style={{ width: 220 }} placeholder="搜索资产名…" value={keyword}
            onChange={(e) => setKeyword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} aria-label="搜索资产" />
          <button className="btn btn-secondary" onClick={load}><Icon name="Search" size={15} /></button>
        </div>

        {/* 资产网格 */}
        {loading ? <div className="flex items-center justify-center" style={{ padding: "var(--space-16)" }}><Spinner /></div>
          : items.length === 0 ? <EmptyState art="🗃️" title="暂无资产" hint="点击右上角「新建资产」开始" /> : (
            <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: "var(--space-4)" }}>
              {items.map((a) => (
                <div key={a.id} className="card card-hover" style={{ overflow: "hidden", padding: 0 }}>
                  <div style={{ aspectRatio: "1/1", background: "var(--color-primary-grad-from)", position: "relative" }}>
                    {a.image_path
                      ? <img src={fileUrl(a.image_path)} alt={a.name} loading="lazy"
                          style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                      : <div className="flex items-center justify-center" style={{ height: "100%", color: "var(--color-text-tertiary)" }}>
                          <Icon name={{ character: "User", scene: "Mountain", prop: "Sword", costume: "Shirt" }[a.type] || "Package"} size={36} />
                        </div>}
                    <span className="tag" style={{ position: "absolute", top: 8, left: 8 }}>{ASSET_TYPE_LABEL[a.type] || a.type}</span>
                  </div>
                  <div style={{ padding: "var(--space-3)" }}>
                    <div className="flex items-center justify-between">
                      <div className="ellipsis" style={{ fontWeight: "var(--font-medium)" }} title={a.name}>{a.name}</div>
                      {a.usage_count > 0 && <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>用 {a.usage_count}次</span>}
                    </div>
                    {a.style_tag && <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>风格：{a.style_tag}</div>}
                    <div className="flex gap-1 flex-wrap mt-2" style={{ minHeight: 20 }}>
                      {(a.tags || []).slice(0, 3).map((t) => <span key={t} className="tag tag-info">{t}</span>)}
                    </div>
                    <div className="flex gap-1 mt-2" style={{ justifyContent: "flex-end" }}>
                      <button className="btn btn-icon" title="编辑" onClick={() => setEditing(a)}><Icon name="Pencil" size={14} /></button>
                      <button className="btn btn-icon" title="删除" onClick={() => remove(a)}><Icon name="Trash2" size={14} /></button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

        {/* 新建/编辑弹窗 */}
        <AssetEditor asset={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />

        {/* 资产规划弹窗（核心8+弹性2，三级超限处理） */}
        <Modal open={planOpen} onClose={() => setPlanOpen(false)} title="资产规划 · 核心8+弹性2" width={640}>
          <p className="text-secondary" style={{ fontSize: "var(--text-sm)", lineHeight: "var(--leading-relaxed)" }}>
            粘贴候选资产 JSON 数组。11-13 项警告，14-15 项建议简化风格，≥16 项强制合并。
          </p>
          <textarea className="textarea text-mono" rows={6} value={planText} onChange={(e) => setPlanText(e.target.value)}
            aria-label="候选资产 JSON" style={{ fontSize: "var(--text-xs)" }} />
          <div className="flex mt-3" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn-primary" onClick={runPlan}><Icon name="Play" size={14} /> 运行规划</button>
          </div>
          {planResult && (
            <div className="mt-4">
              <div className="flex items-center gap-2 mb-3">
                <span className={"tag " + (LEVEL_TAG[planResult.level] || "")}>{planResult.level}</span>
                <span className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>{planResult.action}（共 {planResult.total} 项）</span>
              </div>
              <div className="flex gap-4 flex-wrap" style={{ fontSize: "var(--text-sm)" }}>
                <span className="text-secondary">核心 {(planResult.core || []).length}/{planResult.core_limit}</span>
                <span className="text-secondary">弹性 {(planResult.flex || []).length}/{planResult.flex_limit}</span>
                <span className="text-secondary">溢出 {(planResult.overflow || []).length}</span>
              </div>
              {planResult.merged_into && (
                <div className="mt-3 flex flex-col gap-1">
                  {planResult.merged_into.map((m, i) => (
                    <div key={i} className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                      「{m.overflow}」已并入 {ASSET_TYPE_LABEL[m.merged_into_type] || m.merged_into_type} 类资产
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </Modal>
      </div>
    );
  }

  function AssetEditor({ asset, onClose, onSaved }) {
    const isNew = asset && !asset.id;
    const [form, setForm] = useState({ name: "", type: "character", style_tag: "", description: "", image_path: "" });
    const [tags, setTags] = useState([]);
    const [newTag, setNewTag] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
      if (!asset) return;
      setForm({
        name: asset.name || "", type: asset.type || "character",
        style_tag: asset.style_tag || "", description: asset.description || "",
        image_path: asset.image_path || "",
      });
      setTags([]);
      if (asset.id) {
        (async () => {
          try { setTags(((await api(`/assets/${asset.id}`)).tags) || []); } catch (e) {}
        })();
      }
    }, [asset]);

    if (!asset) return null;

    const uploadImage = async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try {
        const d = await api("/image/upload", { method: "POST", formData: fd, timeout: 60000 });
        setForm((s) => ({ ...s, image_path: d.path })); toast.ok("图片已上传");
      } catch (err) { toast.err(err.message); }
    };

    const save = async () => {
      if (!form.name.trim()) { toast.warn("资产名不能为空"); return; }
      setSaving(true);
      try {
        if (isNew) await api("/assets", { method: "POST", body: form });
        else await api(`/assets/${asset.id}`, { method: "PUT", body: {
          name: form.name, style_tag: form.style_tag,
          description: form.description, image_path: form.image_path } });
        toast.ok(isNew ? "资产已创建" : "资产已更新");
        Bus.emit("assets-updated", {});
        onSaved();
      } catch (err) { toast.err(err.message); }
      finally { setSaving(false); }
    };

    const addTag = async () => {
      const t = newTag.trim();
      if (!t || !asset.id) return;
      try {
        await api(`/assets/${asset.id}/tags`, { method: "POST", body: { tag: t, source: "user" } });
        setTags(((await api(`/assets/${asset.id}`)).tags) || []);
        setNewTag("");
      } catch (err) { toast.err(err.message); }
    };
    const removeTag = async (tagId) => {
      try {
        await api(`/assets/${asset.id}/tags/${tagId}`, { method: "DELETE" });
        setTags((ts) => ts.filter((x) => x.id !== tagId));
      } catch (err) { toast.err(err.message); }
    };

    return (
      <Modal open={!!asset} onClose={onClose} title={isNew ? "新建资产" : `编辑：${asset.name}`} width={560}
        footer={<>
          <button className="btn btn-secondary" onClick={onClose}>取消</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? <Spinner size={14} /> : <Icon name="Check" size={15} />} 保存</button>
        </>}>
        <div className="flex flex-col gap-3">
          <div className="flex gap-3">
            <label className="flex-1 flex flex-col gap-1">
              <span className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>名称</span>
              <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="资产名称" />
            </label>
            <label className="flex flex-col gap-1" style={{ width: 130 }}>
              <span className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>类型</span>
              {isNew ? (
                <select className="input" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })} aria-label="资产类型">
                  {Object.entries(ASSET_TYPE_LABEL).map(([k, lb]) => <option key={k} value={k}>{lb}</option>)}
                </select>
              ) : <input className="input" value={ASSET_TYPE_LABEL[form.type] || form.type} disabled />}
            </label>
          </div>
          <label className="flex flex-col gap-1">
            <span className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>风格标签（如 赛博朋克 / 水墨）</span>
            <input className="input" value={form.style_tag} onChange={(e) => setForm({ ...form, style_tag: e.target.value })} aria-label="风格标签" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>描述</span>
            <textarea className="textarea" rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} aria-label="资产描述" />
          </label>
          <div className="flex items-center gap-3">
            <label className="btn btn-ghost" style={{ cursor: "pointer" }}>
              <Icon name="Upload" size={15} /> {form.image_path ? "已选图片 ✓" : "上传封面图"}
              <input type="file" accept="image/*" style={{ display: "none" }} onChange={uploadImage} />
            </label>
            {form.image_path && <img src={fileUrl(form.image_path)} alt="封面预览" style={{ width: 56, height: 56, objectFit: "cover", borderRadius: "var(--radius-sm)" }} />}
          </div>
          {!isNew && (
            <div>
              <span className="text-secondary" style={{ fontSize: "var(--text-xs)" }}>标签</span>
              <div className="flex gap-1 flex-wrap mt-2">
                {tags.map((t) => (
                  <span key={t.id} className="tag tag-info">
                    {t.tag}
                    <button onClick={() => removeTag(t.id)} aria-label={`删除标签 ${t.tag}`}
                      style={{ border: "none", background: "none", cursor: "pointer", marginLeft: 4, color: "inherit", padding: 0 }}>×</button>
                  </span>
                ))}
              </div>
              <div className="flex gap-2 mt-2">
                <input className="input flex-1" placeholder="新标签…" value={newTag} onChange={(e) => setNewTag(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addTag()} aria-label="新标签" />
                <button className="btn btn-secondary" onClick={addTag}><Icon name="Plus" size={14} /></button>
              </div>
            </div>
          )}
        </div>
      </Modal>
    );
  }

  /* ==================== 应用外壳 ==================== */
  function AppShell() {
    const { route, nav } = useHashRoute();
    const [wsOk, setWsOk] = useState(false);
    const [sysStatus, setSysStatus] = useState(null);

    useEffect(() => {
      WS.connect();
      const off1 = Bus.on("ws-state", (s) => setWsOk(!!s.connected));
      const off2 = Bus.on("system-status-update", (s) => setSysStatus(s));
      return () => { off1(); off2(); WS.close(); };
    }, []);

    const pages = {
      home: <HomePage nav={nav} />,
      manga: window.M6 ? <window.M6.Workbench route={route} nav={nav} /> : <EmptyState title="漫剧工作台加载中" />,
      assets: <AssetsPage nav={nav} />,
      chat: <ChatPage />,
      image: <ImagePage />,
      knowledge: <KnowledgePage />,
      models: <ModelsPage />,
      settings: <SettingsPage />,
    };

    return (
      <div className="app-shell">
        <Sidebar route={route} nav={nav} wsOk={wsOk} sysStatus={sysStatus} />
        <main className="main-content">
          {(pages[route.name] || pages.home)}
        </main>
        <ToastHost />
      </div>
    );
  }

  window.Omni = { api, apiSSE, Bus, WS, toast, fileUrl, Icon, Spinner, Progress, Modal, EmptyState, ApiError };
  window.AppShell = AppShell;
})();
