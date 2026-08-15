/*!
 * SUP-10 OmniGuide - 本地化交互式引导模块（driver.js 风格，零依赖离线实现）
 * 为离线安装包提供功能导览：元素高亮聚光灯 + 步骤气泡 + 键盘导航
 * 用法:
 *   var tour = OmniGuide.create({ steps: [...], onComplete: fn });
 *   tour.start();
 */
(function (global) {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var STORAGE_PREFIX = 'omniguide-done:';

  var DEFAULT_TEXTS = {
    next: '下一步',
    prev: '上一步',
    done: '完成',
    skip: '跳过引导',
    stepOf: '第 {{current}} / {{total}} 步'
  };

  function fmt(tpl, map) {
    return tpl.replace(/\{\{(\w+)\}\}/g, function (_, k) {
      return map[k] != null ? map[k] : '';
    });
  }

  function createEl(tag, cls, parent) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (parent) parent.appendChild(el);
    return el;
  }

  function injectStyles() {
    if (document.getElementById('omniguide-styles')) return;
    var css = ''
      + '.og-overlay{position:fixed;inset:0;z-index:99990;pointer-events:auto}'
      + '.og-overlay svg{position:absolute;inset:0;width:100%;height:100%}'
      + '.og-spotlight-hole{transition:all .25s ease}'
      + '.og-popover{position:fixed;z-index:99999;max-width:340px;background:var(--bg-secondary,#1e1e2e);'
      + 'color:var(--text-primary,#eee);border:1px solid var(--border,#444);border-radius:12px;'
      + 'padding:16px 18px;box-shadow:0 12px 40px rgba(0,0,0,.45);font-size:14px;line-height:1.6;'
      + 'transition:top .25s ease,left .25s ease}'
      + '.og-popover h4{margin:0 0 8px;font-size:15px;font-weight:600}'
      + '.og-popover .og-desc{margin:0 0 12px;opacity:.85;font-size:13px}'
      + '.og-popover .og-footer{display:flex;align-items:center;justify-content:space-between;gap:8px}'
      + '.og-popover .og-progress{font-size:12px;opacity:.6}'
      + '.og-popover .og-btns{display:flex;gap:8px}'
      + '.og-btn{padding:6px 14px;border-radius:8px;border:1px solid var(--border,#555);'
      + 'background:var(--bg-tertiary,#2a2a3c);color:inherit;font-size:13px;cursor:pointer}'
      + '.og-btn:hover{filter:brightness(1.15)}'
      + '.og-btn.og-primary{background:var(--accent,#e91e63);border-color:transparent;color:#fff}'
      + '.og-skip{position:fixed;z-index:99999;top:16px;right:20px;background:none;border:none;'
      + 'color:#fff;opacity:.7;font-size:13px;cursor:pointer;text-decoration:underline}'
      + '.og-skip:hover{opacity:1}'
      + '.og-highlighted{position:relative;z-index:99995 !important}';
    var style = createEl('style');
    style.id = 'omniguide-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function Tour(opts) {
    this.steps = (opts && opts.steps) || [];
    this.texts = Object.assign({}, DEFAULT_TEXTS, (opts && opts.texts) || {});
    this.onComplete = (opts && opts.onComplete) || null;
    this.onSkip = (opts && opts.onSkip) || null;
    this.tourId = (opts && opts.id) || 'default';
    this.current = -1;
    this._els = null;
    this._keyHandler = this._onKey.bind(this);
    this._repositionHandler = this._reposition.bind(this);
    this._repositionTimer = null;
  }

  Tour.prototype.start = function () {
    if (!this.steps.length) return;
    injectStyles();
    this.current = 0;
    this._render();
    document.addEventListener('keydown', this._keyHandler, true);
    // 修复: 窗口缩放/页面滚动后聚光灯需跟随目标重新定位
    window.addEventListener('resize', this._repositionHandler);
    window.addEventListener('scroll', this._repositionHandler, true);
  };

  /** 重新测量目标坐标并同步聚光灯镂空与气泡位置 */
  Tour.prototype._reposition = function () {
    if (!this._els || !this._els.target || !this._els.hole || !this._els.pop) return;
    var rect = this._els.target.getBoundingClientRect();
    this._positionHole(this._els.hole, rect, 8);
    this._positionPopover(this._els.pop, rect, 8);
  };

  Tour.prototype._render = function () {
    this._destroyEls();
    var step = this.steps[this.current];
    var target = step.element && document.querySelector(step.element);

    // 遮罩层（带镂空聚光灯）
    var overlay = createEl('div', 'og-overlay', document.body);
    var svg = document.createElementNS(SVG_NS, 'svg');
    var mask = document.createElementNS(SVG_NS, 'mask');
    mask.id = 'og-mask';
    var maskBg = document.createElementNS(SVG_NS, 'rect');
    maskBg.setAttribute('x', '0'); maskBg.setAttribute('y', '0');
    maskBg.setAttribute('width', '100%'); maskBg.setAttribute('height', '100%');
    maskBg.setAttribute('fill', 'white');
    mask.appendChild(maskBg);
    var hole = document.createElementNS(SVG_NS, 'rect');
    hole.setAttribute('class', 'og-spotlight-hole');
    hole.setAttribute('rx', '10');
    mask.appendChild(hole);
    svg.appendChild(mask);
    var shade = document.createElementNS(SVG_NS, 'rect');
    shade.setAttribute('x', '0'); shade.setAttribute('y', '0');
    shade.setAttribute('width', '100%'); shade.setAttribute('height', '100%');
    shade.setAttribute('fill', 'rgba(0,0,0,0.62)');
    shade.setAttribute('mask', 'url(#og-mask)');
    svg.appendChild(shade);
    overlay.appendChild(svg);
    // 点击遮罩不穿透，但也不前进（防误触）
    overlay.addEventListener('click', function (e) { e.stopPropagation(); });

    var pad = 8, rect = null;
    if (target) {
      target.classList.add('og-highlighted');
      // 修复: 先滚动再测量。原实现先取 rect 后 smooth 滚动，
      // 滚动未完成导致聚光灯/气泡按旧坐标错位；
      // 滚动结束后通过定时器重测（_reposition）兜底
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      rect = target.getBoundingClientRect();
      var self = this;
      clearTimeout(this._repositionTimer);
      this._repositionTimer = setTimeout(function () { self._reposition(); }, 380);
    }
    this._positionHole(hole, rect, pad);

    // 跳过按钮
    var self = this;
    var skip = createEl('button', 'og-skip', document.body);
    skip.type = 'button';
    skip.textContent = this.texts.skip;
    skip.addEventListener('click', function () { self.skip(); });

    // 气泡
    var pop = createEl('div', 'og-popover', document.body);
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.setAttribute('aria-live', 'polite');
    var title = createEl('h4', '', pop);
    title.textContent = step.title || '';
    var desc = createEl('p', 'og-desc', pop);
    desc.textContent = step.description || '';
    var footer = createEl('div', 'og-footer', pop);
    var progress = createEl('span', 'og-progress', footer);
    progress.textContent = fmt(this.texts.stepOf, {
      current: this.current + 1, total: this.steps.length
    });
    var btns = createEl('div', 'og-btns', footer);

    if (this.current > 0) {
      var prevBtn = createEl('button', 'og-btn', btns);
      prevBtn.type = 'button';
      prevBtn.textContent = this.texts.prev;
      prevBtn.addEventListener('click', function () { self.prev(); });
    }
    var isLast = this.current === this.steps.length - 1;
    var nextBtn = createEl('button', 'og-btn og-primary', btns);
    nextBtn.type = 'button';
    nextBtn.textContent = isLast ? this.texts.done : this.texts.next;
    nextBtn.addEventListener('click', function () {
      if (isLast) self.complete(); else self.next();
    });

    this._positionPopover(pop, rect, pad);
    nextBtn.focus();

    this._els = { overlay: overlay, pop: pop, skip: skip, target: target, hole: hole };
  };

  Tour.prototype._positionHole = function (hole, rect, pad) {
    if (!rect) {
      hole.setAttribute('x', '-9999'); hole.setAttribute('y', '-9999');
      hole.setAttribute('width', '0'); hole.setAttribute('height', '0');
      return;
    }
    hole.setAttribute('x', String(rect.left - pad));
    hole.setAttribute('y', String(rect.top - pad));
    hole.setAttribute('width', String(rect.width + pad * 2));
    hole.setAttribute('height', String(rect.height + pad * 2));
  };

  Tour.prototype._positionPopover = function (pop, rect, pad) {
    var pw = 340, ph = pop.offsetHeight || 160;
    var vw = window.innerWidth, vh = window.innerHeight;
    var left, top;
    if (!rect) {
      left = (vw - pw) / 2; top = (vh - ph) / 3;
    } else {
      // 优先放在目标下方，空间不足则放上方
      top = rect.bottom + pad + 12;
      if (top + ph > vh - 16) top = rect.top - ph - pad - 12;
      if (top < 16) top = Math.max(16, (vh - ph) / 2);
      left = rect.left + rect.width / 2 - pw / 2;
      left = Math.max(16, Math.min(left, vw - pw - 16));
    }
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  };

  Tour.prototype._onKey = function (e) {
    if (e.key === 'Escape') { e.preventDefault(); this.skip(); return; }
    // 修复: Enter 键在按钮/链接上时不拦截——否则会抑制按钮自身 click 默认行为
    // （如焦点在"跳过引导"上按 Enter 反而进入下一步，与按钮语义冲突）
    var t = e.target;
    var onInteractive = t && (t.tagName === 'BUTTON' || t.tagName === 'A' ||
      (t.closest && t.closest('button, a')));
    if (e.key === 'Enter') {
      if (onInteractive) return;  // 交给按钮自身 click 处理
      e.preventDefault();
      if (this.current === this.steps.length - 1) this.complete(); else this.next();
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      if (this.current === this.steps.length - 1) this.complete(); else this.next();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault(); this.prev();
    }
  };

  Tour.prototype.next = function () {
    if (this.current < this.steps.length - 1) {
      this.current++;
      this._render();
    }
  };

  Tour.prototype.prev = function () {
    if (this.current > 0) {
      this.current--;
      this._render();
    }
  };

  Tour.prototype.complete = function () {
    this._markDone();
    this._teardown();
    if (this.onComplete) { try { this.onComplete(); } catch (e) {} }
  };

  Tour.prototype.skip = function () {
    this._markDone();
    this._teardown();
    if (this.onSkip) { try { this.onSkip(); } catch (e) {} }
  };

  Tour.prototype._markDone = function () {
    try { localStorage.setItem(STORAGE_PREFIX + this.tourId, '1'); } catch (e) {}
  };

  Tour.prototype._destroyEls = function () {
    if (!this._els) return;
    if (this._els.target) this._els.target.classList.remove('og-highlighted');
    ['overlay', 'pop', 'skip'].forEach(function (k) {
      var el = this._els[k];
      if (el && el.parentNode) el.parentNode.removeChild(el);
    }, this);
    this._els = null;
  };

  Tour.prototype._teardown = function () {
    this._destroyEls();
    document.removeEventListener('keydown', this._keyHandler, true);
    window.removeEventListener('resize', this._repositionHandler);
    window.removeEventListener('scroll', this._repositionHandler, true);
    clearTimeout(this._repositionTimer);
  };

  var OmniGuide = {
    create: function (opts) { return new Tour(opts); },
    /** 某引导是否已完成 */
    isDone: function (tourId) {
      try { return localStorage.getItem(STORAGE_PREFIX + (tourId || 'default')) === '1'; }
      catch (e) { return false; }
    },
    /** 重置完成标记（设置页"重新观看引导"用） */
    reset: function (tourId) {
      try { localStorage.removeItem(STORAGE_PREFIX + (tourId || 'default')); } catch (e) {}
    }
  };

  global.OmniGuide = OmniGuide;
})(window);
