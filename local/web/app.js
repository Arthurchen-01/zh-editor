/* 清一新教育 · 文章工作台 —— 前端逻辑
   无框架、无构建。原生 DOM + fetch。 */

const TOKEN = document.querySelector('meta[name="qy-token"]').content;

const S = {
  view: 'library',
  only: '',
  q: '',
  items: [],
  stats: {},
  cur: null,          // 当前文章详情
  tab: 'check',
  sel: new Set(),
  status: null,
  aiReply: '',
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n > 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}
function fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN'); }

/* ─────────────── API ─────────────── */

async function api(path, body) {
  const opt = { headers: { 'X-QY-Token': TOKEN } };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let d = null;
  try { d = await r.json(); } catch (e) { d = { ok: false, error: 'HTTP ' + r.status }; }
  if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 9000 : 4200);
}

/* ─────────────── 模态 ─────────────── */

function openModal(html) {
  $('#modal-box').innerHTML = html;
  $('#modal').hidden = false;
  return $('#modal-box');
}
function closeModal() { $('#modal').hidden = true; $('#modal-box').innerHTML = ''; }
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* 后台任务：进度条 + 终端输出 */
function runTask(taskId, title, onDone) {
  const box = openModal(`
    <h2>${esc(title)}</h2>
    <p class="sub" id="tk-cur">正在启动…</p>
    <div class="bar"><i id="tk-bar"></i></div>
    <div class="term" id="tk-log"></div>
    <div class="row"><button class="btn" id="tk-close" disabled>关闭</button></div>`);
  let stop = false;
  $('#tk-close').onclick = () => { stop = true; closeModal(); };
  const tick = async () => {
    if (stop) return;
    let t;
    try { t = (await api('/api/task?id=' + encodeURIComponent(taskId))).task; }
    catch (e) { $('#tk-cur').textContent = '任务查询失败：' + e.message; return; }
    if (!t) { $('#tk-cur').textContent = '任务不存在'; return; }
    const pct = t.total ? Math.round(t.done / t.total * 100) : (t.state === 'running' ? 8 : 100);
    $('#tk-bar').style.width = pct + '%';
    const cur = t.current ? ' · ' + t.current : '';
    $('#tk-cur').textContent = t.state === 'running'
      ? `${t.done}/${t.total || '?'}${cur} · 已用 ${t.elapsed || Math.round(Date.now() / 1000 - t.t0)}s`
      : (t.state === 'done' ? `完成 · 用时 ${t.elapsed}s` : '出错');
    const log = $('#tk-log');
    log.textContent = (t.log || []).join('\n');
    log.scrollTop = log.scrollHeight;
    if (t.state === 'running') return setTimeout(tick, 700);
    $('#tk-close').disabled = false;
    if (t.state === 'error') { $('#tk-cur').textContent = '出错：' + (t.error || ''); }
    if (onDone) onDone(t);
  };
  tick();
}

/* ─────────────── 状态 ─────────────── */

async function loadStatus() {
  const d = await api('/api/status');
  S.status = d;
  const st = d.stats || {};
  S.stats = st;
  $('#n-docs').textContent = fmtNum(st.documents);
  $('#n-blocked').textContent = fmtNum(st.blocked);
  $('#n-rules').textContent = fmtNum(d.rules);
  $('#n-nobody').textContent = fmtNum(Math.max(0, (st.documents || 0) - (st.snapshots || 0)));
  const acct = d.account || {};
  const nm = acct.name || acct.url_token || acct.error;
  const dot = $('#conn-dot');
  if (d.logged_in && !acct.error) {
    dot.className = 'dot on';
    $('#who-text').textContent = nm || '已登录';
    $('#brand-sub').textContent = nm || '清一新教育';
  } else {
    dot.className = 'dot off';
    $('#who-text').textContent = d.logged_in ? '凭证异常' : '未登录';
  }
  $('#btn-login').textContent = d.logged_in ? '更换 Cookie' : '填入 Cookie';
  $('#mode-hint').textContent = d.allow_write
    ? '可写回知乎'
    : '只读模式 · 改动不会上传';
  try {
    const ac = await api('/api/ai/config');
    S.aiConfig = ac.config || {};
    const mn = S.aiConfig.model || 'deepseek-v4.1-flash';
    const aiText = $('#ai-status-text');
    const aiDot = $('#ai-dot');
    if (aiText) aiText.textContent = mn + ' · 已连接';
    if (aiDot) aiDot.className = 'dot on';
  } catch (e) {
    const aiText = $('#ai-status-text');
    const aiDot = $('#ai-dot');
    if (aiText) aiText.textContent = 'AI 接口异常';
    if (aiDot) aiDot.className = 'dot off';
  }
  return d;
}

async function loadList() {
  const d = await api('/api/articles?only=' + encodeURIComponent(S.only) +
    '&q=' + encodeURIComponent(S.q));
  S.items = d.items || [];
  renderList();
}

