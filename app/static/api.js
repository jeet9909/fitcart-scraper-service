/* Live FitCart API adapter for the approved standalone interface. */
'use strict';

const SESSION_KEY = 'fitcart-anonymous-session-v1';
// Empty when the UI is served by the API itself; set by static/config.js to the
// Supabase Edge Function URL when the UI is hosted on GitHub Pages.
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

function productFromApi(item, sourceUrl) {
  const sizes = item.sizes?.filter(Boolean) || [];
  return {
    name: item.title || 'Imported product',
    brand: item.brand || item.store || 'Imported listing',
    color: item.colors?.join(', ') || 'Colour not provided',
    category: item.category || 'Other wearable',
    price: item.price?.amount ?? null,
    sizes: sizes.length ? sizes : ['One size / check store'],
    image: item.image_urls?.[0] || '',
    material: item.material || 'Not provided by store',
    style: item.external_id || 'Not provided',
    sourceUrl,
  };
}

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
};

function categoryForApi(category) {
  return ({ Tops: 'top', Bottoms: 'bottom', Shoes: 'shoes', Watch: 'watch' })[category] || String(category || 'wearable').toLowerCase();
}

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
    form.append('category', categoryForApi(state.product.category));
    form.append('country', 'IN');
    if (state.mode === 'manual') {
      form.append('product_image', dataUrlBlob(state.product.image), 'product.jpg');
    } else {
      form.append('product_page_url', state.sourceUrl);
    }
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
  screen.innerHTML = `<div class="sectionhead"><div><div class="finishmark">${isSample ? 'Sample look' : 'Your virtual try-on is ready'}</div><h2>A new way to picture it.</h2><p class="small muted">${isSample ? 'Pre-made illustration.' : 'AI-generated appearance preview. Always verify size and product details with the retailer.'}</p></div><button class="btn secondary" data-action="editPhotos">Try another photo</button></div><div class="resultgrid"><section><img class="resultcanvas" src="${esc(state.resultImageUrl)}" alt="Your generated FitCart virtual try-on"><div class="row" style="margin-top:14px"><a class="btn secondary" href="${esc(state.resultImageUrl)}" target="_blank" rel="noopener">Open full image</a><button class="btn" data-action="shareLive">Share result</button></div></section><section><div class="card pad"><span class="badge">Saved to private gallery</span><h3 style="margin-top:16px">${esc(state.product.name)}</h3><p class="small muted">${esc(state.product.color)} · Size ${esc(state.size)}</p><div class="row between"><span class="price">${money(state.product.price)}</span><span class="small">${esc(state.sourceStore || 'FitCart')}</span></div><p class="micro muted" style="margin:14px 0 18px">A visual preview cannot guarantee physical fit, exact scale, colour, texture, or product availability.</p>${state.sourceUrl ? `<a class="btn wide" href="${esc(state.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open original product ${icon('arrow')}</a>` : ''}<button class="linkbtn" data-action="backCompare" style="display:block;margin-top:12px">Review product</button></div></section></div>`;
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

try {
  session().catch(() => {});
} catch {}
