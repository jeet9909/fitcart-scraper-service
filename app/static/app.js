/* FitCart web app: marketing page, look builder, try-on, wardrobe and looks, wired to the FitCart API. */
'use strict';
const IMG = {
  before:'static/img/before.jpg', after:'static/img/after.jpg',
  shirt:'static/img/shirt.jpg', jeans:'static/img/jeans.jpg',
  sneakers:'static/img/sneakers.jpg', tee:'static/img/tee.jpg',
};
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');

const SLOTS = [
  {key:'top', label:'Top', add:'Top'},
  {key:'outerwear', label:'Layer', add:'Jacket or layer'},
  {key:'dress', label:'Dress', add:'Dress'},
  {key:'bottom', label:'Bottom wear', add:'Bottom wear'},
  {key:'footwear', label:'Footwear', add:'Footwear'},
  {key:'accessory', label:'Accessory', add:'Accessory'},
  {key:'jewelry', label:'Jewellery', add:'Jewellery'},
];
const SLOT_LABEL = Object.fromEntries(SLOTS.map(s => [s.key, s.label]));
const SLOT_PROMPT = {top:'top', outerwear:'jacket or layer', dress:'dress or one-piece', bottom:'bottom wear', footwear:'footwear', accessory:'accessory', jewelry:'jewelry'};
const MULTI = ['accessory','jewelry'];
const MAX_PIECES = 5;

/* Sample pieces for the demo look. Their photos are uploaded to the API as files. */
const CATALOG = {
  shirt:{id:'sample-shirt', slot:'top', name:'Relaxed linen shirt, sage green', store:'Sample', price:1899, mrp:2999, sizes:['XS','S','M','L','XL'], soldOut:['XS'], img:IMG.shirt, url:null, sample:true, color:'Sage green'},
  jeans:{id:'sample-jeans', slot:'bottom', name:'Straight-fit mid-rise jeans, indigo', store:'Sample', price:1499, mrp:2499, sizes:['26','28','30','32','34'], soldOut:['34'], img:IMG.jeans, url:null, sample:true, color:'Indigo'},
  sneakers:{id:'sample-sneakers', slot:'footwear', name:'Court low sneakers, white leather', store:'Sample', price:2499, mrp:3999, sizes:['UK 4','UK 5','UK 6','UK 7','UK 8'], soldOut:['UK 8'], img:IMG.sneakers, url:null, sample:true, color:'White'},
};

