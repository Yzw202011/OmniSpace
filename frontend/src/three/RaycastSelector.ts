/* ==========================================================================
 * RaycastSelector.ts —— 射线选择器
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170
 * 职责：
 *   - 鼠标点击选择 3D 对象（Raycaster）
 *   - 高亮选中对象（轮廓边框）
 *   - 框选（Frustum 矩形选择）
 * ========================================================================== */

import * as THREE from 'three';

/** 选择配置 */
export interface SelectorConfig {
  /** 相机 */
  camera: THREE.PerspectiveCamera;
  /** DOM 元素 */
  domElement: HTMLElement;
  /** 可选对象列表（不传则检测场景所有 Mesh） */
  selectableObjects?: THREE.Object3D[];
  /** 显式场景引用（退化检测时使用；不传则回退 camera.parent——相机未加入场景时为空导致永不命中） */
  scene?: THREE.Scene;
  /** 高亮颜色 */
  highlightColor?: number;
}

/** 选择事件回调 */
export interface SelectorCallbacks {
  /** 选中对象 */
  onSelect?: (object: THREE.Object3D | null) => void;
  /** 悬停对象 */
  onHover?: (object: THREE.Object3D | null) => void;
  /** 框选完成 */
  onBoxSelect?: (objects: THREE.Object3D[]) => void;
}

/** 框选状态 */
interface BoxSelectState {
  active: boolean;
  startX: number;
  startY: number;
  currentX: number;
  currentY: number;
}

/**
 * 射线选择器
 * 支持单击选择、悬停高亮、框选（Frustum）。
 */
export class RaycastSelector {
  private camera: THREE.PerspectiveCamera;
  private domElement: HTMLElement;
  private callbacks: SelectorCallbacks;
  private selectableObjects: THREE.Object3D[] | null;
  /** 显式场景引用（优先于 camera.parent 退化路径） */
  private scene: THREE.Scene | null;

  /** 射线检测器 */
  private raycaster: THREE.Raycaster;
  /** 鼠标 NDC 坐标 */
  private mouse: THREE.Vector2;

  /** 当前选中对象 */
  private selectedObject: THREE.Object3D | null = null;
  /** 原始材质缓存（高亮前） */
  private originalMaterials: Map<THREE.Object3D, THREE.Material | THREE.Material[]> = new Map();
  /** 高亮颜色 */
  private highlightColor: number;
  /** 高亮材质 */
  private highlightMaterial: THREE.MeshBasicMaterial;

  /** 框选状态 */
  private boxSelect: BoxSelectState = {
    active: false,
    startX: 0,
    startY: 0,
    currentX: 0,
    currentY: 0,
  };

  /** 框选遮罩元素 */
  private boxOverlay: HTMLDivElement;
  /** 是否启用框选模式 */
  private boxSelectMode = false;

  /** 是否启用 */
  private enabled = true;
  /** 是否已销毁 */
  private disposed = false;

