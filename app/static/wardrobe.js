/* FitCart online wardrobe: shopping wardrobe (saved store products), home wardrobe (clothes you own),
   multi-store outfit import, outfit try-on and AI outfit suggestions. Loaded after api.js. */
'use strict';

const WARDROBE_SLOTS = [
  ['top', 'Tops'], ['bottom', 'Bottoms'], ['dress', 'Dresses & one-pieces'], ['outerwear', 'Jackets & layers'],
  ['footwear', 'Footwear'], ['jewelry', 'Jewellery'], ['accessory', 'Accessories'], ['other', 'Other'],
];
const SLOT_LABEL = Object.fromEntries(WARDROBE_SLOTS);
const MULTI_SLOTS = ['jewelry', 'accessory'];
const MAX_PIECES = 5;

const wardrobe = {
  tab: 'store',
  items: [],
  loaded: false,
  loading: false,
  error: '',
  selected: [],
  person: null,
  consent: false,
  busy: '',
  suggestions: [],
  occasion: '',
  scope: 'all',
  result: null,
  imports: [{ url: '', slot: '' }, { url: '', slot: '' }, { url: '', slot: '' }],
};

(function injectWardrobeStyles() {
  const style = document.createElement('style');
  style.textContent = `
.wardrobe-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 22px}
.wardrobe-tabs button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:30px;padding:10px 18px;font-weight:600}
.wardrobe-tabs button[aria-selected=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.wardrobe-layout{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr);gap:26px;align-items:start}
.wardrobe-side{position:sticky;top:16px;display:grid;gap:18px}
.wardrobe-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin:12px 0 26px}
.wcard{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column}
.wcard.selected{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.wcard img{width:100%;aspect-ratio:3/4;object-fit:cover;background:#ecebe5;display:block}
.wcard .wbody{padding:10px;display:grid;gap:4px;flex:1}
.wcard .wname{font-size:13px;font-weight:600;line-height:1.3;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.wcard .wactions{display:flex;gap:6px;padding:0 10px 10px}
.wcard .wactions .btn{min-height:36px;padding:6px 10px;font-size:13px;flex:1}
.wcard .wdel{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:6px 10px}
.importrow{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:8px;margin-bottom:8px}
.importstatus{font-size:12px;margin:-4px 0 8px}
.pieces{display:grid;gap:8px;margin:10px 0}
.piece{display:flex;gap:10px;align-items:center;font-size:13px}
.piece img{width:44px;height:56px;object-fit:cover;border-radius:8px;background:#ecebe5}
.suggestion{border:1px solid var(--line);border-radius:14px;padding:14px;display:grid;gap:8px}
.suggestion .thumbs{display:flex;gap:6px;flex-wrap:wrap}
.suggestion .thumbs img{width:54px;height:70px;object-fit:cover;border-radius:8px;background:#ecebe5}
.personthumb{width:72px;height:96px;object-fit:cover;border-radius:10px}
.wardrobe-result img{width:100%;max-width:520px;border-radius:18px;display:block}
@media (max-width:860px){.wardrobe-layout{grid-template-columns:1fr}.wardrobe-side{position:static}.importrow{grid-template-columns:1fr}}
`;
  document.head.appendChild(style);
})();

function slotOptions(selected, includeAuto) {
  const auto = includeAuto ? `<option value="" ${selected ? '' : 'selected'}>Detect automatically</option>` : '';
  return auto + WARDROBE_SLOTS.map(([value, label]) => `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`).join('');
}

function wardrobeItems(collection) {
  return wardrobe.items.filter(item => !collection || item.collection === collection);
}

function itemById(id) {
  return wardrobe.items.find(item => item.id === id);
}

async function loadWardrobe(force = false) {
  if (wardrobe.loading || (wardrobe.loaded && !force)) return;
  wardrobe.loading = true;
  wardrobe.error = '';
  try {
    const response = await authorizedFetch(apiUrl('/v1/wardrobe'));
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(apiErrorMessage(payload, 'Could not load your wardrobe.'));
    wardrobe.items = payload.items || [];
    wardrobe.selected = wardrobe.selected.filter(id => itemById(id));
    wardrobe.loaded = true;
  } catch (error) {
    wardrobe.error = error.message;
  } finally {
    wardrobe.loading = false;
    if (pageView === 'wardrobe') wardrobePage();
  }
}

