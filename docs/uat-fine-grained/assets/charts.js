/* OmniSpace 细粒度测试报告图表 */
(function () {
  'use strict';

  var C = {
    ink: '#1A2332',
    muted: '#64748B',
    rule: '#E2E8F0',
    accent: '#7C3AED',
    accent2: '#0D9488',
    sev0: '#DC2626',
    sev1: '#EA580C',
    sev2: '#CA8A04',
    sev3: '#64748B',
    ok: '#059669'
  };

  function initWhenReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function renderSeverity() {
    var el = document.getElementById('chart-severity');
    if (!el || typeof echarts === 'undefined') return;
    var chart = echarts.init(el);

    var sevs = ['P0 致命', 'P1 严重', 'P2 一般', 'P3 轻微'];
    var roles = [
      { name: 'A 新手·林晓', color: '#E8547E', data: [1, 0, 3, 0] },
      { name: 'B 对话·阿哲', color: '#6366F1', data: [0, 3, 2, 1] },
      { name: 'C 绘画·小满', color: '#0D9488', data: [1, 3, 3, 1] },
      { name: 'D 漫剧·老周', color: '#EA580C', data: [0, 1, 3, 1] },
      { name: 'E 运维·雯姐', color: '#64748B', data: [1, 1, 3, 2] }
    ];

    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 60, right: 30, top: 56, bottom: 40 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: '#FFFFFF',
        borderColor: C.rule,
        textStyle: { color: C.ink, fontSize: 13 },
        formatter: function (params) {
          var total = 0;
          params.forEach(function (p) { if (p.seriesName !== '合计') total += p.value; });
          var lines = ['<b>' + params[0].name + '</b>（共 ' + total + ' 项）'];
          params.forEach(function (p) {
            if (p.value > 0 && p.seriesName !== '合计') {
              lines.push(p.marker + ' ' + p.seriesName + '：<b>' + p.value + '</b>');
            }
          });
          return lines.join('<br>');
        }
      },
      legend: {
        top: 0,
        textStyle: { color: C.ink, fontSize: 13, fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif' },
        itemWidth: 14,
        itemHeight: 9,
        data: roles.map(function (r) { return r.name; })
      },
      xAxis: {
        type: 'category',
        data: sevs,
        axisLine: { lineStyle: { color: C.rule } },
        axisTick: { show: false },
        axisLabel: {
          fontSize: 13,
          fontWeight: 600,
          margin: 14,
          color: function (value) {
            if (value.indexOf('P0') === 0) return C.sev0;
            if (value.indexOf('P1') === 0) return C.sev1;
            if (value.indexOf('P2') === 0) return C.sev2;
            return C.sev3;
          }
        }
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        name: '问题数',
        nameTextStyle: { color: C.muted, fontSize: 12, padding: [0, 0, 4, 0] },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: C.muted },
        splitLine: { lineStyle: { color: C.rule, type: 'dashed' } }
      },
      series: roles.map(function (r) {
        return {
          name: r.name,
          type: 'bar',
          stack: 'total',
          data: r.data,
          barWidth: 74,
          itemStyle: { color: r.color }
        };
      }).concat([
        {
          name: '合计',
          type: 'line',
          silent: true,
          symbol: 'none',
          lineStyle: { width: 0, opacity: 0 },
          data: [3, 8, 12, 5],
          tooltip: { show: false },
          label: {
            show: true,
            position: 'top',
            distance: 6,
            fontWeight: 700,
            fontSize: 14,
            color: C.ink,
            formatter: function (p) {
              return [3, 8, 12, 5][p.dataIndex] + ' 项';
            }
          }
        }
      ])
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  initWhenReady(function () {
    renderSeverity();
  });
})();