  constructor(config: SelectorConfig, callbacks: SelectorCallbacks = {}) {
    this.camera = config.camera;
    this.domElement = config.domElement;
    this.callbacks = callbacks;
    this.selectableObjects = config.selectableObjects || null;
    this.scene = config.scene ?? null;
    this.highlightColor = config.highlightColor ?? 0xF8A5C2; // 樱花粉

    this.raycaster = new THREE.Raycaster();
    this.mouse = new THREE.Vector2();

    // 高亮材质（线框模式）
    this.highlightMaterial = new THREE.MeshBasicMaterial({
      color: this.highlightColor,
      wireframe: true,
      transparent: true,
      opacity: 0.6,
    });

    // 创建框选遮罩
    this.boxOverlay = document.createElement('div');
    this.boxOverlay.style.cssText = `
      position: absolute;
      border: 2px dashed #F8A5C2;
      background: rgba(248, 165, 194, 0.1);
      pointer-events: none;
      display: none;
      z-index: 1000;
    `;

    this.bindEvents();
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

  /** 获取可检测对象 */
  private getTargets(): THREE.Object3D[] {
    if (this.selectableObjects && this.selectableObjects.length > 0) {
      return this.selectableObjects;
    }
    // 退化：检测场景所有 Mesh（排除辅助对象）。
    // 优先显式注入的 scene；camera.parent 仅在相机被加入场景时非空（COMIC-079 修复）。
    const scene = this.scene ?? this.camera.parent;
    if (!scene) return [];
    const targets: THREE.Object3D[] = [];
    scene.traverse((obj) => {
      if (obj instanceof THREE.Mesh && !obj.name.startsWith('__')) {
        targets.push(obj);
      }
    });
    return targets;
  }

  /** 指针按下 */
  private onPointerDown = (event: PointerEvent): void => {
    if (!this.enabled || this.disposed) return;

    if (event.button === 0) {
      // 左键
      if (this.boxSelectMode) {
        // 框选模式：记录起点
        this.boxSelect.active = true;
        this.boxSelect.startX = event.clientX;
        this.boxSelect.startY = event.clientY;
        this.boxSelect.currentX = event.clientX;
        this.boxSelect.currentY = event.clientY;
        this.showBoxOverlay();
      } else {
        // 单击选择
        this.updateMouseNDC(event);
        this.handleClick();
      }
    }
  };

  /** 处理单击选择 */
  private handleClick(): void {
    this.raycaster.setFromCamera(this.mouse, this.camera);
    const targets = this.getTargets();
    const intersects = this.raycaster.intersectObjects(targets, true);

    // 过滤辅助对象
    const validIntersect = intersects.find((i) => {
      let obj: THREE.Object3D | null = i.object;
      while (obj) {
        if (obj.name.startsWith('__') || obj.name.startsWith('gizmo_')) return false;
        obj = obj.parent;
      }
      return true;
    });

    if (validIntersect) {
      // 找到最顶层可选中对象
      let target: THREE.Object3D = validIntersect.object;
      while (target.parent && target.parent.type !== 'Scene') {
        target = target.parent;
      }
      this.select(target);
    } else {
      // 点击空白处取消选择
      this.select(null);
    }
  }

  /** 指针移动 */
  private onPointerMove = (event: PointerEvent): void => {
    if (!this.enabled || this.disposed) return;

    if (this.boxSelect.active) {
      // 更新框选区域
      this.boxSelect.currentX = event.clientX;
      this.boxSelect.currentY = event.clientY;
      this.updateBoxOverlay();
    } else {
      // 悬停检测
      this.updateMouseNDC(event);
      this.raycaster.setFromCamera(this.mouse, this.camera);
      const targets = this.getTargets();
      const intersects = this.raycaster.intersectObjects(targets, true);

      const validIntersect = intersects.find((i) => {
        let obj: THREE.Object3D | null = i.object;
        while (obj) {
          if (obj.name.startsWith('__') || obj.name.startsWith('gizmo_')) return false;
          obj = obj.parent;
        }
        return true;
      });

      if (validIntersect) {
        let target: THREE.Object3D = validIntersect.object;
        while (target.parent && target.parent.type !== 'Scene') {
          target = target.parent;
        }
        this.callbacks.onHover?.(target);
        this.domElement.style.cursor = 'pointer';
      } else {
        this.callbacks.onHover?.(null);
        this.domElement.style.cursor = 'default';
      }
    }
  };

  /** 指针释放 */
  private onPointerUp = (event: PointerEvent): void => {
    if (!this.enabled || this.disposed) return;

    if (this.boxSelect.active && event.button === 0) {
      this.boxSelect.active = false;
      this.hideBoxOverlay();
      this.performBoxSelect();
    }
  };

  /** 执行框选 */
  private performBoxSelect(): void {
    const rect = this.domElement.getBoundingClientRect();
    const minX = Math.min(this.boxSelect.startX, this.boxSelect.currentX) - rect.left;
    const maxX = Math.max(this.boxSelect.startX, this.boxSelect.currentX) - rect.left;
    const minY = Math.min(this.boxSelect.startY, this.boxSelect.currentY) - rect.top;
    const maxY = Math.max(this.boxSelect.startY, this.boxSelect.currentY) - rect.top;

    // 太小的框选视为单击
    if (maxX - minX < 5 || maxY - minY < 5) {
      this.updateMouseNDC({ clientX: this.boxSelect.startX, clientY: this.boxSelect.startY } as PointerEvent);
      this.handleClick();
      return;
    }

    // 转换为 NDC 坐标
    const ndcMinX = (minX / rect.width) * 2 - 1;
    const ndcMaxX = (maxX / rect.width) * 2 - 1;
    const ndcMinY = -((maxY / rect.height) * 2 - 1);
    const ndcMaxY = -((minY / rect.height) * 2 - 1);

    // 使用 Frustum 进行框选
    const frustum = new THREE.Frustum();
    const projScreenMatrix = new THREE.Matrix4();
    projScreenMatrix.multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse);

    // 构建选择区域的投影矩阵
    const selectionMatrix = new THREE.Matrix4().makeOrthographic(
      ndcMinX, ndcMaxX, ndcMaxY, ndcMinY, 0, 1,
    );
    projScreenMatrix.multiply(selectionMatrix);
    frustum.setFromProjectionMatrix(projScreenMatrix);

    // 检测哪些对象在框选区域内
    const targets = this.getTargets();
    const selected: THREE.Object3D[] = [];
    const bbox = new THREE.Box3();

    targets.forEach((obj) => {
      bbox.setFromObject(obj);
      if (frustum.intersectsBox(bbox)) {
        selected.push(obj);
      }
    });

    this.callbacks.onBoxSelect?.(selected);
  }