function toggleSelected(id) {
  const item = itemById(id);
  if (!item) return;
  if (wardrobe.selected.includes(id)) {
    wardrobe.selected = wardrobe.selected.filter(other => other !== id);
    return;
  }
  let next = wardrobe.selected.filter(other => {
    const slot = itemById(other)?.slot;
    if (slot === item.slot) return MULTI_SLOTS.includes(slot);
    if (item.slot === 'dress') return slot !== 'top' && slot !== 'bottom';
    if (item.slot === 'top' || item.slot === 'bottom') return slot !== 'dress';
    return true;
  });
  if (next.length >= MAX_PIECES) {
    toast(`An outfit can have up to ${MAX_PIECES} pieces. Remove one first.`);
    return;
  }
  next.push(id);
  wardrobe.selected = next;
}

function itemCard(item) {
  const chosen = wardrobe.selected.includes(item.id);
  const meta = item.collection === 'store'
    ? [item.price != null ? money(Number(item.price)) : '', item.store].filter(Boolean).join(' · ')
    : [item.color, item.brand].filter(Boolean).join(' · ');
  return `<article class="wcard ${chosen ? 'selected' : ''}"><img src="${esc(item.image_url)}" alt="${esc(item.name)}" loading="lazy"><div class="wbody"><span class="wname">${esc(item.name)}</span><span class="micro muted">${esc(meta || SLOT_LABEL[item.slot])}</span>${item.selected_size ? `<span class="micro">Size ${esc(item.selected_size)}</span>` : ''}${item.product_url ? `<a class="micro" href="${esc(item.product_url)}" target="_blank" rel="noopener noreferrer">Open in store</a>` : ''}</div><div class="wactions"><button class="btn ${chosen ? '' : 'secondary'}" data-wpick="${esc(item.id)}" aria-pressed="${chosen}">${chosen ? '✓ In outfit' : 'Add to outfit'}</button><button class="wdel" data-wdelete="${esc(item.id)}" aria-label="Remove ${esc(item.name)} from wardrobe">×</button></div></article>`;
}

function itemsBySlotHtml(items) {
  if (!items.length) return '';
  return WARDROBE_SLOTS.map(([slot, label]) => {
    const group = items.filter(item => item.slot === slot);
    return group.length ? `<h3 style="margin-top:8px">${label} <span class="small muted">(${group.length})</span></h3><div class="wardrobe-grid">${group.map(itemCard).join('')}</div>` : '';
  }).join('');
}

function storeAddPanel() {
  const rows = wardrobe.imports.map((row, index) => `<div class="importrow"><input class="input" type="url" data-import-url="${index}" value="${esc(row.url)}" placeholder="Product link ${index + 1} (Myntra, Amazon, Flipkart, AJIO…)" aria-label="Product link ${index + 1}"><select class="input" data-import-slot="${index}" aria-label="Category for link ${index + 1}">${slotOptions(row.slot, true)}</select></div>${row.status ? `<p class="importstatus ${row.failed ? 'error' : 'muted'}">${esc(row.status)}</p>` : ''}`).join('');
  return `<div class="card pad" style="margin-bottom:22px"><h3>Build an outfit from any store</h3><p class="small muted">Paste a top, a bottom, shoes or jewellery, each from a different store if you like. They are saved to your shopping wardrobe so you can try them on together.</p><form id="importForm">${rows}<div class="row"><button class="btn" type="submit" ${wardrobe.busy ? 'disabled' : ''}>${wardrobe.busy === 'import' ? 'Importing…' : 'Import to my wardrobe'}</button>${wardrobe.imports.length < MAX_PIECES ? '<button class="linkbtn" type="button" data-waction="addImportRow">+ Add another link</button>' : ''}</div></form></div>`;
}

function homeAddPanel() {
  return `<div class="card pad" style="margin-bottom:22px"><h3>Add clothes you already own</h3><p class="small muted">Photograph each piece on its own, laid flat or on a hanger, against a plain background. Add several photos of the same category at once.</p><form id="homeForm"><div class="grid2"><label class="field">Category<select class="input" id="homeSlot" required>${slotOptions('top', false)}</select></label><label class="field">Colour (optional)<input class="input" id="homeColor" maxlength="60" placeholder="e.g. Navy blue"></label></div><label class="field">Name (optional)<input class="input" id="homeName" maxlength="120" placeholder="e.g. White linen shirt"></label><label class="field">Photos<input class="input" id="homePhotos" type="file" accept="image/jpeg,image/png,image/webp" multiple required></label><div class="row"><button class="btn" type="submit" ${wardrobe.busy ? 'disabled' : ''}>${wardrobe.busy === 'home' ? 'Uploading…' : 'Add to home wardrobe'}</button></div><p class="error" id="homeError" role="alert"></p></form></div>`;
}

