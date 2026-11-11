// Small calendar popup for every <input class="date"> (dd/mm/yyyy). Typing still works.
(function(){
  const p2 = n => String(n).padStart(2,'0');
  const fmt = d => `${p2(d.getDate())}/${p2(d.getMonth()+1)}/${d.getFullYear()}`;
  function parse(s){
    const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec((s||'').trim()); if(!m) return null;
    const d = new Date(+m[3], +m[2]-1, +m[1]);
    return (d.getFullYear()===+m[3] && d.getMonth()===+m[2]-1 && d.getDate()===+m[1]) ? d : null;
  }
  const same = (a,b) => a && b && a.getFullYear()===b.getFullYear() && a.getMonth()===b.getMonth() && a.getDate()===b.getDate();

  const pop = document.createElement('div');
  pop.className = 'cal'; pop.hidden = true;
  document.body.appendChild(pop);
  let target = null, view = null;

  function render(){
    const y = view.getFullYear(), m = view.getMonth();
    const sel = parse(target.value), today = new Date();
    const lead = (new Date(y, m, 1).getDay() + 6) % 7;          // Monday first
    const days = new Date(y, m+1, 0).getDate();
    let h = `<div class="cal-head">
        <button type="button" data-nav="-1" aria-label="Tháng trước">&lsaquo;</button>
        <span>Tháng ${m+1}, ${y}</span>
        <button type="button" data-nav="1" aria-label="Tháng sau">&rsaquo;</button>
      </div><div class="cal-grid">`;
    ['T2','T3','T4','T5','T6','T7','CN'].forEach(n => h += `<span class="cal-dow">${n}</span>`);
    for(let i=0;i<lead;i++) h += '<span></span>';
    for(let d=1; d<=days; d++){
      const dt = new Date(y, m, d);
      const cls = (same(dt, sel) ? ' sel' : '') + (same(dt, today) ? ' today' : '');
      h += `<button type="button" class="cal-day${cls}" data-day="${d}">${d}</button>`;
    }
    h += `</div><div class="cal-foot"><button type="button" data-today="1">Hôm nay</button>
          <button type="button" data-clear="1">Xóa</button></div>`;
    pop.innerHTML = h;
  }
  function place(){
    const r = target.getBoundingClientRect();
    pop.style.left = '0px'; pop.style.top = '0px';
    const w = pop.offsetWidth, hgt = pop.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - w - 8);
    let top = r.bottom + 4;
    if(top + hgt > window.innerHeight - 8 && r.top - hgt - 4 > 8) top = r.top - hgt - 4;   // flip above
    pop.style.left = Math.max(8, left) + 'px'; pop.style.top = top + 'px';
  }
  function open(el){
    target = el;
    const d = parse(el.value) || new Date();
    view = new Date(d.getFullYear(), d.getMonth(), 1);
    pop.hidden = false; render(); place();
  }
  function close(){ pop.hidden = true; target = null; }
  function pick(d){
    target.value = d ? fmt(d) : '';
    target.dispatchEvent(new Event('change', {bubbles:true}));
    close();
  }

  document.addEventListener('focusin', e => { if(e.target.classList && e.target.classList.contains('date')) open(e.target); });
  document.addEventListener('click', e => { if(e.target.classList && e.target.classList.contains('date') && pop.hidden) open(e.target); });
  document.addEventListener('mousedown', e => {
    if(pop.hidden) return;
    if(pop.contains(e.target)){ e.preventDefault(); return; }       // keep focus in the input
    if(e.target !== target) close();
  });
  pop.addEventListener('click', e => {
    const b = e.target.closest('button'); if(!b || !target) return;
    if(b.dataset.nav){ view = new Date(view.getFullYear(), view.getMonth() + (+b.dataset.nav), 1); render(); place(); }
    else if(b.dataset.day) pick(new Date(view.getFullYear(), view.getMonth(), +b.dataset.day));
    else if(b.dataset.today) pick(new Date());
    else if(b.dataset.clear) pick(null);
  });
  document.addEventListener('keydown', e => { if(!pop.hidden && (e.key === 'Escape' || e.key === 'Tab' || e.key === 'Enter')) close(); });
  document.addEventListener('input', e => {                       // typing a full date moves the calendar
    if(pop.hidden || e.target !== target) return;
    const d = parse(target.value); if(d){ view = new Date(d.getFullYear(), d.getMonth(), 1); }
    render();
  });
  window.addEventListener('resize', () => { if(!pop.hidden) close(); });
  window.addEventListener('scroll', () => { if(!pop.hidden && target) place(); }, true);
})();
