/* Dashboard UX bundle — Tier 3 (toasts, scroll-to-top, period-sync,
 * table-filter). Pure JS, no deps. Loaded via base.html. */

(function () {
  'use strict';

  // -------------------------------------------------------------------
  // 3A. Toast notifications
  // -------------------------------------------------------------------
  function showToast(message, kind) {
    kind = kind || 'info';  // info | success | error | warn
    var container = document.getElementById('toast-container');
    if (!container) return;
    var t = document.createElement('div');
    t.className = 'toast toast-' + kind;
    t.textContent = message;
    container.appendChild(t);
    // Animate in
    requestAnimationFrame(function () {
      t.classList.add('toast-show');
    });
    // Auto-dismiss after 5s
    setTimeout(function () {
      t.classList.remove('toast-show');
      setTimeout(function () { t.remove(); }, 300);
    }, 5000);
  }
  window.showToast = showToast;

  // Wire HTMX afterRequest to surface apply success/error as toasts.
  document.body.addEventListener('htmx:afterRequest', function (e) {
    var path = e.detail.requestConfig.path || '';
    var verb = e.detail.requestConfig.verb || '';
    var status = e.detail.xhr ? e.detail.xhr.status : 0;
    // Apply suggestions
    if (verb === 'post' && path.indexOf('/api/advisor/apply/') === 0) {
      if (status >= 200 && status < 300) {
        showToast('Suggestion applied — git commit created', 'success');
      } else {
        showToast('Apply failed (HTTP ' + status + ')', 'error');
      }
    }
    // Run advisor on demand
    if (verb === 'post' && path === '/api/advisor/run-now') {
      if (status >= 200 && status < 300) {
        showToast('Advisor job started — polling for results…', 'info');
      }
    }
    // Settle expired positions on demand (Positions tab)
    if (verb === 'post' && path === '/api/positions/settle') {
      if (status >= 200 && status < 300) {
        showToast('Liquidação concluída — tabela atualizada', 'success');
      } else {
        showToast('Falha na liquidação (HTTP ' + status + ')', 'error');
      }
    }
  });

  // Also expose response errors as toasts (HTMX swaps error responses too)
  document.body.addEventListener('htmx:responseError', function (e) {
    var path = e.detail.requestConfig.path || '';
    showToast('Request failed: ' + path, 'error');
  });
  document.body.addEventListener('htmx:sendError', function () {
    showToast('Network error — server unreachable', 'error');
  });

  // -------------------------------------------------------------------
  // 3F. Scroll-to-top button
  // -------------------------------------------------------------------
  var scrollBtn = document.getElementById('scroll-top');
  if (scrollBtn) {
    window.addEventListener('scroll', function () {
      if (window.scrollY > 500) {
        scrollBtn.classList.add('show');
      } else {
        scrollBtn.classList.remove('show');
      }
    });
    scrollBtn.addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  // -------------------------------------------------------------------
  // 3G. Period sync across Performance + Costs tabs
  // -------------------------------------------------------------------
  var PERIOD_KEY = 'pm-dash-period-days';

  function getStoredPeriod() {
    try { return localStorage.getItem(PERIOD_KEY); }
    catch (e) { return null; }
  }
  function setStoredPeriod(days) {
    try { localStorage.setItem(PERIOD_KEY, String(days)); }
    catch (e) {}
  }

  // Hijack period-selector buttons (any element with data-period-days attr)
  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-period-days]');
    if (!el) return;
    var d = el.getAttribute('data-period-days');
    if (d) setStoredPeriod(d);
  });

  // On page load, if URL has ?days= and we're on Performance/Costs, store it.
  // If URL is missing days BUT a stored value exists, redirect to use it.
  (function syncPeriodOnLoad() {
    var path = window.location.pathname;
    if (path !== '/performance' && path !== '/costs') return;
    var url = new URL(window.location.href);
    var urlDays = url.searchParams.get('days');
    if (urlDays) {
      setStoredPeriod(urlDays);
      return;
    }
    var stored = getStoredPeriod();
    if (stored && stored !== '30') {  // 30 = default, no redirect needed
      url.searchParams.set('days', stored);
      window.location.replace(url.toString());
    }
  })();

  // -------------------------------------------------------------------
  // 3C. Table filter — client-side substring match across visible cells
  // Activated by adding data-filter-target="<table-id>" to any <input>.
  // -------------------------------------------------------------------
  document.addEventListener('input', function (e) {
    var inp = e.target;
    if (!inp.matches || !inp.matches('[data-filter-target]')) return;
    var tableId = inp.getAttribute('data-filter-target');
    var table = document.getElementById(tableId);
    if (!table) return;
    var query = inp.value.trim().toLowerCase();
    var rows = table.querySelectorAll('tbody tr');
    var visible = 0;
    rows.forEach(function (r) {
      var text = r.textContent.toLowerCase();
      if (!query || text.indexOf(query) !== -1) {
        r.style.display = '';
        visible++;
      } else {
        r.style.display = 'none';
      }
    });
    // Optional counter
    var counter = document.getElementById(tableId + '-filter-count');
    if (counter) {
      counter.textContent = query
        ? visible + ' of ' + rows.length + ' rows'
        : '';
    }
  });

})();
