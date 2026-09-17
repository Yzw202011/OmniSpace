/* ==========================================================================
 * CharacterTrainPanel.tsx —— 人物训练面板（P3，2026-09-17 用户拍板 3A）
 * --------------------------------------------------------------------------
 * 角色资产列表（图数/可练判定/当前版本）+ 一键训练 + 任务进度 + 版本回滚。
 * 素材=角色主图+四视图（comic_assets），练出的 LoRA 部署到资产旁
 * lora.safetensors，生图链自动挂载（D-LoRA 身份硬锁，消费端零改动）。
 * ========================================================================== */
import React, { useCallback, useEffect, useState } from 'react';
import { History, UserRound } from 'lucide-react';

import {
  cancelCharacterTask,
  fetchCharacterAssets,
  fetchCharacterTasks,
  fetchCharacterVersions,
  rollbackCharacterLora,
  trainCharacterLora,
  type CharacterAssetCard,
  type CharacterTask,
  type CharacterVersion,
} from '@/services/characterLoraApi';
import { getErrorMessage } from '@/utils/errors';
import { useAppStore } from '@/stores/useAppStore';

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', training: '训练中', done: '已完成',
  error: '失败', cancelled: '已取消',
};

export const CharacterTrainPanel: React.FC = () => {
  const showToast = useAppStore((s) => s.showToast);
  const [assets, setAssets] = useState<CharacterAssetCard[]>([]);
  const [tasks, setTasks] = useState<CharacterTask[]>([]);
  const [versionsFor, setVersionsFor] = useState<string | null>(null);
  const [versions, setVersions] = useState<CharacterVersion[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [a, t] = await Promise.all([
        fetchCharacterAssets(), fetchCharacterTasks()]);
      setAssets(a);
      setTasks(t);
      setLoadError(null);
    } catch (e) {
      setLoadError(getErrorMessage(e, '人物训练数据拉取失败'));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const activeByAsset = new Map(
    tasks.filter((t) => t.status === 'queued' || t.status === 'training')
      .map((t) => [t.asset_id, t]));

  const doTrain = async (assetId: string, name: string) => {
    setBusy(assetId);
    try {
      await trainCharacterLora(assetId);
      showToast(`「${name}」已入队训练（完成后自动挂到生图链）`, 'success');
      await refresh();
    } catch (e) {
      showToast(getErrorMessage(e, '入队失败'), 'error');
    } finally {
      setBusy(null);
    }
  };

  const doCancel = async (taskId: string) => {
    try {
      await cancelCharacterTask(taskId);
      await refresh();
    } catch (e) {
      showToast(getErrorMessage(e, '取消失败'), 'error');
    }
  };

  const openVersions = async (assetId: string) => {
    setVersionsFor(assetId);
    try {
      setVersions(await fetchCharacterVersions(assetId));
    } catch (e) {
      showToast(getErrorMessage(e, '版本拉取失败'), 'error');
    }
  };

  const doRollback = async (assetId: string, version: string) => {
    try {
      await rollbackCharacterLora(assetId, version);
      showToast(`已回滚到 ${version}（生图链即刻生效）`, 'success');
      await openVersions(assetId);
      await refresh();
    } catch (e) {
      showToast(getErrorMessage(e, '回滚失败'), 'error');
    }
  };

  return (
    <section className="card" aria-label="人物训练">
      <h3 className="card-title">
        <UserRound size={16} aria-hidden="true" /> 人物 LoRA 训练
      </h3>
      <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
        选一个角色练「专属脸蛋」：主图+四视图作素材，练完自动挂到生图链
        （身份硬锁）。角色需先在漫画/漫剧资产库升级四视图才够素材。
      </p>

      {loadError && (
        <p className="text-sm" style={{ color: 'var(--color-error)' }} role="alert">
          {loadError}
        </p>
      )}

      {!loadError && assets.length === 0 && (
        <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          还没有角色资产。去 AI漫画 或 漫剧创作 的资产库创建角色（建角色时勾选四视图）。
        </p>
      )}

      <ul className="training-queue">
        {assets.map((a) => {
          const t = activeByAsset.get(a.asset_id);
          return (
            <li key={a.asset_id} className="training-queue-item">
              <span
                className="training-kind-chip"
                style={{ borderLeftColor: 'var(--color-accent)' }}
              >
                {a.current_version ? `已练 ${a.current_version}` : '未练'}
              </span>
              <span className="training-queue-name" title={a.prompt || a.name}>
                {a.name}
                <span style={{ color: 'var(--color-text-tertiary)' }}>
                  {' '}· {a.image_count} 张图{a.trainable ? '' : '（需≥4张：先升级四视图）'}
                </span>
              </span>
              {t ? (
                <>
                  <span style={{ color: 'var(--color-success)' }}>
                    {STATUS_TEXT[t.status] ?? t.status}
                    {' '}{Math.round((t.progress ?? 0) * 100)}%
                  </span>
                  <button
                    type="button" className="btn btn-ghost btn-sm"
                    onClick={() => void doCancel(t.id)}
                  >
                    取消
                  </button>
                </>
              ) : (
                <button
                  type="button" className="btn btn-primary btn-sm"
                  disabled={!a.trainable || busy === a.asset_id}
                  onClick={() => void doTrain(a.asset_id, a.name)}
                >
                  {busy === a.asset_id ? '入队中…' : '训练'}
                </button>
              )}
              <button
                type="button" className="btn btn-ghost btn-sm"
                onClick={() => void openVersions(a.asset_id)}
                aria-label={`查看 ${a.name} 的版本`}
              >
                <History size={14} aria-hidden="true" />
              </button>
            </li>
          );
        })}
      </ul>

      {versionsFor && (
        <div className="card" style={{ marginTop: 12 }} aria-label="版本列表">
          <div className="flex items-center justify-between mb-2">
            <h4 className="card-title" style={{ fontSize: 13 }}>版本（最近 3 个）</h4>
            <button
              type="button" className="btn btn-ghost btn-sm"
              onClick={() => setVersionsFor(null)}
            >
              收起
            </button>
          </div>
          {versions.length === 0 ? (
            <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              还没有版本——先练一次。
            </p>
          ) : (
            <ul className="training-queue">
              {versions.map((v) => (
                <li key={v.version} className="training-queue-item">
                  <span className="training-queue-name">
                    {v.version} · {v.data_count ?? '?'} 张图
                  </span>
                  {v.is_current ? (
                    <span style={{ color: 'var(--color-primary)' }}>当前</span>
                  ) : (
                    <button
                      type="button" className="btn btn-ghost btn-sm"
                      onClick={() => void doRollback(versionsFor, v.version)}
                    >
                      回滚到此版
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
};

export default CharacterTrainPanel;
