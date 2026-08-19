(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var danger = style.getPropertyValue('--danger').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var ok = style.getPropertyValue('--ok').trim();

  function axisCommon() {
    return {
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 11 },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    };
  }

  /* ---------- 图 1：六维健康度雷达 ---------- */
  var radar = echarts.init(document.getElementById('chart-radar'), null, { renderer: 'svg' });
  radar.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'item' },
    radar: {
      indicator: [
        { name: '需求管理', max: 100 },
        { name: '架构设计', max: 100 },
        { name: '代码实现', max: 100 },
        { name: '测试保障', max: 100 },
        { name: '交付运维', max: 100 },
        { name: '安全合规', max: 100 }
      ],
      radius: '62%',
      center: ['50%', '52%'],
      axisName: { color: ink, fontSize: 12, fontWeight: 600 },
      splitLine: { lineStyle: { color: rule } },
      splitArea: { areaStyle: { color: [bg2, '#f2f4f7'] } },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'radar',
      data: [{
        value: [40, 55, 55, 15, 35, 55],
        name: '健康度评分',
        areaStyle: { color: accent, opacity: 0.18 },
        lineStyle: { color: accent, width: 2.5 },
        itemStyle: { color: accent },
        symbolSize: 6
      }],
      label: { show: true, color: ink, fontSize: 12, fontWeight: 700, formatter: '{c}' }
    }]
  });

  /* ---------- 图 2：严重度 × 环节分布（堆叠） ---------- */
  var severity = echarts.init(document.getElementById('chart-severity'), null, { renderer: 'svg' });
  severity.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, textStyle: { color: muted, fontSize: 12 }, itemWidth: 14, itemHeight: 10 },
    grid: { left: 8, right: 16, top: 42, bottom: 4, containLabel: true },
    xAxis: Object.assign({ type: 'category', data: ['需求', '设计', '后端', '前端', '测试', '运维', '安全'] },
      { axisLabel: { color: ink, fontSize: 12, fontWeight: 600 }, splitLine: { show: false } }),
    yAxis: Object.assign({ type: 'value', max: 4, interval: 1, name: '发现数', nameTextStyle: { color: muted } }, axisCommon()),
    series: [
      { name: '高危', type: 'bar', stack: 'total', barWidth: '46%', itemStyle: { color: danger }, label: { show: true, color: '#fff', fontSize: 11, formatter: function (p) { return p.value > 0 ? p.value : ''; } }, data: [1, 1, 3, 2, 2, 2, 2] },
      { name: '中危', type: 'bar', stack: 'total', itemStyle: { color: warn }, label: { show: true, color: '#fff', fontSize: 11, formatter: function (p) { return p.value > 0 ? p.value : ''; } }, data: [1, 2, 3, 3, 1, 1, 1] },
      { name: '低危', type: 'bar', stack: 'total', itemStyle: { color: ok }, label: { show: true, color: '#fff', fontSize: 11, formatter: function (p) { return p.value > 0 ? p.value : ''; } }, data: [0, 0, 0, 0, 0, 2, 1] }
    ]
  });

  /* ---------- 图 3：RTM 需求状态分布 ---------- */
  var rtm = echarts.init(document.getElementById('chart-rtm'), null, { renderer: 'svg' });
  rtm.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 40, top: 14, bottom: 4, containLabel: true },
    xAxis: Object.assign({ type: 'value', max: 18, name: '条数', nameTextStyle: { color: muted } }, axisCommon()),
    yAxis: Object.assign({
      type: 'category', inverse: true,
      data: ['缺失', '已下载未接线', '已实现', '降级实现', '合理化豁免'],
      axisLabel: { color: ink, fontSize: 12, fontWeight: 600 }, splitLine: { show: false }
    }, { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false } }),
    series: [{
      type: 'bar',
      barWidth: '52%',
      data: [
        { value: 16, itemStyle: { color: danger } },
        { value: 15, itemStyle: { color: accent } },
        { value: 7, itemStyle: { color: ok } },
        { value: 4, itemStyle: { color: warn } },
        { value: 2, itemStyle: { color: muted } }
      ],
      label: { show: true, position: 'right', color: ink, fontWeight: 700, fontSize: 13, formatter: '{c} 条' }
    }]
  });

  /* ---------- 图 4：后端最大文件 TOP 5 ---------- */
  var godfile = echarts.init(document.getElementById('chart-godfile'), null, { renderer: 'svg' });
  godfile.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow', formatter: function (p) { return p[0].name + '<br/>' + p[0].value + ' 行'; } } },
    grid: { left: 8, right: 52, top: 14, bottom: 4, containLabel: true },
    xAxis: Object.assign({ type: 'value', max: 4400, name: '行数', nameTextStyle: { color: muted } }, axisCommon()),
    yAxis: Object.assign({
      type: 'category', inverse: true,
      data: ['manga.py', 'browser_agent_service.py', 'lora_training_service.py', 'system.py', 'video_engine.py'],
      axisLabel: { color: ink, fontSize: 11.5, fontWeight: 600 }, splitLine: { show: false }
    }, { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false } }),
    series: [{
      type: 'bar',
      barWidth: '52%',
      data: [
        { value: 4035, itemStyle: { color: danger } },
        { value: 1574, itemStyle: { color: warn } },
        { value: 1363, itemStyle: { color: warn } },
        { value: 1277, itemStyle: { color: warn } },
        { value: 1268, itemStyle: { color: warn } }
      ],
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: muted, type: 'dashed', width: 1.2 },
        label: { color: muted, fontSize: 11, formatter: '1000 行参考线', position: 'insideEndTop' },
        data: [{ xAxis: 1000 }]
      },
      label: { show: true, position: 'right', color: ink, fontWeight: 700, fontSize: 12, formatter: '{c}' }
    }]
  });

  /* ---------- 图 5：except Exception 密度 TOP 8 ---------- */
  var exceptions = echarts.init(document.getElementById('chart-exceptions'), null, { renderer: 'svg' });
  exceptions.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 34, top: 14, bottom: 4, containLabel: true },
    xAxis: Object.assign({ type: 'value', max: 64, name: '捕获次数', nameTextStyle: { color: muted } }, axisCommon()),
    yAxis: Object.assign({
      type: 'category', inverse: true,
      data: ['manga.py', 'dialog.py', 'system.py', 'models.py', 'learning.py', 'hardware.py', 'draw.py', 'knowledge.py'],
      axisLabel: { color: ink, fontSize: 11.5, fontWeight: 600 }, splitLine: { show: false }
    }, { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false } }),
    series: [{
      type: 'bar',
      barWidth: '55%',
      data: [58, 33, 27, 18, 16, 14, 10, 8],
      itemStyle: { color: accent },
      label: { show: true, position: 'right', color: ink, fontWeight: 700, fontSize: 12, formatter: '{c}' }
    }]
  });

  /* ---------- 图 6：核心模块测试函数数 ---------- */
  var tests = echarts.init(document.getElementById('chart-tests'), null, { renderer: 'svg' });
  tests.setOption({
    animation: false,
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 16, top: 14, bottom: 4, containLabel: true },
    xAxis: Object.assign({
      type: 'category',
      data: ['知识管线', 'API 冒烟', '对话引擎', '绘画引擎', '视频引擎', '语音引擎', '其余推理引擎', '调度器', '模型管理', '前端(全部)'],
      axisLabel: { color: ink, fontSize: 11, fontWeight: 600, interval: 0, rotate: 32 }, splitLine: { show: false }
    }, { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false } }),
    yAxis: Object.assign({ type: 'value', max: 10, interval: 2, name: '测试函数数', nameTextStyle: { color: muted } }, axisCommon()),
    series: [{
      type: 'bar',
      barWidth: '50%',
      data: [
        { value: 8, itemStyle: { color: ok } },
        { value: 8, itemStyle: { color: ok } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } },
        { value: 0, itemStyle: { color: danger } }
      ],
      label: { show: true, position: 'top', color: ink, fontWeight: 700, fontSize: 12, formatter: '{c}' }
    }]
  });

  /* ---------- 图 7：模型磁盘占用与接线状态 ---------- */
  var cats = [
    'paint（SDXL在用/FLUX.2未接）',
    'sd15（AnimateLCM 底座）',
    'video_gen（LTX在用/Hunyuan弃置）',
    'qwen3-32b（GGUF 无加载路径）',
    'qwen3-vl-8b（高档对话候选）',
    '3d（Hunyuan3D/TripoSR 无端点）',
    'codestral-22b（GGUF 无加载路径）',
    'embed（bge-large-zh在用/M3未接）',
    'qwen3-vl-4b（默认对话模型）',
    'cosyvoice2（引擎门控跳过）',
    'qwen3-asr（无 ASR 管线）',
    'qwen3-tts（引擎不识别）',
    'gpt-sovits（引擎不识别）',
    'sam-vit-h（无端点）',
    'qwen2-vl-2b（回退候选）',
    'lora（在用）'
  ];
  var wiredVals = [null, 32.50, null, null, 16.34, null, null, null, 8.28, null, null, null, null, null, 2.30, 0.47];
  var partialVals = [32.61, null, 25.34, null, null, null, null, 9.13, null, null, null, null, null, null, null, null];
  var unwiredVals = [null, null, null, 19.26, null, 15.45, 12.42, null, null, 5.23, 4.38, 4.21, 2.56, 2.39, null, null];
  var models = echarts.init(document.getElementById('chart-models'), null, { renderer: 'svg' });
  var barLabel = {
    show: true, position: 'right', color: ink, fontWeight: 700, fontSize: 11,
    formatter: function (p) { return p.value == null ? '' : p.value + ' GB'; }
  };
  models.setOption({
    animation: false,
    tooltip: {
      appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: function (ps) {
        var out = ps[0].name;
        ps.forEach(function (p) { if (p.value != null) { out += '<br/>' + p.marker + p.seriesName + '：' + p.value + ' GB'; } });
        return out;
      }
    },
    legend: { top: 0, textStyle: { color: muted, fontSize: 12 }, itemWidth: 14, itemHeight: 10 },
    grid: { left: 8, right: 52, top: 40, bottom: 4, containLabel: true },
    xAxis: Object.assign({ type: 'value', max: 36, name: 'GB', nameTextStyle: { color: muted } }, axisCommon()),
    yAxis: Object.assign({
      type: 'category', inverse: true,
      data: cats,
      axisLabel: { color: ink, fontSize: 11 }, splitLine: { show: false }
    }, { axisLine: { lineStyle: { color: rule } }, axisTick: { show: false } }),
    series: [
      { name: '已接线', type: 'bar', stack: 'size', barWidth: '58%', itemStyle: { color: ok }, label: barLabel, data: wiredVals },
      { name: '部分接线', type: 'bar', stack: 'size', itemStyle: { color: accent }, label: barLabel, data: partialVals },
      { name: '未接线', type: 'bar', stack: 'size', itemStyle: { color: danger }, label: barLabel, data: unwiredVals }
    ]
  });

  window.addEventListener('resize', function () {
    radar.resize();
    severity.resize();
    rtm.resize();
    godfile.resize();
    exceptions.resize();
    tests.resize();
    models.resize();
  });
})();