function renderList() {
  const body = $('#list-body');
  if (!S.items.length) {
    body.innerHTML = `<div class="empty">${
      S.stats.documents ? '没有匹配的文章' : '本地库是空的<br><br>点上方「同步列表」从知乎拉取'}</div>`;
    $('#sel-count').textContent = '未选中';
    return;
  }
  body.innerHTML = S.items.map(it => {
    const lv = it.check_status || 'unchecked';
    const tags = [];
    if (lv === 'block') tags.push(`<span class="tag block">阻断 ${it.check_hits}</span>`);
    else if (lv === 'warn') tags.push(`<span class="tag warn">提醒 ${it.check_hits}</span>`);
    else if (lv === 'pass') tags.push(`<span class="tag pass">已通过</span>`);
    else tags.push(`<span class="tag mute">未检查</span>`);
    if (!it.has_body) tags.push(`<span class="tag mute">缺正文</span>`);
    if (it.upload_status === 'uploaded') tags.push(`<span class="tag info">已上传</span>`);
    if (it.upload_status === 'failed') tags.push(`<span class="tag block">上传失败</span>`);
    if (it.upload_status === 'dirty') tags.push(`<span class="tag warn">待上传</span>`);
    const imgs = it.image_count == null ? '图未知' : `图 ${it.image_count}`;
    const on = S.sel.has(it.doc_id) ? ' checked' : '';
    return `<div class="item${S.cur && S.cur.doc.doc_id === it.doc_id ? ' active' : ''}"
                 data-id="${esc(it.doc_id)}">
      <div class="item-t">${esc(it.title || '(无标题)')}</div>
      <div class="item-m">
        <input type="checkbox" data-pick="${esc(it.doc_id)}"${on}>
        ${tags.join('')}
        <span class="grow item-sub">${imgs}</span>
      </div>
      <div class="item-sub">${esc(it.updated_text || it.synced_text || '')}</div>
    </div>`;
  }).join('');
  updateSelCount();
}

function updateSelCount() {
  const n = S.sel.size;
  $('#sel-count').textContent = n ? `已选 ${n} 篇` : '未选中';
}

/* ─────────────── 详情 ─────────────── */

async function openArticle(id) {
  const d = await api('/api/article/' + encodeURIComponent(id));
  S.cur = d;
  S.tab = 'check';
  S.aiReply = '';
  renderDetail();
  renderList();
}

function renderDetail() {
  const d = S.cur;
  if (!d) return;
  const doc = d.doc || {};
  const chk = d.check || {};
  const lv = chk.level || 'unchecked';
  const lvTag = lv === 'block' ? `<span class="tag block">阻断 ${chk.hits}</span>`
    : lv === 'warn' ? `<span class="tag warn">提醒 ${chk.hits}</span>`
      : lv === 'pass' ? `<span class="tag pass">规则未命中</span>`
        : `<span class="tag mute">未检查</span>`;
  const nF = ((chk.findings) || []).length;
  const nRev = (d.revisions || []).length;
  const cons = d.consistency || {};
  const flag = cons.status && cons.status !== 'ok'
    ? `<span class="tag block">${esc(cons.status)}</span>` : '';

  $('#detail').innerHTML = `
    <div class="d-head">
      <div class="d-title">${esc(d.title || '(无标题)')}</div>
      <div class="d-meta">
        <span>${lvTag}</span>${flag}
        <span>正文 <b>${fmtNum((d.text || '').length)}</b> 字</span>
        <span>图片 <b>${d.image_count}</b> 张</span>
        <span>快照 <b>${esc(d.snapshot_at || '无')}</b></span>
        <span><a href="${esc(d.url)}" target="_blank" rel="noopener">线上原文</a></span>
      </div>
      <div class="tabs">
        <button class="tab${S.tab === 'check' ? ' active' : ''}" data-tab="check">检查<em>${nF || ''}</em></button>
        <button class="tab${S.tab === 'article' ? ' active' : ''}" data-tab="article">原文</button>
        <button class="tab${S.tab === 'edit' ? ' active' : ''}" data-tab="edit">编辑</button>
        <button class="tab${S.tab === 'log' ? ' active' : ''}" data-tab="log">记录<em>${nRev || ''}</em></button>
      </div>
    </div>
    <div class="d-body" id="d-body">${renderTab()}</div>
    <div class="d-foot">
      <button class="btn primary" id="btn-dual-ai" style="background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600">🤖 双轮 AI 智能修润与质检</button>
      <button class="btn" id="btn-export1">导出 Word</button>
      <button class="btn" id="btn-aiprompt">取 AI 提示词</button>
      <button class="btn" id="btn-check1">重新检查</button>
      <button class="btn danger" id="btn-restore">还原原文</button>
      <span class="foot-note" id="foot-note"></span>
    </div>`;

  $$('#detail .tab').forEach(t => t.onclick = () => {
    S.tab = t.dataset.tab; renderDetail();
  });
  $('#btn-dual-ai').onclick = () => showDualAiModal(d.doc.doc_id);
  $('#btn-export1').onclick = () => doExport([d.doc.doc_id]);
  $('#btn-check1').onclick = () => doCheck([d.doc.doc_id], true);
  $('#btn-aiprompt').onclick = () => showAiPrompt(d.doc.doc_id);
  $('#btn-restore').onclick = () => doRestore(d.doc.doc_id);
  bindTab();
  const fn = $('#foot-note');
  if (fn) fn.textContent = S.status && !S.status.allow_write
    ? '只读模式 · 保存按钮不会写回知乎' : '';
}

