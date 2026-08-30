(function() {
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' });
  }

  var nav = document.getElementById('tocNav');
  if (!nav) return;
  var links = Array.prototype.slice.call(nav.querySelectorAll('a'));
  var targets = links.map(function(a) {
    var id = a.getAttribute('href').slice(1);
    return document.getElementById(id);
  }).filter(Boolean);

  function onScroll() {
    var pos = window.scrollY + 120;
    var activeIdx = 0;
    for (var i = 0; i < targets.length; i++) {
      if (targets[i].offsetTop <= pos) activeIdx = i;
    }
    links.forEach(function(a, i) { a.classList.toggle('active', i === activeIdx); });
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();
