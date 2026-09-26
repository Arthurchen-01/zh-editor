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

/* 静默后台任务轮询（不弹窗阻塞操作） */
function pollTaskSilently(taskId, onDone) {
  const tick = async () => {
    let t;
    try {
      const d = await api('/api/task?id=' + encodeURIComponent(taskId));
      t = d.task;
    } catch (_) { return; }
    if (!t) return;
    if (t.state === 'running') return setTimeout(tick, 1000);
    if (t.state === 'done' || t.state === 'error') {
      if (onDone) onDone(t);
    }
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
  if ($('#n-articles')) $('#n-articles').textContent = fmtNum(st.articles || 244);
  if ($('#n-answers')) $('#n-answers').textContent = fmtNum(st.answers || 3);
  if ($('#n-pins')) $('#n-pins').textContent = fmtNum(st.pins || 15);
  if ($('#n-questions')) $('#n-questions').textContent = fmtNum(st.questions || 2);
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
  const modeHint = $('#mode-hint');
  if (modeHint) {
    modeHint.innerHTML = d.allow_write
      ? `<span class="badge" id="btn-toggle-write" style="cursor:pointer;background:#fee2e2;color:#b91c1c;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600">⚠️ 允许写回知乎 (点击切回只读)</span>`
      : `<span class="badge" id="btn-toggle-write" style="cursor:pointer;background:#f1f5f9;color:#475569;padding:3px 8px;border-radius:4px;font-size:11px">🔒 只读保护 (点击解锁写回)</span>`;
    const btnToggle = $('#btn-toggle-write');
    if (btnToggle) {
      btnToggle.onclick = async () => {
        try {
          const res = await api('/api/mode/toggle_write');
          S.status.allow_write = res.allow_write;
          toast(res.allow_write ? '已开启【允许写回知乎】模式' : '已恢复【只读保护】模式', res.allow_write ? 'warn' : 'good');
          await loadStatus();
          if (S.tab === 'edit') renderDetail();
        } catch (e) { toast('切换模式失败: ' + e.message, 'bad'); }
      };
    }
  }
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
      S.stats.documents ? '没有匹配的内容' : '本地库是空的<br><br>点上方「同步列表」从知乎拉取'}</div>`;
    $('#sel-count').textContent = '未选中';
    return;
  }
  body.innerHTML = S.items.map(it => {
    const lv = it.check_status || 'unchecked';
    const tags = [];
    if (it.kind === 'answer') {
      tags.push(`<span class="tag info" style="background:#e0f2fe;color:#0369a1;font-weight:600">知乎回答</span>`);
    } else if (it.kind === 'pin') {
      tags.push(`<span class="tag info" style="background:#f3e8ff;color:#7e22ce;font-weight:600">知乎想法</span>`);
    } else if (it.kind === 'question') {
      tags.push(`<span class="tag info" style="background:#fef3c7;color:#b45309;font-weight:600">知乎提问</span>`);
    }
    if (lv === 'block') tags.push(`<span class="tag block">阻断 ${it.check_hits}</span>`);
    else if (lv === 'warn') tags.push(`<span class="tag warn">提醒 ${it.check_hits}</span>`);
    else if (lv === 'pass') tags.push(`<span class="tag pass">已通过</span>`);
    else tags.push(`<span class="tag mute">未检查</span>`);
    if (!it.has_body) tags.push(`<span class="tag mute">缺正文</span>`);
    if (it.upload_status === 'uploaded') tags.push(`<span class="tag info">已上传</span>`);
    if (it.upload_status === 'failed') tags.push(`<span class="tag block">上传失败</span>`);
    if (it.upload_status === 'dirty') tags.push(`<span class="tag warn">待上传</span>`);
    const imgs = it.image_count == null ? '图未知' : `图 ${it.image_count}`;
    const social = [];
    if (it.comment_count) social.push(`💬 ${it.comment_count}`);
    if (it.voteup_count) social.push(`👍 ${it.voteup_count}`);
    const socialHtml = social.length ? `<span class="item-sub" style="color:var(--text-2);font-weight:600">${social.join(' · ')}</span>` : '';
    const on = S.sel.has(it.doc_id) ? ' checked' : '';
    return `<div class="item${S.cur && S.cur.doc.doc_id === it.doc_id ? ' active' : ''}"
                 data-id="${esc(it.doc_id)}">
      <div class="item-t">${esc(it.title || '(无标题)')}</div>
      <div class="item-m">
        <input type="checkbox" data-pick="${esc(it.doc_id)}"${on}>
        ${tags.join('')}
        <span class="grow item-sub">${imgs}</span>
        ${socialHtml}
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
  const nCm = d.comment_count || ((d.doc && d.doc.comment_count) || 0);
  const nUp = d.voteup_count || ((d.doc && d.doc.voteup_count) || 0);
  const kindTag = d.kind === 'answer'
    ? `<span class="tag info" style="background:#e0f2fe;color:#0369a1;font-weight:600">知乎回答</span>`
    : d.kind === 'pin'
    ? `<span class="tag info" style="background:#f3e8ff;color:#7e22ce;font-weight:600">知乎想法</span>`
    : d.kind === 'question'
    ? `<span class="tag info" style="background:#fef3c7;color:#b45309;font-weight:600">知乎提问</span>`
    : `<span class="tag" style="background:#f1f5f9;color:#475569">专栏文章</span>`;

  $('#detail').innerHTML = `
    <div class="d-head">
      <div class="d-title">${esc(d.title || '(无标题)')}</div>
      <div class="d-meta">
        <span>${lvTag}</span>${kindTag}${flag}
        <span>正文 <b>${fmtNum((d.text || '').length)}</b> 字</span>
        <span>图片 <b>${d.image_count}</b> 张</span>
        <span>💬 评论 <b>${nCm}</b> 条</span>
        <span>👍 赞同 <b>${nUp}</b> 个</span>
        <span>快照 <b>${esc(d.snapshot_at || '无')}</b></span>
        <span><a href="${esc(d.url)}" target="_blank" rel="noopener">线上原文</a></span>
      </div>
      <div class="tabs">
        <button class="tab${S.tab === 'check' ? ' active' : ''}" data-tab="check">检查<em>${nF || ''}</em></button>
        <button class="tab${S.tab === 'article' ? ' active' : ''}" data-tab="article">原文</button>
        <button class="tab${S.tab === 'edit' ? ' active' : ''}" data-tab="edit">编辑</button>
        <button class="tab${S.tab === 'comments' ? ' active' : ''}" data-tab="comments">读者评论<em>${nCm ? nCm : ''}</em></button>
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
  if (S.tab === 'comments') return renderComments();
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
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <label style="margin:0;font-weight:600">标题</label>
        <span style="font-size:11.5px;color:var(--text-2)">自定义修改标题，保存后自动留存快照</span>
      </div>
      <input type="text" id="ed-title" value="${esc(d.title || '')}" style="font-size:14px;font-weight:600">
    </div>
    <div class="field">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <label style="margin:0;font-weight:600">正文内容 / HTML（保留 &lt;img&gt; 标签即保留图片）</label>
        <button class="btn mini" id="ed-live-preview" style="font-size:11.5px">👁️ 实时排版预览</button>
      </div>
      <textarea id="ed-body" spellcheck="false" style="min-height:220px;font-family:var(--mono);font-size:13px;line-height:1.6">${esc(d.body_html || '')}</textarea>
      <div id="ed-preview-box" style="display:none;margin-top:10px;padding:16px;background:var(--bg-code);border:1px solid var(--border);border-radius:var(--r);max-height:300px;overflow-y:auto"></div>
    </div>
    <div class="note" style="margin-top:10px;line-height:1.6">
      💡 <b>自定义修改与保存说明</b>：<br>
      ① 点击「💾 保存为本地草稿」仅安全保存在本地数据库，生成新版本快照并记录修改历史，随时可多次编辑/AI质检；<br>
      ② 若要写回知乎线上，系统会自动存一份永久 <b>pre_upload</b> 基准快照并对齐图片清单，丢图自动拦截。<br>
      <b>当前模式</b>：${canWrite ? '<span style="color:var(--danger);font-weight:600">⚠️ 已解锁知乎写回权限</span>' : '<span style="color:var(--ok);font-weight:600">🔒 只读保护中（修改仅安全保存在本地）</span>'}
    </div>
    <div class="row" style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
      <button class="btn primary" id="ed-save-local" style="font-weight:700">💾 保存为本地草稿</button>
      <button class="btn" id="ed-preview">🔍 差异对比</button>
      <button class="btn" id="ed-save" style="${canWrite ? 'color:var(--danger);border-color:var(--danger)' : ''}">☁️ 保存到知乎草稿</button>
      <button class="btn" id="ed-publish" style="${canWrite ? 'background:var(--danger);color:#fff;border-color:var(--danger)' : ''}">🚀 保存并直接发布</button>
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

function renderComments() {
  const d = S.cur;
  if (!d) return '';
  const did = d.doc ? d.doc.doc_id : '';
  setTimeout(() => loadAndRenderComments(did), 50);
  return `
    <div class="sec-h" style="display:flex;justify-content:space-between;align-items:center">
      <span>知乎读者真实评论 · 共 <b id="cm-cnt">${d.comment_count || ((d.doc && d.doc.comment_count) || 0)}</b> 条</span>
      <button class="btn mini" id="btn-refresh-cm">刷新评论</button>
    </div>
    <div id="cm-list" style="margin-top:14px">
      <div class="empty"><span class="spin"></span> 正在实时连接知乎拉取读者评论…</div>
    </div>
  `;
}

async function loadAndRenderComments(docId) {
  const box = $('#cm-list');
  if (!box) return;
  const btnRefresh = $('#btn-refresh-cm');
  if (btnRefresh) {
    btnRefresh.onclick = () => loadAndRenderComments(docId);
  }
  try {
    const res = await api('/api/article/' + encodeURIComponent(docId) + '/comments');
    if (!res.ok) {
      box.innerHTML = `<div class="note" style="color:var(--danger)">拉取评论失败：${esc(res.error || '网络异常')}</div>`;
      return;
    }
    const list = res.comments || [];
    const cntEl = $('#cm-cnt');
    if (cntEl) cntEl.textContent = res.total_counts ?? list.length;
    if (!list.length) {
      box.innerHTML = `<div class="empty">该篇内容暂无读者评论。</div>`;
      return;
    }
    box.innerHTML = list.map(c => `
      <div class="card" style="margin-bottom:12px;padding:12px 14px;background:var(--bg)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          ${c.avatar_url ? `<img src="${esc(c.avatar_url)}" style="width:28px;height:28px;border-radius:50%;object-fit:cover">` : ''}
          <div style="font-weight:600;font-size:13px">${esc(c.author_name)}</div>
          <div style="font-size:11px;color:var(--text-3);margin-left:auto">${esc(c.created_text || '')} · 👍 ${c.vote_count || 0}</div>
        </div>
        <div style="font-size:13px;line-height:1.6;color:var(--text);margin-bottom:6px">${c.content}</div>
        ${(c.child_comments && c.child_comments.length) ? `
          <div style="margin-top:8px;padding:8px 12px;background:var(--bg-code);border-radius:var(--r);font-size:12px;border:1px solid var(--border)">
            ${c.child_comments.map(ch => `
              <div style="margin-bottom:6px;line-height:1.5">
                <span style="font-weight:600;color:var(--text-2)">${esc(ch.author_name)}</span>:
                <span>${ch.content}</span>
                <span style="color:var(--text-3);font-size:11px;margin-left:6px">${esc(ch.created_text || '')}</span>
              </div>
            `).join('')}
          </div>
        ` : ''}
      </div>
    `).join('');
  } catch (e) {
    box.innerHTML = `<div class="note" style="color:var(--danger)">请求异常：${esc(e.message)}</div>`;
  }
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
    openModal(`<h2>改动差异对比</h2><p class="sub">尚未写入知乎或本地数据库。</p>
      <div class="diff">${rows.join('')}</div>
      <div class="row"><button class="btn" onclick="closeModal()">知道了</button></div>`);
  };

  const btnSaveLocal = $('#ed-save-local');
  if (btnSaveLocal) btnSaveLocal.onclick = async () => {
    const newTitle = t.value.trim();
    const newBody = b.value.trim();
    if (!newTitle) return toast('标题不能为空', 'bad');
    try {
      const r = await api('/api/save_local', {
        doc_id: S.cur.doc.doc_id, title: newTitle, body_html: newBody
      });
      if (!r.changed) return toast('内容未变动，无需保存', 'good');
      toast(`✓ 已成功保存到本地草稿（${fmtNum(r.body_len)} 字），生成新快照`, 'good');
      await openArticle(S.cur.doc.doc_id);
      await loadStatus();
      S.tab = 'edit';
      renderDetail();
    } catch (e) { toast('保存本地草稿失败: ' + e.message, 'bad'); }
  };

  const btnLivePreview = $('#ed-live-preview');
  const previewBox = $('#ed-preview-box');
  if (btnLivePreview && previewBox) {
    btnLivePreview.onclick = () => {
      const isHidden = previewBox.style.display === 'none';
      if (isHidden) {
        previewBox.style.display = 'block';
        previewBox.innerHTML = `<div style="font-size:16px;font-weight:700;margin-bottom:12px;color:var(--text)">${esc(t.value)}</div><div class="article">${b.value}</div>`;
        btnLivePreview.textContent = '🙈 收起排版预览';
      } else {
        previewBox.style.display = 'none';
        btnLivePreview.textContent = '👁️ 实时排版预览';
      }
    };
  }

  const doSave = async (publish) => {
    if (!S.status || !S.status.allow_write) {
      if (!confirm('⚠️ 当前处于【只读保护模式】。\n\n是否确认一键解锁写回权限，并将此篇内容写回知乎？')) return;
      try {
        const toggleRes = await api('/api/mode/toggle_write', { enable: true });
        S.status.allow_write = toggleRes.allow_write;
        await loadStatus();
      } catch (e) {
        return toast('解锁写回权限失败: ' + e.message, 'bad');
      }
    }
    if (!confirm(publish
      ? '将把标题和正文写回知乎并直接发布。确定继续？'
      : '将把标题和正文保存到知乎草稿箱。确定继续？')) return;
    try {
      const r = await api('/api/save', {
        doc_id: S.cur.doc.doc_id, title: t.value,
        body_html: b.value, publish: !!publish
      });
      if (!r.changed) return toast('内容没有变化', 'good');
      const v = r.verify || {};
      toast(v.ok ? '✓ 已写入知乎并复核通过！' : '已写入，但复核不一致：' + (v.why || ''),
        v.ok ? 'good' : 'bad');
      await openArticle(S.cur.doc.doc_id);
      await loadStatus();
    } catch (e) { toast('保存到知乎失败：' + e.message, 'bad'); }
  };
  if (save) save.onclick = () => doSave(false);
  if (pub) pub.onclick = () => doSave(true);
}

/* ─────────────── 动作 ─────────────── */

async function doInspect(withBody, silent = false) {
  let bodyLimit = 0;
  if (withBody) {
    const input = prompt('同步正文的篇数上限（0 = 全部，越多越慢）：', '30');
    if (input === null) return;
    bodyLimit = Number(input) || 0;
  }
  if (silent) {
    toast('正在自动从知乎同步全量内容（文章、回答、想法、提问）…', 'good');
  }
  try {
    const r = await api('/api/inspect', {
      kinds: ['article', 'answer', 'pin', 'question'], cap: 0, with_body: withBody, body_limit: bodyLimit
    });
    if (silent) {
      pollTaskSilently(r.task_id, async () => {
        await loadStatus();
        await loadList();
        const cnt = S.stats.documents || 0;
        toast(`✓ 全量内容同步完成（共 ${cnt} 篇）`, 'good');
      });
    } else {
      runTask(r.task_id, withBody ? '同步列表 + 正文' : '同步全量内容（文章/回答/想法/提问）', async () => {
        await loadStatus();
        await loadList();
      });
    }
  } catch (e) {
    if (!silent) toast('同步启动失败：' + e.message, 'bad');
  }
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
    <p class="sub">已预先配置谢迪安友情资助 AI 中转站（DeepSeek V4.1 Flash），开箱即用。凭证保存在本地 <code>data/ai_config.json</code>。</p>
    <div class="field">
      <label>API Endpoint (接口地址)</label>
      <input type="text" id="ai-cfg-url" value="${esc(cfg.api_url || 'http://156.225.31.92:7863/v1')}">
    </div>
    <div class="field">
      <label>API Key (已预配置 · 点击可修改)</label>
      <input type="password" id="ai-cfg-key" value="${esc(cfg.api_key || '605eea2541c9ce3c4cbd7115a7339b7b7c3788e7577e8c47')}">
    </div>
    <div class="field">
      <label>模型名称 (Model)</label>
      <select id="ai-cfg-model" style="width:100%;padding:8px 10px;border-radius:var(--r);border:1px solid var(--border-2);background:var(--bg)">
        <option value="deepseek-v4.1-flash"${(!cfg.model || cfg.model === 'deepseek-v4.1-flash') ? ' selected' : ''}>deepseek-v4.1-flash (推荐 · 快速高智能)</option>
        <option value="hy3"${cfg.model === 'hy3' ? ' selected' : ''}>hy3 (备用模型)</option>
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
    if (S.view === 'articles') {
      S.only = 'article';
      $$('.pill').forEach(p => p.classList.remove('active'));
      const p = $('.pill[data-only="article"]'); if (p) p.classList.add('active');
    } else if (S.view === 'answers') {
      S.only = 'answer';
      $$('.pill').forEach(p => p.classList.remove('active'));
      const p = $('.pill[data-only="answer"]'); if (p) p.classList.add('active');
    } else if (S.view === 'pins') {
      S.only = 'pin';
      $$('.pill').forEach(p => p.classList.remove('active'));
      const p = $('.pill[data-only="pin"]'); if (p) p.classList.add('active');
    } else if (S.view === 'questions') {
      S.only = 'question';
      $$('.pill').forEach(p => p.classList.remove('active'));
      const p = $('.pill[data-only="question"]'); if (p) p.classList.add('active');
    } else if (S.view === 'blocked') {
      S.only = 'blocked';
      $$('.pill').forEach(p => p.classList.remove('active'));
      $('.pill[data-only="blocked"]').classList.add('active');
    } else if (S.view === 'nobody') {
      S.only = '';
    } else {
      S.only = '';
      $$('.pill').forEach(p => p.classList.remove('active'));
      $('.pill[data-only=""]').classList.add('active');
    }
    await loadList();
  });

  $$('.pill').forEach(p => p.onclick = async () => {
    $$('.pill').forEach(x => x.classList.remove('active'));
    p.classList.add('active');
    S.only = p.dataset.only;
    $$('.nav-item').forEach(x => x.classList.remove('active'));
    if (S.only === 'article') {
      const nb = $('.nav-item[data-view="articles"]'); if (nb) nb.classList.add('active');
    } else if (S.only === 'answer') {
      const nb = $('.nav-item[data-view="answers"]'); if (nb) nb.classList.add('active');
    } else if (S.only === 'pin') {
      const nb = $('.nav-item[data-view="pins"]'); if (nb) nb.classList.add('active');
    } else if (S.only === 'question') {
      const nb = $('.nav-item[data-view="questions"]'); if (nb) nb.classList.add('active');
    } else if (S.only === 'blocked') {
      const nb = $('.nav-item[data-view="blocked"]'); if (nb) nb.classList.add('active');
    } else if (!S.only) {
      const nb = $('.nav-item[data-view="library"]') || $('.nav-item[data-view="all"]'); if (nb) nb.classList.add('active');
    }
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

  const btnLogin = $('#btn-login');
  if (btnLogin) btnLogin.onclick = () => showLoginModal();

  const btnAi = $('#btn-ai-cfg');
  if (btnAi) btnAi.onclick = () => showAiConfigModal();

  const btnCheckUp = $('#btn-check-update');
  if (btnCheckUp) btnCheckUp.onclick = () => checkForUpdates(false);
}

/* ─────────────── 智能免 F12 登录 ─────────────── */

async function showLoginModal() {
  openModal(`
    <h2>知乎账号登录认证</h2>
    <p class="sub">支持自动读取本机浏览器登录或手机知乎 App 扫码，零代码、彻底免 F12。</p>
    
    <!-- 通道 1: 自动读取本机浏览器 -->
    <div class="auth-channel highlight">
      <div class="auth-header">
        <span class="auth-title">🚀 自动读取本机 Edge / Chrome 登录</span>
        <span class="badge" style="background:#EAD8C7;color:#873800;font-weight:600">推荐首选</span>
      </div>
      <div class="auth-desc">直接解密并提取本机浏览器已登录的知乎 Cookie，一键秒读。</div>
      <div class="row" style="justify-content:flex-start;margin:0 0 6px">
        <button class="btn primary" id="auth-btn-autodetect">一键读取本机登录</button>
        <button class="btn" id="auth-btn-close-browser" style="display:none;background:#fee2e2;border:1px solid #f87171;color:#b91c1c;font-weight:600">⚡ 关闭浏览器并直读</button>
      </div>
      <div id="auth-autodetect-msg" style="font-size:11.5px;color:var(--text-2);margin-top:6px"></div>
    </div>

    <!-- 通道 2: 手机知乎扫码登录 -->
    <div class="auth-channel">
      <div class="auth-header">
        <span class="auth-title">📱 手机知乎扫码登录</span>
        <button class="btn mini" id="auth-btn-qr-start">生成扫码二维码</button>
      </div>
      <div class="auth-desc">使用手机知乎 App 扫一扫即可安全授权登录。</div>
      <div id="auth-qr-container" style="display:none" class="auth-qr-box">
        <img id="auth-qr-img" class="auth-qr-img" src="" alt="知乎登录二维码">
        <div id="auth-qr-status" class="auth-qr-status">⏳ 正在等待手机知乎扫码…</div>
      </div>
    </div>

    <!-- 通道 3: 从云端凭证柜同步 -->
    <div class="auth-channel">
      <div class="auth-header">
        <span class="auth-title">☁️ 从云端凭证柜同步</span>
        <button class="btn mini" id="auth-btn-cloud">一键同步</button>
      </div>
      <div class="auth-desc">若曾使用过浏览器扩展程序或云端扫码，可直接从云端同步凭证。</div>
      <div id="auth-cloud-msg" style="font-size:11.5px;color:var(--text-2)"></div>
    </div>

    <!-- 通道 4: 手动粘贴 (折叠备用) -->
    <details class="auth-details">
      <summary>高级选项：手动粘贴 Cookie (备用)</summary>
      <div style="margin-top:10px">
        <textarea id="ck" style="min-height:80px" placeholder="z_c0=…; _zap=…; d_c0=…"></textarea>
        <div class="row" style="margin-top:8px">
          <button class="btn mini" id="ck-go">保存手动 Cookie</button>
        </div>
      </div>
    </details>

    <div class="row" style="margin-top:16px">
      <button class="btn" onclick="closeModal()">关闭</button>
    </div>
  `);

  let qrTimer = null;

  const finishLoginAndSync = async () => {
    closeModal();
    const st = await loadStatus();
    await loadList();
    if (st && st.logged_in && (!st.stats || !st.stats.documents)) {
      doInspect(false, true);
    }
  };

  // 绑定通道 1: 自动读取
  const btnAuto = $('#auth-btn-autodetect');
  const btnCloseBr = $('#auth-btn-close-browser');
  const msgAuto = $('#auth-autodetect-msg');

  async function doAutoDetect(closeBrowser = false) {
    if (closeBrowser) {
      msgAuto.innerHTML = '<i>正在关闭浏览器并解密直读知乎凭据，请稍候…</i>';
    } else {
      msgAuto.innerHTML = '<i>正在解密扫描本机 Edge/Chrome 登录数据…</i>';
    }
    btnAuto.disabled = true;
    btnCloseBr.disabled = true;
    try {
      const opt = {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-QY-Token': TOKEN
        },
        body: JSON.stringify({ close_browser: closeBrowser })
      };
      const r = await fetch('/api/auth/auto_detect', opt);
      let res = {};
      try { res = await r.json(); } catch(err) { res = { ok: false, error: '接口解析失败' }; }

      if (res.ok) {
        msgAuto.innerHTML = `<span style="color:var(--ok)">✓ 读取成功（来源: ${esc(res.source || '本机浏览器')}），正在进入工作台…</span>`;
        toast(`已登录：${(res.account && res.account.name) || '成功'}`, 'good');
        setTimeout(finishLoginAndSync, 800);
      } else {
        if (res.locked) {
          msgAuto.innerHTML = `
            <div style="margin-top:8px;padding:10px 12px;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;color:#991b1b">
              <div style="font-weight:600;margin-bottom:6px">⚠️ ${esc(res.browser || 'Edge')} 正在运行并锁定了 Cookie 数据文件</div>
              <div style="font-size:12px;color:#7f1d1d;margin-bottom:8px">由于 Windows 独占锁限制，需秒级退出浏览器后解密读取：</div>
              <button class="btn" id="btn-inline-close-br" style="background:#dc2626;color:#fff;font-weight:600;padding:6px 14px;border:none;cursor:pointer">
                ⚡ 立即关闭浏览器并直读
              </button>
            </div>
          `;
          btnCloseBr.style.display = 'inline-block';
          const btnInline = $('#btn-inline-close-br');
          if (btnInline) {
            btnInline.onclick = () => doAutoDetect(true);
          }
        } else {
          msgAuto.innerHTML = `<span style="color:var(--danger)">${esc(res.error || '未检测到知乎登录态')}</span>`;
        }
      }
    } catch (e) {
      msgAuto.innerHTML = `<span style="color:var(--danger)">提取失败：${esc(e.message)}</span>`;
    } finally {
      btnAuto.disabled = false;
      btnCloseBr.disabled = false;
    }
  }

  btnAuto.onclick = () => doAutoDetect(false);
  btnCloseBr.onclick = () => doAutoDetect(true);

  // 绑定通道 2: 扫码登录
  const btnQr = $('#auth-btn-qr-start');
  const qrBox = $('#auth-qr-container');
  const qrImg = $('#auth-qr-img');
  const qrStatus = $('#auth-qr-status');

  btnQr.onclick = async () => {
    btnQr.disabled = true;
    btnQr.textContent = '正在获取…';
    try {
      const res = await api('/api/auth/qr_start', {});
      if (res.ok && res.qr_base64) {
        qrImg.src = res.qr_base64;
        qrBox.style.display = 'flex';
        qrStatus.textContent = '⏳ 请使用手机【知乎 App】扫一扫';
        btnQr.textContent = '刷新二维码';
        btnQr.disabled = false;

        if (qrTimer) clearInterval(qrTimer);
        qrTimer = setInterval(async () => {
          try {
            const p = await api(`/api/auth/qr_poll?token=${encodeURIComponent(res.token)}`);
            if (p.status === 'scanned') {
              qrStatus.textContent = '📱 手机已扫码！请在手机上点击确认登录…';
            } else if (p.status === 'success') {
              clearInterval(qrTimer);
              qrStatus.innerHTML = '<b style="color:var(--ok)">✓ 扫码登录成功！正在进入…</b>';
              toast(`欢迎：${(p.account && p.account.name) || '知乎用户'}`, 'good');
              setTimeout(finishLoginAndSync, 800);
            } else if (p.status === 'expired') {
              clearInterval(qrTimer);
              qrStatus.textContent = '❌ 二维码已过期，请点击刷新。';
            }
          } catch(e) {}
        }, 1500);
      } else {
        toast(res.error || '获取二维码失败', 'bad');
        btnQr.disabled = false;
        btnQr.textContent = '重新生成';
      }
    } catch (e) {
      toast('请求失败：' + e.message, 'bad');
      btnQr.disabled = false;
      btnQr.textContent = '重新生成';
    }
  };

  // 绑定通道 3: 云端同步
  const btnCloud = $('#auth-btn-cloud');
  const msgCloud = $('#auth-cloud-msg');
  btnCloud.onclick = async () => {
    btnCloud.disabled = true;
    msgCloud.innerHTML = '<i>正在查询云端凭证柜…</i>';
    try {
      const res = await api('/api/auth/cloud_sync');
      if (res.ok) {
        msgCloud.innerHTML = '<span style="color:var(--ok)">✓ 云端同步成功！</span>';
        toast(`已从云端同步凭证：${(res.account && res.account.name) || '成功'}`, 'good');
        setTimeout(finishLoginAndSync, 800);
      } else {
        msgCloud.innerHTML = `<span style="color:var(--danger)">${esc(res.error || '云端凭证柜为空')}</span>`;
        btnCloud.disabled = false;
      }
    } catch(e) {
      msgCloud.innerHTML = `<span style="color:var(--danger)">同步失败：${esc(e.message)}</span>`;
      btnCloud.disabled = false;
    }
  };

  // 绑定通道 4: 手动粘贴
  $('#ck-go').onclick = async () => {
    const v = $('#ck').value.trim();
    if (!v) return toast('还没粘贴', 'bad');
    try {
      const r = await api('/api/login', { cookie: v });
      if (!r.ok) return toast(r.error || '失败', 'bad');
      const a = r.account || {};
      toast(a.name ? `已登录：${a.name}` : 'Cookie 已保存', 'good');
      await finishLoginAndSync();
    } catch (e) { toast('失败：' + e.message, 'bad'); }
  };
}

/* ─────────────── 远程更新与全自动后台静默预载 ─────────────── */

let _updatePollTimer = null;
let _currentUpInfo = null;

async function checkForUpdates(silent = true) {
  try {
    const res = await api('/api/system/check_update');
    if (res.ok && res.has_update) {
      _currentUpInfo = res;
      showUpdateBanner(res);
      if (!silent) {
        showUpdateModal(res);
      }
      startUpdateStatusPolling();
    } else {
      if (!silent) {
        toast(`当前已是最新版本 (v${res.current_version || '1.1.0'})`, 'good');
      }
    }
  } catch (e) {
    if (!silent) toast('检查更新失败：' + e.message, 'bad');
  }
}

function startUpdateStatusPolling() {
  if (_updatePollTimer) return;
  _updatePollTimer = setInterval(async () => {
    try {
      const st = await api('/api/system/update_status');
      if (st.ready) {
        clearInterval(_updatePollTimer);
        _updatePollTimer = null;
        if (_currentUpInfo) {
          _currentUpInfo.ready = true;
          showUpdateBanner(_currentUpInfo);
          const modalBtn = $('#modal-btn-update');
          if (modalBtn) {
            modalBtn.textContent = '⚡ 立即一键重启生效 (已在后台下载就绪)';
            modalBtn.style.background = '#16a34a';
          }
        }
      } else if (st.status === 'downloading') {
        const btnBanner = $('#btn-banner-update');
        if (btnBanner && !btnBanner.dataset.clicked) {
          btnBanner.textContent = `后台下载中 ${st.downloaded_mb || 0}M/${st.total_mb || 92}M · 点击即可立即应用`;
        }
      }
    } catch (_) {}
  }, 1500);
}

function showUpdateBanner(upInfo) {
  let b = $('#update-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'update-banner';
    b.className = 'update-banner';
    const app = $('#app');
    app.parentNode.insertBefore(b, app);
  }
  const isReady = !!upInfo.ready;
  const tagText = isReady ? '✓ 新版本已就绪' : '后台静默下载中';
  const tagBg = isReady ? '#16a34a' : '#cc5500';
  const btnText = isReady ? '⚡ 立即一键重启生效' : '一键自动更新并重启';
  const btnStyle = isReady ? 'background:#16a34a;border-color:#16a34a;font-weight:700;' : '';
  const desc = isReady 
    ? `已在后台自动预载完成！点击即刻 0 秒原地更新生效并重启`
    : `后台正在静默下载安装包（支持断点续传），随时点击均可立即一键自动更新`;

  b.innerHTML = `
    <div class="update-banner-left">
      <span class="update-banner-tag" style="background:${tagBg}">${tagText}</span>
      <span>发现新版本 <b>v${esc(upInfo.latest_version)}</b>（当前 v${esc(upInfo.current_version)}）：${esc(upInfo.release_notes || '常规升级')} · <span style="color:#6b7280">${desc}</span></span>
    </div>
    <div class="update-banner-actions">
      <button class="btn btn-sm primary" id="btn-banner-update" style="${btnStyle}">${btnText}</button>
      <button class="btn btn-sm" id="btn-banner-dismiss">稍后</button>
    </div>
  `;
  $('#btn-banner-update').onclick = () => doApplyUpdate(upInfo.download_url);
  $('#btn-banner-dismiss').onclick = () => {
    b.remove();
    if (_updatePollTimer) { clearInterval(_updatePollTimer); _updatePollTimer = null; }
  };
}

function showUpdateModal(upInfo) {
  const isReady = !!upInfo.ready;
  const btnText = isReady ? '⚡ 立即一键重启生效 (后台已就绪)' : '立即一键更新并重启';
  const btnStyle = isReady ? 'background:#16a34a;border-color:#16a34a;' : '';
  openModal(`
    <h2>发现新版本 v${esc(upInfo.latest_version)}</h2>
    <p class="sub">发布时间：${esc(upInfo.release_date || '最新')} ｜ 当前版本：v${esc(upInfo.current_version)}</p>
    <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:var(--r);padding:12px;font-size:12px;line-height:1.6;white-space:pre-wrap;margin-bottom:14px">${esc(upInfo.release_notes || '常规性能提升与体验优化')}</div>
    <div class="row">
      <button class="btn" onclick="closeModal()">稍后提醒</button>
      <button class="btn primary" id="modal-btn-update" style="${btnStyle}">${btnText}</button>
    </div>
  `);
  $('#modal-btn-update').onclick = () => {
    closeModal();
    doApplyUpdate(upInfo.download_url);
  };
}

async function doApplyUpdate(downloadUrl) {
  try {
    const btn = $('#btn-banner-update');
    if (btn) { btn.dataset.clicked = '1'; btn.disabled = true; btn.textContent = '正在准备重启...'; }
    toast('✓ 正在准备原地静默更新并自动重启，请稍候…', 'good');
    const res = await api('/api/system/apply_update', { download_url: downloadUrl });
    if (res.ok && res.task_id) {
      runTask(res.task_id, '一键自动更新并重启');
    } else {
      toast(res.error || '启动更新失败', 'bad');
      if (btn) { btn.disabled = false; btn.textContent = '一键自动更新并重启'; }
    }
  } catch (e) {
    toast('更新异常：' + e.message, 'bad');
    const btn = $('#btn-banner-update');
    if (btn) { btn.disabled = false; btn.textContent = '一键自动更新并重启'; }
  }
}

/* ─────────────── 启动 ─────────────── */

(async function boot() {
  bind();
  try {
    const st = await loadStatus();
    await loadList();
    // 若已登录但本地数据库为空（0 篇），自动在后台拉取知乎文章列表，免去手动点击
    if (st && st.logged_in && (!st.stats || !st.stats.documents)) {
      doInspect(false, true);
    }
    // 软件启动时自动执行云端远程更新检查
    checkForUpdates(true);
  } catch (e) {
    toast('初始化失败：' + e.message, 'bad');
  }
})();