function renderTab() {
  const d = S.cur;
  if (S.tab === 'check') return renderCheck();
  if (S.tab === 'article') return renderArticle();
  if (S.tab === 'edit') return renderEdit();
  return renderLog();
}

function renderCheck() {
  const d = S.cur;
  const chk = d.check;
  if (!chk) {
    return `<div class="note">这篇还没检查过。点下方「重新检查」跑一遍规则引擎
      （共 ${fmtNum((S.status || {}).rules || 0)} 条规则）。<br><br>
      规则引擎只能查出<b>已知</b>的风险表述。要覆盖未知的，请点
      「取 AI 提示词」，把提示词交给你自己的 AI 再查一遍，然后把它的回复贴回来。</div>`;
  }
  const fs = chk.findings || [];
  const head = `<div class="sec-h">检查结论 · ${esc(chk.engine || '')}
    ${chk.checked_at ? '（' + new Date(chk.checked_at * 1000).toLocaleString('zh-CN') + '）' : ''}
    · 共 ${fs.length} 处</div>`;
  if (!fs.length) {
    return head + `<div class="note">规则引擎未发现已知风险表述。<br>
      <b>这不等于没有问题。</b>请务必用你自己的 AI 再交叉检查一遍。</div>`;
  }
  const order = { block: 0, warn: 1, info: 2 };
  const sorted = fs.slice().sort((a, b) => (order[a.level] ?? 9) - (order[b.level] ?? 9));
  const cards = sorted.map(f => `
    <div class="card ${esc(f.level)}">
      <div class="card-h">
        <span class="lv ${esc(f.level)}">${esc(lvName(f.level))}</span>
        <span class="lb">${esc(f.label || '')}</span>
        <span class="pos">${f.field === 'title' ? '标题' : '正文'}
          ${f.source === 'ai' ? '· AI' : ''} · <span class="rid">${esc(f.rule_id || '')}</span></span>
      </div>
      <div class="card-b">
        ${f.excerpt ? `<div class="why">命中：<span class="hit">${esc(f.text)}</span>
          <span style="color:var(--text-3)">… ${esc(f.excerpt.replace(f.text, '')).slice(0, 70)}</span></div>` : ''}
        ${f.why ? `<div class="why">原因：${esc(f.why)}</div>` : ''}
        ${f.fix ? `<div class="fix">建议：${esc(f.fix)}</div>` : ''}
      </div>
    </div>`).join('');
  const cnt = chk.findings.reduce((m, f) => { m[f.level] = (m[f.level] || 0) + 1; return m; }, {});
  const sum = `<div class="note">阻断 ${cnt.block || 0} · 提醒 ${cnt.warn || 0} · 提示 ${cnt.info || 0}
    &nbsp;|&nbsp; 规则命中<b>只是线索</b>，改法要你自己判断。
    点「取 AI 提示词」可让你自己的 AI 再查一遍，两边结果会自动交叉核对。</div>`;
  return head + sum + cards;
}
function lvName(l) {
  return l === 'block' ? '阻断' : l === 'warn' ? '提醒' : l === 'info' ? '提示' : '通过';
}

function renderArticle() {
  const d = S.cur;
  if (!d.has_body) {
    return `<div class="note">本地还没有这篇的正文。点左侧「同步正文」拉取。</div>`;
  }
  const clean = (d.body_html || '')
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/ on\w+="[^"]*"/gi, '');
  return `<div class="article">${clean}</div>`;
}

function renderEdit() {
  const d = S.cur;
  const canWrite = S.status && S.status.allow_write;
  return `
    <div class="field">
      <label>标题</label>
      <input type="text" id="ed-title" value="${esc(d.title || '')}">
    </div>
    <div class="field">
      <label>正文 HTML（图片用 &lt;img&gt; 标签，原样保留即可）</label>
      <textarea id="ed-body" spellcheck="false">${esc(d.body_html || '')}</textarea>
    </div>
    <div class="note">
      保存前系统会自动做三件事：① 存一份 <b>pre_upload</b> 快照（永不覆盖）；
      ② 对比图片清单，<b>丢图就中止</b>；③ 逐字记录改动，保存后回读线上复核。
      ${canWrite ? '' : '<br><br><b>当前是只读模式</b>，保存按钮不会写回知乎。'}
    </div>
    <div class="row" style="display:flex;gap:8px;margin-top:14px;">
      <button class="btn" id="ed-preview">预览改动</button>
      <button class="btn" id="ed-save">保存到知乎草稿</button>
      <button class="btn primary" id="ed-publish">保存并发布</button>
    </div>`;
}

