/* ==========================================================================
 * SkeletonAnimator.ts —— 骨骼动画管理器
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170
 * 职责：
 *   - 加载 GLTF 模型（含骨骼与动画）
 *   - 播放/暂停/停止动画
 *   - 姿态预设切换（15 种预设关节旋转）
 *   - 手动骨骼旋转控制
 * ========================================================================== */

import * as THREE from 'three';

/** 姿态预设关节定义 */
export interface PosePreset {
  /** 姿态名称 */
  name: string;
  /** 关节旋转参数（关节名 → [x, y, z] 弧度） */
  joints: Record<string, [number, number, number]>;
}

/** 骨骼关节名称列表 */
export const JOINT_NAMES = [
  'hips', 'spine', 'chest', 'neck', 'head',
  'shoulderL', 'upperArmL', 'lowerArmL', 'handL',
  'shoulderR', 'upperArmR', 'lowerArmR', 'handR',
  'upperLegL', 'lowerLegL', 'footL',
  'upperLegR', 'lowerLegR', 'footR',
] as const;

/** 15 种预设姿态 */
export const POSE_PRESETS: PosePreset[] = [
  { name: '站立', joints: {} },
  { name: '坐姿', joints: { upperLegL: [-1.5, 0, 0], upperLegR: [-1.5, 0, 0], lowerLegL: [1.5, 0, 0], lowerLegR: [1.5, 0, 0], spine: [0.1, 0, 0] } },
  { name: '举手', joints: { upperArmR: [0, 0, -2.8], lowerArmR: [0, 0, -0.3], head: [-0.2, 0, 0] } },
  { name: '叉腰', joints: { upperArmL: [0, 0, 1.1], lowerArmL: [0, 0, 2.2], upperArmR: [0, 0, -1.1], lowerArmR: [0, 0, -2.2] } },
  { name: '行走', joints: { upperLegL: [-0.5, 0, 0], upperLegR: [0.5, 0, 0], lowerLegL: [0.3, 0, 0], lowerLegR: [0.1, 0, 0], upperArmL: [0.4, 0, 0], upperArmR: [-0.4, 0, 0] } },
  { name: '跪姿', joints: { upperLegL: [-1.2, 0, 0], upperLegR: [-1.2, 0, 0], lowerLegL: [2.1, 0, 0], lowerLegR: [2.1, 0, 0], spine: [0.15, 0, 0], head: [0.3, 0, 0] } },
  { name: '躺姿', joints: { hips: [1.57, 0, 0], spine: [0.05, 0, 0], upperLegL: [0.05, 0, 0], upperLegR: [0.05, 0, 0] } },
  { name: '指向', joints: { upperArmR: [-1.5, 0, 0], lowerArmR: [0, 0, 0], head: [0, -0.3, 0] } },
  { name: '拥抱', joints: { upperArmL: [-1.2, 0.5, 0.6], lowerArmL: [0, 0.8, 0], upperArmR: [-1.2, -0.5, -0.6], lowerArmR: [0, -0.8, 0], spine: [0.1, 0, 0] } },
  { name: '奔跑', joints: { upperLegL: [-0.9, 0, 0], lowerLegL: [1.2, 0, 0], upperLegR: [0.7, 0, 0], lowerLegR: [0.4, 0, 0], upperArmL: [0.8, 0, 0], lowerArmL: [-0.9, 0, 0], upperArmR: [-0.8, 0, 0], lowerArmR: [-0.9, 0, 0], spine: [0.25, 0, 0] } },
  { name: '鞠躬', joints: { spine: [0.8, 0, 0], chest: [0.4, 0, 0], head: [0.3, 0, 0] } },
  { name: '挥手', joints: { upperArmR: [0, 0, -2.4], lowerArmR: [0, 0, -0.6], head: [0, 0, -0.1] } },
  { name: '抱臂', joints: { upperArmL: [0, 0.8, 1.0], lowerArmL: [0, -0.9, 2.2], upperArmR: [0, -0.8, -1.0], lowerArmR: [0, 0.9, -2.2], chest: [0.05, 0, 0] } },
  { name: '低头', joints: { head: [0.6, 0, 0], neck: [0.3, 0, 0] } },
  { name: '仰头', joints: { head: [-0.5, 0, 0], neck: [-0.2, 0, 0] } },
];

/** 动画状态 */
export type AnimationState = 'idle' | 'playing' | 'paused';

/**
 * 骨骼动画管理器
 * 加载 GLTF 模型，管理骨骼动画与姿态预设。
 */
export class SkeletonAnimator {
  /** GLTF 模型根对象 */
  private model: THREE.Object3D | null = null;
  /** 动画混合器 */
  private mixer: THREE.AnimationMixer | null = null;
  /** 动画动作映射 */
  private actions: Map<string, THREE.AnimationAction> = new Map();
  /** 骨骼映射（关节名 → Bone 对象） */
  private bones: Map<string, THREE.Bone> = new Map();
  /** 当前播放的动作 */
  private currentAction: THREE.AnimationAction | null = null;
  /** 当前动画状态 */
  private state: AnimationState = 'idle';
  /** 当前姿态名称 */
  private currentPose: string = '';
  /** 时钟（用于更新混合器） */
  private clock: THREE.Clock;

  constructor() {
    this.clock = new THREE.Clock();
  }

