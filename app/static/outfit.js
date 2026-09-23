/* "Complete the look": add a bottom, footwear, layers, accessories and jewellery from any store to the
   main product, then generate one try-on wearing everything. Loaded after api.js. */
'use strict';

const OUTFIT_ADD_SLOTS = [
  ['top', 'Top'], ['bottom', 'Bottom wear'], ['outerwear', 'Jacket / layer'], ['footwear', 'Footwear'],
  ['accessory', 'Accessory'], ['jewelry', 'Jewellery'], ['dress', 'Dress'],
];
const OUTFIT_SLOT_NAME = Object.fromEntries(OUTFIT_ADD_SLOTS);
const OUTFIT_TWO_ALLOWED = ['jewelry', 'accessory'];
const OUTFIT_MAX_EXTRAS = 4;
let outfitDraft = null;

function outfitExtras() {
  if (!Array.isArray(state.outfit)) state.outfit = [];
  return state.outfit;
}

function outfitEnabled() {
  return Boolean(state.product) && (state.mode === 'scraped' || state.mode === 'manual');
}

function outfitTakenSlots() {
  return [slotForProduct(state.product), ...outfitExtras().map(item => item.slot)];
}

function outfitSlotOpen(slot, taken = outfitTakenSlots()) {
  if (outfitExtras().length >= OUTFIT_MAX_EXTRAS) return false;
  const count = taken.filter(other => other === slot).length;
  if (OUTFIT_TWO_ALLOWED.includes(slot)) return count < 2;
  if (slot === 'dress') return !taken.some(other => ['top', 'bottom', 'dress'].includes(other));
  if (slot === 'top' || slot === 'bottom') return !count && !taken.includes('dress');
  return !count;
}

/* Drop extras that clash with the main product after it changes, e.g. a new main top replaces an extra top. */
function normalizeOutfit() {
  const main = slotForProduct(state.product);
  const kept = [];
  const dropped = [];
  for (const item of outfitExtras()) {
    const taken = [main, ...kept.map(other => other.slot)];
    const clash = !OUTFIT_TWO_ALLOWED.includes(item.slot) && (
      taken.includes(item.slot)
      || (item.slot === 'dress' && taken.some(other => ['top', 'bottom'].includes(other)))
      || ((item.slot === 'top' || item.slot === 'bottom') && taken.includes('dress'))
    );
    (clash ? dropped : kept).push(item);
  }
  state.outfit = kept;
  if (dropped.length) toast(`Removed ${dropped.map(item => item.name).join(', ')} because it clashes with your main product.`);
}

function outfitTotal() {
  const prices = [state.product?.price, ...outfitExtras().map(item => item.price)];
  return prices.every(price => price != null) ? prices.reduce((sum, price) => sum + Number(price), 0) : null;
}

function outfitPieceRow(item, index) {
  const sizes = item.sizes?.length
    ? `<select class="input" style="min-height:36px;padding:4px 8px;width:auto" data-outfit-size="${index}" aria-label="Size for ${esc(item.name)}"><option value="">Size</option>${item.sizes.map(size => `<option value="${esc(size)}" ${item.size === size ? 'selected' : ''} ${item.soldOut?.includes(size) ? 'disabled' : ''}>${esc(size)}${item.soldOut?.includes(size) ? ' (sold out)' : ''}</option>`).join('')}</select>`
    : '';
  return `<div class="outfit-piece"><img src="${esc(item.image)}" alt=""><div class="outfit-piece-body"><strong class="small">${esc(OUTFIT_SLOT_NAME[item.slot] || item.slot)}</strong><span class="small">${esc(item.name)}</span><span class="micro muted">${[item.price != null ? money(Number(item.price)) : '', item.store].filter(Boolean).map(esc).join(' · ')}</span>${sizes}</div><button class="wdel" data-outfit-remove="${index}" aria-label="Remove ${esc(item.name)} from outfit">×</button></div>`;
}

