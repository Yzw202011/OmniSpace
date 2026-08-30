(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var success = style.getPropertyValue('--success').trim();
  var warning = style.getPropertyValue('--warning').trim();
  var danger = style.getPropertyValue('--danger').trim();

  // ============================================================
  // ROI Quadrant Chart (scatter with category colors)
  // ============================================================
  var chartDom = document.getElementById('chart-roi');
  if (chartDom) {
    var chart = echarts.init(chartDom, null, { renderer: 'svg' });

    // x: effort (1=low, 5=high), y: value (1=low, 5=high)
    var dataReplace = [
      // [effort, value, name]
      [2.5, 5.0, 'PuLID-FLUX 角色一致性'],
      [2.8, 4.8, 'ControlNet 控制'],
      [1.2, 4.2, '超分升级'],
      [1.5, 3.8, 'Inpaint 升级'],
    ];
    var dataSupplement = [
      [1.0, 3.5, 'FaceID 备选'],
      [1.0, 3.2, '面部修复'],
      [1.2, 3.0, '风格迁移'],
      [1.0, 2.8, 'ReActor 换脸'],
      [1.0, 2.8, '视频插帧'],
      [0.8, 2.5, '深度控制'],
      [0.8, 2.3, '线稿控制'],
      [4.0, 4.0, 'StoryDiffusion'],
    ];
    var dataKeep = [
      [3.0, 1.5, '基础文生图'],
      [2.5, 1.5, '图生图'],
      [3.0, 1.8, '视频生成'],
      [3.5, 1.2, '漫剧全流程'],
      [2.8, 2.0, 'LoRA 训练'],
    ];
    var dataSkip = [
      [2.0, 0.8, '老照片修复'],
      [2.5, 1.0, '电商产品图'],
      [2.0, 1.0, '艺术字海报'],
      [1.5, 1.2, 'T2I Adapter'],
      [1.8, 1.5, 'InstantID'],
    ];

    function makeSeries(data, color, name) {
      return {
        name: name,
        type: 'scatter',
        symbolSize: 14,
        itemStyle: {
          color: color,
          opacity: 0.85,
          borderColor: '#fff',
          borderWidth: 2,
          shadowBlur: 4,
          shadowColor: 'rgba(0,0,0,0.15)',
        },
        label: {
          show: true,
          formatter: function(p) { return p.data[2]; },
          position: 'right',
          color: ink,
          fontSize: 11,
          fontWeight: 500,
          distance: 4,
        },
        data: data,
      };
    }

    chart.setOption({
      animation: false,
      tooltip: {
        trigger: 'item',
        appendToBody: true,
        formatter: function(p) {
          return '<strong>' + p.data[2] + '</strong><br/>业务价值: ' + p.data[1] + '<br/>接入成本: ' + p.data[0];
        },
      },
      legend: {
        top: 10,
        data: ['🔴 建议替换', '🟡 建议补充', '🟢 保留现有', '⚪ 跳过'],
        textStyle: { fontSize: 11, color: ink },
        itemWidth: 10,
        itemHeight: 10,
      },
      grid: { left: 60, right: 40, top: 50, bottom: 50 },
      xAxis: {
        type: 'value',
        name: '接入成本（低 → 高）',
        nameLocation: 'middle',
        nameGap: 30,
        nameTextStyle: { fontSize: 11, color: muted },
        min: 0,
        max: 5,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: rule } },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      },
      yAxis: {
        type: 'value',
        name: '业务价值（低 → 高）',
        nameLocation: 'middle',
        nameGap: 40,
        nameTextStyle: { fontSize: 11, color: muted },
        min: 0,
        max: 5.5,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: rule } },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      },
      series: [
        makeSeries(dataReplace, danger, '🔴 建议替换'),
        makeSeries(dataSupplement, warning, '🟡 建议补充'),
        makeSeries(dataKeep, success, '🟢 保留现有'),
        makeSeries(dataSkip, muted, '⚪ 跳过'),
      ],
      // Quadrant divider lines
      graphic: [
        {
          type: 'line',
          shape: { x1: 0, y1: 0, x2: 0, y2: 0 },
          // Will be positioned via zrender — instead use markLine
        },
      ],
      markLine: {},
    });

    window.addEventListener('resize', function() { chart.resize(); });
  }
})();
