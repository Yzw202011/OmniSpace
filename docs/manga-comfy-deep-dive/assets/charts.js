(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();

  var el = document.getElementById('chart-vram');
  if (!el || typeof echarts === 'undefined') return;

  var chart = echarts.init(el, null, { renderer: 'svg' });

  var engines = ['vLLM 常驻', 'klein-9b 关键帧采样峰值', 'H3 视频生成峰值'];
  var values = [7.0, 7.5, 12.0];
  var colors = [muted, accent, accent2];

  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      appendToBody: true,
      backgroundColor: bg2,
      borderColor: rule,
      textStyle: { color: ink, fontSize: 12 },
      valueFormatter: function (v) { return v + ' GB'; }
    },
    grid: { left: 56, right: 30, top: 46, bottom: 42 },
    xAxis: {
      type: 'category',
      data: engines,
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 11.5, interval: 0 }
    },
    yAxis: {
      type: 'value',
      name: '显存 GB',
      max: 16,
      splitNumber: 8,
      nameTextStyle: { color: muted, fontSize: 11 },
      axisLabel: { color: muted, fontSize: 11 },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    },
    series: [{
      type: 'bar',
      barWidth: '46%',
      data: values.map(function (v, i) {
        return { value: v, itemStyle: { color: colors[i], borderRadius: [4, 4, 0, 0] } };
      }),
      label: {
        show: true,
        position: 'top',
        color: ink,
        fontSize: 12,
        fontWeight: 600,
        formatter: '{c} GB'
      },
      markLine: {
        silent: true,
        symbol: 'none',
        data: [
          {
            yAxis: 14.4,
            lineStyle: { color: warn, type: 'solid', width: 2 },
            label: {
              color: warn,
              fontSize: 11,
              fontWeight: 600,
              position: 'insideEndTop',
              formatter: '90% 红线 14.4GB'
            }
          },
          {
            yAxis: 16,
            lineStyle: { color: rule, type: 'dashed', width: 1.5 },
            label: {
              color: muted,
              fontSize: 11,
              position: 'insideEndBottom',
              formatter: 'RTX 5070 Ti 16GB'
            }
          }
        ]
      }
    }]
  });

  window.addEventListener('resize', function () { chart.resize(); });
})();
