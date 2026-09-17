/* ==========================================================================
 * LearningPage.tsx —— 知识学习页（/learn，TASK-036）
 * --------------------------------------------------------------------------
 * 区块（从上到下）：
 *   1. LearningDashboard  实时学习状态卡片
 *   2. TopicManager       学习主题管理列表
 *   3. BrowserView        内置浏览器窗口（可展开/收起）
 *   4. BehaviorStats      行为学习面板
 *   5. AnalysisReport     学习分析报表（效率/来源/趋势/主题对比，2026-09-17）
 *   6. KnowledgeBrowser   知识库管理
 *   7. ImportDoc          导入文档区域
 *   8. LearningSettings   学习设置
 *
 * P1 训练中心（2026-09-17 用户拍板 2A）：训练任务（LearnView）与 LoRA
 * 版本管理（LoRAVersionManager）整体迁入 /training 训练中心；本页瘦身为
 * 「用知识」（会话/主题/浏览/行为/知识库），训练去训练中心。
 * ========================================================================== */

import React from 'react';
import { BookOpen } from 'lucide-react';
import LearningDashboard from './LearningDashboard';
import QuotaCard from './QuotaCard';
import TopicManager from './TopicManager';
import BrowserView from './BrowserView';
import BehaviorStatsPanel from './BehaviorStats';
import AnalysisReport from './AnalysisReport';
import KnowledgeBrowser from './KnowledgeBrowser';
import ImportDoc from './ImportDoc';
import LearningSettingsPanel from './LearningSettings';

export const LearningPage: React.FC = () => {
  return (
    <div className="page">
      <h1 className="page-title"><BookOpen size={20} aria-hidden="true" /> 知识学习</h1>
      <p className="page-subtitle">AI 自主学习：主题管理 · 实时浏览 · 行为偏好 · 知识库（训练已迁入训练中心）</p>

      <div className="flex flex-col gap-4 mt-5">
        <LearningDashboard />
        <QuotaCard />
        <TopicManager />
        <BrowserView />
        <BehaviorStatsPanel />
        <AnalysisReport />
        <KnowledgeBrowser />
        <ImportDoc />
        <LearningSettingsPanel />
      </div>
    </div>
  );
};

export default LearningPage;
// 本项目仅供学习使用，商业授权请+Q 3559331368