  /** 显示框选遮罩 */
  private showBoxOverlay(): void {
    if (!this.boxOverlay.parentNode) {
      this.domElement.parentElement?.appendChild(this.boxOverlay);
    }
    this.boxOverlay.style.display = 'block';
    this.updateBoxOverlay();
  }

  /** 更新框选遮罩位置 */
  private updateBoxOverlay(): void {
    const rect = this.domElement.getBoundingClientRect();
    const left = Math.min(this.boxSelect.startX, this.boxSelect.currentX);
    const top = Math.min(this.boxSelect.startY, this.boxSelect.currentY);
    const width = Math.abs(this.boxSelect.currentX - this.boxSelect.startX);
    const height = Math.abs(this.boxSelect.currentY - this.boxSelect.startY);

    this.boxOverlay.style.left = (left - rect.left) + 'px';
    this.boxOverlay.style.top = (top - rect.top) + 'px';
    this.boxOverlay.style.width = width + 'px';
    this.boxOverlay.style.height = height + 'px';
  }

  /** 隐藏框选遮罩 */
  private hideBoxOverlay(): void {
    this.boxOverlay.style.display = 'none';
  }

  /**
   * 选中对象
   * @param object 要选中的对象，null 取消选择
   */
  public select(object: THREE.Object3D | null): void {
    // 恢复之前选中对象的原材质
    this.clearHighlight();

    this.selectedObject = object;

    if (object) {
      this.applyHighlight(object);
    }

    this.callbacks.onSelect?.(object);
  }

  /** 应用高亮 */
  private applyHighlight(object: THREE.Object3D): void {
    object.traverse((child) => {
      if (child instanceof THREE.Mesh) {
        // 保存原始材质
        this.originalMaterials.set(child, child.material);
        // 创建高亮材质副本
        const highlightMat = this.highlightMaterial.clone();
        child.material = highlightMat;
      }
    });
  }

  /** 清除高亮 */
  private clearHighlight(): void {
    this.originalMaterials.forEach((material, obj) => {
      if (obj instanceof THREE.Mesh) {
        // 释放高亮材质
        if (obj.material !== material && !Array.isArray(obj.material)) {
          obj.material.dispose();
        }
        obj.material = material;
      }
    });
    this.originalMaterials.clear();
  }

  /** 获取当前选中对象 */
  public getSelected(): THREE.Object3D | null {
    return this.selectedObject;
  }

  /** 设置可选对象列表 */
  public setSelectable(objects: THREE.Object3D[]): void {
    this.selectableObjects = objects;
  }

  /** 启用/禁用框选模式 */
  public setBoxSelectMode(enabled: boolean): void {
    this.boxSelectMode = enabled;
    this.domElement.style.cursor = enabled ? 'crosshair' : 'default';
  }

  /** 设置启用/禁用 */
  public setEnabled(enabled: boolean): void {
    this.enabled = enabled;
    if (!enabled) {
      this.select(null);
    }
  }

  /** 销毁 */
  public dispose(): void {
    this.disposed = true;
    this.clearHighlight();
    this.highlightMaterial.dispose();

    this.domElement.removeEventListener('pointerdown', this.onPointerDown);
    this.domElement.removeEventListener('pointermove', this.onPointerMove);
    this.domElement.removeEventListener('pointerup', this.onPointerUp);

    if (this.boxOverlay.parentNode) {
      this.boxOverlay.parentNode.removeChild(this.boxOverlay);
    }
  }
}

export default RaycastSelector;
