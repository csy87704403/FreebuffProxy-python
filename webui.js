// FreebuffProxy Web UI — 完整交互逻辑
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const ERROR_HINTS = {
  '401':'认证失败：Token无效或已过期，需重新登录','403':'禁止访问：账号可能被风控或地区限制',
  '404':'资源不存在：模型/Agent未找到','409':'冲突：Session模型不匹配，需先删除旧Session',
  '429':'限流/额度耗尽：请求过多或每日额度用完','500':'服务器内部错误','502':'网关错误：上游服务不可达',
  '503':'服务不可用：等待队列已满或服务维护','unknown':'未知错误'
};

function flash(msg, isErr) {
  const el = $('#flash');
  el.textContent = msg; el.className = isErr ? 'error' : '';
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 4000);
}

function api(path, opts) {
  return fetch(path, opts).then(r => { if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); });
}

// ============ 导航切换 ============
$$('.nav a').forEach(a => {
  a.addEventListener('click', () => {
    $$('.nav a').forEach(x => x.classList.remove('active'));
    a.classList.add('active');
    $$('.page').forEach(p => p.classList.remove('active'));
    const page = document.getElementById('page-'+a.dataset.page);
    if(page) page.classList.add('active');
  });
});

// ============ 状态 ============
async function loadStatus() {
  try {
    const s = await api('/api/webui/status');
    $('#metrics').innerHTML = `
      <div class="metric"><div class="num">${s.models||0}</div><div class="label">模型数</div></div>
      <div class="metric"><div class="num">${s.tokens||0}</div><div class="label">账号数</div></div>
      <div class="metric"><div class="num">${s.api_keys||0}</div><div class="label">API Key</div></div>
      <div class="metric"><div class="num">${Math.floor((s.uptime_sec||0)/60)}m</div><div class="label">运行时长</div></div>
    `;
  } catch(e) { flash('状态加载失败', true); }
}

// ============ 账号管理 ============
let authSession = null, authPollAbort = null;

async function startAuth() {
  const status = $('#auth-status');
  status.textContent = '生成中...';
  try {
    const s = await api('/api/webui/auth/code', {method:'POST'});
    authSession = s;
    $('#auth-login-url').textContent = s.loginUrl;
    $('#auth-login-url').href = s.loginUrl;
    $('#auth-step2').style.display = 'block';
    $('#auth-result').style.display = 'none';
    status.textContent = '已生成链接，请登录后粘贴回调URL';
    navigator.clipboard.writeText(s.loginUrl).catch(()=>{});
  } catch(e) { status.textContent = '生成失败: '+e.message; }
}

function copyUrl() {
  navigator.clipboard.writeText($('#auth-login-url').textContent).catch(()=>{});
  flash('链接已复制');
}

function cancelAuth() {
  if(authPollAbort) { authPollAbort.abort(); authPollAbort=null; }
  authSession=null;
  $('#auth-step2').style.display='none';
  $('#auth-progress').style.display='none';
  $('#auth-result').style.display='none';
  $('#auth-status').textContent='';
}

