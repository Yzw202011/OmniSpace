(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var ok = style.getPropertyValue('--ok').trim();
  var danger = style.getPropertyValue('--danger').trim();

  /* ---------- 图 3：四层问题条目分布 ---------- */
  var issueEl = document.getElementById('chart-issues');
  if (issueEl) {
    var issueChart = echarts.init(issueEl, null, { renderer: 'svg' });
    issueChart.setOption({
      animation: false,
      grid: { left: 40, right: 24, top: 24, bottom: 40 },
      tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'category',
        data: ['一致性 / 质量 (A)', '架构 / 工程 (B)', '性能 / 资源 (C)', '产品 / 功能 (D)'],
        axisLine: { lineStyle: { color: rule } },
        axisLabel: { color: ink, fontSize: 12 },
        axisTick: { show: false }
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        axisLine: { show: false },
        axisLabel: { color: muted },
        splitLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'bar',
        barWidth: '48%',
        data: [
          { value: 6, itemStyle: { color: accent } },
          { value: 6, itemStyle: { color: accent2 } },
          { value: 4, itemStyle: { color: warn } },
          { value: 4, itemStyle: { color: ok } }
        ],
        label: { show: true, position: 'top', color: ink, fontWeight: 700 }
      }]
    });
    window.addEventListener('resize', function () { issueChart.resize(); });
  }

  /* ---------- 图 4：模块成熟度雷达 ---------- */
  var radarEl = document.getElementById('chart-radar');
  if (radarEl) {
    var radarChart = echarts.init(radarEl, null, { renderer: 'svg' });
    radarChart.setOption({
      animation: false,
      tooltip: { trigger: 'item', appendToBody: true },
      radar: {
        indicator: [
          { name: '对话', max: 100 },
          { name: 'AI 绘画', max: 100 },
          { name: '漫剧分镜', max: 100 },
          { name: '视频生成', max: 100 },
          { name: '语音', max: 100 },
          { name: '知识学习', max: 100 },
          { name: '3D 资产', max: 100 }
        ],
        radius: '66%',
        splitNumber: 5,
        axisName: { color: ink, fontSize: 12 },
        splitLine: { lineStyle: { color: rule } },
        splitArea: { areaStyle: { color: [bg2, 'rgba(99,102,241,0.03)'] } },
        axisLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'radar',
        data: [{
          value: [90, 85, 80, 65, 50, 75, 40],
          name: '成熟度',
          areaStyle: { color: accent },
          lineStyle: { color: accent, width: 2 },
          itemStyle: { color: accent }
        }]
      }]
    });
    window.addEventListener('resize', function () { radarChart.resize(); });
  }

  /* ---------- 图 5：V3 优先级矩阵 ---------- */
  var priEl = document.getElementById('chart-priority');
  if (priEl) {
    var priChart = echarts.init(priEl, null, { renderer: 'svg' });
    var p0 = { name: 'P0 一致性闭环', color: danger };
    var p1 = { name: 'P1 治理+性能', color: accent };
    var p2 = { name: 'P2 生态扩展', color: accent2 };
    var seriesData = [
      // P0
      { value: [9, 4, '画风维度门禁'], c: p0 },
      { value: [7, 2, '远景镜分级门禁'], c: p0 },
      { value: [7, 5, '确定性重放'], c: p0 },
      { value: [6, 2, '描述词活源回写'], c: p0 },
      { value: [6, 3, '重抽策略收敛'], c: p0 },
      // P1
      { value: [6, 4, '单一职责重构'], c: p1 },
      { value: [5, 2, '文档三方对齐'], c: p1 },
      { value: [5, 3, 'git 版本管理'], c: p1 },
      { value: [6, 4, '回归测试+golden 帧'], c: p1 },
      { value: [8, 6, '并行调度'], c: p1 },
      { value: [7, 5, '量化分级'], c: p1 },
      { value: [8, 5, '热加载增速'], c: p1 },
      // P2
      { value: [6, 6, '语音克隆'], c: p2 },
      { value: [7, 7, '3D 管线'], c: p2 },
      { value: [8, 8, '插件系统'], c: p2 },
      { value: [8, 9, '协作+共享'], c: p2 },
      { value: [7, 8, '跨平台+移动端'], c: p2 }
    ];
    priChart.setOption({
      animation: false,
      grid: { left: 55, right: 30, top: 30, bottom: 55 },
      tooltip: {
        appendToBody: true,
        formatter: function (p) { return p.data.value[2] + '<br/>影响 ' + p.data.value[0] + ' · 成本 ' + p.data.value[1]; }
      },
      legend: {
        data: [p0.name, p1.name, p2.name],
        bottom: 8,
        textStyle: { color: ink },
        itemGap: 20
      },
      xAxis: {
        name: '业务影响 →',
        nameLocation: 'middle',
        nameGap: 30,
        min: 2, max: 10,
        axisLine: { lineStyle: { color: rule } },
        axisLabel: { color: muted },
        splitLine: { lineStyle: { color: rule } },
        nameTextStyle: { color: muted }
      },
      yAxis: {
        name: '实施成本 →',
        nameLocation: 'middle',
        nameGap: 34,
        min: 1, max: 10,
        axisLine: { lineStyle: { color: rule } },
        axisLabel: { color: muted },
        splitLine: { lineStyle: { color: rule } },
        nameTextStyle: { color: muted }
      },
      series: [{
        type: 'scatter',
        symbolSize: function (val) { return Math.max(13, val[0] * 3.2); },
        data: seriesData.map(function (d) {
          return {
            value: [d.value[0], d.value[1]],
            name: d.value[2],
            itemStyle: { color: d.c.color, opacity: 0.82 }
          };
        }),
        label: {
          show: true,
          position: 'top',
          formatter: function (p) { return p.name; },
          fontSize: 11,
          color: ink
        },
        emphasis: { label: { fontWeight: 700 } }
      }]
    });
    window.addEventListener('resize', function () { priChart.resize(); });
  }
})();