(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // --- Chart 1: API route distribution ---
  var endpointData = [
    ['models.py', 30], ['system.py', 27], ['draw.py', 27], ['storyboard.py', 27],
    ['knowledge.py', 26], ['dialog.py', 24], ['style.py', 23], ['comic_asset.py', 21],
    ['learning.py', 19], ['video.py', 17], ['director.py', 17], ['learn.py', 12],
    ['comic.py', 12], ['logs.py', 10], ['browser.py', 7], ['keyframe.py', 7],
    ['voice.py', 6], ['hardware.py', 6], ['vision_tools.py', 6]
  ].slice().sort(function(a, b) { return a[1] - b[1]; });

  var el1 = document.getElementById('chart-endpoints');
  if (el1) {
    var chart1 = echarts.init(el1, null, { renderer: 'svg' });
    chart1.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true,
        formatter: function(p) { return p[0].name + '：' + p[0].value + ' 个路由注册'; } },
      grid: { left: 130, right: 40, top: 16, bottom: 30 },
      xAxis: { type: 'value', axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
      yAxis: {
        type: 'category',
        data: endpointData.map(function(d) { return d[0]; }),
        axisLabel: { color: ink, fontFamily: 'JetBrainsMono, Consolas, monospace', fontSize: 11.5 },
        axisLine: { lineStyle: { color: rule } }, axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        data: endpointData.map(function(d) {
          var mangaFiles = ['storyboard.py', 'comic_asset.py', 'video.py', 'director.py', 'comic.py', 'keyframe.py', 'voice.py'];
          return { value: d[1], itemStyle: { color: mangaFiles.indexOf(d[0]) >= 0 ? accent2 : accent } };
        }),
        barWidth: 15,
        label: { show: true, position: 'right', color: muted, fontSize: 11, fontFamily: 'JetBrainsMono, monospace' }
      }]
    });
    window.addEventListener('resize', function() { chart1.resize(); });
  }

  // --- Chart 2: Model disk usage ---
  var modelData = [
    ['sd15 (SD1.5 底座)', 32.5], ['sdxl-base-1.0', 32.6], ['ltx-video-0.9.5', 23.7],
    ['qwen3-32b (Q4 GGUF)', 19.3], ['qwen3-vl-8b (awq)', 16.3], ['hunyuan3d-2.1', 13.9],
    ['flux2-klein-4b', 13.2], ['codestral-22b (Q4)', 12.4], ['bge-m3', 7.9],
    ['qwen3-vl-4b', 8.3], ['cosyvoice2', 5.2], ['qwen3-asr', 4.4], ['qwen3-tts', 4.2],
    ['gpt-sovits', 2.6], ['sam-vit-h', 2.4], ['qwen2-vl-2b', 2.3],
    ['AnimateLCM', 1.7], ['TripoSR', 1.6], ['bge-large-zh', 1.2], ['whisper-tiny', 0.1]
  ].slice().sort(function(a, b) { return a[1] - b[1]; });

  var el2 = document.getElementById('chart-models');
  if (el2) {
    var chart2 = echarts.init(el2, null, { renderer: 'svg' });
    chart2.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true,
        formatter: function(p) { return p[0].name + '：' + p[0].value + ' GB'; } },
      grid: { left: 165, right: 50, top: 16, bottom: 30 },
      xAxis: { type: 'value', name: 'GB', nameTextStyle: { color: muted },
        axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
      yAxis: {
        type: 'category',
        data: modelData.map(function(d) { return d[0]; }),
        axisLabel: { color: ink, fontSize: 11.5 },
        axisLine: { lineStyle: { color: rule } }, axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        data: modelData.map(function(d) { return d[1]; }),
        itemStyle: { color: accent },
        barWidth: 14,
        label: { show: true, position: 'right', color: muted, fontSize: 11, fontFamily: 'JetBrainsMono, monospace',
          formatter: '{c} GB' }
      }]
    });
    window.addEventListener('resize', function() { chart2.resize(); });
  }
})();