async function verifyAuth() {
  const raw = $('#auth-callback-input').value.trim();
  if(!raw) { flash('请粘贴回调URL',true); return; }
  let code = raw;
  if(raw.includes('auth_code=')) { try{ const u=new URL(raw); code=u.searchParams.get('auth_code'); }catch(e){} }
  if(!code||code.split('.').length<3) { flash('URL格式不对，请粘贴完整回调URL',true); return; }
  if(authSession&&authSession.fingerprintId) {
    if(code.split('.')[0]!==authSession.fingerprintId) { flash('Fingerprint不匹配，请重新生成链接',true); return; }
  }
  const btn = document.querySelector('#auth-step2 .btn-primary');
  btn.disabled=true; btn.textContent='验证中...';
  $('#auth-progress').style.display='block';
  $('#auth-progress-bar').style.width='0%';
  $('#auth-progress-text').textContent='等待登录完成...';
  authPollAbort = new AbortController();
  try {
    let user=null;
    for(let i=0;i<15;i++) {
      if(authPollAbort.signal.aborted) throw new Error('已取消');
      $('#auth-progress-bar').style.width = Math.min(((i+1)/15)*100,100)+'%';
      const r = await fetch('/api/webui/auth/status',{method:'POST',signal:authPollAbort.signal,
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fingerprintId:authSession.fingerprintId,fingerprintHash:authSession.fingerprintHash,expiresAt:authSession.expiresAt})});
      if(!r.ok) throw new Error('状态检查失败');
      const d=await r.json();
      if(d.error) throw new Error(d.error);
      if(!d.pending&&d.user) { user=d.user; break; }
      await new Promise(r=>setTimeout(r,2000));
    }
    if(!user) throw new Error('超时：30秒内未完成登录');
    const ir = await fetch('/api/webui/auth/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({authToken:user.authToken,email:user.email,name:user.name,fingerprintId:user.fingerprintId})});
    const id = await ir.json();
    if(!id.ok) throw new Error(id.error||'导入失败');
    $('#auth-result').style.display='block';
    $('#auth-result-detail').textContent = `账号: ${user.name} (${user.email}) | Token: ${user.authToken.slice(0,8)}***`;
    $('#auth-step2').style.display='none';
    $('#auth-progress').style.display='none';
    $('#auth-status').textContent = '✅ 登录成功';
    flash('Token已添加');
    refreshAccounts();
  } catch(e) {
    if(e.message!=='已取消') { $('#auth-status').textContent='验证失败: '+e.message; flash('验证失败',true); }
  } finally {
    btn.disabled=false; btn.textContent='验证并获取Token';
    authPollAbort=null; $('#auth-progress').style.display='none';
  }
}