function outfitPanelHtml() {
  if (!outfitEnabled()) return '';
  normalizeOutfit();
  const main = state.product;
  const taken = outfitTakenSlots();
  const addButtons = OUTFIT_ADD_SLOTS.filter(([slot]) => outfitSlotOpen(slot, taken))
    .map(([slot, label]) => `<button class="btn secondary" style="min-height:38px;padding:6px 12px;font-size:13px" data-outfit-add="${slot}">+ ${label}</button>`).join('');
  const total = outfitTotal();
  const stores = new Set([state.sourceStore, ...outfitExtras().map(item => item.store)].filter(Boolean));
  return `<section class="card pad outfit-panel" id="outfitPanel"><div class="row between"><h3 style="margin:0">Complete the look</h3><span class="badge outline">${1 + outfitExtras().length} of ${OUTFIT_MAX_EXTRAS + 1} pieces</span></div><p class="small muted" style="margin:6px 0 12px">Add a bottom, footwear, accessories or jewellery from any store. Your try-on will show you wearing all of them together.</p><div class="outfit-piece"><img src="${esc(main.image)}" alt=""><div class="outfit-piece-body"><strong class="small">${esc(SLOT_NAMES[slotForProduct(main)] || 'Main product')} · main</strong><span class="small">${esc(main.name)}</span><span class="micro muted">${[main.price != null ? money(main.price) : '', state.sourceStore].filter(Boolean).map(esc).join(' · ')}${state.size ? ` · Size ${esc(state.size)}` : ''}</span></div></div>${outfitExtras().map(outfitPieceRow).join('')}${addButtons ? `<div class="row" style="gap:8px;margin-top:12px">${addButtons}</div>` : ''}${outfitExtras().length ? `<p class="small" style="margin:14px 0 0">${total != null ? `<strong>Outfit total ${money(total)}</strong> · ` : ''}${stores.size} store${stores.size === 1 ? '' : 's'}</p>` : ''}</section>`;
}

(function injectOutfitStyles() {
  const style = document.createElement('style');
  style.textContent = `
.outfit-panel{margin-top:22px}
.outfit-piece{display:flex;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.outfit-piece img{width:52px;height:68px;object-fit:cover;border-radius:10px;background:#ecebe5;flex-shrink:0}
.outfit-piece-body{display:grid;gap:2px;flex:1;min-width:0}
.outfit-piece .wdel{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:6px 10px}
.outfit-preview{display:flex;gap:14px;align-items:flex-start;margin:14px 0}
.outfit-preview img{width:96px;height:124px;object-fit:cover;border-radius:12px;background:#ecebe5}
`;
  document.head.appendChild(style);
})();

function refreshOutfitPanel() {
  const panel = $('#outfitPanel');
  if (panel) panel.outerHTML = outfitPanelHtml();
}

function outfitDialogHtml() {
  const draft = outfitDraft;
  const slotSelect = `<label class="field">Category<select class="input" id="outfitSlot">${OUTFIT_ADD_SLOTS.map(([slot, label]) => `<option value="${slot}" ${draft.slot === slot ? 'selected' : ''} ${slot !== draft.slot && !outfitSlotOpen(slot) ? 'disabled' : ''}>${label}</option>`).join('')}</select></label>`;
  if (draft.item) {
    const item = draft.item;
    const sizes = item.sizes?.length
      ? `<div class="sizes" style="margin:8px 0">${item.sizes.map(size => `<button type="button" class="size ${item.size === size ? 'selected' : ''}" data-draft-size="${esc(size)}" ${item.soldOut?.includes(size) ? 'disabled style="text-decoration:line-through"' : ''}>${esc(size)}</button>`).join('')}</div><p class="micro muted">${item.soldOut?.length ? 'Crossed-out sizes are sold out. ' : ''}Size is optional for the preview.</p>`
      : '';
    return `<div class="outfit-preview"><img src="${esc(item.image)}" alt=""><div><strong>${esc(item.name)}</strong><p class="small muted" style="margin:4px 0">${[item.brand, item.color].filter(Boolean).map(esc).join(' · ')}</p><p class="price" style="margin:4px 0">${money(item.price)}${item.originalPrice && item.originalPrice > item.price ? ` <span class="small muted" style="text-decoration:line-through">${money(item.originalPrice)}</span>` : ''}</p><span class="badge outline">${esc(item.store || 'Your photo')}</span></div></div>${slotSelect}${sizes}<div class="row" style="margin-top:14px"><button class="btn" data-outfit-confirm>Add to outfit</button><button class="btn secondary" data-outfit-back>Choose another</button></div>`;
  }
  return `<form id="outfitLinkForm"><p class="small muted">Paste a product link from any store: Myntra, Amazon, Flipkart, AJIO, Nike and more.</p>${slotSelect}<label class="field">Product link<input class="input" id="outfitLink" type="url" required placeholder="https://…" value="${esc(draft.url || '')}"></label><button class="btn wide" type="submit" id="outfitFind" ${draft.busy ? 'disabled' : ''}>${draft.busy ? 'Finding product…' : 'Find product'}</button><p class="error" role="alert">${esc(draft.error || '')}</p></form><div class="rule"></div><form id="outfitPhotoForm"><p class="small muted">No link? Add a photo of the item instead.</p><label class="field">Name<input class="input" id="outfitPhotoName" maxlength="120" placeholder="e.g. White sneakers"></label><label class="field">Photo<input class="input" id="outfitPhoto" type="file" accept="image/jpeg,image/png,image/webp" required></label><button class="btn secondary wide" type="submit">Use this photo</button></form>`;
}

