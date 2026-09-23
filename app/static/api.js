/* Live FitCart API adapter for the approved standalone interface. */
'use strict';

const SESSION_KEY = 'fitcart-anonymous-session-v1';
// Empty when the UI is served by the API itself; set by static/config.js to the
// Render API URL when the UI is hosted on GitHub Pages.
const API_BASE = String(window.FITCART_API_BASE || '').replace(/\/+$/, '');
const apiUrl = path => `${API_BASE}${path}`;

function apiErrorMessage(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return fallback;
}

function loadSession() {
  try {
    const session = JSON.parse(localStorage.getItem(SESSION_KEY));
    if (!session?.access_token || !session?.expires_at) return null;
    if (Date.parse(session.expires_at) <= Date.now() + 60_000) return null;
    return session;
  } catch {
    return null;
  }
}

async function newSession() {
  const response = await fetch(apiUrl('/v1/sessions/anonymous'), { method: 'POST' });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(apiErrorMessage(payload, 'Could not start a private session.'));
  localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
  return payload;
}

async function session(forceRefresh = false) {
  if (!forceRefresh) {
    const existing = loadSession();
    if (existing) return existing;
  }
  localStorage.removeItem(SESSION_KEY);
  return newSession();
}

async function authorizedFetch(url, options = {}) {
  let current = await session();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await fetch(url, {
      ...options,
      headers: { ...(options.headers || {}), Authorization: `Bearer ${current.access_token}` },
    });
    if (response.status !== 401 || attempt === 1) return response;
    current = await session(true);
  }
}

function dataUrlBlob(dataUrl) {
  const [header, encoded] = dataUrl.split(',');
  const mime = header.match(/data:([^;]+)/)?.[1] || 'image/jpeg';
  const bytes = atob(encoded);
  const output = new Uint8Array(bytes.length);
  for (let index = 0; index < bytes.length; index += 1) output[index] = bytes.charCodeAt(index);
  return new Blob([output], { type: mime });
}

const SLOT_NAMES = { top: 'Top', bottom: 'Bottom wear', dress: 'Dress', outerwear: 'Layer', footwear: 'Footwear', jewelry: 'Jewellery', accessory: 'Accessory', other: 'Other wearable' };

const LETTER_SIZES = ['XXXS', 'XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', 'XXXL', '3XL', '4XL', '5XL'];

function sizeOrder(label) {
  const letter = LETTER_SIZES.indexOf(String(label).toUpperCase());
  if (letter >= 0) return letter;
  const number = parseFloat(String(label).replace(/^[A-Z ]+/i, ''));
  return Number.isFinite(number) ? 100 + number : 10_000;
}

function productFromApi(item, sourceUrl) {
  const sizes = item.sizes?.filter(Boolean) || [];
  const soldOut = item.unavailable_sizes?.filter(Boolean) || [];
  return {
    name: item.title || 'Imported product',
    brand: item.brand || item.store || 'Imported listing',
    color: item.colors?.join(', ') || 'Colour not listed',
    category: item.category || SLOT_NAMES[item.outfit_slot] || 'Wearable',
    storeCategory: Boolean(item.category),
    slot: item.outfit_slot || 'other',
    price: item.price?.amount ?? null,
    originalPrice: item.original_price?.amount ?? null,
    discount: item.discount_percent ?? null,
    rating: item.rating ?? null,
    reviews: item.review_count ?? null,
    sizes: sizes.length || soldOut.length ? [...sizes, ...soldOut].sort((a, b) => sizeOrder(a) - sizeOrder(b)) : ['One size'],
    soldOut,
    sizesKnown: Boolean(sizes.length || soldOut.length),
    image: item.image_urls?.[0] || '',
    images: (item.image_urls || []).slice(0, 8),
    material: item.material || 'Not listed by store',
    style: item.external_id || 'Not listed',
    description: item.description || '',
    sourceUrl,
  };
}

