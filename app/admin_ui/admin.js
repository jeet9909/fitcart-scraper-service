'use strict';
/* MyDripCheck Admin Console: talks to the API it is served from (/v1/admin/*). */
const API = location.origin;
const KEY = 'mdc-admin-session';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr = n => n == null ? '–' : '₹' + Number(n).toLocaleString('en-IN', {maximumFractionDigits: Number(n) % 1 ? 2 : 0});
const num = n => n == null ? '–' : Number(n).toLocaleString('en-IN');
const pct = n => n == null ? '–' : n + '%';

const S = {token: null, email: null, page: 'overview', days: 7, query: '', filter: 'All', cache: {}, notices: []};
const SECTIONS = [
  ['overview', '◫', 'Overview'], ['users', '♙', 'Users & looks'], ['tryons', '◇', 'Try-on activity'],
  ['products', '▤', 'Products'], ['revenue', '↗', 'Revenue & plans'], ['integrations', '⊞', 'Integrations'],
  ['support', '☏', 'Support inbox'], ['settings', '⚙', 'Settings'],
];
const KIND = {look: 'Look', outfit: 'Outfit', spin: '360° view', pose: 'Social pose', scrape: 'Product import'};
const PLAN = {free: 'Free', pass: 'Pass', plus: 'Plus', pro: 'Pro', unlimited: 'Unlimited', bonus: 'Bonus'};

/* ---------- helpers ---------- */
function load() { try { return JSON.parse(sessionStorage.getItem(KEY) || localStorage.getItem(KEY) || 'null'); } catch { return null; } }
function store(v) { try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : (localStorage.removeItem(KEY), sessionStorage.removeItem(KEY)); } catch {} }

async function api(path, {method = 'GET', body} = {}) {
  let res;
  try {
    res = await fetch(API + path, {method, headers: {'Content-Type': 'application/json', ...(S.token ? {Authorization: 'Bearer ' + S.token} : {})},
      body: body ? JSON.stringify(body) : undefined});
  } catch { throw new Error('Could not reach the MyDripCheck API. Check your connection.'); }
  let data = null;
  try { data = await res.json(); } catch {}
  if (res.status === 401 && path !== '/v1/admin/session') { logout('Your session ended. Sign in again.'); throw new Error('Signed out'); }
  if (!res.ok) {
    const d = data?.detail;
    throw new Error(typeof d === 'string' ? d : d?.message || (Array.isArray(d) ? d[0]?.msg : '') || `Request failed (${res.status})`);
  }
  return data;
}

function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.classList.toggle('bad', !!bad); t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 3600);
}

