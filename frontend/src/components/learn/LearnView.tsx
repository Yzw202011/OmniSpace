/* ==========================================================================
 * LearnView.tsx —— 训练任务管理（知识学习页子区块，规格 §9.4）
 * --------------------------------------------------------------------------
 * 数据源：useLearnStore（真实 API：POST /learn/train、GET /learn/tasks、
 *        GET /learn/models、POST /learn/tasks/{id}/cancel）
 * 进度：WS task_progress（useTaskStore）实时合并 + 有活跃任务时 3s 轮询兜底。
 * 说明：
 *   - 后端无暂停/恢复端点，仅提供取消；不再保留伪造状态的暂停/恢复按钮。
 *   - 校验/反馈统一走 useAppStore.showToast（替换原生 alert）。
 * ========================================================================== */

import React, { useState, useEffect } from 'react';
import { Dumbbell } from 'lucide-react';
import { formatPercent, formatRelativeTime } from '@utils/format';
import type { TrainTask } from '@/types';
import { TRAIN_STATUS_CLASSES, TRAIN_STATUS_LABELS } from '@/constants/statusLabels';
import { useLearnStore } from '@/stores/useLearnStore';
import { useTaskStore } from '@/stores/useTaskStore';
import { useAppStore } from '@/stores/useAppStore';

/** 与 learnApi.DEFAULT_BASE_MODEL 对齐的兜底基座 */
const DEFAULT_FORM = {
  name: '',
  base_model: '',
  lora_rank: 16,
  lora_alpha: 32,
  learning_rate: 0.0001,
  epochs: 3,
  dataset_path: '',
};

/** 与 learnApi.DEFAULT_BASE_MODEL 对齐的兜底基座 */
const FALLBACK_BASE_MODEL = 'qwen3-vl-4b';

/**
 * 训练任务管理组件
 * 任务列表 + 创建训练表单，全部接线 useLearnStore 真实 API。
 */