function readStore(key, fallback){ try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
function persist(key, value){ try { localStorage.setItem(key, JSON.stringify(value)); } catch {} }

const state = {
  view:'landing', billing:'monthly', balance:null, billingCfg:null, pending:null, checkingOut:null,
  look:[],
  wardrobe:null, wardrobeLoading:false, wardrobeError:'', wardrobeTab:'home', wardrobeFilter:'all', confirmDelete:null,
  occasion:null, ideas:[], ideasLoading:false, ideasError:'',
  gallery:null, galleryLoading:false, galleryError:'',
  photo:null, pose:'standard', consent:false,
  current:null, justGenerated:false,
  addSlot:null, addTab:'link', importing:false, importError:'', imported:null, draftSize:null,
  itemDraft:null,
  gen:null,
  account:readStore('fitcart-account', null), signin:null,
};

/* ---------- API ---------- */
const API_BASE = String(window.FITCART_API_BASE || '').replace(/\/+$/, '');
const apiUrl = path => `${API_BASE}${path}`;
const SESSION_KEY = 'fitcart-anonymous-session-v1';
function apiError(payload, fallback){
  const d = payload?.detail;
  if (typeof d === 'string') return d;
  if (d?.message) return d.message;
  if (Array.isArray(d) && d[0]?.msg) return d[0].msg;
  return fallback;
}
function loadSession(){
  const s = readStore(SESSION_KEY, null);
  if (!s?.access_token || !s?.expires_at || Date.parse(s.expires_at) <= Date.now() + 60000) return null;
  return s;
}
let sessionPromise = null;
async function session(force = false){
  if (!force){ const s = loadSession(); if (s) return s; }
  if (!sessionPromise){
    sessionPromise = fetch(apiUrl('/v1/sessions/anonymous'), {method:'POST'}).then(async r => {
      const payload = await r.json().catch(() => ({}));
      if (!r.ok) throw Error(apiError(payload, 'Could not start a private session.'));
      persist(SESSION_KEY, payload);
      setAccount(null);
      return payload;
    }).finally(() => { sessionPromise = null; });
  }
  return sessionPromise;
}
async function api(path, opts = {}, auth = true){
  let res;
  try {
    if (auth){
      let s = await session();
      res = await fetch(apiUrl(path), {...opts, headers:{...(opts.headers || {}), Authorization:`Bearer ${s.access_token}`}});
      if (res.status === 401){
        s = await session(true);
        res = await fetch(apiUrl(path), {...opts, headers:{...(opts.headers || {}), Authorization:`Bearer ${s.access_token}`}});
      }
    } else res = await fetch(apiUrl(path), opts);
  } catch (err){
    if (err.name === 'AbortError') throw err;
    throw Error('Could not reach FitCart. Check your connection and try again.');
  }
  const payload = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok){ const e = Error(apiError(payload, `Something went wrong (${res.status}). Please try again.`)); e.status = res.status; e.code = payload?.detail?.code; throw e; }
  return payload;
}
function dataUrlBlob(dataUrl){
  const [header, encoded] = dataUrl.split(',');
  const mime = header.match(/data:([^;]+)/)?.[1] || 'image/jpeg';
  const bytes = atob(encoded); const out = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i += 1) out[i] = bytes.charCodeAt(i);
  return new Blob([out], {type:mime});
}
const isRemote = src => /^https?:\/\//i.test(src || '');
async function toBlob(src){
  if (src.startsWith('data:')) return dataUrlBlob(src);
  const r = await fetch(src); if (!r.ok) throw Error('Could not read an image for the try-on.');
  return r.blob();
}
const LETTER_SIZES = ['XXXS','XXS','XS','S','M','L','XL','XXL','2XL','XXXL','3XL','4XL','5XL'];
function sizeOrder(label){
  const letter = LETTER_SIZES.indexOf(String(label).toUpperCase());
  if (letter >= 0) return letter;
  const n = parseFloat(String(label).replace(/^[A-Z ]+/i, ''));
  return Number.isFinite(n) ? 100 + n : 10000;
}
function fromScrape(d){
  const sizes = [...(d.sizes || []), ...(d.unavailable_sizes || [])].filter(Boolean);
  const slot = SLOT_LABEL[d.outfit_slot] ? d.outfit_slot : 'top';
  const host = (() => { try { return new URL(d.source_url).hostname.replace(/^www\./, ''); } catch { return ''; } })();
  return {
    id:'p-' + (d.external_id || d.source_url), slot, detected:SLOT_LABEL[d.outfit_slot] ? d.outfit_slot : null,
    name:d.title || 'Store product', brand:d.brand || '', store:storeName(d.store || host), price:d.price?.amount ?? null,
    mrp:d.original_price?.amount ?? null, sizes:[...new Set(sizes)].sort((a, b) => sizeOrder(a) - sizeOrder(b)),
    soldOut:d.unavailable_sizes || [], img:d.image_urls?.[0] || '', images:(d.image_urls || []).slice(0, 8),
    url:d.source_url, color:(d.colors || []).join(', '), source:'store', rating:d.rating, material:d.material,
  };
}
function storeName(host){
  const h = String(host || '').toLowerCase();
  const known = [['myntra','Myntra'],['amazon','Amazon'],['amzn','Amazon'],['ajio','AJIO'],['flipkart','Flipkart'],['fkrt','Flipkart'],['nike','Nike'],['meesho','Meesho'],['tatacliq','Tata CLiQ'],['nykaa','Nykaa Fashion']];
  return known.find(([k]) => h.includes(k))?.[1] || h.replace(/^www\./, '') || 'Store';
}
function fromWardrobe(w){
  return {id:'w-' + w.id, wardrobeId:w.id, slot:SLOT_LABEL[w.slot] ? w.slot : 'top', name:w.name, brand:w.brand || '', store:w.collection === 'store' ? (w.store || 'Saved') : '',
    price:w.price ?? null, mrp:null, sizes:w.sizes || [], soldOut:[], img:w.image_url, url:w.product_url, color:w.color || '', source:w.collection === 'store' ? 'store' : 'owned', collection:w.collection};
}

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr = n => '₹' + Number(n).toLocaleString('en-IN');
const off = i => i.mrp && i.mrp > i.price ? Math.round((i.mrp - i.price) / i.mrp * 100) : 0;
const short = i => i.name.split(',')[0];
const img = k => IMG[k] || k;
const ic = {
  plus:'<path d="M12 5v14M5 12h14"/>', x:'<path d="M6 6l12 12M18 6 6 18"/>', check:'<path d="m5 12.5 4.5 4.5L19 7"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1.2 1.2M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1.2-1.2"/>',
  arrow:'<path d="M5 12h14M13 6l6 6-6 6"/>', spark:'<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>',
  lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>', store:'<path d="M4 9 5.5 4h13L20 9M4 9v11h16V9M4 9h16M9 20v-6h6v6"/>',
  upload:'<path d="M12 16V4M7 9l5-5 5 5M4 16v4h16v-4"/>', share:'<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.6-4.4M8.2 13.2l7.6 4.4"/>',
  redo:'<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>', edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/>', bag:'<path d="M6 7h12l-1 13H7z"/><path d="M9 7a3 3 0 0 1 6 0"/>',
  up:'<path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 3 2l-1 5h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7"/>',
  down:'<path d="M17 13V4h3v9zM17 13l-4 8a2 2 0 0 1-3-2l1-5H6a2 2 0 0 1-2-2.3l1.2-6A2 2 0 0 1 7.2 4H17"/>',
  shield:'<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  camera:'<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>', sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/>',
  face:'<circle cx="12" cy="12" r="9"/><path d="M9 10h.01M15 10h.01M8.5 15a4.5 4.5 0 0 0 7 0"/>', body:'<circle cx="12" cy="4.5" r="2"/><path d="M12 7v8M8 10h8M12 15l-3 6M12 15l3 6"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>', alert:'<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', mail:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 7 8.5-7"/>',
  swap:'<path d="m9 6-6 6 6 6M15 6l6 6-6 6"/>', hanger:'<path d="M12 6a2 2 0 1 1 2 2c-1 0-2 .8-2 2v1"/><path d="M12 11 3 17.5a1 1 0 0 0 .6 1.8h16.8a1 1 0 0 0 .6-1.8z"/>',
};
const icon = (n, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ic[n]}</svg>`;
const privatePill = (text = 'Private to you') => `<span class="private">${icon('lock','s')}${text}</span>`;
const POSE_STD = '<svg class="fig" viewBox="0 0 44 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="22" cy="7" r="4.5"/><path d="M22 12v20M22 32l-5 20M22 32l5 20M22 16l-7 15M22 16l7 15"/></svg>';
const POSE_KEEP = '<svg class="fig" viewBox="0 0 44 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="20" cy="7" r="4.5"/><path d="M20 12l3 18M23 30l-8 22M23 30l9 21M21 16l11 3 3-9M21 17l-10 5 5 8"/></svg>';
const FIGURE = '<svg viewBox="0 0 120 220" aria-hidden="true"><ellipse cx="60" cy="30" rx="19" ry="23" fill="currentColor"/><path d="M60 58c-22 0-38 9-42 26l-8 64c-1 6 3 10 8 10h8l2 62h64l2-62h8c5 0 9-4 8-10l-8-64c-4-17-20-26-42-26z" fill="currentColor"/></svg>';
const EMPTY_ART = '<svg class="art" viewBox="0 0 148 112" fill="none" aria-hidden="true"><rect x="8" y="14" width="132" height="4" rx="2" fill="var(--line-strong)"/><path d="M40 18v10" stroke="var(--line-strong)" stroke-width="3" stroke-linecap="round"/><path d="M40 28 20 44h40z" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round" fill="var(--accent-soft)"/><path d="M26 44h28l4 50H22z" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round"/><path d="M76 18v10" stroke="var(--line-strong)" stroke-width="3" stroke-linecap="round"/><path d="M76 28 58 44h36z" stroke="var(--gold)" stroke-width="3" stroke-linejoin="round" fill="var(--gold-soft)"/><path d="M62 44h28v22l-4 32H78l-2-24-2 24h-8l-4-32z" fill="var(--gold-soft)" stroke="var(--gold)" stroke-width="3" stroke-linejoin="round"/><path d="M112 18v10" stroke="var(--line-strong)" stroke-width="3" stroke-linecap="round"/><rect x="99" y="30" width="26" height="30" rx="8" stroke="var(--line-strong)" stroke-width="3" stroke-dasharray="5 5"/><path d="M112 39v12M106 45h12" stroke="var(--muted)" stroke-width="3" stroke-linecap="round"/></svg>';

/* ---------- Toast ---------- */
let toastTimer, toastAction = null;
function toast(msg, opts = {}){
  const kind = opts.kind || 'success';
  const host = $('#toastHost');
  clearTimeout(toastTimer);
  toastAction = opts.action || null;
  host.innerHTML = `<div class="toast ${kind}"><span class="ti">${icon(kind === 'error' ? 'alert' : kind === 'info' ? 'info' : 'check','s')}</span><span class="tx">${esc(msg)}</span>${toastAction ? `<button class="ta" data-act="toast-action">${esc(toastAction.label)}</button>` : ''}</div>`;
  toastTimer = setTimeout(dismissToast, toastAction ? 5200 : 3200);
}
function dismissToast(){
  const t = $('#toastHost .toast');
  if (!t) return;
  if (REDUCED.matches){ t.remove(); return; }
  t.classList.add('leaving');
  t.addEventListener('animationend', () => t.remove(), {once:true});
}
function announce(text){ const a = $('#announcer'); a.textContent = ''; requestAnimationFrame(() => a.textContent = text); }

/* ---------- Navigation ---------- */
function go(view){
  if (state.view === 'generating' && view !== 'result') stopGeneration();
  state.view = view;
  render(true);
  window.scrollTo({top:0, behavior:'instant'});
  $('#view').focus({preventScroll:true});
}
function lookTotal(){ return state.look.reduce((sum, p) => sum + (p.item.price || 0), 0); }
function lookStores(){ return new Set(state.look.map(p => p.item.store).filter(Boolean)); }
function inLook(id){ return state.look.some(p => p.item.id === id); }
function addToLook(item, size = null){
  const clash = other => !MULTI.includes(item.slot) && other.slot === item.slot;
  state.look = state.look.filter(p => !clash(p.item));
  if (state.look.length >= MAX_PIECES) state.look.shift();
  state.look.push({item, size});
  const c = $('#lookCount'); c.classList.remove('bump'); void c.offsetWidth; c.classList.add('bump');
}

function render(enter = false){
  document.querySelectorAll('.toptabs [data-view], .tabbar [data-view]').forEach(b => {
    const v = b.dataset.view;
    const current = v === state.view || (v === 'home' && ['builder','generating','result'].includes(state.view));
    b.setAttribute('aria-current', current ? 'page' : 'false');
  });
  const n = state.look.length;
  $('#lookCount').textContent = n;
  $('#tabCount').textContent = n; $('#tabCount').hidden = !n;
  const acct = $('#accountBtn');
  acct.classList.toggle('signed', Boolean(state.account));
  acct.setAttribute('aria-label', state.account ? `Account: ${state.account.email}` : 'Sign in');
  const views = {landing, pricing:pricingPage, home, builder, generating, result, wardrobe, looks};
  const view = $('#view');
  view.innerHTML = views[state.view]();
  if (enter && !REDUCED.matches){ view.classList.remove('view-enter'); void view.offsetWidth; view.classList.add('view-enter'); }
  bindView();
  setupReveal();
}

/* ---------- Compare ---------- */
function compareHtml(before, after, labelL = 'You', labelR = 'New look', nudge = false){
  return `<div class="compare${nudge ? ' nudge' : ''}"><img src="${img(before)}" alt="Original photo"><div class="after-wrap"><img src="${img(after)}" alt="Try-on result wearing the outfit"></div><input type="range" min="0" max="100" step="1" value="50" aria-label="Compare your photo with the new look" aria-valuetext="Half and half"><div class="divider"></div><div class="knob" aria-hidden="true">${icon('swap')}</div><span class="lbl l">${labelL}</span><span class="lbl r">${labelR}</span></div>`;
}
function bindCompare(el){
  const r = el.querySelector('input[type=range]');
  let lastZone = 1;
  const set = () => {
    el.classList.remove('nudge');
    el.style.setProperty('--pos', r.value + '%');
    const v = Number(r.value);
    r.setAttribute('aria-valuetext', v <= 2 ? 'Showing the new look' : v >= 98 ? 'Showing your photo' : `${100 - v}% new look`);
    el.querySelector('.lbl.l').style.opacity = v < 18 ? 0 : 1;
    el.querySelector('.lbl.r').style.opacity = v > 82 ? 0 : 1;
    const zone = v <= 2 ? 0 : v >= 98 ? 2 : 1;
    if (zone !== lastZone && navigator.vibrate){ try { navigator.vibrate(8); } catch {} }
    lastZone = zone;
  };
  r.addEventListener('input', set);
  r.addEventListener('pointerdown', () => el.classList.add('dragging'));
  ['pointerup','pointercancel','blur'].forEach(ev => r.addEventListener(ev, () => el.classList.remove('dragging')));
  el.addEventListener('animationend', () => el.classList.remove('nudge'));
}


/* ---------- Plans (prices include 18% GST) ---------- */
const PLANS = [
  {key:'free', name:'Free', for:'Try FitCart on your next outfit.', monthly:0, yearly:0, looks:3, quality:'Standard', imports:15, stylist:1, wardrobe:25, cta:'Start free',
   perks:['3 looks a month, any mix of stores','Standard quality','Wardrobe up to 25 items','1 AI stylist idea'], missing:['HD looks','Full Looks history']},
  {key:'pass', name:'Occasion Pass', for:'One-time pack for a wedding, festival or trip.', once:129, days:7, looks:10, quality:'HD', imports:30, stylist:5, wardrobe:25, cta:'Buy the pass',
   perks:['10 HD looks for 7 days','No autopay, pay once with UPI','5 AI stylist ideas','Stacks on any plan'], missing:[]},
  {key:'plus', name:'Plus', for:'For people who shop online every month.', monthly:349, yearly:3299, looks:25, quality:'HD', imports:150, stylist:20, wardrobe:200, cta:'Get Plus', popular:true,
   perks:['25 HD looks every month','Standard pose, head to toe','Wardrobe up to 200 items','20 AI stylist ideas','Full Looks history and HD downloads'], missing:[]},
  {key:'pro', name:'Pro', for:'For stylists, creators and big wardrobes.', monthly:799, yearly:7499, looks:60, quality:'HD', imports:300, stylist:40, wardrobe:1000, cta:'Get Pro',
   perks:['60 HD looks every month','Priority generation','Wardrobe up to 1,000 items','40 AI stylist ideas','Early access to Fit score'], missing:[]},
];
function planPrice(p){
  if (p.once) return {amt:p.once, unit:'one-time', per:p.once / p.looks, was:null};
  if (!p.monthly) return {amt:0, unit:'forever', per:null, was:null};
  if (state.billing === 'yearly') return {amt:Math.round(p.yearly / 12), unit:'/mo, billed ₹' + p.yearly.toLocaleString('en-IN') + ' yearly', per:p.yearly / 12 / p.looks, was:p.monthly};
  return {amt:p.monthly, unit:'/month', per:p.monthly / p.looks, was:null};
}
function pricingHtml(){
  const tiers = PLANS.map(p => {
    const pr = planPrice(p);
    const current = Boolean(state.account) && !unlimited() && (state.balance?.plan || 'free') === p.key;
    return `<article class="tier${p.popular ? ' pop' : ''} reveal">${p.popular ? '<span class="ribbon">Most popular</span>' : ''}
      <div><h3>${p.name}</h3><p class="for">${p.for}</p></div>
      <div><div class="amt"><b>₹${pr.amt.toLocaleString('en-IN')}</b><span>${pr.unit}</span></div>
        <p style="display:flex;gap:8px;flex-wrap:wrap;min-height:20px">${pr.was ? `<span class="was">₹${pr.was}/mo</span>` : ''}${pr.per ? `<span class="per num">₹${pr.per.toFixed(1)} per look</span>` : '<span class="per">No card needed</span>'}</p></div>
      <button class="btn ${p.popular ? 'brand' : p.key === 'free' ? 'glassy' : ''} wide" data-act="choose-plan" data-plan="${p.key}" ${(current && p.key !== 'pass') || state.checkingOut ? 'disabled' : ''}>${state.checkingOut === p.key ? '<span class="spin" aria-hidden="true"></span> Opening checkout…' : current && p.key !== 'pass' ? 'Your current plan' : p.cta}</button>
      <ul>${p.perks.map(x => `<li>${icon('check','s')}${esc(x)}</li>`).join('')}${p.missing.map(x => `<li class="no">${icon('x','s')}${esc(x)}</li>`).join('')}</ul>
    </article>`;
  }).join('');
  const rows = [
    ['Looks (1 look = whole outfit, up to 5 pieces)', ...PLANS.map(p => p.once ? `${p.looks} in 7 days` : `${p.looks} / month`)],
    ['Quality', ...PLANS.map(p => p.quality)],
    ['Automatic face check', 'Coming soon', 'Coming soon', 'Coming soon', 'Coming soon'],
    ['Product imports from store links', ...PLANS.map(p => p.imports)],
    ['AI stylist ideas', ...PLANS.map(p => p.stylist)],
    ['Wardrobe items', ...PLANS.map(p => p.wardrobe.toLocaleString('en-IN'))],
    ['Looks history', 'Last 5', 'Last 5', 'Unlimited', 'Unlimited'],
    ['Priority generation', '—', '—', '—', '✓'],
    ['Fit score (coming soon)', '—', '—', 'Included', 'Early access'],
  ];
  return `<div class="pricing">
    <div class="billing glass" role="group" aria-label="Billing period"><button data-act="billing" data-billing="monthly" aria-pressed="${state.billing === 'monthly'}">Monthly</button><button data-act="billing" data-billing="yearly" aria-pressed="${state.billing === 'yearly'}">Yearly <span class="save">Save 21%</span></button></div>
    <div class="tiers">${tiers}</div>
    <div class="plan-table reveal"><table><caption class="sr">Compare plans</caption><thead><tr><th scope="col">Compare plans</th>${PLANS.map(p => `<th scope="col">${p.name}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr><th scope="row">${r[0]}</th>${r.slice(1).map((c, i) => `<td class="${PLANS[i].popular ? 'hi' : ''}">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    ${state.billingCfg?.test_mode ? `<div class="notice test-mode" role="note">${icon('info')}<span><strong>Test mode.</strong> No real money moves. Pay with UPI ID <b>success@razorpay</b>, or pick Netbanking, any bank, then Success.</span></div>` : ''}
    <div class="plan-notes"><p>${icon('lock','s')} Prices include 18% GST. Secure checkout by Razorpay: UPI, cards and netbanking. Plans renew with UPI AutoPay or card until cancelled.</p><p>A look that fails is never counted. Unused looks don't carry over to the next month.</p></div>
  </div>`;
}
function pricingPage(){
  return `<section style="display:grid;gap:22px;padding-top:8px">
    <div class="lp-head"><p class="eyebrow">Plans</p><h1 style="font:400 clamp(32px,7vw,52px)/1 var(--display);letter-spacing:-.015em">Pick how you <span class="grad-text">try it on</span></h1><p>Every look is a whole outfit from up to five stores, in one image.</p></div>
    ${pricingHtml()}
  </section>`;
}

/* ---------- Marketing page ---------- */
function landing(){
  const fl = (cls, key, store, price) => { const i = CATALOG[key]; return `<div class="floater glass ${cls}"><img src="${img(i.img)}" alt=""><div><small>${store}</small>${esc(short(i))}<br><span class="p num">${inr(price)}</span></div></div>`; };
  return `<div class="landing">
  <section class="lp-hero">
    <div class="aurora" aria-hidden="true"><i></i><i></i><i></i></div>
    <div class="lp-grid">
      <div class="lp-copy">
        <span class="pill-new glass"><b>New</b> Multi-store outfit try-on</span>
        <h1>Wear it<br><span class="grad-text">before you buy it.</span></h1>
        <p class="lede">Paste links from Myntra, Amazon, AJIO and more. FitCart puts the whole outfit on you in seconds, then sends you to each store to buy.</p>
        <div class="lp-ctas"><button class="btn brand big" data-act="go" data-view="home">${icon('spark')} Try it free</button><button class="btn glassy big" data-act="scroll" data-target="lpPricing">See plans</button></div>
        <div class="lp-trust"><span>${icon('check','s')} 3 free looks a month</span><span>${icon('lock','s')} Your photo stays private</span><span>${icon('body','s')} Head-to-toe standard pose</span></div>
      </div>
      <div class="stage">
        <div class="device glass">${compareHtml('before','after','You','New look', true)}</div>
        ${fl('f1','shirt','Myntra',1899)}${fl('f2','jeans','Amazon',1499)}${fl('f3','sneakers','AJIO',2499)}
        <div class="floater glass f-look"><div><small>Your look · 3 stores</small><b class="num">${inr(5897)}</b></div></div>
      </div>
    </div>
  </section>
  <section class="marquee" aria-label="Works with these stores"><div class="marquee-track" aria-hidden="true">${Array(2).fill(['Myntra','Amazon','AJIO','Flipkart','Nike','Tata CLiQ','Nykaa Fashion','Meesho'].map(s => `<span>${s}</span>`).join('')).join('')}</div><p class="sr">Works with Myntra, Amazon, AJIO, Flipkart, Nike and more.</p></section>
  <section class="lp-section">
    <div class="lp-head reveal"><p class="eyebrow">How it works</p><h2>From three tabs to <span class="grad-text">one outfit.</span></h2></div>
    <ol class="flow">
      <li class="glass reveal"><h3>Paste your finds</h3><p class="muted">A shirt from Myntra, jeans from Amazon, shoes from AJIO. We fetch the real price, sizes and photos.</p></li>
      <li class="glass reveal"><h3>Add one photo</h3><p class="muted">Any pose. We show you standing straight, head to toe, and check the face is still yours.</p></li>
      <li class="glass reveal"><h3>See it, then buy</h3><p class="muted">Compare before and after, save the look, and open each piece at its own store.</p></li>
    </ol>
  </section>
  <section class="lp-section">
    <div class="lp-head reveal"><p class="eyebrow">Why FitCart</p><h2>A fitting room for <span class="grad-text">the whole internet.</span></h2></div>
    <div class="bento">
      <article class="hero-card wide reveal"><span class="ico">${icon('bag')}</span><h3>One outfit, many stores</h3><p>Mix up to five pieces from different shops and see them together in one image, priced as one look.</p><div class="stack"><img src="${img('shirt')}" alt=""><img src="${img('jeans')}" alt=""><img src="${img('sneakers')}" alt=""></div></article>
      <article class="glass reveal"><span class="ico t">${icon('face')}</span><h3>Built to keep you, you</h3><p>The AI is told to keep your face, hair, skin tone and body exactly as they are. Automatic face checks are coming next.</p></article>
      <article class="glass reveal"><span class="ico">${icon('body')}</span><h3>Standard pose</h3><p>Upload any photo. See yourself upright, arms relaxed, the whole outfit clearly visible.</p></article>
      <article class="glass reveal"><span class="ico g">${icon('spark')}</span><h3>AI stylist</h3><p>Office, wedding, weekend. Get outfits built from clothes you own and pieces you saved.</p></article>
      <article class="glass reveal"><span class="ico t">${icon('hanger')}</span><h3>Your wardrobe, online</h3><p>Snap what's in your cupboard once. Mix it with new finds before you spend.</p></article>
      <article class="glass wide reveal"><span class="soon">Coming soon</span><span class="ico g">${icon('check')}</span><h3>Fit score</h3><p>Your measurements against each store's real size chart, with a plain answer: which size, and where it might feel tight or long.</p></article>
    </div>
  </section>
  <section class="lp-section showcase">
    <div class="reveal">${compareHtml('before','after','Before','After')}</div>
    <div class="lp-head reveal" style="gap:16px"><p class="eyebrow">See the difference</p><h2>Drag. <span class="grad-text">Decide.</span> Done.</h2><ul class="checks"><li>${icon('check')}Real product photos from the store, not lookalikes</li><li>${icon('check')}Sizes in stock, crossed out when sold out</li><li>${icon('check')}Saved privately to your Looks</li></ul><button class="btn brand" data-act="go" data-view="home" style="justify-self:start">${icon('spark','s')} Build my look</button></div>
  </section>
  <section class="lp-section" id="lpPricing">
    <div class="lp-head reveal"><p class="eyebrow">Pricing</p><h2>Premium looks. <span class="grad-text">Fair price.</span></h2><p>Start free. Buy a pass for a big occasion, or subscribe if you shop every month.</p></div>
    ${pricingHtml()}
  </section>
  <section class="lp-section">
    <div class="lp-head reveal"><p class="eyebrow">Questions</p><h2>Good to know</h2></div>
    <div class="faq">
      <details class="glass reveal"><summary>Is my photo private?</summary><p>Yes. Your photo and looks are saved only to your private gallery and are never shown publicly. Links to your images expire after an hour.</p></details>
      <details class="glass reveal"><summary>What counts as one look?</summary><p>One generated image of you. It can include up to five pieces from any mix of stores, and it still counts as one look.</p></details>
      <details class="glass reveal"><summary>Will my face change?</summary><p>The AI is instructed to keep your face and body exactly as they are. For the closest likeness, use a clear, front-facing photo in good light, or choose Keep my pose. A look that fails is never counted.</p></details>
      <details class="glass reveal"><summary>Which stores work?</summary><p>Myntra, Amazon, AJIO, Flipkart and Nike links work today. For any other store, add the item with a photo.</p></details>
      <details class="glass reveal"><summary>Can I cancel anytime?</summary><p>Yes. Cancel from your account and you keep your plan until the end of the period. The Occasion Pass never renews.</p></details>
    </div>
  </section>
  <section class="cta-band reveal"><h2>Your next outfit is three links away.</h2><p style="opacity:.92;max-width:44ch">Try FitCart free. No card needed. Sign in with your email for 3 free looks every month.</p><button class="btn big" data-act="go" data-view="home">${icon('spark')} Try it free</button></section>
  <footer class="lp-foot"><span>© 2026 FitCart · See the look. Choose the fit.</span><span>Prices include GST · Made in India</span></footer>
</div>`;
}

let revealObserver;
function setupReveal(){
  if (REDUCED.matches || !('IntersectionObserver' in window)) return;
  document.body.classList.add('js-reveal');
  revealObserver?.disconnect();
  revealObserver = new IntersectionObserver(entries => entries.forEach(e => { if (e.isIntersecting){ e.target.classList.add('in'); revealObserver.unobserve(e.target); } }), {rootMargin:'0px 0px -8% 0px', threshold:.08});
  document.querySelectorAll('.reveal:not(.in)').forEach(el => revealObserver.observe(el));
}
const unlimited = () => Boolean(state.account?.unlimited);
function planChip(){
  if (unlimited()) return `<button class="plan-chip" data-act="account">${icon('spark','s')} Unlimited looks · ${esc(state.account.email)}</button>`;
  const free = state.balance?.free_looks_per_month ?? 3;
  if (!state.account) return `<button class="plan-chip" data-act="account" data-reason="free">${icon('spark','s')} Sign in for ${free} free looks a month</button>`;
  const left = looksLeft();
  if (left === null) return `<button class="plan-chip" data-act="go" data-view="pricing">${icon('spark','s')} Checking your looks…</button>`;
  if (left === Infinity) return `<button class="plan-chip" data-act="go" data-view="pricing">${icon('spark','s')} Looks available</button>`;
  const p = PLANS.find(x => x.key === (state.balance.plan || 'free'));
  return `<button class="plan-chip" data-act="go" data-view="pricing">${icon('spark','s')} ${esc(p.name)} · ${left} ${left === 1 ? 'look' : 'looks'} left</button>`;
}

/* ---------- Home ---------- */
function home(){
  return `<section class="hero">
    <div class="hero-copy">
      <p class="eyebrow">Your AI fitting room</p>
      <h1>See the whole outfit on you <em>before you buy it.</em></h1>
      <p class="lede">Paste links from any store. Mix a top from Myntra with jeans from Amazon and shoes from AJIO, then see yourself wearing all of it.</p>
      <form class="linkbox" id="linkForm" novalidate>
        <label for="homeLink" class="small" style="font-weight:800">Product link</label>
        <div class="linkrow">
          <span style="display:grid;place-items:center;padding-left:8px;color:var(--muted)">${icon('link')}</span>
          <input id="homeLink" type="url" inputmode="url" autocomplete="off" placeholder="https://www.myntra.com/…" aria-describedby="homeLinkHelp homeError">
          <button class="btn" type="submit" id="homeSubmit">Add</button>
        </div>
        <p class="error" id="homeError" role="alert"></p>
        <p id="homeLinkHelp" class="stores">Works with <b>Myntra</b><b>Amazon</b><b>AJIO</b><b>Flipkart</b><b>Nike</b></p>
      </form>
      <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">
        <button class="chip" data-act="demo">${icon('spark','s')} Try a 3-store sample look</button>
        <button class="chip" data-act="go" data-view="wardrobe">${icon('hanger','s')} Start from my wardrobe</button>
      </div>
      <div class="trust"><span>${icon('lock','s')} Your look is private</span><span>${icon('shield','s')} 3 free looks a month</span><span>${icon('store','s')} Live prices from each store</span></div>
    </div>
    <div class="hero-visual">${compareHtml('before','after','Before','After', true)}<p class="tiny muted" style="margin-top:8px">Drag the handle to compare. Sample result.</p></div>
  </section>
  <div class="section-title"><h2>How it works</h2></div>
  <ol class="steps3">
    <li><div><strong>Build your look</strong><span class="small muted">Paste product links or pick clothes you already own. Up to 5 pieces from any mix of stores.</span></div></li>
    <li><div><strong>Add one photo</strong><span class="small muted">Any pose works. We show you standing straight so the whole outfit is visible.</span></div></li>
    <li><div><strong>See it, then buy each piece</strong><span class="small muted">Compare before and after, save the look, and open each item at its own store.</span></div></li>
  </ol>`;
}

/* ---------- Builder ---------- */
function sizesHtml(item, selected, act, index){
  if (!item.sizes?.length) return item.source === 'owned' ? '<p class="tiny muted">From your wardrobe</p>' : '';
  const out = item.soldOut || [];
  const buttons = item.sizes.map(s => {
    const sold = out.includes(s);
    return `<button class="size" data-act="${act}" ${index != null ? `data-index="${index}"` : ''} data-size="${esc(s)}" aria-pressed="${selected === s}" ${sold ? `disabled aria-label="${esc(s)}, sold out"` : `aria-label="Size ${esc(s)}"`}>${esc(s)}</button>`;
  }).join('');
  const note = [
    selected ? `<span class="picked">${icon('check','s')} Size ${esc(selected)}</span>` : `<span class="need">${icon('info','s')} Pick your size to buy</span>`,
    out.length ? `<span>Crossed out = sold out</span>` : '',
  ].join('');
  return `<div class="sizes" role="group" aria-label="Size for ${esc(item.name)}">${buttons}</div><p class="size-note">${note}</p>`;
}
function slotCard(p, index){
  const i = p.item, pct = off(i);
  const save = i.source === 'store' && !i.sample ? (i.wardrobeId ? `<span class="tag ok">${icon('check','s')} Saved</span>` : `<button class="chip mini" data-act="save-piece" data-index="${index}" ${i.saving ? 'disabled' : ''}>${icon('hanger','s')} ${i.saving ? 'Saving…' : 'Save'}</button>`) : '';
  const view = i.url && !i.sample ? `<a class="link tiny" href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">View at ${esc(i.store || 'store')}</a>` : '';
  return `<article class="slot" aria-label="${esc(SLOT_LABEL[i.slot])}: ${esc(i.name)}">
    <div class="thumb"><img src="${esc(img(i.img))}" alt=""></div>
    <div class="slot-body">
      <div class="slot-top"><span style="display:flex;gap:6px;align-items:center;min-width:0"><select class="slot-cat" data-change="piece-slot" data-index="${index}" aria-label="Category for ${esc(i.name)}">${SLOTS.map(s => `<option value="${s.key}" ${s.key === i.slot ? 'selected' : ''}>${s.label}</option>`).join('')}</select><span class="tiny muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(i.sample ? 'Sample' : i.source === 'owned' ? 'My clothes' : i.store || '')}</span></span><button class="iconbtn" data-act="remove" data-index="${index}" aria-label="Remove ${esc(i.name)}">${icon('x','s')}</button></div>
      <p class="slot-name">${esc(i.name)}</p>
      ${i.price ? `<div class="pricerow"><span class="price">${inr(i.price)}</span>${i.mrp ? `<span class="mrp">${inr(i.mrp)}</span>` : ''}${pct ? `<span class="off tiny">${pct}% off</span>` : ''}</div>` : ''}
      ${sizesHtml(i, p.size, 'size', index)}
      ${view || save ? `<div class="slot-links">${view}${save}</div>` : ''}
    </div>
  </article>`;
}
function openSlots(){
  if (state.look.length >= MAX_PIECES) return [];
  const taken = state.look.map(p => p.item.slot);
  return SLOTS.filter(s => MULTI.includes(s.key) ? taken.filter(t => t === s.key).length < 2 : !taken.includes(s.key));
}
function poseHtml(prefix){
  return `<fieldset style="border:0;padding:0;margin:0;display:grid;gap:8px"><legend class="small" style="font-weight:800;margin-bottom:8px">Pose</legend><div class="poses">
    <label class="pose"><input type="radio" name="pose-${prefix}" value="standard" data-act="pose" ${state.pose === 'standard' ? 'checked' : ''}><span class="check">${icon('check','s')}</span>${POSE_STD}<strong>Standard pose</strong><span class="d">Straight, head to toe. Whole outfit visible.</span></label>
    <label class="pose"><input type="radio" name="pose-${prefix}" value="keep" data-act="pose" ${state.pose === 'keep' ? 'checked' : ''}><span class="check">${icon('check','s')}</span>${POSE_KEEP}<strong>Keep my pose</strong><span class="d">Your own pose and background.</span></label>
  </div></fieldset>`;
}
function consentHtml(){ return `<label class="consent"><input type="checkbox" data-act="consent" ${state.consent ? 'checked' : ''}>I have permission to use this photo. It is used only to create my try-on.</label>`; }
function canGenerate(){ return state.look.length > 0 && Boolean(state.photo) && state.consent; }
function personHtml(){
  if (!state.photo) return `<button class="photo-drop" data-act="pick-photo" data-drop="photo">
      <span class="photo-drop-art" aria-hidden="true">${icon('body')}</span>
      <span class="photo-drop-copy"><strong>Upload your photo</strong><span class="small muted">A full-body photo, face clearly visible, in good light. JPG or PNG.</span></span>
      <span class="btn brand small" aria-hidden="true">${icon('upload','s')} Choose photo</span>
    </button>`;
  return `<div class="person"><img src="${state.photo}" alt="Your photo"><div style="display:grid;gap:6px;justify-items:start"><strong>Your photo</strong>${privatePill()}<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn ghost small" data-act="pick-photo">${icon('upload','s')} Change photo</button><button class="link small" data-act="remove-photo">Remove</button></div></div></div>`;
}
function generateHint(){
  if (!state.look.length) return 'Add at least one piece to your look.';
  if (!state.photo) return 'Upload your photo to continue.';
  return state.consent ? 'About 15 to 30 seconds.' : 'Tick the permission box to continue.';
}
function tryPanel(){
  const stores = lookStores();
  return `<div class="card panel">
    <div class="panel-head"><h2>Try it on</h2>${privatePill()}</div>
    ${planChip()}
    ${personHtml()}
    ${poseHtml('side')}
    ${state.look.length ? `<div class="panel-total"><span class="small muted num">${state.look.length} ${state.look.length === 1 ? 'piece' : 'pieces'} · ${stores.size} ${stores.size === 1 ? 'store' : 'stores'}</span><span class="total">${inr(lookTotal())}</span></div>` : ''}
    ${consentHtml()}
    <button class="btn brand wide" data-act="generate" ${canGenerate() ? '' : 'disabled'}>${icon('spark')} Generate my look</button>
    <p class="tiny muted gen-hint">${generateHint()}</p>
  </div>`;
}
function builder(){
  const stores = lookStores();
  const open = openSlots();
  const n = state.look.length;
  const body = n ? `<div class="slots${n >= 3 ? ' compact' : ''}">${state.look.map(slotCard).join('')}</div>
      ${open.length ? `<div class="more"><p class="small" style="font-weight:800">Complete the look <span class="muted" style="font-weight:600">· ${MAX_PIECES - n} more ${MAX_PIECES - n === 1 ? 'piece' : 'pieces'} allowed</span></p><div class="chips">${open.map(s => `<button class="chip add" data-act="add" data-slot="${s.key}">${icon('plus','s')} ${s.add}</button>`).join('')}</div></div>` : ''}
      <div class="summary"><div><div class="total">${inr(lookTotal())}</div><div class="meta num">${n} ${n === 1 ? 'piece' : 'pieces'} · ${stores.size} ${stores.size === 1 ? 'store' : 'stores'}</div></div><button class="btn brand" data-act="open-photo">${icon('spark')} Try it on</button></div>`
    : `<div class="empty">${EMPTY_ART}<h2>Start with one piece you love</h2><p class="muted">Paste a product link from any store. We'll bring in the price, sizes and photos, then you can add the rest of the outfit.</p><div class="row"><button class="btn" data-act="add" data-slot="top">${icon('link','s')} Paste a link</button><button class="btn ghost" data-act="demo">${icon('spark','s')} Try a sample look</button></div><button class="link small" data-act="go" data-view="wardrobe">Or pick from your wardrobe</button></div>`;
  return `<section class="builder">
    <div class="page-head"><div><p class="eyebrow">Look builder</p><h1>Your look</h1></div>${n ? `<p class="small muted num">${n} of ${MAX_PIECES} pieces</p>` : ''}</div>
    <div style="display:grid;gap:14px;align-content:start">${body}</div>
    <aside class="side" aria-label="Try it on">${tryPanel()}</aside>
  </section>`;
}

/* ---------- Generating ---------- */
function orderedLook(){
  const order = SLOTS.map(s => s.key);
  return [...state.look].sort((a, b) => order.indexOf(a.item.slot) - order.indexOf(b.item.slot));
}
function buildSteps(){
  return [
    {label:'Checking your photo', ms:2500},
    ...orderedLook().map(p => ({label:`Fitting ${short(p.item).toLowerCase()}`, ms:3500})),
    {label: state.pose === 'standard' ? 'Standing you straight, head to toe' : 'Matching your own pose', ms:4000},
    {label:'Final touches', ms:0},
  ];
}
function generating(){
  const g = state.gen;
  const confetti = Array.from({length:18}, (_, i) => {
    const a = (i / 18) * Math.PI * 2, d = 110 + (i % 3) * 40;
    const colors = ['var(--accent)','var(--gold)','var(--teal)','var(--accent-line)'];
    return `<i style="--x:${Math.round(Math.cos(a) * d)}px;--y:${Math.round(Math.sin(a) * d)}px;--r:${(i * 47) % 360}deg;--c:${colors[i % 4]};--d:${(i % 6) * 25}ms"></i>`;
  }).join('');
  if (g.error){
    return `<section class="gen" aria-labelledby="genTitle">
      <div class="gen-stage"><div class="gen-visual" aria-hidden="true"><div class="layer l-photo"><img src="${state.photo || ''}" alt=""></div></div></div>
      <div class="gen-side">
        <h1 class="gen-title" id="genTitle">We couldn't create your look</h1>
        <div class="notice" role="alert">${icon('alert')}<span>${esc(g.error)}</span></div>
        <p class="small muted">Your look is saved and no look was used from your plan.</p>
        <div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn brand" data-act="generate">${icon('redo','s')} Try again</button><button class="btn ghost" data-act="go" data-view="builder">Back to my look</button></div>
      </div>
    </section>`;
  }
  return `<section class="gen" aria-labelledby="genTitle">
    <div class="gen-stage">
      <div class="gen-visual p1" id="genVisual" aria-hidden="true">
        <div class="layer l-photo"><img src="${state.photo || ''}" alt=""></div>
        <div class="layer l-aura"><i></i><i></i><i></i></div>
        <div class="layer l-glass">${FIGURE}</div>
        <div class="layer l-soft"><img id="genSoft" alt=""></div>
        <div class="layer l-sharp"><img id="genSharp" alt=""></div>
        <div class="layer l-scan"></div>
        <div class="confetti">${confetti}</div>
      </div>
      ${privatePill('Your photo stays private')}
    </div>
    <div class="gen-side">
      <h1 class="gen-title" id="genTitle">Creating your look</h1>
      <ol class="stages" id="genStages">${g.steps.map((s, i) => `<li data-i="${i}"><span class="dot"></span><span>${esc(s.label)}</span></li>`).join('')}</ol>
      <div class="gen-foot">
        <div class="progress" role="progressbar" aria-label="Try-on progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="genBar"><i></i></div>
        <div class="gen-foot-row"><span class="small muted">Usually 15–30 seconds</span><button class="btn ghost small" data-act="cancel">Cancel</button></div>
      </div>
    </div>
  </section>`;
}
async function photoBlob(){
  if (!state.photo) throw Error('Upload your photo first.');
  return dataUrlBlob(state.photo);
}
async function requestTryOn(signal){
  const pieces = orderedLook();
  const form = new FormData();
  form.append('person_image', await photoBlob(), 'person.jpg');
  form.append('pose', state.pose === 'keep' ? 'keep' : 'standard');
  const main = pieces[0].item;
  form.append('category', SLOT_PROMPT[main.slot] || 'clothing');
  form.append('product_name', [main.color, main.name].filter(Boolean).join(' ').slice(0, 200));
  if (isRemote(main.img)){
    form.append('product_image_url', main.img);
    if (main.url) form.append('product_page_url', main.url);
  } else form.append('product_image', await toBlob(main.img), 'product.jpg');
  const extras = []; let uploads = 0;
  for (const p of pieces.slice(1)){
    const i = p.item;
    const entry = {slot:i.slot, name:[i.color, i.name].filter(Boolean).join(' ').slice(0, 200), store:i.sample ? null : (i.store || null), price:i.price ?? null, size:p.size || null, page_url:i.url || null};
    if (isRemote(i.img)) entry.image_url = i.img;
    else { form.append('outfit_images', await toBlob(i.img), `piece-${uploads}.jpg`); entry.upload = uploads; uploads += 1; }
    extras.push(entry);
  }
  if (extras.length) form.append('outfit_items', JSON.stringify(extras));
  return api('/v1/try-ons', {method:'POST', body:form, signal});
}
function startGeneration(){
  if (!canGenerate()) return;
  if (!state.account){ closeSheet($('#photoSheet')); state.pending = {generate:true}; openSignin('free'); return; }
  if (looksLeft() === 0){ outOfLooks(); return; }
  closeSheet($('#photoSheet'));
  stopGeneration();
  const steps = buildSteps();
  const g = state.gen = {steps, at:0, total:steps.reduce((s, x) => s + x.ms, 0), done:0, timers:[], controller:new AbortController(), result:null, error:null};
  state.view = 'generating';
  render(true);
  window.scrollTo({top:0, behavior:'instant'});
  $('#view').focus({preventScroll:true});
  requestAnimationFrame(runStep);
  requestTryOn(g.controller.signal).then(async item => {
    if (state.gen !== g) return;
    const pic = new Image(); pic.src = item.result_image_url;
    try { await pic.decode(); } catch {}
    if (state.gen !== g) return;
    g.result = item;
    if (g.at >= g.steps.length - 1){ g.timers.forEach(clearTimeout); g.at = g.steps.length; finishGeneration(); }
  }).catch(err => {
    if (err.name === 'AbortError' || state.gen !== g) return;
    if (err.code === 'sign_in_required' || err.code === 'no_looks_left'){
      stopGeneration(); go('builder');
      if (err.code === 'no_looks_left'){ loadBalance(); outOfLooks(); }
      else { setAccount(null); state.pending = {generate:true}; openSignin('free'); }
      return;
    }
    g.timers.forEach(clearTimeout);
    g.error = err.message || 'Something went wrong. Please try again.';
    render();
    announce('We could not create your look. ' + g.error);
  });
}
function later(fn, ms){ const t = setTimeout(fn, ms); state.gen?.timers.push(t); return t; }
function paintGen(){
  const g = state.gen; if (!g) return;
  document.querySelectorAll('#genStages li').forEach(li => {
    const i = Number(li.dataset.i);
    li.className = i < g.at ? 'done' : i === g.at ? 'now' : '';
    li.querySelector('.dot').innerHTML = i < g.at ? icon('check','s') : '';
    if (i === g.at) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
  });
  const frac = g.at / g.steps.length;
  const v = $('#genVisual');
  if (v && !v.classList.contains('p4')) v.className = 'gen-visual ' + (frac < .2 ? 'p1' : frac < .55 ? 'p2' : 'p3');
}
function setBar(fraction, ms){
  const bar = $('#genBar'); if (!bar) return;
  const fill = bar.querySelector('i');
  fill.style.transitionDuration = (REDUCED.matches ? 0 : ms) + 'ms';
  fill.style.transform = `scaleX(${fraction})`;
  bar.setAttribute('aria-valuenow', Math.round(fraction * 100));
}
function runStep(){
  const g = state.gen; if (!g || g.error || state.view !== 'generating') return;
  paintGen();
  const last = g.steps.length - 1;
  if (g.at >= g.steps.length){ finishGeneration(); return; }
  announce(`Step ${g.at + 1} of ${g.steps.length}: ${g.steps[g.at].label}`);
  if (g.at === last){
    if (g.result){ g.at = g.steps.length; finishGeneration(); return; }
    setBar(.96, 25000);   // hold on "Final touches" until the image arrives
    return;
  }
  const step = g.steps[g.at];
  g.done += step.ms;
  setBar(Math.min(.9, g.done / g.total * .9), step.ms);
  later(() => { g.at += 1; runStep(); }, step.ms);
}
function finishGeneration(){
  const g = state.gen; if (!g || !g.result) return;
  paintGen();
  setBar(1, 350);
  $('#genSoft').src = g.result.result_image_url;
  $('#genSharp').src = g.result.result_image_url;
  const v = $('#genVisual');
  v.className = 'gen-visual p4';
  $('#genTitle').textContent = 'Your look is ready';
  announce('Your look is ready');
  later(() => v.classList.add('done'), REDUCED.matches ? 0 : 1050);
  later(() => {
    const item = g.result;
    const look = {id:item.id, img:item.result_image_url, before:state.photo, title:orderedLook().map(p => short(p.item)).join(', '),
      items:orderedLook().map(p => ({item:{...p.item}, size:p.size})), pose:state.pose, editable:true};
    state.current = look; state.justGenerated = true;
    if (Number.isFinite(state.balance?.remaining)) state.balance.remaining = Math.max(0, state.balance.remaining - 1);
    loadBalance();
    state.gallery = null;
    state.gen = null;
    go('result');
    const left = looksLeft();
    toast(left === Infinity || left === null ? 'Look saved privately' : `Look saved privately · ${left} ${left === 1 ? 'look' : 'looks'} left`, {action:{label:'View Looks', run:() => go('looks')}});
  }, REDUCED.matches ? 300 : 2000);
}
function stopGeneration(){
  if (state.gen){ state.gen.timers.forEach(clearTimeout); state.gen.controller?.abort(); state.gen = null; }
}

/* ---------- Result ---------- */
function result(){
  const look = state.current;
  if (!look) return looks();
  const items = look.items;
  const buyable = items.filter(p => p.item.url && !p.item.sample && p.item.source !== 'owned');
  const total = buyable.reduce((s, p) => s + (p.item.price || 0), 0);
  const stores = new Set(buyable.map(p => p.item.store));
  const celebrate = state.justGenerated; state.justGenerated = false;
  const status = p => p.item.sample ? '<span class="tag">Sample</span>' : p.item.source === 'owned' ? '<span class="tag ok">Owned</span>'
    : p.item.url ? `<a class="btn small" href="${esc(p.item.url)}" target="_blank" rel="noopener noreferrer" aria-label="Buy ${esc(p.item.name)} at ${esc(p.item.store)}">Buy ${icon('arrow','s')}</a>` : '';
  return `<section class="result">
    <div class="result-head"><div><p class="eyebrow">Saved to Looks</p><h1 style="margin-top:4px">Here's your look</h1></div><div style="display:flex;flex-wrap:wrap;gap:8px">${privatePill()}<span class="tag ai">${icon('spark','s')} ${look.pose === 'keep' ? 'Your pose' : 'Standard pose'}</span></div></div>
    <div class="result-visual${celebrate ? ' celebrate' : ''}">${compareHtml(look.before, look.img, 'You', 'New look', !celebrate)}<a class="link small" href="${esc(look.img)}" target="_blank" rel="noopener">Open full image</a></div>
    <div style="display:grid;gap:14px;align-content:start">
      <div class="card shop">
        <h2>Shop this look</h2>
        <p class="small muted" style="margin-bottom:4px">Each piece opens at its own store.</p>
        ${items.map(p => `<div class="shop-row">${p.item.img ? `<img src="${esc(img(p.item.img))}" alt="">` : `<span class="ph-slot">${icon('bag')}</span>`}<div><p class="n">${esc(p.item.name)}</p><p class="small muted num">${esc(SLOT_LABEL[p.item.slot] || '')}${p.item.store && !p.item.sample ? ' · ' + esc(p.item.store) : ''}${p.item.price ? ' · ' + inr(p.item.price) : ''}${p.item.url && !p.item.sample ? (p.size ? ' · Size ' + esc(p.size) : '') : ''}</p></div>${status(p)}</div>`).join('')}
        ${buyable.length ? `<div class="shop-total"><span class="small muted num">${buyable.length} to buy · ${stores.size} ${stores.size === 1 ? 'store' : 'stores'}</span><span class="price" style="font-size:22px">${inr(total)}</span></div>` : ''}
      </div>
      <div class="actions">
        <button class="action" data-act="share">${icon('share')}Share</button>
        <button class="action" data-act="generate-again" ${look.editable ? '' : 'disabled'}>${icon('redo')}Try again</button>
        <button class="action" data-act="edit-look" ${look.editable ? '' : 'disabled'}>${icon('edit')}Edit look</button>
        <button class="action" data-act="toggle-pose" ${look.editable ? '' : 'disabled'}>${icon('body')}${look.pose === 'keep' ? 'Standard pose' : 'My pose'}</button>
      </div>
      <div class="card feedback"><span style="font-weight:700">Does this look like you?</span><div class="thumbs"><button class="btn ghost small" data-act="feedback">${icon('up','s')} Yes</button><button class="btn ghost small" data-act="feedback-no">${icon('down','s')} Not quite</button></div></div>
      <p class="tiny muted">AI preview of appearance only. Check each store's size chart before buying.</p>
    </div>
  </section>`;
}

/* ---------- Wardrobe ---------- */
const OCCASIONS = ['Office', 'Weekend', 'Date night', 'Wedding guest', 'Festive'];
async function loadWardrobe(force = false){
  if (state.wardrobeLoading || (state.wardrobe && !force)) return;
  state.wardrobeLoading = true; state.wardrobeError = '';
  if (state.view === 'wardrobe') render();
  try { state.wardrobe = (await api('/v1/wardrobe')).items || []; }
  catch (err){ state.wardrobeError = err.message; }
  finally { state.wardrobeLoading = false; if (['wardrobe'].includes(state.view) || $('#addSheet').open){ if (state.view === 'wardrobe') render(); if ($('#addSheet').open) renderAdd(); } }
}
async function askStylist(occasion){
  state.occasion = occasion; state.ideas = []; state.ideasError = ''; state.ideasLoading = true; render();
  try {
    const res = await api('/v1/wardrobe/suggestions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({collection:'all', occasion, count:3})});
    state.ideas = res.outfits || [];
  } catch (err){ state.ideasError = err.message; }
  finally { state.ideasLoading = false; if (state.view === 'wardrobe') render(); }
}
function wardrobe(){
  if (!state.wardrobe && !state.wardrobeLoading && !state.wardrobeError) setTimeout(() => loadWardrobe(), 0);
  const all = state.wardrobe || [];
  const list = all.filter(w => w.collection === state.wardrobeTab && (state.wardrobeFilter === 'all' || w.slot === state.wardrobeFilter));
  const counts = {home:all.filter(w => w.collection === 'home').length, store:all.filter(w => w.collection === 'store').length};
  const byId = id => all.find(w => w.id === id);
  let ideas = '<p class="small muted">Pick an occasion. Outfits use only your own clothes and the items you saved.</p>';
  if (state.ideasLoading) ideas = `<div class="ideas"><div class="idea"><div class="sk" style="height:74px"></div><div class="sk" style="height:16px;width:60%"></div></div><div class="idea"><div class="sk" style="height:74px"></div><div class="sk" style="height:16px;width:50%"></div></div></div><p class="small muted" role="status">Styling outfits from your wardrobe…</p>`;
  else if (state.ideasError) ideas = `<div class="notice" role="alert">${icon('alert')}<span>${esc(state.ideasError)}</span></div>`;
  else if (state.ideas.length) ideas = `<div class="ideas">${state.ideas.map((idea, n) => `<article class="idea"><div class="idea-thumbs">${idea.item_ids.map(byId).filter(Boolean).map(w => `<img src="${esc(w.image_url)}" alt="${esc(w.name)}">`).join('')}</div><strong>${esc(idea.title)}</strong><p class="small muted">${esc(idea.reason)}</p><button class="btn small" data-act="use-idea" data-idea="${n}">Try this look ${icon('arrow','s')}</button></article>`).join('')}</div>`;
  let grid;
  if (state.wardrobeLoading && !state.wardrobe) grid = `<div class="grid">${'<div class="item"><div class="ph sk"></div><div class="sk" style="height:14px;width:70%"></div></div>'.repeat(4)}</div>`;
  else if (state.wardrobeError) grid = `<div class="notice" role="alert">${icon('alert')}<span>${esc(state.wardrobeError)}</span></div><button class="btn ghost small" data-act="wardrobe-retry" style="justify-self:start">${icon('redo','s')} Try again</button>`;
  else if (list.length) grid = `<div class="grid">${list.map(w => {
    const inl = inLook('w-' + w.id), confirm = state.confirmDelete === w.id;
    return `<article class="item"><div class="ph"><img src="${esc(w.image_url)}" alt="${esc(w.name)}" loading="lazy">${w.store ? `<span class="tag">${esc(w.store)}</span>` : ''}<button class="del${confirm ? ' confirm' : ''}" data-act="delete-item" data-id="${w.id}" aria-label="${confirm ? 'Confirm delete' : 'Delete'} ${esc(w.name)}">${confirm ? 'Delete?' : icon('x','s')}</button><button class="add" data-act="toggle-item" data-id="${w.id}" aria-pressed="${inl}" aria-label="${inl ? 'Remove from' : 'Add to'} my look: ${esc(w.name)}">${icon(inl ? 'check' : 'plus')}</button></div><div><p class="item-name">${esc(w.name)}</p><p class="tiny muted num">${w.price ? inr(w.price) : esc(w.color || SLOT_LABEL[w.slot] || '')}${w.product_url ? ` · <a href="${esc(w.product_url)}" target="_blank" rel="noopener noreferrer">View at store</a>` : ''}</p></div></article>`;
  }).join('')}</div>`;
  else grid = `<div class="empty">${EMPTY_ART}<h2>${state.wardrobeTab === 'home' ? 'Nothing here yet' : 'No saved items yet'}</h2><p class="muted">${state.wardrobeTab === 'home' ? 'Snap your clothes laid flat or on a hanger. The stylist will mix them into outfits for you.' : 'Save store products from your look to plan outfits before you buy.'}</p>${state.wardrobeTab === 'home' ? `<button class="btn" data-act="upload-item">${icon('camera','s')} Add clothes</button>` : `<button class="btn" data-act="go" data-view="home">${icon('link','s')} Paste a store link</button>`}</div>`;
  return `<section style="display:grid;gap:18px;padding-top:8px">
    <div class="page-head" style="padding-top:0"><div><p class="eyebrow">Wardrobe</p><h1>Your clothes</h1></div><button class="btn small quiet" data-act="upload-item">${icon('camera','s')} Add clothes</button></div>
    <div class="card stylist">
      <span class="tag ai" style="justify-self:start">${icon('spark','s')} AI stylist</span>
      <h2>What's the occasion?</h2>
      <div class="chips" role="group" aria-label="Occasion">${OCCASIONS.map(o => `<button class="chip" data-act="occasion" data-occasion="${o}" aria-pressed="${state.occasion === o}" ${state.ideasLoading ? 'disabled' : ''}>${o}</button>`).join('')}</div>
      ${ideas}
    </div>
    <div class="seg" role="tablist" aria-label="Wardrobe">
      <button role="tab" aria-selected="${state.wardrobeTab === 'home'}" data-act="wtab" data-tab="home">My clothes <span class="num">(${counts.home})</span></button>
      <button role="tab" aria-selected="${state.wardrobeTab === 'store'}" data-act="wtab" data-tab="store">Saved from stores <span class="num">(${counts.store})</span></button>
    </div>
    <div class="chips" role="group" aria-label="Category">${[['all','All'],['top','Tops'],['bottom','Bottoms'],['dress','Dresses'],['outerwear','Layers'],['footwear','Footwear'],['accessory','Accessories'],['jewelry','Jewellery']].map(([k, l]) => `<button class="chip" data-act="wfilter" data-filter="${k}" aria-pressed="${state.wardrobeFilter === k}">${l}</button>`).join('')}</div>
    ${grid}
  </section>`;
}

/* ---------- Looks ---------- */
async function loadGallery(){
  if (state.galleryLoading) return;
  state.galleryLoading = true; state.galleryError = '';
  try { state.gallery = (await api('/v1/gallery')).items || []; }
  catch (err){ state.galleryError = err.message; }
  finally { state.galleryLoading = false; if (state.view === 'looks') render(); }
}
function galleryTitle(g){
  const names = (g.items || []).map(i => i.name).filter(Boolean);
  return names.length ? names.map(n => n.split(',')[0]).join(', ') : (g.category || 'Try-on').replace(/^\w/, c => c.toUpperCase());
}
function looks(){
  if (!state.gallery && !state.galleryLoading && !state.galleryError) setTimeout(loadGallery, 0);
  let body;
  if (!state.gallery && !state.galleryError) body = `<div class="looks">${'<div class="look"><div class="sk" style="aspect-ratio:3/4;border-radius:14px"></div><div class="sk" style="height:14px;width:70%"></div></div>'.repeat(4)}</div>`;
  else if (state.galleryError) body = `<div class="notice" role="alert">${icon('alert')}<span>${esc(state.galleryError)}</span></div><button class="btn ghost small" data-act="gallery-retry" style="justify-self:start">${icon('redo','s')} Try again</button>`;
  else if (!state.gallery.length) body = `<div class="empty">${EMPTY_ART}<h2>No looks yet</h2><p class="muted">Every look you create is saved here, privately.</p><button class="btn brand" data-act="go" data-view="home">${icon('spark','s')} Create my first look</button></div>`;
  else body = `<div class="looks">${state.gallery.map(g => `<button class="look" data-act="open-look" data-id="${esc(g.id)}"><img src="${esc(g.result_image_url)}" alt="" loading="lazy"><span style="font-weight:700;line-height:1.3">${esc(galleryTitle(g))}</span><span class="tiny muted">${new Date(g.created_at).toLocaleDateString('en-IN', {day:'numeric', month:'short'})} · ${Math.max(1, (g.items || []).length)} ${Math.max(1, (g.items || []).length) === 1 ? 'piece' : 'pieces'}</span></button>`).join('')}</div>`;
  return `<section style="display:grid;gap:18px;padding-top:8px">
    <div class="page-head" style="padding-top:0"><div><p class="eyebrow">Private gallery</p><h1>Your looks</h1></div>${privatePill()}</div>
    ${body}
  </section>`;
}
function lookFromGallery(g){
  const items = (g.items && g.items.length ? g.items : [{slot:null, category:g.category, name:g.category, product_url:g.product_url}]).map((it, i) => ({
    item:{id:'g-' + i, slot:it.slot || 'top', name:it.name || it.category || 'Item', store:it.store || (it.product_url ? storeName((() => { try { return new URL(it.product_url).hostname; } catch { return ''; } })()) : ''), price:it.price ?? null, url:it.product_url || null, img:i === 0 ? g.product_image_url : '', source:it.collection === 'home' ? 'owned' : 'store'},
    size:it.size || null,
  }));
  return {id:g.id, img:g.result_image_url, before:g.person_image_url, title:galleryTitle(g), items, pose:'standard', editable:false};
}

/* ---------- Account ---------- */
function setAccount(account){
  state.account = account ? {email:account.email, unlimited:Boolean(account.unlimited)} : null;
  if (state.account) persist('fitcart-account', state.account); else { try { localStorage.removeItem('fitcart-account'); } catch {} }
}
function startAccountSession(payload){
  persist(SESSION_KEY, {anonymous_user_id:payload.anonymous_user_id, access_token:payload.access_token, token_type:payload.token_type, expires_at:payload.expires_at});
  setAccount(payload);
  state.wardrobe = null; state.gallery = null; state.ideas = [];
}
function openSignin(reason = null){
  state.signin = state.account ? {step:'account'} : {step:'email', email:'', busy:false, error:'', reason};
  renderSignin(); openSheet($('#signinSheet'));
  setTimeout(() => $('#signinEmail')?.focus(), 80);
}
function renderSignin(){
  const s = state.signin, a = state.account;
  let body;
  if (s.step === 'account'){
    body = `<div class="account-card"><span class="small muted">Signed in as</span><strong>${esc(a.email)}</strong>${a.unlimited ? `<span class="tag" style="justify-self:start">${icon('spark','s')} Unlimited looks</span>` : `<span class="small muted">${looksLeft() === null ? 'Checking your looks…' : looksLeft() === Infinity ? 'Looks available' : `${looksLeft()} ${looksLeft() === 1 ? 'look' : 'looks'} left`}</span>`}</div>
      <p class="small muted">Your wardrobe and looks are saved to this account, so they follow you to any device you sign in on.</p>
      <button class="btn ghost wide" data-act="sign-out">Sign out</button>`;
  } else if (s.step === 'email'){
    body = `<form id="signinForm" novalidate style="display:grid;gap:14px">
      ${s.reason === 'free' ? `<div class="notice">${icon('spark')}<span>Sign in to get <strong>${state.balance?.free_looks_per_month ?? 3} free looks every month</strong>. Your looks and wardrobe are saved to your account.</span></div>` : s.reason === 'buy' ? `<div class="notice">${icon('lock')}<span>Sign in first so your pass or plan is added to your account.</span></div>` : ''}
      <p class="small muted">We'll email you a sign-in code. No password needed.</p>
      <label class="field" for="signinEmail">Email<input class="input" id="signinEmail" type="email" inputmode="email" autocomplete="email" maxlength="254" placeholder="you@example.com" value="${esc(s.email)}" required></label>
      <p class="error" role="alert">${esc(s.error)}</p>
      <button class="btn brand wide" type="submit" ${s.busy ? 'disabled' : ''}>${s.busy ? '<span class="spin" aria-hidden="true"></span> Sending…' : `${icon('mail','s')} Email me a code`}</button>
    </form>`;
  } else {
    body = `<form id="signinForm" novalidate style="display:grid;gap:14px">
      <p class="small muted">We sent a code to <strong>${esc(s.email)}</strong>. It can take a minute, so check spam too. You can also tap the link in the email on this device.</p>
      <label class="field" for="signinCode">Code<input class="input code-input" id="signinCode" inputmode="numeric" autocomplete="one-time-code" maxlength="10" placeholder="••••••" required></label>
      <p class="error" role="alert">${esc(s.error)}</p>
      <button class="btn brand wide" type="submit" ${s.busy ? 'disabled' : ''}>${s.busy ? '<span class="spin" aria-hidden="true"></span> Checking…' : 'Sign in'}</button>
      <div style="display:flex;justify-content:space-between;gap:10px"><button class="link small" type="button" data-act="signin-back">Use another email</button><button class="link small" type="button" data-act="signin-resend">Send a new code</button></div>
    </form>`;
  }
  $('#signinSheet').innerHTML = `<div class="grabber" aria-hidden="true"></div><div class="sheet-head"><h2 id="signinTitle">${s.step === 'account' ? 'Your account' : 'Sign in'}</h2><button class="iconbtn" data-act="close-sheet" aria-label="Close">${icon('x')}</button></div><div class="sheet-body">${body}</div>`;
  const form = $('#signinForm');
  if (form) form.onsubmit = e => {
    e.preventDefault();
    if (s.step === 'email') sendSigninCode($('#signinEmail').value);
    else verifySigninCode($('#signinCode').value);
  };
}
async function sendSigninCode(raw, resend = false){
  const email = String(raw || '').trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)){ state.signin = {reason:state.signin?.reason, step:'email', email, busy:false, error:'Enter a valid email address.'}; renderSignin(); $('#signinEmail')?.focus(); return; }
  state.signin = {reason:state.signin?.reason, step: resend ? 'code' : 'email', email, busy:true, error:''}; renderSignin();
  try {
    await api('/v1/auth/email/code', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})}, false);
    state.signin = {reason:state.signin?.reason, step:'code', email, busy:false, error:''}; renderSignin();
    setTimeout(() => $('#signinCode')?.focus(), 60);
    if (resend) toast('New code sent.');
  } catch (err){
    state.signin = {reason:state.signin?.reason, step: resend ? 'code' : 'email', email, busy:false, error:err.message}; renderSignin();
  }
}
async function verifySigninCode(raw){
  const s = state.signin, code = String(raw || '').replace(/\s+/g, '');
  if (!/^\d{6,10}$/.test(code)){ state.signin = {...s, error:'Enter the code from the email.'}; renderSignin(); $('#signinCode')?.focus(); return; }
  state.signin = {...s, busy:true, error:''}; renderSignin();
  try {
    const payload = await api('/v1/auth/email/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email:s.email, code})}, false);
    signedIn(payload);
  } catch (err){
    state.signin = {...s, busy:false, error:err.message}; renderSignin(); $('#signinCode')?.focus();
  }
}
function signedIn(payload){
  startAccountSession(payload);
  closeSheet($('#signinSheet'));
  state.balance = null;
  render();
  toast(payload.unlimited ? `Signed in as ${payload.email} · unlimited looks` : `Signed in as ${payload.email}`);
  const pending = state.pending; state.pending = null;
  loadBalance().then(() => {
    if (pending?.plan) checkout(pending.plan);
    else if (pending?.generate && canGenerate()) startGeneration();
  });
}
async function signInFromLink(){
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get('access_token');
  const failed = params.get('error_description');
  if (!token && !failed) return false;
  history.replaceState(null, '', location.pathname + location.search);
  if (failed){ toast(failed.replace(/\+/g, ' '), {kind:'error'}); return true; }
  try { signedIn(await api('/v1/auth/email/link', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({access_token:token})}, false)); }
  catch (err){ toast(err.message, {kind:'error'}); }
  return true;
}
async function refreshAccount(){
  if (!state.account) return;
  try {
    const me = await api('/v1/me');
    setAccount(me.email ? me : null);
    render();
  } catch {}
}
function looksLeft(){
  if (unlimited()) return Infinity;
  if (!state.balance) return null;
  if (!state.balance.enforced) return Infinity;
  return state.balance.remaining ?? null;
}
async function loadBalance(){
  try {
    state.balance = await api('/v1/looks/balance');
    if (state.balance.unlimited !== unlimited() && state.account) setAccount({...state.account, unlimited:state.balance.unlimited});
  } catch { return; }
  if (state.view !== 'generating') render();
  if ($('#signinSheet').open && state.signin?.step === 'account') renderSignin();
}
function outOfLooks(){
  closeSheet($('#photoSheet'));
  go('pricing');
  toast('You have used all your looks. Pick a pass or plan to keep going.', {kind:'info'});
}
let razorpayScript = null;
function loadRazorpay(){
  if (window.Razorpay) return Promise.resolve();
  razorpayScript ??= new Promise((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = 'https://checkout.razorpay.com/v1/checkout.js';
    tag.onload = resolve;
    tag.onerror = () => { razorpayScript = null; reject(Error('Could not load Razorpay. Check your connection and try again.')); };
    document.head.appendChild(tag);
  });
  return razorpayScript;
}
async function checkout(plan){
  if (!state.account){ state.pending = {plan}; openSignin('buy'); return; }
  state.checkingOut = plan; render();
  const done = () => { state.checkingOut = null; if (state.view === 'pricing') render(); };
  try {
    const [opts] = await Promise.all([
      api('/v1/billing/checkout', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({plan, billing:state.billing})}),
      loadRazorpay(),
    ]);
    const dark = document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    const rzp = new window.Razorpay({
      key:opts.key_id, name:opts.name, description:opts.description, currency:opts.currency, amount:opts.amount,
      ...(opts.order_id ? {order_id:opts.order_id} : {subscription_id:opts.subscription_id}),
      prefill:{email:opts.email}, theme:{color: dark ? '#FF4D8D' : '#D81B64'},
      handler: response => { done(); confirmPayment(response); },
      modal:{ondismiss: () => { done(); toast('Payment cancelled. You were not charged.', {kind:'info'}); }},
    });
    rzp.on('payment.failed', e => toast(e?.error?.description || 'The payment failed. Please try again.', {kind:'error'}));
    rzp.open();
  } catch (err){
    done();
    if (err.code === 'sign_in_required'){ setAccount(null); state.pending = {plan}; openSignin('buy'); }
    else toast(err.message, {kind:'error'});
  }
}
async function confirmPayment(response, attempt = 0){
  try {
    state.balance = await api('/v1/billing/confirm', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(response)});
    const plan = PLANS.find(p => p.key === state.balance.plan);
    go(state.look.length ? 'builder' : 'home');
    toast(`Payment received · ${plan && plan.key !== 'free' ? plan.name + ' is active · ' : ''}${state.balance.remaining} looks ready`, {action:{label:'Try it on', run:() => go(state.look.length ? 'builder' : 'home')}});
  } catch (err){
    if (err.status === 409 && attempt < 5){
      if (!attempt) toast('Payment received. Adding your looks…', {kind:'info'});
      setTimeout(() => confirmPayment(response, attempt + 1), 3000);
      return;
    }
    toast(err.status === 409 ? 'Your payment is still processing. Your looks will appear in a minute.' : err.message, {kind: err.status === 409 ? 'info' : 'error'});
    setTimeout(loadBalance, 5000);
  }
}
function signOut(){
  try { localStorage.removeItem(SESSION_KEY); } catch {}
  setAccount(null);
  state.wardrobe = null; state.gallery = null; state.ideas = []; state.balance = null;
  closeSheet($('#signinSheet'));
  session(true).then(loadBalance).catch(() => {});
  render();
  toast('Signed out.', {kind:'info'});
}