function renderLog() {
  const d = S.cur;
  const revs = d.revisions || [];
  const tl = d.timeline || [];
  const cons = d.consistency || {};
  let out = '';
  if (cons.status && cons.status !== 'ok') {
    out += `<div class="card block"><div class="card-h">
      <span class="lv block">不一致</span><span class="lb">${esc(cons.status)}</span></div>
      <div class="card-b">${esc(cons.note || JSON.stringify(cons))}</div></div>`;
  }
  out += `<div class="sec-h">改动记录（${revs.length}）</div>`;
  if (!revs.length) out += `<div class="note">这篇还没有任何改动记录。</div>`;
  else out += revs.map(r => `
    <div class="card ${r.applied ? 'pass' : 'info'}">
      <div class="card-h">
        <span class="lv ${r.applied ? 'pass' : 'info'}">${r.applied ? '已写入' : '未写入'}</span>
        <span class="lb">${esc(r.rule_label || r.kind)}</span>
        <span class="pos">${esc(r.field)} · ${esc(r.rule_id || '')}
          ${r.verified ? ' · 已复核' : ''}</span>
      </div>
      <div class="card-b diff">
        <div class="diff-row"><span class="k">原</span>
          <span class="strike">${esc(String(r.before_text || '').slice(0, 180))}</span></div>
        <div class="diff-row"><span class="k">改</span>
          <span class="sugg">${esc(String(r.after_text || '').slice(0, 180))}</span></div>
      </div>
    </div>`).join('');

  out += `<div class="sec-h" style="margin-top:22px;">时间线（${tl.length}）</div>`;
  out += tl.length ? `<div class="tl">${tl.map(e => `
    <div class="tl-item ${e.ok === false ? 'fail' : (e.kind === 'upload' ? 'done' : '')}">
      <div class="tl-t">${esc(e.title || e.kind || '')}</div>
      <div class="tl-d">${esc(e.detail || '')} · ${e.at ? new Date(e.at * 1000).toLocaleString('zh-CN') : ''}</div>
    </div>`).join('')}</div>` : `<div class="note">暂无时间线。</div>`;
  return out;
}

function bindTab() {
  if (S.tab !== 'edit') return;
  const t = $('#ed-title'), b = $('#ed-body');
  if (!t || !b) return;
  const preview = $('#ed-preview');
  const save = $('#ed-save'), pub = $('#ed-publish');
  if (preview) preview.onclick = () => {
    const d = S.cur;
    const oldT = d.title, newT = t.value;
    const oldB = d.body_html, newB = b.value;
    const imgRe = /<img[^>]*>/gi;
    const oi = (oldB.match(imgRe) || []).length;
    const ni = (newB.match(imgRe) || []).length;
    const rows = [];
    if (oldT !== newT) rows.push(`<div class="diff-row"><span class="k">标题</span>
      <span><span class="strike">${esc(oldT)}</span> → <span class="sugg">${esc(newT)}</span></span></div>`);
    rows.push(`<div class="diff-row"><span class="k">正文</span><span>
      长度 ${fmtNum(oldB.length)} → ${fmtNum(newB.length)}；
      图片 ${oi} → ${ni} ${ni < oi ? '<b style="color:var(--danger)">（会丢图，保存会被拦下）</b>' : ''}
    </span></div>`);
    openModal(`<h2>改动预览</h2><p class="sub">还没有写入任何地方。</p>
      <div class="diff">${rows.join('')}</div>
      <div class="row"><button class="btn" onclick="closeModal()">知道了</button></div>`);
  };
  const doSave = async (publish) => {
    if (!confirm(publish
      ? '将把标题和正文写回知乎并发布。确定继续？'
      : '将把标题和正文保存到知乎草稿。确定继续？')) return;
    try {
      const r = await api('/api/save', {
        doc_id: S.cur.doc.doc_id, title: t.value,
        body_html: b.value, publish: !!publish
      });
      if (!r.changed) return toast('内容没有变化', 'good');
      const v = r.verify || {};
      toast(v.ok ? '已写入并复核通过' : '已写入，但复核不一致：' + (v.why || ''),
        v.ok ? 'good' : 'bad');
      await openArticle(S.cur.doc.doc_id);
      await loadStatus();
    } catch (e) { toast('保存失败：' + e.message, 'bad'); }
  };
  if (save) save.onclick = () => doSave(false);
  if (pub) pub.onclick = () => doSave(true);
}

