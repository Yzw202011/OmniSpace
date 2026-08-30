(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var danger = style.getPropertyValue('--danger').trim();

  var baseText = { color: muted, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' };
  var axisStyle = { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false }, axisLabel: baseText, splitLine: { lineStyle: { color: rule, type: 'dashed' } } };

  // ---- Chart 1: cost-benefit bubble matrix ----
  var el1 = document.getElementById('chart-matrix');
  if (el1) {
    var c1 = echarts.init(el1, null, { renderer: 'svg' });
    c1.setOption({
      animation: false,
      grid: { left: 60, right: 40, top: 50, bottom: 60 },
      tooltip: {
        appendToBody: true,
        backgroundColor: '#182032',
        borderColor: rule,
        textStyle: { color: ink, fontSize: 12 },
        formatter: function (p) {
          return '<b>' + p.data.name + '</b><br/>工程成本：' + p.data[0] + ' 人日<br/>收益预估：' + p.data[1] + ' / 10<br/>资源开销指数：' + p.data[2];
        }
      },
      legend: {
        top: 6, left: 'center', itemWidth: 10, itemHeight: 10,
        textStyle: baseText,
        data: ['底座内增量（S1–S4）', '换权重 / 换底座（S5–S6）', '探索性候选（S7，生态调研新增）']
      },
      xAxis: Object.assign({}, axisStyle, {
        name: '工程成本（人日）', nameLocation: 'middle', nameGap: 32,
        nameTextStyle: baseText, min: 0, max: 6.5, splitLine: { show: false }
      }),
      yAxis: Object.assign({}, axisStyle, {
        name: '一致性收益预估（0–10）', nameLocation: 'middle', nameGap: 40,
        nameTextStyle: baseText, min: 3, max: 10.5
      }),
      series: [
        {
          name: '底座内增量（S1–S4）', type: 'scatter',
          itemStyle: { color: accent, opacity: 0.82, borderColor: bg2, borderWidth: 1 },
          data: [
            { name: 'S1 多视图设定图', value: [3, 8.5, 3] },
            { name: 'S2 裁脸重采样', value: [2, 8.0, 5] },
            { name: 'S3 色彩锚', value: [2, 6.0, 2] },
            { name: 'S4 ControlNet 深度锁', value: [3.5, 5.0, 4] }
          ],
          symbolSize: function (d) { return 16 + d[2] * 4.5; },
          label: {
            show: true, position: 'top', distance: 8,
            color: ink, fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
            formatter: function (p) { return p.data.name; }
          }
        },
        {
          name: '换权重 / 换底座（S5–S6）', type: 'scatter',
          itemStyle: { color: accent2, opacity: 0.82, borderColor: bg2, borderWidth: 1 },
          data: [
            { name: 'S5 FLUX.2 dev Q4', value: [5, 9.5, 9] },
            { name: 'S6 Klein True-V2', value: [2, 6.5, 6] }
          ],
          symbolSize: function (d) { return 16 + d[2] * 4.5; },
          label: {
            show: true, position: 'top', distance: 8,
            color: ink, fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
            formatter: function (p) { return p.data.name; }
          }
        },
        {
          name: '探索性候选（S7，生态调研新增）', type: 'scatter',
          itemStyle: { color: 'rgba(79,195,247,0.28)', borderColor: accent, borderWidth: 1.6, borderType: 'dashed' },
          data: [
            { name: 'S7 网格批内注意力共享', value: [5, 7.0, 4] }
          ],
          symbolSize: function (d) { return 16 + d[2] * 4.5; },
          label: {
            show: true, position: 'top', distance: 8,
            color: muted, fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
            formatter: function (p) { return p.data.name; }
          }
        }
      ]
    });
    window.addEventListener('resize', function () { c1.resize(); });
  }

  // ---- Chart 2: VRAM budget bars ----
  var el2 = document.getElementById('chart-vram');
  if (el2) {
    var cats = ['klein-4B Q4', 'Klein True-V2 fp8', 'klein-9B Q6_K（当前）', 'dev Q2_K', 'dev Q4_K_S', 'dev Q5_K_M', 'dev FP8'];
    var vals = [2.6, 8.8, 10.3, 13, 19, 24, 33];
    var colors = [];
    for (var i = 0; i < vals.length; i++) {
      if (vals[i] <= 12) colors.push(accent);
      else if (vals[i] <= 16.3) colors.push(accent2);
      else colors.push(danger);
    }
    var c2 = echarts.init(el2, null, { renderer: 'svg' });
    c2.setOption({
      animation: false,
      grid: { left: 170, right: 70, top: 40, bottom: 44 },
      tooltip: {
        appendToBody: true, backgroundColor: '#182032', borderColor: rule,
        textStyle: { color: ink, fontSize: 12 },
        formatter: function (p) { return p.name + '<br/>显存需求 ≈ <b>' + p.value + ' GB</b>'; }
      },
      xAxis: Object.assign({}, axisStyle, {
        name: 'GB', nameTextStyle: baseText, min: 0, max: 36
      }),
      yAxis: Object.assign({}, axisStyle, {
        type: 'category', data: cats, splitLine: { show: false },
        axisLabel: { color: ink, fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }
      }),
      series: [{
        type: 'bar', data: vals.map(function (v, i) { return { value: v, itemStyle: { color: colors[i] } }; }),
        barWidth: 18,
        label: { show: true, position: 'right', color: muted, fontSize: 11, fontFamily: 'JetBrains Mono, monospace', formatter: '{c} GB' }
      }],
      graphic: [
        { type: 'text', left: '72%', top: 4, style: { text: '■ ≤12GB 入门档可行', fill: accent, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } },
        { type: 'text', left: '72%', top: 20, style: { text: '■ ≤16GB 本机可行', fill: accent2, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } },
        { type: 'text', left: '72%', top: 36, style: { text: '■ 两档均不可行', fill: danger, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } }
      ]
    });
    window.addEventListener('resize', function () { c2.resize(); });
  }

  // ---- Chart 3: open-source mechanism coverage ----
  var el3 = document.getElementById('chart-mech');
  if (el3) {
    var mechCats = ['身份嵌入注入', '批内/跨帧注意力共享', 'MLLM / Agent 编排', '编辑模型参考', '后处理修复', '权重级训练', '布局控制', '记忆 / 自回归条件', '场景一致性专用'];
    var mechVals = [9, 5, 5, 4, 4, 3, 3, 3, 1];
    var mechColors = [];
    for (var j = 0; j < mechVals.length; j++) {
      if (j === mechVals.length - 1) mechColors.push(danger);
      else if (mechVals[j] >= 5) mechColors.push(accent);
      else mechColors.push(accent2);
    }
    var c3 = echarts.init(el3, null, { renderer: 'svg' });
    c3.setOption({
      animation: false,
      grid: { left: 190, right: 60, top: 46, bottom: 44 },
      tooltip: {
        appendToBody: true, backgroundColor: '#182032', borderColor: rule,
        textStyle: { color: ink, fontSize: 12 },
        formatter: function (p) {
          return '<b>' + p.name + '</b><br/>覆盖项目数：<b>' + p.value + '</b> / 40';
        }
      },
      title: {
        text: '红色 = 生态空白带（仅 1 项，且为视频向）',
        left: 190, top: 6,
        textStyle: { color: danger, fontSize: 11, fontFamily: 'JetBrains Mono, monospace', fontWeight: 'normal' }
      },
      xAxis: Object.assign({}, axisStyle, {
        name: '项目数（多标签归类）', nameTextStyle: baseText, min: 0, max: 10
      }),
      yAxis: Object.assign({}, axisStyle, {
        type: 'category', data: mechCats, splitLine: { show: false },
        axisLabel: { color: ink, fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }
      }),
      series: [{
        type: 'bar',
        data: mechVals.map(function (v, i) { return { value: v, itemStyle: { color: mechColors[i] } }; }),
        barWidth: 20,
        label: { show: true, position: 'right', color: muted, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }
      }],
      graphic: [
        { type: 'text', left: '72%', top: 58, style: { text: '■ ≥5 项 · 生态拥挤赛道', fill: accent, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } },
        { type: 'text', left: '72%', top: 76, style: { text: '■ 3–4 项 · 中等覆盖', fill: accent2, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } },
        { type: 'text', left: '72%', top: 94, style: { text: '■ 1 项 · 空白带（S3+S4 自研）', fill: danger, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' } }
      ]
    });
    window.addEventListener('resize', function () { c3.resize(); });
  }
})();
