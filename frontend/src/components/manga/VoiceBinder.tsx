// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * VoiceBinder.tsx —— 音色绑定组件（FE-023 真实接线）
 * --------------------------------------------------------------------------
 * 数据源（后端 src/api/manga/ 包真实端点，无本地伪造）：
 *   - 音色列表  GET /manga/voices（预置音色后端首启种子化）
 *   - 绑定     POST /manga/voices/bind（后端自动解除该角色旧绑定）
 *   - 情感标签 PUT /manga/voices/{voiceId}/emotion（默认/愤怒/悲伤）
 *   - 试听     POST /manga/voices/preview → base64 WAV 本地播放
 * 角色列表：后端无角色端点，由分镜行 characters 字段去重派生（诚实门控）。
 * 降级诚实展示：语音模型未随包时试听返回静音占位 + degraded 标记，
 * 以 warning toast 如实告知（审计 BK-013）。
 * ========================================================================== */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Mic, Star, Wrench } from 'lucide-react';
import { useMangaStore } from '@/stores/useMangaStore';
import { useAppStore } from '@/stores/useAppStore';
import type { VoiceItem } from '@/services/schema';
import { uploadCustomVoice } from '@/services/mangaApi';
import { Upload } from 'lucide-react';

/** 试听示例文本（后端合成内容的输入） */
const PREVIEW_TEXT = '你好，这是一段音色试听。';

/**
 * 音色绑定组件
 * 左栏：角色列表（分镜行派生）；右栏：真实音色列表（绑定/情感/试听）。
 */