/* ---------- Sheets ---------- */
function openSheet(d){ if (!d.open){ d.classList.remove('closing'); d.showModal(); } }
function closeSheet(d){
  if (!d || !d.open || d.classList.contains('closing')) return;
  if (REDUCED.matches){ d.close(); return; }
  d.classList.add('closing');
  d.addEventListener('animationend', () => { d.classList.remove('closing'); d.close(); }, {once:true});
}
function openAdd(slot){
  Object.assign(state, {addSlot:slot, addTab:'link', importError:'', imported:null, importing:false, draftSize:null});
  renderAdd(); openSheet($('#addSheet'));
  setTimeout(() => $('#addLink')?.focus(), 80);
}
const SKELETON = `<div class="skeleton" role="status" aria-label="Fetching product"><div class="sk" style="aspect-ratio:3/4"></div><div style="display:grid;gap:8px;align-content:start"><div class="sk" style="height:14px;width:40%"></div><div class="sk" style="height:18px"></div><div class="sk" style="height:18px;width:70%"></div><div class="sk" style="height:44px;width:90%"></div></div></div>`;
function slotSelect(id, value, act){
  return `<label class="field" for="${id}">This piece is a<select class="input" id="${id}" data-change="${act}">${SLOTS.map(s => `<option value="${s.key}" ${s.key === value ? 'selected' : ''}>${s.label}</option>`).join('')}</select></label>`;
}
function renderAdd(){
  const label = SLOT_LABEL[state.addSlot] || 'item';
  let body = '';
  if (state.addTab === 'link'){
    if (state.importing) body = `${SKELETON}<p class="small muted">Fetching price, sizes and photos from the store. This can take up to a minute.</p>`;
    else if (state.imported){
      const i = state.imported, pct = off(i);
      body = `<div class="preview"><img src="${esc(i.img)}" alt=""><div style="display:grid;gap:6px"><span class="tag" style="justify-self:start">${esc(i.store)}</span><strong style="line-height:1.3">${esc(i.name)}</strong>${i.price ? `<div class="pricerow"><span class="price">${inr(i.price)}</span>${i.mrp ? `<span class="mrp">${inr(i.mrp)}</span>` : ''}${pct ? `<span class="off tiny">${pct}% off</span>` : ''}</div>` : ''}${i.color ? `<span class="small muted">${esc(i.color)}</span>` : ''}</div></div>
      ${i.images.length > 1 ? `<div style="display:grid;gap:6px"><p class="small" style="font-weight:800">Photo used for your try-on</p><div class="imgpick">${i.images.map(u => `<button data-act="pick-image" data-src="${esc(u)}" aria-pressed="${u === i.img}" aria-label="Use this photo"><img src="${esc(u)}" alt=""></button>`).join('')}</div></div>` : ''}
      ${slotSelect('importSlot', i.slot, 'import-slot')}
      ${i.sizes.length ? `<div style="display:grid;gap:6px"><p class="small" style="font-weight:800">Your size <span class="muted" style="font-weight:600">· optional for now</span></p>${sizesHtml(i, state.draftSize, 'draft-size')}</div>` : ''}
      <div style="display:flex;gap:10px"><button class="btn" style="flex:1" data-act="confirm-add">${icon('plus','s')} Add to look</button><button class="btn ghost" data-act="add-reset">Back</button></div>`;
    } else body = `<form id="addForm" novalidate style="display:grid;gap:10px"><label class="field" for="addLink">Link from any store<input class="input" id="addLink" type="url" inputmode="url" autocomplete="off" placeholder="Paste a Myntra, Amazon, AJIO or Nike link" aria-describedby="addError addTip"></label><p class="error" id="addError" role="alert">${esc(state.importError)}</p><button class="btn wide" type="submit">Find product</button></form>
      <p class="tiny muted" id="addTip">Tip: in the store's app, tap Share, then Copy link.</p>`;
  } else if (state.addTab === 'wardrobe'){
    if (!state.wardrobe){ if (!state.wardrobeLoading && !state.wardrobeError) setTimeout(loadWardrobe, 0); }
    const owned = (state.wardrobe || []).filter(w => w.slot === state.addSlot && !inLook('w-' + w.id));
    body = state.wardrobeError ? `<div class="notice">${icon('alert')}<span>${esc(state.wardrobeError)}</span></div>`
      : !state.wardrobe ? `<div class="pickgrid">${'<div class="pick"><div class="sk" style="aspect-ratio:3/4"></div></div>'.repeat(3)}</div>`
      : owned.length ? `<div class="pickgrid">${owned.map(w => `<button class="pick" data-act="pick-owned" data-id="${w.id}"><img src="${esc(w.image_url)}" alt="">${esc(w.name)}</button>`).join('')}</div>`
      : `<div class="notice">${icon('info')}<span>No ${esc(label.toLowerCase())} in your wardrobe yet. Add one with a photo.</span></div>`;
  } else {
    body = `<button class="drop" data-act="upload-item">${icon('upload')}<strong>Upload a photo of the item</strong><span class="small muted">Laid flat or on a hanger works best. It's also saved to your wardrobe.</span></button>`;
  }
  $('#addSheet').innerHTML = `<div class="grabber" aria-hidden="true"></div><div class="sheet-head"><h2 id="addTitle">Add ${esc(label.toLowerCase())}</h2><button class="iconbtn" data-act="close-sheet" aria-label="Close">${icon('x')}</button></div>
    <div class="sheet-body"><div class="seg" role="tablist" aria-label="Where from">${[['link','Store link'],['wardrobe','My wardrobe'],['photo','Photo']].map(([k, l]) => `<button role="tab" aria-selected="${state.addTab === k}" data-act="add-tab" data-tab="${k}">${l}</button>`).join('')}</div>${body}</div>`;
  const form = $('#addForm');
  if (form) form.onsubmit = e => { e.preventDefault(); importLink($('#addLink').value, true); };
}
function validLink(raw){
  try { const u = new URL(String(raw).trim()); if (!/^https?:$/.test(u.protocol) || u.username || u.password) throw 0; return u.href; } catch { return null; }
}
async function scrape(url){
  const res = await api('/v1/products/scrape', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url, country:'IN'})}, false);
  if (!res.data?.image_urls?.length) throw Error('The store did not share a product photo. Add this item with a photo instead.');
  return fromScrape(res.data);
}
async function importLink(raw, inSheet){
  const url = validLink(raw);
  if (!url){
    const msg = 'Paste a full product link that starts with https://';
    if (inSheet){ state.importError = msg; renderAdd(); $('#addLink').value = raw; $('#addLink').focus(); }
    else { $('#homeError').textContent = msg; $('#homeLink').focus(); }
    return;
  }
  if (inSheet){
    Object.assign(state, {importing:true, importError:'', draftSize:null}); renderAdd();
    try {
      const item = await scrape(url);
      if (state.addSlot && item.detected == null) item.slot = state.addSlot;
      state.imported = item;
    } catch (err){ state.importError = err.message; }
    finally { state.importing = false; if ($('#addSheet').open){ renderAdd(); if (state.importError){ $('#addLink').value = url; } } }
    return;
  }
  const btn = $('#homeSubmit'); btn.disabled = true; btn.innerHTML = '<span class="spin" aria-hidden="true"></span> Finding…';
  $('#homeError').textContent = '';
  try {
    const item = await scrape(url);
    addToLook(item);
    go('builder');
    toast(`${short(item)} added from ${item.store}`, {action:{label:'Add more', run:() => openAdd(openSlots()[0]?.key || 'bottom')}});
  } catch (err){
    if ($('#homeError')){ $('#homeError').textContent = err.message; btn.disabled = false; btn.textContent = 'Add'; }
  }
}
function openPhoto(){
  $('#photoSheet').innerHTML = `<div class="grabber" aria-hidden="true"></div><div class="sheet-head"><h2 id="photoTitle">Try it on</h2><button class="iconbtn" data-act="close-sheet" aria-label="Close">${icon('x')}</button></div>
  <div class="sheet-body">
    ${personHtml()}
    <div class="tips"><div class="tip">${icon('face')}Face clearly visible</div><div class="tip">${icon('body')}Standing, head to toe is best</div><div class="tip">${icon('sun')}Good light</div></div>
    ${poseHtml('sheet')}
    ${consentHtml()}
    <button class="btn brand wide" data-act="generate" ${canGenerate() ? '' : 'disabled'}>${icon('spark')} Generate my look</button>
    <p class="tiny muted gen-hint" style="text-align:center">${state.photo && state.consent ? '' : generateHint()}</p>
    <div style="display:flex;justify-content:center">${planChip()}</div>
    <p class="tiny muted" style="display:flex;gap:6px;align-items:center">${icon('lock','s')} Saved only to your private gallery.</p>
  </div>`;
  openSheet($('#photoSheet'));
}
function openItemSheet(data, addAfter){
  const slot = addAfter && state.addSlot ? state.addSlot : (state.wardrobeFilter !== 'all' ? state.wardrobeFilter : 'top');
  state.itemDraft = {data, slot, addAfter, saving:false, error:''};
  renderItemSheet(); openSheet($('#itemSheet'));
}
function renderItemSheet(){
  const d = state.itemDraft;
  $('#itemSheet').innerHTML = `<div class="grabber" aria-hidden="true"></div><div class="sheet-head"><h2 id="itemTitle">Add to your wardrobe</h2><button class="iconbtn" data-act="close-sheet" aria-label="Close">${icon('x')}</button></div>
  <form class="sheet-body" id="itemForm" novalidate>
    <div class="preview"><img src="${d.data}" alt="Your item"><div style="display:grid;gap:10px">${slotSelect('itemSlot', d.slot, 'item-slot')}</div></div>
    <div class="poses" style="grid-template-columns:1fr 1fr"><label class="field" for="itemColor">Colour <span class="muted" style="font-weight:600">(optional)</span><input class="input" id="itemColor" maxlength="60" placeholder="e.g. Navy blue"></label><label class="field" for="itemName">Name <span class="muted" style="font-weight:600">(optional)</span><input class="input" id="itemName" maxlength="120" placeholder="e.g. Linen kurta"></label></div>
    <p class="error" role="alert">${esc(d.error)}</p>
    <button class="btn wide" type="submit" ${d.saving ? 'disabled' : ''}>${d.saving ? '<span class="spin" aria-hidden="true"></span> Saving…' : `${icon('plus','s')} ${d.addAfter ? 'Save and add to look' : 'Save to My clothes'}`}</button>
  </form>`;
  $('#itemForm').onsubmit = async e => {
    e.preventDefault();
    const color = $('#itemColor').value.trim(), name = $('#itemName').value.trim();
    d.saving = true; d.error = ''; renderItemSheet();
    try {
      const form = new FormData();
      form.append('collection', 'home'); form.append('slot', d.slot);
      form.append('name', name || [color, SLOT_LABEL[d.slot]].filter(Boolean).join(' '));
      if (color) form.append('color', color);
      form.append('image', dataUrlBlob(d.data), 'item.jpg');
      const w = await api('/v1/wardrobe', {method:'POST', body:form});
      if (state.wardrobe) state.wardrobe.unshift(w); else state.wardrobe = [w];
      closeSheet($('#itemSheet'));
      if (d.addAfter){ addToLook(fromWardrobe(w)); closeSheet($('#addSheet')); if (state.view !== 'builder') go('builder'); else render(); toast(`${w.name} added to your look and wardrobe`); }
      else { state.wardrobeTab = 'home'; render(); toast(`${w.name} saved to My clothes`); }
    } catch (err){ d.saving = false; d.error = err.message; renderItemSheet(); }
  };
}
async function saveToWardrobe(index){
  const p = state.look[index]; const i = p?.item;
  if (!i || i.wardrobeId || i.saving) return;
  i.saving = true; render();
  try {
    const form = new FormData();
    form.append('collection', 'store'); form.append('slot', i.slot); form.append('name', i.name);
    if (isRemote(i.img)) form.append('image_url', i.img); else form.append('image', await toBlob(i.img), 'item.jpg');
    if (i.brand) form.append('brand', i.brand);
    if (i.color) form.append('color', i.color.slice(0, 80));
    if (i.price != null){ form.append('price', i.price); form.append('currency', 'INR'); }
    const inStock = i.sizes.filter(s => !i.soldOut.includes(s));
    if (inStock.length) form.append('sizes', inStock.join(','));
    if (p.size) form.append('selected_size', p.size);
    if (i.store) form.append('store', i.store);
    if (i.url) form.append('product_url', i.url);
    const w = await api('/v1/wardrobe', {method:'POST', body:form});
    i.wardrobeId = w.id;
    if (state.wardrobe) state.wardrobe.unshift(w);
    toast('Saved to your wardrobe', {action:{label:'Open', run:() => { state.wardrobeTab = 'store'; go('wardrobe'); }}});
  } catch (err){ toast(err.message, {kind:'error'}); }
  finally { i.saving = false; render(); }
}

