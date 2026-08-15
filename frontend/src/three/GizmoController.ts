/* ==========================================================================
 * GizmoController.ts —— 变换工具控制器
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170
 * 职责：
 *   - 位置/旋转/缩放 Gizmo（自实现，不依赖 TransformControls）
 *   - 拖拽操控选中对象
 *   - 模式切换：translate / rotate / scale
 *   - 键盘快捷键：W(移动) E(旋转) R(缩放)
 * ========================================================================== */

import * as THREE from 'three';

/** 变换模式 */
export type TransformMode = 'translate' | 'rotate' | 'scale';

/** Gizmo 配置 */
export interface GizmoConfig {
  /** 相机 */
  camera: THREE.PerspectiveCamera;
  /** DOM 元素（监听鼠标事件） */
  domElement: HTMLElement;
  /** 拖拽平面大小 */
  axisSize?: number;
}

/** Gizmo 事件回调 */
export interface GizmoCallbacks {
  /** 开始拖拽 */
  onDragStart?: (object: THREE.Object3D) => void;
  /** 拖拽中 */
  onDrag?: (object: THREE.Object3D) => void;
  /** 拖拽结束 */
  onDragEnd?: (object: THREE.Object3D) => void;
  /** 模式切换 */
  onModeChange?: (mode: TransformMode) => void;
}

/**
 * 变换工具控制器
 * 自实现 3 轴 Gizmo，支持位置/旋转/缩放三种模式。
 */
export class GizmoController {
  /** 相机 */
  private camera: THREE.PerspectiveCamera;
  /** DOM 元素 */
  private domElement: HTMLElement;
  /** 回调 */
  private callbacks: GizmoCallbacks;

  /** 当前变换模式 */
  private mode: TransformMode = 'translate';
  /** 当前选中的对象 */
  private selectedObject: THREE.Object3D | null = null;

  /** Gizmo 根组 */
  private gizmoGroup: THREE.Group;
  /** X 轴箭头 */
  private xAxis: THREE.Object3D;
  /** Y 轴箭头 */
  private yAxis: THREE.Object3D;
  /** Z 轴箭头 */
  private zAxis: THREE.Object3D;

  /** 是否正在拖拽 */
  private isDragging = false;
  /** 拖拽的轴 */
  private dragAxis: 'x' | 'y' | 'z' | null = null;
  /** 拖拽起始点（世界坐标） */
  private dragStartPoint: THREE.Vector3 = new THREE.Vector3();
  /** 拖拽起始对象属性 */
  private dragStartValue: THREE.Vector3 = new THREE.Vector3();

  /** 射线 */
  private raycaster: THREE.Raycaster;
  /** 鼠标 NDC 坐标 */
  private mouse: THREE.Vector2;
  /** 拖拽平面 */
  private dragPlane: THREE.Plane;

  /** 是否启用 */
  private enabled = true;
  /** 是否已销毁 */
  private disposed = false;
  /** 网格吸附步进（COMIC-088；0 = 关闭。位移按米、旋转按 15° 吸附） */
  private snapSize = 0;

  constructor(config: GizmoConfig, callbacks: GizmoCallbacks = {}) {
    this.camera = config.camera;
    this.domElement = config.domElement;
    this.callbacks = callbacks;
    const axisSize = config.axisSize ?? 1.2;

    this.raycaster = new THREE.Raycaster();
    this.mouse = new THREE.Vector2();
    this.dragPlane = new THREE.Plane();

    // ---- 创建 Gizmo 组 ----
    this.gizmoGroup = new THREE.Group();
    this.gizmoGroup.name = '__gizmo__';
    this.gizmoGroup.visible = false;

    // X 轴：红色
    this.xAxis = this.createAxis(0xff4466, axisSize, 'x');
    // Y 轴：绿色
    this.yAxis = this.createAxis(0x44ff66, axisSize, 'y');
    // Z 轴：蓝色
    this.zAxis = this.createAxis(0x4488ff, axisSize, 'z');

    this.gizmoGroup.add(this.xAxis, this.yAxis, this.zAxis);

    // ---- 绑定事件 ----
    this.bindEvents();
  }