const standaloneProduct = product;
product = function liveProduct() {
  if (state.mode !== 'scraped') return standaloneProduct();
  const p = state.product;
  const mrp = p.originalPrice && p.price && p.originalPrice > p.price
    ? `<span class="muted" style="text-decoration:line-through">${money(p.originalPrice)}</span>${p.discount ? `<span class="badge sand">${Math.round(p.discount)}% off</span>` : ''}`
    : '';
  const rating = p.rating ? `<span class="small muted">★ ${esc(p.rating)}${p.reviews ? ` · ${esc(p.reviews.toLocaleString('en-IN'))} ratings` : ''}</span>` : '';
  screen.innerHTML = `<div class="sectionhead"><p class="eyebrow" style="margin:0">Your find / ${esc(p.category)}</p><span class="badge">Live from ${esc(state.sourceStore)}</span></div><div class="productgrid"><div><div class="productphoto"><img src="${esc(p.image)}" alt="${esc(p.name)}"><span class="badge outline" style="background:var(--card)">Store image</span></div>${p.images.length > 1 ? `<div class="row" style="gap:8px;margin-top:10px" aria-label="Choose the product photo used for your try-on">${p.images.slice(0, 8).map(url => `<button class="imgpick" data-product-image="${esc(url)}" aria-pressed="${url === p.image}" style="padding:0;border:2px solid ${url === p.image ? 'var(--accent)' : 'var(--line)'};border-radius:10px;overflow:hidden;background:none"><img src="${esc(url)}" alt="" style="width:52px;height:64px;object-fit:cover;display:block"></button>`).join('')}</div><p class="micro muted" style="margin:6px 0 0">Tap the photo that shows the product most clearly. It is used for your try-on.</p>` : ''}<p class="micro muted" style="margin:10px 0">Fetched from the original listing. Prices and stock can change; confirm on the store.</p></div><section class="productinfo"><div class="eyebrow">${esc(p.brand)}</div><h1>${esc(p.name)}</h1><p class="muted">${esc(p.color)} · ${esc(p.category)}</p><div class="row"><span class="price">${money(p.price)}</span>${mrp}${rating}</div><dl class="detailgrid"><div><dt>Fabric / material</dt><dd>${esc(p.material)}</dd></div><div><dt>Store product ID</dt><dd>${esc(p.style)}</dd></div><div><dt>Sizes in stock</dt><dd>${p.sizesKnown ? esc(p.sizes.filter(size => !p.soldOut.includes(size)).join(', ') || 'None') : 'Not listed'}</dd></div><div><dt>Data status</dt><dd>Fetched live · ${esc(state.sourceStore)}</dd></div></dl><label class="field" style="margin-top:14px">This product is a<select class="input" id="productSlot">${Object.entries(SLOT_NAMES).map(([value, label]) => `<option value="${value}" ${p.slot === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label><p class="micro muted" style="margin-top:-6px">Detected automatically. Change it if it's wrong: the try-on only swaps this part of your outfit.</p><div class="rule"></div><div class="row between"><strong class="small">Choose your size</strong><a class="linkbtn" href="${esc(state.sourceUrl)}" target="_blank" rel="noopener noreferrer">Store size chart</a></div><div class="sizes">${sizeButtons()}</div><p id="sizeAvailability" class="small muted">${sizeAvailability()}</p><div style="margin-top:22px" class="row"><button class="btn wide" data-action="toCompare" ${!state.size ? 'disabled' : ''}>${state.size ? 'Continue with size ' + esc(state.size) : 'Select a size to continue'} ${icon('arrow')}</button><button class="btn secondary wide" data-action="saveToWardrobe">Save to my shopping wardrobe</button></div></section></div>`;
  $('#productSlot').onchange = event => {
    p.slot = event.target.value;
    if (!p.storeCategory) p.category = SLOT_NAMES[p.slot];
    state.result = false;
    product();
  };
};

const standaloneSizeButtons = sizeButtons;
sizeButtons = function liveSizeButtons() {
  if (state.mode !== 'scraped') return standaloneSizeButtons();
  const soldOut = state.product.soldOut || [];
  return state.product.sizes.map(size => {
    const out = soldOut.includes(size);
    return `<button class="size ${state.size === size ? 'selected' : ''}" data-size="${esc(size)}" aria-pressed="${state.size === size}" ${out ? 'disabled title="Sold out at the store"' : ''} ${out ? 'style="text-decoration:line-through"' : ''}>${esc(size)}</button>`;
  }).join('');
};

const standaloneSizeAvailability = sizeAvailability;
sizeAvailability = function liveSizeAvailability() {
  if (state.mode !== 'scraped') return standaloneSizeAvailability();
  const p = state.product;
  if (!p.sizesKnown) return 'The store did not list sizes for this product. Check the original listing.';
  if (!state.size) return p.soldOut.length ? `Crossed-out sizes are sold out at ${esc(state.sourceStore)}.` : 'Select your size.';
  return `Size ${esc(state.size)} is in stock at ${esc(state.sourceStore)} right now.`;
};

const standaloneCompare = compare;
compare = function liveCompare() {
  standaloneCompare();
  if (state.mode !== 'scraped') return;
  const note = screen.querySelector('.sectionhead p.muted.small');
  if (note) note.textContent = `Live price from ${state.sourceStore}. Price comparison with other stores is not connected yet.`;
  const badge = screen.querySelector('.compare-product .badge');
  if (badge) badge.textContent = 'Live listing';
};

function installLiveLinkForm() {
  const form = $('#linkForm');
  if (!form) return;
  form.onsubmit = async event => {
    event.preventDefault();
    const field = $('#productLink');
    const button = form.querySelector('button[type="submit"]');
    let url;
    try {
      url = new URL(field.value.trim());
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw Error();
    } catch {
      toast('Enter a valid public product link.');
      return;
    }

    button.disabled = true;
    button.textContent = 'Finding product…';
    try {
      const response = await fetch(apiUrl('/v1/products/scrape'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.href, country: 'IN' }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(apiErrorMessage(payload, 'The product could not be imported.'));
      const item = payload.data;
      if (!item?.image_urls?.length) throw Error('The store did not provide a usable product image. Add it manually instead.');
      state.sourceUrl = item.source_url || url.href;
      state.sourceStore = item.store || url.hostname;
      state = {
        ...state,
        mode: 'scraped',
        product: productFromApi(item, state.sourceUrl),
        size: null,
        offer: 0,
        step: 1,
        max: 1,
        result: false,
      };
      render();
      window.scrollTo(0, 0);
      toast('Live product details imported.');
    } catch (error) {
      modal('Could not import this product', `<p>${esc(error.message)}</p><p class="muted">You can upload the product image and details manually, or try another public product link.</p><div class="row"><button class="btn" data-action="manual">Add product manually</button><button class="btn secondary" data-action="close">Try another link</button></div>`);
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.innerHTML = `Find product ${icon('arrow')}`;
      }
    }
  };
}

const standaloneStart = start;
start = function liveStart() {
  standaloneStart();
  installLiveLinkForm();
};

const standaloneCanContinue = canContinue;
canContinue = function liveCanContinue() {
  return state.mode === 'scraped' ? Boolean(state.size) : standaloneCanContinue();
};

const standaloneOffersHtml = offersHtml;
offersHtml = function liveOffersHtml() {
  if (state.mode !== 'scraped') return standaloneOffersHtml();
  return `<div class="card pad"><span class="badge">Live imported product</span><h3 style="margin-top:14px">${esc(state.sourceStore)}</h3><p class="price">${money(state.product.price)}</p><p class="small muted">Size ${esc(state.size)} · Confirm stock, delivery, returns and final price on the original store.</p><a class="btn secondary" href="${esc(state.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open original listing</a></div>`;
};

const standaloneUpload = upload;
upload = function liveUpload() {
  standaloneUpload();
  const notice = document.querySelector('.uploadgrid .notice');
  if (notice && !state.samplePerson) notice.textContent = 'Your front-facing photo and the selected product are securely sent to the FitCart API to create the try-on. The result is saved in your private anonymous gallery.';
  const privacyText = document.querySelector('.uploadgrid section + section > p.small.muted');
  if (privacyText) privacyText.innerHTML = `${icon('lock')} Images and the generated result are saved in your private Supabase gallery.`;
  const button = $('#generateButton');
  if (button && !state.samplePerson) button.textContent = 'Generate my virtual try-on';
  const chartNote = document.querySelector('.uploadgrid .infobox');
  if (chartNote && state.mode === 'scraped') chartNote.textContent = `Check ${state.sourceStore}'s size chart for this product before buying.`;
  const consent = document.querySelector('.consent span');
  if (consent) consent.textContent = 'I have permission to use these photos. I understand they are sent to the FitCart API to create the try-on, and a visual preview cannot guarantee fit.';
};

function categoryForApi(product) {
  if (product.slot && product.slot !== 'other') return SLOT_NAMES[product.slot].toLowerCase();
  return ({ Tops: 'top', Bottoms: 'bottom', Shoes: 'shoes', Watch: 'watch' })[product.category] || String(product.category || 'wearable').toLowerCase();
}

function slotForProduct(product) {
  if (product.slot) return product.slot;
  return ({ Tops: 'top', Bottoms: 'bottom', Shoes: 'footwear', Watch: 'accessory' })[product.category] || 'other';
}

async function saveProductToWardrobe() {
  const p = state.product;
  if (!p) return;
  const form = new FormData();
  form.append('collection', 'store');
  form.append('slot', slotForProduct(p));
  form.append('name', p.name);
  if (state.mode === 'manual') form.append('image', dataUrlBlob(p.image), 'product.jpg');
  else form.append('image_url', p.image);
  if (p.brand) form.append('brand', p.brand);
  if (p.color && !/not (listed|provided)/i.test(p.color)) form.append('color', p.color);
  if (p.price != null) { form.append('price', p.price); form.append('currency', 'INR'); }
  if (p.sizes?.length) form.append('sizes', p.sizes.filter(size => !(p.soldOut || []).includes(size)).join(','));
  if (state.size) form.append('selected_size', state.size);
  if (state.sourceStore) form.append('store', state.sourceStore);
  if (state.sourceUrl) form.append('product_url', state.sourceUrl);
  const response = await authorizedFetch(apiUrl('/v1/wardrobe'), { method: 'POST', body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(apiErrorMessage(payload, 'Could not save to your wardrobe.'));
  return payload;
}

document.addEventListener('click', event => {
  const target = event.target.closest('[data-product-image]');
  if (!target || state.mode !== 'scraped' || !state.product) return;
  state.product.image = target.dataset.productImage;
  state.result = false;
  product();
});

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action="saveToWardrobe"]');
  if (!target) return;
  target.disabled = true;
  try {
    await saveProductToWardrobe();
    toast('Saved to your shopping wardrobe. Open Wardrobe to build an outfit.');
  } catch (error) {
    toast(error.message);
  } finally {
    target.disabled = false;
  }
});