/* ─────────────── 动作 ─────────────── */

async function doInspect(withBody) {
  const bodyLimit = withBody
    ? Number(prompt('同步正文的篇数上限（0 = 全部，越多越慢）：', '30') || 0) : 0;
  if (withBody && bodyLimit === null) return;
  const r = await api('/api/inspect', {
    kinds: ['article'], cap: 0, with_body: withBody, body_limit: bodyLimit
  });
  runTask(r.task_id, withBody ? '同步列表 + 正文' : '同步列表', async () => {
    await loadStatus(); await loadList();
  });
}

async function doCheck(ids, single) {
  if (!ids.length) return toast('先选文章', 'bad');
  const r = await api('/api/check', { doc_ids: ids });
  runTask(r.task_id, `敏感检查 · ${ids.length} 篇`, async () => {
    await loadStatus(); await loadList();
    if (single && ids.length === 1) await openArticle(ids[0]);
  });
}

async function doExport(ids) {
  if (!ids.length) return toast('先选文章', 'bad');
  const r = await api('/api/export', { doc_ids: ids });
  runTask(r.task_id, `导出 Word · ${ids.length} 篇`, async (t) => {
    const res = t.result || {};
    if (res.count) {
      toast(`已导出 ${res.count} 个文件，共 ${fmtBytes(res.total_bytes)}`, 'good');
      try { await api('/api/reveal', { path: res.dir }); } catch (e) { /* 忽略 */ }
    }
  });
}

async function doRestore(id) {
  if (!confirm('还原到最初的原文快照？\n\n默认只改本地记录；如需同时写回知乎，请使用命令行或手动操作。')) return;
  try {
    await api('/api/restore', { doc_id: id, push: false });
    toast('已还原到本地原文快照', 'good');
    await openArticle(id);
  } catch (e) { toast('还原失败：' + e.message, 'bad'); }
}

async function showAiPrompt(id) {
  const r = await api('/api/prompt/' + encodeURIComponent(id));
  openModal(`
    <h2>交给你自己的 AI 检查</h2>
    <p class="sub">下面这段提示词已经带上了这篇文章的全文（${fmtNum(r.chars)} 字）。
      复制 → 粘贴给你常用的 AI（ChatGPT / Claude / 豆包 / DeepSeek 都行）→
      把它回复的整段内容贴回下面第二个框。</p>
    <div class="field"><label>① 提示词</label>
      <textarea id="ai-p" readonly style="min-height:150px">${esc(r.prompt)}</textarea></div>
    <div class="row" style="justify-content:flex-start;margin:8px 0 16px">
      <button class="btn" id="ai-copy">复制提示词</button>
    </div>
    <div class="field"><label>② 把 AI 的回复贴到这里</label>
      <textarea id="ai-r" placeholder="粘贴 AI 回复的完整内容（含 JSON 代码块）…"></textarea></div>
    <div class="row">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="ai-go">解析并交叉核对</button>
    </div>`);
  $('#ai-copy').onclick = async () => {
    const ta = $('#ai-p'); ta.select();
    try { await navigator.clipboard.writeText(ta.value); toast('已复制', 'good'); }
    catch (e) { document.execCommand('copy'); toast('已复制', 'good'); }
  };
  $('#ai-go').onclick = async () => {
    const reply = $('#ai-r').value.trim();
    if (!reply) return toast('还没粘贴 AI 的回复', 'bad');
    try {
      const res = await api('/api/ai_ingest', { doc_id: id, reply });
      closeModal();
      toast(`AI 报 ${res.ai_count} 处，规则报 ${res.rule_count} 处，合并 ${res.merged_count} 处`,
        'good');
      await loadStatus(); await loadList(); await openArticle(id);
      S.tab = 'check'; renderDetail();
    } catch (e) { toast('解析失败：' + e.message, 'bad'); }
  };
}

