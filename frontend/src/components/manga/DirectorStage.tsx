/* ==========================================================================
 * DirectorStage.tsx —— 导演台 3D 视口（规格 §9.3）
 * --------------------------------------------------------------------------
 * Three.js r170 WebGL2
 * 功能：
 *   - 左键拖拽旋转，右键拖拽平移，滚轮缩放
 *   - 左键单击选择对象（Raycaster）
 *   - 右键角色弹出菜单（锁定/解锁/删除/姿态）
 *   - 锁定占位：金色虚线边框
 *   - 远程桌面检测 → 静态截图模式
 * ========================================================================== */

import React, { useEffect, useRef, useState, useCallback } from 'react';
import * as THREE from 'three';
import { SceneManager } from '@three/SceneManager';
import { RaycastSelector } from '@three/RaycastSelector';
import { GizmoController, type TransformMode } from '@three/GizmoController';
import * as mangaApi from '@/services/mangaApi';
import { useAppStore } from '@/stores/useAppStore';

/** 三维向量（机位/角色变换持久化载荷） */
interface Vec3 {
  x: number;
  y: number;
  z: number;
}

/** 导演台机位（后端持久化 id 或本地态 id；变换随条目本地保存） */
interface DirectorCam {
  id: string;
  name: string;
  position: Vec3;
  rotation: Vec3;
  fov: number;
}

/** 读取 CSS 令牌并解析为 Three.js 十六进制颜色（仅支持 #rrggbb 令牌） */
function cssTokenHex(name: string, fallback: number): number {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const m = /^#([0-9a-fA-F]{6})$/.exec(v);
  return m ? parseInt(m[1], 16) : fallback;
}

/** 右键菜单项 */
interface ContextMenuItem {
  label: string;
  action: () => void;
  danger?: boolean;
}

/** 3D 对象元数据 */
interface ObjectMeta {
  id: string;
  name: string;
  type: 'character' | 'prop' | 'scene' | 'light';
  locked: boolean;
}

/** 组件 Props */
export interface DirectorStageProps {
  /** 项目 ID */
  projectId?: string;
  /** 分镜 ID */
  shotId?: string;
  /** 关闭回调 */
  onClose?: () => void;
  /** 截图回调 */
  onScreenshot?: (dataUrl: string) => void;
}

/**
 * 导演台 3D 视口组件
 * 使用 Three.js r170 WebGL2 渲染 3D 场景，支持交互操控。
 */