async function refreshAccounts() {
  const tb = $('#account-table tbody');
  try {
    const snaps = await api('/api/webui/tokens');
    tb.innerHTML = '';
    if(!snaps.length) { tb.innerHTML='<tr><td colspan="5" style="color:#8b949e">暂无账号</td></tr>'; return; }
    snaps.forEach(s => {
      const name = s.name||(s.token?s.token.slice(0,8)+'***':'Token');
      let st,cls;
      if(s.cooldown_until&&new Date(s.cooldown_until)>new Date()) { st='冷却'; cls='badge-warn'; }
      else if(s.last_error) { st='异常'; cls='badge-bad'; }
      else { st='正常'; cls='badge-good'; }
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${name}</td><td><span class="badge ${cls}">${st}</span></td><td>${s.runs?s.runs.length:0}</td><td style="font-size:12px;word-break:break-all">${s.last_error||'—'}</td><td><button class="btn btn-sm btn-danger" onclick="delAccount('${s.token||''}')">删除</button></td>`;
      tb.appendChild(tr);
    });
  } catch(e) { tb.innerHTML='<tr><td colspan="5">加载失败</td></tr>'; }
}

async function delAccount(token) {
  if(!confirm('确定删除此账号？')) return;
  try {
    await api('/api/webui/auth/account/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});
    flash('已删除');
    refreshAccounts();
  } catch(e) { flash('删除失败',true); }
}

// ============ 模型管理 ============
let allModels = [];

async function loadModels() {
  try {
    const res = await fetch('/api/webui/models');
    if(!res.ok) throw new Error('HTTP '+res.status);
    const d = await res.json();
    allModels = d.models||[];
    renderModelList();
    flash(`已拉取 ${allModels.length} 个模型`);
  } catch(e) {
    flash('拉取模型失败: '+e.message, true);
  }
}

function renderModelList() {
  const box = $('#model-list');
  box.innerHTML = '';
  allModels.forEach(m => {
    const div = document.createElement('div');
    div.style.cssText = 'display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid #21262d';
    div.innerHTML = `
      <input type="checkbox" ${m.enabled?'checked':''} onchange="toggleModel('${m.id}',this.checked)">
      <span style="flex:1;font-size:13px">${m.id}</span>
      <span class="chip ${m.status==='ok'?'ok':m.status==='err'?'err':''}" style="font-size:11px">${m.status||'未知'}</span>
      ${m.latency_ms?`<span class="chip time" style="font-size:11px">${m.latency_ms}ms</span>`:''}
      ${m.error_code?`<span style="color:#f85149;font-size:11px">${m.error_code} — ${errHint(m.error_code)}</span>`:''}
    `;
    box.appendChild(div);
  });
}

async function toggleModel(modelId, enabled) {
  try {
    await api('/api/webui/models/toggle',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:modelId,enabled})});
  } catch(e) { flash('操作失败',true); }
}

async function probeModels() {
  const btn = document.querySelector('#page-accounts .btn-primary+.btn');
  if(btn) { btn.disabled=true; btn.textContent='探测中...'; }
  try {
    const results = await api('/api/webui/probe');
    if(allModels.length) {
      allModels.forEach(m => {
        const r = results.find(x=>x.model===m.id);
        if(r) { m.status=r.status; m.latency_ms=r.latency_ms; m.error_code=r.error_code; }
      });
    }
    renderModelList();
    flash('探测完成');
  } catch(e) { flash('探测失败: '+e.message, true); }
  if(btn) { btn.disabled=false; btn.textContent='探测延迟'; }
}

function errHint(code) { return ERROR_HINTS[code]||ERROR_HINTS['unknown']; }

// ============ IP 池管理 ============
let ipPool = [];

async function loadIPPool() {
  try {
    const d = await api('/api/webui/proxy/pool');
    ipPool = d.entries||[];
    renderIPTable();
  } catch(e) { /* 忽略 */ }
}

function renderIPTable() {
  const tb = $('#ip-table tbody');
  tb.innerHTML = '';
  ipPool.forEach((p,i) => {
    const color = p.mode==='full'?'#3fb950':p.mode==='limited'?'#f47067':'#8b949e';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:12px">${p.addr}</td>
      <td><span style="color:${color}">${p.mode.toUpperCase()}</span></td>
      <td>${p.latency_ms?p.latency_ms+'ms':'—'}</td>
      <td>${p.alive?'✅':'❌'}</td>
      <td>${p.fail_count||0}</td>
      <td><button class="btn btn-sm btn-danger" onclick="delIP(${i})">删除</button></td>
    `;
    tb.appendChild(tr);
  });
}

function addIPBatch() {
  const input = $('#ip-input').value.trim();
  if(!input) return;
  const lines = input.split('\n').map(s=>s.trim()).filter(s=>s&&s.includes(':'));
  if(!lines.length) { flash('无效输入',true); return; }
  api('/api/webui/proxy/pool',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',addrs:lines})}).then(() => {
      flash(`已添加 ${lines.length} 个`);
      loadIPPool();
    }).catch(e=>flash('添加失败',true));
}

async function detectAllIPs() {
  const btn = document.querySelector('#page-ips .btn-primary');
  if(btn) btn.disabled=true;
  flash('检测中...');
  try {
    await api('/api/webui/proxy/refresh',{method:'POST'});
    flash('检测完成');
    loadIPPool();
  } catch(e) { flash('检测失败: '+e.message, true); }
  if(btn) btn.disabled=false;
}

function saveIPPool() {
  api('/api/webui/proxy/pool',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'save'})}).then(()=>flash('已保存')).catch(e=>flash('保存失败',true));
}

function delIP(idx) {
  const addr = ipPool[idx]?.addr;
  if(!addr) return;
  api('/api/webui/proxy/pool',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'remove',addr})}).then(() => {
      flash('已删除');
      loadIPPool();
    }).catch(e=>flash('删除失败',true));
}

// ============ API Keys 管理 ============
async function loadAPIKeys() {
  try {
    const d = await api('/api/webui/apis');
    $('#base-url').textContent = d.base_url||'获取中...';
    renderAPITable(d.apis||[]);
  } catch(e) { /* 忽略 */ }
}

