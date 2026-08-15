/* ==========================================================================
 * CubeCameraPanorama.ts —— 720 全景图生成（规格 §5.6）
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170
 * 职责：
 *   - 使用 CubeCamera 渲染场景 6 面（立方体贴图）
 *   - 通过 ShaderMaterial 将立方体贴图转换为等距柱状投影（Equirectangular）
 *   - 支持分辨率：1024 | 2048 | 4096 | 8192
 *   - 输出全景图 DataURL 供 PanoramaViewer 显示
 * ========================================================================== */

import * as THREE from 'three';

/** 全景图分辨率选项 */
export type PanoramaResolution = 1024 | 2048 | 4096 | 8192;

/** 全景图生成配置 */
export interface PanoramaConfig {
  /** 等距柱状投影输出分辨率（宽=2×高） */
  resolution?: PanoramaResolution;
  /** 近裁面 */
  near?: number;
  /** 远裁面 */
  far?: number;
}

/**
 * 720 全景图生成器
 * 使用 CubeCamera 捕获 6 面环境贴图，再通过 ShaderMaterial 投影转换为等距柱状全景图。
 */
export class CubeCameraPanorama {
  /** CubeCamera 实例 */
  private cubeCamera: THREE.CubeCamera;
  /** 立方体渲染目标 */
  private cubeTarget: THREE.WebGLCubeRenderTarget;
  /** 用于投影转换的全屏四边形场景 */
  private projectionScene: THREE.Scene;
  /** 投影转换相机（正交，全屏） */
  private projectionCamera: THREE.OrthographicCamera;
  /** 投影转换材质（ShaderMaterial） */
  private projectionMaterial: THREE.ShaderMaterial;
  /** 离屏渲染器 */
  private offscreenRenderer: THREE.WebGLRenderer;
  /** 输出画布 */
  private outputCanvas: HTMLCanvasElement;
  /** 输出画布上下文 */
  private outputCtx: CanvasRenderingContext2D;
  /** 分辨率 */
  private resolution: PanoramaResolution;
  /** 是否已销毁 */
  private disposed = false;