function builderPanel() {
  const pieces = wardrobe.selected.map(itemById).filter(Boolean);
  const person = wardrobe.person || state.photos?.[0]?.data || null;
  const piecesHtml = pieces.length
    ? pieces.map(item => `<div class="piece"><img src="${esc(item.image_url)}" alt=""><div><strong>${esc(SLOT_LABEL[item.slot])}</strong><br><span class="muted">${esc(item.name)}</span><br><span class="micro muted">${item.collection === 'home' ? 'Home wardrobe' : esc(item.store || 'Shopping wardrobe')}</span></div></div>`).join('')
    : '<p class="small muted">Tap “Add to outfit” on up to 5 pieces, from either wardrobe.</p>';
  const ready = pieces.length && person && wardrobe.consent && !wardrobe.busy;
  return `<div class="card pad"><div class="row between"><h3 style="margin:0">Your outfit</h3>${pieces.length ? '<button class="linkbtn" data-waction="clearOutfit">Clear</button>' : ''}</div><div class="pieces">${piecesHtml}</div><div class="rule"></div><div class="row">${person ? `<img class="personthumb" src="${person}" alt="Your photo">` : ''}<label class="btn secondary" style="cursor:pointer">${person ? 'Change photo' : 'Add your full-body photo'}<input type="file" id="wardrobePerson" accept="image/jpeg,image/png,image/webp" hidden></label></div><label class="consent" style="margin-top:12px"><input type="checkbox" id="wardrobeConsent" ${wardrobe.consent ? 'checked' : ''}><span class="small">I have permission to use this photo. It is sent to the FitCart API to create the try-on; a preview cannot guarantee fit.</span></label><button class="btn wide" data-waction="tryOutfit" ${ready ? '' : 'disabled'}>${wardrobe.busy === 'tryon' ? 'Creating your look…' : 'Try on this outfit'} ${icon('arrow')}</button><p class="error" id="outfitError" role="alert"></p></div>`;
}

function stylistPanel() {
  const cards = wardrobe.suggestions.map((outfit, index) => {
    const thumbs = outfit.item_ids.map(itemById).filter(Boolean).map(item => `<img src="${esc(item.image_url)}" alt="${esc(item.name)}" title="${esc(item.name)}">`).join('');
    return `<div class="suggestion"><strong>${esc(outfit.title)}</strong><div class="thumbs">${thumbs}</div><p class="small muted" style="margin:0">${esc(outfit.reason)}</p><button class="btn secondary" data-wsuggest="${index}">Use this outfit</button></div>`;
  }).join('');
  return `<div class="card pad"><h3>Ask the AI stylist</h3><p class="small muted">Get complete outfit ideas made only from your wardrobe.</p><form id="stylistForm"><label class="field">Occasion (optional)<input class="input" id="stylistOccasion" maxlength="120" value="${esc(wardrobe.occasion)}" placeholder="e.g. office, wedding, weekend brunch"></label><label class="field">Use items from<select class="input" id="stylistScope"><option value="all" ${wardrobe.scope === 'all' ? 'selected' : ''}>Both wardrobes</option><option value="home" ${wardrobe.scope === 'home' ? 'selected' : ''}>Home wardrobe only</option><option value="store" ${wardrobe.scope === 'store' ? 'selected' : ''}>Shopping wardrobe only</option></select></label><button class="btn wide" type="submit" ${wardrobe.busy ? 'disabled' : ''}>${wardrobe.busy === 'suggest' ? 'Styling…' : 'Suggest outfits'}</button><p class="error" id="stylistError" role="alert"></p></form><div style="display:grid;gap:12px;margin-top:14px">${cards}</div></div>`;
}