function renderAPITable(keys) {
  const tb = $('#api-table tbody');
  tb.innerHTML = '';
  keys.forEach(k => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><code>${k}</code></td><td><button class="btn btn-sm btn-danger" onclick="delAPIKey('${k}')">删除</button></td>`;
    tb.appendChild(tr);
  });
  if(!keys.length) tb.innerHTML = '<tr><td colspan="2" style="color:#8b949e">暂无API Key</td></tr>';
}

async function createAPIKey() {
  const key = $('#api-key-input').value.trim();
  if(!key) { flash('请输入Key',true); return; }
  try {
    await api('/api/webui/apis/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    flash('已创建');
    $('#api-key-input').value='';
    loadAPIKeys();
  } catch(e) { flash('创建失败',true); }
}

async function delAPIKey(key) {
  if(!confirm('确定删除？')) return;
  try {
    await api('/api/webui/apis/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    flash('已删除');
    loadAPIKeys();
  } catch(e) { flash('删除失败',true); }
}

function refreshAPIKeys() { loadAPIKeys(); }

// ============ 用量 ============
async function loadUsage() {
  const days = $('#usage-days').value;
  try {
    const d = await api('/api/webui/usage?days='+days);
    const m = $('#usage-model tbody'); m.innerHTML='';
    const k = $('#usage-key tbody'); k.innerHTML='';
    const byModel = d.by_model||{};
    const byKey = d.by_api_key||{};
    const entriesM = Object.entries(byModel).sort((a,b)=>b[1]-a[1]);
    const entriesK = Object.entries(byKey).sort((a,b)=>b[1]-a[1]);
    if(!entriesM.length) m.innerHTML='<tr><td colspan="2" style="color:#8b949e">暂无数据</td></tr>';
    entriesM.forEach(([model,tokens]) => {
      const tr=document.createElement('tr'); tr.innerHTML=`<td>${model}</td><td>${tokens.toLocaleString()}</td>`; m.appendChild(tr);
    });
    if(!entriesK.length) k.innerHTML='<tr><td colspan="2" style="color:#8b949e">暂无数据</td></tr>';
    entriesK.forEach(([key,tokens]) => {
      const short = key.length>12?key.slice(0,6)+'***'+key.slice(-4):(key||'(默认)');
      const tr=document.createElement('tr'); tr.innerHTML=`<td>${short}</td><td>${tokens.toLocaleString()}</td>`; k.appendChild(tr);
    });
  } catch(e) { flash('用量加载失败',true); }
}

// ============ 日志 ============
async function loadLogs() {
  try {
    const d = await api('/api/webui/logs');
    const box = $('#log-box');
    box.innerHTML = '';
    (d.logs||[]).forEach(l => {
      const div = document.createElement('div');
      div.className = 'log-line log-'+(l.level||'info');
      div.textContent = `[${l.time}] [${l.level}] [${l.source}] ${l.message}`;
      box.appendChild(div);
    });
    if(!(d.logs||[]).length) box.innerHTML='<div class="log-line" style="color:#8b949e">暂无日志</div>';
  } catch(e) {}
}

async function clearLogs() {
  try {
    await api('/api/webui/logs/clear',{method:'POST'});
    loadLogs(); flash('日志已清空');
  } catch(e) { flash('清空失败',true); }
}

// ============ 设置 ============
async function changePassword() {
  const old = $('#pwd-old').value.trim();
  const pwd = $('#pwd-new').value.trim();
  if(!old||!pwd) { $('#pwd-result').textContent='请填写当前密码和新密码'; return; }
  try {
    await api('/api/webui/settings/password',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({old_password:old,new_password:pwd})});
    $('#pwd-old').value=''; $('#pwd-new').value='';
    $('#pwd-result').textContent='✅ 密码已修改';
    flash('密码已修改');
  } catch(e) { $('#pwd-result').textContent='❌ 修改失败: '+e.message; }
}

// ============ 定时刷新 ============
setInterval(()=>{ loadLogs(); }, 5000);
setInterval(()=>{ refreshAccounts(); }, 15000);

// ============ 初始化 ============
(async function init() {
  loadStatus();
  refreshAccounts();
  loadIPPool();
  loadAPIKeys();
  loadUsage();
  loadLogs();
})();