export const LearnView: React.FC = () => {
  /* ------------------------------ store 数据 ------------------------------ */
  const trainTasks = useLearnStore((s) => s.trainTasks);
  const tasksLoaded = useLearnStore((s) => s.tasksLoaded);
  const availableModels = useLearnStore((s) => s.availableModels);
  const fetchTasks = useLearnStore((s) => s.fetchTasks);
  const fetchAvailableModels = useLearnStore((s) => s.fetchAvailableModels);
  const createTrain = useLearnStore((s) => s.createTrain);
  const cancelTask = useLearnStore((s) => s.cancelTask);
  const liveTasks = useTaskStore((s) => s.tasks);
  const showToast = useAppStore((s) => s.showToast);

  /* ------------------------------ 本地状态 ------------------------------ */
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [submitting, setSubmitting] = useState(false);

  /* 挂载：拉取任务列表 + 可训练基座 */
  useEffect(() => {
    fetchTasks();
    fetchAvailableModels();
  }, [fetchTasks, fetchAvailableModels]);

  /* 可训练基座就绪后，校正表单 base_model（优先首个 ready 基座） */
  useEffect(() => {
    if (form.base_model) return;
    const ready = availableModels.find((m) => m.ready) ?? availableModels[0];
    setForm((f) => ({ ...f, base_model: ready?.name ?? FALLBACK_BASE_MODEL }));
  }, [availableModels, form.base_model]);

  /* 有排队/训练中任务时 3s 轮询兜底（WS 之外的进度保障） */
  const hasActive = trainTasks.some((t) => t.status === 'pending' || t.status === 'running');
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => {
      fetchTasks();
    }, 3000);
    return () => clearInterval(timer);
  }, [hasActive, fetchTasks]);

  /** 合并 WS 实时进度（同 id 任务以 useTaskStore 推送为准） */
  const mergedTasks = trainTasks.map((t) => {
    const live = liveTasks.find((lt) => lt.id === t.id);
    if (!live) return t;
    return {
      ...t,
      progress: typeof live.progress === 'number' ? live.progress : t.progress,
      status: (live.status as TrainTask['status'] | undefined) ?? t.status,
      error: live.error ?? t.error,
    };
  });

  /** 提交创建训练 */
  const handleSubmit = async () => {
    if (!form.name.trim()) {
      showToast('请输入任务名称', 'warning');
      return;
    }
    if (!form.dataset_path.trim()) {
      showToast('请输入数据集路径', 'warning');
      return;
    }
    setSubmitting(true);
    try {
      const ok = await createTrain({
        type: 'knowledge_lora',
        name: form.name.trim(),
        priority: 'manual',
        target_model: form.base_model || FALLBACK_BASE_MODEL,
        dataset: form.dataset_path.trim(),
        hyperparams: {
          lora_rank: form.lora_rank,
          lora_alpha: form.lora_alpha,
          learning_rate: form.learning_rate,
          epochs: form.epochs,
        },
      });
      if (ok) {
        showToast('训练任务已创建', 'success');
        setForm(DEFAULT_FORM);
        setShowForm(false);
      }
      // 失败时 createTrain 内部已 toast 错误原因（含功能互斥阻断）
    } finally {
      setSubmitting(false);
    }
  };

  /** 优先级徽章（TrainPriority：manual/auto） */
  const priorityBadge = (priority: TrainTask['priority']) =>
    priority === 'manual'
      ? { text: '手动', cls: 'learn-pri-manual' }
      : { text: '自动', cls: 'learn-pri-auto' };

  /** 取超参数数值（hyperparams 为宽松 Record，缺省回退 --） */
  const hp = (task: TrainTask, key: string): string => {
    const v = task.hyperparams?.[key];
    return typeof v === 'number' ? String(v) : '--';
  };

  return (
    <div className="learn-view-container">
      <div className="learn-header">
        <h2 className="learn-title inline-flex items-center gap-2"><Dumbbell size={18} aria-hidden="true" /> 训练任务</h2>
        <button
          className="btn btn-primary"
          onClick={() => setShowForm(!showForm)}
        >
          {showForm ? '收起表单' : '+ 创建训练任务'}
        </button>
      </div>

      {/* 创建训练任务表单 */}
      {showForm && (
        <div className="learn-form-card card">
          <h3 className="learn-form-title">训练参数配置</h3>
          <div className="learn-form-grid">
            {/* 任务名称 */}
            <div className="learn-form-item">
              <label className="learn-label">任务名称</label>
              <input
                className="input"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：角色知识微调"
              />
            </div>

            {/* 基础模型（来自 /learn/models 可训练基座） */}
            <div className="learn-form-item">
              <label className="learn-label">基础模型</label>
              <select
                className="input"
                value={form.base_model}
                onChange={(e) => setForm({ ...form, base_model: e.target.value })}
              >
                {availableModels.length === 0 ? (
                  <option value={FALLBACK_BASE_MODEL}>{FALLBACK_BASE_MODEL}（默认基座）</option>
                ) : (
                  availableModels.map((m) => (
                    <option key={m.name} value={m.name} disabled={!m.ready}>
                      {m.name}{m.ready ? '' : '（未就绪）'}{m.description ? ` — ${m.description}` : ''}
                    </option>
                  ))
                )}
              </select>
            </div>

            {/* LoRA Rank */}
            <div className="learn-form-item">
              <label className="learn-label">LoRA Rank: {form.lora_rank}</label>
              <input
                type="range"
                min={1}
                max={64}
                value={form.lora_rank}
                onChange={(e) => setForm({ ...form, lora_rank: Number(e.target.value) })}
              />
            </div>

            {/* LoRA Alpha */}
            <div className="learn-form-item">
              <label className="learn-label">LoRA Alpha: {form.lora_alpha}</label>
              <input
                type="range"
                min={1}
                max={128}
                value={form.lora_alpha}
                onChange={(e) => setForm({ ...form, lora_alpha: Number(e.target.value) })}
              />
            </div>

            {/* 学习率 */}
            <div className="learn-form-item">
              <label className="learn-label">学习率</label>
              <select
                className="input"
                value={form.learning_rate}
                onChange={(e) => setForm({ ...form, learning_rate: Number(e.target.value) })}
              >
                <option value={0.00001}>1e-5（保守）</option>
                <option value={0.00005}>5e-5（温和）</option>
                <option value={0.0001}>1e-4（标准）</option>
                <option value={0.0003}>3e-4（激进）</option>
                <option value={0.001}>1e-3（实验）</option>
              </select>
            </div>

            {/* 训练轮次 */}
            <div className="learn-form-item">
              <label className="learn-label">训练轮次: {form.epochs}</label>
              <input
                type="range"
                min={1}
                max={20}
                value={form.epochs}
                onChange={(e) => setForm({ ...form, epochs: Number(e.target.value) })}
              />
            </div>

            {/* 数据集路径 */}
            <div className="learn-form-item learn-form-full">
              <label className="learn-label">数据集路径</label>
              <input
                className="input"
                value={form.dataset_path}
                onChange={(e) => setForm({ ...form, dataset_path: e.target.value })}
                placeholder="如：data/training/uploads/knowledge_20260807.jsonl"
              />
            </div>
          </div>

          <div className="learn-form-actions">
            <button className="btn btn-primary" onClick={handleSubmit} disabled={submitting}>
              {submitting ? '创建中…' : '开始训练'}
            </button>
            <button className="btn btn-secondary" onClick={() => setShowForm(false)}>
              取消
            </button>
          </div>
        </div>
      )}

      {/* 训练任务列表 */}
      <div className="learn-task-list">
        {mergedTasks.length === 0 ? (
          <div className="learn-empty">
            <p>{tasksLoaded ? '暂无训练任务' : '任务加载中…'}</p>
            <p className="text-secondary">点击「创建训练任务」开始</p>
          </div>
        ) : (
          mergedTasks.map((task) => {
            const pri = priorityBadge(task.priority);
            return (
              <div key={task.id} className="learn-task-card card">
                <div className="learn-task-header">
                  <div className="learn-task-title-row">
                    <span className="learn-task-name">{task.name}</span>
                    <span className={`learn-priority-badge ${pri.cls}`}>{pri.text}</span>
                    <span className={`learn-status-badge ${TRAIN_STATUS_CLASSES[task.status]}`}>
                      {TRAIN_STATUS_LABELS[task.status]}
                    </span>
                  </div>
                  <div className="learn-task-meta">
                    {task.target_model && (
                      <span className="learn-meta-item">模型: {task.target_model}</span>
                    )}
                    <span className="learn-meta-item">Rank: {hp(task, 'lora_rank')}</span>
                    <span className="learn-meta-item">Alpha: {hp(task, 'lora_alpha')}</span>
                    <span className="learn-meta-item">LR: {hp(task, 'learning_rate')}</span>
                    <span className="learn-meta-item">Epochs: {hp(task, 'epochs')}</span>
                    <span className="learn-meta-item">创建: {formatRelativeTime(task.created_at)}</span>
                  </div>
                </div>

                {/* 训练进度（排队/训练中） */}
                {(task.status === 'pending' || task.status === 'running') && (
                  <div className="learn-progress-section">
                    <div className="learn-progress-bar-wrapper">
                      <div
                        className="learn-progress-bar"
                        style={{ width: `${Math.round(task.progress * 100)}%` }}
                      />
                      <span className="learn-progress-text">
                        {formatPercent(task.progress, true)}
                      </span>
                    </div>
                  </div>
                )}

                {/* 已完成 */}
                {task.status === 'done' && (
                  <div className="learn-progress-section">
                    <div className="learn-progress-bar-wrapper">
                      <div className="learn-progress-bar learn-progress-done" style={{ width: '100%' }} />
                      <span className="learn-progress-text">100%</span>
                    </div>
                  </div>
                )}

                {/* 失败原因（如实展示后端错误） */}
                {task.status === 'error' && task.error && (
                  <div className="learn-progress-meta">
                    <span className="text-error">错误: {task.error}</span>
                  </div>
                )}

                {/* 操作按钮（仅取消：后端无暂停/恢复端点） */}
                {(task.status === 'pending' || task.status === 'running') && (
                  <div className="learn-task-actions">
                    <button className="btn btn-secondary btn-sm" onClick={() => cancelTask(task.id)}>
                      取消
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default LearnView;
