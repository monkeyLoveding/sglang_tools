/* SGLang NPU Nightly 看板 —— 前端逻辑
 * 数据文件（同源相对路径）：
 *   data/meta.json          概览 / 覆盖范围
 *   data/runs.json          运行级记录
 *   data/jobs/<YYYY-MM>.json    job 级记录
 *   data/cases/<YYYY-MM>.json   用例文件级记录
 */
'use strict';

const PAGE_SIZE = 50;

const state = {
  meta: null,
  runs: [],
  jobs: [],
  cases: [],
  from: null,
  to: null,
  jobFilter: new Set(),
  modelFilter: new Set(),
  search: '',
  hideSkipped: true,
  tab: 'job',
  sort: { key: null, dir: 'desc' },
  page: 1,
  detail: null,
  knownJobs: new Set(),
  knownModels: new Set(),
};

const jobCache = new Map();
const caseCache = new Map();
const charts = { case: null, queue: null, detail: null };

/* ----------------------------- utils ----------------------------- */
async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function shiftDate(dateStr, days) {
  const d = new Date(dateStr + 'T00:00:00');
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function monthsBetween(from, to) {
  const res = [];
  let [y, m] = from.split('-').map(Number);
  const [ty, tm] = to.split('-').map(Number);
  let guard = 0;
  while ((y < ty || (y === ty && m <= tm)) && guard++ < 600) {
    res.push(`${y}-${String(m).padStart(2, '0')}`);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return res;
}

function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return '-';
  let s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${String(s % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, '0')}m`;
}

function pct(pass, total) {
  if (!total) return '-';
  return `${((pass / total) * 100).toFixed(1)}%`;
}

function avg(values) {
  const v = values.filter((x) => x != null && !isNaN(x));
  if (!v.length) return null;
  return v.reduce((a, b) => a + b, 0) / v.length;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (v != null) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/* ----------------------------- loading ----------------------------- */
async function ensureMonth(month) {
  if (jobCache.has(month)) return;
  const [jobs, cases] = await Promise.all([
    fetchJSON(`data/jobs/${month}.json`).catch(() => []),
    fetchJSON(`data/cases/${month}.json`).catch(() => []),
  ]);
  jobCache.set(month, jobs);
  caseCache.set(month, cases);
  jobs.forEach((j) => state.knownJobs.add(j.job_group));
  cases.forEach((c) => state.knownModels.add(c.model));
}

async function loadRunsAndMeta() {
  state.meta = await fetchJSON('data/meta.json').catch(() => null);
  state.runs = await fetchJSON('data/runs.json').catch(() => []);
}

/* ----------------------------- filtering ----------------------------- */
function inRange(rec) {
  if (!rec.date) return true;
  return rec.date >= state.from && rec.date <= state.to;
}

function filteredJobs() {
  return state.jobs.filter((j) => {
    if (!inRange(j)) return false;
    if (state.hideSkipped && (j.conclusion === 'skipped' || j.conclusion === 'cancelled')) return false;
    if (state.jobFilter.size && !state.jobFilter.has(j.job_group)) return false;
    return true;
  });
}

function filteredCases() {
  return state.cases.filter((c) => {
    if (!inRange(c)) return false;
    if (state.jobFilter.size && !state.jobFilter.has(c.job_group)) return false;
    if (state.modelFilter.size && !state.modelFilter.has(c.model)) return false;
    if (state.search && !(c.case_id || '').toLowerCase().includes(state.search)) return false;
    return true;
  });
}

function filteredRuns() {
  return state.runs.filter((r) => (!r.date ? true : r.date >= state.from && r.date <= state.to));
}

/* ----------------------------- aggregation ----------------------------- */
function groupBy(list, keyFn) {
  const map = new Map();
  for (const item of list) {
    const k = keyFn(item);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(item);
  }
  return map;
}

function aggJobs(jobs, cases) {
  const casesByGroup = groupBy(cases, (c) => c.job_group);
  const rows = [];
  for (const [group, list] of groupBy(jobs, (j) => j.job_group)) {
    const cs = casesByGroup.get(group) || [];
    const passed = cs.filter((c) => c.passed).length;
    const failed = cs.length - passed;
    rows.push({
      group,
      runs: new Set(list.map((j) => j.run_id)).size,
      jobOk: list.filter((j) => j.conclusion === 'success').length,
      jobTotal: list.length,
      avgQueue: avg(list.map((j) => j.queue_seconds)),
      avgDuration: avg(list.map((j) => j.duration_seconds)),
      cases: cs.length,
      passed,
      failed,
      avgCase: avg(cs.map((c) => c.duration_seconds)),
      _link: { type: 'job', key: group },
    });
  }
  return rows;
}

function aggModels(cases) {
  const rows = [];
  for (const [model, list] of groupBy(cases, (c) => c.model || 'unknown')) {
    const passed = list.filter((c) => c.passed).length;
    rows.push({
      group: model,
      casesDistinct: new Set(list.map((c) => c.case_id)).size,
      cases: list.length,
      passed,
      failed: list.length - passed,
      avgCase: avg(list.map((c) => c.duration_seconds)),
      _link: { type: 'model', key: model },
    });
  }
  return rows;
}

function aggCases(cases) {
  const rows = [];
  for (const [caseId, list] of groupBy(cases, (c) => c.case_id || c.case)) {
    const passed = list.filter((c) => c.passed).length;
    const last = list[list.length - 1];
    rows.push({
      group: caseId,
      model: last.model,
      category: last.category,
      cases: list.length,
      passed,
      failed: list.length - passed,
      avgCase: avg(list.map((c) => c.duration_seconds)),
      _link: { type: 'case', key: caseId },
    });
  }
  return rows;
}

/* ----------------------------- rendering ----------------------------- */
function renderMeta() {
  const m = state.meta;
  const el = document.getElementById('metaInfo');
  if (!m) { el.textContent = '未找到 data/meta.json，请先运行采集脚本。'; return; }
  const dr = m.date_range || {};
  el.innerHTML = `数据更新：${new Date(m.updated_at).toLocaleString('zh-CN')}<br>` +
    `覆盖：${dr.from || '-'} ~ ${dr.to || '-'} · 运行 ${m.counts.runs} · Job ${m.counts.jobs} · 用例 ${m.counts.cases}`;
}

function renderCards() {
  const runs = filteredRuns();
  const jobs = filteredJobs();
  const cases = filteredCases();
  const passed = cases.filter((c) => c.passed).length;
  const failed = cases.length - passed;
  const failRuns = runs.filter((r) => r.conclusion === 'failure').length;
  const dataJobs = jobs.filter((j) => j.kind !== 'infra');

  const cards = [
    { k: '运行次数', v: runs.length, s: `失败 ${failRuns} 次`, cls: failRuns ? 'bad' : 'ok' },
    { k: '用例执行总数', v: cases.length, s: `通过 ${passed} / 失败 ${failed}`, cls: '' },
    { k: '用例通过率', v: pct(passed, cases.length), s: '', cls: !cases.length ? '' : failed === 0 ? 'ok' : 'bad' },
    { k: '平均用例时长', v: fmtDur(avg(cases.map((c) => c.duration_seconds))), s: '按文件统计', cls: '' },
    { k: '平均 Job 排队', v: fmtDur(avg(dataJobs.map((j) => j.queue_seconds))), s: `依赖感知（${dataJobs.length} 个 job）`, cls: '' },
    { k: '平均 Job 时长', v: fmtDur(avg(dataJobs.map((j) => j.duration_seconds))), s: '测试类 job', cls: '' },
  ];
  const host = document.getElementById('summaryCards');
  host.innerHTML = '';
  for (const c of cards) {
    host.appendChild(el('div', { class: `stat ${c.cls}` }, [
      el('div', { class: 'k', text: c.k }),
      el('div', { class: 'v', text: String(c.v) }),
      el('div', { class: 's', text: c.s }),
    ]));
  }
}

function dailySeries(cases) {
  const dates = [...new Set(cases.map((c) => c.date))].sort();
  const passed = [], failed = [];
  for (const d of dates) {
    const day = cases.filter((c) => c.date === d);
    passed.push(day.filter((c) => c.passed).length);
    failed.push(day.filter((c) => !c.passed).length);
  }
  return { dates, passed, failed };
}

function renderCharts() {
  if (typeof Chart === 'undefined') return; // CDN 不可用时仍渲染表格
  const cases = filteredCases();
  const jobs = filteredJobs().filter((j) => j.kind !== 'infra');

  const { dates, passed, failed } = dailySeries(cases);
  const caseCtx = document.getElementById('caseChart');
  if (charts.case) charts.case.destroy();
  charts.case = new Chart(caseCtx, {
    type: 'bar',
    data: {
      labels: dates,
      datasets: [
        { label: '通过', data: passed, backgroundColor: '#16a34a' },
        { label: '失败', data: failed, backgroundColor: '#dc2626' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
      plugins: { legend: { position: 'bottom' } },
    },
  });

  const byDate = groupBy(jobs, (j) => j.date);
  const qDates = [...byDate.keys()].sort();
  const qAvg = qDates.map((d) => {
    const v = avg(byDate.get(d).map((j) => j.queue_seconds));
    return v == null ? null : Math.round(v);
  });
  const qCtx = document.getElementById('queueChart');
  if (charts.queue) charts.queue.destroy();
  charts.queue = new Chart(qCtx, {
    type: 'line',
    data: { labels: qDates, datasets: [{ label: '平均排队(秒)', data: qAvg, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.15)', fill: true, tension: .25 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { y: { beginAtZero: true, title: { display: true, text: '秒' } } },
      plugins: { legend: { position: 'bottom' } },
    },
  });
}

const TAB_COLUMNS = {
  job: [
    { key: 'group', label: 'Job 分组' },
    { key: 'runs', label: '出现运行数', num: true },
    { key: 'jobOk', label: 'Job 成功/总', num: false, render: (r) => `${r.jobOk}/${r.jobTotal} (${pct(r.jobOk, r.jobTotal)})` },
    { key: 'avgQueue', label: '平均排队', num: true, render: (r) => fmtDur(r.avgQueue) },
    { key: 'avgDuration', label: '平均时长', num: true, render: (r) => fmtDur(r.avgDuration) },
    { key: 'cases', label: '用例数', num: true },
    { key: 'passed', label: '通过', num: true },
    { key: 'failed', label: '失败', num: true },
    { key: 'passRate', label: '用例通过率', num: false, sortValue: (r) => (r.cases ? r.passed / r.cases : -1), render: (r) => pill(r.passed, r.cases) },
    { key: 'avgCase', label: '平均用例时长', num: true, render: (r) => fmtDur(r.avgCase) },
  ],
  model: [
    { key: 'group', label: '模型' },
    { key: 'casesDistinct', label: '用例文件数', num: true },
    { key: 'cases', label: '执行次数', num: true },
    { key: 'passed', label: '通过', num: true },
    { key: 'failed', label: '失败', num: true },
    { key: 'passRate', label: '通过率', num: false, sortValue: (r) => (r.cases ? r.passed / r.cases : -1), render: (r) => pill(r.passed, r.cases) },
    { key: 'avgCase', label: '平均用例时长', num: true, render: (r) => fmtDur(r.avgCase) },
  ],
  case: [
    { key: 'group', label: '用例文件' },
    { key: 'model', label: '模型' },
    { key: 'category', label: '类型' },
    { key: 'cases', label: '执行次数', num: true },
    {
      key: 'passFail',
      label: '成功/失败',
      num: false,
      sortValue: (r) => (r.cases ? r.passed / r.cases : -1),
      render: (r) => `<span class="pill ok">${r.passed}</span> <span class="pill bad">${r.failed}</span>`,
    },
    { key: 'passRate', label: '通过率', num: false, sortValue: (r) => (r.cases ? r.passed / r.cases : -1), render: (r) => pill(r.passed, r.cases) },
    { key: 'avgCase', label: '平均时长', num: true, render: (r) => fmtDur(r.avgCase) },
  ],
};

function pill(pass, total) {
  if (!total) return '<span class="pill mid">-</span>';
  const ratio = pass / total;
  const cls = ratio === 1 ? 'ok' : ratio >= 0.8 ? 'mid' : 'bad';
  return `<span class="pill ${cls}">${pct(pass, total)}</span>`;
}

function chip(label, value, cls = '') {
  return `<span class="chip ${cls}"><b>${escapeHtml(String(label))}</b>${escapeHtml(String(value))}</span>`;
}

/* 让显式排序控件与当前 tab 及表头点击状态保持一致。 */
function syncSortControls() {
  const cols = TAB_COLUMNS[state.tab] || [];
  const sel = document.getElementById('sortSelect');
  const btn = document.getElementById('sortDirBtn');
  if (!sel || !btn) return;
  const active = state.sort.key || (cols[0] && cols[0].key) || '';
  sel.innerHTML = '';
  for (const c of cols) {
    const opt = el('option', { value: c.key, text: c.label });
    if (c.key === active) opt.selected = true;
    sel.appendChild(opt);
  }
  btn.textContent = state.sort.dir === 'asc' ? '升序 ▲' : '降序 ▼';
}

function renderTable() {
  const jobs = filteredJobs();
  const cases = filteredCases();
  const tab = state.tab;
  let rows = tab === 'job' ? aggJobs(jobs, cases) : tab === 'model' ? aggModels(cases) : aggCases(cases);

  const cols = TAB_COLUMNS[tab];
  const nodeById = Object.fromEntries(cols.map((c) => [c.key, c]));
  syncSortControls();

  const sortCol = state.sort.key || cols[0].key;
  const dir = state.sort.dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const col = nodeById[sortCol] || cols[0];
    const va = col.sortValue ? col.sortValue(a) : a[sortCol];
    const vb = col.sortValue ? col.sortValue(b) : b[sortCol];
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  if (state.page > totalPages) state.page = totalPages;
  const start = (state.page - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);

  const thead = document.querySelector('#aggTable thead');
  const tbody = document.querySelector('#aggTable tbody');
  thead.innerHTML = '';
  tbody.innerHTML = '';
  const htr = el('tr');
  for (const c of cols) {
    const arrow = state.sort.key === c.key ? (state.sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
    const th = el('th', { class: c.num ? 'num' : '', text: c.label + arrow });
    th.onclick = () => {
      if (state.sort.key === c.key) state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
      else { state.sort.key = c.key; state.sort.dir = 'desc'; }
      renderTable();
    };
    htr.appendChild(th);
  }
  thead.appendChild(htr);

  for (const r of pageRows) {
    const tr = el('tr');
    for (const c of cols) {
      const td = el('td', { class: c.num ? 'num' : '' });
      if (c.render) td.innerHTML = c.render(r);
      else if (c.key === 'group') { td.innerHTML = `<span class="mono">${escapeHtml(r.group)}</span>`; }
      else td.textContent = r[c.key] ?? '';
      tr.appendChild(td);
    }
    tr.onclick = () => openDetail(r._link);
    tbody.appendChild(tr);
  }
  if (!pageRows.length) {
    tbody.appendChild(el('tr', {}, [el('td', { colspan: cols.length, text: '当前筛选条件下暂无数据。' })]));
  }

  const pager = document.getElementById('pager');
  pager.innerHTML = '';
  pager.appendChild(el('span', { text: `共 ${rows.length} 行 · 第 ${state.page}/${totalPages} 页` }));
  const mkBtn = (label, page, disabled) => {
    const b = el('button', { text: label });
    b.disabled = disabled;
    b.onclick = () => { state.page = page; renderTable(); };
    return b;
  };
  pager.appendChild(mkBtn('上一页', state.page - 1, state.page <= 1));
  pager.appendChild(mkBtn('下一页', state.page + 1, state.page >= totalPages));
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

/* ----------------------------- detail ----------------------------- */
function openDetail(link) {
  state.detail = link;
  document.getElementById('detailCard').hidden = false;
  renderDetail();
  document.getElementById('detailCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  const cases = filteredCases();
  const jobs = filteredJobs();
  let title = '';
  let recs = [];
  let chartKind = 'case';

  if (d.type === 'job') {
    title = `Job 分组：${d.key}`;
    recs = jobs.filter((j) => j.job_group === d.key);
    chartKind = 'queue';
  } else if (d.type === 'model') {
    title = `模型：${d.key}`;
    recs = cases.filter((c) => (c.model || 'unknown') === d.key);
  } else {
    title = `用例：${d.key}`;
    recs = cases.filter((c) => (c.case_id || c.case) === d.key);
  }
  document.getElementById('detailTitle').textContent = title;

  const stats = document.getElementById('detailStats');
  if (chartKind === 'queue') {
    const ok = recs.filter((j) => j.conclusion === 'success').length;
    stats.innerHTML = [
      chip('Job 次数', recs.length),
      chip('成功', ok, 'ok'),
      chip('失败', recs.length - ok, 'bad'),
      chip('平均排队', fmtDur(avg(recs.map((j) => j.queue_seconds)))),
      chip('平均时长', fmtDur(avg(recs.map((j) => j.duration_seconds)))),
    ].join('');
  } else {
    const ok = recs.filter((c) => c.passed).length;
    stats.innerHTML = [
      chip('执行次数', recs.length),
      chip('成功', ok, 'ok'),
      chip('失败', recs.length - ok, 'bad'),
      chip('成功率', pct(ok, recs.length)),
      chip('平均时长', fmtDur(avg(recs.map((c) => c.duration_seconds)))),
    ].join('');
  }

  const tbody = document.querySelector('#detailTable tbody');
  const thead = document.querySelector('#detailTable thead');
  tbody.innerHTML = '';
  if (chartKind === 'queue') {
    thead.innerHTML = '<tr><th>日期</th><th>Run</th><th>结果</th><th>排队</th><th>时长</th><th>Job</th></tr>';
    for (const j of recs.slice().sort((a, b) => (a.started_at || '').localeCompare(b.started_at || ''))) {
      tbody.appendChild(el('tr', {}, [
        el('td', { text: j.date }),
        runLink(j.run_id),
        el('td', { html: conclPill(j.conclusion) }),
        el('td', { text: fmtDur(j.queue_seconds) }),
        el('td', { text: fmtDur(j.duration_seconds) }),
        el('td', { class: 'path', text: j.job_group }),
      ]));
    }
  } else {
    thead.innerHTML = '<tr><th>日期</th><th>Run</th><th>结果</th><th>时长</th><th>所属 Job</th><th>文件</th></tr>';
    for (const c of recs.slice().sort((a, b) => (a.date || '').localeCompare(b.date || ''))) {
      tbody.appendChild(el('tr', {}, [
        el('td', { text: c.date }),
        runLink(c.run_id),
        el('td', { html: conclPill(c.passed ? 'success' : 'failure') }),
        el('td', { text: fmtDur(c.duration_seconds) }),
        el('td', { class: 'path', text: c.job_group }),
        el('td', { class: 'path', text: c.file || c.case || '' }),
      ]));
    }
  }

  // detail chart
  const ctx = document.getElementById('detailChart');
  if (typeof Chart === 'undefined') return;
  if (charts.detail) charts.detail.destroy();
  if (chartKind === 'queue') {
    const byDate = groupBy(recs, (j) => j.date);
    const dates = [...byDate.keys()].sort();
    charts.detail = new Chart(ctx, {
      type: 'bar',
      data: { labels: dates, datasets: [{ label: '排队(秒)', data: dates.map((x) => Math.round(avg(byDate.get(x).map((j) => j.queue_seconds)) || 0)), backgroundColor: '#2563eb' }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } }, scales: { y: { beginAtZero: true } } },
    });
  } else {
    const { dates, passed, failed } = dailySeries(recs);
    charts.detail = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: dates,
        datasets: [
          { label: '通过', data: passed, backgroundColor: '#16a34a' },
          { label: '失败', data: failed, backgroundColor: '#dc2626' },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } }, plugins: { legend: { position: 'bottom' } } },
    });
  }
}

function runLink(runId) {
  const a = el('a', { href: `https://github.com/sgl-project/sglang/actions/runs/${runId}`, target: '_blank', rel: 'noopener', text: String(runId), class: 'mono' });
  const td = el('td');
  td.appendChild(a);
  return td;
}

function conclPill(conclusion) {
  const map = { success: 'ok', failure: 'bad', skipped: 'mid', cancelled: 'mid' };
  return `<span class="pill ${map[conclusion] || 'mid'}">${escapeHtml(conclusion || '-')}</span>`;
}

/* ----------------------------- wiring ----------------------------- */
function populateSelectors() {
  const jobSel = document.getElementById('jobSelect');
  const modelSel = document.getElementById('modelSelect');
  const keepJobs = new Set(state.jobFilter);
  const keepModels = new Set(state.modelFilter);

  const rebuild = (sel, values, keep) => {
    sel.innerHTML = '';
    for (const v of [...values].sort()) {
      const opt = el('option', { value: v, text: v });
      if (keep.has(v)) opt.selected = true;
      sel.appendChild(opt);
    }
  };
  rebuild(jobSel, state.knownJobs, keepJobs);
  rebuild(modelSel, state.knownModels, keepModels);
}

function readSelectors() {
  state.jobFilter = new Set([...document.getElementById('jobSelect').selectedOptions].map((o) => o.value));
  state.modelFilter = new Set([...document.getElementById('modelSelect').selectedOptions].map((o) => o.value));
}

/* 清空指定下拉框的选中项。 */
function clearSelect(id) {
  const sel = document.getElementById(id);
  for (const opt of sel.options) opt.selected = false;
}

/* 多选列表默认无法通过再次单击已选项来取消，这里补上，便于回到“全部”。 */
function enableToggleDeselect(sel) {
  sel.addEventListener('mousedown', (e) => {
    if (e.target.tagName !== 'OPTION' || !e.target.selected) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    e.target.selected = false;
    sel.dispatchEvent(new Event('change'));
  });
}

async function refresh() {
  const months = monthsBetween(state.from, state.to);
  await Promise.all(months.map(ensureMonth));
  state.jobs = months.flatMap((m) => jobCache.get(m) || []);
  state.cases = months.flatMap((m) => caseCache.get(m) || []);
  populateSelectors();
  renderMeta();
  renderCards();
  renderCharts();
  renderTable();
  if (state.detail) renderDetail();
}

function applyPreset(days) {
  state.to = todayStr();
  state.from = shiftDate(state.to, -(days - 1));
  document.getElementById('fromDate').value = state.from;
  document.getElementById('toDate').value = state.to;
  state.page = 1;
  refresh();
}

async function init() {
  await loadRunsAndMeta();
  const meta = state.meta || {};
  const dr = meta.date_range || {};
  const to = dr.to || todayStr();
  let from = shiftDate(to, -29);
  if (dr.from && dr.from > from) from = dr.from; // 不要越过数据起点
  state.from = from;
  state.to = to;
  document.getElementById('fromDate').value = state.from;
  document.getElementById('toDate').value = state.to;
  state.hideSkipped = document.getElementById('hideSkipped').checked;

  document.querySelectorAll('.presets button').forEach((b) => {
    b.onclick = () => applyPreset(Number(b.dataset.days));
  });
  document.getElementById('fromDate').onchange = (e) => { state.from = e.target.value; state.page = 1; refresh(); };
  document.getElementById('toDate').onchange = (e) => { state.to = e.target.value; state.page = 1; refresh(); };
  document.getElementById('refreshBtn').onclick = () => { jobCache.clear(); caseCache.clear(); refresh(); };
  document.getElementById('jobSelect').onchange = () => { clearSelect('modelSelect'); readSelectors(); state.page = 1; renderCards(); renderCharts(); renderTable(); };
  document.getElementById('modelSelect').onchange = () => { clearSelect('jobSelect'); readSelectors(); state.page = 1; renderCards(); renderCharts(); renderTable(); };
  enableToggleDeselect(document.getElementById('jobSelect'));
  enableToggleDeselect(document.getElementById('modelSelect'));
  document.getElementById('clearFilters').onclick = () => {
    clearSelect('jobSelect');
    clearSelect('modelSelect');
    readSelectors();
    state.page = 1;
    renderCards();
    renderCharts();
    renderTable();
  };
  document.getElementById('caseSearch').oninput = (e) => { state.search = e.target.value.trim().toLowerCase(); state.page = 1; renderCards(); renderCharts(); renderTable(); };
  document.getElementById('hideSkipped').onchange = (e) => { state.hideSkipped = e.target.checked; renderCards(); renderCharts(); renderTable(); };
  document.getElementById('detailClose').onclick = () => { state.detail = null; document.getElementById('detailCard').hidden = true; };
  document.getElementById('sortSelect').onchange = (e) => { state.sort.key = e.target.value; state.page = 1; renderTable(); };
  document.getElementById('sortDirBtn').onclick = () => {
    state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
    state.page = 1;
    renderTable();
  };
  document.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
      t.classList.add('active');
      state.tab = t.dataset.tab;
      state.sort = { key: null, dir: 'desc' };
      state.page = 1;
      state.detail = null;
      document.getElementById('detailCard').hidden = true;
      renderTable();
    };
  });

  await refresh();
}

init().catch((err) => {
  console.error(err);
  document.getElementById('metaInfo').textContent = `加载失败：${err.message}`;
});
