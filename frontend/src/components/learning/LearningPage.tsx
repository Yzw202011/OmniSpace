/* ==========================================================================
 * LearningPage.tsx —— 知识学习页（/learn，TASK-036）
 * --------------------------------------------------------------------------
 * 区块（从上到下）：
 *   1. LearningDashboard  实时学习状态卡片
 *   2. TopicManager       学习主题管理列表
 *   3. BrowserView        内置浏览器窗口（可展开/收起）
 *   4. BehaviorStats      行为学习面板
 *   5. KnowledgeBrowser   知识库管理
 *   6. ImportDoc          导入文档区域
 *   7. LearningSettings   学习设置
 *   8. TrainTaskSection   训练任务（沿用既有 LearnView，零回归）
 * ========================================================================== */

import React from 'react';
import { BookOpen, Dumbbell } from 'lucide-react';
import LearningDashboard from './LearningDashboard';
import TopicManager from './TopicManager';
import BrowserView from './BrowserView';
import BehaviorStatsPanel from './BehaviorStats';
import KnowledgeBrowser from './KnowledgeBrowser';
import ImportDoc from './ImportDoc';
import LearningSettingsPanel from './LearningSettings';
import LearnView from '@/components/learn/LearnView';

export const LearningPage: React.FC = () => {
  return (
    <div className="page">
      <h1 className="page-title"><BookOpen size={20} aria-hidden="true" /> 知识学习</h1>
      <p className="page-subtitle">AI 自主学习：主题管理 · 实时浏览 · 行为偏好 · 知识库 · 微调训练</p>

      <div className="flex flex-col gap-4 mt-5">
        <LearningDashboard />
        <TopicManager />
        <BrowserView />
        <BehaviorStatsPanel />
        <KnowledgeBrowser />
        <ImportDoc />
        <LearningSettingsPanel />

        {/* 既有训练任务功能（LearnView 原样嵌入，零回归） */}
        <section className="card" aria-label="训练任务">
          <h3 className="card-title"><Dumbbell size={16} aria-hidden="true" /> 训练任务</h3>
          <LearnView />
        </section>
      </div>
    </div>
  );
};

export default LearningPage;
// 本项目仅供学习使用，商业授权请+Q 3559331368
