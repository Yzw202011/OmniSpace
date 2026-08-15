/* ==========================================================================
 * SceneManager.ts —— Three.js 场景管理器（规格 §5.6, §5.7）
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170（不可升级：r182+ 自动切 WebGPU）
 * 职责：
 *   - 创建 Scene / PerspectiveCamera / WebGLRenderer（WebGL2）
 *   - 灯光设置（环境光 + 方向光 + 半球光）
 *   - 动画循环管理（requestAnimationFrame）
 *   - 窗口尺寸自适应
 *   - dispose 资源清理
 * ========================================================================== */

import * as THREE from 'three';

/** 场景管理器配置 */
export interface SceneManagerConfig {
  /** 容器元素 */
  container: HTMLElement;
  /** 初始相机视野角度（FOV），默认 50 */
  fov?: number;
  /** 近裁面，默认 0.1 */
  near?: number;
  /** 远裁面，默认 1000 */
  far?: number;
  /** 相机初始位置 */
  cameraPosition?: [number, number, number];
  /** 渲染器清除色 */
  clearColor?: number;
  /** 是否开启抗锯齿 */
  antialias?: boolean;
  /** 是否启用阴影 */
  shadowMap?: boolean;
  /** 像素比上限（防止高 DPI 设备过载） */
  maxPixelRatio?: number;
}

/**
 * Three.js 场景管理器
 * 封装 Scene、Camera、Renderer 的创建与生命周期管理。
 */
export class SceneManager {
  /** 场景对象 */
  public scene: THREE.Scene;
  /** 透视相机 */
  public camera: THREE.PerspectiveCamera;
  /** WebGL 渲染器（WebGL2） */
  public renderer: THREE.WebGLRenderer;
  /** 容器元素 */
  public container: HTMLElement;
  /** 时钟 */
  public clock: THREE.Clock;

  /** 动画帧 ID */
  private animationId: number | null = null;
  /** 每帧回调列表 */
  private updateCallbacks: Array<(delta: number, elapsed: number) => void> = [];
  /** 尺寸变化观察器 */
  private resizeObserver: ResizeObserver | null = null;
  /** 是否已销毁 */
  private disposed = false;

