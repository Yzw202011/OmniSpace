/* OmniSpace UAT 报告图表 — 配色对齐报告设计系统 */
(function () {
  'use strict';

  var C = {
    ink: '#1A2332',
    muted: '#64748B',
    rule: '#E2E8F0',
    accent: '#E8547E',
    accent2: '#0D9488',
    sev0: '#DC2626',
    sev1: '#EA580C',
    sev2: '#CA8A04',
    sev3: '#64748B',
    ok: '#059669'
  };

  function baseText() {
    return { color: C.ink, fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif', fontSize: 13 };
  }

  function initWhenReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  /* ---------- 图 1 · 关键操作耗时实测（对数刻度） ---------- */
  function renderTiming() {
    var el = document.getElementById('chart-timing');
    if (!el || typeof echarts === 'undefined') return;
    var chart = echarts.init(el);

    var cats = [
      'AI 切分分镜 · 冷启动\n（含模型加载 242s）',
      'AI 绘画生成\n（30 步 87s）',
      'AI 切分分镜 · 热启动\n（模型已驻留 18s）',
      '对话一轮总耗时\n（一句话 23s / 第二轮 27s）',
      '前端首屏加载\n（#/chat 2.57s）',
      '对话首字响应\n（空会话+首 token 2.02s）'
    ];

    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 190, right: 90, top: 30, bottom: 46 },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          backgroundColor: '#FFFFFF',
          borderColor: C.rule,
          textStyle: { color: C.ink, fontSize: 13 },
          formatter: function (params) {
            var lines = ['<b>' + params[0].name.replace(/\n/g, '') + '</b>'];
            params.forEach(function (p) {
              if (p.value != null) {
                lines.push(p.marker + ' ' + p.seriesName + '：<b>' + p.value.toFixed(2) + ' s</b>');
              }
            });
            return lines.join('<br>');
          }
        },
        legend: {
          top: 0,
          right: 0,
          textStyle: baseText(),
          itemWidth: 14,
          itemHeight: 9
        },
        xAxis: {
          type: 'log',
          logBase: 10,
          min: 0.1,
          max: 500,
          name: '秒（对数刻度）',
          nameLocation: 'end',
          nameTextStyle: { color: C.muted, fontSize: 12, padding: [0, 0, 0, 8] },
          axisLine: { lineStyle: { color: C.rule } },
          axisTick: { show: false },
          axisLabel: {
            color: C.muted,
            formatter: function (v) {
              return v >= 1 ? v + 's' : '';
            }
          },
          splitLine: { lineStyle: { color: C.rule, type: 'dashed', opacity: 0.6 } }
        },
        yAxis: {
          type: 'category',
          data: cats,
          axisLine: { lineStyle: { color: C.rule } },
          axisTick: { show: false },
          axisLabel: { color: C.ink, fontSize: 12.5, lineHeight: 17, margin: 14 }
        },
        series: [
          {
            name: '本次实测',
            type: 'bar',
            data: [242, 87, 18, 23, 2.568, 2.017],
            barWidth: 17,
            itemStyle: { color: C.accent, borderRadius: [0, 4, 4, 0] },
            label: {
              show: true,
              position: 'right',
              color: C.ink,
              fontWeight: 600,
              fontSize: 12.5,
              formatter: function (p) {
                return p.value >= 10 ? p.value + 's' : p.value.toFixed(2) + 's';
              }
            }
          },
          {
            name: '项目基线（CLAUDE.md）',
            type: 'bar',
            data: [null, 13.6, null, null, null, 0.461],
            barWidth: 17,
            barGap: '-100%',
            itemStyle: { color: C.accent2, opacity: 0.85, borderRadius: [0, 4, 4, 0] },
            label: {
              show: true,
              position: 'right',
              color: C.accent2,
              fontSize: 12,
              formatter: function (p) {
                return p.value != null ? p.value.toFixed(1) + 's' : '';
              }
            }
          }
        ]
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  /* ---------- 图 2 · 问题严重度分布（按发现角色堆叠） ---------- */
  function renderSeverity() {
    var el = document.getElementById('chart-severity');
    if (!el || typeof echarts === 'undefined') return;
    var chart = echarts.init(el);

    var sevs = ['P0 致命', 'P1 严重', 'P2 一般', 'P3 轻微'];
    var roles = [
      { name: 'A 新手·林晓', color: '#E8547E', data: [1, 1, 2, 2] },
      { name: 'B 对话·阿哲', color: '#6366F1', data: [0, 2, 1, 1] },
      { name: 'C 绘画·小满', color: '#0D9488', data: [1, 3, 3, 0] },
      { name: 'D 漫剧·老周', color: '#EA580C', data: [1, 1, 6, 0] },
      { name: 'E 运维·雯姐', color: '#64748B', data: [1, 2, 3, 0] }
    ];
    var sevColors = [C.sev0, C.sev1, C.sev2, C.sev3];

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
          var lines = ['<b>' + params[0].name + '</b>（共 ' + params.reduce(function (s, p) { return s + p.value; }, 0) + ' 项）'];
          params.forEach(function (p) {
            if (p.value > 0) { lines.push(p.marker + ' ' + p.seriesName + '：<b>' + p.value + '</b>'); }
          });
          return lines.join('<br>');
        }
      },
      legend: {
        top: 0,
        textStyle: baseText(),
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
          data: [4, 9, 15, 3],
          tooltip: { show: false },
          label: {
            show: true,
            position: 'top',
            distance: 6,
            fontWeight: 700,
            fontSize: 14,
            color: C.ink,
            formatter: function (p) {
              return [4, 9, 15, 3][p.dataIndex] + ' 项';
            }
          }
        }
      ])
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  initWhenReady(function () {
    renderTiming();
    renderSeverity();
  });
})();
