(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  function tooltip() {
    return { trigger: 'item', appendToBody: true, backgroundColor: bg2, borderColor: rule, textStyle: { color: ink, fontSize: 13 } };
  }

  /* ---- Chart 1: 瓶颈影响矩阵（气泡：x=修复成本，y=影响强度） ---- */
  var matrixEl = document.getElementById('chart-matrix');
  if (matrixEl) {
    var matrix = echarts.init(matrixEl, null, { renderer: 'svg' });
    // [成本x, 影响y, 半径, 名称, 优先级]
    var pts = [
      [3.5, 8.6, 15, 'vLLM 同步阻塞', 'P0'],
      [2.6, 8.2, 13, 'SQLite 写串行', 'P0'],
      [3.0, 7.4, 12, '前端轮询叠加', 'P1'],
      [2.2, 6.8, 11, '3D 主线程渲染', 'P1'],
      [3.2, 6.5, 11, '3D/前端构建体积', 'P1'],
      [2.8, 6.0, 10, 'ChromaDB 逐条检索', 'P1'],
      [3.6, 5.6, 11, '显存驱逐链分散', 'P1'],
      [4.2, 6.2, 12, '模型加载链长', 'P2'],
      [2.0, 5.0, 9, '磁盘阈值口径不一', 'P2'],
      [4.8, 5.4, 11, '调度器 tick 串联', 'P2'],
      [5.2, 4.2, 10, '中间件/限流开销', 'P3'],
      [6.0, 3.4, 9, '多卡/设备扩展', 'P3']
    ];
    matrix.setOption({
      animation: false,
      grid: { left: 42, right: 20, top: 30, bottom: 36 },
      tooltip: Object.assign(tooltip(), { formatter: function (p) {
        return '<b>' + p.data.name + '</b><br/>修复成本：' + p.data.value[0].toFixed(1) + ' / 影响强度：' + p.data.value[1].toFixed(1) + '<br/>优先级：' + p.data.p;
      } }),
      xAxis: {
        type: 'value', min: 1, max: 7, name: '修复成本 →', nameTextStyle: { color: muted },
        axisLine: { lineStyle: { color: rule } }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } }
      },
      yAxis: {
        type: 'value', min: 0, max: 10, name: '影响强度 →', nameTextStyle: { color: muted },
        axisLine: { lineStyle: { color: rule } }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'scatter',
        symbolSize: function (v) { return v[2]; },
        itemStyle: { color: function (p) { return p.data.p === 'P0' ? accent : (p.data.p === 'P1' ? accent2 : '#b9c2d0'); } },
        label: { show: true, formatter: function (p) { return p.data.name; }, position: 'top', color: ink, fontSize: 11, fontFamily: 'WorkSans' },
        emphasis: { label: { fontSize: 12, fontWeight: 'bold' } },
        data: pts
      }],
      graphic: {
        type: 'line', silent: true,
        shape: { x1: matrixEl.clientWidth * 0.55, y1: matrixEl.clientHeight * 0.4, x2: matrixEl.clientWidth * 0.98, y2: 12 },
        style: { stroke: rule, lineDash: [5, 5] }
      }
    });
    window.addEventListener('resize', function () { matrix.resize(); });
  }

  /* ---- Chart 2: 16GB 显存分区理想 vs 现状 ---- */
  var vramEl = document.getElementById('chart-vram');
  if (vramEl) {
    var vram = echarts.init(vramEl, null, { renderer: 'svg' });
    var categories = ['系统/浏览器', 'bge-large-zh 常驻', '对话模型(vLLM)', '绘画模型', '视频模型', '预留余量(>90%)'];
    // 理想：将 ~14.4GB 可调度空间留给推理，0 越界
    var ideal = [1.6, 1.2, 4.5, 3.6, 3.5, 1.6];
    // 现状：常驻偏高、切换期临时峰值，临界段挤占
    var current = [1.8, 1.4, 5.2, 4.0, 2.4, 1.2];
    vram.setOption({
      animation: false,
      grid: { left: 8, right: 40, top: 30, bottom: 20, containLabel: true },
      tooltip: Object.assign(tooltip(), { trigger: 'axis', axisPointer: { type: 'shadow' } }),
      legend: { data: ['理想分区(GB)', '典型占用(GB)'], textStyle: { color: ink }, top: 0 },
      xAxis: { type: 'value', max: 16, name: 'GB', nameTextStyle: { color: muted }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
      yAxis: { type: 'category', data: categories, axisLabel: { color: ink }, axisLine: { lineStyle: { color: rule } } },
      series: [
        { name: '理想分区(GB)', type: 'bar', data: ideal, stack: 's', itemStyle: { color: accent }, barCategoryGap: '35%' },
        { name: '典型占用(GB)', type: 'bar', data: current, stack: 's', itemStyle: { color: accent2 + '66' } }
      ]
    });
    window.addEventListener('resize', function () { vram.resize(); });
  }
})();