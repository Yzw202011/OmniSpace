/* 漫剧创作模块 · 选型报告图表 */
(function () {
  function init() {
    if (typeof echarts === 'undefined') {
      setTimeout(init, 300);
      return;
    }
    renderVidTime();
  }

  function baseTheme() {
    return {
      textStyle: { fontFamily: 'WorkSans, system-ui, sans-serif', color: '#111a2e' },
      grid: { left: 50, right: 24, top: 40, bottom: 44, containLabel: true },
    };
  }

  /* 图 1 · 单段 5s 视频本地生成耗时（16GB 显存档，社区实测区间） */
  function renderVidTime() {
    var el = document.getElementById('chart-vid-time');
    if (!el) return;
    var chart = echarts.init(el, null, { renderer: 'canvas' });

    var data = [
      { name: 'Wan2.2-TI2V-5B (480p)', lo: 12, hi: 30, color: '#2c965a', note: '主力' },
      { name: 'CogVideoX-5B-I2V', lo: 15, hi: 45, color: '#2453d0', note: '最稳' },
      { name: 'MiniMax H3 (INT4, 20步)', lo: 120, hi: 420, color: '#d68628', note: '补充·慢' },
      { name: 'Flux 生图基准 (≈6.5GB)', lo: 1, hi: 3, color: '#7a5cff', note: '参照' },
    ];

    var series = [];
    data.forEach(function (d, i) {
      var base = i * 4;
      series.push({
        type: 'bar', name: d.name, barWidth: '38%', data: [d.lo],
        itemStyle: { color: d.color },
        stack: 'g' + i, silent: true,
      });
      series.push({
        type: 'bar', name: '', data: [d.hi - d.lo], stack: 'g' + i,
        itemStyle: { color: 'transparent' }, silent: true,
      });
      series.push({
        type: 'bar', name: d.name + '·上界', barWidth: '38%', data: [d.hi],
        itemStyle: { color: 'transparent', borderColor: 'transparent' },
        label: {
          show: true, position: 'right', color: '#5b6675',
          fontFamily: 'IBMPlexMono', fontSize: 11,
          formatter: function () { return d.note + ' · ' + d.lo + '~' + d.hi + 's'; },
        },
        emphasis: { itemStyle: { color: 'transparent' } },
        tooltip: {
          trigger: 'item',
          formatter: '<b>' + d.name + '</b><br/>单段 5s 本地生成：' + d.lo + ' ~ ' + d.hi + ' 秒',
        },
      });
    });

    chart.setOption({
      baseOption: Object.assign(baseTheme(), {
        title: {
          text: '单段 5s 视频生成耗时（对数刻度，秒）',
          subtext: '来源：社区实测区间；Flux 为单图参照基准',
          textStyle: { fontFamily: 'BricolageGrotesque', fontweight: 700, fontSize: 14 },
          subtextStyle: { color: '#5b6675', fontSize: 11 },
          top: 0,
        },
        tooltip: {},
        legend: { show: false },
        xAxis: { type: 'value', logBase: 10, axisLabel: { fontFamily: 'IBMPlexMono', fontSize: 11 } },
        yAxis: { type: 'category', data: data.map(function (d) { return d.name; }), axisLabel: { fontFamily: 'WorkSans', fontSize: 12 } },
        series: series,
      }),
      media: [
        { query: { maxWidth: 520 }, option: { grid: { top: 64, bottom: 40 } } },
      ],
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();