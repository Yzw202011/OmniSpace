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
  var danger = style.getPropertyValue('--danger').trim();

  // ============================================================
  // Chart 1: Radar — OmniSpace vs 豆包 一致性能力对比
  // ============================================================
  var radarDom = document.getElementById('chart-radar');
  if (radarDom) {
    var radarChart = echarts.init(radarDom, null, { renderer: 'svg' });

    var indicators = [
      { name: '角色身份锁定', max: 10 },
      { name: '参考图语义解耦', max: 10 },
      { name: '构图/布局控制', max: 10 },
      { name: '序列上下文理解', max: 10 },
      { name: '场景空间一致性', max: 10 },
      { name: '风格一致性', max: 10 },
    ];

    radarChart.setOption({
      animation: false,
      tooltip: {
        trigger: 'item',
        appendToBody: true,
      },
      legend: {
        data: ['OmniSpace (FLUX)', '豆包 Seedream 4.0'],
        bottom: 0,
        textStyle: { color: ink, fontSize: 12 },
        itemGap: 24,
      },
      radar: {
        indicator: indicators,
        center: ['50%', '45%'],
        radius: '65%',
        shape: 'polygon',
        splitNumber: 4,
        axisName: {
          color: ink,
          fontSize: 12,
          fontWeight: 600,
        },
        splitLine: {
          lineStyle: { color: rule, width: 1 },
        },
        splitArea: {
          show: true,
          areaStyle: {
            color: ['rgba(99,102,241,0.02)', 'rgba(99,102,241,0.04)', 'rgba(99,102,241,0.06)', 'rgba(99,102,241,0.08)'],
          },
        },
        axisLine: {
          lineStyle: { color: rule },
        },
      },
      series: [
        {
          type: 'radar',
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { width: 2 },
          areaStyle: { opacity: 0.25 },
          data: [
            {
              value: [3, 2, 2, 1, 5, 5],
              name: 'OmniSpace (FLUX)',
              itemStyle: { color: warning },
              lineStyle: { color: warning },
              areaStyle: { color: warning },
            },
            {
              value: [9, 9, 8, 8, 7, 9],
              name: '豆包 Seedream 4.0',
              itemStyle: { color: accent },
              lineStyle: { color: accent },
              areaStyle: { color: accent },
            },
          ],
        },
      ],
    });

    window.addEventListener('resize', function() { radarChart.resize(); });
  }

  // ============================================================
  // Chart 2: Bar — 三阶段改进后一致性能力提升预期
  // ============================================================
  var progressDom = document.getElementById('chart-progress');
  if (progressDom) {
    var progressChart = echarts.init(progressDom, null, { renderer: 'svg' });

    var categories = ['角色面部一致性', '全身身份一致性', '场景布局一致性', '帧间连贯性', '风格一致性'];

    progressChart.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        axisPointer: { type: 'shadow' },
        valueFormatter: function(v) { return v + ' / 10'; },
      },
      legend: {
        data: ['当前水平', '第一阶段后', '第二阶段后', '第三阶段后'],
        bottom: 0,
        textStyle: { color: ink, fontSize: 12 },
        itemGap: 16,
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '18%',
        top: '8%',
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: categories,
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false },
        axisLabel: {
          color: muted,
          fontSize: 12,
          interval: 0,
        },
      },
      yAxis: {
        type: 'value',
        max: 10,
        min: 0,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      },
      series: [
        {
          name: '当前水平',
          type: 'bar',
          barWidth: '16%',
          itemStyle: {
            color: warning,
            borderRadius: [4, 4, 0, 0],
          },
          data: [3, 2.5, 3, 2, 5],
        },
        {
          name: '第一阶段后',
          type: 'bar',
          barWidth: '16%',
          itemStyle: {
            color: accent,
            borderRadius: [4, 4, 0, 0],
          },
          data: [7.5, 5.5, 3.5, 3, 5.5],
        },
        {
          name: '第二阶段后',
          type: 'bar',
          barWidth: '16%',
          itemStyle: {
            color: accent2,
            borderRadius: [4, 4, 0, 0],
          },
          data: [8.5, 7.5, 7, 5.5, 7],
        },
        {
          name: '第三阶段后',
          type: 'bar',
          barWidth: '16%',
          itemStyle: {
            color: success,
            borderRadius: [4, 4, 0, 0],
          },
          data: [9, 8.5, 8.5, 8, 8.5],
        },
      ],
    });

    window.addEventListener('resize', function() { progressChart.resize(); });
  }
})();