function wardrobePage() {
  if (!wardrobe.loaded && !wardrobe.loading && !wardrobe.error) loadWardrobe();
  const storeCount = wardrobeItems('store').length;
  const homeCount = wardrobeItems('home').length;
  const items = wardrobeItems(wardrobe.tab);
  const intro = wardrobe.tab === 'store'
    ? 'Products you have saved from stores but not bought yet.'
    : 'Clothes, footwear and jewellery you already have at home.';
  let body;
  if (wardrobe.loading && !wardrobe.loaded) body = '<p class="muted">Loading your wardrobe…</p>';
  else if (wardrobe.error) body = `<div class="notice">${esc(wardrobe.error)}</div><button class="btn secondary" data-waction="reload" style="margin-top:12px">Try again</button>`;
  else body = itemsBySlotHtml(items) || `<p class="muted">${wardrobe.tab === 'store' ? 'Nothing saved yet. Import a few product links above, or use “Save to my shopping wardrobe” on any product.' : 'Your home wardrobe is empty. Add photos of your clothes above.'}</p>`;
  const result = wardrobe.result
    ? `<section class="card pad wardrobe-result" id="wardrobeResult" style="margin-top:26px"><div class="finishmark">Your outfit try-on is ready</div><p class="small muted">AI-generated appearance preview. Saved to your private gallery.</p><img src="${esc(wardrobe.result.result_image_url)}" alt="Your outfit try-on"><div class="row" style="margin-top:14px"><a class="btn secondary" href="${esc(wardrobe.result.result_image_url)}" target="_blank" rel="noopener">Open full image</a>${(wardrobe.result.items || []).filter(item => item.product_url).map(item => `<a class="btn secondary" href="${esc(item.product_url)}" target="_blank" rel="noopener noreferrer">Buy ${esc(SLOT_LABEL[item.slot] || 'item').toLowerCase()}</a>`).join('')}</div></section>`
    : '';
  screen.innerHTML = `<div class="sectionhead"><div><div class="eyebrow">Your online wardrobe</div><h2>Mix, match and try it on.</h2><p class="muted small">${intro}</p></div><span class="badge">${icon('lock')} Private to this device's session</span></div><div class="wardrobe-tabs" role="tablist"><button role="tab" data-wtab="store" aria-selected="${wardrobe.tab === 'store'}">Shopping wardrobe (${storeCount})</button><button role="tab" data-wtab="home" aria-selected="${wardrobe.tab === 'home'}">Home wardrobe (${homeCount})</button></div><div class="wardrobe-layout"><section>${wardrobe.tab === 'store' ? storeAddPanel() : homeAddPanel()}${body}</section><aside class="wardrobe-side">${builderPanel()}${stylistPanel()}</aside></div>${result}`;
  bindWardrobeForms();
}

function bindWardrobeForms() {
  document.querySelectorAll('[data-import-url]').forEach(input => { input.oninput = () => { wardrobe.imports[Number(input.dataset.importUrl)].url = input.value; }; });
  document.querySelectorAll('[data-import-slot]').forEach(select => { select.onchange = () => { wardrobe.imports[Number(select.dataset.importSlot)].slot = select.value; }; });
  const importForm = $('#importForm');
  if (importForm) importForm.onsubmit = event => { event.preventDefault(); importLinks(); };
  const homeForm = $('#homeForm');
  if (homeForm) homeForm.onsubmit = event => { event.preventDefault(); addHomeItems(); };
  const stylistForm = $('#stylistForm');
  if (stylistForm) stylistForm.onsubmit = event => { event.preventDefault(); suggestOutfits(); };
  const occasion = $('#stylistOccasion');
  if (occasion) occasion.oninput = () => { wardrobe.occasion = occasion.value; };
  const scope = $('#stylistScope');
  if (scope) scope.onchange = () => { wardrobe.scope = scope.value; };
  const consent = $('#wardrobeConsent');
  if (consent) consent.onchange = () => { wardrobe.consent = consent.checked; wardrobePage(); };
  const person = $('#wardrobePerson');
  if (person) person.onchange = async () => {
    try {
      wardrobe.person = await readPhoto(person.files[0]);
      wardrobePage();
    } catch (error) {
      toast(error.message);
    }
  };
}