async function showAiConfigModal() {
  let cfg = S.aiConfig;
  if (!cfg) {
    try { const r = await api('/api/ai/config'); cfg = r.config || {}; } catch(e) { cfg = {}; }
  }
  openModal(`
    <h2>配置 AI 接口</h2>
    <p class="sub">用于「双轮 AI 智能修润与对抗质检」。凭证保存在本地 <code>data/ai_config.json</code>，永不上传。</p>
    <div class="field">
      <label>API Endpoint (接口地址)</label>
      <input type="text" id="ai-cfg-url" value="${esc(cfg.api_url || 'http://156.225.31.92:7863/v1')}">
    </div>
    <div class="field">
      <label>API Key</label>
      <input type="password" id="ai-cfg-key" value="${esc(cfg.api_key || '')}">
    </div>
    <div class="field">
      <label>模型名称 (Model)</label>
      <select id="ai-cfg-model" style="width:100%;padding:8px 10px;border-radius:var(--r);border:1px solid var(--border-2);background:var(--bg)">
        <option value="deepseek-v4.1-flash"${cfg.model === 'deepseek-v4.1-flash' ? ' selected' : ''}>deepseek-v4.1-flash (推荐 · 快速高智能)</option>
        <option value="hy3"${cfg.model === 'hy3' ? ' selected' : ''}>hy3</option>
      </select>
    </div>
    <div class="row" style="margin-top:16px">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="btn-save-aicfg">保存配置</button>
    </div>
  `);
  $('#btn-save-aicfg').onclick = async () => {
    const url = $('#ai-cfg-url').value.trim();
    const key = $('#ai-cfg-key').value.trim();
    const model = $('#ai-cfg-model').value.trim();
    if (!url || !key) return toast('地址和 Key 不能为空', 'bad');
    try {
      const r = await api('/api/ai/config', { api_url: url, api_key: key, model: model });
      S.aiConfig = r.config;
      toast('AI 配置已更新', 'good');
      closeModal();
      await loadStatus();
    } catch(e) {
      toast('保存失败: ' + e.message, 'bad');
    }
  };
}

async function showDualAiModal(id) {
  const d = S.cur;
  if (!d || !d.has_body) return toast('请先同步正文再运行双轮 AI', 'bad');

  const mName = (S.aiConfig && S.aiConfig.model) || 'deepseek-v4.1-flash';

  const r = await api('/api/ai/dual_round', { doc_id: id, model: mName });
  runTask(r.task_id, `🤖 双轮 AI 智能修润与对抗质检`, async (task) => {
    const res = task.result || {};
    if (!res.ok) return toast('双轮 AI 处理未成功: ' + (res.error || ''), 'bad');

    const score = res.adversarial_score || 0;
    const passed = res.passed;
    const scoreColor = score >= 90 ? 'var(--ok)' : score >= 80 ? 'var(--warn)' : 'var(--danger)';
    const scoreBg = score >= 90 ? 'var(--ok-soft)' : score >= 80 ? 'var(--warn-soft)' : 'var(--danger-soft)';

    const changesHtml = (res.changes || []).map(c => `
      <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;margin-bottom:8px">
        <div style="font-size:12px;margin-bottom:4px">
          <span class="strike" style="color:var(--danger)">${esc(c.original_phrase)}</span>
          &nbsp;→&nbsp;
          <span style="color:var(--ok);font-weight:600">${esc(c.modified_phrase)}</span>
        </div>
        <div style="font-size:11.5px;color:var(--text-2)">💡 ${esc(c.reason)}</div>
      </div>
    `).join('');

    openModal(`
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <h2 style="margin:0">🛡️ 双轮 AI 智能修润与假想敌质检结果</h2>
        <span style="background:${scoreBg};color:${scoreColor};font-weight:700;padding:4px 10px;border-radius:6px;font-size:13px">
          对抗安全分: ${score} / 100 · ${passed ? '质检合格' : '待复核'}
        </span>
      </div>
      <p class="sub" style="margin-bottom:14px">
        模型: <b>${esc(res.model)}</b> · 总用时 ${res.total_elapsed}s · ${res.refine_done ? '已触发自动二次自愈精修' : '首轮即通过'}
      </p>

      <div class="card ${passed ? 'pass' : 'warn'}" style="margin-bottom:14px">
        <div class="card-h">
          <span class="lv ${passed ? 'pass' : 'warn'}">${passed ? '裁决通过' : '预警'}</span>
          <span class="lb">假想敌魔鬼审查员点评</span>
          <span class="pos">${esc(res.verdict || '')}</span>
        </div>
        <div class="card-b">
          <div class="why" style="font-size:12.5px;line-height:1.6">${esc(res.critique || '未发现把柄与生硬语病')}</div>
        </div>
      </div>

      <details style="background:var(--bg-code);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;margin-bottom:14px">
        <summary style="font-weight:600;cursor:pointer;color:var(--text)">🧠 查看 AI 1 修润思考过程（动宾自适应与段落衔接逻辑）</summary>
        <div style="font-family:var(--mono);font-size:12px;line-height:1.6;margin-top:8px;white-space:pre-wrap;color:var(--text-2);max-height:160px;overflow-y:auto">
${esc(res.thinking || '')}
        </div>
      </details>

      <div class="sec-h" style="margin-bottom:8px">📋 检出敏感项与上下文自适应置换对照（${(res.changes || []).length} 处）</div>
      <div style="max-height:180px;overflow-y:auto;margin-bottom:14px">
        ${changesHtml || '<div class="note">全文未检出显性敏感词，仅对文风与语境做了润色。</div>'}
      </div>

      <div class="sec-h" style="margin-bottom:8px">✍️ 修润后成品（可原地微调，点击一键采纳存入草稿）</div>
      <div class="field">
        <label>修润后标题</label>
        <input type="text" id="ai-res-title" value="${esc(res.modified_title)}">
      </div>
      <div class="field">
        <label>修润后正文 HTML</label>
        <textarea id="ai-res-body" style="min-height:140px">${esc(res.modified_body)}</textarea>
      </div>

      <div class="row" style="margin-top:14px">
        <button class="btn" onclick="closeModal()">暂不采纳</button>
        <button class="btn primary" id="btn-adopt-ai" style="background:var(--accent);color:#fff;border-color:var(--accent);font-weight:700">
          ✓ 一键采纳双轮修润稿
        </button>
      </div>
    `);

    $('#btn-adopt-ai').onclick = async () => {
      const newTitle = $('#ai-res-title').value.trim();
      const newBody = $('#ai-res-body').value.trim();
      if (!newTitle || !newBody) return toast('标题或正文不能为空', 'bad');
      try {
        await api('/api/ai/apply', { doc_id: id, title: newTitle, body_html: newBody });
        toast('已成功采纳双轮修润稿并存入草稿！', 'good');
        closeModal();
        await openArticle(id);
        S.tab = 'edit';
        renderDetail();
      } catch(e) {
        toast('采纳失败: ' + e.message, 'bad');
      }
    };
  });
}

