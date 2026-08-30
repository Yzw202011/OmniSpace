(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var success = style.getPropertyValue('--success').trim();
  var warning = style.getPropertyValue('--warning').trim();

  // ============================================================
  // Chart 1: Workflow maturity scores (horizontal bar)
  // ============================================================
  var chartDom = document.getElementById('chart-workflow-score');
  if (chartDom) {
    var chart = echarts.init(chartDom, null, { renderer: 'svg' });

    var categories = [
      'ReActor 换脸后处理',
      'StoryDiffusion 序列生成',
      'VNCCS 角色工作流',
      'InstantID 工作流',
      'FLUX-Klein 九宫格分镜',
      'Consistent Character Creator 3.0',
      'IP-Adapter-FaceID PlusV2',
      'PuLID-FLUX 文生图',
    ];

    var values = [8.5, 7.8, 7.2, 7.5, 7.0, 8.3, 8.8, 9.2];

    var colors = values.map(function(v) {
      if (v >= 9) return success;
      if (v >= 8) return accent;
      if (v >= 7) return warning;
      return muted;
    });

    chart.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        formatter: function(params) {
          return params[0].name + '<br/>评分: ' + params[0].value + '/10';
        },
      },
      grid: { left: 180, right: 60, top: 20, bottom: 30 },
      xAxis: {
        type: 'value',
        min: 0,
        max: 10,
        axisLabel: { color: muted, fontSize: 11 },
        axisLine: { lineStyle: { color: rule } },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      },
      yAxis: {
        type: 'category',
        data: categories,
        axisLabel: { color: ink, fontSize: 12, fontWeight: 500 },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [{
        type: 'bar',
        data: values.map(function(v, i) {
          return { value: v, itemStyle: { color: colors[i], borderRadius: [0, 4, 4, 0] } };
        }),
        barWidth: 18,
        label: {
          show: true,
          position: 'right',
          color: ink,
          fontWeight: 700,
          fontSize: 12,
          formatter: '{c}',
        },
      }],
    });

    window.addEventListener('resize', function() { chart.resize(); });
  }
})();