function when(iso) {
  if (!iso) return '–';
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 86400 * 7) return Math.floor(s / 86400) + ' d ago';
  return d.toLocaleDateString('en-IN', {day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'});
}
const fullDate = iso => iso ? new Date(iso).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'}) : '–';

function badge(s, label) {
  const v = String(s || '').toLowerCase();
  const cls = ['failed', 'suspended', 'offline', 'high'].includes(v) ? 'bad'
    : ['queued', 'review', 'open', 'degraded', 'rejected', 'not set up', 'warn'].includes(v) ? 'warn'
    : ['free', 'normal', 'off', 'resolved'].includes(v) ? 'neutral'
    : ['pro', 'plus', 'pass', 'unlimited', 'bonus'].includes(v) ? 'gold' : '';
  return `<span class="status ${cls}">${esc(label || PLAN[v] || s)}</span>`;
}

function change(c, suffix) {
  if (c == null) return `<small>${suffix || 'No earlier data to compare'}</small>`;
  return `<small class="${c < 0 ? 'down' : ''}">${c >= 0 ? '↑' : '↓'} ${Math.abs(c)}% vs previous ${S.days} days</small>`;
}

function title(eyebrow, h, sub, buttons = '') {
  return `<div class="heading"><div><div class="eyebrow">${eyebrow}</div><h1>${h}</h1><p class="sub">${sub}</p></div><div class="actions">${buttons}</div></div>`;
}
function stats(items) {
  return `<div class="stats">${items.map((x, i) => `<div class="stat ${i === 0 ? 'feature' : ''}"><span class="statlabel">${x[0]}</span><span class="mini">${x[3] || ['↗', '♙', '◇', '◷'][i]}</span><strong title="${esc(x[1])}">${x[1]}</strong>${x[2]}</div>`).join('')}</div>`;
}
const periodSelect = (opts = [7, 30]) => `<select aria-label="Report period" data-act="period">${opts.map(d => `<option value="${d}" ${S.days === d ? 'selected' : ''}>Last ${d} days</option>`).join('')}</select>`;
const toolbar = (ph, options) => `<div class="toolbar"><input type="search" aria-label="Search" placeholder="${ph}" value="${esc(S.query)}" data-act="search"><select aria-label="Filter" data-act="filter">${options.map(o => `<option value="${o[0]}" ${S.filter === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')}</select></div>`;
const matches = o => !S.query || JSON.stringify(o).toLowerCase().includes(S.query.toLowerCase());
const initials = e => (e || '?').split('@')[0].split(/[._-]/).filter(Boolean).slice(0, 2).map(p => p[0]).join('').toUpperCase() || '?';
const setupNote = what => `<div class="alert"><span style="font-size:20px">ⓘ</span><p><b>One-time database setup needed</b>Run <code>supabase/schema.sql</code> in the Supabase SQL editor to turn on ${what}.</p></div>`;

function csv(name, rows) {
  if (!rows.length) return toast('Nothing to export.');
  const cols = Object.keys(rows[0]);
  const cell = v => '"' + String(typeof v === 'object' && v !== null ? JSON.stringify(v) : v ?? '').replace(/^[=+@-]/, "'$&").replaceAll('"', '""') + '"';
  const text = [cols.map(cell).join(','), ...rows.map(r => cols.map(k => cell(r[k])).join(','))].join('\r\n');
  const a = document.createElement('a'), url = URL.createObjectURL(new Blob(['﻿' + text], {type: 'text/csv;charset=utf-8;'}));
  a.href = url; a.download = `MyDripCheck-${name}-${new Date().toISOString().slice(0, 10)}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast('CSV downloaded.');
}

/* ---------- auth ---------- */
function showLogin(msg) {
  $('#app').hidden = true; $('#login').hidden = false;
  $('#loginError').textContent = msg || '';
  setTimeout(() => $('#loginEmail').focus(), 50);
}
function logout(msg) { S.token = null; store(null); S.cache = {}; showLogin(msg); }

$('#loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('#loginButton'); btn.disabled = true; btn.textContent = 'Signing in…'; $('#loginError').textContent = '';
  try {
    const r = await api('/v1/admin/session', {method: 'POST', body: {email: $('#loginEmail').value, password: $('#loginPassword').value}});
    S.token = r.access_token; S.email = r.email; store({token: r.access_token, email: r.email, exp: r.expires_at});
    $('#loginPassword').value = ''; start();
  } catch (err) { $('#loginError').textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = 'Sign in'; }
});

function start() {
  $('#login').hidden = true; $('#app').hidden = false;
  $('#whoami').textContent = S.email || ''; $('#avatar').textContent = initials(S.email);
  const fromHash = location.hash.slice(1);
  go(SECTIONS.some(s => s[0] === fromHash) ? fromHash : 'overview', true);
}

/* ---------- navigation ---------- */
function nav() {
  const open = S.cache.overview?.open_tickets;
  $('#nav').innerHTML = SECTIONS.map(([k, i, t]) => `<button class="${S.page === k ? 'active' : ''}" ${S.page === k ? 'aria-current="page"' : ''} data-go="${k}"><span class="ico">${i}</span>${t}${k === 'support' && open ? `<span class="nav-count">${open}</span>` : ''}</button>`).join('');
  $('#crumb').textContent = SECTIONS.find(s => s[0] === S.page)[2];
}

function go(page, keepFilters) {
  S.page = page; if (!keepFilters) { S.query = ''; S.filter = 'All'; }
  if (location.hash.slice(1) !== page) history.replaceState(null, '', '#' + page);
  document.body.classList.remove('nav-open');
  nav(); render(); window.scrollTo(0, 0);
}

async function render(fresh) {
  const page = S.page, main = $('#main');
  if (fresh) delete S.cache[page + S.days];
  const cached = S.cache[page + S.days];
  if (!cached) main.innerHTML = `<div class="loading">Loading ${esc(SECTIONS.find(s => s[0] === page)[2].toLowerCase())}…</div>`;
  try {
    const data = cached || await LOADERS[page]();
    S.cache[page + S.days] = data;
    if (S.page !== page) return;
    main.innerHTML = VIEWS[page](data) + `<div class="footer"><span>MYDRIPCHECK ADMIN CONSOLE · Live data</span><span>Signed in as ${esc(S.email)}</span></div>`;
    nav(); notices();
  } catch (err) {
    if (S.page === page && S.token) main.innerHTML = `<div class="panel empty"><h2>Could not load this page</h2><p class="sub">${esc(err.message)}</p><button class="small" data-act="refresh" style="margin-top:14px">Try again</button></div>`;
  }
}

const LOADERS = {
  overview: () => api(`/v1/admin/overview?days=${S.days}`),
  users: () => api('/v1/admin/users'),
  tryons: () => api(`/v1/admin/activity?days=${S.days}&limit=500`),
  products: () => api('/v1/admin/products'),
  revenue: () => api(`/v1/admin/revenue?days=${S.days}`),
  integrations: () => api('/v1/admin/integrations'),
  support: () => api('/v1/admin/support'),
  settings: () => api('/v1/admin/settings'),
};

/* ---------- views ---------- */
function chart(series) {
  const max = Math.max(4, ...series.map(d => d.completed + d.failed));
  const top = Math.ceil(max / 4) * 4, h = 189;
  const label = d => { const x = new Date(d.date + 'T00:00:00'); return series.length <= 7 ? x.toLocaleDateString('en-IN', {weekday: 'short'}) : x.getDate(); };
  return `<div class="chart"><div class="axis">${[1, .75, .5, .25, 0].map(f => `<span>${Math.round(top * f)}</span>`).join('')}</div><div class="plot"><div class="bars">${series.map(d => {
    const ok = d.completed / top * h, bad = d.failed / top * h;
    return `<div class="bar" tabindex="0" role="img" aria-label="${d.date}: ${d.completed} completed, ${d.failed} failed"><i class="ok" style="height:${ok}px"></i><i class="bad" style="height:${bad}px"></i><span class="tip">${new Date(d.date + 'T00:00:00').toLocaleDateString('en-IN', {day: 'numeric', month: 'short'})}: ${d.completed} done${d.failed ? ', ' + d.failed + ' failed' : ''}</span></div>`;
  }).join('')}</div><div class="labels">${series.map(d => `<span>${label(d)}</span>`).join('')}</div></div></div>`;
}

function eventRows(events, empty = 'Nothing yet. Activity appears here as soon as people create looks.') {
  return `<div class="table-wrap"><table><thead><tr><th>Request / user</th><th>Details</th><th>Status</th><th>Time</th><th>Est. cost</th><th></th></tr></thead><tbody>${events.map(e => `<tr>
    <td><b>${KIND[e.kind] || esc(e.kind)}</b><small>${esc(e.email || (e.user_id ? 'Guest ' + e.user_id.slice(0, 8) : 'Guest'))} · ${when(e.created_at)}</small></td>
    <td class="wrap">${esc(e.meta?.product || e.meta?.store || e.meta?.pose || '–')}<small>${[e.meta?.pieces ? e.meta.pieces + ' piece' + (e.meta.pieces > 1 ? 's' : '') : '', e.meta?.pose && e.kind !== 'pose' ? (e.meta.pose === 'keep' ? 'My pose' : 'Standard pose') : '', e.kind === 'pose' ? '' : e.meta?.store].filter(Boolean).map(esc).join(' · ')}</small></td>
    <td>${badge(e.status)}${e.error ? `<small style="max-width:220px;white-space:normal">${esc(e.error.slice(0, 90))}</small>` : ''}</td>
    <td>${e.duration_ms != null ? (e.duration_ms / 1000).toFixed(1) + ' s' : '–'}</td>
    <td>${e.cost_inr ? inr(e.cost_inr) : '–'}</td>
    <td><button class="small" data-act="event" data-id="${esc(e.id)}">View →</button></td></tr>`).join('') || `<tr><td colspan="6" class="empty">${empty}</td></tr>`}</tbody></table></div>`;
}

const VIEWS = {
  overview(d) {
    const sr = d.success_rate;
    return title('YOUR BUSINESS, AT A GLANCE', 'How MyDripCheck is doing', `Welcome back. Live numbers for the last ${d.days} days.`,
      periodSelect() + `<button data-act="export-overview">↓ Export</button>`)
      + stats([
        ['Revenue', inr(d.revenue.value), change(d.revenue.change, d.test_mode ? 'Razorpay test mode: not real money' : '')],
        ['Active users', num(d.active_users.value), change(d.active_users.change)],
        ['Looks created', num(d.looks.value), change(d.looks.change)],
        ['Success rate', pct(sr.value), `<small>${sr.previous != null && sr.value != null ? (sr.value >= sr.previous ? '↑ ' : '↓ ') + Math.abs(Math.round((sr.value - sr.previous) * 10) / 10) + ' points' : 'Completed vs failed try-ons'}</small>`],
      ])
      + `<div class="alerts">${d.alerts.map(a => `<div class="alert ${a.level === 'bad' ? 'bad' : ''}"><span style="font-size:20px">${a.level === 'bad' ? '⚠' : 'ⓘ'}</span><p><b>${esc(a.title)}</b>${esc(a.message)}</p><button class="small" data-go="${a.page}">Review →</button></div>`).join('')}</div>`
      + `<div class="grid"><section class="panel"><div class="panelhead"><div><h2>Outfits brought to life</h2><p class="sub">Completed and failed try-ons per day (India time)</p></div><span class="badge">TRY-ONS</span></div>${chart(d.series)}
        <div class="chartfoot"><span><span class="legend-dot" style="background:var(--green)"></span>Completed</span><span><span class="legend-dot" style="background:#e2a69b"></span>Failed <b>${num(d.failed)}</b></span><span>Average time <b>${d.avg_seconds != null ? d.avg_seconds + ' s' : '–'}</b></span><span>Average cost per look <b>${inr(d.avg_cost_inr)}</b></span><span>New sign-ups <b>${num(d.signups.value)}</b> of ${num(d.signups.total)}</span></div></section>
        <section class="panel"><div class="panelhead"><div><h2>Store imports</h2><p class="sub">Product links pasted by shoppers</p></div><button class="small" data-go="integrations">All services</button></div>
        ${d.stores.slice(0, 6).map(s => `<div class="service"><span class="service-icon">${esc(s.store[0])}</span><div><b>${esc(s.store)}</b><small>${s.attempts} imports · ${pct(s.success_rate)} worked</small></div>${badge(s.status)}</div>`).join('') || '<p class="sub">No product links imported in this period.</p>'}</section></div>`
      + `<section class="panel"><div class="panelhead"><div><h2>Latest try-on activity</h2><p class="sub">The newest requests, from start to finish</p></div><button class="small" data-go="tryons">All activity →</button></div>${eventRows(d.latest)}</section>`;
  },

  users(d) {
    const rows = d.users.filter(u => matches(u) && (S.filter === 'All' || u.plan === S.filter || u.status === S.filter));
    return title('PEOPLE & ACCESS', 'Users & looks', 'Every MyDripCheck account, their plan and the looks they have left.', `<button data-act="export-users">↓ Export users</button>`)
      + stats([['Registered accounts', num(d.total), '<small>Email sign-ups, all time</small>'], ['Paying now', num(d.paying), '<small>Active Pass, Plus or Pro</small>'],
        ['New this week', num(d.new_this_week), '<small>Last 7 days</small>'], ['Suspended', num(d.suspended), '<small>Blocked from creating looks</small>']])
      + `<section class="panel">${toolbar('Search email or user ID…', [['All', 'All accounts'], ['free', 'Free'], ['pass', 'Pass'], ['plus', 'Plus'], ['pro', 'Pro'], ['unlimited', 'Unlimited'], ['suspended', 'Suspended']])}
      <div class="table-wrap"><table><thead><tr><th>Account</th><th>Plan</th><th>Looks left</th><th>Looks made</th><th>Joined</th><th>Last sign-in</th><th>Status</th><th></th></tr></thead><tbody>${rows.slice(0, 500).map(u => `<tr>
        <td><div class="person"><span class="avatar">${initials(u.email)}</span><div><b>${esc(u.email)}</b>${u.admin ? ' ' + badge('admin', 'Admin') : ''}<small>${u.id}</small></div></div></td>
        <td>${badge(u.plan)}</td><td>${u.looks_left == null ? '∞' : num(u.looks_left)}</td><td>${num(u.looks_made)}</td>
        <td>${when(u.created_at)}</td><td>${when(u.last_sign_in_at)}</td><td>${badge(u.status)}</td>
        <td><button class="small" data-act="user" data-id="${u.id}">Manage →</button></td></tr>`).join('') || '<tr><td colspan="8" class="empty">No accounts match.</td></tr>'}</tbody></table></div>
      ${rows.length > 500 ? `<p class="sub" style="margin-top:12px">Showing 500 of ${rows.length}. Search to narrow down.</p>` : ''}</section>`;
  },

  tryons(d) {
    const st = d.stats;
    const rows = d.events.filter(e => matches(e) && (S.filter === 'All' || e.status === S.filter || e.kind === S.filter));
    return title('GENERATION MONITOR', 'Try-on activity', 'Every look, 360° view, social pose and product import, with failures and their reasons.', periodSelect([7, 30, 90]) + `<button data-act="export-activity">↓ Export</button>`)
      + (d.setup_needed ? setupNote('activity logging') : '')
      + stats([['Completed', num(st.completed), `<small>${num(st.looks)} looks · ${st.active_users} people</small>`], ['Failed', num(st.failed), '<small>Errors after a request started</small>'],
        ['Success rate', pct(st.success_rate), `<small>Average look ${st.avg_seconds != null ? st.avg_seconds + ' s' : '–'}</small>`], ['Model cost (est.)', inr(st.cost_inr), `<small>${st.rejected} refused (no looks, plan)</small>`]])
      + `<section class="panel">${toolbar('Search email, product, error…', [['All', 'Everything'], ['completed', 'Completed'], ['failed', 'Failed'], ['rejected', 'Refused'], ['look', 'Looks'], ['outfit', 'Outfits'], ['spin', '360° views'], ['pose', 'Social poses'], ['scrape', 'Product imports']])}${eventRows(rows, d.setup_needed ? 'Activity appears after the database setup.' : 'No activity matches.')}</section>`;
  },

  products(d) {
    const slots = [...new Set(d.products.map(p => p.slot).filter(Boolean))];
    const rows = d.products.filter(p => matches(p) && (S.filter === 'All' || p.slot === S.filter || p.store === S.filter));
    const icon = {top: '👕', bottom: '👖', dress: '👗', outerwear: '🧥', footwear: '👟', jewelry: '💍', accessory: '🕶', other: '🛍'};
    return title('WHAT SHOPPERS WANT', 'Products', 'Pieces people try on and save, across every store. Use it to spot trends and partner stores.', `<button data-act="export-products">↓ Export</button>`)
      + stats([['Different products', num(d.total), '<small>Tried on or saved</small>'], ['Looks created', num(d.looks), '<small>All time</small>'],
        ['Saved to wardrobes', num(d.saved), '<small>From store links</small>'], ['Top store', esc(d.stores[0]?.[0] || '–'), `<small>${d.stores[0] ? num(d.stores[0][1]) + ' products' : ''}</small>`]])
      + `<section>${toolbar('Search product or store…', [['All', 'All products'], ...slots.map(s => [s, s[0].toUpperCase() + s.slice(1)]), ...d.stores.map(s => [s[0], s[0]])])}
      <div class="cards">${rows.slice(0, 120).map(p => `<section class="panel"><div class="product-art">${icon[p.slot] || '🛍'}</div><p class="eyebrow">${esc(p.store || 'Uploaded photo')} · ${esc(p.slot || 'item')}</p><h2>${esc(p.name || 'Unnamed product')}</h2>
        <div class="productmeta"><b>${p.price ? inr(p.price) : 'Price not saved'}</b><span>${num(p.tried)} tried · ${num(p.saved)} saved</span></div>
        <p class="sub">Last seen ${when(p.last_seen)}</p>${p.product_url ? `<a class="small" href="${esc(p.product_url)}" target="_blank" rel="noopener noreferrer">Open in store ↗</a>` : ''}</section>`).join('') || '<div class="empty">No products yet.</div>'}</div></section>`;
  },

  revenue(d) {
    return title('BUSINESS PERFORMANCE', 'Revenue & plans', 'Payments from Razorpay, model costs and what is left.', periodSelect([7, 30, 90, 365]) + `<button data-act="export-revenue">↓ Export payments</button>`)
      + (d.test_mode ? `<div class="alert"><span style="font-size:20px">ⓘ</span><p><b>Razorpay is in test mode</b>These payments are test payments, not real money. Switch to live keys in Render when you launch.</p></div>` : '')
      + stats([['Gross revenue', inr(d.gross_inr), change(d.change)], ['Model cost (est.)', inr(d.generation_cost_inr), '<small>Gemini and Vertex calls</small>', '◇'],
        ['GST + fixed costs', inr(d.gst_inr + d.fixed_cost_inr), `<small>GST ${inr(d.gst_inr)} · fixed ${inr(d.fixed_cost_inr)}</small>`, '₹'],
        ['Contribution', inr(d.contribution_inr), `<small>${d.gross_inr ? Math.round(d.contribution_inr / d.gross_inr * 100) + '% of revenue · ' : ''}before salaries & tax</small>`, '◷']])
      + `<div class="grid"><section class="panel"><div class="panelhead"><div><h2>Payments</h2><p class="sub">${d.purchases.length} in this period · ${inr(d.all_time_inr)} all time</p></div></div>
        <div class="table-wrap"><table><thead><tr><th>Customer</th><th>Plan</th><th>Billing</th><th>Amount</th><th>Paid</th></tr></thead><tbody>${d.purchases.map(p => `<tr><td><b>${esc(p.email || p.user_id)}</b><small>${esc(p.ref)}</small></td><td>${badge(p.kind)}</td><td>${esc(p.billing)}</td><td>${inr(p.amount_inr)}</td><td>${when(p.created_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No payments in this period.</td></tr>'}</tbody></table></div></section>
        <section class="panel"><h2>Revenue by plan</h2><p class="sub">This period</p>${d.by_plan.map(p => `<div class="metric-row"><span>${esc(PLAN[p.plan])} · ${p.count} sold</span><b>${inr(p.amount_inr)}</b></div><div class="progress"><i style="width:${d.gross_inr ? p.amount_inr / d.gross_inr * 100 : 0}%"></i></div>`).join('')}
        <div class="help-box" style="margin-top:18px">Cost estimates: ${inr(d.costs.gemini_image)} per Gemini image and ${inr(d.costs.vertex)} per Vertex try-on. Fixed costs: ${inr(d.costs.monthly_fixed)}/month. Change them with <code>COST_GEMINI_IMAGE_INR</code>, <code>COST_VERTEX_TRYON_INR</code> and <code>MONTHLY_FIXED_COSTS_INR</code> in Render.</div></section></div>`
      + `<div class="panelhead"><div><h2>Plans on sale</h2><p class="sub">Prices include GST. They live in the code (app/billing.py) so checkout and the site always agree; ask to change them there.</p></div></div>
      <div class="cards">${d.plans.map(p => `<section class="panel plan" style="${p.key === 'plus' ? 'border-color:#92a776' : ''}"><div class="panelhead"><h2>${esc(PLAN[p.key] || p.name)}</h2>${p.key === 'plus' ? '<span class="badge">MOST POPULAR</span>' : ''}</div>
        <div class="plan-price">${p.prices.once != null ? inr(p.prices.once) + '<small> once</small>' : inr(p.prices.monthly) + '<small> / month</small>'}</div>
        <p class="sub">${p.prices.yearly ? inr(p.prices.yearly) + ' / year' : '&nbsp;'}</p><ul><li>${esc(p.note)}</li></ul></section>`).join('')}</div>`;
  },

  integrations(d) {
    const icon = {gemini: 'G', database: 'S', auth: '@', razorpay: '₹', brightdata: 'B', vertex: 'V'};
    return title('CONNECTIONS & RELIABILITY', 'Integrations', `Live checks, run just now (${new Date(d.checked_at).toLocaleTimeString('en-IN', {timeStyle: 'short'})}). No secrets are shown.`, `<button data-act="refresh">↻ Run checks again</button>`)
      + `<div class="cards">${d.services.map(s => `<section class="panel"><div class="panelhead"><span class="service-icon">${icon[s.key] || '•'}</span>${badge(s.status)}</div><h2>${esc(s.name)}</h2><p class="sub">${esc(s.provider)}</p><p style="font-size:12px;margin:12px 0 4px">${esc(s.detail)}</p><p class="sub">Answered in ${s.latency_ms} ms</p></section>`).join('')}</div>`
      + `<div class="two" style="margin-top:22px"><section class="panel"><h2>Store imports (last 7 days)</h2><p class="sub">How often pasted product links worked, per store</p>${d.stores.map(s => `<div class="metric-row"><span>${esc(s.store)} · ${s.attempts} imports</span><b>${pct(s.success_rate)}</b></div><div class="progress ${s.status !== 'Healthy' ? 'bad' : ''}"><i style="width:${s.success_rate ?? 0}%"></i></div>`).join('') || '<p class="sub" style="margin-top:12px">No imports yet.</p>'}</section>
      <section class="panel"><h2>Gemini since the last restart</h2><p class="sub">Counted by this server; resets on every deploy</p><div class="kv" style="margin-top:14px"><span>Since</span><b>${fullDate(d.gemini_since_restart.since)}</b><span>Requests</span><b>${num(d.gemini_since_restart.requests)}</b><span>Succeeded</span><b>${num(d.gemini_since_restart.succeeded)}</b><span>Failed</span><b>${num(d.gemini_since_restart.failed)}</b><span>Last error</span><b>${esc(d.gemini_since_restart.last_error || 'None')}</b></div><p class="sub" style="margin-top:14px">Google does not show remaining credit through the API. See <a href="https://aistudio.google.com/usage" target="_blank" rel="noopener noreferrer">AI Studio usage</a>.</p></section></div>`;
  },

  support(d) {
    const rows = d.tickets.filter(t => matches(t) && (S.filter === 'All' || t.status === S.filter || t.priority === S.filter));
    const open = d.tickets.filter(t => t.status === 'open');
    return title('CUSTOMER CARE', 'Support inbox', 'Messages sent from the help form on mydripcheck.com.', `<button data-act="export-support">↓ Export</button>`)
      + (d.setup_needed ? setupNote('the support inbox') : '')
      + stats([['Open', num(open.length), '<small>Waiting for a reply</small>', '☏'], ['High priority', num(open.filter(t => t.priority === 'high').length), '<small>Payments and missing looks</small>', '!'],
        ['Resolved', num(d.tickets.length - open.length), '<small>All time</small>', '✓'], ['Oldest open', open.length ? when(open[open.length - 1].created_at) : '–', '<small>First in line</small>', '◷']])
      + `<section class="panel">${toolbar('Search ticket, email or message…', [['All', 'All tickets'], ['open', 'Open'], ['resolved', 'Resolved'], ['high', 'High priority']])}
      <div class="table-wrap"><table><thead><tr><th>Ticket</th><th>Customer</th><th>Priority</th><th>Status</th><th>Received</th><th></th></tr></thead><tbody>${rows.map(t => `<tr><td class="wrap"><b>${esc(t.subject)}</b><small>${esc(t.message.slice(0, 90))}${t.message.length > 90 ? '…' : ''}</small></td><td>${esc(t.email)}</td><td>${badge(t.priority)}</td><td>${badge(t.status)}</td><td>${when(t.created_at)}</td><td><button class="small" data-act="ticket" data-id="${t.id}">Open →</button></td></tr>`).join('') || '<tr><td colspan="6" class="empty">No tickets. Nice.</td></tr>'}</tbody></table></div></section>`;
  },

  settings(d) {
    const s = d.settings, sv = d.server;
    const toggle = (key, t, sub) => `<label class="setting"><span><b>${t}</b><small>${sub}</small></span><input class="switch" type="checkbox" name="${key}" ${s[key] ? 'checked' : ''}></label>`;
    return title('WORKSPACE PREFERENCES', 'Settings', 'Switches that change the live site, and who changed what.')
      + (d.setup_needed ? setupNote('saved settings and the audit log') : '')
      + `<div class="two"><section class="panel"><h2>Live site controls</h2><p class="sub">Saved to the database and picked up by the API within 30 seconds.</p>
        ${toggle('maintenance', 'Maintenance mode', 'Pause new looks, 360° views and poses for everyone. Browsing and payments keep working.')}
        <label class="field">Maintenance message<input id="setMaintenanceMessage" maxlength="300" value="${esc(s.maintenance_message)}"></label>
        <label class="field">Site announcement (shown as a banner, leave empty for none)<input id="setAnnouncement" maxlength="300" value="${esc(s.announcement)}" placeholder="e.g. Diwali offer: 20% off Plus this week"></label>
        <label class="field">Support email shown to customers<input id="setSupportEmail" type="email" maxlength="254" value="${esc(s.support_email)}" placeholder="support@mydripcheck.com"></label>
        <button class="primary" data-act="save-settings">Save changes</button></section>
        <section class="panel"><h2>Server configuration</h2><p class="sub">Set in Render's environment. Shown read-only.</p><div class="kv" style="margin-top:14px">
        <span>Free looks / month</span><b>${sv.free_looks_per_month}</b><span>Look limits</span><b>${sv.look_limits_enabled ? 'On' : 'Off'}</b>
        <span>Image model</span><b>${esc(sv.image_model)}</b><span>Face check target</span><b>${sv.face_check_target}</b><span>Razorpay autopay</span><b>${sv.razorpay_autopay ? 'On' : 'Off (prepaid)'}</b>
        <span>Admins</span><b>${sv.admin_emails.map(esc).join('<br>') || '–'}</b><span>Unlimited accounts</span><b>${sv.unlimited_emails.map(esc).join('<br>') || 'None'}</b><span>Suspended accounts</span><b>${d.suspended_count}</b></div>
        <h3 style="margin-top:26px">Activity log</h3>${d.audit.slice(0, 12).map(a => `<div class="service"><span class="service-icon">✓</span><div><b>${esc(describeAudit(a))}</b><small>${esc(a.admin_email)} · ${when(a.created_at)}</small></div></div>`).join('') || '<p class="sub">No admin changes yet.</p>'}</section></div>`;
  },
};

function describeAudit(a) {
  const d = a.detail || {};
  return {add_looks: `Gave ${d.looks} looks (${d.days} days)${d.reason ? ': ' + d.reason : ''}`, suspend: `Suspended ${d.email || a.target}`, restore: `Restored ${d.email || a.target}`,
    settings: 'Changed ' + Object.keys(d).join(', '), ticket: `Updated ticket ${String(a.target).slice(0, 8)}${d.status ? ' → ' + d.status : ''}`}[a.action] || a.action;
}

/* ---------- dialogs ---------- */
let lastFocus;
function modal(t, body, wide) {
  if (!$('#modal').classList.contains('open')) lastFocus = document.activeElement;
  $('#dialog').className = 'dialog' + (wide ? ' wide' : '');
  $('#dialog').innerHTML = `<div class="dialoghead"><h2 id="dialog-title">${t}</h2><button class="small" data-act="close" aria-label="Close dialog">✕</button></div>${body}`;
  $('#modal').classList.add('open'); ($('#dialog input, #dialog textarea') || $('#dialog button')).focus();
}
function closeModal() { $('#modal').classList.remove('open'); lastFocus?.focus?.(); }
const detail = (k, v) => `<div class="detail"><span>${k}</span><b>${v}</b></div>`;

async function userDialog(id) {
  modal('Loading…', '<div class="loading">Loading account…</div>', true);
  try {
    const u = await api('/v1/admin/users/' + id);
    const active = u.grants.filter(g => g.active);
    modal(esc(u.email), `<p class="sub">${u.id}</p>
      ${detail('Joined', fullDate(u.created_at))}${detail('Last sign-in', fullDate(u.last_sign_in_at))}${detail('Status', badge(u.suspended ? 'suspended' : 'active'))}
      ${detail('Looks left now', u.unlimited ? 'Unlimited' : num(active.reduce((n, g) => n + g.remaining, 0)))}
      <h3>Active allowances</h3>${active.map(g => `<div class="detail"><span>${badge(g.kind)} ${g.period || ''}</span><b>${g.remaining} of ${g.looks} left · until ${fullDate(g.expires_at)}</b></div>`).join('') || '<p class="sub">No active allowance this month yet (the free looks start on the first try-on).</p>'}
      ${u.purchases.length ? `<h3>Payments</h3>${u.purchases.map(p => detail(`${badge(p.kind)} ${esc(p.billing)}`, `${inr(p.amount_inr)} · ${fullDate(p.created_at)}`)).join('')}` : ''}
      <h3>Give free looks</h3><div class="actions"><input id="giveLooks" type="number" min="1" max="1000" value="5" style="width:90px" aria-label="Looks"><input id="giveDays" type="number" min="1" max="365" value="30" style="width:90px" aria-label="Valid for days"><span class="sub">looks, valid for days</span></div>
      <label class="field">Reason (saved in the activity log)<input id="giveReason" maxlength="200" placeholder="e.g. Sorry for the failed look"></label>
      <div class="actions"><button class="primary" data-act="give-looks" data-id="${u.id}">Add looks</button><button class="danger" data-act="toggle-user" data-id="${u.id}" data-suspended="${u.suspended}">${u.suspended ? 'Restore account' : 'Suspend account'}</button></div>
      <h3>Recent activity</h3>${eventRows(u.events.slice(0, 8), 'No activity recorded.')}
      <h3>Saved looks</h3>${u.looks.map(l => detail(esc(l.category), fullDate(l.created_at))).join('') || '<p class="sub">No saved looks.</p>'}
      <p class="sub" style="margin-top:14px">Customer photos are private and are not shown here.</p>`, true);
  } catch (err) { modal('Could not open account', `<p>${esc(err.message)}</p>`); }
}

function eventDialog(id) {
  const all = [S.cache['tryons' + S.days]?.events, S.cache['overview' + S.days]?.latest].flat().filter(Boolean);
  const e = all.find(x => String(x.id) === String(id));
  if (!e) return toast('Refresh and try again.');
  modal(KIND[e.kind] || esc(e.kind), `${detail('Status', badge(e.status))}${detail('Customer', esc(e.email || e.user_id || 'Guest'))}${detail('When', fullDate(e.created_at))}
    ${detail('Time taken', e.duration_ms != null ? (e.duration_ms / 1000).toFixed(1) + ' s' : '–')}${detail('Model calls', `${e.image_calls} Gemini image · ${e.vertex_calls} Vertex`)}${detail('Estimated cost', inr(e.cost_inr))}
    ${Object.entries(e.meta || {}).map(([k, v]) => detail(esc(k), esc(v))).join('')}${detail('HTTP status', e.http_status ?? '–')}
    ${e.error ? `<h3>What went wrong</h3><div class="message">${esc(e.error)}</div>` : ''}
    ${e.status === 'failed' ? '<div class="help-box" style="margin-top:16px">Failed looks are never charged: the look goes back to the customer automatically. If it keeps failing, check Integrations.</div>' : ''}`);
}

function ticketDialog(id) {
  const t = S.cache['support' + S.days]?.tickets.find(x => x.id === id);
  if (!t) return;
  const reply = `mailto:${encodeURIComponent(t.email)}?subject=${encodeURIComponent('Re: ' + t.subject)}&body=${encodeURIComponent('\n\n---\n' + t.message)}`;
  modal(esc(t.subject), `${detail('Customer', esc(t.email))}${detail('Received', fullDate(t.created_at))}${detail('Priority', badge(t.priority))}${detail('Status', badge(t.status))}
    <h3>Message</h3><div class="message">${esc(t.message)}</div>
    <label class="field">Internal note (only admins see this)<textarea id="ticketNote" rows="3" maxlength="4000">${esc(t.note || '')}</textarea></label>
    <div class="actions"><a class="btn-link" href="${reply}">✉ Reply by email</a>
    <button data-act="ticket-save" data-id="${t.id}">Save note</button>
    <button data-act="ticket-priority" data-id="${t.id}" data-value="${t.priority === 'high' ? 'normal' : 'high'}">${t.priority === 'high' ? 'Set normal priority' : 'Mark high priority'}</button>
    <button class="primary" data-act="ticket-status" data-id="${t.id}" data-value="${t.status === 'open' ? 'resolved' : 'open'}">${t.status === 'open' ? 'Mark resolved' : 'Reopen'}</button></div>`, true);
}

function notices() {
  const o = S.cache['overview' + S.days] || S.cache.overview7;
  S.notices = o ? o.alerts : [];
  const n = S.notices.length + (o?.open_tickets ? 1 : 0);
  $('#notice').textContent = n;
  const badgeEl = $('#modeBadge');
  if (o) { badgeEl.hidden = false; badgeEl.textContent = o.test_mode ? 'PAYMENTS · TEST MODE' : 'PAYMENTS · LIVE'; badgeEl.classList.toggle('warn', o.test_mode); }
}

/* ---------- events ---------- */
document.addEventListener('click', async e => {
  const goTo = e.target.closest('[data-go]');
  if (goTo) { closeModal(); return go(goTo.dataset.go); }
  const el = e.target.closest('[data-act]');
  if (!el) { if (e.target === $('#modal')) closeModal(); return; }
  const act = el.dataset.act, id = el.dataset.id;
  const data = S.cache[S.page + S.days];
  try {
    switch (act) {
      case 'open-nav': document.body.classList.add('nav-open'); break;
      case 'close-nav': document.body.classList.remove('nav-open'); break;
      case 'logout': logout(); break;
      case 'refresh': render(true); break;
      case 'close': closeModal(); break;
      case 'user': userDialog(id); break;
      case 'event': eventDialog(id); break;
      case 'ticket': ticketDialog(id); break;
      case 'notifications':
        modal('Needs your attention', (S.notices.map(a => `<div class="service"><span class="service-icon">${a.level === 'bad' ? '⚠' : 'ⓘ'}</span><div><b>${esc(a.title)}</b><small>${esc(a.message)}</small></div><button class="small" data-go="${a.page}">Open</button></div>`).join('')
          + (S.cache['overview' + S.days]?.open_tickets ? `<div class="service"><span class="service-icon">☏</span><div><b>${S.cache['overview' + S.days].open_tickets} open support tickets</b><small>Customers waiting for a reply</small></div><button class="small" data-go="support">Open</button></div>` : '')) || '<p class="sub">All clear. Nothing needs you right now.</p>');
        break;
      case 'give-looks': {
        const looks = +$('#giveLooks').value, days = +$('#giveDays').value;
        if (!Number.isInteger(looks) || looks < 1 || looks > 1000) return toast('Enter 1 to 1,000 looks.', true);
        if (!Number.isInteger(days) || days < 1 || days > 365) return toast('Enter 1 to 365 days.', true);
        el.disabled = true;
        await api(`/v1/admin/users/${id}/looks`, {method: 'POST', body: {looks, days, reason: $('#giveReason').value}});
        toast(`${looks} looks added.`); delete S.cache['users' + S.days]; userDialog(id); if (S.page === 'users') render(true);
        break;
      }
      case 'toggle-user': {
        const suspend = el.dataset.suspended !== 'true';
        if (!confirm(suspend ? 'Suspend this account? They will not be able to sign in or create looks.' : 'Restore this account?')) return;
        el.disabled = true;
        await api(`/v1/admin/users/${id}/status`, {method: 'POST', body: {suspended: suspend}});
        toast(suspend ? 'Account suspended.' : 'Account restored.'); userDialog(id); if (S.page === 'users') render(true);
        break;
      }
      case 'ticket-save': case 'ticket-status': case 'ticket-priority': {
        const body = {note: $('#ticketNote').value};
        if (act === 'ticket-status') body.status = el.dataset.value;
        if (act === 'ticket-priority') body.priority = el.dataset.value;
        el.disabled = true;
        const updated = await api('/v1/admin/support/' + id, {method: 'PATCH', body});
        const list = S.cache['support' + S.days].tickets; list[list.findIndex(t => t.id === id)] = updated;
        toast('Ticket updated.'); render(); ticketDialog(id);
        break;
      }
      case 'save-settings': {
        const body = {maintenance: $('[name="maintenance"]').checked, maintenance_message: $('#setMaintenanceMessage').value.trim(),
          announcement: $('#setAnnouncement').value.trim(), support_email: $('#setSupportEmail').value.trim()};
        if (body.support_email && !$('#setSupportEmail').checkValidity()) return toast('Enter a valid support email.', true);
        if (body.maintenance && !confirm('Turn on maintenance mode? Nobody will be able to create looks until you turn it off.')) return;
        el.disabled = true;
        await api('/v1/admin/settings', {method: 'PUT', body});
        toast('Settings saved. The live site picks them up within 30 seconds.'); delete S.cache['overview' + S.days]; render(true);
        break;
      }
      case 'export-users': csv('users', data.users.map(u => ({email: u.email, id: u.id, plan: u.plan, looks_left: u.looks_left, looks_made: u.looks_made, status: u.status, joined: u.created_at, last_sign_in: u.last_sign_in_at}))); break;
      case 'export-activity': csv('activity', data.events.map(e => ({when: e.created_at, kind: e.kind, status: e.status, email: e.email, seconds: e.duration_ms / 1000, cost_inr: e.cost_inr, error: e.error, ...e.meta}))); break;
      case 'export-products': csv('products', data.products); break;
      case 'export-revenue': csv('payments', data.purchases.map(p => ({paid: p.created_at, email: p.email, plan: p.kind, billing: p.billing, amount_inr: p.amount_inr, ref: p.ref}))); break;
      case 'export-support': csv('support', data.tickets); break;
      case 'export-overview': csv('overview', data.series.map(s => ({...s, period_days: data.days}))); break;
    }
  } catch (err) { el.disabled = false; if (err.message !== 'Signed out') toast(err.message, true); }
});

document.addEventListener('change', e => {
  const act = e.target.dataset?.act;
  if (act === 'period') { S.days = +e.target.value; render(); }
  if (act === 'filter') { S.filter = e.target.value; rerender(); }
});
document.addEventListener('input', e => { if (e.target.dataset?.act === 'search') { S.query = e.target.value; clearTimeout(rerender.t); rerender.t = setTimeout(rerender, 150); } });
function rerender() {
  const data = S.cache[S.page + S.days]; if (!data) return;
  const pos = document.activeElement?.dataset?.act === 'search' ? document.activeElement.selectionStart : null;
  $('#main').innerHTML = VIEWS[S.page](data) + `<div class="footer"><span>MYDRIPCHECK ADMIN CONSOLE · Live data</span><span>Signed in as ${esc(S.email)}</span></div>`;
  if (pos != null) { const i = $('[data-act="search"]'); i.focus(); i.setSelectionRange(pos, pos); }
}
document.addEventListener('keydown', e => {
  if (!$('#modal').classList.contains('open')) return;
  if (e.key === 'Escape') closeModal();
  if (e.key === 'Tab') {
    const f = [...$('#dialog').querySelectorAll('button,input,select,textarea,a[href]')].filter(x => !x.disabled);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
    else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
  }
});
window.addEventListener('hashchange', () => { const p = location.hash.slice(1); if (S.token && p !== S.page && SECTIONS.some(s => s[0] === p)) go(p); });

/* ---------- boot ---------- */
(async () => {
  const saved = load();
  if (!saved?.token || (saved.exp && new Date(saved.exp) < new Date())) return showLogin();
  S.token = saved.token; S.email = saved.email;
  try { await api('/v1/admin/me'); start(); } catch { /* api() already showed the sign-in screen */ }
})();