function openOutfitDialog(slot) {
  outfitDraft = { slot, url: '', item: null, busy: false, error: '' };
  showOutfitDialog();
}

function showOutfitDialog() {
  modal(`Add ${(OUTFIT_SLOT_NAME[outfitDraft.slot] || 'an item').toLowerCase()} to your look`, outfitDialogHtml());
  const slot = $('#outfitSlot');
  if (slot) slot.onchange = () => { outfitDraft.slot = slot.value; };
  const linkForm = $('#outfitLinkForm');
  if (linkForm) linkForm.onsubmit = event => { event.preventDefault(); findOutfitProduct($('#outfitLink').value.trim()); };
  const photoForm = $('#outfitPhotoForm');
  if (photoForm) photoForm.onsubmit = async event => {
    event.preventDefault();
    try {
      const data = await readPhoto($('#outfitPhoto').files[0]);
      const name = $('#outfitPhotoName').value.trim() || OUTFIT_SLOT_NAME[outfitDraft.slot];
      outfitDraft.item = { name, image: data, upload: true, price: null, store: 'Your photo', sizes: [], soldOut: [] };
      showOutfitDialog();
    } catch (error) {
      toast(error.message);
    }
  };
}

async function findOutfitProduct(link) {
  try {
    const url = new URL(link);
    if (!['http:', 'https:'].includes(url.protocol)) throw Error();
  } catch {
    outfitDraft.error = 'Enter a valid product link.';
    return showOutfitDialog();
  }
  outfitDraft = { ...outfitDraft, url: link, busy: true, error: '' };
  showOutfitDialog();
  try {
    const response = await fetch(apiUrl('/v1/products/scrape'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: link, country: 'IN' }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(apiErrorMessage(payload, 'This product could not be imported.'));
    const item = payload.data;
    if (!item?.image_urls?.length) throw Error('The store did not provide a product image. Add a photo instead.');
    const found = productFromApi(item, item.source_url || link);
    const detected = item.outfit_slot;
    if (detected && detected !== 'other' && detected !== outfitDraft.slot && outfitSlotOpen(detected)) outfitDraft.slot = detected;
    outfitDraft.item = {
      name: found.name, brand: found.brand, color: /not listed/i.test(found.color) ? '' : found.color,
      image: found.image, price: found.price, originalPrice: found.originalPrice,
      sizes: found.sizesKnown ? found.sizes : [], soldOut: found.soldOut, size: null,
      store: item.store || new URL(link).hostname, sourceUrl: item.source_url || link,
    };
  } catch (error) {
    outfitDraft.error = error.message;
  } finally {
    outfitDraft.busy = false;
    if ($('#modal').open) showOutfitDialog();
  }
}

function confirmOutfitItem() {
  const { slot, item } = outfitDraft;
  if (!outfitSlotOpen(slot)) {
    toast(`Your look already has a ${(OUTFIT_SLOT_NAME[slot] || slot).toLowerCase()}. Remove it first or pick another category.`);
    return;
  }
  outfitExtras().push({ ...item, slot });
  outfitDraft = null;
  $('#modal').close();
  state.result = false;
  refreshOutfitPanel();
  toast(`${OUTFIT_SLOT_NAME[slot]} added to your look.`);
}

/* Called by api.js while building the try-on request. */
function appendOutfitItems(form) {
  const extras = outfitEnabled() ? outfitExtras() : [];
  if (!extras.length) return 0;
  let uploads = 0;
  const items = extras.map(item => {
    const entry = { slot: item.slot, name: [item.color, item.name].filter(Boolean).join(' ').slice(0, 200), store: item.store || null, price: item.price ?? null, size: item.size || null, page_url: item.sourceUrl || null };
    if (item.upload) {
      form.append('outfit_images', dataUrlBlob(item.image), `piece-${uploads}.jpg`);
      entry.upload = uploads;
      uploads += 1;
    } else {
      entry.image_url = item.image;
    }
    return entry;
  });
  form.append('outfit_items', JSON.stringify(items));
  return extras.length;
}

document.addEventListener('click', event => {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.dataset.outfitAdd) { openOutfitDialog(target.dataset.outfitAdd); return; }
  if (target.dataset.outfitRemove !== undefined) {
    outfitExtras().splice(Number(target.dataset.outfitRemove), 1);
    state.result = false;
    refreshOutfitPanel();
    return;
  }
  if (target.dataset.draftSize !== undefined && outfitDraft?.item) {
    outfitDraft.item.size = outfitDraft.item.size === target.dataset.draftSize ? null : target.dataset.draftSize;
    showOutfitDialog();
    return;
  }
  if (target.hasAttribute('data-outfit-confirm')) { confirmOutfitItem(); return; }
  if (target.hasAttribute('data-outfit-back')) { outfitDraft.item = null; showOutfitDialog(); }
});