/* ─────────────── 其他视图 ─────────────── */

async function renderAudit() {
  $('#detail').innerHTML = `<div class="d-head"><div class="d-title">一致性审计</div>
    <div class="d-meta"><span>扫描「改了标题没改正文」「改了正文没改标题」「写了一半」等历史残留</span></div>
    </div><div class="d-body"><div class="empty"><span class="spin"></span> 正在审计…</div></div>`;
  try {
    const d = await api('/api/audit');
    const rem = d.remote || {}, loc = d.local || {};
    const issues = rem.issues || {};
    const rows = Object.entries(issues).map(([k, v]) =>
      `<div class="card ${v && v.length ? 'block' : 'pass'}">
        <div class="card-h"><span class="lv ${v && v.length ? 'block' : 'pass'}">
          ${v && v.length ? '需处理' : '正常'}</span>
        <span class="lb">${esc(k)}</span>
        <span class="pos">${(v && v.length) || 0} 处</span></div>
        ${v && v.length ? `<div class="card-b">${
          v.slice(0, 12).map(x => `<div>· ${esc(typeof x === 'string' ? x : JSON.stringify(x))}</div>`).join('')
        }</div>` : ''}
      </div>`).join('');
    const tail = (d.audit_tail || []).slice(0, 30).map(a =>
      `<div class="tl-item"><div class="tl-t">${esc(a.action)}</div>
       <div class="tl-d">${esc(a.detail || '')} ${esc(a.doc_id || '')} · ${
        a.at ? new Date(a.at * 1000).toLocaleString('zh-CN') : ''}</div></div>`).join('');
    $('#detail').innerHTML = `<div class="d-head"><div class="d-title">一致性审计</div>
      <div class="d-meta">
        <span>本地文档 <b>${fmtNum(loc.has_body)}</b> 篇有正文 / <b>${fmtNum(loc.snapshots)}</b> 份快照</span>
        <span>改动记录 <b>${fmtNum(loc.revisions)}</b> 条</span>
        <span>上传失败 <b>${fmtNum(loc.failed_upload)}</b></span>
      </div></div>
      <div class="d-body">
        <div class="sec-h">线上一致性</div>
        ${rows || '<div class="note">未登录，无法扫描线上。</div>'}
        <div class="sec-h" style="margin-top:22px;">最近操作日志</div>
        <div class="tl">${tail || '<div class="note">暂无</div>'}</div>
      </div>`;
  } catch (e) {
    $('#detail').innerHTML = `<div class="d-body"><div class="note">审计失败：${esc(e.message)}</div></div>`;
  }
}

async function renderRules() {
  const d = await api('/api/rules');
  const byCat = {};
  (d.rules || []).forEach(r => { (byCat[r.cat] = byCat[r.cat] || []).push(r); });
  const secs = Object.entries(byCat).map(([cat, rs]) => `
    <div class="sec-h" style="margin-top:20px">${esc(cat)} · ${rs.length} 条</div>
    ${rs.map(r => `<div class="card ${r.level}">
      <div class="card-h">
        <span class="lv ${r.level}">${lvName(r.level)}</span>
        <span class="lb">${esc(r.label)}</span>
        <span class="pos rid">${esc(r.id)} · ${esc(r.scope)}</span>
      </div>
      <div class="card-b">
        ${r.why ? `<div class="why">${esc(r.why)}</div>` : ''}
        ${r.fix ? `<div class="fix">建议：${esc(r.fix)}</div>` : ''}
      </div></div>`).join('')}`).join('');
  $('#detail').innerHTML = `<div class="d-head"><div class="d-title">检查规则 · 共 ${d.count} 条</div>
    <div class="d-meta"><span>规则只提供线索，不替代判断。改法请自行确认。</span></div></div>
    <div class="d-body">${secs}</div>`;
}

