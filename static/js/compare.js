// Batch 2 — Compare feature (localStorage based)
(function () {
  var STORAGE_KEY = 'vm_compare_ids';
  var MAX_ITEMS = 4;

  function getIds() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.filter(function (x) { return typeof x === 'number'; }) : [];
    } catch (e) {
      return [];
    }
  }

  function setIds(ids) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(ids.slice(0, MAX_ITEMS)));
    } catch (e) {}
    updateBar();
  }

  function updateBar() {
    var bar = document.getElementById('compare-bar');
    var countEl = document.getElementById('compare-count');
    var goEl = document.getElementById('compare-go');
    if (!bar || !countEl || !goEl) return;

    var ids = getIds();
    if (ids.length === 0) {
      bar.classList.add('hidden');
      return;
    }
    bar.classList.remove('hidden');
    countEl.textContent = ids.length + (ids.length === 1 ? ' item' : ' items');
    goEl.href = '/compare?ids=' + ids.join(',');
  }

  function toggle(id) {
    var ids = getIds();
    var idx = ids.indexOf(id);
    if (idx >= 0) {
      ids.splice(idx, 1);
    } else {
      if (ids.length >= MAX_ITEMS) {
        ids.shift();
      }
      ids.push(id);
    }
    setIds(ids);
    return ids.indexOf(id) >= 0;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var buttons = document.querySelectorAll('[data-compare-id]');
    buttons.forEach(function (btn) {
      var id = parseInt(btn.getAttribute('data-compare-id'), 10);
      if (isNaN(id)) return;

      var ids = getIds();
      if (ids.indexOf(id) >= 0) {
        btn.classList.add('bg-plum-100', 'border-plum-400');
      }

      btn.addEventListener('click', function (e) {
        e.preventDefault();
        var added = toggle(id);
        if (added) {
          btn.classList.add('bg-plum-100', 'border-plum-400');
        } else {
          btn.classList.remove('bg-plum-100', 'border-plum-400');
        }
      });
    });

    var clearBtn = document.getElementById('compare-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        setIds([]);
      });
    }

    updateBar();
  });
})();