document.addEventListener('change', event => {
  const select = event.target.closest('[data-outfit-size]');
  if (!select) return;
  outfitExtras()[Number(select.dataset.outfitSize)].size = select.value || null;
});

const productWithoutOutfit = product;
product = function productWithOutfit() {
  productWithoutOutfit();
  if (!outfitEnabled()) return;
  screen.querySelector('.productinfo')?.insertAdjacentHTML('beforeend', outfitPanelHtml());
};

const uploadWithoutOutfit = upload;
upload = function uploadWithOutfit() {
  uploadWithoutOutfit();
  if (!outfitEnabled()) return;
  screen.querySelector('.uploadgrid > section:last-child')?.insertAdjacentHTML('afterbegin', outfitPanelHtml().replace('outfit-panel', 'outfit-panel" style="margin:0 0 22px'));
  const button = $('#generateButton');
  if (button && outfitExtras().length) button.textContent = `Generate my look with ${outfitExtras().length + 1} pieces`;
};

const resultWithoutOutfit = result;
result = function resultWithOutfit() {
  resultWithoutOutfit();
  if (!outfitEnabled() || !outfitExtras().length || !state.result) return;
  const card = screen.querySelector('.resultgrid section + section .card');
  if (!card) return;
  const total = outfitTotal();
  const rows = outfitExtras().map(item => `<div class="outfit-piece"><img src="${esc(item.image)}" alt=""><div class="outfit-piece-body"><strong class="small">${esc(OUTFIT_SLOT_NAME[item.slot])}</strong><span class="small">${esc(item.name)}</span><span class="micro muted">${[item.price != null ? money(Number(item.price)) : '', item.store, item.size ? 'Size ' + item.size : ''].filter(Boolean).map(esc).join(' · ')}</span></div>${item.sourceUrl ? `<a class="btn secondary" style="min-height:36px;padding:6px 12px;font-size:13px" href="${esc(item.sourceUrl)}" target="_blank" rel="noopener noreferrer">Buy</a>` : ''}</div>`).join('');
  card.insertAdjacentHTML('beforeend', `<div class="rule"></div><h3>Also in this look</h3>${rows}${total != null ? `<p style="margin:14px 0 0"><strong>Outfit total ${money(total)}</strong></p>` : ''}`);
};