async function importOne(row) {
  const response = await fetch(apiUrl('/v1/products/scrape'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: row.url.trim(), country: 'IN' }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(apiErrorMessage(payload, 'Could not import this product.'));
  const item = payload.data;
  if (!item?.image_urls?.length) throw Error('The store did not provide a product image.');
  const form = new FormData();
  form.append('collection', 'store');
  form.append('slot', row.slot || item.outfit_slot || 'other');
  form.append('name', item.title || 'Store product');
  form.append('image_url', item.image_urls[0]);
  if (item.brand) form.append('brand', item.brand);
  if (item.colors?.length) form.append('color', item.colors.join(', '));
  if (item.price?.amount != null) { form.append('price', item.price.amount); form.append('currency', item.price.currency || 'INR'); }
  if (item.sizes?.length) form.append('sizes', item.sizes.join(','));
  form.append('store', item.store || new URL(row.url).hostname);
  form.append('product_url', item.source_url || row.url.trim());
  const saved = await authorizedFetch(apiUrl('/v1/wardrobe'), { method: 'POST', body: form });
  const savedPayload = await saved.json().catch(() => ({}));
  if (!saved.ok) throw Error(apiErrorMessage(savedPayload, 'Could not save to your wardrobe.'));
  return savedPayload;
}

async function importLinks() {
  const rows = wardrobe.imports.filter(row => row.url.trim());
  if (!rows.length) return toast('Paste at least one product link.');
  for (const row of rows) {
    try {
      const url = new URL(row.url.trim());
      if (!['http:', 'https:'].includes(url.protocol)) throw Error();
    } catch {
      row.status = 'This is not a valid product link.';
      row.failed = true;
      return wardrobePage();
    }
  }
  wardrobe.busy = 'import';
  rows.forEach(row => { row.status = 'Fetching product…'; row.failed = false; });
  wardrobePage();
  // Two at a time keeps the scraper within its concurrency limits.
  const queue = [...rows];
  const added = [];
  await Promise.all([0, 1].map(async () => {
    while (queue.length) {
      const row = queue.shift();
      try {
        const item = await importOne(row);
        added.push(item);
        row.status = `Saved: ${item.name}${item.price != null ? ' · ' + money(Number(item.price)) : ''}`;
        row.url = '';
      } catch (error) {
        row.status = error.message;
        row.failed = true;
      }
      if (pageView === 'wardrobe') wardrobePage();
    }
  }));
  wardrobe.items = [...added, ...wardrobe.items];
  added.forEach(item => { if (!wardrobe.selected.includes(item.id)) toggleSelected(item.id); });
  wardrobe.busy = '';
  wardrobe.tab = 'store';
  wardrobePage();
  if (added.length) toast(`${added.length} product${added.length > 1 ? 's' : ''} added to your outfit.`);
}

async function addHomeItems() {
  const files = [...($('#homePhotos')?.files || [])];
  const error = $('#homeError');
  if (!files.length) { error.textContent = 'Choose at least one photo.'; return; }
  if (files.length > 20) { error.textContent = 'Add up to 20 photos at a time.'; return; }
  const slot = $('#homeSlot').value;
  const color = $('#homeColor').value.trim();
  const name = $('#homeName').value.trim();
  wardrobe.busy = 'home';
  wardrobePage();
  const failures = [];
  for (const [index, file] of files.entries()) {
    try {
      const data = await readPhoto(file);
      const form = new FormData();
      form.append('collection', 'home');
      form.append('slot', slot);
      form.append('name', name ? (files.length > 1 ? `${name} ${index + 1}` : name) : [color, SLOT_LABEL[slot].replace(/s$/, '')].filter(Boolean).join(' '));
      if (color) form.append('color', color);
      form.append('image', dataUrlBlob(data), 'item.jpg');
      const response = await authorizedFetch(apiUrl('/v1/wardrobe'), { method: 'POST', body: form });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(apiErrorMessage(payload, 'Upload failed.'));
      wardrobe.items = [payload, ...wardrobe.items];
    } catch (failure) {
      failures.push(`${file.name}: ${failure.message}`);
    }
  }
  wardrobe.busy = '';
  wardrobe.tab = 'home';
  wardrobePage();
  if (failures.length) $('#homeError').textContent = failures.join(' ');
  else toast(`${files.length} item${files.length > 1 ? 's' : ''} added to your home wardrobe.`);
}

async function suggestOutfits() {
  wardrobe.busy = 'suggest';
  wardrobePage();
  let message = '';
  try {
    const response = await authorizedFetch(apiUrl('/v1/wardrobe/suggestions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ collection: wardrobe.scope, occasion: wardrobe.occasion.trim() || null, count: 3 }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(apiErrorMessage(payload, 'The stylist could not suggest outfits right now.'));
    wardrobe.suggestions = payload.outfits || [];
  } catch (error) {
    message = error.message;
  } finally {
    wardrobe.busy = '';
    wardrobePage();
    if (message) $('#stylistError').textContent = message;
  }
}

async function tryOutfit() {
  const person = wardrobe.person || state.photos?.[0]?.data;
  if (!person || !wardrobe.selected.length || !wardrobe.consent) return;
  wardrobe.busy = 'tryon';
  wardrobePage();
  const dialog = $('#generationDialog');
  $('#generationTitle').textContent = 'Putting your outfit together';
  $('#generationNote').textContent = 'Gemini is dressing your photo in every selected piece. This may take a minute.';
  $('#generationStatus').textContent = 'Generating your outfit try-on…';
  dialog.showModal();
  let message = '';
  try {
    const form = new FormData();
    form.append('person_image', dataUrlBlob(person), 'person.jpg');
    form.append('item_ids', wardrobe.selected.join(','));
    const response = await authorizedFetch(apiUrl('/v1/try-ons/outfit'), { method: 'POST', body: form });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(apiErrorMessage(payload, 'Outfit try-on failed.'));
    wardrobe.result = payload;
  } catch (error) {
    message = error.message;
  } finally {
    dialog.close();
    wardrobe.busy = '';
    wardrobePage();
    if (message) $('#outfitError').textContent = message;
    else $('#wardrobeResult')?.scrollIntoView({ behavior: 'smooth' });
  }
}

async function deleteItem(id) {
  const item = itemById(id);
  if (!item || !confirm(`Remove “${item.name}” from your wardrobe?`)) return;
  const response = await authorizedFetch(apiUrl(`/v1/wardrobe/${encodeURIComponent(id)}`), { method: 'DELETE' });
  if (!response.ok && response.status !== 404) {
    const payload = await response.json().catch(() => ({}));
    toast(apiErrorMessage(payload, 'Could not remove this item.'));
    return;
  }
  wardrobe.items = wardrobe.items.filter(other => other.id !== id);
  wardrobe.selected = wardrobe.selected.filter(other => other !== id);
  wardrobe.suggestions = wardrobe.suggestions.map(outfit => ({ ...outfit, item_ids: outfit.item_ids.filter(other => other !== id) }));
  wardrobePage();
}

document.addEventListener('click', event => {
  const target = event.target.closest('button,[data-wtab]');
  if (!target || pageView !== 'wardrobe') return;
  if (target.dataset.wtab) { wardrobe.tab = target.dataset.wtab; wardrobePage(); return; }
  if (target.dataset.wpick) { toggleSelected(target.dataset.wpick); wardrobePage(); return; }
  if (target.dataset.wdelete) { deleteItem(target.dataset.wdelete); return; }
  if (target.dataset.wsuggest !== undefined) {
    const outfit = wardrobe.suggestions[Number(target.dataset.wsuggest)];
    wardrobe.selected = outfit.item_ids.filter(id => itemById(id)).slice(0, MAX_PIECES);
    wardrobePage();
    toast('Outfit selected. Add your photo and tap “Try on this outfit”.');
    return;
  }
  switch (target.dataset.waction) {
    case 'addImportRow': if (wardrobe.imports.length < MAX_PIECES) wardrobe.imports.push({ url: '', slot: '' }); wardrobePage(); break;
    case 'clearOutfit': wardrobe.selected = []; wardrobePage(); break;
    case 'tryOutfit': tryOutfit(); break;
    case 'reload': wardrobe.error = ''; loadWardrobe(true); break;
  }
});

document.addEventListener('click', event => {
  const target = event.target.closest('[data-action="wardrobe"]');
  if (!target) return;
  $('#modal').open && $('#modal').close();
  showPage('wardrobe');
});

const renderWithoutWardrobe = render;
render = function renderWithWardrobe() {
  document.querySelectorAll('[data-action="wardrobe"].navlink').forEach(button => button.classList.toggle('active', pageView === 'wardrobe'));
  if (pageView !== 'wardrobe') return renderWithoutWardrobe();
  screen.classList.remove('result-enter');
  updateAccountNav();
  $('#steps').hidden = true;
  wardrobePage();
};

const startWithoutWardrobe = start;
start = function startWithWardrobeInvite() {
  startWithoutWardrobe();
  const strip = document.createElement('div');
  strip.className = 'demo-strip';
  strip.innerHTML = `<div><h3 style="margin-bottom:3px">Styling a whole outfit?</h3><p class="small muted" style="margin:0">Mix a top, bottom and shoes from different stores with clothes you already own, then try them on together.</p></div><button class="btn secondary" data-action="wardrobe">Open my wardrobe ${icon('arrow')}</button>`;
  screen.appendChild(strip);
};

(function addWardrobeNav() {
  const nav = document.querySelector('.nav');
  if (!nav) return;
  const button = document.createElement('button');
  button.dataset.action = 'wardrobe';
  button.className = 'navlink';
  button.textContent = 'Wardrobe';
  nav.insertBefore(button, nav.querySelector('[data-action="pricing"]'));
})();
