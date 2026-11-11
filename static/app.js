const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const api = (u, opt) => fetch(u, opt).then(r => r.json());
const fmtBytes = b => b > 1e6 ? (b/1e6).toFixed(1)+' MB' : (b/1e3).toFixed(0)+' KB';
const fmtTime = t => t ? new Date(t*1000).toLocaleString('vi-VN') : '-';
const fmtDur = s => { s=Math.round(s||0); return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; };

// tabs
$$('.tab').forEach(t => t.onclick = () => {
  $$('.tab').forEach(x=>x.classList.remove('active'));
  $$('.tab-panel').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  $('#tab-'+t.dataset.tab).classList.add('active');
  if(t.dataset.tab==='logs') loadLogs();
  // recordings tab: do NOT auto-load — user must pick a filter and press Xem
});
function gotoTab(name){ const b=[...$$('.tab')].find(x=>x.dataset.tab===name); if(b) b.click(); }

// ---------------- devices
async function loadDevices(){
  const devs = await api('/api/devices');
  const el = $('#device-list'); el.innerHTML='';
  const filt = $('#rec-filter'); filt.innerHTML='<option value="">Tất cả thiết bị</option>';
  devs.forEach(d=>{
    filt.innerHTML += `<option value="${d.id}">${d.name}</option>`;
    el.innerHTML += `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div><h3>${d.name}</h3><div class="host">${d.host}:${d.port}</div></div>
        <span class="badge ${d.enabled?'on':'off'}">${d.enabled?'Đang bật':'Tắt'}</span>
      </div>
      <div class="meta">
        Chu kỳ <b>${d.interval_min}'</b> · Lùi ${d.lookback_days} ngày<br>
        File đã tải: <b>${d.recordings}</b> · Lần chạy: ${fmtTime(d.last_run)}
      </div>
      <div class="status">${d.last_status||'chưa chạy'}</div>
      <div class="card-actions">
        <button class="primary" onclick="downloadNow(${d.id})">⬇ Tải ngay</button>
        <button onclick="callLog(${d.id})">📋 Nhật ký cuộc gọi</button>
        <button onclick="editDevice(${d.id})">Sửa</button>
        <button class="danger" onclick="delDevice(${d.id})">Xóa</button>
      </div>
    </div>`;
  });
  window._devices = devs;
}
let dlDevice = null;
function downloadNow(id){                 // open date-range popup
  dlDevice = id;
  const dev = (window._devices||[]).find(x=>x.id===id);
  $('#dl-title').textContent = 'Tải file ghi âm — ' + (dev?dev.name:('#'+id));
  const today = new Date().toISOString().slice(0,10);
  $('#dl-from').value = today; $('#dl-to').value = today;
  $('#dl-modal').classList.remove('hidden');
}
$('#dl-cancel').onclick = ()=>$('#dl-modal').classList.add('hidden');
$('#dl-go').onclick = async ()=>{
  const from=$('#dl-from').value, to=$('#dl-to').value;
  if(!from||!to){ alert('Chọn khoảng ngày'); return; }
  $('#dl-modal').classList.add('hidden');
  const r = await api(`/api/devices/${dlDevice}/download`,
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from,to})});
  alert(r.msg||'Đã bắt đầu tải'); setTimeout(loadDevices, 1500);
};
async function delDevice(id){
  if(!confirm('Xóa thiết bị này? (file đã tải vẫn giữ)')) return;
  await api(`/api/devices/${id}`,{method:'DELETE'}); loadDevices();
}
const FIELDS=['id','name','host','port','user','interval_min','lookback_days'];
function openModal(d){
  d = d||{};
  $('#modal-title').textContent = d.id?'Sửa thiết bị':'Thêm thiết bị';
  FIELDS.forEach(k=>{ const e=$('#f-'+k); if(e) e.value = d[k]!==undefined && d[k]!==null ? d[k] : (e.defaultValue||''); });
  $('#f-password').value=''; $('#f-password').placeholder = d.id?'để trống = giữ nguyên':'nhập mật khẩu';
  $('#f-enabled').checked = d.enabled===undefined?true:!!d.enabled;
  $('#test-result').textContent='';
  $('#modal').classList.remove('hidden');
}
function editDevice(id){ openModal((window._devices||[]).find(x=>x.id===id)); }
$('#btn-add-device').onclick = ()=>openModal({});
$('#btn-cancel').onclick = ()=>$('#modal').classList.add('hidden');
function formData(){
  const d={}; ['name','host','port','user','interval_min','lookback_days'].forEach(k=>d[k]=$('#f-'+k).value);
  const pw=$('#f-password').value; if(pw) d.password=pw;          // blank = keep current
  d.method='db6066'; d.db_folder='';                             // standalone only
  d.enabled = $('#f-enabled').checked?1:0; return d;
}
$('#btn-save').onclick = async ()=>{
  const d=formData(); const id=$('#f-id').value;
  if(id) await api(`/api/devices/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  else   await api('/api/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  $('#modal').classList.add('hidden'); loadDevices();
};
$('#btn-test').onclick = async ()=>{
  $('#test-result').textContent='đang test...';
  const d=formData();
  // save-less test: create temp via PUT needs id; use a transient endpoint by posting then deleting is heavy.
  // simplest: require save first if new. Here we test an existing device if editing.
  const id=$('#f-id').value;
  if(!id){ $('#test-result').textContent='Lưu trước rồi Test'; return; }
  const r = await api(`/api/devices/${id}/test`,{method:'POST'});
  $('#test-result').textContent = (r.ok?'✓ ':'✗ ')+(r.msg||'')+(r.ok?'':' — xem tab Log');
};

// ---------------- recordings (call log) — nothing loads until user presses "Xem"
let recPage = 1, recLoaded = false;
async function loadRecordings(){
  const did = $('#rec-filter').value;
  const ps = $('#rec-pagesize').value;
  const from = $('#rec-from').value, to = $('#rec-to').value;
  const qs = new URLSearchParams({page: recPage, page_size: ps});
  if(did) qs.set('device', did);
  if(from) qs.set('from', from);
  if(to) qs.set('to', to);
  const r = await api('/api/recordings?'+qs.toString());
  recLoaded = true;
  $('#rec-count').textContent = `${r.total} cuộc gọi`;
  const tb = $('#rec-table tbody'); tb.innerHTML='';
  if(!r.items.length){
    $('#rec-table').style.display='none'; $('#pager').style.display='none';
    $('#rec-empty').style.display='block';
    $('#rec-empty').textContent = 'Không có cuộc gọi nào khớp bộ lọc.';
    return;
  }
  $('#rec-empty').style.display='none';
  r.items.forEach(x=>{
    const dir = x.direction==='Gọi đi' ? '<span class="dir out">↗ Gọi đi</span>'
              : x.direction==='Gọi đến' ? '<span class="dir in">↘ Gọi đến</span>' : (x.direction||'');
    tb.innerHTML += `<tr>
      <td>${x.call_time||'-'}</td><td>${dir}</td><td>${x.phone||'-'}</td><td>${x.channel||'-'}</td>
      <td>${x.device_name}</td><td>${fmtDur(x.duration)}</td><td>${fmtBytes(x.bytes)}</td>
      <td><button class="link" onclick="play(${x.id}, ${JSON.stringify((x.phone||'')+' · '+(x.call_time||''))})">▶ Nghe</button></td>
      <td><a href="/api/recordings/${x.id}/download"><button>⬇</button></a></td>
    </tr>`;
  });
  $('#rec-table').style.display='';
  $('#pager').style.display = r.pages>1 ? 'flex' : 'none';
  $('#pg-info').textContent = `Trang ${r.page}/${r.pages}`;
  $('#pg-prev').disabled = r.page<=1;
  $('#pg-next').disabled = r.page>=r.pages;
}
function play(id,title){
  $('#player-wrap').style.display='block';
  $('#player-title').textContent = title||'';
  const p=$('#player'); p.src='/api/recordings/'+id+'/play'; p.play();
  $('#player-wrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function applyFilter(){ recPage=1; loadRecordings(); }
$('#btn-apply-filter').onclick = applyFilter;
$('#rec-pagesize').onchange = ()=>{ if(recLoaded) applyFilter(); };
$('#pg-prev').onclick = ()=>{ if(recPage>1){recPage--; loadRecordings();} };
$('#pg-next').onclick = ()=>{ recPage++; loadRecordings(); };
// ---------------- live call-log popup (fetch CDR directly from device)
let clDevice = null;
function callLog(did){
  clDevice = (window._devices||[]).find(x=>x.id===did);
  $('#cl-title').textContent = 'Nhật ký cuộc gọi — ' + (clDevice?clDevice.name:('#'+did));
  const today = new Date().toISOString().slice(0,10);
  const wk = new Date(Date.now()-6*864e5).toISOString().slice(0,10);
  $('#cl-from').value = wk; $('#cl-to').value = today;
  $('#cl-status').textContent='';
  $('#cl-player-wrap').style.display='none';
  $('#cl-body').innerHTML = '<div class="muted" style="padding:24px;text-align:center">Chọn khoảng ngày rồi bấm <b>Lấy nhật ký</b> — dữ liệu lấy trực tiếp từ thiết bị.</div>';
  $('#cl-modal').classList.remove('hidden');
}
$('#cl-close').onclick = ()=>{ $('#cl-player').pause(); $('#cl-modal').classList.add('hidden'); };
$('#cl-load').onclick = async ()=>{
  if(!clDevice) return;
  const from=$('#cl-from').value, to=$('#cl-to').value;
  if(!from||!to){ alert('Chọn khoảng ngày'); return; }
  $('#cl-status').textContent='Đang lấy từ thiết bị... (tắt phần mềm gốc nếu đang mở)';
  $('#cl-load').disabled=true;
  try{
    const r = await api(`/api/devices/${clDevice.id}/calllog`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from,to})});
    if(r.error){ $('#cl-status').textContent='Lỗi: '+r.error; $('#cl-body').innerHTML=''; return; }
    $('#cl-status').textContent = `${r.total} cuộc gọi`;
    renderCallLog(r.items);
  } finally { $('#cl-load').disabled=false; }
};
function renderCallLog(items){
  if(!items.length){ $('#cl-body').innerHTML='<div class="muted" style="padding:24px;text-align:center">Không có cuộc gọi trong khoảng này.</div>'; return; }
  let h = `<table><thead><tr><th>Thời điểm</th><th>Hướng</th><th>Số ĐT</th><th>Kênh</th><th>Thời lượng</th><th>Dung lượng</th><th>Thao tác</th></tr></thead><tbody>`;
  items.forEach((x,i)=>{
    const dir = x.direction==='Gọi đi' ? '<span class="dir out">↗ Gọi đi</span>'
              : x.direction==='Gọi đến' ? '<span class="dir in">↘ Gọi đến</span>' : (x.direction||'');
    let act;
    if(x.downloaded) act = `<button class="link" onclick="clPlay(${x.rec_id}, ${JSON.stringify((x.phone||'')+' · '+(x.call_time||''))})">▶ Nghe</button>`;
    else if(x.downloadable) act = `<button id="clbtn${i}" onclick='clFetch(${i})'>⬇ Tải & nghe</button>`;
    else act = '<span class="muted">—</span>';
    h += `<tr><td>${x.call_time||'-'}</td><td>${dir}</td><td>${x.phone||'-'}</td><td>${x.channel||'-'}</td>
      <td>${fmtDur(x.rec_time)}</td><td>${fmtBytes(x.filesize)}</td><td>${act}</td></tr>`;
  });
  h += '</tbody></table>';
  $('#cl-body').innerHTML = h;
  window._clItems = items;
}
async function clFetch(i){
  const x = window._clItems[i];
  const btn = $('#clbtn'+i);
  btn.disabled=true; btn.textContent='Đang tải...';
  try{
    const r = await api(`/api/devices/${clDevice.id}/fetch`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cdr:x.cdr})});
    if(!r.ok){ btn.disabled=false; btn.textContent='⬇ Tải & nghe'; alert('Lỗi tải: '+(r.error||'')); return; }
    x.downloaded=true; x.rec_id=r.rec_id;
    btn.outerHTML = `<button class="link" onclick="clPlay(${r.rec_id}, ${JSON.stringify((x.phone||'')+' · '+(x.call_time||''))})">▶ Nghe</button>`;
    if($('#cl-autoplay').checked) clPlay(r.rec_id,(x.phone||'')+' · '+(x.call_time||''));
  } catch(e){ btn.disabled=false; btn.textContent='⬇ Tải & nghe'; alert('Lỗi: '+e); }
}
function clPlay(rid,title){
  $('#cl-player-wrap').style.display='block';
  $('#cl-player-title').textContent = title||'';
  const p=$('#cl-player'); p.src='/api/recordings/'+rid+'/play'; p.play();
  $('#cl-player-wrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}

// ---------------- logs + update
async function loadLogs(){ const l=await api('/api/logs'); $('#log-box').textContent=l.join('\n'); $('#log-box').scrollTop=1e9; }
$('#btn-refresh-log').onclick = loadLogs;
$('#btn-update').onclick = async ()=>{
  if(!confirm('Cập nhật từ GitHub và khởi động lại?')) return;
  const r = await api('/api/update',{method:'POST'});
  alert((r.ok?'Đã cập nhật, đang khởi động lại...\n\n':'Lỗi:\n')+(r.output||''));
};

loadDevices();
setInterval(()=>{ if($('#tab-devices').classList.contains('active')) loadDevices(); }, 15000);
