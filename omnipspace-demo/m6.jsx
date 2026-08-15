/* ============================================================
 * OmniSpace AI · M6 漫剧工作台（文档 2.1.5）
 * 11 列分镜表 · 撤销/重做 · 资产绑定 · 音色抽屉 · 3D 导演台 · 四工序流水线
 * 依赖 window.Omni（app.jsx 提供：api/Bus/toast/fileUrl/Icon/Spinner/Progress/Modal/EmptyState）
 * ============================================================ */
(function () {
  const { useState, useEffect, useRef, useCallback, useMemo } = React;
  const O = window.Omni || {};
  const api = O.api, Bus = O.Bus, toast = O.toast, fileUrl = O.fileUrl;
  const Icon = O.Icon, Spinner = O.Spinner, Progress = O.Progress,
        Modal = O.Modal, EmptyState = O.EmptyState;

  /* ---------------- 常量 ---------------- */
  const STAGE_LABEL = { text2img: "文生图", img2video: "图生视频", audio: "配音", compose: "合成" };
  const STAGE_ORDER = ["text2img", "img2video", "audio", "compose"];
  const SHOT_STATUS = { pending: "待制作", generating: "制作中", done: "已完成", failed: "失败" };

  // 音色/情感标签数据源：/api/v1/voices/list（文档 2.1 §4.1，21 预置音色 × 3 默认情感标签）

  /* ---------------- 分镜字段解析（JSON 字符串 → 对象） ---------------- */
  function parseShot(shot) {
    if (!shot) return shot;
    const s = { ...shot };
    for (const k of ["character_ids", "prop_ids", "voice_config"]) {
      if (typeof s[k] === "string") {
        try { s[k] = JSON.parse(s[k]); } catch (e) { s[k] = k === "voice_config" ? {} : []; }
      }
    }
    return s;
  }

  /* ==================== 工作台外壳 ==================== */
  function Workbench({ route, nav }) {
    const [projects, setProjects] = useState([]);
    const [project, setProject] = useState(null);
    const [shots, setShots] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [drawer, setDrawer] = useState("");           // "" | voice | director | pipeline | spatial
    const [hist, setHist] = useState({ undo_steps: 0, redo_steps: 0 });
    const [pipeline, setPipeline] = useState(null);
    const [loading, setLoading] = useState(false);

    const loadProjects = useCallback(async () => {
      try { setProjects((await api("/projects")).items || []); } catch (e) {}
    }, []);

    const loadShots = useCallback(async (pid) => {
      try {
        const d = await api(`/storyboard/${pid}`);
        setShots((d.items || []).map(parseShot));
      } catch (err) { toast.err(err.message); }
    }, []);

    const loadHist = useCallback(async (pid) => {
      try { setHist(await api(`/storyboard/${pid}/history`)); } catch (e) {}
    }, []);

    const loadPipeline = useCallback(async (pid) => {
      try { setPipeline(await api(`/projects/${pid}/pipeline`)); } catch (e) {}
    }, []);

    const openProject = useCallback(async (p) => {
      setProject(p); setSelectedId(null); setDrawer("");
      await Promise.all([loadShots(p.id), loadHist(p.id), loadPipeline(p.id)]);
    }, [loadShots, loadHist, loadPipeline]);

    useEffect(() => { loadProjects(); }, [loadProjects]);

    // 路由参数支持 #/manga/{projectId}
    useEffect(() => {
      if (route.param && !project) {
        (async () => {
          try { setProject(await api(`/projects/${route.param}`)); } catch (e) {}
        })();
      }
    }, [route.param]);
    useEffect(() => {
      if (project && !shots.length) openProject(project);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [project && project.id]);

    // WebSocket 任务完成 → 刷新分镜状态
    useEffect(() => Bus.on("task-progress-update", ({ type }) => {
      if (project && (type === "task_completed" || type === "task_failed")) {
        loadShots(project.id); loadPipeline(project.id);
      }
    }), [project, loadShots, loadPipeline]);

    // Ctrl+Z / Ctrl+Y
    useEffect(() => {
      const onKey = (e) => {
        if (!project) return;
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey) {
          e.preventDefault(); doUndo();
        } else if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) {
          e.preventDefault(); doRedo();
        }
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    });

    const doUndo = async () => {
      if (!project) return;
      try {
        const d = await api(`/storyboard/${project.id}/undo`, { method: "POST" });
        setShots((d.items || []).map(parseShot)); loadHist(project.id);
        toast.info("已撤销");
      } catch (err) { toast.warn(err.message); }
    };
    const doRedo = async () => {
      if (!project) return;
      try {
        const d = await api(`/storyboard/${project.id}/redo`, { method: "POST" });
        setShots((d.items || []).map(parseShot)); loadHist(project.id);
        toast.info("已重做");
      } catch (err) { toast.warn(err.message); }
    };

    const refreshShots = useCallback(() => {
      if (project) { loadShots(project.id); loadHist(project.id); }
    }, [project, loadShots, loadHist]);

    /* ---------- 渲染 ---------- */
    if (!project) {
      return <ProjectPicker projects={projects} onOpen={openProject} onCreated={(p) => { loadProjects(); openProject(p); }} />;
    }
    if (project.status === "SCRIPT_INPUT" && shots.length === 0) {
      return <ScriptImport project={project} onBack={() => setProject(null)}
        onParsed={async () => { await openProject({ ...project, status: "STORYBOARD" }); }} />;
    }

    const selected = shots.find((s) => s.id === selectedId) || null;

    return (
      <div className="flex flex-col" style={{ height: "100%", overflow: "hidden" }}>
        {/* 工具栏 */}
        <div className="flex items-center gap-2 flex-wrap"
          style={{ padding: "var(--space-3) var(--space-4)", borderBottom: "1px solid var(--color-border)", background: "var(--color-surface)" }}>
          <button className="btn btn-icon" title="返回项目列表" onClick={() => setProject(null)}><Icon name="ChevronLeft" size={18} /></button>
          <div style={{ minWidth: 0 }}>
            <div className="ellipsis" style={{ fontWeight: "var(--font-semibold)" }}>{project.name}</div>
            <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
              {shots.length}/50 镜 {pipeline ? `· ${pipeline.current}` : ""}
            </div>
          </div>
          <div style={{ marginLeft: "auto" }} className="flex items-center gap-2">
            <button className="btn btn-icon" title="撤销 (Ctrl+Z)" disabled={!hist.undo_steps} onClick={doUndo}><Icon name="Undo2" size={16} /></button>
            <button className="btn btn-icon" title="重做 (Ctrl+Y)" disabled={!hist.redo_steps} onClick={doRedo}><Icon name="Redo2" size={16} /></button>
            <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{hist.undo_steps} 步可撤销</span>
            <button className="btn btn-secondary" onClick={async () => {
              try {
                const d = await api(`/storyboard/${project.id}/shots`, { method: "POST", body: { original_text: "新分镜", shot_description: "" } });
                refreshShots(); setSelectedId(d.id);
              } catch (err) { toast.err(err.message); }
            }}><Icon name="Plus" size={15} /> 插入分镜</button>
            <button className="btn btn-secondary" onClick={() => setDrawer(drawer === "spatial" ? "" : "spatial")}>
              <Icon name="ScanSearch" size={15} /> 空间推理
            </button>
            <button className="btn btn-ghost" onClick={() => nav("assets")}><Icon name="Package" size={15} /> 资产库</button>
          </div>
        </div>

        {/* 项目流水线进度 */}
        {pipeline && (
          <div className="flex items-center gap-1" style={{ padding: "var(--space-2) var(--space-4)", borderBottom: "1px solid var(--color-border)", background: "var(--color-surface-muted)" }}>
            {(pipeline.states || []).map((st, i) => (
              <React.Fragment key={st}>
                <span className={"tag " + (i < pipeline.current_index ? "tag-success" : i === pipeline.current_index ? "tag-warning" : "")}>{st}</span>
                {i < pipeline.states.length - 1 && <Icon name="ChevronRight" size={12} />}
              </React.Fragment>
            ))}
          </div>
        )}

        {/* 主区：分镜表 + 右侧抽屉 */}
        <div className="flex flex-1" style={{ minHeight: 0 }}>
          <div className="flex-1" style={{ minWidth: 0, overflow: "auto" }}>
            <StoryboardTable shots={shots} selectedId={selectedId}
              onSelect={(s) => { setSelectedId(s.id); }}
              onChanged={refreshShots}
              onOpenDrawer={(kind, shot) => { setSelectedId(shot.id); setDrawer(kind); }} />
          </div>
          {drawer && selected && (
            <aside style={{
              width: drawer === "director" ? 420 : 320, flexShrink: 0,
              borderLeft: "1px solid var(--color-border)", background: "var(--color-surface)",
              overflowY: "auto", padding: "var(--space-4)",
            }}>
              <div className="flex items-center justify-between mb-3">
                <h3 style={{ margin: 0, fontSize: "var(--text-lg)", fontWeight: "var(--font-semibold)" }}>
                  {{ voice: "音色配置", director: "3D 导演台", pipeline: "四工序流水线", spatial: "空间推理" }[drawer]}
                  <span className="text-tertiary" style={{ fontSize: "var(--text-sm)", fontWeight: "var(--font-normal)" }}> · 镜 {selected.sort_index}</span>
                </h3>
                <button className="btn btn-icon" onClick={() => setDrawer("")} aria-label="关闭抽屉"><Icon name="X" size={16} /></button>
              </div>
              {drawer === "voice" && <VoiceDrawer shot={selected} onSaved={refreshShots} />}
              {drawer === "director" && <DirectorPanel shot={selected} />}
              {drawer === "pipeline" && <PipelinePanel shot={selected} onChanged={refreshShots} />}
              {drawer === "spatial" && <SpatialPanel />}
            </aside>
          )}
          {drawer === "spatial" && !selected && (
            <aside style={{ width: 320, flexShrink: 0, borderLeft: "1px solid var(--color-border)", background: "var(--color-surface)", overflowY: "auto", padding: "var(--space-4)" }}>
              <div className="flex items-center justify-between mb-3">
                <h3 style={{ margin: 0, fontSize: "var(--text-lg)", fontWeight: "var(--font-semibold)" }}>空间推理</h3>
                <button className="btn btn-icon" onClick={() => setDrawer("")} aria-label="关闭抽屉"><Icon name="X" size={16} /></button>
              </div>
              <SpatialPanel />
            </aside>
          )}
        </div>
      </div>
    );
  }

  /* ==================== 项目选择 ==================== */
  function ProjectPicker({ projects, onOpen, onCreated }) {
    const [name, setName] = useState("");
    const [script, setScript] = useState("");
    const [creating, setCreating] = useState(false);

    const create = async () => {
      if (!name.trim()) { toast.warn("请输入项目名称"); return; }
      setCreating(true);
      try {
        const p = await api("/projects", { method: "POST", body: { name: name.trim(), script_text: script } });
        toast.ok("项目已创建");
        onCreated(p);
      } catch (err) { toast.err(err.message); }
      finally { setCreating(false); }
    };

    const STATE_LABEL = {
      SCRIPT_INPUT: "剧本录入", STORYBOARD: "分镜表", ASSET_PLAN: "资产规划",
      ASSET_GEN: "资产生成", DIRECTOR: "导演台", VIDEO: "视频制作",
      CONFIRM: "待确认", COMPLETE: "已完成",
    };
    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <header className="mb-6">
          <h1 style={{ margin: 0, fontSize: "var(--text-3xl)", fontWeight: "var(--font-bold)" }}>漫剧创作</h1>
          <p className="text-secondary" style={{ margin: "4px 0 0" }}>选择项目进入工作台，或创建新项目</p>
        </header>
        <div className="grid" style={{ gridTemplateColumns: "minmax(320px, 5fr) minmax(360px, 7fr)", gap: "var(--space-5)", alignItems: "start" }}>
          <div className="card" style={{ padding: "var(--space-5)" }}>
            <h3 className="section-title"><Icon name="FolderPlus" size={16} /> 新建项目</h3>
            <div className="flex flex-col gap-3">
              <input className="input" placeholder="项目名称（如：深夜食堂 第1集）" value={name}
                onChange={(e) => setName(e.target.value)} aria-label="项目名称" />
              <textarea className="textarea" rows={8} value={script} onChange={(e) => setScript(e.target.value)}
                placeholder="粘贴剧本全文（可选，稍后也可录入）…" aria-label="剧本" />
              {script.length > 3000 && (
                <div className="tag tag-warning">剧本 {script.length} 字，超过 3000 字建议拆分为多集</div>)}
              <button className="btn btn-primary" onClick={create} disabled={creating}>
                {creating ? <Spinner size={15} /> : <Icon name="Clapperboard" size={15} />} 创建并进入
              </button>
            </div>
          </div>
          <div className="flex flex-col gap-3">
            {projects.length === 0 && <EmptyState art="🎬" title="暂无项目" hint="从左侧创建第一个漫剧项目" />}
            {projects.map((p) => (
              <button key={p.id} className="card card-hover flex items-center gap-3"
                style={{ padding: "var(--space-4)", border: "none", cursor: "pointer", textAlign: "left", fontFamily: "var(--font-sans)", width: "100%" }}
                onClick={() => onOpen(p)}>
                <div style={{
                  width: 44, height: 44, borderRadius: "var(--radius-md)", flexShrink: 0,
                  background: "linear-gradient(135deg, var(--color-primary), var(--color-primary-deep))",
                  display: "flex", alignItems: "center", justifyContent: "center", color: "#fff",
                }}><Icon name="Film" size={20} /></div>
                <div className="flex-1" style={{ minWidth: 0 }}>
                  <div className="ellipsis" style={{ fontWeight: "var(--font-semibold)", color: "var(--color-text)" }}>{p.name}</div>
                  <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                    {STATE_LABEL[p.status] || p.status} · {p.updated_at ? new Date(p.updated_at * 1000).toLocaleString("zh-CN") : ""}
                  </div>
                </div>
                <Icon name="ChevronRight" size={18} />
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  /* ==================== 剧本导入 ==================== */
  function ScriptImport({ project, onBack, onParsed }) {
    const [script, setScript] = useState(project.script_text || "");
    const [mode, setMode] = useState("auto");
    const [parsing, setParsing] = useState(false);

    const parse = async () => {
      if (script.trim().length < 50) { toast.warn("剧本内容过短（至少 50 字）"); return; }
      setParsing(true);
      try {
        if (script !== (project.script_text || "")) {
          await api(`/projects/${project.id}`, { method: "PUT", body: { script_text: script } });
        }
        const d = await api("/storyboard/parse", {
          method: "POST", timeout: 120000,
          body: { project_id: project.id, script_text: script, mode },
        });
        toast.ok(`解析完成：${d.count} 个分镜（引擎：${d.engine === "llm" ? "LLM" : "规则"}）`);
        if (d.split_hint) toast.warn(d.split_hint);
        onParsed();
      } catch (err) { toast.err(err.message); }
      finally { setParsing(false); }
    };

    return (
      <div className="page-scroll" style={{ padding: "var(--space-6)" }}>
        <div className="flex items-center gap-3 mb-6">
          <button className="btn btn-icon" onClick={onBack} aria-label="返回"><Icon name="ChevronLeft" size={18} /></button>
          <div>
            <h1 style={{ margin: 0, fontSize: "var(--text-2xl)", fontWeight: "var(--font-bold)" }}>剧本录入 · {project.name}</h1>
            <p className="text-secondary" style={{ margin: "4px 0 0" }}>粘贴剧本，AI 将自动切分为分镜表（上限 50 镜）</p>
          </div>
        </div>
        <div className="card" style={{ padding: "var(--space-5)" }}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>{script.length} 字</span>
            <div className="seg">
              {[["auto", "自动"], ["fast", "快速"], ["expert", "专家"]].map(([k, lb]) => (
                <button key={k} className={"seg-item" + (mode === k ? " active" : "")} onClick={() => setMode(k)}>{lb}</button>
              ))}
            </div>
          </div>
          <textarea className="textarea" rows={16} value={script} onChange={(e) => setScript(e.target.value)}
            placeholder="第一幕：深夜，便利店的灯光在雨中晕开。林晚推开门，风铃轻响……" aria-label="剧本文本" />
          {script.length > 3000 && (
            <div className="tag tag-warning mt-2">剧本 {script.length} 字，超过 3000 字建议拆分为多集以保证分镜质量</div>)}
          <div className="flex mt-4" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn-primary" onClick={parse} disabled={parsing}>
              {parsing ? <Spinner size={15} /> : <Icon name="Wand2" size={15} />} AI 切分分镜
            </button>
          </div>
        </div>
      </div>
    );
  }

  /* ==================== 11 列分镜表（行高 48px，react-window 虚拟滚动） ==================== */
  const COLS = [
    { key: "idx", label: "镜号", w: 52 },
    { key: "original", label: "原文", w: 200 },
    { key: "desc", label: "画面描述", w: 220 },
    { key: "chars", label: "角色", w: 130 },
    { key: "scene", label: "场景", w: 110 },
    { key: "props", label: "道具", w: 110 },
    { key: "voice", label: "音色", w: 110 },
    { key: "director", label: "导演台", w: 76 },
    { key: "pipeline", label: "工序", w: 120 },
    { key: "status", label: "状态", w: 76 },
    { key: "ops", label: "操作", w: 128 },
  ];
  const TABLE_W = COLS.reduce((a, c) => a + c.w, 0);

  function StoryboardTable({ shots, selectedId, onSelect, onChanged, onOpenDrawer }) {
    const [assets, setAssets] = useState([]);
    const [editCell, setEditCell] = useState(null);      // {shot, field}
    const [bindShot, setBindShot] = useState(null);
    const wrapRef = useRef(null);
    const [viewH, setViewH] = useState(520);

    const loadAssets = useCallback(async () => {
      try { setAssets((await api("/assets")).items || []); } catch (e) {}
    }, []);
    useEffect(() => { loadAssets(); }, [loadAssets]);
    useEffect(() => Bus.on("assets-updated", loadAssets), [loadAssets]);

    useEffect(() => {
      const measure = () => {
        if (wrapRef.current) setViewH(Math.max(280, wrapRef.current.clientHeight - 40));
      };
      measure();
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }, []);

    const assetName = useCallback((id) => {
      const a = assets.find((x) => x.id === id);
      return a ? a.name : (id || "").slice(0, 8);
    }, [assets]);

    const saveField = async (shot, field, value) => {
      if (value === shot[field]) { setEditCell(null); return; }
      try {
        await api(`/storyboard/shots/${shot.id}`, { method: "PUT", body: { [field]: value } });
        onChanged();
      } catch (err) { toast.err(err.message); }
      setEditCell(null);
    };

    const move = async (shot, dir) => {
      const to = shot.sort_index + dir;
      if (to < 1 || to > shots.length) return;
      try { await api(`/storyboard/shots/${shot.id}/move?to_index=${to}`, { method: "POST" }); onChanged(); }
      catch (err) { toast.err(err.message); }
    };
    const insertBelow = async (shot) => {
      try {
        await api(`/storyboard/${shot.project_id}/shots`, {
          method: "POST",
          body: { original_text: "", shot_description: "", sort_index: shot.sort_index + 1 },
        });
        onChanged();
      } catch (err) { toast.err(err.message); }
    };
    const remove = async (shot) => {
      try { await api(`/storyboard/shots/${shot.id}`, { method: "DELETE" }); onChanged(); }
      catch (err) { toast.err(err.message); }
    };

    const STATUS_TAG = { pending: "", generating: "tag-warning", done: "tag-success", failed: "tag-error" };

    const renderRow = (shot, style) => {
      const vc = shot.voice_config || {};
      const selected = shot.id === selectedId;
      return (
        <div key={shot.id} role="row" aria-selected={selected}
          className="sb-row"
          style={{
            ...(style || {}), display: "grid", gridTemplateColumns: COLS.map((c) => c.w + "px").join(" "),
            minWidth: TABLE_W, height: 48, alignItems: "center",
            borderBottom: "1px solid var(--color-border)", cursor: "pointer",
            background: selected ? "var(--color-primary-soft)" : "var(--color-surface)",
            transition: "background var(--dur-hover) var(--ease-out)",
          }}
          onClick={() => onSelect(shot)}>
          {/* 镜号 */}
          <div className="text-tertiary text-mono" style={{ textAlign: "center", fontSize: "var(--text-sm)" }}>{shot.sort_index}</div>
          {/* 原文 */}
          <CellText value={shot.original_text} onEdit={() => setEditCell({ shot, field: "original_text" })} />
          {/* 画面描述 */}
          <CellText value={shot.shot_description} onEdit={() => setEditCell({ shot, field: "shot_description" })} />
          {/* 角色 */}
          <div className="sb-cell">
            {(shot.character_ids || []).slice(0, 2).map((cid) => (
              <span key={cid} className="tag tag-info ellipsis" style={{ maxWidth: 56 }}>{assetName(cid)}</span>))}
            <button className="btn btn-icon" title="绑定资产" onClick={(e) => { e.stopPropagation(); setBindShot(shot); }}>
              <Icon name="Plus" size={13} /></button>
          </div>
          {/* 场景 */}
          <div className="sb-cell">
            {shot.scene_id ? <span className="tag ellipsis" style={{ maxWidth: 72 }}>{assetName(shot.scene_id)}</span> : null}
            {!shot.scene_id && (
              <button className="btn btn-icon" title="绑定场景" onClick={(e) => { e.stopPropagation(); setBindShot(shot); }}>
                <Icon name="Plus" size={13} /></button>)}
          </div>
          {/* 道具 */}
          <div className="sb-cell">
            {(shot.prop_ids || []).slice(0, 2).map((pid) => (
              <span key={pid} className="tag ellipsis" style={{ maxWidth: 52 }}>{assetName(pid)}</span>))}
          </div>
          {/* 音色 */}
          <div className="sb-cell">
            <button className="btn btn-icon" title="音色情感配置" onClick={(e) => { e.stopPropagation(); onOpenDrawer("voice", shot); }}>
              <Icon name="Mic" size={14} /></button>
            {(() => {
              const entries = Object.values(vc.characters || {});
              if (!entries.length) return null;
              const first = entries[0];
              return (
                <span className="text-tertiary ellipsis" style={{ fontSize: "var(--text-xs)", maxWidth: 64 }}
                  title={entries.map((en) => `${en.voice_id}/${en.emotion_label || en.emotion_id}`).join("\n")}>
                  {first.emotion_label || first.voice_id}{entries.length > 1 ? ` +${entries.length - 1}` : ""}
                </span>
              );
            })()}
          </div>
          {/* 导演台 */}
          <div className="sb-cell" style={{ justifyContent: "center" }}>
            <button className="btn btn-icon" title="3D 导演台" onClick={(e) => { e.stopPropagation(); onOpenDrawer("director", shot); }}>
              <Icon name="Video" size={15} /></button>
          </div>
          {/* 工序 */}
          <div className="sb-cell">
            <PipelineDots shotId={shot.id} onOpen={() => onOpenDrawer("pipeline", shot)} />
          </div>
          {/* 状态 */}
          <div className="sb-cell" style={{ justifyContent: "center" }}>
            <span className={"tag " + (STATUS_TAG[shot.status] || "")}>{SHOT_STATUS[shot.status] || shot.status}</span>
          </div>
          {/* 操作 */}
          <div className="sb-cell" style={{ gap: 0 }}>
            <button className="btn btn-icon" title="上移" disabled={shot.sort_index <= 1}
              onClick={(e) => { e.stopPropagation(); move(shot, -1); }}><Icon name="ArrowUp" size={13} /></button>
            <button className="btn btn-icon" title="下移" disabled={shot.sort_index >= shots.length}
              onClick={(e) => { e.stopPropagation(); move(shot, 1); }}><Icon name="ArrowDown" size={13} /></button>
            <button className="btn btn-icon" title="下方插入"
              onClick={(e) => { e.stopPropagation(); insertBelow(shot); }}><Icon name="CornerDownRight" size={13} /></button>
            <button className="btn btn-icon" title="删除"
              onClick={(e) => { e.stopPropagation(); remove(shot); }}><Icon name="Trash2" size={13} /></button>
          </div>
        </div>
      );
    };

    const List = window.ReactWindow && window.ReactWindow.FixedSizeList;

    return (
      <div ref={wrapRef} style={{ height: "100%", padding: "var(--space-3)" }}>
        <div style={{ minWidth: TABLE_W, border: "1px solid var(--color-border)", borderRadius: "var(--radius-md)", overflow: "hidden", background: "var(--color-surface)" }}>
          {/* 表头 */}
          <div style={{
            display: "grid", gridTemplateColumns: COLS.map((c) => c.w + "px").join(" "),
            background: "var(--color-surface-muted)", borderBottom: "1px solid var(--color-border)",
            height: 40, alignItems: "center",
          }}>
            {COLS.map((c) => (
              <div key={c.key} className="text-tertiary" style={{
                fontSize: "var(--text-xs)", fontWeight: "var(--font-medium)",
                padding: "0 var(--space-2)", textAlign: c.key === "idx" ? "center" : "left",
              }}>{c.label}</div>
            ))}
          </div>
          {/* 表体（虚拟滚动） */}
          {shots.length === 0 ? (
            <EmptyState art="🎞️" title="分镜表为空" hint="点击工具栏「插入分镜」或重新解析剧本" />
          ) : List ? (
            <List height={Math.min(viewH, shots.length * 48)} itemCount={shots.length} itemSize={48} itemData={shots}
              style={{ overflowX: "hidden" }}>
              {({ index, style, data }) => renderRow(data[index], style)}
            </List>
          ) : (
            <div style={{ maxHeight: viewH, overflowY: "auto" }}>
              {shots.map((s) => renderRow(s, null))}
            </div>
          )}
        </div>

        {/* 单元格编辑器 */}
        <CellEditor editCell={editCell} onSave={saveField} onClose={() => setEditCell(null)} />
        {/* 资产绑定 */}
        <AssetBinder shot={bindShot} assets={assets} onClose={() => setBindShot(null)}
          onBound={() => { setBindShot(null); onChanged(); Bus.emit("director-data-updated", {}); }} />
      </div>
    );
  }

  function CellText({ value, onEdit }) {
    return (
      <div className="sb-cell" onClick={(e) => { e.stopPropagation(); onEdit(); }}
        title={value || "点击编辑"} role="button" aria-label="编辑单元格">
        <span className="ellipsis" style={{ fontSize: "var(--text-sm)", color: value ? "var(--color-text)" : "var(--color-text-tertiary)" }}>
          {value || "—"}
        </span>
      </div>
    );
  }

  /* ---------------- 单元格编辑弹窗 ---------------- */
  function CellEditor({ editCell, onSave, onClose }) {
    const [val, setVal] = useState("");
    useEffect(() => {
      if (editCell) setVal(editCell.shot[editCell.field] || "");
    }, [editCell]);
    if (!editCell) return null;
    const label = editCell.field === "original_text" ? "原文" : "画面描述";
    return (
      <Modal open onClose={onClose} title={`编辑 · 镜 ${editCell.shot.sort_index} · ${label}`} width={560}
        footer={<>
          <button className="btn btn-secondary" onClick={onClose}>取消</button>
          <button className="btn btn-primary" onClick={() => onSave(editCell.shot, editCell.field, val)}>
            <Icon name="Check" size={15} /> 保存</button>
        </>}>
        <textarea className="textarea" rows={6} value={val} onChange={(e) => setVal(e.target.value)}
          aria-label={label} autoFocus />
      </Modal>
    );
  }

  /* ---------------- 资产绑定弹窗（搜索 + 快速创建） ---------------- */
  function AssetBinder({ shot, assets, onClose, onBound }) {
    const [keyword, setKeyword] = useState("");
    const [selChars, setSelChars] = useState([]);
    const [selScene, setSelScene] = useState("");
    const [selProps, setSelProps] = useState([]);
    const [quickName, setQuickName] = useState("");
    const [quickType, setQuickType] = useState("character");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
      if (shot) {
        setSelChars([...(shot.character_ids || [])]);
        setSelScene(shot.scene_id || "");
        setSelProps([...(shot.prop_ids || [])]);
      }
    }, [shot]);
    if (!shot) return null;

    const filtered = assets.filter((a) =>
      !keyword.trim() || (a.name || "").toLowerCase().includes(keyword.trim().toLowerCase()));

    const toggle = (list, setList, id) =>
      setList(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

    const quickCreate = async () => {
      if (!quickName.trim()) return;
      try {
        const a = await api("/assets", { method: "POST", body: { name: quickName.trim(), type: quickType } });
        toast.ok(`资产「${a.name}」已创建`);
        Bus.emit("assets-updated", {});
        if (quickType === "character") setSelChars((xs) => [...xs, a.id]);
        else if (quickType === "scene") setSelScene(a.id);
        else setSelProps((xs) => [...xs, a.id]);
        setQuickName("");
      } catch (err) { toast.err(err.message); }
    };

    const bind = async () => {
      setSaving(true);
      try {
        await api(`/storyboard/shots/${shot.id}/bind-assets`, {
          method: "POST", body: { character_ids: selChars, scene_id: selScene, prop_ids: selProps },
        });
        toast.ok("资产绑定已保存");
        onBound();
      } catch (err) { toast.err(err.message); }
      finally { setSaving(false); }
    };

    const Section = ({ title, type, multi, value, onToggle }) => (
      <div className="mb-3">
        <div className="text-secondary mb-2" style={{ fontSize: "var(--text-sm)", fontWeight: "var(--font-medium)" }}>{title}</div>
        <div className="flex gap-1 flex-wrap" style={{ maxHeight: 110, overflowY: "auto" }}>
          {filtered.filter((a) => a.type === type).map((a) => {
            const active = multi ? value.includes(a.id) : value === a.id;
            return (
              <button key={a.id} className={"tag" + (active ? " tag-success" : "")}
                style={{ cursor: "pointer", border: active ? "1px solid var(--color-success)" : "1px solid transparent" }}
                onClick={() => multi ? onToggle(a.id) : onToggle(active ? "" : a.id)}>
                {a.name}
              </button>
            );
          })}
          {filtered.filter((a) => a.type === type).length === 0 &&
            <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>无匹配资产</span>}
        </div>
      </div>
    );

    return (
      <Modal open onClose={onClose} title={`资产绑定 · 镜 ${shot.sort_index}`} width={600}
        footer={<>
          <button className="btn btn-secondary" onClick={onClose}>取消</button>
          <button className="btn btn-primary" onClick={bind} disabled={saving}>
            {saving ? <Spinner size={14} /> : <Icon name="Link" size={15} />} 保存绑定</button>
        </>}>
        <input className="input mb-3" placeholder="搜索资产…" value={keyword}
          onChange={(e) => setKeyword(e.target.value)} aria-label="搜索资产" />
        <Section title="角色（可多选）" type="character" multi value={selChars}
          onToggle={(id) => toggle(selChars, setSelChars, id)} />
        <Section title="场景（单选）" type="scene" value={selScene} onToggle={setSelScene} />
        <Section title="道具（可多选）" type="prop" multi value={selProps}
          onToggle={(id) => toggle(selProps, setSelProps, id)} />
        {/* 快速创建 */}
        <div className="flex gap-2 mt-4" style={{ borderTop: "1px solid var(--color-border)", paddingTop: "var(--space-3)" }}>
          <input className="input flex-1" placeholder="快速创建资产名…" value={quickName}
            onChange={(e) => setQuickName(e.target.value)} aria-label="快速创建资产名" />
          <select className="input" style={{ width: 100 }} value={quickType} onChange={(e) => setQuickType(e.target.value)} aria-label="资产类型">
            <option value="character">角色</option><option value="scene">场景</option>
            <option value="prop">道具</option><option value="costume">服装</option>
          </select>
          <button className="btn btn-secondary" onClick={quickCreate}><Icon name="Plus" size={14} /> 创建</button>
        </div>
      </Modal>
    );
  }

  /* ---------------- 工序小圆点 ---------------- */
  function PipelineDots({ shotId, onOpen }) {
    const [prog, setProg] = useState(null);
    useEffect(() => {
      let alive = true;
      (async () => {
        try { const d = await api(`/director/pipeline/${shotId}`); if (alive) setProg(d); } catch (e) {}
      })();
      return () => { alive = false; };
    }, [shotId]);
    const done = (prog && prog.completed_stages) || [];
    const cur = prog && prog.current_stage;
    return (
      <button className="flex items-center gap-1" title="四工序流水线"
        style={{ border: "none", background: "none", cursor: "pointer", padding: 0 }}
        onClick={(e) => { e.stopPropagation(); onOpen(); }}>
        {STAGE_ORDER.map((st) => (
          <span key={st} title={STAGE_LABEL[st]} style={{
            width: 10, height: 10, borderRadius: "50%",
            background: done.includes(st) ? "var(--color-success)"
              : st === cur ? "var(--color-warning)" : "var(--color-border)",
          }} />
        ))}
      </button>
    );
  }

  /* ==================== 音色情感抽屉（文档 2.1 §4.1/4.2） ====================
   * 角色→音色绑定（一角色一音色+多情感标签）· 弹窗确认→全局同步
   * 情感标签管理（添加/编辑/删除，编辑弹窗确认→全局同步）
   * GPT-SoVITS 克隆（独立子进程，轮询状态）· 预置/克隆音色试听
   * ============================================================ */
  function VoiceDrawer({ shot, onSaved }) {
    const vc = shot.voice_config || {};
    const bound = vc.characters || {};                 // cid → {voice_id, emotion_id, ...}
    const charIds = shot.character_ids || [];
    const [voices, setVoices] = useState([]);
    const [charMap, setCharMap] = useState({});
    const [forms, setForms] = useState({});            // cid → {voice_id, emotion_ids, default_emotion_id}
    const [confirm, setConfirm] = useState(null);      // {title, lines, onOk}
    const [editEmo, setEditEmo] = useState(null);      // {voice_id, emo} 编辑中标签
    const [addFor, setAddFor] = useState("");          // 添加标签的 voice_id
    const [newEmo, setNewEmo] = useState({ label: "", emotion_intensity: 0.5, speed: 1.0, pitch: 0.0 });
    const [cloneOpen, setCloneOpen] = useState(false);
    const [cloneForm, setCloneForm] = useState({ name: "", reference_audio: "", reference_text: "" });
    const [cloneTask, setCloneTask] = useState(null);
    const [expanded, setExpanded] = useState("");      // 音色库展开管理的 voice_id
    const [playing, setPlaying] = useState("");
    const [busy, setBusy] = useState("");
    const audioRef = useRef(null);
    const pollRef = useRef(null);

    const loadVoices = useCallback(async () => {
      try { setVoices((await api("/voices/list")).items || []); } catch (err) { toast.err(err.message); }
    }, []);

    useEffect(() => {
      loadVoices();
      (async () => {
        try {
          const items = (await api("/assets/characters/list")).items || [];
          const map = {}; items.forEach((c) => { map[c.id] = c; });
          setCharMap(map);
        } catch (e) {}
      })();
      return () => clearInterval(pollRef.current);
    }, [loadVoices]);

    // 依据当前绑定/音色默认标签初始化每个角色的表单
    useEffect(() => {
      if (!voices.length) return;
      setForms((prev) => {
        const next = { ...prev };
        for (const cid of charIds) {
          if (next[cid]) continue;
          const cur = bound[cid];
          const voice = voices.find((v) => v.id === (cur && cur.voice_id)) || voices[0];
          const emos = voice.emotions || [];
          const defEmo = emos.find((e) => e.is_default) || emos[0] || {};
          next[cid] = {
            voice_id: voice.id,
            emotion_ids: cur && cur.emotion_id ? [cur.emotion_id] : emos.map((e) => e.id),
            default_emotion_id: (cur && cur.emotion_id) || defEmo.id || "",
          };
        }
        return next;
      });
    }, [voices, shot.id]);

    const voiceOf = (vid) => voices.find((v) => v.id === vid) || {};
    const setForm = (cid, patch) => setForms((f) => ({ ...f, [cid]: { ...f[cid], ...patch } }));

    /* ---------- 角色绑定（confirm 流） ---------- */
    const doBind = async (cid, confirmed) => {
      const f = forms[cid];
      if (!f || !f.voice_id) return;
      setBusy("bind:" + cid);
      try {
        const d = await api(`/characters/${cid}/bind-voice`, {
          method: "POST",
          body: { voice_id: f.voice_id, emotion_ids: f.emotion_ids,
                  default_emotion_id: f.default_emotion_id, confirm: !!confirmed },
        });
        if (d.need_confirm && !confirmed) {
          setConfirm({
            title: "角色音色变更全局同步",
            lines: [`角色「${(charMap[cid] || {}).name || cid}」的音色变更将同步到 ${d.affected.storyboards} 个关联分镜。`,
                    d.current_binding ? "该角色已有绑定，将被覆盖。" : "首次绑定。"],
            onOk: () => doBind(cid, true),
          });
          return;
        }
        toast.ok(`已绑定并全局同步（${d.synced_storyboards} 个分镜）`);
        onSaved && onSaved();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    /* ---------- 情感标签编辑（confirm 流） ---------- */
    const saveEmotion = async (confirmed) => {
      const emo = editEmo.emo;
      setBusy("emo");
      try {
        const d = await api(`/voices/emotions/${emo.id}`, {
          method: "PUT",
          body: { label: emo.label, emotion_intensity: emo.emotion_intensity,
                  speed: emo.speed, pitch: emo.pitch, confirm: !!confirmed },
        });
        if (d.need_confirm && !confirmed) {
          setConfirm({
            title: "情感参数修改全局同步",
            lines: [`标签「${emo.label}」的参数修改将全局同步：`,
                    `引用角色 ${d.affected.characters} 个 · 关联分镜 ${d.affected.storyboards} 个`],
            onOk: () => saveEmotion(true),
          });
          return;
        }
        toast.ok(`标签已更新并同步（${d.synced_storyboards} 个分镜）`);
        setEditEmo(null); loadVoices(); onSaved && onSaved();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    const addEmotion = async (vid) => {
      if (!newEmo.label.trim()) { toast.warn("标签名称不能为空"); return; }
      setBusy("add");
      try {
        await api(`/voices/${vid}/emotions`, { method: "POST", body: newEmo });
        toast.ok("情感标签已添加");
        setAddFor(""); setNewEmo({ label: "", emotion_intensity: 0.5, speed: 1.0, pitch: 0.0 });
        loadVoices();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    const delEmotion = async (emo) => {
      setConfirm({
        title: "删除情感标签",
        lines: [`删除「${emo.label}」后，引用它的角色与分镜将回退到该音色默认标签。`],
        onOk: async () => {
          try {
            const d = await api(`/voices/emotions/${emo.id}`, { method: "DELETE" });
            toast.ok(`已删除，${d.synced_storyboards} 个分镜已回退默认标签`);
            loadVoices(); onSaved && onSaved();
          } catch (err) { toast.err(err.message); }
        },
      });
    };

    /* ---------- 试听 ---------- */
    const playSample = async (vid) => {
      try {
        const d = await api(`/voices/sample/${vid}`);
        if (!d.available || !d.sample_url) { toast.warn("试听音频未就绪（克隆中或缺失）"); return; }
        if (audioRef.current) { audioRef.current.pause(); }
        const a = new Audio(d.sample_url);
        audioRef.current = a;
        setPlaying(vid);
        a.onended = () => setPlaying("");
        a.play().catch(() => { toast.warn("播放失败"); setPlaying(""); });
      } catch (err) { toast.err(err.message); }
    };

    /* ---------- 克隆 ---------- */
    const doClone = async () => {
      if (!cloneForm.name.trim()) { toast.warn("音色名称不能为空"); return; }
      setBusy("clone");
      try {
        const d = await api("/voices/clone", { method: "POST", body: cloneForm });
        toast.info("克隆任务已启动（GPT-SoVITS 独立子进程）");
        setCloneTask({ id: d.clone_task_id, state: "running", progress: 0 });
        loadVoices();
        clearInterval(pollRef.current);
        pollRef.current = setInterval(async () => {
          try {
            const st = await api(`/voices/clone/${d.clone_task_id}/status`);
            setCloneTask({ id: d.clone_task_id, ...st });
            if (st.state !== "running") {
              clearInterval(pollRef.current);
              if (st.state === "done") {
                toast.ok(st.stub ? "克隆完成（占位降级试听）" : "克隆完成，可试听");
                setCloneOpen(false); loadVoices();
              } else toast.err("克隆失败：" + (st.error || ""));
            }
          } catch (e) { clearInterval(pollRef.current); }
        }, 2000);
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    const emoParamsText = (e) => `强度 ${Number(e.emotion_intensity).toFixed(2)} · 语速 ${Number(e.speed).toFixed(2)}x · 音调 ${e.pitch >= 0 ? "+" : ""}${Number(e.pitch).toFixed(2)}`;

    return (
      <div className="flex flex-col gap-4">
        {/* ============ 角色音色绑定 ============ */}
        <section>
          <h4 className="section-title"><Icon name="Users" size={15} /> 角色音色绑定</h4>
          {charIds.length === 0 && (
            <p className="text-tertiary" style={{ fontSize: "var(--text-sm)", margin: 0 }}>
              本分镜未绑定角色。请先在分镜表「角色」列绑定角色，再为其配置音色与情感标签。
            </p>
          )}
          <div className="flex flex-col gap-3">
            {charIds.map((cid) => {
              const f = forms[cid] || {};
              const voice = voiceOf(f.voice_id);
              const emos = voice.emotions || [];
              const cur = bound[cid];
              return (
                <div key={cid} className="card" style={{ padding: "var(--space-3)" }}>
                  <div className="flex items-center gap-2 mb-2">
                    <Icon name="User" size={14} />
                    <b style={{ fontSize: "var(--text-sm)" }}>{(charMap[cid] || {}).name || cid}</b>
                    {cur && (
                      <span className="tag tag-success" title={`同步于 ${cur.synced_at ? new Date(cur.synced_at * 1000).toLocaleString("zh-CN") : "—"}`}>
                        已绑定 · {cur.emotion_label || cur.emotion_id}
                      </span>
                    )}
                  </div>
                  <select className="select mb-2" value={f.voice_id || ""} aria-label="选择音色"
                    onChange={(e) => {
                      const v = voiceOf(e.target.value);
                      const ves = v.emotions || [];
                      const def = ves.find((x) => x.is_default) || ves[0] || {};
                      setForm(cid, { voice_id: v.id, emotion_ids: ves.map((x) => x.id), default_emotion_id: def.id || "" });
                    }}>
                    {voices.map((v) => <option key={v.id} value={v.id}>{v.name}{v.is_preset ? "" : "（克隆）"}</option>)}
                  </select>
                  {emos.length > 0 && (
                    <div className="flex flex-col gap-1 mb-2">
                      <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>拥有情感标签（勾选）· 圆点为分镜默认</span>
                      {emos.map((e) => (
                        <label key={e.id} className="flex items-center gap-2" style={{ cursor: "pointer", fontSize: "var(--text-sm)" }}>
                          <input type="checkbox" checked={(f.emotion_ids || []).includes(e.id)}
                            onChange={(ev) => {
                              const set_ = new Set(f.emotion_ids || []);
                              ev.target.checked ? set_.add(e.id) : set_.delete(e.id);
                              setForm(cid, { emotion_ids: [...set_] });
                            }} />
                          <input type="radio" name={`def-emo-${cid}`} checked={f.default_emotion_id === e.id}
                            title="设为分镜默认标签"
                            onChange={() => setForm(cid, { default_emotion_id: e.id })} />
                          <span className="flex-1">{e.label}{e.is_default ? "（音色默认）" : ""}</span>
                          <span className="text-tertiary" style={{ fontSize: 10 }}>{emoParamsText(e)}</span>
                        </label>
                      ))}
                    </div>
                  )}
                  <button className="btn btn-primary" style={{ padding: "4px 12px" }}
                    disabled={busy === "bind:" + cid || !f.voice_id}
                    onClick={() => doBind(cid, false)}>
                    {busy === "bind:" + cid ? <Spinner size={13} /> : <Icon name="Link" size={13} />} 绑定并全局同步
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        {/* ============ 音色库与情感标签 ============ */}
        <section>
          <div className="flex items-center justify-between mb-2">
            <h4 className="section-title" style={{ margin: 0 }}><Icon name="Library" size={15} /> 音色库（{voices.length}）</h4>
            <button className="btn btn-secondary" style={{ padding: "3px 10px" }} onClick={() => setCloneOpen(true)}>
              <Icon name="Copy" size={13} /> 克隆音色
            </button>
          </div>
          <div className="flex flex-col gap-2" style={{ maxHeight: 340, overflowY: "auto", paddingRight: 2 }}>
            {voices.map((v) => (
              <div key={v.id} className="card" style={{ padding: "var(--space-2) var(--space-3)" }}>
                <div className="flex items-center gap-2">
                  <button className="btn btn-icon" title={playing === v.id ? "播放中" : "试听"}
                    onClick={() => playSample(v.id)}>
                    <Icon name={playing === v.id ? "Volume2" : "Play"} size={13} />
                  </button>
                  <span className="flex-1 ellipsis" style={{ fontSize: "var(--text-sm)" }}>{v.name}</span>
                  <span className="tag">{v.category || (v.is_preset ? "预置" : "克隆")}</span>
                  <button className="btn btn-icon" title="管理情感标签"
                    onClick={() => setExpanded(expanded === v.id ? "" : v.id)}>
                    <Icon name={expanded === v.id ? "ChevronUp" : "ChevronDown"} size={13} />
                  </button>
                </div>
                {expanded === v.id && (
                  <div className="flex flex-col gap-1 mt-2" style={{ borderTop: "1px solid var(--color-border)", paddingTop: 8 }}>
                    {(v.emotions || []).map((e) => (
                      <div key={e.id} className="flex items-center gap-2" style={{ fontSize: "var(--text-xs)" }}>
                        <span className={"tag " + (e.is_default ? "tag-info" : "")}>{e.label}</span>
                        <span className="text-tertiary flex-1">{emoParamsText(e)}</span>
                        <button className="btn btn-icon" title="编辑标签参数"
                          onClick={() => setEditEmo({ voice_id: v.id, emo: { ...e } })}>
                          <Icon name="Pencil" size={11} />
                        </button>
                        <button className="btn btn-icon" title={e.is_default ? "默认标签不可删除" : "删除标签"}
                          disabled={!!e.is_default} onClick={() => delEmotion(e)}>
                          <Icon name="Trash2" size={11} />
                        </button>
                      </div>
                    ))}
                    {addFor === v.id ? (
                      <div className="flex items-center gap-1 mt-1">
                        <input className="input" style={{ flex: 1, padding: "3px 8px" }} placeholder="新标签名（如：喜悦）"
                          value={newEmo.label} onChange={(e) => setNewEmo((n) => ({ ...n, label: e.target.value }))} />
                        <button className="btn btn-primary" style={{ padding: "3px 10px" }} disabled={busy === "add"}
                          onClick={() => addEmotion(v.id)}><Icon name="Check" size={12} /></button>
                        <button className="btn btn-ghost" style={{ padding: "3px 8px" }} onClick={() => setAddFor("")}>
                          <Icon name="X" size={12} /></button>
                      </div>
                    ) : (
                      <button className="btn btn-ghost mt-1" style={{ padding: "2px 8px", alignSelf: "flex-start" }}
                        onClick={() => setAddFor(v.id)}>
                        <Icon name="Plus" size={12} /> 添加标签
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* ============ 情感标签编辑弹窗 ============ */}
        <Modal open={!!editEmo} onClose={() => setEditEmo(null)} title="编辑情感标签" width={420}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setEditEmo(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy === "emo"} onClick={() => saveEmotion(false)}>
              {busy === "emo" ? <Spinner size={14} /> : <Icon name="Save" size={14} />} 保存并同步
            </button>
          </>}>
          {editEmo && (
            <div className="flex flex-col gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>标签名称</span>
                <input className="input" value={editEmo.emo.label}
                  onChange={(e) => setEditEmo((s) => ({ ...s, emo: { ...s.emo, label: e.target.value } }))} />
              </label>
              {[
                { k: "emotion_intensity", label: "情感强度", min: 0, max: 1, step: 0.05 },
                { k: "speed", label: "语速", min: 0.5, max: 2, step: 0.05 },
                { k: "pitch", label: "音调", min: -1, max: 1, step: 0.05 },
              ].map((s) => (
                <div key={s.k}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{s.label}</span>
                    <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                      {Number(editEmo.emo[s.k]).toFixed(2)}
                    </span>
                  </div>
                  <input type="range" min={s.min} max={s.max} step={s.step} value={editEmo.emo[s.k]}
                    aria-label={s.label} style={{ width: "100%", accentColor: "var(--color-primary)" }}
                    onChange={(e) => setEditEmo((st) => ({ ...st, emo: { ...st.emo, [s.k]: parseFloat(e.target.value) } }))} />
                </div>
              ))}
              <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
                保存后若该标签已被引用，将弹窗确认并全局同步到所有角色与分镜。
              </p>
            </div>
          )}
        </Modal>

        {/* ============ 克隆音色弹窗 ============ */}
        <Modal open={cloneOpen} onClose={() => setCloneOpen(false)} title="克隆音色（GPT-SoVITS）" width={460}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setCloneOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy === "clone" || (cloneTask && cloneTask.state === "running")}
              onClick={doClone}>
              {busy === "clone" ? <Spinner size={14} /> : <Icon name="Wand2" size={14} />} 开始克隆
            </button>
          </>}>
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>音色名称</span>
              <input className="input" value={cloneForm.name} placeholder="如：主角小满"
                onChange={(e) => setCloneForm((f) => ({ ...f, name: e.target.value }))} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>参考音频路径（≥3 秒清晰人声）</span>
              <input className="input" value={cloneForm.reference_audio} placeholder="D:/samples/ref.wav"
                onChange={(e) => setCloneForm((f) => ({ ...f, reference_audio: e.target.value }))} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>参考音频对应文本（零样本）</span>
              <textarea className="textarea" rows={2} value={cloneForm.reference_text}
                onChange={(e) => setCloneForm((f) => ({ ...f, reference_text: e.target.value }))} />
            </label>
            {cloneTask && cloneTask.state === "running" && (
              <div className="flex items-center gap-2">
                <Spinner size={14} />
                <span style={{ fontSize: "var(--text-sm)" }}>克隆中… {Math.round((cloneTask.progress || 0) * 100)}%</span>
              </div>
            )}
            <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
              创建后自动生成 3 个默认情感标签（默认/愤怒/悲伤）；克隆在独立子进程执行，依赖缺失时降级占位试听。
            </p>
          </div>
        </Modal>

        {/* ============ 全局同步确认弹窗 ============ */}
        <Modal open={!!confirm} onClose={() => setConfirm(null)} title={confirm ? confirm.title : ""} width={420}
          footer={<>
            <button className="btn btn-ghost" onClick={() => setConfirm(null)}>取消</button>
            <button className="btn btn-primary" onClick={async () => { const fn = confirm && confirm.onOk; setConfirm(null); fn && (await fn()); }}>
              确认同步
            </button>
          </>}>
          {confirm && (
            <div className="flex flex-col gap-2">
              {confirm.lines.map((ln, i) => (
                <p key={i} className="text-secondary" style={{ margin: 0, fontSize: "var(--text-sm)" }}>{ln}</p>
              ))}
            </div>
          )}
        </Modal>
      </div>
    );
  }

  /* ==================== 3D 导演台 ==================== */
  const MOTION_TYPES = [
    { id: "push", label: "推" }, { id: "pull", label: "拉" },
    { id: "pan", label: "摇" }, { id: "truck", label: "移" },
    { id: "follow", label: "跟" }, { id: "static", label: "固定" },
  ];
  const SHOT_SIZES = [
    { id: "extreme_close", label: "大特写" }, { id: "close", label: "特写" },
    { id: "medium_close", label: "近景" }, { id: "medium", label: "中景" },
    { id: "full", label: "全景" }, { id: "extreme_full", label: "大全景" },
  ];

  function DirectorPanel({ shot }) {
    const [data, setData] = useState(null);
    const [saving, setSaving] = useState(false);
    const [stageOpen, setStageOpen] = useState(false);
    const Stage3D = window.Director3D && window.Director3D.StageModal;

    const load = useCallback(async () => {
      try {
        const d = await api(`/director/shot/${shot.id}`);
        const dd = d.director_data || {};
        setData({
          camera: {
            shot_size: (dd.camera || {}).shot_size || "medium",
            angle: (dd.camera || {}).angle || "eye",
            fov: (dd.camera || {}).fov != null ? dd.camera.fov : 35,
            height: (dd.camera || {}).height != null ? dd.camera.height : 1.6,
          },
          lighting: {
            key: (dd.lighting || {}).key || "natural",
            intensity: (dd.lighting || {}).intensity != null ? dd.lighting.intensity : 0.8,
            mood: (dd.lighting || {}).mood || "normal",
          },
          motion: {
            type: (dd.motion || {}).type || "static",
            duration: (dd.motion || {}).duration != null ? dd.motion.duration : 3,
            easing: (dd.motion || {}).easing || "linear",
          },
          notes: dd.notes || "",
        });
      } catch (err) { toast.err(err.message); }
    }, [shot.id]);

    useEffect(() => { load(); }, [load]);

    if (!data) return <div className="flex justify-center" style={{ padding: 32 }}><Spinner /></div>;

    const set = (sec, k, v) => setData((d) => ({ ...d, [sec]: { ...d[sec], [k]: v } }));

    const save = async () => {
      setSaving(true);
      try {
        await api(`/director/shot/${shot.id}`, { method: "PUT", body: data });
        toast.ok("导演数据已保存");
        Bus.emit("director-data-updated", { shot_id: shot.id });
      } catch (err) { toast.err(err.message); }
      finally { setSaving(false); }
    };

    // 机位俯视示意（SVG）
    const angleDeg = { low: -30, eye: 0, high: 30, top: 90 }[data.camera.angle] || 0;

    return (
      <div className="flex flex-col gap-4">
        {/* 3D 导演台入口（文档 2.1 §4.2：全屏 Three.js 导演台） */}
        <button className="btn btn-primary" onClick={() => setStageOpen(true)} disabled={!Stage3D}>
          <Icon name="Clapperboard" size={15} /> 打开 3D 导演台
        </button>
        {Stage3D && (
          <Stage3D shot={shot} open={stageOpen}
            onClose={() => setStageOpen(false)}
            onSaved={load} />
        )}

        {/* 机位示意 */}
        <div className="card" style={{ padding: "var(--space-3)", background: "var(--color-surface-muted)" }}>
          <svg viewBox="0 0 200 110" style={{ width: "100%", display: "block" }} aria-label="机位示意图">
            <rect x="8" y="8" width="184" height="94" rx="6" fill="none" stroke="var(--color-border)" />
            {/* 主体 */}
            <circle cx="100" cy="55" r="10" fill="var(--color-primary)" opacity="0.85" />
            {/* 相机 */}
            <g transform={`translate(100,55) rotate(${angleDeg}) translate(${-58 - data.camera.height * 4},0)`}>
              <rect x="-8" y="-5" width="16" height="10" rx="2" fill="var(--color-text-secondary)" />
              <polygon points="8,-4 8,4 30,12 30,-12" fill="var(--color-info)" opacity="0.35" />
            </g>
            <text x="14" y="22" fontSize="9" fill="var(--color-text-tertiary)">
              {(SHOT_SIZES.find((s) => s.id === data.camera.shot_size) || {}).label} · FOV {data.camera.fov}°
            </text>
          </svg>
        </div>

        {/* 机位 */}
        <section>
          <h4 className="section-title"><Icon name="Camera" size={15} /> 机位</h4>
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>景别</span>
              <select className="select" value={data.camera.shot_size}
                onChange={(e) => set("camera", "shot_size", e.target.value)}>
                {SHOT_SIZES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>角度</span>
              <select className="select" value={data.camera.angle}
                onChange={(e) => set("camera", "angle", e.target.value)}>
                <option value="low">低角度（仰拍）</option>
                <option value="eye">平视</option>
                <option value="high">高角度（俯拍）</option>
                <option value="top">顶视</option>
              </select>
            </label>
            {[
              { k: "fov", label: "焦距 FOV", min: 10, max: 120, step: 1, unit: "°" },
              { k: "height", label: "机位高度", min: 0.2, max: 3.0, step: 0.1, unit: "m" },
            ].map((s) => (
              <div key={s.k}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{s.label}</span>
                  <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                    {data.camera[s.k]}{s.unit}
                  </span>
                </div>
                <input type="range" min={s.min} max={s.max} step={s.step} value={data.camera[s.k]}
                  aria-label={s.label} style={{ width: "100%", accentColor: "var(--color-primary)" }}
                  onChange={(e) => set("camera", s.k, parseFloat(e.target.value))} />
              </div>
            ))}
          </div>
        </section>

        {/* 灯光 */}
        <section>
          <h4 className="section-title"><Icon name="Lightbulb" size={15} /> 灯光</h4>
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>主光类型</span>
              <select className="select" value={data.lighting.key}
                onChange={(e) => set("lighting", "key", e.target.value)}>
                <option value="natural">自然光</option>
                <option value="three_point">三点布光</option>
                <option value="rembrandt">伦勃朗光</option>
                <option value="backlight">逆光剪影</option>
                <option value="neon">霓虹氛围</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>情绪基调</span>
              <select className="select" value={data.lighting.mood}
                onChange={(e) => set("lighting", "mood", e.target.value)}>
                <option value="normal">常规</option>
                <option value="warm">温暖</option>
                <option value="cold">冷峻</option>
                <option value="dark">阴暗</option>
                <option value="dreamy">梦幻</option>
              </select>
            </label>
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>光强</span>
                <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                  {Math.round(data.lighting.intensity * 100)}%
                </span>
              </div>
              <input type="range" min={0} max={1} step={0.05} value={data.lighting.intensity}
                aria-label="光强" style={{ width: "100%", accentColor: "var(--color-primary)" }}
                onChange={(e) => set("lighting", "intensity", parseFloat(e.target.value))} />
            </div>
          </div>
        </section>

        {/* 运镜 */}
        <section>
          <h4 className="section-title"><Icon name="Video" size={15} /> 运镜</h4>
          <div className="flex flex-wrap gap-2 mb-3">
            {MOTION_TYPES.map((m) => (
              <button key={m.id}
                className={"tag " + (data.motion.type === m.id ? "tag-info" : "")}
                style={{ cursor: "pointer", border: "none", padding: "4px var(--space-3)" }}
                onClick={() => set("motion", "type", m.id)}>{m.label}</button>
            ))}
          </div>
          {data.motion.type !== "static" && (
            <div className="flex flex-col gap-3">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>时长</span>
                  <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{data.motion.duration}s</span>
                </div>
                <input type="range" min={1} max={10} step={0.5} value={data.motion.duration}
                  aria-label="运镜时长" style={{ width: "100%", accentColor: "var(--color-primary)" }}
                  onChange={(e) => set("motion", "duration", parseFloat(e.target.value))} />
              </div>
              <label className="flex flex-col gap-1">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>缓动</span>
                <select className="select" value={data.motion.easing}
                  onChange={(e) => set("motion", "easing", e.target.value)}>
                  <option value="linear">匀速</option>
                  <option value="ease_in">缓入</option>
                  <option value="ease_out">缓出</option>
                  <option value="ease_in_out">缓入缓出</option>
                </select>
              </label>
            </div>
          )}
        </section>

        {/* 导演备注 */}
        <section>
          <h4 className="section-title"><Icon name="StickyNote" size={15} /> 导演备注</h4>
          <textarea className="textarea" rows={3} value={data.notes}
            placeholder="此镜头的特殊要求、参考片例…"
            onChange={(e) => setData((d) => ({ ...d, notes: e.target.value }))} />
        </section>

        <button className="btn btn-primary" disabled={saving} onClick={save}>
          {saving ? <Spinner size={14} /> : <Icon name="Save" size={15} />} 保存导演数据
        </button>
      </div>
    );
  }

  /* ==================== 四工序流水线 ==================== */
  function PipelinePanel({ shot, onChanged }) {
    const [prog, setProg] = useState(null);
    const [busy, setBusy] = useState("");

    const load = useCallback(async () => {
      try { setProg(await api(`/director/pipeline/${shot.id}`)); } catch (err) { toast.err(err.message); }
    }, [shot.id]);
    useEffect(() => { load(); }, [load]);

    // 任务完成后自动刷新
    useEffect(() => Bus.on("task-progress-update", ({ type, payload }) => {
      if ((type === "task_completed" || type === "task_failed") &&
          payload && payload.shot_id === shot.id) load();
    }), [shot.id, load]);

    if (!prog) return <div className="flex justify-center" style={{ padding: 32 }}><Spinner /></div>;

    const completed = prog.completed_stages || [];
    const outputs = prog.stage_outputs || {};
    const cur = prog.current_stage;
    const allDone = prog.status === "done";

    const advance = async () => {
      setBusy(cur);
      try {
        const d = await api(`/director/pipeline/${shot.id}/advance`, { method: "POST" });
        toast.ok(d.task_id ? `已提交 ${STAGE_LABEL[cur]} 任务` : "工序已推进");
        load(); onChanged && onChanged();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };
    const reset = async () => {
      try {
        await api(`/director/pipeline/${shot.id}/reset`, { method: "POST" });
        toast.info("流水线已重置");
        load(); onChanged && onChanged();
      } catch (err) { toast.err(err.message); }
    };

    return (
      <div className="flex flex-col gap-4">
        {/* 工序步骤条 */}
        <div className="flex flex-col gap-2">
          {STAGE_ORDER.map((st, i) => {
            const done = completed.includes(st);
            const active = st === cur && !allDone;
            const out = outputs[st];
            return (
              <div key={st} className="card" style={{
                padding: "var(--space-3)",
                border: active ? "1px solid var(--color-primary)" : "1px solid var(--color-border)",
                background: done ? "var(--color-surface-muted)" : "var(--color-surface)",
              }}>
                <div className="flex items-center gap-2">
                  <span style={{
                    width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    fontSize: "var(--text-xs)", fontWeight: "var(--font-semibold)",
                    background: done ? "var(--color-success)" : active ? "var(--color-primary)" : "var(--color-border)",
                    color: "#fff",
                  }}>{done ? <Icon name="Check" size={12} /> : i + 1}</span>
                  <span style={{ fontWeight: "var(--font-medium)" }}>{STAGE_LABEL[st]}</span>
                  {active && <span className="tag tag-warning">当前工序</span>}
                  {done && <span className="tag tag-success">已完成</span>}
                </div>
                {out && out.path && (
                  <div className="mt-2">
                    {st === "audio" ? (
                      <audio controls src={fileUrl(out.path)} style={{ width: "100%", height: 32 }} />
                    ) : st === "img2video" || st === "compose" ? (
                      <video controls src={fileUrl(out.path)} style={{ width: "100%", borderRadius: "var(--radius-sm)" }} />
                    ) : (
                      <img src={fileUrl(out.path)} alt={STAGE_LABEL[st]}
                        style={{ width: "100%", borderRadius: "var(--radius-sm)" }} />
                    )}
                  </div>
                )}
                {out && out.message && !out.path && (
                  <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0" }}>{out.message}</p>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex gap-2">
          {!allDone ? (
            <button className="btn btn-primary flex-1" disabled={!!busy} onClick={advance}>
              {busy ? <Spinner size={14} /> : <Icon name="Play" size={15} />}
              执行 {STAGE_LABEL[cur]}
            </button>
          ) : (
            <div className="tag tag-success" style={{ flex: 1, justifyContent: "center", padding: "var(--space-2)" }}>
              <Icon name="CheckCircle2" size={14} /> 全部工序完成
            </div>
          )}
          <button className="btn btn-secondary" onClick={reset}><Icon name="RotateCcw" size={14} /> 重置</button>
        </div>

        <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
          工序按 text2img → img2video → audio → compose 顺序执行，任务提交至 GPU/CPU 队列异步处理。
        </p>
      </div>
    );
  }

  /* ==================== 空间推理面板 ==================== */
  function SpatialPanel() {
    const [mode, setMode] = useState("fast");
    const [status, setStatus] = useState(null);
    const [result, setResult] = useState(null);
    const [running, setRunning] = useState(false);
    const fileRef = useRef(null);

    useEffect(() => {
      let alive = true;
      (async () => {
        try { const d = await api("/director/spatial/status"); if (alive) setStatus(d); } catch (e) {}
      })();
      return () => { alive = false; };
    }, []);

    const run = async (file) => {
      if (!file) return;
      setRunning(true); setResult(null);
      try {
        const fd = new FormData();
        fd.append("file", file);
        const d = await api(`/director/spatial/upload?mode=${mode}`, { method: "POST", formData: fd, timeout: 120000 });
        setResult(d);
      } catch (err) { toast.err(err.message); }
      finally { setRunning(false); if (fileRef.current) fileRef.current.value = ""; }
    };

    const objects = result && result.outputs && result.outputs.objects || [];
    const desc = result && result.outputs && result.outputs.spatial_desc;
    const hints = result && result.outputs && result.outputs.director_hints;
    const graph = result && result.outputs && result.outputs.scene_graph;
    const zones = result && result.outputs && result.outputs.depth_map && result.outputs.depth_map.zones;

    return (
      <div className="flex flex-col gap-4">
        {/* 引擎状态 */}
        {status && (
          <div className="flex items-center gap-2 flex-wrap">
            <span className={"tag " + (status.backend === "torch" ? "tag-success" : "tag-warning")}>
              {status.backend === "torch" ? "MiDaS+YOLO" : "规则引擎"}
            </span>
            <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
              {status.device}{status.load_error ? " · " + status.load_error : ""}
            </span>
          </div>
        )}

        {/* 模式切换 */}
        <div className="seg" role="tablist" aria-label="推理模式">
          <button className={"seg-item" + (mode === "fast" ? " active" : "")} role="tab"
            aria-selected={mode === "fast"} onClick={() => setMode("fast")}>快速模式</button>
          <button className={"seg-item" + (mode === "expert" ? " active" : "")} role="tab"
            aria-selected={mode === "expert"} onClick={() => setMode("expert")}>专家模式</button>
        </div>
        <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
          {mode === "fast"
            ? "快速模式：深度分区 + 物体检测 + 空间描述，秒级返回"
            : "专家模式：完整 7 步管线，额外输出深度图 / 场景图 / 导演参数建议"}
        </p>

        {/* 上传 */}
        <input ref={fileRef} type="file" accept="image/*" style={{ display: "none" }}
          onChange={(e) => run(e.target.files && e.target.files[0])} />
        <button className="btn btn-primary" disabled={running} onClick={() => fileRef.current && fileRef.current.click()}>
          {running ? <Spinner size={14} /> : <Icon name="Upload" size={15} />}
          {running ? "推理中…" : "上传图片分析"}
        </button>

        {/* 结果 */}
        {result && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-2 text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
              <Icon name="Timer" size={12} /> {result.elapsed_ms}ms
              <span>·</span><span>{result.image_size.w}×{result.image_size.h}</span>
              <span>·</span><span>{result.outputs.objects ? result.outputs.objects.length : 0} 个物体</span>
            </div>

            {/* 深度分区 */}
            {zones && (
              <section>
                <h4 className="section-title"><Icon name="Layers" size={15} /> 深度分区</h4>
                <div className="flex gap-2">
                  {[["近景", zones.near], ["中景", zones.mid], ["远景", zones.far]].map(([lb, v]) => (
                    <div key={lb} className="card flex-1" style={{ padding: "var(--space-2)", textAlign: "center" }}>
                      <div className="text-mono" style={{ fontWeight: "var(--font-semibold)" }}>
                        {v != null ? Math.round(v * 100) + "%" : "—"}
                      </div>
                      <div className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{lb}</div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* 物体列表 */}
            {objects.length > 0 && (
              <section>
                <h4 className="section-title"><Icon name="Boxes" size={15} /> 检测物体</h4>
                <div className="flex flex-col gap-1">
                  {objects.slice(0, 10).map((o, i) => (
                    <div key={i} className="flex items-center gap-2" style={{ fontSize: "var(--text-sm)" }}>
                      <span className="tag tag-info">{o.label || o.cls}</span>
                      <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                        {o.depth_zone ? `位于${{ near: "近景", mid: "中景", far: "远景" }[o.depth_zone] || o.depth_zone}` : ""}
                        {o.confidence != null ? ` · 置信度 ${(o.confidence * 100).toFixed(0)}%` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* 空间描述 */}
            {desc && (
              <section>
                <h4 className="section-title"><Icon name="FileText" size={15} /> 空间描述</h4>
                <p className="text-secondary" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: "var(--leading-relaxed)" }}>
                  {typeof desc === "string" ? desc : JSON.stringify(desc, null, 2)}
                </p>
              </section>
            )}

            {/* 场景图（专家） */}
            {graph && mode === "expert" && graph.edges_list && (
              <section>
                <h4 className="section-title"><Icon name="GitBranch" size={15} /> 场景图</h4>
                <div className="flex flex-col gap-1">
                  {(graph.edges_list || []).slice(0, 12).map((e, i) => (
                    <div key={i} className="text-secondary" style={{ fontSize: "var(--text-sm)" }}>
                      <span className="tag">{e.subject}</span>
                      <span className="text-tertiary" style={{ margin: "0 6px" }}>{e.relation}</span>
                      <span className="tag">{e.object}</span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* 导演参数建议 */}
            {hints && (
              <section>
                <h4 className="section-title"><Icon name="Clapperboard" size={15} /> 导演建议</h4>
                <div className="card" style={{ padding: "var(--space-3)", background: "var(--color-primary-grad-from)" }}>
                  <pre className="text-secondary text-mono" style={{
                    fontSize: "var(--text-xs)", margin: 0, whiteSpace: "pre-wrap",
                  }}>{JSON.stringify(hints, null, 2)}</pre>
                </div>
              </section>
            )}
          </div>
        )}

        {!result && !running && (
          <EmptyState art="🧭" title="尚未分析" hint="上传一张参考图，引擎将输出深度分区、物体空间关系与导演参数建议" />
        )}
      </div>
    );
  }

  window.M6 = { Workbench };
})();