export const VoiceBinder: React.FC = () => {
  const rows = useMangaStore((s) => s.rows);
  const voices = useMangaStore((s) => s.voices);
  const voicesLoaded = useMangaStore((s) => s.voicesLoaded);
  const fetchVoices = useMangaStore((s) => s.fetchVoices);
  const bindVoice = useMangaStore((s) => s.bindVoice);
  const updateVoiceEmotion = useMangaStore((s) => s.updateVoiceEmotion);
  const previewVoice = useMangaStore((s) => s.previewVoice);
  const showToast = useAppStore((s) => s.showToast);

  const [selectedChar, setSelectedChar] = useState('');
  const [previewingId, setPreviewingId] = useState<string | null>(null);
  const [bindingId, setBindingId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const voiceFileRef = useRef<HTMLInputElement>(null);

  // 挂载时拉取真实音色列表
  useEffect(() => {
    if (!voicesLoaded) {
      void fetchVoices();
    }
  }, [voicesLoaded, fetchVoices]);

  // 卸载时停止试听播放
  useEffect(() => {
    return () => {
      audioRef.current?.pause();
      audioRef.current = null;
    };
  }, []);

  /** 角色列表：分镜行 characters 去重派生（后端无角色端点） */
  const characters = useMemo(() => {
    const set = new Set<string>();
    rows.forEach((r) => r.characters.forEach((c) => c && set.add(c)));
    return Array.from(set);
  }, [rows]);

  /** 上传自定义音色（FileList 先快照再清 value，2026-09-08 教训） */
  const handleUploadVoice = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    e.target.value = '';
    if (!file) return;
    const stem = file.name.replace(/\.[^.]+$/, '').slice(0, 40) || '自定义音色';
    const name = window.prompt('给这个音色起个名字：', stem);
    if (name === null) return;
    setUploading(true);
    try {
      const res = await uploadCustomVoice(name || stem, file);
      showToast(`音色「${res.name}」已上传，可在列表中绑定`, 'success');
      await fetchVoices();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '上传失败';
      showToast(msg, 'error');
    } finally {
      setUploading(false);
    }
  };

  /** 角色 → 已绑定音色 */
  const boundVoiceOf = (charName: string): VoiceItem | undefined =>
    voices.find((v) => v.character_id === charName);

  /** 绑定音色到选中角色 */
  const handleBind = async (voiceId: string) => {
    if (!selectedChar) {
      showToast('请先在左侧选择角色', 'warning');
      return;
    }
    setBindingId(voiceId);
    try {
      await bindVoice(voiceId, selectedChar);
      showToast(`音色已绑定到「${selectedChar}」`, 'success');
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '音色绑定失败';
      showToast(msg, 'error');
    } finally {
      setBindingId(null);
    }
  };

  /** 更新音色情感标签 */
  const handleEmotion = async (voiceId: string, emotion: string) => {
    try {
      await updateVoiceEmotion(voiceId, emotion);
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '情感标签更新失败';
      showToast(msg, 'error');
    }
  };

  /** 试听（base64 音频本地播放；降级时如实提示） */
  const handlePreview = async (voice: VoiceItem) => {
    if (previewingId) {
      return;
    }
    setPreviewingId(voice.id);
    try {
      const res = await previewVoice(voice.id, PREVIEW_TEXT, voice.emotion);
      audioRef.current?.pause();
      const audio = new Audio(`data:audio/${res.format || 'wav'};base64,${res.audio}`);
      audioRef.current = audio;
      audio.onended = () => setPreviewingId((cur) => (cur === voice.id ? null : cur));
      audio.onerror = () => setPreviewingId((cur) => (cur === voice.id ? null : cur));
      await audio.play();
      if (res.degraded) {
        showToast(
          res.degrade_reason || '试听音频为降级占位（非真实音色）',
          'warning',
        );
      }
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '试听失败';
      showToast(msg, 'error');
      setPreviewingId(null);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* 页头 */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-[var(--color-divider)]">
        <span className="text-sm font-semibold text-[var(--color-text-primary)] inline-flex items-center gap-1.5">
          <Mic size={15} aria-hidden="true" className="text-[var(--color-primary)]" /> 音色绑定
        </span>
        <span className="text-xs text-[var(--color-text-tertiary)]">
          角色来自分镜行「角色」列；音色来自后端 voice_profiles（预置音色首启自动种子化）
        </span>
        <button
          type="button"
          className="btn btn-outline btn-sm ml-auto"
          disabled={uploading}
          onClick={() => voiceFileRef.current?.click()}
        >
          <Upload size={13} aria-hidden="true" /> {uploading ? '上传中…' : '上传音色'}
        </button>
        <input
          ref={voiceFileRef}
          type="file"
          accept=".wav,.mp3,.flac,.m4a"
          hidden
          onChange={(e) => void handleUploadVoice(e)}
        />
      </div>

      <div className="flex flex-1 min-h-0">
        {/* 左栏：角色列表 */}
        <div className="w-56 shrink-0 border-r border-[var(--color-divider)] flex flex-col">
          <div className="px-3 py-2 text-xs font-medium text-[var(--color-text-secondary)]">
            角色列表（{characters.length}）
          </div>
          <div className="flex-1 overflow-auto px-2 pb-2 space-y-1">
            {characters.length === 0 ? (
              <div className="px-2 py-6 text-xs text-[var(--color-text-tertiary)] text-center">
                暂无角色。
                <br />
                在分镜表「角色」列填写角色名后在此显示。
              </div>
            ) : (
              characters.map((name) => {
                const bound = boundVoiceOf(name);
                const active = selectedChar === name;
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setSelectedChar(name)}
                    className={[
                      'w-full text-left px-3 py-2 rounded-lg text-sm transition-colors border',
                      active
                        ? 'border-[var(--color-primary)] bg-[var(--color-primary-100)]'
                        : 'border-transparent hover:bg-[var(--color-primary-50)]',
                    ].join(' ')}
                  >
                    <div className="text-[var(--color-text-primary)]">{name}</div>
                    <div className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
                      {bound ? `已绑定：${bound.name}` : '未绑定音色'}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* 右栏：音色列表 */}
        <div className="flex-1 min-w-0 overflow-auto p-3 space-y-2">
          {!voicesLoaded ? (
            <div className="py-10 text-center text-sm text-[var(--color-text-tertiary)]">
              音色列表加载中…
            </div>
          ) : voices.length === 0 ? (
            <div className="py-10 text-center text-sm text-[var(--color-text-tertiary)]">
              音色列表为空（后端不可达或尚未种子化）。
            </div>
          ) : (
            voices.map((voice) => {
              const boundToSelected =
                selectedChar !== '' && voice.character_id === selectedChar;
              return (
                <div
                  key={voice.id}
                  className={[
                    'rounded-lg border px-3 py-2 bg-[var(--color-card)]',
                    boundToSelected
                      ? 'border-[var(--color-primary)]'
                      : 'border-[var(--color-border-light)]',
                  ].join(' ')}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-[var(--color-text-primary)] inline-flex items-center gap-1.5">
                      {voice.is_preset ? (
                        <Star size={13} aria-hidden="true" className="text-[var(--color-accent)] fill-current shrink-0" />
                      ) : (
                        <Wrench size={13} aria-hidden="true" className="text-[var(--color-text-tertiary)] shrink-0" />
                      )}
                      {voice.name}
                    </span>
                    {voice.character_id && (
                      <span className="text-xs text-[var(--color-accent)]">
                        → {voice.character_id}
                      </span>
                    )}
                    <span className="flex-1" />
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => void handlePreview(voice)}
                      disabled={previewingId !== null}
                    >
                      {previewingId === voice.id ? '播放中…' : '试听'}
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => void handleBind(voice.id)}
                      disabled={!selectedChar || bindingId !== null}
                      title={
                        selectedChar
                          ? `绑定到 ${selectedChar}`
                          : '请先在左侧选择角色'
                      }
                    >
                      {bindingId === voice.id ? '绑定中…' : '绑定'}
                    </button>
                  </div>
                  {/* 情感标签（真实持久化到 voice_profiles.emotion） */}
                  <div className="flex items-center gap-1 mt-2">
                    <span className="text-xs text-[var(--color-text-tertiary)]">
                      情感：
                    </span>
                    {voice.emotions.map((emo) => (
                      <button
                        key={emo}
                        type="button"
                        onClick={() => void handleEmotion(voice.id, emo)}
                        className={[
                          'px-2 h-6 rounded text-xs transition-colors',
                          voice.emotion === emo
                            ? 'bg-[var(--color-primary)] text-white'
                            : 'bg-[var(--color-input-bg)] text-[var(--color-text-secondary)] hover:bg-[var(--color-primary-50)]',
                        ].join(' ')}
                      >
                        {emo}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default VoiceBinder;
