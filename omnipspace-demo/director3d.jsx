/* ============================================================
 * OmniSpace AI · 3D 导演台（文档 2.1 §4.2）
 * Three.js 全屏导演台：场景/机位/灯光/姿态/材质/输出 6 Tab
 * 多机位 · 截图输出 · ControlNet 条件（深度图/骨骼 3D 优先）· TripoSR
 * 画质自适应：FPS 上报 /director/quality/fps，应用三档质量预设
 * 依赖 window.THREE（libs/three.min.js）+ window.Omni（app.jsx）
 * ============================================================ */
(function () {
  const { useState, useEffect, useRef, useCallback, useMemo } = React;
  const O = window.Omni || {};
  const api = O.api, toast = O.toast, Icon = O.Icon, Spinner = O.Spinner;
  const THREE = window.THREE;

  /* ---------------- 预设 ---------------- */
  const LIGHT_PRESETS = {
    natural:     { label: "自然光",   key: { color: "#fff4e0", intensity: 1.0, pos: [4, 6, 3] },  fill: { color: "#cfe4ff", intensity: 0.35, pos: [-4, 3, 2] }, rim: { color: "#ffffff", intensity: 0.2, pos: [0, 4, -5] }, ambient: 0.45 },
    three_point: { label: "三点布光", key: { color: "#ffffff", intensity: 1.1, pos: [3, 5, 4] },  fill: { color: "#e8f0ff", intensity: 0.5, pos: [-4, 2.5, 3] }, rim: { color: "#fff2d9", intensity: 0.6, pos: [-1, 5, -5] }, ambient: 0.3 },
    rembrandt:   { label: "伦勃朗光", key: { color: "#ffe9c4", intensity: 1.2, pos: [2.5, 4, 2] }, fill: { color: "#b8c8e8", intensity: 0.18, pos: [-3, 1.5, 2] }, rim: { color: "#ffffff", intensity: 0.3, pos: [0, 3, -4] }, ambient: 0.22 },
    backlight:   { label: "逆光剪影", key: { color: "#ffffff", intensity: 0.25, pos: [0, 3, 4] }, fill: { color: "#dfe8ff", intensity: 0.1, pos: [2, 2, 3] }, rim: { color: "#fff8ee", intensity: 1.3, pos: [0, 4, -5] }, ambient: 0.18 },
    neon:        { label: "霓虹氛围", key: { color: "#ff4fa3", intensity: 0.9, pos: [3, 3, 2] },  fill: { color: "#3fd4ff", intensity: 0.7, pos: [-3, 2, 2] }, rim: { color: "#8a5cff", intensity: 0.8, pos: [0, 4, -4] }, ambient: 0.25 },
  };
  const MOOD_TINT = { normal: null, warm: "#ffb46b", cold: "#6bb4ff", dark: "#3a3f5c", dreamy: "#d9a6ff" };

  // 参数化骨架：17 关节（ControlNet pose 输出 + 视口骨架线）
  const JOINTS = ["head", "neck", "shoulder_l", "shoulder_r", "elbow_l", "elbow_r",
    "hand_l", "hand_r", "hip_center", "hip_l", "hip_r", "knee_l", "knee_r",
    "foot_l", "foot_r", "spine", "chest"];
  const BONES = [["head", "neck"], ["neck", "chest"], ["chest", "spine"], ["spine", "hip_center"],
    ["neck", "shoulder_l"], ["shoulder_l", "elbow_l"], ["elbow_l", "hand_l"],
    ["neck", "shoulder_r"], ["shoulder_r", "elbow_r"], ["elbow_r", "hand_r"],
    ["hip_center", "hip_l"], ["hip_l", "knee_l"], ["knee_l", "foot_l"],
    ["hip_center", "hip_r"], ["hip_r", "knee_r"], ["knee_r", "foot_r"]];
  const POSE_PRESETS = {
    stand: { label: "站立", joints: { head: [0, 1.72, 0], neck: [0, 1.5, 0], chest: [0, 1.32, 0], spine: [0, 1.1, 0], hip_center: [0, 0.95, 0], shoulder_l: [-0.22, 1.46, 0], shoulder_r: [0.22, 1.46, 0], elbow_l: [-0.3, 1.18, 0.02], elbow_r: [0.3, 1.18, 0.02], hand_l: [-0.34, 0.92, 0.04], hand_r: [0.34, 0.92, 0.04], hip_l: [-0.11, 0.92, 0], hip_r: [0.11, 0.92, 0], knee_l: [-0.12, 0.5, 0.01], knee_r: [0.12, 0.5, 0.01], foot_l: [-0.13, 0.03, 0.05], foot_r: [0.13, 0.03, 0.05] } },
    t_pose: { label: "T-Pose", joints: { head: [0, 1.72, 0], neck: [0, 1.5, 0], chest: [0, 1.32, 0], spine: [0, 1.1, 0], hip_center: [0, 0.95, 0], shoulder_l: [-0.22, 1.46, 0], shoulder_r: [0.22, 1.46, 0], elbow_l: [-0.5, 1.45, 0], elbow_r: [0.5, 1.45, 0], hand_l: [-0.8, 1.44, 0], hand_r: [0.8, 1.44, 0], hip_l: [-0.11, 0.92, 0], hip_r: [0.11, 0.92, 0], knee_l: [-0.12, 0.5, 0.01], knee_r: [0.12, 0.5, 0.01], foot_l: [-0.13, 0.03, 0.05], foot_r: [0.13, 0.03, 0.05] } },
    sit: { label: "坐姿", joints: { head: [0, 1.32, 0], neck: [0, 1.12, 0], chest: [0, 0.96, 0], spine: [0, 0.78, 0], hip_center: [0, 0.62, 0], shoulder_l: [-0.22, 1.08, 0], shoulder_r: [0.22, 1.08, 0], elbow_l: [-0.3, 0.86, 0.1], elbow_r: [0.3, 0.86, 0.1], hand_l: [-0.32, 0.66, 0.2], hand_r: [0.32, 0.66, 0.2], hip_l: [-0.11, 0.6, 0.05], hip_r: [0.11, 0.6, 0.05], knee_l: [-0.12, 0.55, 0.45], knee_r: [0.12, 0.55, 0.45], foot_l: [-0.13, 0.03, 0.5], foot_r: [0.13, 0.03, 0.5] } },
    walk: { label: "行走", joints: { head: [0, 1.7, 0.02], neck: [0, 1.48, 0.02], chest: [0, 1.3, 0.02], spine: [0, 1.08, 0.01], hip_center: [0, 0.93, 0], shoulder_l: [-0.22, 1.44, 0], shoulder_r: [0.22, 1.44, 0.04], elbow_l: [-0.28, 1.2, 0.14], elbow_r: [0.3, 1.16, -0.08], hand_l: [-0.3, 0.96, 0.24], hand_r: [0.34, 0.9, -0.14], hip_l: [-0.11, 0.9, 0.1], hip_r: [0.11, 0.9, -0.1], knee_l: [-0.12, 0.52, 0.28], knee_r: [0.12, 0.48, -0.22], foot_l: [-0.13, 0.06, 0.42], foot_r: [0.13, 0.22, -0.3] } },
  };

  const PRIMITIVES = [
    { type: "box", label: "立方体" }, { type: "sphere", label: "球体" },
    { type: "cylinder", label: "圆柱" }, { type: "plane", label: "平面" },
    { type: "character", label: "角色（胶囊）" },
  ];
  const TABS = [
    { id: "scene", label: "场景", icon: "Boxes" },
    { id: "camera", label: "机位", icon: "Camera" },
    { id: "light", label: "灯光", icon: "Lightbulb" },
    { id: "pose", label: "姿态", icon: "PersonStanding" },
    { id: "material", label: "材质", icon: "Palette" },
    { id: "output", label: "输出", icon: "ImageDown" },
  ];
  const MOTION_TYPES = [
    { id: "static", label: "固定" }, { id: "push", label: "推" }, { id: "pull", label: "拉" },
    { id: "pan", label: "摇" }, { id: "truck", label: "移" }, { id: "follow", label: "跟" },
  ];

  let _uid = 1;
  const uid = (p) => `${p}_${Date.now().toString(36)}_${(_uid++).toString(36)}`;

  /* ============================================================
   * StageEngine：Three.js 场景封装（轨道控制/多机位/深度图/骨架投影）
   * ============================================================ */
  class StageEngine {
    constructor(canvas) {
      this.canvas = canvas;
      this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
      this.renderer.shadowMap.enabled = true;
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color("#171a22");
      this.grid = new THREE.GridHelper(20, 20, 0x3a4152, 0x262b38);
      this.scene.add(this.grid);
      this.scene.add(new THREE.AxesHelper(0.6));

      // 编辑器自由视角（非出图机位）
      this.editCam = new THREE.PerspectiveCamera(45, 1, 0.1, 200);
      this.orbit = { theta: 0.7, phi: 1.05, radius: 7, target: new THREE.Vector3(0, 1, 0) };
      this._applyOrbit();

      // 灯光组
      this.lights = {};
      this.lights.key = new THREE.DirectionalLight("#fff4e0", 1.0);
      this.lights.key.position.set(4, 6, 3);
      this.lights.key.castShadow = true;
      this.lights.fill = new THREE.DirectionalLight("#cfe4ff", 0.35);
      this.lights.fill.position.set(-4, 3, 2);
      this.lights.rim = new THREE.DirectionalLight("#ffffff", 0.2);
      this.lights.rim.position.set(0, 4, -5);
      this.lights.ambient = new THREE.AmbientLight("#ffffff", 0.45);
      Object.values(this.lights).forEach((l) => this.scene.add(l));

      this.objects = new Map();      // id → THREE.Object3D（userData.meta 存序列化）
      this.cameras = [];             // {id,name,position,target,fov}
      this.activeCamera = "main";
      this.skelLine = null;          // 骨架线（姿态 Tab）
      this.skelObjectId = "";        // 骨架跟随对象
      this.poseName = "stand";
      this._previewRenderer = null;  // 惰性离屏预览渲染器
      this._bindOrbit();
      this.resize();
    }

    /* ---------------- 基础 ---------------- */
    resize() {
      const el = this.canvas.parentElement;
      if (!el) return;
      const w = el.clientWidth || 640, h = el.clientHeight || 360;
      this.renderer.setSize(w, h, false);
      this.editCam.aspect = w / h;
      this.editCam.updateProjectionMatrix();
      this.render();
    }

    render(cam) {
      const c = cam || this._activeCamObj() || this.editCam;
      this.renderer.render(this.scene, c);
    }

    dispose() {
      this._unbindOrbit();
      this.objects.forEach((o) => this._disposeObj(o));
      if (this._previewRenderer) this._previewRenderer.dispose();
      this.renderer.dispose();
    }

    _disposeObj(o) {
      o.traverse((n) => {
        if (n.geometry) n.geometry.dispose();
        if (n.material) (Array.isArray(n.material) ? n.material : [n.material]).forEach((m) => m.dispose());
      });
    }

    /* ---------------- 轨道控制（无 OrbitControls 依赖） ---------------- */
    _applyOrbit() {
      const { theta, phi, radius, target } = this.orbit;
      const p = Math.max(0.05, Math.min(Math.PI - 0.05, phi));
      this.editCam.position.set(
        target.x + radius * Math.sin(p) * Math.sin(theta),
        target.y + radius * Math.cos(p),
        target.z + radius * Math.sin(p) * Math.cos(theta));
      this.editCam.lookAt(target);
    }

    _bindOrbit() {
      const cv = this.canvas;
      let drag = null;
      this._onDown = (e) => {
        drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 };
        cv.setPointerCapture && cv.setPointerCapture(e.pointerId);
      };
      this._onMove = (e) => {
        if (!drag) return;
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.x = e.clientX; drag.y = e.clientY;
        if (drag.pan) {
          const s = this.orbit.radius * 0.0016;
          const right = new THREE.Vector3().setFromMatrixColumn(this.editCam.matrix, 0);
          const up = new THREE.Vector3().setFromMatrixColumn(this.editCam.matrix, 1);
          this.orbit.target.addScaledVector(right, -dx * s).addScaledVector(up, dy * s);
        } else {
          this.orbit.theta -= dx * 0.008;
          this.orbit.phi -= dy * 0.008;
        }
        this._applyOrbit(); this.render();
      };
      this._onUp = () => { drag = null; };
      this._onWheel = (e) => {
        e.preventDefault();
        this.orbit.radius = Math.max(1.2, Math.min(60, this.orbit.radius * (1 + Math.sign(e.deltaY) * 0.1)));
        this._applyOrbit(); this.render();
      };
      this._onCtx = (e) => e.preventDefault();
      cv.addEventListener("pointerdown", this._onDown);
      cv.addEventListener("pointermove", this._onMove);
      cv.addEventListener("pointerup", this._onUp);
      cv.addEventListener("wheel", this._onWheel, { passive: false });
      cv.addEventListener("contextmenu", this._onCtx);
    }

    _unbindOrbit() {
      const cv = this.canvas;
      cv.removeEventListener("pointerdown", this._onDown);
      cv.removeEventListener("pointermove", this._onMove);
      cv.removeEventListener("pointerup", this._onUp);
      cv.removeEventListener("wheel", this._onWheel);
      cv.removeEventListener("contextmenu", this._onCtx);
    }

    /* ---------------- 场景对象 ---------------- */
    addPrimitive(type, name) {
      let mesh, meta;
      const mat = new THREE.MeshStandardMaterial({ color: "#8b93a8", roughness: 0.7, metalness: 0.1 });
      if (type === "box") mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), mat);
      else if (type === "sphere") mesh = new THREE.Mesh(new THREE.SphereGeometry(0.5, 24, 16), mat);
      else if (type === "cylinder") mesh = new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.4, 1.2, 20), mat);
      else if (type === "plane") mesh = new THREE.Mesh(new THREE.PlaneGeometry(3, 2), new THREE.MeshStandardMaterial({ color: "#7d8598", side: THREE.DoubleSide }));
      else { // character 胶囊占位
        mesh = new THREE.Mesh(new THREE.CapsuleGeometry(0.28, 0.9, 6, 12),
          new THREE.MeshStandardMaterial({ color: "#c98a9b", roughness: 0.6 }));
        mesh.position.y = 0.9;
      }
      if (type !== "character") mesh.position.y = 0.5;
      mesh.castShadow = mesh.receiveShadow = true;
      meta = { id: uid("obj"), type, name: name || type,
        position: mesh.position.toArray(), rotation: [mesh.rotation.x, mesh.rotation.y, mesh.rotation.z],
        scale: mesh.scale.toArray(),
        material: { color: "#" + mesh.material.color.getHexString(), roughness: mesh.material.roughness, metalness: mesh.material.metalness, opacity: 1, wireframe: false } };
      mesh.userData.meta = meta;
      this.scene.add(mesh);
      this.objects.set(meta.id, mesh);
      this.render();
      return meta;
    }

    addOBJ(url, name) {
      const meta = { id: uid("obj"), type: "obj", name: name || "模型", url,
        position: [0, 0, 0], rotation: [0, 0, 0], scale: [1, 1, 1],
        material: { color: "#9aa3b8", roughness: 0.65, metalness: 0.05, opacity: 1, wireframe: false } };
      return fetch(url).then((r) => {
        if (!r.ok) throw new Error("模型下载失败 " + r.status);
        return r.text();
      }).then((text) => {
        const geo = parseOBJ(text);
        const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
          color: meta.material.color, roughness: meta.material.roughness, metalness: meta.material.metalness }));
        // 归一化到 1.6m 高并落地
        geo.computeBoundingBox();
        const bb = geo.boundingBox, size = new THREE.Vector3(); bb.getSize(size);
        const s = size.y > 0 ? 1.6 / size.y : 1;
        mesh.scale.setScalar(s);
        const center = new THREE.Vector3(); bb.getCenter(center);
        mesh.position.set(-center.x * s, -bb.min.y * s, -center.z * s);
        // 用 Group 包裹，让变换基准在脚底中心
        const g = new THREE.Group();
        g.add(mesh); g.castShadow = true;
        mesh.castShadow = mesh.receiveShadow = true;
        g.userData.meta = meta;
        this.scene.add(g);
        this.objects.set(meta.id, g);
        this.render();
        return meta;
      });
    }

    removeObject(id) {
      const o = this.objects.get(id);
      if (!o) return;
      this.scene.remove(o);
      this._disposeObj(o);
      this.objects.delete(id);
      this.render();
    }

    setTransform(id, key, idx, v) {
      const o = this.objects.get(id);
      if (!o) return;
      o[key][["x", "y", "z"][idx]] = v;
      o.userData.meta[key] = o[key].toArray ? o[key].toArray() : [o.rotation.x, o.rotation.y, o.rotation.z];
      if (key === "rotation") o.userData.meta.rotation = [o.rotation.x, o.rotation.y, o.rotation.z];
      this.render();
    }

    applyMaterial(id, patch) {
      const o = this.objects.get(id);
      if (!o) return;
      Object.assign(o.userData.meta.material, patch);
      o.traverse((n) => {
        if (!n.material) return;
        const m = n.material;
        if (patch.color) m.color.set(patch.color);
        if (patch.roughness != null) m.roughness = patch.roughness;
        if (patch.metalness != null) m.metalness = patch.metalness;
        if (patch.opacity != null) { m.opacity = patch.opacity; m.transparent = patch.opacity < 1; }
        if (patch.wireframe != null) m.wireframe = patch.wireframe;
      });
      this.render();
    }

    /* ---------------- 灯光 ---------------- */
    applyLighting(cfg) {
      const p = LIGHT_PRESETS[cfg.preset] || LIGHT_PRESETS.natural;
      const merged = { ...p, ...(cfg.overrides || {}) };
      for (const k of ["key", "fill", "rim"]) {
        const src = (cfg.overrides && cfg.overrides[k]) || p[k];
        this.lights[k].color.set(src.color);
        this.lights[k].intensity = src.intensity * (cfg.intensity != null ? cfg.intensity : 1);
        this.lights[k].position.set(...src.pos);
      }
      this.lights.ambient.intensity = (cfg.ambient != null ? cfg.ambient : p.ambient);
      const tint = MOOD_TINT[cfg.mood || "normal"];
      this.scene.background = new THREE.Color(tint ? shade("#171a22", tint, 0.35) : "#171a22");
      this.render();
    }

    /* ---------------- 多机位 ---------------- */
    addCamera(name) {
      const idx = this.cameras.length + 1;
      const cam = { id: uid("cam"), name: name || `机位 ${idx}`,
        position: this.editCam.position.toArray(),
        target: this.orbit.target.toArray(), fov: 35 };
      this.cameras.push(cam);
      return cam;
    }

    removeCamera(id) {
      this.cameras = this.cameras.filter((c) => c.id !== id);
      if (this.activeCamera === id) this.activeCamera = this.cameras[0] ? this.cameras[0].id : "";
    }

    updateCamera(id, patch) {
      const c = this.cameras.find((x) => x.id === id);
      if (c) Object.assign(c, patch);
    }

    _activeCamObj() {
      const c = this.cameras.find((x) => x.id === this.activeCamera);
      if (!c) return null;
      const cam = new THREE.PerspectiveCamera(c.fov || 35, this.editCam.aspect, 0.1, 200);
      cam.position.set(...c.position);
      cam.lookAt(new THREE.Vector3(...c.target));
      return cam;
    }

    frameCamera(id) {
      const c = this.cameras.find((x) => x.id === id);
      if (!c) return;
      this.activeCamera = id;
      // 编辑视角同步到该机位取景
      const pos = new THREE.Vector3(...c.position), tgt = new THREE.Vector3(...c.target);
      const off = pos.clone().sub(tgt);
      this.orbit.radius = off.length();
      this.orbit.target.copy(tgt);
      this.orbit.theta = Math.atan2(off.x, off.z);
      this.orbit.phi = Math.acos(Math.max(-1, Math.min(1, off.y / (off.length() || 1))));
      this._applyOrbit(); this.render();
    }

    cameraPreview(cam) {
      /* 离屏渲染单张机位预览（preview_w/h 由质量档位给） */
      const w = this._previewW || 160, h = this._previewH || 90;
      if (!this._previewRenderer) {
        this._previewRenderer = new THREE.WebGLRenderer({ antialias: false });
        this._previewRenderer.setSize(w, h);
      }
      const c = new THREE.PerspectiveCamera(cam.fov || 35, w / h, 0.1, 200);
      c.position.set(...cam.position);
      c.lookAt(new THREE.Vector3(...cam.target));
      this._previewRenderer.render(this.scene, c);
      return this._previewRenderer.domElement.toDataURL("image/jpeg", 0.7);
    }

    /* ---------------- 画质档位 ---------------- */
    setQuality(q) {
      if (!q) return;
      this._previewW = q.preview_w || 160; this._previewH = q.preview_h || 90;
      this.renderer.setPixelRatio(Math.max(0.4, (window.devicePixelRatio || 1) * (q.render_scale != null ? q.render_scale : 1)));
      this.renderer.shadowMap.enabled = !!q.shadows;
      this.lights.key.castShadow = !!q.shadows;
      this.scene.fog = q.fog ? new THREE.Fog("#171a22", 12, 40) : null;
      this.resize();
    }

    /* ---------------- 骨架（姿态 Tab + ControlNet pose） ---------------- */
    showSkeleton(objectId, poseName) {
      this.hideSkeleton();
      this.skelObjectId = objectId || "";
      this.poseName = poseName || "stand";
      const mat = new THREE.LineBasicMaterial({ color: "#5cff9d" });
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(BONES.length * 6), 3));
      this.skelLine = new THREE.LineSegments(geo, mat);
      this.skelLine.frustumCulled = false;
      this.scene.add(this.skelLine);
      this._updateSkeleton();
    }

    hideSkeleton() {
      if (this.skelLine) { this.scene.remove(this.skelLine); this.skelLine.geometry.dispose(); this.skelLine.material.dispose(); this.skelLine = null; }
      this.render();
    }

    _skelOrigin() {
      const o = this.skelObjectId && this.objects.get(this.skelObjectId);
      return o ? o.position : new THREE.Vector3(0, 0, 0);
    }

    _updateSkeleton() {
      if (!this.skelLine) return;
      const origin = this._skelOrigin();
      const joints = (POSE_PRESETS[this.poseName] || POSE_PRESETS.stand).joints;
      const pos = this.skelLine.geometry.attributes.position.array;
      let i = 0;
      for (const [a, b] of BONES) {
        const ja = joints[a], jb = joints[b];
        pos[i++] = origin.x + ja[0]; pos[i++] = origin.y + ja[1]; pos[i++] = origin.z + ja[2];
        pos[i++] = origin.x + jb[0]; pos[i++] = origin.y + jb[1]; pos[i++] = origin.z + jb[2];
      }
      this.skelLine.geometry.attributes.position.needsUpdate = true;
      this.render();
    }

    skeletonKeypoints() {
      /* 17 关节经当前出图机位投影 → 归一化坐标（ControlNet pose 3D 优先路径） */
      const cam = this._activeCamObj() || this.editCam;
      const origin = this._skelOrigin();
      const joints = (POSE_PRESETS[this.poseName] || POSE_PRESETS.stand).joints;
      const out = {};
      for (const name of JOINTS) {
        const j = joints[name] || [0, 0, 0];
        const v = new THREE.Vector3(origin.x + j[0], origin.y + j[1], origin.z + j[2]).project(cam);
        out[name] = { x: Math.round(((v.x + 1) / 2) * 1000) / 1000, y: Math.round(((1 - v.y) / 2) * 1000) / 1000, visible: v.z < 1 };
      }
      return { keypoints: out, bones: BONES, pose: this.poseName };
    }

    /* ---------------- 出图 ---------------- */
    screenshot(format, quality) {
      const fmt = format === "jpeg" ? "image/jpeg" : "image/png";
      this.render();
      return this.canvas.toDataURL(fmt, quality == null ? 0.92 : quality);
    }

    depthDataURL() {
      /* 场景深度：overrideMaterial 渲染灰度深度图（ControlNet depth 3D 优先路径） */
      const prev = this.scene.overrideMaterial;
      const prevSkel = this.skelLine; if (prevSkel) prevSkel.visible = false;
      this.scene.overrideMaterial = new THREE.MeshDepthMaterial();
      this.render();
      const url = this.canvas.toDataURL("image/png");
      this.scene.overrideMaterial = prev;
      if (prevSkel) prevSkel.visible = true;
      this.render();
      return url;
    }

    /* ---------------- 序列化 ---------------- */
    serialize() {
      const objects = [];
      this.objects.forEach((o) => objects.push(o.userData.meta));
      return objects;
    }

    loadObjects(list) {
      (list || []).forEach((meta) => {
        if (meta.type === "obj" && meta.url) {
          this.addOBJ(meta.url, meta.name).then((m) => {
            m.id = meta.id;
            const g = this.objects.get(m.id);
          }).catch(() => {});
        } else {
          const m = this.addPrimitive(meta.type, meta.name);
          // 覆写为新 id 后还原原 id/变换/材质
          const mesh = this.objects.get(m.id);
          this.objects.delete(m.id);
          mesh.userData.meta = JSON.parse(JSON.stringify(meta));
          this.objects.set(meta.id, mesh);
          mesh.position.set(...meta.position);
          mesh.rotation.set(...(meta.rotation || [0, 0, 0]));
          mesh.scale.set(...(meta.scale || [1, 1, 1]));
          this.applyMaterial(meta.id, meta.material || {});
        }
      });
      this.render();
    }
  }

  /* ---------------- OBJ 极简解析（v/f，扇形三角化） ---------------- */
  function parseOBJ(text) {
    const verts = [], out = [];
    const lines = text.split("\n");
    for (const raw of lines) {
      const line = raw.trim();
      if (line.startsWith("v ")) {
        const p = line.slice(2).trim().split(/\s+/).map(Number);
        verts.push([p[0] || 0, p[1] || 0, p[2] || 0]);
      } else if (line.startsWith("f ")) {
        const idx = line.slice(2).trim().split(/\s+/)
          .map((t) => parseInt(t.split("/")[0], 10))
          .filter((n) => !isNaN(n))
          .map((n) => (n < 0 ? verts.length + n : n - 1));
        for (let i = 1; i + 1 < idx.length; i++) {
          for (const j of [idx[0], idx[i], idx[i + 1]]) {
            const v = verts[j] || [0, 0, 0];
            out.push(v[0], v[1], v[2]);
          }
        }
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(out), 3));
    geo.computeVertexNormals();
    return geo;
  }

  function shade(base, tint, k) {
    const b = new THREE.Color(base), t = new THREE.Color(tint);
    return "#" + b.lerp(t, k).getHexString();
  }

  /* ---------------- 默认状态 ---------------- */
  function defaultState() {
    return {
      scene: { objects: [], fog: false },
      cameras: [], active_camera: "",
      lighting: { preset: "natural", intensity: 1, mood: "normal", ambient: 0.45, overrides: {} },
      motion: { type: "static", duration: 3, easing: "linear" },
      skeleton_mapping: { character_object_id: "", preset: "stand" },
      controlnet_checkboxes: { canny: true, depth: true, pose: true },
    };
  }

  /* ============================================================
   * StageModal 全屏导演台
   * ============================================================ */
  function StageModal({ shot, open, onClose, onSaved }) {
    const canvasRef = useRef(null);
    const engRef = useRef(null);
    const [tab, setTab] = useState("scene");
    const [st, setSt] = useState(null);               // 导演台状态（与后端契约一致）
    const [objects, setObjects] = useState([]);       // 镜像：对象列表（UI 刷新用）
    const [sel, setSel] = useState("");               // 选中对象 id
    const [cameras, setCameras] = useState([]);
    const [activeCam, setActiveCam] = useState("");
    const [quality, setQuality] = useState(null);
    const [tier, setTier] = useState("medium");
    const [fps, setFps] = useState(0);
    const [saving, setSaving] = useState(false);
    const [previews, setPreviews] = useState({});
    const [models3d, setModels3d] = useState([]);
    const stateRef = useRef(null);
    stateRef.current = st;

    /* ---------- 初始化引擎 ---------- */
    useEffect(() => {
      if (!open || !canvasRef.current || !THREE) return undefined;
      const eng = new StageEngine(canvasRef.current);
      engRef.current = eng;
      const onResize = () => eng.resize();
      window.addEventListener("resize", onResize);
      return () => { window.removeEventListener("resize", onResize); eng.dispose(); engRef.current = null; };
    }, [open]);

    /* ---------- 加载会话状态 ---------- */
    useEffect(() => {
      if (!open || !engRef.current) return;
      let alive = true;
      (async () => {
        try {
          const d = await api(`/director/session/${shot.id}/state`);
          if (!alive || !engRef.current) return;
          const raw = d.state || {};
          const merged = { ...defaultState(), ...raw,
            scene: { ...defaultState().scene, ...(raw.scene || {}) },
            lighting: { ...defaultState().lighting, ...(raw.lighting || {}) },
            motion: { ...defaultState().motion, ...(raw.motion || {}) },
            skeleton_mapping: { ...defaultState().skeleton_mapping, ...(raw.skeleton_mapping || {}) },
            controlnet_checkboxes: { ...defaultState().controlnet_checkboxes, ...(raw.controlnet_checkboxes || {}) } };
          const eng = engRef.current;
          eng.loadObjects(merged.scene.objects);
          // TripoSR 自动导入的模型：若未在对象列表中则追加
          const imported = (raw.models3d || []);
          setModels3d(imported);
          const have = new Set((merged.scene.objects || []).map((o) => o.url));
          for (const m of imported) {
            if (m.url && !have.has(m.url)) {
              try {
                const meta = await eng.addOBJ(m.url, "TripoSR 模型");
                merged.scene.objects.push(meta);
              } catch (e) { /* 模型缺失不阻塞 */ }
            }
          }
          // 默认机位：无则创建
          eng.cameras = merged.cameras || [];
          if (!eng.cameras.length) {
            const c1 = eng.addCamera("主机位"); eng.cameras = [c1];
          }
          eng.activeCamera = merged.active_camera || eng.cameras[0].id;
          eng.applyLighting(merged.lighting);
          setSt(merged);
          setObjects(eng.serialize());
          setCameras([...eng.cameras]);
          setActiveCam(eng.activeCamera);
          if (merged.skeleton_mapping.preset && merged.skeleton_mapping.character_object_id) {
            eng.showSkeleton(merged.skeleton_mapping.character_object_id, merged.skeleton_mapping.preset);
          }
        } catch (err) { toast.err(err.message); }
      })();
      return () => { alive = false; };
    }, [open, shot.id]);

    /* ---------- 画质档位 ---------- */
    useEffect(() => {
      if (!open) return;
      let alive = true;
      api("/director/quality").then((d) => {
        if (!alive) return;
        setQuality(d.quality); setTier(d.tier);
        if (engRef.current) engRef.current.setQuality(d.quality);
      }).catch(() => {});
      return () => { alive = false; };
    }, [open]);

    /* ---------- FPS 采样上报（<25fps 持续 3s 后端自动降档） ---------- */
    useEffect(() => {
      if (!open) return undefined;
      let raf, last = performance.now(), frames = 0, acc = 0, lastReport = 0;
      const loop = (t) => {
        frames++; acc += t - last; last = t;
        if (acc >= 1000) {
          const f = Math.round(frames * 1000 / acc);
          setFps(f); frames = 0; acc = 0;
          if (t - lastReport > 3000) {
            lastReport = t;
            api("/director/quality/fps", { method: "POST", body: { fps: f } })
              .then((d) => {
                if (d.downgraded && engRef.current) {
                  setTier(d.tier); setQuality(d.quality);
                  engRef.current.setQuality(d.quality);
                  toast.warn(`帧率过低，画质已自动降为 ${d.tier}`);
                }
              }).catch(() => {});
          }
        }
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
      return () => cancelAnimationFrame(raf);
    }, [open]);

    if (!open) return null;

    /* ---------- 状态写回辅助 ---------- */
    const patch = (sec, val) => setSt((s) => ({ ...s, [sec]: val }));
    const syncObjects = () => { if (engRef.current) setObjects(engRef.current.serialize()); };
    const syncCameras = () => { if (engRef.current) { setCameras([...engRef.current.cameras]); setActiveCam(engRef.current.activeCamera); } };

    const save = async (silent) => {
      const eng = engRef.current, s = stateRef.current;
      if (!eng || !s) return;
      setSaving(true);
      try {
        const body = {
          scene: { ...s.scene, objects: eng.serialize() },
          cameras: eng.cameras, active_camera: eng.activeCamera,
          lighting: s.lighting, motion: s.motion,
          skeleton_mapping: s.skeleton_mapping,
          controlnet_checkboxes: s.controlnet_checkboxes,
        };
        await api(`/director/session/${shot.id}/state`, { method: "PUT", body });
        // 同步 2D 面板导演数据（机位/灯光/运镜摘要）
        const ac = eng.cameras.find((c) => c.id === eng.activeCamera) || {};
        await api(`/director/shot/${shot.id}`, { method: "PUT", body: {
          camera: { shot_size: "medium", angle: "eye", fov: ac.fov || 35, height: (ac.position || [0, 1.6, 0])[1] },
          lighting: { key: s.lighting.preset, intensity: s.lighting.intensity, mood: s.lighting.mood },
          motion: s.motion, notes: (s.notes || "") } }).catch(() => {});
        if (!silent) toast.ok("导演台状态已保存");
        O.Bus && O.Bus.emit("director-data-updated", { shot_id: shot.id });
        onSaved && onSaved();
      } catch (err) { toast.err(err.message); }
      finally { setSaving(false); }
    };

    const eng = () => engRef.current;

    /* ---------- 渲染 ---------- */
    return (
      <div className="modal-mask" style={{ zIndex: 60 }} role="dialog" aria-modal="true" aria-label="3D 导演台">
        <div className="flex flex-col" style={{
          width: "96vw", height: "94vh", background: "var(--color-surface)",
          borderRadius: "var(--radius-lg)", overflow: "hidden",
          boxShadow: "var(--shadow-xl)", border: "1px solid var(--color-border)" }}>
          {/* 标题栏 */}
          <div className="flex items-center gap-3" style={{ padding: "var(--space-3) var(--space-4)", borderBottom: "1px solid var(--color-border)" }}>
            <Icon name="Clapperboard" size={18} />
            <b>3D 导演台</b>
            <span className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>镜 {shot.sort_index} · 会话 {shot.id.slice(0, 8)}</span>
            <span className="tag" title="实时帧率">{fps} fps</span>
            <span className={"tag " + (tier === "high" ? "tag-success" : tier === "medium" ? "tag-warning" : "tag-danger")} title="画质档位（GPU 统一管理器）">{tier}</span>
            <div style={{ marginLeft: "auto" }} className="flex items-center gap-2">
              <select className="select" style={{ width: 110 }} value={tier} aria-label="画质档位"
                onChange={(e) => {
                  const v = e.target.value;
                  api("/director/quality", { method: "PUT", body: { tier: v } }).then((d) => {
                    setTier(d.tier); setQuality(d.quality);
                    if (engRef.current) engRef.current.setQuality(d.quality);
                  }).catch((err) => toast.err(err.message));
                }}>
                <option value="high">高画质</option><option value="medium">均衡</option><option value="low">流畅</option>
              </select>
              <button className="btn btn-secondary" disabled={saving} onClick={() => save(false)}>
                {saving ? <Spinner size={14} /> : <Icon name="Save" size={15} />} 保存
              </button>
              <button className="btn btn-icon" onClick={() => { save(true); onClose(); }} aria-label="关闭"><Icon name="X" size={18} /></button>
            </div>
          </div>

          {/* 主区 */}
          <div className="flex flex-1" style={{ minHeight: 0 }}>
            {/* 视口 */}
            <div className="flex-1 flex flex-col" style={{ minWidth: 0, background: "#10131a" }}>
              <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
                <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block", touchAction: "none" }} />
                <div className="text-tertiary" style={{ position: "absolute", left: 10, bottom: 8, fontSize: 11, pointerEvents: "none" }}>
                  拖拽旋转 · Shift/右键平移 · 滚轮缩放
                </div>
              </div>
              {/* 机位条 */}
              <div className="flex items-center gap-2" style={{ padding: "var(--space-2) var(--space-3)", borderTop: "1px solid var(--color-border)", overflowX: "auto" }}>
                {cameras.map((c) => (
                  <button key={c.id} className={"tag " + (activeCam === c.id ? "tag-info" : "")}
                    style={{ cursor: "pointer", border: "none", whiteSpace: "nowrap" }}
                    onClick={() => { eng() && eng().frameCamera(c.id); syncCameras(); }}>
                    <Icon name="Video" size={12} /> {c.name}
                  </button>
                ))}
                <button className="tag" style={{ cursor: "pointer", border: "none" }}
                  onClick={() => { eng() && eng().addCamera(); syncCameras(); }}>
                  <Icon name="Plus" size={12} /> 机位
                </button>
              </div>
            </div>

            {/* Tab 面板 */}
            <aside className="flex flex-col" style={{ width: 380, flexShrink: 0, borderLeft: "1px solid var(--color-border)", minHeight: 0 }}>
              <div className="flex" style={{ borderBottom: "1px solid var(--color-border)" }}>
                {TABS.map((t) => (
                  <button key={t.id} onClick={() => setTab(t.id)} title={t.label}
                    className="flex-1 flex flex-col items-center gap-1"
                    style={{
                      padding: "8px 0 6px", border: "none", cursor: "pointer", fontSize: 11,
                      background: tab === t.id ? "var(--color-primary-soft)" : "transparent",
                      color: tab === t.id ? "var(--color-primary)" : "var(--color-text-tertiary)",
                      borderBottom: tab === t.id ? "2px solid var(--color-primary)" : "2px solid transparent",
                    }}>
                    <Icon name={t.icon} size={15} />{t.label}
                  </button>
                ))}
              </div>
              <div className="flex-1" style={{ overflowY: "auto", padding: "var(--space-4)", minHeight: 0 }}>
                {!st ? <div className="flex justify-center" style={{ padding: 40 }}><Spinner /></div> : (<>
                  {tab === "scene" && <SceneTab st={st} patch={patch} objects={objects} sel={sel} setSel={setSel} eng={eng} syncObjects={syncObjects} models3d={models3d} />}
                  {tab === "camera" && <CameraTab st={st} patch={patch} cameras={cameras} activeCam={activeCam} eng={eng} syncCameras={syncCameras} previews={previews} setPreviews={setPreviews} />}
                  {tab === "light" && <LightTab st={st} patch={patch} eng={eng} />}
                  {tab === "pose" && <PoseTab st={st} patch={patch} objects={objects} eng={eng} />}
                  {tab === "material" && <MaterialTab objects={objects} sel={sel} setSel={setSel} eng={eng} syncObjects={syncObjects} />}
                  {tab === "output" && <OutputTab shot={shot} st={st} patch={patch} cameras={cameras} eng={eng} />}
                </>)}
              </div>
            </aside>
          </div>
        </div>
      </div>
    );
  }

  /* ============================================================ Tab1 场景 */
  function SceneTab({ st, patch, objects, sel, setSel, eng, syncObjects, models3d }) {
    const selObj = objects.find((o) => o.id === sel);
    return (
      <div className="flex flex-col gap-4">
        <section>
          <h4 className="section-title"><Icon name="Plus" size={14} /> 添加对象</h4>
          <div className="flex flex-wrap gap-2">
            {PRIMITIVES.map((p) => (
              <button key={p.type} className="btn btn-ghost" style={{ padding: "4px 10px" }}
                onClick={() => { eng().addPrimitive(p.type, p.label); syncObjects(); }}>{p.label}</button>
            ))}
          </div>
          {models3d.length > 0 && (
            <div className="mt-2">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>TripoSR 已生成模型（{models3d.length}）已自动导入场景</span>
            </div>
          )}
        </section>

        <section>
          <h4 className="section-title"><Icon name="Boxes" size={14} /> 对象列表（{objects.length}）</h4>
          <div className="flex flex-col gap-1">
            {objects.length === 0 && <span className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>场景为空，从上方添加对象</span>}
            {objects.map((o) => (
              <div key={o.id} className="flex items-center gap-2">
                <button className={"flex-1 text-left " + (sel === o.id ? "tag tag-info" : "tag")}
                  style={{ cursor: "pointer", border: "none" }}
                  onClick={() => setSel(sel === o.id ? "" : o.id)}>
                  {o.name} <span className="text-tertiary" style={{ fontSize: 10 }}>{o.type}</span>
                </button>
                <button className="btn btn-icon" title="删除" onClick={() => { eng().removeObject(o.id); if (sel === o.id) setSel(""); syncObjects(); }}>
                  <Icon name="Trash2" size={13} />
                </button>
              </div>
            ))}
          </div>
        </section>

        {selObj && (
          <section>
            <h4 className="section-title"><Icon name="Move3d" size={14} /> 变换 · {selObj.name}</h4>
            {["position", "rotation", "scale"].map((key) => (
              <div key={key} className="mb-2">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>
                  {{ position: "位置 (m)", rotation: "旋转 (rad)", scale: "缩放" }[key]}
                </span>
                <div className="flex gap-2">
                  {[0, 1, 2].map((i) => (
                    <input key={i} type="number" step={key === "rotation" ? 0.1 : key === "scale" ? 0.1 : 0.1}
                      className="input" style={{ width: 0, flex: 1, padding: "4px 8px" }}
                      value={Number((selObj[key] || [0, 0, 0])[i]).toFixed(2)}
                      aria-label={`${key} ${"xyz"[i]}`}
                      onChange={(e) => {
                        const v = parseFloat(e.target.value);
                        if (isNaN(v)) return;
                        eng().setTransform(selObj.id, key, i, v);
                        syncObjects();
                      }} />
                  ))}
                </div>
              </div>
            ))}
          </section>
        )}

        <section>
          <h4 className="section-title"><Icon name="CloudFog" size={14} /> 环境</h4>
          <label className="flex items-center gap-2" style={{ cursor: "pointer" }}>
            <input type="checkbox" checked={!!st.scene.fog}
              onChange={(e) => patch("scene", { ...st.scene, fog: e.target.checked })} />
            <span style={{ fontSize: "var(--text-sm)" }}>雾效（高档画质下由渲染器应用）</span>
          </label>
        </section>
      </div>
    );
  }

  /* ============================================================ Tab2 机位 */
  function CameraTab({ st, patch, cameras, activeCam, eng, syncCameras, previews, setPreviews }) {
    const refreshPreviews = () => {
      const map = {};
      cameras.forEach((c) => { try { map[c.id] = eng().cameraPreview(c); } catch (e) {} });
      setPreviews(map);
    };
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h4 className="section-title" style={{ margin: 0 }}><Icon name="Camera" size={14} /> 多机位（{cameras.length}）</h4>
          <div className="flex gap-2">
            <button className="btn btn-ghost" style={{ padding: "4px 10px" }} onClick={refreshPreviews}>
              <Icon name="RefreshCw" size={13} /> 预览
            </button>
            <button className="btn btn-secondary" style={{ padding: "4px 10px" }}
              onClick={() => { eng().addCamera(); syncCameras(); }}>
              <Icon name="Plus" size={13} /> 添加
            </button>
          </div>
        </div>
        <div className="flex flex-col gap-3">
          {cameras.map((c) => (
            <div key={c.id} className="card" style={{ padding: "var(--space-3)", borderColor: activeCam === c.id ? "var(--color-primary)" : "var(--color-border)" }}>
              <div className="flex items-center gap-2 mb-2">
                <input className="input flex-1" style={{ padding: "3px 8px" }} value={c.name}
                  aria-label="机位名称"
                  onChange={(e) => { eng().updateCamera(c.id, { name: e.target.value }); syncCameras(); }} />
                {activeCam === c.id
                  ? <span className="tag tag-success">出图中</span>
                  : <button className="btn btn-ghost" style={{ padding: "3px 8px" }}
                      onClick={() => { eng().frameCamera(c.id); syncCameras(); }}>取景</button>}
                <button className="btn btn-icon" title="删除机位" disabled={cameras.length <= 1}
                  onClick={() => { eng().removeCamera(c.id); syncCameras(); }}>
                  <Icon name="Trash2" size={13} />
                </button>
              </div>
              {previews[c.id] && (
                <img src={previews[c.id]} alt={`${c.name} 预览`}
                  style={{ width: "100%", borderRadius: "var(--radius-sm)", marginBottom: 8, display: "block" }} />
              )}
              <div className="flex items-center gap-2">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)", width: 56 }}>FOV {c.fov}°</span>
                <input type="range" min={10} max={120} step={1} value={c.fov} style={{ flex: 1, accentColor: "var(--color-primary)" }}
                  aria-label="焦距"
                  onChange={(e) => { eng().updateCamera(c.id, { fov: parseInt(e.target.value, 10) }); if (activeCam === c.id) eng().render(); syncCameras(); }} />
              </div>
              <div className="text-tertiary" style={{ fontSize: 10, marginTop: 4 }}>
                位置 ({c.position.map((n) => n.toFixed(1)).join(", ")}) → 目标 ({c.target.map((n) => n.toFixed(1)).join(", ")})
              </div>
            </div>
          ))}
        </div>
        <section>
          <h4 className="section-title"><Icon name="Video" size={14} /> 运镜</h4>
          <div className="flex flex-wrap gap-2 mb-2">
            {MOTION_TYPES.map((m) => (
              <button key={m.id} className={"tag " + (st.motion.type === m.id ? "tag-info" : "")}
                style={{ cursor: "pointer", border: "none" }}
                onClick={() => patch("motion", { ...st.motion, type: m.id })}>{m.label}</button>
            ))}
          </div>
          {st.motion.type !== "static" && (
            <div className="flex items-center gap-2">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)", width: 64 }}>{st.motion.duration}s</span>
              <input type="range" min={1} max={10} step={0.5} value={st.motion.duration}
                style={{ flex: 1, accentColor: "var(--color-primary)" }} aria-label="运镜时长"
                onChange={(e) => patch("motion", { ...st.motion, duration: parseFloat(e.target.value) })} />
            </div>
          )}
        </section>
      </div>
    );
  }

  /* ============================================================ Tab3 灯光 */
  function LightTab({ st, patch, eng }) {
    const apply = (next) => { patch("lighting", next); eng().applyLighting(next); };
    return (
      <div className="flex flex-col gap-4">
        <section>
          <h4 className="section-title"><Icon name="Lightbulb" size={14} /> 布光预设</h4>
          <div className="flex flex-wrap gap-2">
            {Object.entries(LIGHT_PRESETS).map(([k, p]) => (
              <button key={k} className={"tag " + (st.lighting.preset === k ? "tag-info" : "")}
                style={{ cursor: "pointer", border: "none" }}
                onClick={() => apply({ ...st.lighting, preset: k })}>{p.label}</button>
            ))}
          </div>
        </section>
        <section>
          <h4 className="section-title"><Icon name="Sun" size={14} /> 参数</h4>
          {[
            { k: "intensity", label: "总强度", min: 0.1, max: 2, step: 0.05, fmt: (v) => Math.round(v * 100) + "%" },
            { k: "ambient", label: "环境光", min: 0, max: 1, step: 0.05, fmt: (v) => Math.round(v * 100) + "%" },
          ].map((s) => (
            <div key={s.k} className="mb-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{s.label}</span>
                <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{s.fmt(st.lighting[s.k])}</span>
              </div>
              <input type="range" min={s.min} max={s.max} step={s.step} value={st.lighting[s.k]}
                style={{ width: "100%", accentColor: "var(--color-primary)" }} aria-label={s.label}
                onChange={(e) => apply({ ...st.lighting, [s.k]: parseFloat(e.target.value) })} />
            </div>
          ))}
          <label className="flex flex-col gap-1">
            <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>情绪基调</span>
            <select className="select" value={st.lighting.mood}
              onChange={(e) => apply({ ...st.lighting, mood: e.target.value })}>
              <option value="normal">常规</option><option value="warm">温暖</option>
              <option value="cold">冷峻</option><option value="dark">阴暗</option>
              <option value="dreamy">梦幻</option>
            </select>
          </label>
        </section>
        <section>
          <h4 className="section-title"><Icon name="Lamp" size={14} /> 单灯微调</h4>
          {["key", "fill", "rim"].map((k) => {
            const cur = (st.lighting.overrides && st.lighting.overrides[k]) || LIGHT_PRESETS[st.lighting.preset][k];
            return (
              <div key={k} className="flex items-center gap-2 mb-2">
                <span className="text-tertiary" style={{ fontSize: "var(--text-xs)", width: 48 }}>
                  {{ key: "主光", fill: "辅光", rim: "轮廓" }[k]}
                </span>
                <input type="color" value={cur.color} aria-label={`${k} 颜色`}
                  onChange={(e) => {
                    const ov = { ...(st.lighting.overrides || {}) };
                    ov[k] = { ...cur, color: e.target.value };
                    apply({ ...st.lighting, overrides: ov });
                  }} />
                <input type="range" min={0} max={2} step={0.05} value={cur.intensity}
                  style={{ flex: 1, accentColor: "var(--color-primary)" }} aria-label={`${k} 强度`}
                  onChange={(e) => {
                    const ov = { ...(st.lighting.overrides || {}) };
                    ov[k] = { ...cur, intensity: parseFloat(e.target.value) };
                    apply({ ...st.lighting, overrides: ov });
                  }} />
                <span className="text-mono text-tertiary" style={{ fontSize: 10, width: 32 }}>{cur.intensity.toFixed(2)}</span>
              </div>
            );
          })}
        </section>
      </div>
    );
  }

  /* ============================================================ Tab4 姿态 */
  function PoseTab({ st, patch, objects, eng }) {
    const sm = st.skeleton_mapping;
    const apply = (next) => {
      patch("skeleton_mapping", next);
      if (next.character_object_id) eng().showSkeleton(next.character_object_id, next.preset);
      else eng().hideSkeleton();
    };
    return (
      <div className="flex flex-col gap-4">
        <section>
          <h4 className="section-title"><Icon name="PersonStanding" size={14} /> 骨骼映射</h4>
          <label className="flex flex-col gap-1 mb-3">
            <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>绑定角色对象（骨架线跟随其位置）</span>
            <select className="select" value={sm.character_object_id}
              onChange={(e) => apply({ ...sm, character_object_id: e.target.value })}>
              <option value="">未绑定（原点显示）</option>
              {objects.map((o) => <option key={o.id} value={o.id}>{o.name}（{o.type}）</option>)}
            </select>
          </label>
          <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>姿态预设（17 关节参数化骨架）</span>
          <div className="flex flex-wrap gap-2 mt-2">
            {Object.entries(POSE_PRESETS).map(([k, p]) => (
              <button key={k} className={"tag " + (sm.preset === k ? "tag-info" : "")}
                style={{ cursor: "pointer", border: "none" }}
                onClick={() => apply({ ...sm, preset: k })}>{p.label}</button>
            ))}
          </div>
        </section>
        <section>
          <h4 className="section-title"><Icon name="Info" size={14} /> 说明</h4>
          <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", lineHeight: 1.7, margin: 0 }}>
            骨架线（绿色）实时显示在 3D 视口中；17 关节坐标经当前出图机位投影后，
            作为 ControlNet pose 条件的 3D 优先数据源（输出 Tab → 提取 ControlNet）。
            未绑定角色时骨架显示在原点。
          </p>
        </section>
        <section>
          <h4 className="section-title"><Icon name="ListTree" size={14} /> 关节清单</h4>
          <div className="flex flex-wrap gap-1">
            {JOINTS.map((j) => <span key={j} className="tag" style={{ fontSize: 10 }}>{j}</span>)}
          </div>
        </section>
      </div>
    );
  }

  /* ============================================================ Tab5 材质 */
  function MaterialTab({ objects, sel, setSel, eng, syncObjects }) {
    const selObj = objects.find((o) => o.id === sel);
    const mat = selObj ? selObj.material : null;
    return (
      <div className="flex flex-col gap-4">
        <section>
          <h4 className="section-title"><Icon name="Palette" size={14} /> 选择对象</h4>
          <div className="flex flex-wrap gap-2">
            {objects.map((o) => (
              <button key={o.id} className={"tag " + (sel === o.id ? "tag-info" : "")}
                style={{ cursor: "pointer", border: "none" }} onClick={() => setSel(o.id)}>{o.name}</button>
            ))}
            {!objects.length && <span className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>场景为空</span>}
          </div>
        </section>
        {mat && (
          <section>
            <h4 className="section-title"><Icon name="SlidersHorizontal" size={14} /> 材质 · {selObj.name}</h4>
            <label className="flex items-center gap-3 mb-3">
              <span className="text-tertiary" style={{ fontSize: "var(--text-xs)", width: 56 }}>基础色</span>
              <input type="color" value={mat.color} aria-label="基础色"
                onChange={(e) => { eng().applyMaterial(selObj.id, { color: e.target.value }); syncObjects(); }} />
              <span className="text-mono text-tertiary" style={{ fontSize: 10 }}>{mat.color}</span>
            </label>
            {[
              { k: "roughness", label: "粗糙度", min: 0, max: 1 },
              { k: "metalness", label: "金属度", min: 0, max: 1 },
              { k: "opacity", label: "不透明度", min: 0.05, max: 1 },
            ].map((s) => (
              <div key={s.k} className="mb-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{s.label}</span>
                  <span className="text-mono text-tertiary" style={{ fontSize: "var(--text-xs)" }}>{Number(mat[s.k]).toFixed(2)}</span>
                </div>
                <input type="range" min={s.min} max={s.max} step={0.01} value={mat[s.k]}
                  style={{ width: "100%", accentColor: "var(--color-primary)" }} aria-label={s.label}
                  onChange={(e) => { eng().applyMaterial(selObj.id, { [s.k]: parseFloat(e.target.value) }); syncObjects(); }} />
              </div>
            ))}
            <label className="flex items-center gap-2" style={{ cursor: "pointer" }}>
              <input type="checkbox" checked={!!mat.wireframe}
                onChange={(e) => { eng().applyMaterial(selObj.id, { wireframe: e.target.checked }); syncObjects(); }} />
              <span style={{ fontSize: "var(--text-sm)" }}>线框模式</span>
            </label>
          </section>
        )}
      </div>
    );
  }

  /* ============================================================ Tab6 输出 */
  function OutputTab({ shot, st, patch, cameras, eng }) {
    const [shots, setShots] = useState([]);
    const [busy, setBusy] = useState("");
    const [cn, setCn] = useState(null);
    const [triposr, setTriposr] = useState(null);
    const [fmt, setFmt] = useState("png");
    const fileRef = useRef(null);

    const loadShots = useCallback(async () => {
      try { setShots((await api(`/director/session/${shot.id}/screenshots`)).items || []); } catch (e) {}
    }, [shot.id]);
    useEffect(() => { loadShots(); }, [loadShots]);

    const cb = st.controlnet_checkboxes;
    const activeCam = cameras.find((c) => c.id === eng().activeCamera);

    const capture = async (camId) => {
      setBusy("shot");
      try {
        const cam = cameras.find((c) => c.id === camId);
        if (cam) eng().frameCamera(camId);
        const dataURL = eng().screenshot(fmt, 0.92);
        await api(`/director/session/${shot.id}/screenshot`, { method: "POST", body: {
          camera_id: camId || "main", image_base64: dataURL, format: fmt,
          quality: 0.92, width: 1920, height: 1080,
          note: cam ? cam.name : "自由视角" } });
        toast.ok("截图已保存");
        loadShots();
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    const extract = async () => {
      if (!cb.canny && !cb.depth && !cb.pose) { toast.warn("至少勾选一个维度"); return; }
      setBusy("cn");
      try {
        const body = { checkboxes: cb, image_base64: eng().screenshot("png") };
        if (cb.depth) body.depth_image_base64 = eng().depthDataURL();
        if (cb.pose) body.skeleton = eng().skeletonKeypoints();
        const d = await api(`/director/session/${shot.id}/controlnet`, { method: "POST", body, timeout: 60000 });
        setCn(d);
        toast.ok(`条件已提取（来源：${{ "3d_render": "3D 渲染", mixed: "混合", ai_fallback: "AI 降级" }[d.source] || d.source}）`);
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    const gen3d = async (file) => {
      if (!file) return;
      setBusy("triposr");
      try {
        const dataURL = await new Promise((res, rej) => {
          const r = new FileReader();
          r.onload = () => res(r.result); r.onerror = rej;
          r.readAsDataURL(file);
        });
        const d = await api(`/director/session/${shot.id}/triposr/generate`, { method: "POST",
          body: { image_base64: dataURL, auto_import: true } });
        if (d.available === false) { toast.warn("当前硬件 TripoSR 已锁定（<6GB 显存）"); return; }
        toast.info(`3D 生成中，预计 ${d.estimated_seconds}s…`);
        setTriposr({ task_id: d.task_id, state: "queued", progress: 0 });
      } catch (err) { toast.err(err.message); }
      finally { setBusy(""); }
    };

    // TripoSR 轮询
    useEffect(() => {
      if (!triposr || (triposr.state !== "queued" && triposr.state !== "running")) return undefined;
      const t = setInterval(async () => {
        try {
          const d = await api(`/director/session/${shot.id}/triposr/status?task_id=${triposr.task_id}`);
          const task = d.task;
          if (task) {
            setTriposr(task);
            if (task.state === "done") {
              toast.ok(task.stub ? "3D 模型已生成（占位降级）并导入场景" : "3D 模型已生成并自动导入场景");
              clearInterval(t);
            } else if (task.state === "failed") {
              toast.err("3D 生成失败：" + (task.error || ""));
              clearInterval(t);
            }
          }
        } catch (e) {}
      }, 2000);
      return () => clearInterval(t);
    }, [triposr && triposr.task_id, triposr && triposr.state]);

    return (
      <div className="flex flex-col gap-4">
        {/* 截图 */}
        <section>
          <h4 className="section-title"><Icon name="Camera" size={14} /> 截图输出</h4>
          <div className="flex items-center gap-2 mb-2">
            <div className="seg">
              {[["png", "PNG"], ["jpeg", "JPEG"]].map(([k, lb]) => (
                <button key={k} className={"seg-item" + (fmt === k ? " active" : "")} onClick={() => setFmt(k)}>{lb}</button>
              ))}
            </div>
            <button className="btn btn-secondary" disabled={busy === "shot"} onClick={() => capture(eng().activeCamera)}>
              {busy === "shot" ? <Spinner size={13} /> : <Icon name="Camera" size={14} />} 当前机位
            </button>
            <button className="btn btn-ghost" disabled={busy === "shot"}
              onClick={async () => { for (const c of cameras) { await capture(c.id); } }}>
              <Icon name="Layers" size={14} /> 全部机位
            </button>
          </div>
          <div className="flex flex-col gap-2">
            {shots.length === 0 && <span className="text-tertiary" style={{ fontSize: "var(--text-sm)" }}>暂无截图</span>}
            {shots.slice().reverse().map((s) => (
              <div key={s.id} className="card flex items-center gap-2" style={{ padding: "var(--space-2)" }}>
                <img src={s.url} alt={s.note || "截图"} style={{ width: 72, height: 40, objectFit: "cover", borderRadius: 4 }} />
                <div className="flex-1" style={{ minWidth: 0 }}>
                  <div className="ellipsis" style={{ fontSize: "var(--text-xs)" }}>{s.note || s.camera_id}</div>
                  <div className="text-tertiary" style={{ fontSize: 10 }}>{new Date(s.created_at * 1000).toLocaleString("zh-CN")}</div>
                </div>
                <button className="btn btn-icon" title="删除截图"
                  onClick={async () => {
                    try { await api(`/director/session/${shot.id}/screenshots/${s.id}`, { method: "DELETE" }); loadShots(); }
                    catch (err) { toast.err(err.message); }
                  }}>
                  <Icon name="Trash2" size={13} />
                </button>
              </div>
            ))}
          </div>
        </section>

        {/* ControlNet */}
        <section>
          <h4 className="section-title"><Icon name="ScanLine" size={14} /> ControlNet 条件</h4>
          <div className="flex items-center gap-3 mb-2">
            {[["canny", "Canny 边缘"], ["depth", "深度图"], ["pose", "骨骼姿态"]].map(([k, lb]) => (
              <label key={k} className="flex items-center gap-1" style={{ cursor: "pointer", fontSize: "var(--text-sm)" }}>
                <input type="checkbox" checked={!!cb[k]}
                  onChange={(e) => patch("controlnet_checkboxes", { ...cb, [k]: e.target.checked })} />{lb}
              </label>
            ))}
          </div>
          <button className="btn btn-primary" disabled={busy === "cn"} onClick={extract}>
            {busy === "cn" ? <Spinner size={13} /> : <Icon name="Wand2" size={14} />} 提取条件（3D 优先）
          </button>
          {cn && cn.conditions && (
            <div className="flex flex-col gap-2 mt-3">
              {Object.entries(cn.conditions).map(([k, v]) => (
                <div key={k} className="card" style={{ padding: "var(--space-2)" }}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="tag tag-info">{k}</span>
                    <span className="text-tertiary" style={{ fontSize: 10 }}>来源 {v.source}</span>
                  </div>
                  {k !== "pose" && v.url && <img src={v.url} alt={`${k} 条件图`} style={{ width: "100%", borderRadius: 4, display: "block" }} />}
                  {k === "pose" && <span className="text-tertiary" style={{ fontSize: 10 }}>骨骼关键点 JSON（{((v.path || "").split(/[\\/]/).pop())}）</span>}
                </div>
              ))}
            </div>
          )}
        </section>

        {/* TripoSR */}
        <section>
          <h4 className="section-title"><Icon name="Box" size={14} /> TripoSR 3D 生成</h4>
          <p className="text-tertiary" style={{ fontSize: "var(--text-xs)", margin: "0 0 8px" }}>
            上传角色正视图 → AI 裁剪 → 单图生成 3D 模型（OBJ）→ 自动导入场景。与 SDXL 显存互斥由 VRAM Manager 自动处理。
          </p>
          <input type="file" accept="image/*" ref={fileRef} style={{ display: "none" }}
            onChange={(e) => { gen3d(e.target.files[0]); e.target.value = ""; }} />
          <button className="btn btn-secondary" disabled={busy === "triposr"}
            onClick={() => fileRef.current && fileRef.current.click()}>
            {busy === "triposr" ? <Spinner size={13} /> : <Icon name="Upload" size={14} />} 上传角色图生成 3D
          </button>
          {triposr && (
            <div className="card mt-2" style={{ padding: "var(--space-3)" }}>
              <div className="flex items-center justify-between mb-1">
                <span style={{ fontSize: "var(--text-sm)" }}>任务 {triposr.task_id}</span>
                <span className={"tag " + (triposr.state === "done" ? "tag-success" : triposr.state === "failed" ? "tag-danger" : "tag-warning")}>
                  {triposr.state}
                </span>
              </div>
              <div style={{ height: 6, background: "var(--color-surface-muted)", borderRadius: 3, overflow: "hidden" }}>
                <div style={{ width: `${Math.round((triposr.progress || 0) * 100)}%`, height: "100%", background: "var(--color-primary)", transition: "width .3s" }} />
              </div>
              {triposr.stage && <div className="text-tertiary mt-1" style={{ fontSize: 10 }}>阶段：{triposr.stage}</div>}
            </div>
          )}
        </section>
      </div>
    );
  }

  /* ---------------- 导出 ---------------- */
  window.Director3D = { StageModal };
})();
