/* ============================================================
 * OmniSpace AI - 纯前端 Mock 层
 * 拦截所有 fetch 请求，返回模拟数据，无需后端即可完整交互
 * ============================================================ */
(function () {
  'use strict';

  // ============ 模拟数据存储 ============
  const DB = {
    projects: [
      {
        id: 'proj_demo_001',
        name: '深夜食堂 第1集',
        status: 'STORYBOARD',
        script_text: '深夜，老街的雨下个不停。少女撑着一把红伞，独自走在青石板路上。她回头看了一眼身后的巷子，嘴角微微上扬。远处传来钟声，已经是午夜十二点了。她加快脚步，走向街角那家还亮着灯的小店。',
        created_at: Date.now() / 1000 - 86400,
        updated_at: Date.now() / 1000 - 3600,
        pipeline: { current: 'STORYBOARD', completed: ['SCRIPT_INPUT'] }
      },
      {
        id: 'proj_demo_002',
        name: '樱花树下的约定',
        status: 'COMPLETE',
        script_text: '春天的樱花树下，少年和少女许下了十年后再见的约定。',
        created_at: Date.now() / 1000 - 172800,
        updated_at: Date.now() / 1000 - 7200,
        pipeline: { current: 'COMPLETE', completed: ['SCRIPT_INPUT','STORYBOARD','ASSET_PLAN','ASSET_GEN','DIRECTOR','VIDEO','CONFIRM'] }
      }
    ],
    storyboards: {
      'proj_demo_001': [
        { id: 'shot_001', project_id: 'proj_demo_001', sort_index: 0, original_text: '深夜，老街的雨下个不停。', shot_description: '雨夜老街全景，青石板路反射着昏黄的路灯，雨丝细密', character_ids: ['char_001'], scene_id: 'scene_001', prop_ids: [], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } },
        { id: 'shot_002', project_id: 'proj_demo_001', sort_index: 1, original_text: '少女撑着一把红伞，独自走在青石板路上。', shot_description: '中景，少女撑红伞走在雨中，背影，红伞在冷色调中格外醒目', character_ids: ['char_001'], scene_id: 'scene_001', prop_ids: ['prop_001'], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } },
        { id: 'shot_003', project_id: 'proj_demo_001', sort_index: 2, original_text: '她回头看了一眼身后的巷子，嘴角微微上扬。', shot_description: '近景特写，少女回头，嘴角微笑，雨水从发梢滴落', character_ids: ['char_001'], scene_id: 'scene_001', prop_ids: ['prop_001'], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } },
        { id: 'shot_004', project_id: 'proj_demo_001', sort_index: 3, original_text: '远处传来钟声，已经是午夜十二点了。', shot_description: '远景，钟楼在雨雾中若隐若现，钟面显示12点', character_ids: [], scene_id: 'scene_002', prop_ids: [], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } },
        { id: 'shot_005', project_id: 'proj_demo_001', sort_index: 4, original_text: '她加快脚步，走向街角那家还亮着灯的小店。', shot_description: '跟拍镜头，少女快步走向街角，小店暖黄色灯光透出', character_ids: ['char_001'], scene_id: 'scene_003', prop_ids: ['prop_001'], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } }
      ]
    },
    assets: [
      { id: 'char_001', name: '林晚', type: 'character', description: '女主角，17岁少女，黑色长发，红色发带', created_at: Date.now()/1000 - 86400 },
      { id: 'char_002', name: '陈默', type: 'character', description: '男主角，18岁少年，戴眼镜，安静内敛', created_at: Date.now()/1000 - 86400 },
      { id: 'scene_001', name: '雨夜老街', type: 'scene', description: '青石板路，两侧老式建筑，昏黄路灯', created_at: Date.now()/1000 - 86400 },
      { id: 'scene_002', name: '钟楼远景', type: 'scene', description: '欧式钟楼，雨雾缭绕', created_at: Date.now()/1000 - 86400 },
      { id: 'scene_003', name: '街角小店', type: 'scene', description: '深夜食堂，暖黄色灯光，木质门面', created_at: Date.now()/1000 - 86400 },
      { id: 'prop_001', name: '红伞', type: 'prop', description: '鲜红色油纸伞', created_at: Date.now()/1000 - 86400 }
    ],
    voices: [
      { id: 'voice_001', name: '温柔少女音', language: 'zh', gender: 'female', emotions: [{ id: 'emo_001', label: '平静', is_default: true, emotion_intensity: 0.5, speed: 1.0, pitch: 0.0 }, { id: 'emo_002', label: '开心', emotion_intensity: 0.8, speed: 1.1, pitch: 2.0 }, { id: 'emo_003', label: '悲伤', emotion_intensity: 0.6, speed: 0.9, pitch: -1.0 }] },
      { id: 'voice_002', name: '清冷少年音', language: 'zh', gender: 'male', emotions: [{ id: 'emo_004', label: '平静', is_default: true, emotion_intensity: 0.5, speed: 1.0, pitch: 0.0 }, { id: 'emo_005', label: '严肃', emotion_intensity: 0.7, speed: 0.95, pitch: -0.5 }] },
      { id: 'voice_003', name: '成熟御姐音', language: 'zh', gender: 'female', emotions: [{ id: 'emo_006', label: '平静', is_default: true, emotion_intensity: 0.5, speed: 1.0, pitch: 0.0 }] }
    ],
    chatHistory: [
      { id: 'chat_001', title: '剧本创作讨论', messages: [
        { role: 'user', content: '帮我写一个深夜食堂的开场剧本', created_at: Date.now()/1000 - 7200 },
        { role: 'assistant', content: '好的，这是一个深夜食堂开场剧本的构思：\n\n**场景**：雨夜，老街深处，一家只有 counter 座位的小店。\n\n**开场镜头**：\n1. 全景：雨夜老街，只有一家小店亮着灯\n2. 中景：少女撑红伞走来，推门进入\n3. 特写：门铃轻响，老板抬头微笑\n\n需要我继续展开吗？', created_at: Date.now()/1000 - 7190 }
      ], created_at: Date.now()/1000 - 7200, updated_at: Date.now()/1000 - 7190 },
      { id: 'chat_002', title: '分镜技巧咨询', messages: [
        { role: 'user', content: '如何让分镜更有电影感？', created_at: Date.now()/1000 - 3600 },
        { role: 'assistant', content: '提升分镜电影感的几个技巧：\n\n1. **景别变化**：避免连续使用相同景别，用全景-中景-特写的节奏\n2. **角度设计**：低角度显力量，高角度显渺小，平视显客观\n3. **运镜衔接**：匹配剪辑（match cut）让转场更流畅\n4. **光线叙事**：用光影暗示情绪和时间\n5. **留白构图**：不要把画面填满，给观众想象空间', created_at: Date.now()/1000 - 3590 }
      ], created_at: Date.now()/1000 - 3600, updated_at: Date.now()/1000 - 3590 }
    ],
    imageHistory: [
      { id: 'img_001', prompt: '雨夜老街，青石板路，昏黄路灯，电影质感', created_at: Date.now()/1000 - 1800, status: 'done', thumbnail: null },
      { id: 'img_002', prompt: '少女撑红伞走在雨中，背影，冷色调', created_at: Date.now()/1000 - 900, status: 'done', thumbnail: null }
    ],
    knowledge: {
      stats: { knowledge_count: 128, corrections_count: 12, mode: 'chroma' },
      corrections: [],
      history: [
        { id: 'kh_001', source: 'manual', content: '分镜设计原则：景别变化、角度设计、运镜衔接', created_at: Date.now()/1000 - 3600, points: 5 },
        { id: 'kh_002', source: 'url', content: 'https://example.com/cinematography-tips', created_at: Date.now()/1000 - 7200, points: 12 }
      ],
      trainData: []
    },
    models: [
      { id: 'qwen2vl', name: 'Qwen2-VL', type: 'multimodal', size_mb: 4096, vram_mb: 2048, status: 'ready', strategy: 'resident', loaded: true },
      { id: 'sdxl', name: 'SDXL', type: 'image', size_mb: 6554, vram_mb: 3584, status: 'ready', strategy: 'on_demand', loaded: false },
      { id: 'animatelcm', name: 'AnimateLCM', type: 'video', size_mb: 4096, vram_mb: 2048, status: 'missing', strategy: 'on_demand', loaded: false },
      { id: 'gpt_sovits', name: 'GPT-SoVITS', type: 'voice', size_mb: 3072, vram_mb: 1536, status: 'ready', strategy: 'on_demand', loaded: false },
      { id: 'triposr', name: 'TripoSR', type: '3d', size_mb: 6144, vram_mb: 3072, status: 'missing', strategy: 'on_demand', loaded: false },
      { id: 'midas', name: 'MiDaS', type: 'depth', size_mb: 820, vram_mb: 410, status: 'ready', strategy: 'resident', loaded: true },
      { id: 'yolov8', name: 'YOLOv8', type: 'detection', size_mb: 820, vram_mb: 410, status: 'ready', strategy: 'resident', loaded: true },
      { id: 'sam', name: 'SAM', type: 'segmentation', size_mb: 5120, vram_mb: 2560, status: 'missing', strategy: 'on_demand', loaded: false }
    ],
    tasks: [
      { id: 'task_001', type: 'image_generate', model: 'sdxl', status: 'done', progress: 100, created_at: Date.now()/1000 - 1800, completed_at: Date.now()/1000 - 1700 },
      { id: 'task_002', type: 'voice_clone', model: 'gpt_sovits', status: 'running', progress: 45, created_at: Date.now()/1000 - 300 },
      { id: 'task_003', type: 'learning', model: 'qwen2vl', status: 'queued', progress: 0, created_at: Date.now()/1000 - 60 }
    ],
    trainQueue: [
      { id: 'tq_001', model_type: 'language', model_name: 'Qwen2-VL', learning_type: 'manual', priority: 5, status: 'done', progress: 100, created_at: Date.now()/1000 - 3600 },
      { id: 'tq_002', model_type: 'image', model_name: 'SDXL LoRA', learning_type: 'qa', priority: 3, status: 'queued', progress: 0, created_at: Date.now()/1000 - 600 }
    ],
    qualityLogs: [
      { id: 'ql_001', content_type: 'text', scores: { text: 4.2, image: 0, video: 0, audio: 0 }, overall_score: 4.2, status: 'accepted', evaluated_at: Date.now()/1000 - 1800 },
      { id: 'ql_002', content_type: 'page', scores: { text: 3.8, image: 0, video: 0, audio: 0 }, overall_score: 3.8, status: 'accepted', evaluated_at: Date.now()/1000 - 3600 }
    ]
  };

  // ============ 工具函数 ============
  function ok(data) { return { code: 0, message: 'ok', data: data }; }
  function err(code, msg) { return { code: code, message: msg }; }
  function uid(prefix) { return prefix + '_' + Math.random().toString(36).slice(2, 10); }
  function now() { return Date.now() / 1000; }

  // 路径参数匹配
  function matchPath(pattern, path) {
    const pParts = pattern.split('/').filter(Boolean);
    const aParts = path.split('/').filter(Boolean);
    if (pParts.length !== aParts.length) return null;
    const params = {};
    for (let i = 0; i < pParts.length; i++) {
      if (pParts[i].startsWith('{')) {
        params[pParts[i].slice(1, -1)] = decodeURIComponent(aParts[i]);
      } else if (pParts[i] !== aParts[i]) {
        return null;
      }
    }
    return params;
  }

  // ============ 路由定义 ============
  const routes = [];
  function route(method, pattern, handler) {
    routes.push({ method, pattern, handler });
  }

  // ---------- 系统 ----------
  route('GET', '/system/info', () => ok({
    app: 'OmniSpace AI', version: '1.0.0', python: '3.10.11',
    platform: 'Windows 11', cuda: true, cuda_version: '12.1'
  }));
  route('GET', '/system/gpu', () => ok({
    name: 'NVIDIA GeForce RTX 4060 Laptop GPU', total_mb: 8192, used_mb: 1245, driver: '551.76'
  }));
  route('GET', '/system/power', () => ok({ on_battery: false, percent: 100, sleep_events: 0 }));
  route('GET', '/system/health', () => ok({ status: 'healthy', db_encryption: 'AES-256', uptime: 86400 }));
  route('GET', '/health', () => ok({ status: 'ok' }));

  // ---------- 任务 ----------
  route('GET', '/tasks/stats', () => ok({ total: DB.tasks.length, running: 1, queued: 1, done: 1, failed: 0 }));
  route('GET', '/tasks', (q) => {
    let list = DB.tasks;
    if (q.state) list = list.filter(t => t.status === q.state);
    return ok({ items: list, total: list.length });
  });
  route('POST', '/tasks/{id}/{action}', (p) => {
    const t = DB.tasks.find(x => x.id === p.id);
    if (t) {
      if (p.action === 'pause') t.status = 'paused';
      else if (p.action === 'resume') t.status = 'running';
      else if (p.action === 'cancel') t.status = 'cancelled';
    }
    return ok({ success: true });
  });
  route('POST', '/tasks/{action}', (p) => ok({ success: true, action: p.action }));

  // ---------- 漫剧项目 ----------
  route('GET', '/projects', () => ok({ items: DB.projects, total: DB.projects.length }));
  route('GET', '/manga/projects', () => ok({ items: DB.projects, total: DB.projects.length }));
  route('POST', '/projects', (q, body) => {
    const p = { id: uid('proj'), name: body.name || '新项目', status: 'SCRIPT_INPUT', script_text: body.script_text || '', created_at: now(), updated_at: now(), pipeline: { current: 'SCRIPT_INPUT', completed: [] } };
    DB.projects.unshift(p);
    return ok(p);
  });
  route('POST', '/manga/projects', (q, body) => {
    const p = { id: uid('proj'), name: body.name || '新项目', status: 'SCRIPT_INPUT', script_text: body.script_text || '', created_at: now(), updated_at: now(), pipeline: { current: 'SCRIPT_INPUT', completed: [] } };
    DB.projects.unshift(p);
    return ok(p);
  });
  route('GET', '/projects/{id}', (p) => {
    const proj = DB.projects.find(x => x.id === p.id);
    return proj ? ok(proj) : err(404, '项目不存在');
  });
  route('GET', '/manga/projects/{id}', (p) => {
    const proj = DB.projects.find(x => x.id === p.id);
    return proj ? ok(proj) : err(404, '项目不存在');
  });
  route('PUT', '/projects/{id}', (p, body) => {
    const proj = DB.projects.find(x => x.id === p.id);
    if (proj) { Object.assign(proj, body, { updated_at: now() }); return ok(proj); }
    return err(404, '项目不存在');
  });
  route('PUT', '/manga/projects/{id}', (p, body) => {
    const proj = DB.projects.find(x => x.id === p.id);
    if (proj) { Object.assign(proj, body, { updated_at: now() }); return ok(proj); }
    return err(404, '项目不存在');
  });
  route('DELETE', '/projects/{id}', (p) => {
    const i = DB.projects.findIndex(x => x.id === p.id);
    if (i >= 0) { DB.projects.splice(i, 1); return ok({ success: true }); }
    return err(404, '项目不存在');
  });
  route('GET', '/projects/{id}/pipeline', (p) => {
    const proj = DB.projects.find(x => x.id === p.id);
    return ok(proj ? proj.pipeline : { current: 'SCRIPT_INPUT', completed: [] });
  });

  // ---------- 分镜 ----------
  route('GET', '/storyboard/{pid}', (p) => {
    const shots = DB.storyboards[p.pid] || [];
    return ok({ items: shots, total: shots.length });
  });
  route('GET', '/manga/storyboard', (q) => {
    const shots = DB.storyboards[q.project_id] || [];
    return ok({ items: shots, total: shots.length });
  });
  route('POST', '/storyboard/parse', (q, body) => {
    const shots = DB.storyboards[body.project_id] || [];
    return ok({ count: shots.length || 5, engine: 'llm', split_hint: null });
  });
  route('POST', '/manga/projects/{pid}/ai-split', (p, body) => ok({ count: 5, engine: 'llm' }));
  route('POST', '/storyboard/{pid}/shots', (p, body) => {
    if (!DB.storyboards[p.pid]) DB.storyboards[p.pid] = [];
    const shots = DB.storyboards[p.pid];
    const s = { id: uid('shot'), project_id: p.pid, sort_index: shots.length, original_text: body.original_text || '新分镜', shot_description: body.shot_description || '', character_ids: [], scene_id: null, prop_ids: [], voice_config: {}, status: 'pending', pipeline: { current_stage: 'text2img', completed_stages: [], stage_outputs: {} } };
    shots.push(s);
    return ok(s);
  });
  route('PUT', '/storyboard/shots/{id}', (p, body) => {
    for (const pid in DB.storyboards) {
      const s = DB.storyboards[pid].find(x => x.id === p.id);
      if (s) { Object.assign(s, body); return ok(s); }
    }
    return err(404, '分镜不存在');
  });
  route('PUT', '/manga/storyboard/{sid}', (p, body) => {
    for (const pid in DB.storyboards) {
      const s = DB.storyboards[pid].find(x => x.id === p.sid);
      if (s) { Object.assign(s, body); return ok(s); }
    }
    return err(404, '分镜不存在');
  });
  route('DELETE', '/storyboard/shots/{id}', (p) => {
    for (const pid in DB.storyboards) {
      const i = DB.storyboards[pid].findIndex(x => x.id === p.id);
      if (i >= 0) { DB.storyboards[pid].splice(i, 1); return ok({ success: true }); }
    }
    return err(404, '分镜不存在');
  });
  route('DELETE', '/manga/storyboard/{sid}', (p) => {
    for (const pid in DB.storyboards) {
      const i = DB.storyboards[pid].findIndex(x => x.id === p.sid);
      if (i >= 0) { DB.storyboards[pid].splice(i, 1); return ok({ success: true }); }
    }
    return err(404, '分镜不存在');
  });
  route('POST', '/storyboard/shots/{id}/move', (p, q) => {
    for (const pid in DB.storyboards) {
      const shots = DB.storyboards[pid];
      const i = shots.findIndex(x => x.id === p.id);
      if (i >= 0) {
        const to = parseInt(q.to_index || i, 10);
        const [item] = shots.splice(i, 1);
        shots.splice(Math.min(to, shots.length), 0, item);
        shots.forEach((s, idx) => s.sort_index = idx);
        return ok({ success: true });
      }
    }
    return err(404, '分镜不存在');
  });
  route('GET', '/storyboard/{pid}/history', () => ok({ items: [], total: 0 }));
  route('POST', '/storyboard/{pid}/undo', () => ok({ success: true }));
  route('POST', '/storyboard/{pid}/redo', () => ok({ success: true }));
  route('POST', '/storyboard/shots/{id}/bind-assets', () => ok({ success: true }));

  // ---------- 资产 ----------
  route('GET', '/assets', (q) => {
    let list = DB.assets;
    if (q.type) list = list.filter(a => a.type === q.type);
    if (q.keyword) list = list.filter(a => a.name.includes(q.keyword));
    return ok({ items: list, total: list.length });
  });
  route('GET', '/manga/assets', (q) => {
    let list = DB.assets;
    if (q.type) list = list.filter(a => a.type === q.type);
    return ok({ items: list, total: list.length });
  });
  route('POST', '/assets', (q, body) => {
    const a = { id: uid(body.type === 'character' ? 'char' : body.type === 'scene' ? 'scene' : 'prop'), name: body.name, type: body.type, description: body.description || '', created_at: now() };
    DB.assets.push(a);
    return ok(a);
  });
  route('POST', '/manga/assets', (q, body) => {
    const a = { id: uid('asset'), name: body.name, type: body.type, description: '', created_at: now() };
    DB.assets.push(a);
    return ok(a);
  });
  route('DELETE', '/assets/{id}', (p) => {
    const i = DB.assets.findIndex(x => x.id === p.id);
    if (i >= 0) { DB.assets.splice(i, 1); return ok({ success: true }); }
    return err(404, '资产不存在');
  });
  route('GET', '/assets/characters/list', () => ok({ items: DB.assets.filter(a => a.type === 'character'), total: DB.assets.filter(a => a.type === 'character').length }));
  route('POST', '/assets/plan', (q, body) => ok({ created: body.candidates.length, skipped: 0 }));
  route('POST', '/characters/{cid}/bind-voice', (p, body) => ok({ success: true, synced_storyboards: 3, affected: { storyboards: 3 } }));

  // ---------- 音色 ----------
  route('GET', '/voices/list', () => ok({ items: DB.voices, total: DB.voices.length }));
  route('GET', '/voices/emotions/{id}', (p) => {
    const v = DB.voices.find(x => x.id === p.id);
    return ok({ items: v ? v.emotions : [], total: v ? v.emotions.length : 0 });
  });
  route('POST', '/voices/{vid}/emotions', (p, body) => {
    const v = DB.voices.find(x => x.id === p.vid);
    if (v) {
      const e = { id: uid('emo'), label: body.label, emotion_intensity: body.emotion_intensity || 0.5, speed: body.speed || 1.0, pitch: body.pitch || 0.0 };
      v.emotions.push(e);
      return ok(e);
    }
    return err(404, '音色不存在');
  });
  route('DELETE', '/voices/emotions/{id}', (p) => {
    for (const v of DB.voices) {
      const i = v.emotions.findIndex(e => e.id === p.id);
      if (i >= 0) { v.emotions.splice(i, 1); return ok({ success: true }); }
    }
    return err(404, '情绪不存在');
  });
  route('GET', '/voices/sample/{vid}', () => ok({ url: '', duration: 3 }));
  route('POST', '/voices/clone', (q, body) => ok({ clone_task_id: uid('clone'), status: 'running' }));
  route('GET', '/voices/clone/{id}/status', () => ok({ status: 'running', progress: 45 }));

  // ---------- 导演台 ----------
  route('GET', '/director/state', () => ok({ active: true }));
  route('GET', '/director/shot/{id}', () => ok({
    director_data: {
      camera: { shot_size: 'medium', angle: 'eye', fov: 35, height: 1.6 },
      lighting: { key: 'natural', intensity: 0.8, mood: 'normal' },
      motion: { type: 'static', duration: 3, easing: 'linear' },
      notes: ''
    }
  }));
  route('PUT', '/director/shot/{id}', () => ok({ success: true }));
  route('GET', '/director/pipeline/{id}', () => ok({
    current_stage: 'text2img',
    completed_stages: [],
    stage_outputs: {},
    status: 'pending'
  }));
  route('POST', '/director/pipeline/{id}/advance', () => ok({ task_id: uid('task'), stage: 'text2img' }));
  route('POST', '/director/pipeline/{id}/reset', () => ok({ success: true }));
  route('GET', '/director/spatial/status', () => ok({ backend: 'torch', device: 'cuda', load_error: null }));
  route('POST', '/director/spatial/upload', () => ok({
    outputs: {
      objects: [{ name: '人', bbox: [100, 200, 300, 500], confidence: 0.95 }, { name: '伞', bbox: [120, 150, 280, 250], confidence: 0.88 }],
      spatial_desc: '画面中央偏左为人物，手持红伞位于人物上方，背景为雨夜街道。',
      director_hints: '建议使用中景，低角度仰拍以突出人物，冷色调主光配合暖黄环境光。',
      scene_graph: { nodes: [{ id: 'person', label: '人物' }, { id: 'umbrella', label: '伞' }], edges: [{ from: 'person', to: 'umbrella', relation: '手持' }] },
      depth_map: { zones: [{ label: '前景', depth: 0.2, area: 0.3 }, { label: '中景', depth: 0.5, area: 0.4 }, { label: '背景', depth: 0.9, area: 0.3 }] }
    }
  }));

  // ---------- AI 对话 ----------
  route('GET', '/chat/history', () => ok({ items: DB.chatHistory, total: DB.chatHistory.length }));
  route('POST', '/chat/send', (q, body) => ok({
    reply: '这是一个模拟的 AI 回复。在纯前端演示模式下，所有对话都由本地 mock 数据生成。\n\n你可以继续提问，我会给出相应的回答。',
    conversation_id: uid('chat'),
    usage: { prompt_tokens: 50, completion_tokens: 80 }
  }));

  // ---------- AI 绘画 ----------
  route('POST', '/image/generate', (q, body) => ok({ task_id: uid('task'), status: 'queued' }));
  route('GET', '/image/history', () => ok({ items: DB.imageHistory, total: DB.imageHistory.length }));

  // ---------- 知识学习 ----------
  route('GET', '/knowledge/stats', () => ok(DB.knowledge.stats));
  route('GET', '/knowledge/corrections', () => ok({ items: DB.knowledge.corrections, total: DB.knowledge.corrections.length }));
  route('GET', '/learning/history', () => ok({ items: DB.knowledge.history, total: DB.knowledge.history.length }));
  route('GET', '/learning/training-data', () => ok({ items: DB.knowledge.trainData, total: DB.knowledge.trainData.length }));
  route('POST', '/learning/text', (q, body) => ok({ points: 3, success: true }));
  route('POST', '/learning/url', (q, body) => ok({ points: 5, success: true }));
  route('POST', '/learning/auto', (q, body) => ok({ session_id: uid('auto'), status: 'running' }));
  route('GET', '/learning/auto/{sid}', () => ok({ status: 'running', pages_done: 3, pages_total: 50 }));
  route('POST', '/learning/auto/{sid}/stop', () => ok({ success: true }));
  route('POST', '/learning/urls/batch', (q, body) => ok({ count: body.urls.length, success: true }));
  route('GET', '/knowledge/search', (q) => ok({
    items: [
      { id: uid('kp'), document: q.q + ' 相关知识点1：分镜设计原则', score: 0.92 },
      { id: uid('kp'), document: q.q + ' 相关知识点2：电影感构图技巧', score: 0.85 },
      { id: uid('kp'), document: q.q + ' 相关知识点3：光线叙事方法', score: 0.78 }
    ], total: 3
  }));
  route('DELETE', '/knowledge/{id}', () => ok({ success: true }));
  route('POST', '/knowledge/rebuild', () => ok({ cleared: 128 }));
  route('POST', '/learning/qa', (q, body) => ok({ qa_pairs: body.points.length * 2, success: true }));
  route('GET', '/learning/panel', () => ok({
    queue: { queued: 1, running: 0, max: 2 },
    models: [
      { model_type: 'language', name: '语言模型', trainable: true, train_note: '', purpose: '对话理解与剧本生成', pending_data: 128, data_ready: true, running: 0, queued: 0, targets: ['Qwen2-VL'], default_priority: 5, last_trained_at: now() - 86400 },
      { model_type: 'image', name: '图像模型', trainable: true, train_note: '', purpose: '角色/场景 LoRA 训练', pending_data: 45, data_ready: true, running: 0, queued: 1, targets: ['SDXL'], default_priority: 4, last_trained_at: null },
      { model_type: 'voice', name: '语音模型', trainable: true, train_note: '', purpose: '音色克隆与情感适配', pending_data: 12, data_ready: false, running: 0, queued: 0, targets: ['GPT-SoVITS'], default_priority: 3, last_trained_at: null },
      { model_type: 'video', name: '视频模型', trainable: false, train_note: '显存不足，需≥12GB', purpose: '视频生成微调', pending_data: 0, data_ready: false, running: 0, queued: 0, targets: ['AnimateLCM'], default_priority: 2, last_trained_at: null },
      { model_type: 'auxiliary', name: '辅助模型', trainable: true, train_note: '', purpose: '深度估计/物体检测优化', pending_data: 0, data_ready: false, running: 0, queued: 0, targets: ['MiDaS', 'YOLOv8'], default_priority: 1, last_trained_at: null }
    ]
  }));
  route('GET', '/learning/train-queue', () => ok({ items: DB.trainQueue, total: DB.trainQueue.length }));
  route('POST', '/learning/train-queue', (q, body) => {
    const t = { id: uid('tq'), model_type: body.model_type, model_name: body.model_type, learning_type: 'manual', priority: 5, status: 'queued', progress: 0, created_at: now() };
    DB.trainQueue.unshift(t);
    return ok(t);
  });
  route('PUT', '/learning/train-queue/{id}/priority', (p, body) => {
    const t = DB.trainQueue.find(x => x.id === p.id);
    if (t) t.priority = body.priority;
    return ok({ success: true });
  });
  route('POST', '/learning/train-queue/{id}/move', (p, body) => ok({ success: true }));
  route('POST', '/learning/train-queue/{id}/cancel', (p) => {
    const t = DB.trainQueue.find(x => x.id === p.id);
    if (t) t.status = 'cancelled';
    return ok({ success: true });
  });
  route('GET', '/learning/quality/logs', (q) => {
    let list = DB.qualityLogs;
    if (q.content_type) list = list.filter(l => l.content_type === q.content_type);
    return ok({ items: list, total: list.length, threshold: 3.5 });
  });
  route('POST', '/learning/quality/evaluate', (q, body) => {
    const scores = { text: 3.5 + Math.random() * 1.5, image: 0, video: 0, audio: 0 };
    const overall = scores.text;
    const result = { id: uid('ql'), content_type: body.content_type, scores, overall_score: overall, overall, status: overall >= 3.5 ? 'accepted' : 'rejected', evaluated_at: now(), backend: 'rule-based' };
    DB.qualityLogs.unshift(result);
    return result;
  });

  // ---------- 模型管理 ----------
  route('GET', '/models', () => ok({ items: DB.models, total: DB.models.length, vram_budget: 7680, vram_used: 2876 }));
  route('POST', '/models/download', (q, body) => ok({ task_id: uid('dl'), status: 'queued' }));
  route('GET', '/models/lora', () => ok({ items: [], total: 0 }));
  route('POST', '/models/lora/train', () => ok({ task_id: uid('lora'), status: 'queued' }));

  // ---------- 激活 ----------
  route('GET', '/activation/status', () => ok({ activated: true, license_key: '****-****-****-DEMO', expires_at: now() + 365 * 86400 }));
  route('POST', '/activation/activate', () => ok({ success: true, activated: true }));

  // ---------- 空间推理 ----------
  route('POST', '/spatial/reason', () => ok({ depth: {}, objects: [], relations: [] }));

  // ============ 拦截 fetch ============
  const originalFetch = window.fetch;
  window.fetch = async function (url, options = {}) {
    // 解析 URL
    let path = url;
    let query = {};
    try {
      const u = new URL(url, window.location.origin);
      path = u.pathname;
      // 去掉 apiPrefix 前缀
      path = path.replace(/^\/api\/v1/, '');
      u.searchParams.forEach((v, k) => { query[k] = v; });
    } catch (e) {
      // 相对路径
      const qIdx = path.indexOf('?');
      if (qIdx >= 0) {
        const qs = path.slice(qIdx + 1);
        path = path.slice(0, qIdx);
        qs.split('&').forEach(kv => {
          const [k, v] = kv.split('=');
          if (k) query[decodeURIComponent(k)] = decodeURIComponent(v || '');
        });
      }
      path = path.replace(/^\/api\/v1/, '');
    }

    const method = (options.method || 'GET').toUpperCase();

    // 解析 body
    let body = null;
    if (options.body && !options.body instanceof FormData) {
      try { body = JSON.parse(options.body); } catch (e) { body = null; }
    }

    // SSE 特殊处理：/chat/completions
    if (path === '/chat/completions' && method === 'POST') {
      // 创建模拟 SSE 流
      const text = body && body.messages ? '这是模拟的流式回复内容，逐字输出以模拟真实的 SSE 流式效果。' : '回复内容';
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        start(controller) {
          let i = 0;
          const interval = setInterval(() => {
            if (i < text.length) {
              const chunk = JSON.stringify({ token: text[i], type: 'token' });
              controller.enqueue(encoder.encode('data: ' + chunk + '\n\n'));
              i++;
            } else {
              controller.enqueue(encoder.encode('data: [DONE]\n\n'));
              controller.close();
              clearInterval(interval);
            }
          }, 30);
        }
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }

    // 匹配路由
    for (const r of routes) {
      if (r.method !== method) continue;
      const params = matchPath(r.pattern, path);
      if (params) {
        try {
          const result = await r.handler({ ...query, ...params }, body, options);
          // 模拟网络延迟
          await new Promise(resolve => setTimeout(resolve, 50 + Math.random() * 150));
          return new Response(JSON.stringify(result), {
            status: 200,
            headers: { 'Content-Type': 'application/json' }
          });
        } catch (e) {
          return new Response(JSON.stringify({ code: 500, message: e.message }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' }
          });
        }
      }
    }

    // 未匹配的路由，返回空成功
    console.warn('[Mock] 未匹配路由:', method, path);
    return new Response(JSON.stringify({ code: 0, message: 'ok', data: path.includes('list') ? { items: [], total: 0 } : null }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  };

  // ============ 模拟 WebSocket ============
  const OriginalWS = window.WebSocket;
  window.WebSocket = function (url) {
    console.log('[Mock] WebSocket 连接（模拟）:', url);
    const ws = {
      url: url,
      readyState: 0,
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      send: function (data) { console.log('[Mock] WebSocket send:', data); },
      close: function () { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000, reason: 'normal' }); }
    };
    // 模拟异步连接成功
    setTimeout(() => {
      ws.readyState = 1;
      if (ws.onopen) ws.onopen({ type: 'open' });
    }, 100);
    return ws;
  };
  window.WebSocket.CONNECTING = 0;
  window.WebSocket.OPEN = 1;
  window.WebSocket.CLOSING = 2;
  window.WebSocket.CLOSED = 3;

  console.log('%c[OmniSpace Mock] 纯前端模拟层已加载，所有 API 请求将返回模拟数据', 'color: #e11d48; font-weight: bold;');
})();