/* ---------- Photos ---------- */
function loadImage(src){ return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(Error('This image could not be opened. Try another photo.')); i.src = src; }); }
async function readPhoto(file, maxSide = 1600){
  if (!file) throw Error('Choose a photo.');
  if (!['image/jpeg','image/png','image/webp'].includes(file.type)) throw Error('Use a JPG, PNG or WebP photo.');
  if (file.size > 10 * 1024 * 1024) throw Error('That photo is over 10 MB. Try a smaller one.');
  const raw = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(Error('This file could not be read.')); r.readAsDataURL(file); });
  const im = await loadImage(raw);
  if (im.naturalWidth * im.naturalHeight > 50000000) throw Error('Please use a photo under 50 megapixels.');
  if (im.naturalWidth < 120 || im.naturalHeight < 120) throw Error('That photo is too small. Use one at least 120 × 120 pixels.');
  const scale = Math.min(1, maxSide / Math.max(im.naturalWidth, im.naturalHeight));
  const c = document.createElement('canvas');
  c.width = Math.round(im.naturalWidth * scale); c.height = Math.round(im.naturalHeight * scale);
  const ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height); ctx.drawImage(im, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', .9);
}

/* ---------- Events ---------- */
function bindView(){
  document.querySelectorAll('.compare').forEach(bindCompare);
  const form = $('#linkForm');
  if (form) form.onsubmit = e => { e.preventDefault(); $('#homeError').textContent = ''; importLink($('#homeLink').value, false); };
}
function removeAt(index){
  const [removed] = state.look.splice(index, 1);
  render();
  toast(`Removed ${short(removed.item).toLowerCase()}`, {kind:'info', action:{label:'Undo', run:() => { state.look.splice(Math.min(index, state.look.length), 0, removed); render(); }}});
}
function loadLook(items){ state.look = items.map(p => ({item:{...p.item}, size:p.size})); }
async function shareLook(){
  const look = state.current; if (!look) return;
  if (navigator.share){ try { await navigator.share({title:'My FitCart look', text:'What do you think of this outfit?', url:look.img}); return; } catch (err){ if (err.name === 'AbortError') return; } }
  try { await navigator.clipboard.writeText(look.img); toast('Private link copied. It works for about an hour.'); }
  catch { toast('Sharing is not available here. Use Open full image and share it from there.', {kind:'info'}); }
}
async function deleteItem(id){
  if (state.confirmDelete !== id){ state.confirmDelete = id; render(); setTimeout(() => { if (state.confirmDelete === id){ state.confirmDelete = null; if (state.view === 'wardrobe') render(); } }, 3500); return; }
  state.confirmDelete = null;
  const w = state.wardrobe.find(x => x.id === id);
  try {
    await api(`/v1/wardrobe/${encodeURIComponent(id)}`, {method:'DELETE'});
    state.wardrobe = state.wardrobe.filter(x => x.id !== id);
    state.look = state.look.filter(p => p.item.wardrobeId !== id);
    state.ideas = state.ideas.map(o => ({...o, item_ids:o.item_ids.filter(x => x !== id)}));
    render(); toast(`${w?.name || 'Item'} removed from your wardrobe`);
  } catch (err){ toast(err.message, {kind:'error'}); }
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-act]');
  if (!t || t.matches('input,select')) return;
  const a = t.dataset.act;
  switch (a){
    case 'go': e.preventDefault(); closeSheet($('#addSheet')); go(t.dataset.view); break;
    case 'theme': {
      const root = document.documentElement;
      const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      root.dataset.theme = dark ? 'light' : 'dark'; persist('fitcart-theme', root.dataset.theme); break;
    }
    case 'demo':
      state.look = []; ['shirt','jeans','sneakers'].forEach(k => addToLook({...CATALOG[k]}, null)); state.look[0].size = 'M';
      go('builder'); toast('Sample look loaded. Add real pieces from any store link.'); break;
    case 'add': openAdd(t.dataset.slot); break;
    case 'remove': removeAt(Number(t.dataset.index)); break;
    case 'save-piece': saveToWardrobe(Number(t.dataset.index)); break;
    case 'size': { const p = state.look[Number(t.dataset.index)]; p.size = p.size === t.dataset.size ? null : t.dataset.size; render(); document.querySelector(`[data-act="size"][data-index="${t.dataset.index}"][data-size="${CSS.escape(t.dataset.size)}"]`)?.focus(); break; }
    case 'open-photo': openPhoto(); break;
    case 'pick-photo': $('#photoFile').click(); break;
    case 'remove-photo': state.photo = null; if ($('#photoSheet').open) openPhoto(); if (state.view === 'builder') render(); break;
    case 'generate': startGeneration(); break;
    case 'generate-again': state.consent = true; loadLook(state.current.items); startGeneration(); break;
    case 'cancel': stopGeneration(); go('builder'); toast('Cancelled. Your look is just as you left it.', {kind:'info'}); break;
    case 'share': shareLook(); break;
    case 'edit-look': loadLook(state.current.items); go('builder'); break;
    case 'toggle-pose': state.pose = state.current.pose === 'keep' ? 'standard' : 'keep'; state.consent = true; loadLook(state.current.items); startGeneration(); break;
    case 'feedback': toast('Thanks! That helps us improve.'); break;
    case 'feedback-no': toast('Thanks. A clear, front-facing photo in good light gives a closer likeness.', {kind:'info', action:{label:'New photo', run:() => $('#photoFile').click()}}); break;
    case 'close-sheet': closeSheet(t.closest('dialog')); break;
    case 'add-tab': state.addTab = t.dataset.tab; renderAdd(); break;
    case 'draft-size': state.draftSize = state.draftSize === t.dataset.size ? null : t.dataset.size; renderAdd(); break;
    case 'pick-image': state.imported.img = t.dataset.src; renderAdd(); break;
    case 'add-reset': state.imported = null; renderAdd(); break;
    case 'confirm-add': {
      const item = state.imported; addToLook(item, state.draftSize); closeSheet($('#addSheet'));
      if (state.view !== 'builder') go('builder'); else render();
      toast(`${SLOT_LABEL[item.slot]} added from ${item.store}`); break;
    }
    case 'pick-owned': {
      const w = state.wardrobe.find(x => x.id === t.dataset.id); addToLook(fromWardrobe(w), w.selected_size || null); closeSheet($('#addSheet'));
      if (state.view !== 'builder') go('builder'); else render();
      toast(`${w.name} added from your wardrobe`); break;
    }
    case 'upload-item': $('#itemFile').click(); break;
    case 'wtab': state.wardrobeTab = t.dataset.tab; render(); break;
    case 'wfilter': state.wardrobeFilter = t.dataset.filter; render(); break;
    case 'wardrobe-retry': state.wardrobeError = ''; loadWardrobe(true); break;
    case 'gallery-retry': state.galleryError = ''; loadGallery(); break;
    case 'occasion': askStylist(t.dataset.occasion); break;
    case 'use-idea': {
      const idea = state.ideas[Number(t.dataset.idea)];
      state.look = []; idea.item_ids.map(id => state.wardrobe.find(w => w.id === id)).filter(Boolean).forEach(w => addToLook(fromWardrobe(w), w.selected_size || null));
      go('builder'); toast('Outfit ready. Add your photo to try it on.'); break;
    }
    case 'toggle-item': {
      const w = state.wardrobe.find(x => x.id === t.dataset.id);
      if (inLook('w-' + w.id)) state.look = state.look.filter(p => p.item.id !== 'w-' + w.id);
      else { addToLook(fromWardrobe(w), w.selected_size || null); toast(`${w.name} added to your look`, {action:{label:'View look', run:() => go('builder')}}); }
      render(); break;
    }
    case 'delete-item': deleteItem(t.dataset.id); break;
    case 'open-look': { const g = state.gallery.find(x => x.id === t.dataset.id); state.current = lookFromGallery(g); go('result'); break; }
    case 'billing': state.billing = t.dataset.billing; render(); document.querySelector(`[data-act="billing"][data-billing="${state.billing}"]`)?.focus(); break;
    case 'scroll': document.getElementById(t.dataset.target)?.scrollIntoView({behavior: REDUCED.matches ? 'auto' : 'smooth', block:'start'}); break;
    case 'choose-plan': {
      const p = PLANS.find(x => x.key === t.dataset.plan);
      if (p.key === 'free'){ go('home'); if (!state.account) openSignin('free'); break; }
      checkout(p.key);
      break;
    }
    case 'account': openSignin(t.dataset.reason || null); break;
    case 'signin-back': state.signin = {step:'email', email:state.signin?.email || '', busy:false, error:'', reason:state.signin?.reason}; renderSignin(); break;
    case 'signin-resend': sendSigninCode(state.signin.email, true); break;
    case 'sign-out': signOut(); break;
    case 'toast-action': { const act = toastAction; dismissToast(); act?.run(); break; }
  }
});
document.addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.act === 'consent'){
    state.consent = t.checked;
    document.querySelectorAll('[data-act="consent"]').forEach(c => c.checked = t.checked);
    document.querySelectorAll('[data-act="generate"]').forEach(b => b.disabled = !canGenerate());
    document.querySelectorAll('.gen-hint').forEach(h => { h.textContent = h.closest('#photoSheet') && canGenerate() ? '' : generateHint(); });
  }
  if (t.dataset.act === 'pose'){ state.pose = t.value; document.querySelectorAll('[data-act="pose"]').forEach(r => r.checked = r.value === state.pose); }
  if (t.dataset.change === 'import-slot' && state.imported) state.imported.slot = t.value;
  if (t.dataset.change === 'item-slot' && state.itemDraft) state.itemDraft.slot = t.value;
  if (t.dataset.change === 'piece-slot'){
    const p = state.look[Number(t.dataset.index)];
    if (p){ p.item.slot = t.value; const clash = state.look.filter(x => x !== p && !MULTI.includes(t.value) && x.item.slot === t.value); if (clash.length){ state.look = state.look.filter(x => !clash.includes(x)); toast(`Replaced the other ${SLOT_LABEL[t.value].toLowerCase()} in your look`, {kind:'info'}); } render(); }
  }
});
async function usePhoto(file){
  try {
    state.photo = await readPhoto(file);
    if ($('#photoSheet').open) openPhoto();
    if (state.view === 'builder') render();
    toast('Photo added. It stays private to you.');
  } catch (err){ if (file) toast(err.message, {kind:'error'}); }
}
$('#photoFile').onchange = e => { const file = e.target.files[0]; e.target.value = ''; usePhoto(file); };
document.addEventListener('dragover', e => { const zone = e.target.closest?.('[data-drop="photo"]'); if (zone){ e.preventDefault(); zone.classList.add('over'); } });
document.addEventListener('dragleave', e => e.target.closest?.('[data-drop="photo"]')?.classList.remove('over'));
document.addEventListener('drop', e => {
  const zone = e.target.closest?.('[data-drop="photo"]');
  if (!zone) return;
  e.preventDefault(); zone.classList.remove('over');
  const file = e.dataTransfer?.files?.[0];
  if (file) usePhoto(file);
});
$('#itemFile').onchange = async e => {
  const file = e.target.files[0]; e.target.value = '';
  try { openItemSheet(await readPhoto(file, 1200), $('#addSheet').open); }
  catch (err){ if (file) toast(err.message, {kind:'error'}); }
};
document.querySelectorAll('dialog.sheet').forEach(d => {
  d.addEventListener('click', e => { if (e.target === d) closeSheet(d); });
  d.addEventListener('cancel', e => { e.preventDefault(); closeSheet(d); });
});
(function boot(){
  const theme = readStore('fitcart-theme', null);
  if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
  fetch(apiUrl('/health'), {cache:'no-store'}).catch(() => {});
  render();
  fetch(apiUrl('/v1/billing/config')).then(r => r.ok ? r.json() : null).then(cfg => { state.billingCfg = cfg; if (state.view === 'pricing') render(); }).catch(() => {});
  signInFromLink().then(async fromLink => {
    if (!fromLink){ await session().catch(() => {}); refreshAccount(); }
    if (!state.balance) loadBalance();
  });
})();