  /** 创建单轴 Gizmo */
  private createAxis(color: number, size: number, axis: 'x' | 'y' | 'z'): THREE.Object3D {
    const group = new THREE.Group();
    group.name = `gizmo_${axis}`;
    group.userData.axis = axis;

    // 方向设置
    const directions: Record<string, THREE.Vector3> = {
      x: new THREE.Vector3(1, 0, 0),
      y: new THREE.Vector3(0, 1, 0),
      z: new THREE.Vector3(0, 0, 1),
    };
    const dir = directions[axis];

    // 线段
    const lineGeom = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0),
      dir.clone().multiplyScalar(size),
    ]);
    const line = new THREE.Line(lineGeom, new THREE.LineBasicMaterial({
      color,
      linewidth: 3,
      depthTest: false,
      transparent: true,
      opacity: 0.9,
    }));
    line.renderOrder = 999;
    group.add(line);

    // 箭头头部（圆锥）
    const coneGeom = new THREE.ConeGeometry(size * 0.08, size * 0.2, 12);
    const cone = new THREE.Mesh(coneGeom, new THREE.MeshBasicMaterial({
      color,
      depthTest: false,
      transparent: true,
      opacity: 0.9,
    }));
    cone.renderOrder = 999;
    cone.position.copy(dir.clone().multiplyScalar(size));

    // 旋转圆锥使其朝向轴方向
    if (axis === 'x') {
      cone.rotation.z = -Math.PI / 2;
    } else if (axis === 'z') {
      cone.rotation.x = Math.PI / 2;
    }

    group.add(cone);

    // 根据模式调整外观
    return group;
  }

  /** 绑定鼠标事件 */
  private bindEvents(): void {
    this.domElement.addEventListener('pointerdown', this.onPointerDown);
    this.domElement.addEventListener('pointermove', this.onPointerMove);
    this.domElement.addEventListener('pointerup', this.onPointerUp);
  }

  /** NDC 坐标转换 */
  private updateMouseNDC(event: PointerEvent): void {
    const rect = this.domElement.getBoundingClientRect();
    this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  }

  /** 指针按下 */
  private onPointerDown = (event: PointerEvent): void => {
    if (!this.enabled || !this.selectedObject || this.disposed) return;
    this.updateMouseNDC(event);

    // 检测是否点击了 Gizmo 轴
    this.raycaster.setFromCamera(this.mouse, this.camera);
    const intersects = this.raycaster.intersectObjects([this.xAxis, this.yAxis, this.zAxis], true);

    if (intersects.length > 0) {
      // 找到所属轴
      let obj: THREE.Object3D | null = intersects[0].object;
      while (obj && !obj.userData.axis) {
        obj = obj.parent;
      }
      if (obj && obj.userData.axis) {
        this.dragAxis = obj.userData.axis as 'x' | 'y' | 'z';
        this.isDragging = true;
        this.setupDragPlane();
        this.dragStartPoint.copy(this.selectedObject.position);
        this.dragStartValue.copy(this.getDragValue());
        this.callbacks.onDragStart?.(this.selectedObject);
        event.stopPropagation();
        event.preventDefault();
      }
    }
  };

  /** 设置拖拽平面（垂直于相机视线，包含拖拽轴） */
  private setupDragPlane(): void {
    if (!this.selectedObject || !this.dragAxis) return;

    // 拖拽平面法线：取相机方向与拖拽轴的叉积
    const cameraDir = new THREE.Vector3();
    this.camera.getWorldDirection(cameraDir);

    const axisVec = new THREE.Vector3();
    switch (this.dragAxis) {
      case 'x': axisVec.set(1, 0, 0); break;
      case 'y': axisVec.set(0, 1, 0); break;
      case 'z': axisVec.set(0, 0, 1); break;
    }

    const planeNormal = new THREE.Vector3().crossVectors(axisVec, cameraDir);
    if (planeNormal.lengthSq() < 1e-6) {
      // 相机与轴平行，退化用相机方向作为法线
      planeNormal.copy(cameraDir);
    }
    planeNormal.normalize();

    this.dragPlane.setFromNormalAndCoplanarPoint(planeNormal, this.selectedObject.position);
  }

  /** 获取当前拖拽值 */
  private getDragValue(): THREE.Vector3 {
    if (!this.selectedObject) return new THREE.Vector3();
    switch (this.mode) {
      case 'translate': return this.selectedObject.position.clone();
      case 'rotate': {
        // Euler 旋转向量（弧度）转 Vector3，便于按轴分量加减角度
        const r = this.selectedObject.rotation;
        return new THREE.Vector3(r.x, r.y, r.z);
      }
      case 'scale': return this.selectedObject.scale.clone();
      default: return new THREE.Vector3();
    }
  }

  /** 指针移动 */
  private onPointerMove = (event: PointerEvent): void => {
    if (!this.isDragging || !this.selectedObject || !this.dragAxis || this.disposed) return;
    this.updateMouseNDC(event);

    // 射线与拖拽平面求交
    this.raycaster.setFromCamera(this.mouse, this.camera);
    const intersection = new THREE.Vector3();
    if (!this.raycaster.ray.intersectPlane(this.dragPlane, intersection)) return;

    switch (this.mode) {
      case 'translate': {
        // 沿轴方向位移
        const delta = intersection.clone().sub(this.dragStartPoint);
        const axisVec = new THREE.Vector3();
        switch (this.dragAxis) {
          case 'x': axisVec.set(1, 0, 0); break;
          case 'y': axisVec.set(0, 1, 0); break;
          case 'z': axisVec.set(0, 0, 1); break;
        }
        const projection = delta.dot(axisVec);
        this.selectedObject.position.copy(this.dragStartValue).add(axisVec.multiplyScalar(projection));
        // COMIC-088 网格吸附：拖拽轴分量按步进取整（其余轴不受影响）
        if (this.snapSize > 0) {
          const axisKey = this.dragAxis as 'x' | 'y' | 'z';
          const v = this.selectedObject.position[axisKey];
          this.selectedObject.position[axisKey] = Math.round(v / this.snapSize) * this.snapSize;
        }
        break;
      }
      case 'rotate': {
        // 计算旋转角度
        const center = this.selectedObject.position;
        const startDir = this.dragStartPoint.clone().sub(center);
        const currentDir = intersection.clone().sub(center);
        const axisVec = new THREE.Vector3();
        switch (this.dragAxis) {
          case 'x': axisVec.set(1, 0, 0); break;
          case 'y': axisVec.set(0, 1, 0); break;
          case 'z': axisVec.set(0, 0, 1); break;
        }
        const angle = Math.atan2(
          currentDir.clone().cross(startDir).dot(axisVec),
          currentDir.dot(startDir),
        );
        const axisKey = this.dragAxis as 'x' | 'y' | 'z';
        this.selectedObject.rotation[axisKey] = this.dragStartValue[axisKey] + angle;
        // COMIC-088 旋转吸附：15° 步进
        if (this.snapSize > 0) {
          const step = Math.PI / 12;
          const v = this.selectedObject.rotation[axisKey];
          this.selectedObject.rotation[axisKey] = Math.round(v / step) * step;
        }
        break;
      }
      case 'scale': {
        // 沿轴方向缩放
        const delta = intersection.clone().sub(this.selectedObject.position);
        const axisVec = new THREE.Vector3();
        switch (this.dragAxis) {
          case 'x': axisVec.set(1, 0, 0); break;
          case 'y': axisVec.set(0, 1, 0); break;
          case 'z': axisVec.set(0, 0, 1); break;
        }
        const projection = delta.dot(axisVec);
        const axisKey = this.dragAxis as 'x' | 'y' | 'z';
        const newScale = Math.max(0.01, this.dragStartValue[axisKey] + projection * 0.5);
        this.selectedObject.scale[axisKey] = newScale;
        break;
      }
    }

    this.callbacks.onDrag?.(this.selectedObject);
  };

  /** 指针释放 */
  private onPointerUp = (): void => {
    if (this.isDragging && this.selectedObject) {
      this.callbacks.onDragEnd?.(this.selectedObject);
    }
    this.isDragging = false;
    this.dragAxis = null;
  };

  /** 设置选中对象 */
  public setSelected(object: THREE.Object3D | null): void {
    this.selectedObject = object;
    if (object) {
      this.gizmoGroup.visible = true;
      this.gizmoGroup.position.copy(object.position);
      this.gizmoGroup.rotation.copy(object.rotation);
    } else {
      this.gizmoGroup.visible = false;
    }
  }

  /** 获取 Gizmo 组（需添加到场景中） */
  public getGizmoGroup(): THREE.Group {
    return this.gizmoGroup;
  }

  /** 设置变换模式 */
  public setMode(mode: TransformMode): void {
    this.mode = mode;
    this.callbacks.onModeChange?.(mode);
    // 根据模式更新 Gizmo 外观
    this.updateGizmoAppearance();
  }

  /** 获取当前模式 */
  public getMode(): TransformMode {
    return this.mode;
  }

  /** 更新 Gizmo 外观（根据模式） */
  private updateGizmoAppearance(): void {
    // 根据模式调整箭头形状（简化实现：旋转模式用方块，缩放模式用方块末端）
    const axes = [this.xAxis, this.yAxis, this.zAxis];
    axes.forEach((axis) => {
      axis.traverse((child) => {
        if (child instanceof THREE.Mesh) {
          // 旋转模式时箭头半透明
          const mat = child.material as THREE.MeshBasicMaterial;
          mat.opacity = this.mode === 'rotate' ? 0.6 : 0.9;
        }
      });
    });
  }

  /** 每帧更新（同步位置） */
  public update(): void {
    if (this.selectedObject && this.gizmoGroup.visible) {
      this.gizmoGroup.position.copy(this.selectedObject.position);
    }
  }

  /** 设置网格吸附步进（COMIC-088；0 关闭，>0 时位移按该米数、旋转按 15° 吸附） */
  public setSnap(size: number): void {
    this.snapSize = Math.max(0, size);
  }

  /** 获取当前吸附步进 */
  public getSnap(): number {
    return this.snapSize;
  }

  /** 设置启用/禁用 */
  public setEnabled(enabled: boolean): void {
    this.enabled = enabled;
    if (!enabled) {
      this.gizmoGroup.visible = false;
      this.isDragging = false;
    }
  }

  /** 销毁 */
  public dispose(): void {
    this.disposed = true;
    this.domElement.removeEventListener('pointerdown', this.onPointerDown);
    this.domElement.removeEventListener('pointermove', this.onPointerMove);
    this.domElement.removeEventListener('pointerup', this.onPointerUp);

    this.gizmoGroup.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (mesh.geometry) mesh.geometry.dispose();
      if (mesh.material) {
        const mat = mesh.material;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else mat.dispose();
      }
    });
  }
}

export default GizmoController;