  /**
   * 加载 GLTF 模型
   * @param loader GLTFLoader 实例（从 three/examples 加载）
   * @param url GLTF 文件 URL
   * @returns 加载完成的模型
   */
  public async loadModel(loader: { loadAsync: (url: string) => Promise<unknown> }, url: string): Promise<THREE.Object3D> {
    const gltf = (await loader.loadAsync(url)) as {
      scene: THREE.Object3D;
      animations: THREE.AnimationClip[];
    };

    this.model = gltf.scene;

    // 初始化动画混合器
    if (gltf.animations && gltf.animations.length > 0) {
      this.mixer = new THREE.AnimationMixer(this.model);
      gltf.animations.forEach((clip) => {
        const action = this.mixer!.clipAction(clip);
        this.actions.set(clip.name, action);
      });
    }

    // 收集骨骼
    this.collectBones();

    return this.model;
  }

  /** 收集所有骨骼并建立名称映射 */
  private collectBones(): void {
    this.bones.clear();
    if (!this.model) return;

    this.model.traverse((obj) => {
      if (obj instanceof THREE.Bone) {
        // 尝试匹配关节名称
        const name = obj.name.toLowerCase();
        // 模糊匹配：去除空格和下划线
        const normalized = name.replace(/[\s_]/g, '').toLowerCase();

        for (const jointName of JOINT_NAMES) {
          const normalizedJoint = jointName.toLowerCase();
          if (normalized === normalizedJoint || normalized.includes(normalizedJoint)) {
            this.bones.set(jointName, obj);
            break;
          }
        }

        // 也保存原始名称
        if (!this.bones.has(obj.name)) {
          this.bones.set(obj.name, obj);
        }
      }
    });
  }

  /**
   * 播放动画
   * @param name 动画名称（不传则播放第一个）
   */
  public play(name?: string): void {
    if (!this.mixer) return;

    const actionName = name || Array.from(this.actions.keys())[0];
    if (!actionName) return;

    const action = this.actions.get(actionName);
    if (!action) return;

    // 停止当前动画
    if (this.currentAction && this.currentAction !== action) {
      this.currentAction.fadeOut(0.3);
    }

    action.reset();
    action.setEffectiveWeight(1);
    action.setEffectiveTimeScale(1);
    action.fadeIn(0.3).play();

    this.currentAction = action;
    this.state = 'playing';
  }

  /** 暂停动画 */
  public pause(): void {
    if (this.currentAction) {
      this.currentAction.paused = true;
      this.state = 'paused';
    }
  }

  /** 恢复动画 */
  public resume(): void {
    if (this.currentAction) {
      this.currentAction.paused = false;
      this.state = 'playing';
    }
  }

  /** 停止所有动画 */
  public stop(): void {
    this.actions.forEach((action) => action.stop());
    this.currentAction = null;
    this.state = 'idle';
  }

  /**
   * 应用姿态预设
   * @param poseName 姿态名称
   */
  public applyPose(poseName: string): void {
    const preset = POSE_PRESETS.find((p) => p.name === poseName);
    if (!preset) return;

    // 先重置所有骨骼旋转
    this.resetPose();

    // 应用预设关节旋转
    for (const [jointName, rotation] of Object.entries(preset.joints)) {
      const bone = this.bones.get(jointName);
      if (bone) {
        bone.rotation.set(rotation[0], rotation[1], rotation[2]);
      }
    }

    this.currentPose = poseName;
  }

  /** 重置所有骨骼到默认姿态 */
  public resetPose(): void {
    this.bones.forEach((bone) => {
      bone.rotation.set(0, 0, 0);
    });
    this.currentPose = '';
  }

  /**
   * 设置单个关节旋转
   * @param jointName 关节名称
   * @param rotation [x, y, z] 弧度
   */
  public setJointRotation(jointName: string, rotation: [number, number, number]): void {
    const bone = this.bones.get(jointName);
    if (bone) {
      bone.rotation.set(rotation[0], rotation[1], rotation[2]);
    }
  }

  /**
   * 获取关节旋转
   * @param jointName 关节名称
   * @returns [x, y, z] 弧度
   */
  public getJointRotation(jointName: string): [number, number, number] | null {
    const bone = this.bones.get(jointName);
    if (!bone) return null;
    return [bone.rotation.x, bone.rotation.y, bone.rotation.z];
  }

  /** 获取所有可用动画名称 */
  public getAnimationNames(): string[] {
    return Array.from(this.actions.keys());
  }

  /** 获取所有可用姿态名称 */
  public getPoseNames(): string[] {
    return POSE_PRESETS.map((p) => p.name);
  }

  /** 获取当前动画状态 */
  public getState(): AnimationState {
    return this.state;
  }

  /** 获取当前姿态名称 */
  public getCurrentPose(): string {
    return this.currentPose;
  }

  /** 获取模型 */
  public getModel(): THREE.Object3D | null {
    return this.model;
  }

  /** 获取骨骼映射 */
  public getBones(): Map<string, THREE.Bone> {
    return this.bones;
  }

  /** 每帧更新（需在动画循环中调用） */
  public update(): void {
    if (this.mixer && this.state === 'playing') {
      const delta = this.clock.getDelta();
      this.mixer.update(delta);
    }
  }

  /** 销毁 */
  public dispose(): void {
    this.stop();
    if (this.mixer) {
      this.mixer.uncacheRoot(this.model as THREE.Object3D);
      this.mixer = null;
    }
    this.actions.clear();
    this.bones.clear();
    this.model = null;
    this.currentAction = null;
    this.state = 'idle';
  }
}

export default SkeletonAnimator;
