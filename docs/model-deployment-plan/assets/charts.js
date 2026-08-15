(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var accent3 = style.getPropertyValue('--accent3').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();

  // --- Chart 1: VRAM Budget ---
  var chart1 = echarts.init(document.getElementById('chart-vram'), null, { renderer: 'svg' });
  chart1.setOption({
    animation: false,
    tooltip: { trigger: 'item', appendToBody: true, formatter: '{b}: {c} GB ({d}%)' },
    legend: { bottom: 0, textStyle: { color: muted, fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['50%', '45%'],
      avoidLabelOverlap: true,
      label: { show: true, color: ink, fontSize: 12, formatter: '{b}\n{c} GB' },
      labelLine: { lineStyle: { color: rule } },
      data: [
        { value: 4.5, name: 'Qwen3-VL-8B (INT4)', itemStyle: { color: accent } },
        { value: 2.0, name: 'BGE-M3', itemStyle: { color: accent2 } },
        { value: 1.0, name: 'OS/驱动', itemStyle: { color: muted } },
        { value: 8.5, name: '可用（按需切换）', itemStyle: { color: accent3 } }
      ]
    }]
  });
  window.addEventListener('resize', function() { chart1.resize(); });

  // --- Chart 2: FP8 vs FP4 Comparison ---
  var chart2 = echarts.init(document.getElementById('chart-fp4'), null, { renderer: 'svg' });
  chart2.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' } },
    legend: { bottom: 0, textStyle: { color: muted, fontSize: 12 } },
    grid: { top: 30, left: '8%', right: '5%', bottom: 60 },
    xAxis: {
      type: 'category',
      data: ['Qwen3-VL-8B', 'FLUX.2 Klein 9B', 'FLUX.2 dev 32B', 'HunyuanVideo 1.5', 'Hunyuan3D 2.1'],
      axisLabel: { color: muted, fontSize: 11, rotate: 15 },
      axisLine: { lineStyle: { color: rule } }
    },
    yAxis: {
      type: 'value',
      name: '显存 (GB)',
      nameTextStyle: { color: muted },
      axisLabel: { color: muted },
      axisLine: { lineStyle: { color: rule } },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    },
    series: [
      {
        name: 'FP8',
        type: 'bar',
        data: [8, 10, 32, 14, 13],
        itemStyle: { color: accent2, borderRadius: [4, 4, 0, 0] },
        barWidth: '30%'
      },
      {
        name: 'FP4 (cu130)',
        type: 'bar',
        data: [4, 5, 13, 7, 6.5],
        itemStyle: { color: accent3, borderRadius: [4, 4, 0, 0] },
        barWidth: '30%'
      },
      {
        name: '16GB 限制',
        type: 'line',
        data: [16, 16, 16, 16, 16],
        lineStyle: { color: warn, type: 'dashed', width: 2 },
        symbol: 'none',
        markLine: { silent: true, symbol: 'none', lineStyle: { color: warn }, data: [{ yAxis: 16, label: { formatter: '16GB 限制', color: warn } }] }
      }
    ]
  });
  window.addEventListener('resize', function() { chart2.resize(); });

  // --- Chart 3: Disk Usage ---
  var chart3 = echarts.init(document.getElementById('chart-disk'), null, { renderer: 'svg' });
  chart3.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' }, formatter: '{b}: {c} GB' },
    grid: { top: 20, left: '15%', right: '5%', bottom: 20 },
    xAxis: {
      type: 'value',
      name: 'GB',
      nameTextStyle: { color: muted },
      axisLabel: { color: muted },
      axisLine: { lineStyle: { color: rule } },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    },
    yAxis: {
      type: 'category',
      data: ['FLUX ControlNet\n(P2)', 'CosyVoice2\n(P1)', 'F5-TTS\n(P1)', 'BGE-M3\n(P1)', 'FLUX.2 Klein 4B\n(P0)', 'Hunyuan3D 2.1\n(P1)', 'HunyuanVideo 1.5\n(P0)', 'Qwen3-VL-8B\n(P0)'],
      axisLabel: { color: muted, fontSize: 11 },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'bar',
      data: [
        { value: 3, itemStyle: { color: accent2 } },
        { value: 2, itemStyle: { color: accent3 } },
        { value: 2, itemStyle: { color: accent3 } },
        { value: 2, itemStyle: { color: accent3 } },
        { value: 8, itemStyle: { color: accent } },
        { value: 8, itemStyle: { color: accent3 } },
        { value: 16, itemStyle: { color: accent } },
        { value: 16, itemStyle: { color: accent } }
      ],
      label: { show: true, position: 'right', color: ink, fontSize: 11, formatter: '{c} GB' },
      barWidth: '55%'
    }]
  });
  window.addEventListener('resize', function() { chart3.resize(); });
})();