  constructor(config: SceneManagerConfig) {
    const {
      container,
      fov = 50,
      near = 0.1,
      far = 1000,
      cameraPosition = [5, 5, 10],
      clearColor = 0xf5edef,
      antialias = true,
      shadowMap = true,
      maxPixelRatio = 2,
    } = config;

    this.container = container;
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 600;

    // ---- 创建场景 ----
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(clearColor);
    this.scene.fog = new THREE.Fog(clearColor, 20, 100);

    // ---- 创建相机 ----
    this.camera = new THREE.PerspectiveCamera(fov, width / height, near, far);
    this.camera.position.set(cameraPosition[0], cameraPosition[1], cameraPosition[2]);
    this.camera.lookAt(0, 0, 0);

    // ---- 创建渲染器（强制 WebGL2） ----
    const canvas = document.createElement('canvas');
    const glContext = canvas.getContext('webgl2', {
      antialias,
      alpha: true,
      powerPreference: 'high-performance',
      preserveDrawingBuffer: true, // 截图需要
    });
    if (!glContext) {
      throw new Error('WebGL2 不可用，请使用支持 WebGL2 的浏览器');
    }
    this.renderer = new THREE.WebGLRenderer({
      canvas,
      context: glContext as WebGLRenderingContext,
      antialias,
      alpha: true,
      preserveDrawingBuffer: true,
    });
    this.renderer.setSize(width, height);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, maxPixelRatio));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;

    if (shadowMap) {
      this.renderer.shadowMap.enabled = true;
      this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    }

    // 将 canvas 挂载到容器
    this.renderer.domElement.style.display = 'block';
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    container.appendChild(this.renderer.domElement);

    // ---- 时钟 ----
    this.clock = new THREE.Clock();

    // ---- 设置灯光 ----
    this.setupLights();

    // ---- 窗口尺寸自适应 ----
    this.setupResize();
  }

  /** 设置灯光（环境光 + 方向光 + 半球光） */
  private setupLights(): void {
    // 环境光：提供基础照明
    const ambient = new THREE.AmbientLight(0xffffff, 0.4);
    this.scene.add(ambient);

    // 半球光：模拟天空与地面反射
    const hemi = new THREE.HemisphereLight(0xfff0f5, 0xefe0e5, 0.3);
    hemi.position.set(0, 20, 0);
    this.scene.add(hemi);

    // 方向光：主光源，投射阴影
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(10, 15, 8);
    dirLight.castShadow = true;
    dirLight.shadow.mapSize.width = 2048;
    dirLight.shadow.mapSize.height = 2048;
    dirLight.shadow.camera.near = 0.5;
    dirLight.shadow.camera.far = 50;
    dirLight.shadow.camera.left = -20;
    dirLight.shadow.camera.right = 20;
    dirLight.shadow.camera.top = 20;
    dirLight.shadow.camera.bottom = -20;
    dirLight.shadow.bias = -0.0005;
    this.scene.add(dirLight);

    // 辅助方向光：填补阴影面
    const fillLight = new THREE.DirectionalLight(0xf8a5c2, 0.15);
    fillLight.position.set(-8, 5, -5);
    this.scene.add(fillLight);
  }

  /** 窗口尺寸自适应（使用 ResizeObserver） */
  private setupResize(): void {
    const onResize = () => {
      if (this.disposed) return;
      const w = this.container.clientWidth;
      const h = this.container.clientHeight;
      if (w > 0 && h > 0) {
        this.camera.aspect = w / h;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(w, h);
      }
    };

    // ResizeObserver 优于 window resize（容器尺寸变化不一定触发 window resize）
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(onResize);
      this.resizeObserver.observe(this.container);
    }
    window.addEventListener('resize', onResize);
    // 保存引用以便清理
    (this as unknown as { _onResize: () => void })._onResize = onResize;
  }

  /** 添加每帧更新回调 */
  public onUpdate(callback: (delta: number, elapsed: number) => void): void {
    this.updateCallbacks.push(callback);
  }

  /** 移除更新回调 */
  public removeUpdate(callback: (delta: number, elapsed: number) => void): void {
    const idx = this.updateCallbacks.indexOf(callback);
    if (idx >= 0) this.updateCallbacks.splice(idx, 1);
  }

  /** 启动动画循环 */
  public start(): void {
    if (this.animationId !== null) return;
    const animate = () => {
      if (this.disposed) return;
      this.animationId = requestAnimationFrame(animate);
      const delta = this.clock.getDelta();
      const elapsed = this.clock.getElapsedTime();
      // 执行所有更新回调
      for (const cb of this.updateCallbacks) {
        try {
          cb(delta, elapsed);
        } catch {
          // 单个回调异常不中断循环
        }
      }
      this.renderer.render(this.scene, this.camera);
    };
    animate();
  }

  /** 停止动画循环 */
  public stop(): void {
    if (this.animationId !== null) {
      cancelAnimationFrame(this.animationId);
      this.animationId = null;
    }
  }

  /** 添加对象到场景 */
  public add(object: THREE.Object3D): void {
    this.scene.add(object);
  }

  /** 从场景移除对象 */
  public remove(object: THREE.Object3D): void {
    this.scene.remove(object);
  }

  /** 手动渲染一帧（静态截图模式用） */
  public renderOnce(): void {
    this.renderer.render(this.scene, this.camera);
  }

  /** 获取渲染器 canvas */
  public getCanvas(): HTMLCanvasElement {
    return this.renderer.domElement;
  }

  /** 截图（返回 DataURL） */
  public screenshot(mimeType = 'image/png', quality = 0.95): string {
    this.renderer.render(this.scene, this.camera);
    return this.renderer.domElement.toDataURL(mimeType, quality);
  }

  /** 销毁：停止循环、移除事件、释放资源 */
  public dispose(): void {
    this.disposed = true;
    this.stop();

    // 移除 ResizeObserver
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    // 移除 window resize
    const onResize = (this as unknown as { _onResize?: () => void })._onResize;
    if (onResize) window.removeEventListener('resize', onResize);

    // 遍历场景释放几何体与材质
    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (mesh.geometry) mesh.geometry.dispose();
      if (mesh.material) {
        const mat = mesh.material;
        if (Array.isArray(mat)) {
          mat.forEach((m) => m.dispose());
        } else {
          mat.dispose();
        }
      }
    });

    // 释放渲染器
    this.renderer.dispose();
    // 移除 canvas
    if (this.renderer.domElement.parentNode) {
      this.renderer.domElement.parentNode.removeChild(this.renderer.domElement);
    }

    // 清空回调
    this.updateCallbacks = [];
  }
}

export default SceneManager;
