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
  // Chart 1: Radar — 六大方案对比
  // ============================================================
  var radarDom = document.getElementById('chart-radar');
  if (radarDom) {
    var radarChart = echarts.init(radarDom, null, { renderer: 'svg' });

    var indicators = [
      { name: '身份保持', max: 10 },
      { name: '文本可控性', max: 10 },
      { name: '风格纯净度', max: 10 },
      { name: '多帧序列', max: 10 },
      { name: 'FLUX 适配', max: 10 },
      { name: '易用性/成熟度', max: 10 },
    ];

    radarChart.setOption({
      animation: false,
      tooltip: {
        trigger: 'item',
        appendToBody: true,
      },
      legend: {
        data: ['PuLID-FLUX', 'IP-Adapter-FaceID', 'WithAnyone', 'InstantCharacter', 'StoryDiffusion', 'InstantID'],
        bottom: 0,
        textStyle: { color: ink, fontSize: 11 },
        itemGap: 16,
        type: 'scroll',
      },
      radar: {
        indicator: indicators,
        center: ['50%', '48%'],
        radius: '62%',
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
          symbolSize: 5,
          lineStyle: { width: 2 },
          areaStyle: { opacity: 0.15 },
          data: [
            {
              value: [9.5, 8.5, 9.5, 3, 9.5, 7.5],
              name: 'PuLID-FLUX',
              itemStyle: { color: accent },
              lineStyle: { color: accent },
              areaStyle: { color: accent },
            },
            {
              value: [8.5, 8, 7, 3, 8, 9.5],
              name: 'IP-Adapter-FaceID',
              itemStyle: { color: success },
              lineStyle: { color: success },
              areaStyle: { color: success },
            },
            {
              value: [9, 9, 9, 2, 9, 4],
              name: 'WithAnyone',
              itemStyle: { color: accent2 },
              lineStyle: { color: accent2 },
              areaStyle: { color: accent2 },
            },
            {
              value: [8.5, 9, 8, 3, 8, 5],
              name: 'InstantCharacter',
              itemStyle: { color: warning },
              lineStyle: { color: warning },
              areaStyle: { color: warning },
            },
            {
              value: [8, 8, 8, 9.5, 5, 7.5],
              name: 'StoryDiffusion',
              itemStyle: { color: '#8b5cf6' },
              lineStyle: { color: '#8b5cf6' },
              areaStyle: { color: '#8b5cf6' },
            },
            {
              value: [8, 6.5, 6.5, 3, 5, 9],
              name: 'InstantID',
              itemStyle: { color: muted },
              lineStyle: { color: muted },
              areaStyle: { color: muted },
            },
          ],
        },
      ],
    });

    window.addEventListener('resize', function() { radarChart.resize(); });
  }
})();