export const DirectorStage: React.FC<DirectorStageProps> = ({
  projectId,
  shotId,
  onClose,
  onScreenshot,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneMgrRef = useRef<SceneManager | null>(null);
  const raycastRef = useRef<RaycastSelector | null>(null);
  const gizmoRef = useRef<GizmoController | null>(null);

  const [transformMode, setTransformMode] = useState<TransformMode>('translate');
  const [selectedName, setSelectedName] = useState<string>('');
  /** 当前选中对象引用（COMIC-087 属性面板数据源） */
  const [selObj, setSelObj] = useState<THREE.Object3D | null>(null);
  /** 属性面板 Transform 数值（位置 m / 旋转 ° / 缩放倍率，字符串态便于输入中暂存） */
  const [tf, setTf] = useState({ px: '', py: '', pz: '', rx: '', ry: '', rz: '', sx: '', sy: '', sz: '' });
  /** 网格吸附开关（COMIC-088：位移 0.5m / 旋转 15° 步进） */
  const [snapOn, setSnapOn] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; items: ContextMenuItem[] } | null>(null);
  const [isRemoteDesktop, setIsRemoteDesktop] = useState(false);
  const [staticMode, setStaticMode] = useState(false);
  const [objectList, setObjectList] = useState<ObjectMeta[]>([]);
  /** 机位列表（添加/更新经 mangaApi 持久化；后端不可达保持本地态 + 如实提示） */
  const [cameras, setCameras] = useState<DirectorCam[]>([]);
  /** 机位操作进行中标记（禁用重复点击） */
  const [camBusy, setCamBusy] = useState(false);
  /** 应用机位到视口相机的桥（在初始化 effect 内赋值，同步轨道状态） */
  const applyCamRef = useRef<((cam: DirectorCam) => void) | null>(null);
  const showToast = useAppStore((s) => s.showToast);

  /**
   * 角色拖拽结束持久化（POST /manga/director/character/position）。
   * 后端不可达时保持本地态并 console.warn，绝不伪造成功提示。
   */
  const persistObjectTransform = useCallback((obj: THREE.Object3D) => {
    const meta = obj.userData.meta as ObjectMeta | undefined;
    if (!meta || meta.type !== 'character') {
      return;
    }
    mangaApi
      .setCharacterPosition({
        character_id: meta.id,
        position: { x: obj.position.x, y: obj.position.y, z: obj.position.z },
        rotation: { x: obj.rotation.x, y: obj.rotation.y, z: obj.rotation.z },
        scale: obj.scale.x,
      })
      .catch((err) => {
        console.warn('[DirectorStage] 角色位置持久化失败（保持本地态）:', err);
      });
  }, []);

  // ---- 远程桌面检测 ----
  useEffect(() => {
    // 检测远程桌面环境（RDP/虚拟机等）
    const checkRemote = () => {
      // 方法1：检测 WebGL 性能（远程桌面通常无 GPU 加速）
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
      if (!gl) {
        setIsRemoteDesktop(true);
        setStaticMode(true);
        return;
      }
      // 方法2：检测远程桌面特征
      const ua = navigator.userAgent.toLowerCase();
      const isRDP = ua.includes('rdp') || ua.includes('remote') || ua.includes('citrix');
      // 方法3：检测屏幕分辨率异常（远程桌面常见）
      const isLowPerf = window.screen.colorDepth < 24;

      if (isRDP || isLowPerf) {
        setIsRemoteDesktop(true);
        setStaticMode(true);
      }
    };
    checkRemote();
  }, []);

  // ---- 初始化 Three.js 场景 ----
  useEffect(() => {
    if (!containerRef.current) return;

    const sceneMgr = new SceneManager({
      container: containerRef.current,
      fov: 50,
      cameraPosition: [8, 6, 12],
      clearColor: cssTokenHex('--color-bg', 0x1a1a2e),
      antialias: !staticMode,
      shadowMap: !staticMode,
    });
    sceneMgrRef.current = sceneMgr;

    // 添加地面网格（令牌化：暗色主题网格线）
    const grid = new THREE.GridHelper(
      40,
      40,
      cssTokenHex('--color-text-tertiary', 0x6e7789),
      cssTokenHex('--color-input-bg', 0x0f3460),
    );
    grid.name = '__grid__';
    sceneMgr.add(grid);

    // 添加地面平面
    const groundGeom = new THREE.PlaneGeometry(40, 40);
    const groundMat = new THREE.MeshStandardMaterial({
      color: cssTokenHex('--color-card', 0x16213e),
      roughness: 0.8,
      metalness: 0.0,
    });
    const ground = new THREE.Mesh(groundGeom, groundMat);
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    ground.name = '__ground__';
    sceneMgr.add(ground);

    // 初始化射线选择器（显式注入 scene：相机未加入场景，camera.parent 为 null 会导致点选永不命中，COMIC-079）
    const raycaster = new RaycastSelector({
      camera: sceneMgr.camera,
      domElement: sceneMgr.getCanvas(),
      scene: sceneMgr.scene,
      highlightColor: cssTokenHex('--color-primary-300', 0xff9bbe),
    }, {
      onSelect: (obj) => {
        setSelectedName(obj ? (obj.userData.meta?.name || obj.name) : '');
        setSelObj(obj);
        gizmoRef.current?.setSelected(obj);
      },
      onHover: (obj) => {
        sceneMgr.getCanvas().style.cursor = obj ? 'pointer' : 'default';
      },
    });
    raycastRef.current = raycaster;

    // 初始化变换工具（拖拽结束 → 角色位置持久化 + 属性面板数值刷新）
    const gizmo = new GizmoController({
      camera: sceneMgr.camera,
      domElement: sceneMgr.getCanvas(),
    }, {
      onModeChange: (mode) => setTransformMode(mode),
      onDragEnd: (obj) => {
        persistObjectTransform(obj);
        syncTfFrom(obj);
      },
    });
    gizmoRef.current = gizmo;
    sceneMgr.add(gizmo.getGizmoGroup());

    // ---- 相机控制器（自实现轨道控制） ----
    const canvas = sceneMgr.getCanvas();
    let isOrbiting = false;
    let isPanning = false;
    let lastX = 0;
    let lastY = 0;
    let spherical = new THREE.Spherical();
    spherical.setFromVector3(sceneMgr.camera.position);

    const target = new THREE.Vector3(0, 0, 0);

    const onPointerDown = (e: PointerEvent) => {
      // 右键平移，左键旋转（未选中 Gizmo 时）
      if (e.button === 2) {
        isPanning = true;
        lastX = e.clientX;
        lastY = e.clientY;
        e.preventDefault();
      } else if (e.button === 0) {
        // 检查是否点击 Gizmo（由 GizmoController 处理）
        isOrbiting = true;
        lastX = e.clientX;
        lastY = e.clientY;
      }
    };

    const onPointerMove = (e: PointerEvent) => {
      if (isOrbiting) {
        const deltaX = e.clientX - lastX;
        const deltaY = e.clientY - lastY;
        spherical.theta -= deltaX * 0.005;
        spherical.phi -= deltaY * 0.005;
        spherical.phi = Math.max(0.1, Math.min(Math.PI - 0.1, spherical.phi));
        sceneMgr.camera.position.setFromSpherical(spherical).add(target);
        sceneMgr.camera.lookAt(target);
        lastX = e.clientX;
        lastY = e.clientY;
      } else if (isPanning) {
        const deltaX = e.clientX - lastX;
        const deltaY = e.clientY - lastY;
        const panSpeed = 0.02;
        const panX = new THREE.Vector3();
        const panY = new THREE.Vector3();
        sceneMgr.camera.matrix.extractBasis(panX, panY, new THREE.Vector3());
        target.addScaledVector(panX, -deltaX * panSpeed);
        target.addScaledVector(panY, deltaY * panSpeed);
        sceneMgr.camera.position.setFromSpherical(spherical).add(target);
        sceneMgr.camera.lookAt(target);
        lastX = e.clientX;
        lastY = e.clientY;
      }
    };

    const onPointerUp = () => {
      isOrbiting = false;
      isPanning = false;
    };

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      spherical.radius *= e.deltaY > 0 ? 1.1 : 0.9;
      spherical.radius = Math.max(2, Math.min(100, spherical.radius));
      sceneMgr.camera.position.setFromSpherical(spherical).add(target);
      sceneMgr.camera.lookAt(target);
    };

    // 应用已保存机位：恢复位置/旋转/FOV，并同步轨道状态（target 取视线前方 10m）
    applyCamRef.current = (cam: DirectorCam) => {
      sceneMgr.camera.position.set(cam.position.x, cam.position.y, cam.position.z);
      sceneMgr.camera.rotation.set(cam.rotation.x, cam.rotation.y, cam.rotation.z);
      if (cam.fov > 0) {
        sceneMgr.camera.fov = cam.fov;
        sceneMgr.camera.updateProjectionMatrix();
      }
      const forward = new THREE.Vector3(0, 0, -1).applyEuler(sceneMgr.camera.rotation);
      target.copy(sceneMgr.camera.position).addScaledVector(forward, 10);
      spherical.setFromVector3(sceneMgr.camera.position.clone().sub(target));
      if (staticMode) {
        sceneMgr.renderOnce();
      }
    };

    // 右键菜单
    const onContextMenu = (e: MouseEvent) => {
      e.preventDefault();
      // 检测右键点击的对象
      const rect = canvas.getBoundingClientRect();
      const mouse = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      );
      const ray = new THREE.Raycaster();
      ray.setFromCamera(mouse, sceneMgr.camera);
      const intersects = ray.intersectObjects(sceneMgr.scene.children, true);
      const validHit = intersects.find((i) => {
        let obj: THREE.Object3D | null = i.object;
        while (obj) {
          if (obj.name.startsWith('__')) return false;
          obj = obj.parent;
        }
        return true;
      });

      if (validHit) {
        let target2: THREE.Object3D = validHit.object;
        while (target2.parent && target2.parent.type !== 'Scene') {
          target2 = target2.parent;
        }
        showContextMenu(e.clientX, e.clientY, target2);
      }
    };

    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('contextmenu', onContextMenu);

    // ---- COMIC-075：双击聚焦对象 ----
    const onDblClick = (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      const mouse = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      );
      const ray = new THREE.Raycaster();
      ray.setFromCamera(mouse, sceneMgr.camera);
      const intersects = ray.intersectObjects(sceneMgr.scene.children, true);
      const hit = intersects.find((i) => {
        let o: THREE.Object3D | null = i.object;
        while (o) {
          if (o.name.startsWith('__')) return false;
          o = o.parent;
        }
        return true;
      });
      if (hit) {
        // 聚焦：轨道目标平移到对象位置，保持当前距离与角度
        const pos = new THREE.Vector3();
        hit.object.getWorldPosition(pos);
        target.copy(pos);
        spherical.setFromVector3(sceneMgr.camera.position.clone().sub(target));
        sceneMgr.camera.lookAt(target);
        if (staticMode) sceneMgr.renderOnce();
      }
    };
    canvas.addEventListener('dblclick', onDblClick);

    // ---- COMIC-075：F 键重置视角 ----
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'f' && e.key !== 'F') return;
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
      target.set(0, 0, 0);
      sceneMgr.camera.position.set(8, 6, 12);
      spherical.setFromVector3(sceneMgr.camera.position.clone().sub(target));
      sceneMgr.camera.lookAt(target);
      if (staticMode) sceneMgr.renderOnce();
    };
    window.addEventListener('keydown', onKeyDown);

    // 每帧更新 Gizmo
    sceneMgr.onUpdate(() => {
      gizmo.update();
    });

    // 启动渲染（静态模式只渲染一帧）
    if (staticMode) {
      sceneMgr.renderOnce();
    } else {
      sceneMgr.start();
    }

    // 清理
    return () => {
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('contextmenu', onContextMenu);
      canvas.removeEventListener('dblclick', onDblClick);
      window.removeEventListener('keydown', onKeyDown);
      applyCamRef.current = null;
      raycaster.dispose();
      gizmo.dispose();
      sceneMgr.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [staticMode]);

  /** 从对象当前变换刷新属性面板数值（COMIC-087） */
  const syncTfFrom = useCallback((obj: THREE.Object3D) => {
    const r2d = (v: number) => ((v * 180) / Math.PI).toFixed(1);
    setTf({
      px: obj.position.x.toFixed(2),
      py: obj.position.y.toFixed(2),
      pz: obj.position.z.toFixed(2),
      rx: r2d(obj.rotation.x),
      ry: r2d(obj.rotation.y),
      rz: r2d(obj.rotation.z),
      sx: obj.scale.x.toFixed(2),
      sy: obj.scale.y.toFixed(2),
      sz: obj.scale.z.toFixed(2),
    });
  }, []);

  // 选中对象变化 → 面板回填（COMIC-087）
  useEffect(() => {
    if (selObj) syncTfFrom(selObj);
  }, [selObj, syncTfFrom]);

  // 网格吸附开关联动 Gizmo（COMIC-088：0.5m 位移步进）
  useEffect(() => {
    gizmoRef.current?.setSnap(snapOn ? 0.5 : 0);
  }, [snapOn]);

  /** 属性面板字段提交（解析失败保持原值并如实提示） */
  const applyTfField = useCallback(
    (field: keyof typeof tf, raw: string) => {
      setTf((prev) => ({ ...prev, [field]: raw }));
      if (!selObj) return;
      const v = parseFloat(raw);
      if (!Number.isFinite(v)) return;
      const d2r = (x: number) => (x * Math.PI) / 180;
      switch (field) {
        case 'px': selObj.position.x = v; break;
        case 'py': selObj.position.y = v; break;
        case 'pz': selObj.position.z = v; break;
        case 'rx': selObj.rotation.x = d2r(v); break;
        case 'ry': selObj.rotation.y = d2r(v); break;
        case 'rz': selObj.rotation.z = d2r(v); break;
        case 'sx': selObj.scale.x = Math.max(0.01, v); break;
        case 'sy': selObj.scale.y = Math.max(0.01, v); break;
        case 'sz': selObj.scale.z = Math.max(0.01, v); break;
      }
      if (staticMode) sceneMgrRef.current?.renderOnce();
    },
    [selObj, staticMode],
  );

  /** 显示右键菜单 */
  const showContextMenu = useCallback((x: number, y: number, obj: THREE.Object3D) => {
    const meta = (obj.userData.meta as ObjectMeta) || { id: obj.uuid, name: obj.name, type: 'prop' as const, locked: false };
    const items: ContextMenuItem[] = [
      {
        label: meta.locked ? '解锁对象' : '锁定对象',
        action: () => {
          meta.locked = !meta.locked;
          obj.userData.meta = meta;
          // 锁定时添加金色虚线边框
          if (meta.locked) {
            applyLockOutline(obj);
          } else {
            removeLockOutline(obj);
          }
          setObjectList((prev) => prev.map((m) => m.id === meta.id ? { ...m, locked: meta.locked } : m));
        },
      },
      {
        label: '设置姿态',
        action: () => {
          // 触发姿态面板（通过事件通知）
          window.dispatchEvent(new CustomEvent('omni:director-show-pose', { detail: { objectId: meta.id } }));
        },
      },
      {
        label: '删除对象',
        action: () => {
          sceneMgrRef.current?.remove(obj);
          setObjectList((prev) => prev.filter((m) => m.id !== meta.id));
        },
        danger: true,
      },
    ];
    setContextMenu({ x, y, items });
  }, []);

  /** 应用锁定金色虚线边框 */
  const applyLockOutline = (obj: THREE.Object3D) => {
    const box = new THREE.Box3().setFromObject(obj);
    const size = new THREE.Vector3();
    box.getSize(size);
    const center = new THREE.Vector3();
    box.getCenter(center);

    const edges = new THREE.EdgesGeometry(new THREE.BoxGeometry(size.x, size.y, size.z));
    const lineMat = new THREE.LineDashedMaterial({
      color: 0xFFD700, // 金色
      dashSize: 0.15,
      gapSize: 0.1,
      linewidth: 2,
    });
    const outline = new THREE.LineSegments(edges, lineMat);
    outline.computeLineDistances();
    outline.position.copy(center);
    outline.name = '__lock_outline__';
    obj.parent?.add(outline);
  };

  /** 移除锁定虚线边框 */
  const removeLockOutline = (obj: THREE.Object3D) => {
    const parent = obj.parent;
    if (!parent) return;
    const outline = parent.children.find((c) => c.name === '__lock_outline__');
    if (outline) {
      parent.remove(outline);
      (outline as THREE.LineSegments).geometry.dispose();
      ((outline as THREE.LineSegments).material as THREE.Material).dispose();
    }
  };

  /** 切换变换模式 */
  const handleModeChange = (mode: TransformMode) => {
    setTransformMode(mode);
    gizmoRef.current?.setMode(mode);
  };

  /** 读取当前视口相机变换（机位持久化载荷） */
  const captureView = (): { position: Vec3; rotation: Vec3; fov: number } | null => {
    const cam = sceneMgrRef.current?.camera;
    if (!cam) return null;
    return {
      position: { x: cam.position.x, y: cam.position.y, z: cam.position.z },
      rotation: { x: cam.rotation.x, y: cam.rotation.y, z: cam.rotation.z },
      fov: cam.fov,
    };
  };

  /**
   * 保存机位（POST /manga/director/camera/add 持久化当前视角）。
   * 后端不可达时降级为本地态机位并如实提示，不伪造持久化成功。
   */
  const handleAddCamera = async () => {
    const view = captureView();
    if (!view || camBusy) return;
    const name = `机位 ${cameras.length + 1}`;
    setCamBusy(true);
    try {
      const id = await mangaApi.addCamera({ name, ...view });
      setCameras((prev) => [...prev, { id, name, ...view }]);
      showToast(`机位「${name}」已保存`, 'success');
    } catch (err) {
      console.warn('[DirectorStage] 机位持久化失败（降级本地态）:', err);
      setCameras((prev) => [...prev, { id: `local-${Date.now()}`, name, ...view }]);
      showToast('机位已保存到本地（后端不可达，未持久化）', 'warning');
    } finally {
      setCamBusy(false);
    }
  };

  /** 应用机位到视口（本地变换恢复，无需后端往返） */
  const handleApplyCamera = (cam: DirectorCam) => {
    applyCamRef.current?.(cam);
  };

  /**
   * 用当前视角覆盖已保存机位（PUT /manga/director/camera/{id}）。
   * 本地态机位（local- 前缀）跳过后端调用；后端失败保留本地更新并如实提示。
   */
  const handleUpdateCamera = async (cam: DirectorCam) => {
    const view = captureView();
    if (!view || camBusy) return;
    const next = { ...cam, ...view };
    setCameras((prev) => prev.map((c) => (c.id === cam.id ? next : c)));
    if (cam.id.startsWith('local-')) {
      showToast(`机位「${cam.name}」已更新（本地态，未持久化）`, 'info');
      return;
    }
    setCamBusy(true);
    try {
      await mangaApi.updateCamera(cam.id, view);
      showToast(`机位「${cam.name}」已更新`, 'success');
    } catch (err) {
      console.warn('[DirectorStage] 机位更新持久化失败（保留本地更新）:', err);
      showToast('机位已在本地更新（后端不可达，未持久化）', 'warning');
    } finally {
      setCamBusy(false);
    }
  };

  /** 截图（TC-E-010：无回调时直接下载 PNG） */
  const handleScreenshot = () => {
    if (!sceneMgrRef.current) return;
    const dataUrl = sceneMgrRef.current.screenshot();
    if (onScreenshot) {
      onScreenshot(dataUrl);
      return;
    }
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = `director_${projectId ?? 'default'}_${shotId ?? 'stage'}_${Date.now()}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  /** 添加测试角色 */
  const addTestCharacter = () => {
    if (!sceneMgrRef.current) return;
    const geom = new THREE.CapsuleGeometry(0.4, 1.2, 8, 16);
    const mat = new THREE.MeshStandardMaterial({
      color: cssTokenHex('--color-primary-300', 0xff9bbe),
      roughness: 0.6,
    });
    const char = new THREE.Mesh(geom, mat);
    char.position.set(
      (Math.random() - 0.5) * 6,
      1.0,
      (Math.random() - 0.5) * 6,
    );
    char.castShadow = true;
    char.name = `角色_${objectList.length + 1}`;
    const meta: ObjectMeta = {
      id: char.uuid,
      name: char.name,
      type: 'character',
      locked: false,
    };
    char.userData.meta = meta;
    sceneMgrRef.current.add(char);
    setObjectList((prev) => [...prev, meta]);
  };

  // 关闭右键菜单（点击外部）
  useEffect(() => {
    if (!contextMenu) return;
    const closeMenu = () => setContextMenu(null);
    const timeoutId = setTimeout(() => window.addEventListener('click', closeMenu), 0);
    return () => {
      clearTimeout(timeoutId);
      window.removeEventListener('click', closeMenu);
    };
  }, [contextMenu]);

  return (
    <div className="director-stage-container">
      {/* 顶部工具栏 */}
      <div className="ds-toolbar">
        <div className="ds-toolbar-left">
          <span className="ds-title">🎬 导演台</span>
          {projectId && <span className="ds-meta">项目: {projectId}</span>}
          {shotId && <span className="ds-meta">分镜: {shotId}</span>}
        </div>
        <div className="ds-toolbar-right">
          {isRemoteDesktop && (
            <span className="ds-remote-badge" title="检测到远程桌面环境，已启用静态截图模式">
              远程桌面模式
            </span>
          )}
          <button className="btn btn-secondary btn-sm" onClick={handleScreenshot}>
            截图
          </button>
          {onClose && (
            <button className="btn btn-secondary btn-sm" onClick={onClose}>
              关闭
            </button>
          )}
        </div>
      </div>

      {/* 变换模式切换 */}
      <div className="ds-mode-bar">
        <button
          className={`btn btn-sm ${transformMode === 'translate' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => handleModeChange('translate')}
          title="移动 (W)"
        >
          移动
        </button>
        <button
          className={`btn btn-sm ${transformMode === 'rotate' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => handleModeChange('rotate')}
          title="旋转 (E)"
        >
          旋转
        </button>
        <button
          className={`btn btn-sm ${transformMode === 'scale' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => handleModeChange('scale')}
          title="缩放 (R)"
        >
          缩放
        </button>
        <span className="ds-selected-info">
          {selectedName ? `已选中: ${selectedName}` : '未选中对象'}
        </span>
        {/* COMIC-088 网格吸附开关 */}
        <button
          className={`btn btn-sm ${snapOn ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setSnapOn((v) => !v)}
          title="网格吸附：位移按 0.5m、旋转按 15° 步进"
          aria-pressed={snapOn}
        >
          吸附{snapOn ? '开' : '关'}
        </button>
        <button className="btn btn-secondary btn-sm" onClick={addTestCharacter}>
          + 添加角色
        </button>
        <button
          className="btn btn-secondary btn-sm"
          onClick={handleAddCamera}
          disabled={camBusy}
          title="把当前视角保存为机位（持久化到后端）"
        >
          + 保存机位
        </button>
      </div>

      {/* 机位条：点击应用视角；💾 用当前视角覆盖该机位（PUT 持久化） */}
      {cameras.length > 0 && (
        <div className="ds-mode-bar" style={{ gap: 6, flexWrap: 'wrap' }}>
          <span className="ds-selected-info">机位：</span>
          {cameras.map((cam) => (
            <span key={cam.id} className="inline-flex items-center gap-1">
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => handleApplyCamera(cam)}
                title={`应用「${cam.name}」视角`}
              >
                {cam.name}
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => handleUpdateCamera(cam)}
                disabled={camBusy}
                title={`用当前视角覆盖「${cam.name}」`}
                aria-label={`用当前视角覆盖${cam.name}`}
              >
                💾
              </button>
            </span>
          ))}
        </div>
      )}

      {/* 3D 视口 */}
      <div
        ref={containerRef}
        className="ds-viewport"
        style={{ position: 'relative', width: '100%', height: 'calc(100% - 96px)' }}
      />

      {/* COMIC-087 属性面板：选中对象的 Transform 精确数值输入 */}
      {selObj && (
        <div
          className="ds-transform-panel"
          data-testid="ds-transform-panel"
          style={{
            position: 'absolute',
            top: 8,
            right: 8,
            zIndex: 10,
            width: 208,
            padding: 'var(--space-3)',
            background: 'var(--color-card)',
            border: '1px solid var(--color-border-light)',
            borderRadius: 'var(--radius-md)',
            fontSize: 'var(--font-size-xs)',
            color: 'var(--color-text-secondary)',
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6, color: 'var(--color-text-primary)' }}>
            属性：{selectedName}
          </div>
          {(
            [
              ['位置 (m)', ['px', 'py', 'pz'], 0.1],
              ['旋转 (°)', ['rx', 'ry', 'rz'], 5],
              ['缩放 (×)', ['sx', 'sy', 'sz'], 0.1],
            ] as Array<[string, Array<keyof typeof tf>, number]>
          ).map(([label, keys, step]) => (
            <div key={label} style={{ marginBottom: 6 }}>
              <div style={{ marginBottom: 2 }}>{label}</div>
              <div style={{ display: 'flex', gap: 4 }}>
                {keys.map((k, i) => (
                  <input
                    key={k}
                    type="number"
                    step={step}
                    aria-label={`${label}${'XYZ'[i]}`}
                    value={tf[k]}
                    onChange={(e) => applyTfField(k, e.target.value)}
                    style={{
                      width: '100%',
                      minWidth: 0,
                      padding: '2px 4px',
                      background: 'var(--color-input-bg)',
                      border: '1px solid var(--color-input-border)',
                      borderRadius: 'var(--radius-sm)',
                      color: 'var(--color-text-primary)',
                      fontSize: 'var(--font-size-xs)',
                    }}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 操作提示 */}
      <div className="ds-hint">
        <span>左键拖拽：旋转视角</span>
        <span>右键拖拽：平移视角</span>
        <span>滚轮：缩放</span>
        <span>左键单击：选择对象</span>
        <span>右键单击：弹出菜单</span>
        <span>双击：聚焦对象</span>
        <span>F：重置视角</span>
      </div>

      {/* 右键菜单 */}
      {contextMenu && (
        <div
          className="ds-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {contextMenu.items.map((item, i) => (
            <button
              key={i}
              className={`ds-menu-item ${item.danger ? 'ds-menu-danger' : ''}`}
              onClick={(e) => {
                e.stopPropagation();
                item.action();
                setContextMenu(null);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}

      {/* 静态模式提示 */}
      {staticMode && (
        <div className="ds-static-notice">
          已启用静态截图模式（远程桌面检测），3D 交互不可用
        </div>
      )}
    </div>
  );
};

export default DirectorStage;