function renderExport() {
  $('#detail').innerHTML = `<div class="d-head"><div class="d-title">导出 Word</div>
    <div class="d-meta"><span>每篇一个 .docx 文件，不打包；图片按原始高清分辨率内嵌</span></div></div>
    <div class="d-body">
      <div class="note">
        <b>怎么导出</b><br>
        1. 在左侧列表勾选要导出的文章（可用「全选」）；<br>
        2. 点列表底部的「导出」；<br>
        3. 完成后文件会落在 <code>${esc((S.status || {}).export_dir || 'exports/')}</code>，
        并自动在访达里打开。<br><br>
        <b>关于体积</b>：高清原图意味着体积大。带图的文章一篇 3–8 MB 是正常的。
        如果只想快速看文字，用「原文」标签页即可，不必导出。
      </div>
      <div class="sec-h" style="margin-top:20px">上次导出统计</div>
      <div class="note">本地库共 ${fmtNum((S.stats || {}).documents)} 篇，
        其中 ${fmtNum((S.stats || {}).snapshots)} 份快照可导出。</div>
    </div>`;
}

/* ─────────────── 事件绑定 ─────────────── */

function bind() {
  $$('.nav-item').forEach(b => b.onclick = async () => {
    $$('.nav-item').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    S.view = b.dataset.view;
    if (S.view === 'audit') return renderAudit();
    if (S.view === 'rules') return renderRules();
    if (S.view === 'export') return renderExport();
    if (S.view === 'blocked') { S.only = 'blocked'; $$('.pill').forEach(p => p.classList.remove('active'));
      $('.pill[data-only="blocked"]').classList.add('active'); }
    else if (S.view === 'nobody') { S.only = ''; }
    else { S.only = ''; $$('.pill').forEach(p => p.classList.remove('active'));
      $('.pill[data-only=""]').classList.add('active'); }
    await loadList();
  });

  $$('.pill').forEach(p => p.onclick = async () => {
    $$('.pill').forEach(x => x.classList.remove('active'));
    p.classList.add('active');
    S.only = p.dataset.only;
    await loadList();
  });

  let tmr;
  $('#q').oninput = e => {
    clearTimeout(tmr);
    tmr = setTimeout(async () => { S.q = e.target.value; await loadList(); }, 220);
  };

  $('#btn-inspect').onclick = () => doInspect(false);
  $('#btn-sync-body').onclick = () => doInspect(true);
  $('#btn-check').onclick = () => doCheck(Array.from(S.sel));
  $('#btn-export').onclick = () => doExport(Array.from(S.sel));

  $('#sel-all').onchange = e => {
    if (e.target.checked) S.items.forEach(i => S.sel.add(i.doc_id));
    else S.sel.clear();
    renderList();
  };

  $('#list-body').addEventListener('click', e => {
    const pick = e.target.closest('[data-pick]');
    if (pick) {
      e.stopPropagation();
      const id = pick.dataset.pick;
      if (pick.checked) S.sel.add(id); else S.sel.delete(id);
      updateSelCount();
      return;
    }
    const item = e.target.closest('.item');
    if (item) openArticle(item.dataset.id).catch(err => toast(err.message, 'bad'));
  });

  $('#btn-login').onclick = () => {
    openModal(`<h2>填入知乎 Cookie</h2>
      <p class="sub">浏览器打开 zhihu.com 并登录 → 按 F12 → Network →
        随便点一个请求 → 复制 Request Headers 里的 <b>cookie</b> 整行 →
        粘贴到下面。<br><br>
        凭证只存在你本机的 <code>cookie.txt</code>，不会上传到任何服务器。</p>
      <div class="field"><textarea id="ck" placeholder="z_c0=…; _zap=…; d_c0=…"></textarea></div>
      <div class="row"><button class="btn" onclick="closeModal()">取消</button>
        <button class="btn primary" id="ck-go">保存并验证</button></div>`);
    $('#ck-go').onclick = async () => {
      const v = $('#ck').value.trim();
      if (!v) return toast('还没粘贴', 'bad');
      try {
        const r = await api('/api/login', { cookie: v });
        if (!r.ok) return toast(r.error || '失败', 'bad');
        const a = r.account || {};
        toast(a.name ? `已登录：${a.name}` : 'Cookie 已保存', 'good');
        closeModal(); await loadStatus(); await loadList();
      } catch (e) { toast('失败：' + e.message, 'bad'); }
    };
  };

  const btnAi = $('#btn-ai-cfg');
  if (btnAi) btnAi.onclick = () => showAiConfigModal();
}

/* ─────────────── 启动 ─────────────── */

(async function boot() {
  bind();
  try {
    await loadStatus();
    await loadList();
  } catch (e) {
    toast('初始化失败：' + e.message, 'bad');
  }
})();