  constructor(renderer: THREE.WebGLRenderer, config: PanoramaConfig = {}) {
    const { resolution = 2048, near = 0.1, far = 1000 } = config;
    this.resolution = resolution;

    // ---- 创建立方体渲染目标 ----
    // CubeCamera 的 size 应为正方形，取输出分辨率的 1/2 作为每面尺寸
    const cubeSize = Math.floor(resolution / 2);
    this.cubeTarget = new THREE.WebGLCubeRenderTarget(cubeSize, {
      type: THREE.HalfFloatType,
      minFilter: THREE.LinearMipmapLinearFilter,
      magFilter: THREE.LinearFilter,
    });

    this.cubeCamera = new THREE.CubeCamera(near, far, this.cubeTarget);

    // ---- 创建离屏渲染器（用于投影转换渲染） ----
    this.offscreenRenderer = renderer; // 复用传入的渲染器

    // ---- 创建投影转换场景 ----
    // 全屏四边形 + 正交相机
    this.projectionScene = new THREE.Scene();
    this.projectionCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

    // ---- 等距柱状投影转换 Shader ----
    // 将立方体贴图（CubeMap）转换为 2:1 等距柱状投影
    this.projectionMaterial = new THREE.ShaderMaterial({
      uniforms: {
        tCube: { value: this.cubeTarget.texture },
        uResolution: { value: new THREE.Vector2(resolution * 2, resolution) },
      },
      vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
          vUv = uv;
          gl_Position = vec4(position.xy, 0.0, 1.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform samplerCube tCube;
        uniform vec2 uResolution;
        varying vec2 vUv;

        const float PI = 3.14159265359;

        void main() {
          // vUv: [0,1] x [0,1]，y 从下到上
          // 等距柱状投影：u -> 经度 [-PI, PI]，v -> 纬度 [PI/2, -PI/2]
          float theta = (vUv.x - 0.5) * 2.0 * PI;   // 经度：-PI ~ PI
          float phi = (0.5 - vUv.y) * PI;            // 纬度：PI/2 ~ -PI/2

          // 转换为立方体方向向量
          vec3 dir;
          dir.x = cos(phi) * sin(theta);
          dir.y = sin(phi);
          dir.z = cos(phi) * cos(theta);

          // 从立方体贴图采样
          vec4 color = textureCube(tCube, dir);
          gl_FragColor = color;
        }
      `,
    });

    const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.projectionMaterial);
    this.projectionScene.add(quad);

    // ---- 创建输出画布 ----
    this.outputCanvas = document.createElement('canvas');
    this.outputCanvas.width = resolution * 2; // 等距柱状投影宽高比 2:1
    this.outputCanvas.height = resolution;
    this.outputCtx = this.outputCanvas.getContext('2d')!;
  }

  /**
   * 生成全景图
   * @param scene 要捕获的场景
   * @param position 捕获位置（相机所在点）
   * @returns 全景图 DataURL（PNG）
   */
  public generate(scene: THREE.Scene, position: THREE.Vector3 = new THREE.Vector3(0, 0, 0)): string {
    if (this.disposed) return '';

    // ---- 1. 更新 CubeCamera 位置 ----
    this.cubeCamera.position.copy(position);
    this.cubeCamera.updateMatrixWorld();

    // 隐藏可能干扰的辅助对象（网格、灯光辅助线等）
    const hiddenObjects: THREE.Object3D[] = [];
    scene.traverse((obj) => {
      if (obj.visible && (obj as THREE.Mesh).material instanceof THREE.LineBasicMaterial) {
        hiddenObjects.push(obj);
        obj.visible = false;
      }
    });

    // ---- 2. CubeCamera 渲染 6 面 ----
    this.cubeCamera.update(this.offscreenRenderer, scene);

    // 恢复隐藏的对象
    hiddenObjects.forEach((obj) => { obj.visible = true; });

    // ---- 3. 设置渲染目标为输出尺寸 ----
    const renderTarget = new THREE.WebGLRenderTarget(this.resolution * 2, this.resolution, {
      type: THREE.UnsignedByteType,
      minFilter: THREE.LinearFilter,
      magFilter: THREE.LinearFilter,
    });

    const originalRenderTarget = this.offscreenRenderer.getRenderTarget();
    const originalSize = new THREE.Vector2();
    this.offscreenRenderer.getSize(originalSize);

    this.offscreenRenderer.setRenderTarget(renderTarget);
    this.offscreenRenderer.setSize(this.resolution * 2, this.resolution);
    this.offscreenRenderer.render(this.projectionScene, this.projectionCamera);
    this.offscreenRenderer.setRenderTarget(null);

    // ---- 4. 从 RenderTarget 读取像素到 canvas ----
    const pixels = new Uint8Array(this.resolution * 2 * this.resolution * 4);
    this.offscreenRenderer.readRenderTargetPixels(
      renderTarget,
      0, 0,
      this.resolution * 2, this.resolution,
      pixels,
    );

    // 写入 canvas（注意 Y 轴翻转）
    const imageData = this.outputCtx.createImageData(this.resolution * 2, this.resolution);
    // 翻转 Y 轴：WebGL 底部为 y=0，Canvas 顶部为 y=0
    for (let y = 0; y < this.resolution; y++) {
      const srcRow = (this.resolution - 1 - y) * this.resolution * 2 * 4;
      const dstRow = y * this.resolution * 2 * 4;
      imageData.data.set(
        pixels.subarray(srcRow, srcRow + this.resolution * 2 * 4),
        dstRow,
      );
    }
    this.outputCtx.putImageData(imageData, 0, 0);

    // ---- 5. 清理 ----
    renderTarget.dispose();
    this.offscreenRenderer.setRenderTarget(originalRenderTarget);
    this.offscreenRenderer.setSize(originalSize.x, originalSize.y);

    return this.outputCanvas.toDataURL('image/png');
  }

  /**
   * 设置分辨率
   * @param resolution 新的分辨率
   */
  public setResolution(resolution: PanoramaResolution): void {
    if (this.resolution === resolution) return;
    this.resolution = resolution;

    // 重建 CubeCamera
    this.cubeTarget.dispose();
    const cubeSize = Math.floor(resolution / 2);
    this.cubeTarget = new THREE.WebGLCubeRenderTarget(cubeSize, {
      type: THREE.HalfFloatType,
      minFilter: THREE.LinearMipmapLinearFilter,
      magFilter: THREE.LinearFilter,
    });
    this.cubeCamera = new THREE.CubeCamera(0.1, 1000, this.cubeTarget);

    // 更新 shader uniform
    this.projectionMaterial.uniforms.tCube.value = this.cubeTarget.texture;
    this.projectionMaterial.uniforms.uResolution.value.set(resolution * 2, resolution);

    // 更新输出画布
    this.outputCanvas.width = resolution * 2;
    this.outputCanvas.height = resolution;
  }

  /** 获取输出画布 */
  public getCanvas(): HTMLCanvasElement {
    return this.outputCanvas;
  }

  /** 获取 CubeCamera（可添加到场景中） */
  public getCubeCamera(): THREE.CubeCamera {
    return this.cubeCamera;
  }

  /** 销毁：释放资源 */
  public dispose(): void {
    this.disposed = true;
    this.cubeTarget.dispose();
    this.projectionMaterial.dispose();
    (this.projectionScene.children[0] as THREE.Mesh).geometry.dispose();
  }
}

export default CubeCameraPanorama;
