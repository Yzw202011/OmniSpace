(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var accent3 = style.getPropertyValue('--accent3').trim();
  var accent4 = style.getPropertyValue('--accent4').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  var baseText = { color: muted, fontSize: 12 };
  var axisCommon = {
    axisLine: { lineStyle: { color: rule } },
    axisTick: { lineStyle: { color: rule } },
    axisLabel: baseText,
    splitLine: { lineStyle: { color: rule, opacity: 0.4 } }
  };

  function make(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    return echarts.init(el, null, { renderer: 'svg' });
  }

  // --- Chart 1: 后端各层代码量 ---
  var c1 = make('chart-loc');
  if (c1) {
    c1.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true },
      grid: { left: 70, right: 30, top: 40, bottom: 40 },
      xAxis: Object.assign({ type: 'category', data: ['api', 'services', 'data', 'middleware', 'engines', 'tests'] }, axisCommon),
      yAxis: Object.assign({ type: 'value', name: '行数' }, axisCommon),
      series: [{
        type: 'bar',
        data: [
          { value: 17946, itemStyle: { color: accent, opacity: 0.85 } },
          { value: 25211, itemStyle: { color: accent, opacity: 1 } },
          { value: 2955, itemStyle: { color: accent, opacity: 0.55 } },
          { value: 1078, itemStyle: { color: accent, opacity: 0.55 } },
          { value: 1613, itemStyle: { color: accent, opacity: 0.55 } },
          { value: 1624, itemStyle: { color: accent3, opacity: 0.75 } }
        ],
        barWidth: '52%',
        itemStyle: { borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: muted, fontSize: 11, formatter: function(p) { return p.value.toLocaleString(); } }
      }]
    });
    window.addEventListener('resize', function() { c1.resize(); });
  }

  // --- Chart 2: 前后端规模对比 ---
  var c2 = make('chart-stack');
  if (c2) {
    c2.setOption({
      animation: false,
      tooltip: { trigger: 'item', appendToBody: true },
      legend: { bottom: 0, textStyle: baseText },
      series: [{
        type: 'pie',
        radius: ['52%', '74%'],
        center: ['50%', '46%'],
        label: { color: ink, fontSize: 12.5, formatter: function(p) { return p.name + '\n' + p.value.toLocaleString() + ' 行 (' + p.percent + '%)'; } },
        data: [
          { value: 51652, name: '后端 (FastAPI · Python)', itemStyle: { color: accent } },
          { value: 26683, name: '前端 (React 19 · TS)', itemStyle: { color: accent4 } }
        ]
      }]
    });
    window.addEventListener('resize', function() { c2.resize(); });
  }

  // --- Chart 3: 问题严重度分布 ---
  var c3 = make('chart-issues');
  if (c3) {
    c3.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true },
      legend: { top: 0, textStyle: baseText },
      grid: { left: 60, right: 30, top: 46, bottom: 36 },
      xAxis: Object.assign({ type: 'category', data: ['严重 / P0', '高 / P1', '中 / P2', '低'] }, axisCommon),
      yAxis: Object.assign({ type: 'value', name: '发现数量' }, axisCommon),
      series: [
        { name: '后端', type: 'bar', data: [2, 5, 5, 5], itemStyle: { color: accent, borderRadius: [3, 3, 0, 0] }, barGap: '20%' },
        { name: '前端', type: 'bar', data: [3, 5, 8, 5], itemStyle: { color: accent4, borderRadius: [3, 3, 0, 0] } },
        { name: '生成链路架构', type: 'bar', data: [2, 5, 6, 0], itemStyle: { color: warn, borderRadius: [3, 3, 0, 0] } }
      ]
    });
    window.addEventListener('resize', function() { c3.resize(); });
  }

  // --- Chart 4: 超大文件 Top 10 ---
  var c4 = make('chart-bigfiles');
  if (c4) {
    var files = [
      { n: 'video_engine.py', v: 2465, be: true },
      { n: 'comic_asset.py', v: 1860, be: true },
      { n: 'browser_agent_service.py', v: 1678, be: true },
      { n: 'api/models.py', v: 1583, be: true },
      { n: 'lora_training_service.py', v: 1480, be: true },
      { n: 'ModelManager.tsx', v: 1460, be: false },
      { n: 'paint_engine.py', v: 1419, be: true },
      { n: 'dialog_engine.py', v: 1212, be: true },
      { n: 'style_lora_service.py', v: 1201, be: true },
      { n: 'RightPanel.tsx', v: 889, be: false }
    ].reverse();
    c4.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true },
      grid: { left: 190, right: 60, top: 20, bottom: 30 },
      xAxis: Object.assign({ type: 'value' }, axisCommon),
      yAxis: Object.assign({
        type: 'category',
        data: files.map(function(f) { return f.n; }),
        axisLabel: { color: muted, fontSize: 11.5, fontFamily: 'JetBrainsMono, monospace' }
      }, axisCommon),
      series: [{
        type: 'bar',
        data: files.map(function(f) {
          return { value: f.v, itemStyle: { color: f.be ? accent : accent4, opacity: 0.9 } };
        }),
        barWidth: '62%',
        itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: muted, fontSize: 11, formatter: '{c}' }
      }]
    });
    window.addEventListener('resize', function() { c4.resize(); });
  }

  // --- Chart 5: 六维雷达 ---
  var c5 = make('chart-radar');
  if (c5) {
    c5.setOption({
      animation: false,
      tooltip: { appendToBody: true },
      radar: {
        indicator: [
          { name: '架构设计', max: 10 },
          { name: '代码质量', max: 10 },
          { name: '安全防护', max: 10 },
          { name: '测试覆盖', max: 10 },
          { name: '性能与资源治理', max: 10 },
          { name: '可维护性', max: 10 }
        ],
        radius: '68%',
        center: ['50%', '52%'],
        axisName: { color: muted, fontSize: 12.5 },
        splitLine: { lineStyle: { color: rule } },
        splitArea: { areaStyle: { color: ['transparent'] } },
        axisLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'radar',
        symbolSize: 5,
        data: [{
          value: [8, 7, 6, 3, 7.5, 5.5],
          name: '工程能力评分',
          lineStyle: { color: accent, width: 2 },
          itemStyle: { color: accent },
          areaStyle: { color: accent, opacity: 0.22 },
          label: { show: true, color: ink, fontSize: 11.5 }
        }]
      }]
    });
    window.addEventListener('resize', function() { c5.resize(); });
  }
})();
