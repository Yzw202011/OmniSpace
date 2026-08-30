(function () {
  if (typeof mermaid === 'undefined') return;
  mermaid.initialize({
    startOnLoad: true,
    theme: 'base',
    securityLevel: 'loose',
    themeVariables: {
      primaryColor: '#151d29',
      primaryTextColor: '#dbe4f0',
      primaryBorderColor: '#2a3d55',
      secondaryColor: '#1a2432',
      secondaryTextColor: '#dbe4f0',
      tertiaryColor: '#10161f',
      tertiaryTextColor: '#8494a7',
      lineColor: '#4da3ff',
      textColor: '#dbe4f0',
      mainBkg: '#151d29',
      nodeBorder: '#2a3d55',
      clusterBkg: '#10161f',
      clusterBorder: '#1f2c3d',
      edgeLabelBackground: '#10161f',
      actorBkg: '#151d29',
      actorBorder: '#2a3d55',
      actorTextColor: '#dbe4f0',
      actorLineColor: '#2a3d55',
      signalColor: '#8494a7',
      signalTextColor: '#dbe4f0',
      labelBoxBkgColor: '#151d29',
      labelBoxBorderColor: '#2a3d55',
      labelTextColor: '#dbe4f0',
      loopTextColor: '#4da3ff',
      noteBkgColor: '#1a2432',
      noteBorderColor: '#2a3d55',
      noteTextColor: '#dbe4f0',
      sequenceNumberColor: '#0a0e14',
      fontFamily: 'JetBrains Mono, Microsoft YaHei, sans-serif',
      fontSize: '13px'
    },
    flowchart: { curve: 'basis', htmlLabels: true, useMaxWidth: true },
    sequence: { useMaxWidth: true, wrap: true }
  });
})();