generate = async function liveGenerate() {
  if (generationController || uploadBusy || !canGenerate()) return;
  if (state.samplePerson) {
    state.resultImageUrl = ASSETS.after;
    state.result = true;
    advance(4);
    return;
  }

  const controller = new AbortController();
  generationController = controller;
  const button = $('#generateButton');
  const dialog = $('#generationDialog');
  button.disabled = true;
  button.textContent = 'Generating your try-on…';
  $('#generationTitle').textContent = 'Creating your personal look';
  $('#generationNote').textContent = 'Gemini is combining your photo with the selected product. This may take a minute.';
  $('#generationStatus').textContent = 'Securely uploading your images…';
  dialog.showModal();

  try {
    const form = new FormData();
    form.append('person_image', dataUrlBlob(state.photos[0].data), 'person.jpg');
    form.append('category', categoryForApi(state.product));
    if (state.product.name) form.append('product_name', [state.product.color && !/not (listed|provided)/i.test(state.product.color) ? state.product.color : '', state.product.name].filter(Boolean).join(' ').slice(0, 200));
    form.append('country', 'IN');
    if (state.mode === 'manual') {
      form.append('product_image', dataUrlBlob(state.product.image), 'product.jpg');
    } else {
      // Reuse the image found by the scrape in step 1 instead of scraping the page again.
      if (state.product.image) form.append('product_image_url', state.product.image);
      form.append('product_page_url', state.sourceUrl);
    }
    const extraPieces = typeof appendOutfitItems === 'function' ? appendOutfitItems(form) : 0;
    if (extraPieces) $('#generationNote').textContent = `Gemini is dressing you in all ${extraPieces + 1} pieces of your look. This may take a minute.`;
    $('#generationStatus').textContent = 'Generating your virtual try-on…';
    const response = await authorizedFetch(apiUrl('/v1/try-ons'), { method: 'POST', body: form, signal: controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(apiErrorMessage(payload, 'Virtual try-on generation failed.'));
    state.galleryItem = payload;
    state.resultImageUrl = payload.result_image_url;
    state.result = true;
    dialog.close();
    advance(4);
  } catch (error) {
    dialog.close();
    if (error.name !== 'AbortError') $('#uploadError').textContent = error.message;
  } finally {
    generationController = null;
    if (state.step === 3) {
      button.disabled = !canGenerate();
      button.textContent = 'Generate my virtual try-on';
    }
  }
};

result = function liveResult() {
  if (!state.result || !state.resultImageUrl) {
    go(3);
    return;
  }
  const isSample = state.samplePerson;
  screen.innerHTML = `<div class="sectionhead"><div><div class="finishmark">${isSample ? 'Sample look' : 'Your virtual try-on is ready'}</div><h2>A new way to picture it.</h2><p class="small muted">${isSample ? 'Pre-made illustration.' : 'AI-generated appearance preview. Always verify size and product details with the retailer.'}</p></div><button class="btn secondary" data-action="editPhotos">Try another photo</button></div><div class="resultgrid"><section><img class="resultcanvas" src="${esc(state.resultImageUrl)}" alt="Your generated FitCart virtual try-on"><div class="row" style="margin-top:14px"><a class="btn secondary" href="${esc(state.resultImageUrl)}" target="_blank" rel="noopener">Open full image</a><button class="btn" data-action="shareLive">Share result</button></div></section><section><div class="card pad"><span class="badge">Saved to private gallery</span><h3 style="margin-top:16px">${esc(state.product.name)}</h3><p class="small muted">${esc(state.product.color)} · Size ${esc(state.size)}</p><div class="row between"><span class="price">${money(state.product.price)}</span><span class="small">${esc(state.sourceStore || 'FitCart')}</span></div><p class="micro muted" style="margin:14px 0 18px">A visual preview cannot guarantee physical fit, exact scale, colour, texture, or product availability.</p>${state.sourceUrl ? `<a class="btn wide" href="${esc(state.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open original product ${icon('arrow')}</a>` : ''}<button class="btn secondary wide" data-action="saveToWardrobe" style="margin-top:12px">Save to my shopping wardrobe</button><button class="linkbtn" data-action="backCompare" style="display:block;margin-top:12px">Review product</button></div></section></div>`;
};

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action="shareLive"]');
  if (!target) return;
  try {
    if (navigator.share) await navigator.share({ title: 'My FitCart look', text: 'My virtual try-on from FitCart', url: state.resultImageUrl });
    else {
      await navigator.clipboard.writeText(state.resultImageUrl);
      toast('Private result link copied. It expires automatically.');
    }
  } catch (error) {
    if (error.name !== 'AbortError') toast('Sharing is unavailable. Open the full image instead.');
  }
});

// Wake the API as soon as the page opens: a sleeping Render instance can take close to a minute to start.
fetch(apiUrl('/health'), { cache: 'no-store' }).catch(() => {});
try {
  session().catch(() => {});
} catch {}
