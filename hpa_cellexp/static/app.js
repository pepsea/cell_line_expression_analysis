/* HPA Cell Line Expression Explorer - front end.
 *
 * No build step and no dependencies: the page is served straight from
 * hpa_cellexp/static.  The heatmap is drawn on a single canvas sized to the
 * viewport with frozen row/column headers, so 200 genes x 1,200 cell lines
 * stays smooth (only visible cells are painted).
 */
'use strict';

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

const state = {
  meta: null,
  metric: 'ntpm',
  selected: { organs: new Set(), species: new Set(), diseases: new Set() },
  cellQuery: '',
  result: null,      // server payload
  view: [],          // column order (indices into result.cellLines)
  sort: 'organ',
  sortGene: 0,
  scale: 'global',
  hover: null,
  maxima: null,      // memoised scaleMaxima(); invalidated when the result changes
  tableDirty: true,  // the table view is built lazily - it is the expensive one
};

// Rendering every cell line as DOM is unusable past a few hundred rows, and a
// 1,200 x 200 table is not something anyone reads on screen anyway - beyond
// this the heatmap and the TSV export are the right tools.
const TABLE_ROW_LIMIT = 500;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// colour scale - validated sequential blue ramp, 100 -> 700
// ---------------------------------------------------------------------------

const RAMP = [
  '#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5',
  '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b',
].map((hex) => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
]);

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function isDarkMode() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'dark') return true;
  if (explicit === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/** Map t in [0,1] onto the sequential ramp with linear interpolation.
 *
 * A sequential scale must let "near zero" recede toward the chart surface, so
 * the ramp is traversed 100 -> 700 on the light surface and 700 -> 100 on the
 * dark one.  Same hue, same validated steps - the direction is what the
 * surface selects, not a filter or an automatic inversion of the colours.
 */
function rampColor(t, dark) {
  const isDark = dark === undefined ? isDarkMode() : dark;
  let clamped = Math.max(0, Math.min(1, t));
  if (isDark) clamped = 1 - clamped;
  const pos = clamped * (RAMP.length - 1);
  const i = Math.floor(pos);
  const j = Math.min(i + 1, RAMP.length - 1);
  const f = pos - i;
  const c = [0, 1, 2].map((k) => Math.round(RAMP[i][k] + (RAMP[j][k] - RAMP[i][k]) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** log1p-normalised position of `value` on a scale topping out at `max`. */
function scalePosition(value, max) {
  if (!(max > 0)) return 0;
  return Math.log10(1 + value) / Math.log10(1 + max);
}

function formatValue(v) {
  if (v === null || v === undefined) return '—';
  if (v === 0) return '0';
  if (v >= 1000) return Math.round(v).toLocaleString('en-US');
  if (v >= 10) return v.toFixed(0);
  if (v >= 1) return v.toFixed(1);
  return v.toFixed(2);
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

async function init() {
  restoreTheme();
  $('themeToggle').addEventListener('click', toggleTheme);

  let meta;
  try {
    meta = await fetchJSON('/api/meta');
  } catch (err) {
    showState('placeholder', 'データベースを読み込めませんでした。<br>' + escapeHtml(err.message), true);
    $('datasetChip').textContent = 'データベース未構築';
    return;
  }
  state.meta = meta;

  $('datasetChip').textContent =
    `${meta.release} · ${meta.geneCount.toLocaleString()} 遺伝子 × ${meta.cellLineCount.toLocaleString()} 細胞株`;
  if (meta.isDemo) $('demoBanner').classList.add('on');

  buildMetricRadios(meta.availableMetrics);
  buildFacet('speciesList', meta.facets.species, 'species', { preselectAll: true });
  buildFacet('organList', meta.facets.organs, 'organs');
  buildFacet('diseaseList', meta.facets.diseases, 'diseases');

  $('organSearch').addEventListener('input', (e) => filterFacet('organList', e.target.value));
  $('diseaseSearch').addEventListener('input', (e) => filterFacet('diseaseList', e.target.value));
  $('cellQuery').addEventListener('input', debounce((e) => {
    state.cellQuery = e.target.value;
    refreshMatchCount();
  }, 250));

  document.querySelectorAll('.facet-actions button').forEach((button) => {
    button.addEventListener('click', () => bulkFacet(button.dataset.facet, button.dataset.action));
  });

  $('geneInput').addEventListener('input', debounce(updateGeneChips, 250));
  $('controls').addEventListener('submit', (e) => { e.preventDefault(); runAnalysis(); });

  $('tabHeatmap').addEventListener('click', () => setView('heatmap'));
  $('tabTable').addEventListener('click', () => setView('table'));
  $('sortSelect').addEventListener('change', (e) => { state.sort = e.target.value; applySort(); });
  $('sortGene').addEventListener('change', (e) => { state.sortGene = Number(e.target.value); applySort(); });
  $('scaleSelect').addEventListener('change', (e) => { state.scale = e.target.value; renderHeatmap(); renderLegend(); });
  $('downloadButton').addEventListener('click', downloadTsv);

  setupHeatmapEvents();
  window.addEventListener('resize', debounce(renderHeatmap, 120));

  restoreFromHash();
  refreshMatchCount();
  updateGeneChips();
}

// ---------------------------------------------------------------------------
// sidebar
// ---------------------------------------------------------------------------

function buildMetricRadios(metrics) {
  const labels = { ntpm: 'nTPM', ptpm: 'pTPM', tpm: 'TPM' };
  const group = $('metricGroup');
  group.innerHTML = '';
  metrics.forEach((metric, index) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'metric';
    input.value = metric;
    input.checked = index === 0;
    if (index === 0) state.metric = metric;
    input.addEventListener('change', () => { state.metric = metric; });
    const span = document.createElement('span');
    span.textContent = labels[metric] || metric;
    label.append(input, span);
    group.append(label);
  });
}

function buildFacet(containerId, values, key, options = {}) {
  const container = $(containerId);
  container.innerHTML = '';
  if (!values.length) {
    container.innerHTML = '<p class="hint" style="padding:6px">該当する情報がありません</p>';
    return;
  }
  values.forEach(({ value, count }) => {
    const item = document.createElement('label');
    item.className = 'facet-item';
    item.dataset.value = value.toLowerCase();

    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = value;
    if (options.preselectAll && values.length === 1) {
      // A single species (the usual case for HPA) is selected by default so
      // the filter is visible without being an obstacle.
      input.checked = true;
      state.selected[key].add(value);
    }
    input.addEventListener('change', () => {
      if (input.checked) state.selected[key].add(value);
      else state.selected[key].delete(value);
      refreshMatchCount();
    });

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = value;
    name.title = value;

    const badge = document.createElement('span');
    badge.className = 'count';
    badge.textContent = count.toLocaleString();

    item.append(input, name, badge);
    container.append(item);
  });
}

function filterFacet(containerId, query) {
  const needle = query.trim().toLowerCase();
  $(containerId).querySelectorAll('.facet-item').forEach((item) => {
    item.style.display = !needle || item.dataset.value.includes(needle) ? '' : 'none';
  });
}

function bulkFacet(facet, action) {
  const map = { organ: ['organList', 'organs'], disease: ['diseaseList', 'diseases'] };
  const [containerId, key] = map[facet];
  $(containerId).querySelectorAll('.facet-item').forEach((item) => {
    if (item.style.display === 'none') return;   // only the visible (searched) subset
    const input = item.querySelector('input');
    input.checked = action === 'all';
    if (input.checked) state.selected[key].add(input.value);
    else state.selected[key].delete(input.value);
  });
  refreshMatchCount();
}

function currentFilters() {
  return {
    organs: [...state.selected.organs],
    species: [...state.selected.species],
    diseases: [...state.selected.diseases],
    cellLineQuery: state.cellQuery || null,
  };
}

const refreshMatchCount = debounce(async () => {
  const filters = currentFilters();
  const params = new URLSearchParams();
  filters.organs.forEach((v) => params.append('organ', v));
  filters.species.forEach((v) => params.append('species', v));
  filters.diseases.forEach((v) => params.append('disease', v));
  if (filters.cellLineQuery) params.set('q', filters.cellLineQuery);
  params.set('limit', '1');
  try {
    const data = await fetchJSON('/api/cell-lines?' + params.toString());
    $('matchCount').innerHTML = `対象細胞株: <strong>${data.total.toLocaleString()}</strong> / ${state.meta.cellLineCount.toLocaleString()}`;
  } catch (err) {
    $('matchCount').textContent = '対象細胞株: 取得できませんでした';
  }
}, 200);

function geneTokens() {
  return $('geneInput').value.split(/[\s,;|]+/).map((t) => t.trim()).filter(Boolean);
}

function updateGeneChips() {
  const tokens = geneTokens();
  const chips = $('geneChips');
  chips.innerHTML = '';
  const seen = new Set();
  tokens.forEach((token) => {
    const key = token.toUpperCase();
    if (seen.has(key)) return;
    seen.add(key);
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = token;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = `${token} を削除`;
    remove.addEventListener('click', () => {
      const remaining = geneTokens().filter((t) => t.toUpperCase() !== key);
      $('geneInput').value = remaining.join(', ');
      updateGeneChips();
    });
    chip.append(remove);
    chips.append(chip);
  });
  const count = seen.size;
  $('runButton').disabled = count === 0;
  $('runHint').textContent = count === 0
    ? '遺伝子を入力してください。'
    : `${count} 遺伝子を解析します。`;
}

// ---------------------------------------------------------------------------
// analysis
// ---------------------------------------------------------------------------

function requestBody() {
  return { genes: geneTokens(), metric: state.metric, ...currentFilters() };
}

async function runAnalysis() {
  const tokens = geneTokens();
  if (!tokens.length) return;

  $('runButton').disabled = true;
  showState('placeholder', '解析中…');
  $('hmViewport').hidden = true;

  try {
    const data = await fetchJSON('/api/expression', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody()),
    });
    state.result = data;
    state.maxima = null;
    state.sortGene = 0;
    saveToHash();
    renderGeneChipsWithMatches(data);
    populateSortGene(data);

    if (!data.cellLines.length) {
      showState('placeholder', 'フィルタ条件に一致する細胞株がありません。絞り込みを緩めてください。');
      $('downloadButton').disabled = true;
      $('legend').hidden = true;
      renderTable();
      return;
    }
    if (!data.genes.length) {
      showState('placeholder', '入力された遺伝子がデータセット内に見つかりませんでした。');
      $('downloadButton').disabled = true;
      $('legend').hidden = true;
      renderTable();
      return;
    }

    applySort();
    $('downloadButton').disabled = false;
  } catch (err) {
    showState('placeholder', escapeHtml(err.message), true);
  } finally {
    $('runButton').disabled = false;
  }
}

function renderGeneChipsWithMatches(data) {
  const chips = $('geneChips');
  chips.innerHTML = '';
  data.genes.forEach((gene) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = gene.symbol;
    chip.title = gene.ensemblId ? `${gene.symbol} (${gene.ensemblId})` : gene.symbol;
    chips.append(chip);
  });
  data.unmatchedGenes.forEach((token) => {
    const chip = document.createElement('span');
    chip.className = 'chip miss';
    chip.textContent = token;
    chip.title = 'データセット内に見つかりません';
    chips.append(chip);
  });
  if (data.unmatchedGenes.length) {
    $('runHint').textContent = `${data.genes.length} 遺伝子がヒット、${data.unmatchedGenes.length} 遺伝子は未検出。`;
  } else {
    $('runHint').textContent = `${data.genes.length} 遺伝子を表示中。`;
  }
}

function populateSortGene(data) {
  const select = $('sortGene');
  select.innerHTML = '';
  data.genes.forEach((gene, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = gene.symbol;
    select.append(option);
  });
  const multi = data.genes.length > 1;
  const byValue = state.sort.startsWith('value');
  select.hidden = !(multi && byValue);
  $('sortGeneLabel').hidden = select.hidden;
}

function applySort() {
  const data = state.result;
  if (!data) return;
  const n = data.cellLines.length;
  const order = Array.from({ length: n }, (_, i) => i);
  const g = Math.min(state.sortGene, Math.max(data.genes.length - 1, 0));

  if (state.sort === 'name') {
    order.sort((a, b) => data.cellLines[a].name.localeCompare(data.cellLines[b].name));
  } else if (state.sort === 'organ') {
    order.sort((a, b) => {
      const A = data.cellLines[a], B = data.cellLines[b];
      const oa = A.organ || '￿', ob = B.organ || '￿';
      return oa.localeCompare(ob) || A.name.localeCompare(B.name);
    });
  } else if (data.genes.length) {
    const sign = state.sort === 'value-desc' ? -1 : 1;
    order.sort((a, b) => {
      const va = data.values[g][a], vb = data.values[g][b];
      // Missing values always sink to the bottom, in both directions.
      if (va === null && vb === null) return data.cellLines[a].name.localeCompare(data.cellLines[b].name);
      if (va === null) return 1;
      if (vb === null) return -1;
      return sign * (va - vb) || data.cellLines[a].name.localeCompare(data.cellLines[b].name);
    });
  }

  state.view = order;
  state.tableDirty = true;
  populateSortGene(data);
  renderLegend();
  if (!$('viewTable').hidden) renderTable();
  // Unhide before painting: the canvas is sized from the viewport's client
  // box, which is 0x0 while the element is still hidden.
  $('placeholder').style.display = 'none';
  $('hmViewport').hidden = false;
  renderHeatmap();
}

// ---------------------------------------------------------------------------
// heatmap
// ---------------------------------------------------------------------------

const HM = { gutter: 150, band: 132, cw: 22, ch: 24 };

/** Row and global maxima, memoised - paint() runs on every mousemove and
 *  scanning 200 x 1,200 values each time would make hovering feel sticky. */
function scaleMaxima() {
  if (state.maxima) return state.maxima;
  const data = state.result;
  const globalMax = { global: 0, rows: [] };
  data.genes.forEach((_, r) => {
    let rowMax = 0;
    const row = data.values[r];
    for (let c = 0; c < row.length; c += 1) {
      const v = row[c];
      if (v !== null && v > rowMax) rowMax = v;
    }
    globalMax.rows.push(rowMax);
    if (rowMax > globalMax.global) globalMax.global = rowMax;
  });
  state.maxima = globalMax;
  return globalMax;
}

function renderHeatmap() {
  const data = state.result;
  const viewport = $('hmViewport');
  const canvas = $('hmCanvas');
  if (!data || !data.genes.length || !state.view.length) return;

  const rows = data.genes.length;
  const cols = state.view.length;

  const available = Math.max(viewport.clientWidth - HM.gutter, 120);
  HM.cw = Math.max(8, Math.min(30, Math.floor(available / cols)));

  // The viewport's only in-flow child is the sticky canvas, so its height has
  // to be set explicitly - otherwise element and canvas size each other in a
  // circle and collapse to the CSS min-height.
  const maxHeight = Math.max(300, window.innerHeight - 300);
  HM.ch = Math.max(15, Math.min(30, Math.floor((maxHeight - HM.band) / Math.max(rows, 1))));

  const contentW = HM.gutter + cols * HM.cw;
  const contentH = HM.band + rows * HM.ch;
  $('hmSizer').style.width = contentW + 'px';
  $('hmSizer').style.height = contentH + 'px';

  const scrollbar = contentW > viewport.clientWidth ? 14 : 0;
  viewport.style.height = Math.min(contentH + scrollbar, maxHeight) + 'px';

  const vw = Math.min(viewport.clientWidth, contentW);
  const vh = Math.min(viewport.clientHeight, contentH);
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = vw + 'px';
  canvas.style.height = vh + 'px';
  canvas.width = Math.round(vw * dpr);
  canvas.height = Math.round(vh * dpr);

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  paint(ctx, vw, vh);
}

function paint(ctx, vw, vh) {
  const data = state.result;
  const rows = data.genes.length;
  const cols = state.view.length;
  const viewport = $('hmViewport');
  const sx = viewport.scrollLeft;
  const sy = viewport.scrollTop;

  const surface = cssVar('--surface');
  const ink = cssVar('--ink');
  const ink2 = cssVar('--ink-2');
  const muted = cssVar('--ink-muted');
  const grid = cssVar('--grid');
  const axis = cssVar('--axis');
  const zero = cssVar('--zero');
  const nodata = cssVar('--nodata');

  ctx.fillStyle = surface;
  ctx.fillRect(0, 0, vw, vh);

  const dark = isDarkMode();
  const maxima = scaleMaxima();
  const first = Math.max(0, Math.floor(sx / HM.cw));
  const last = Math.min(cols - 1, Math.ceil((sx + vw - HM.gutter) / HM.cw));
  const firstRow = Math.max(0, Math.floor(sy / HM.ch));
  const lastRow = Math.min(rows - 1, Math.ceil((sy + vh - HM.band) / HM.ch));

  // --- cells --------------------------------------------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(HM.gutter, HM.band, vw - HM.gutter, vh - HM.band);
  ctx.clip();

  for (let r = firstRow; r <= lastRow; r += 1) {
    const y = HM.band + r * HM.ch - sy;
    const rowValues = data.values[r];
    const max = state.scale === 'row' ? maxima.rows[r] : maxima.global;
    for (let c = first; c <= last; c += 1) {
      const x = HM.gutter + c * HM.cw - sx;
      const v = rowValues[state.view[c]];
      ctx.fillStyle = v === null || v === undefined
        ? nodata
        : (v === 0 ? zero : rampColor(scalePosition(v, max), dark));
      // 1px surface gap between fills keeps adjacent cells legible.
      ctx.fillRect(x, y, HM.cw - 1, HM.ch - 1);
    }
  }

  // organ group separators, carried through the plot so the grouping the sort
  // implies is actually visible in the marks
  if (state.sort === 'organ') {
    ctx.strokeStyle = axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let c = Math.max(first, 1); c <= last; c += 1) {
      if (data.cellLines[state.view[c]].organ !== data.cellLines[state.view[c - 1]].organ) {
        const x = HM.gutter + c * HM.cw - sx - 0.5;
        ctx.moveTo(x, HM.band);
        ctx.lineTo(x, vh);
      }
    }
    ctx.stroke();
  }

  // hover ring (2px surface ring + ink outline, per mark spec)
  if (state.hover && state.hover.kind === 'cell') {
    const { row, col } = state.hover;
    if (row >= firstRow && row <= lastRow && col >= first && col <= last) {
      const x = HM.gutter + col * HM.cw - sx;
      const y = HM.band + row * HM.ch - sy;
      ctx.lineWidth = 2;
      ctx.strokeStyle = ink;
      ctx.strokeRect(x + 0.5, y + 0.5, HM.cw - 2, HM.ch - 2);
    }
  }
  ctx.restore();

  // --- column labels (cell lines) -----------------------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(HM.gutter, 0, vw - HM.gutter, HM.band);
  ctx.clip();
  ctx.fillStyle = surface;
  ctx.fillRect(HM.gutter, 0, vw - HM.gutter, HM.band);

  const labelSize = Math.max(9, Math.min(12, HM.cw));
  ctx.font = `${labelSize}px ${cssVar('--font') || 'system-ui, sans-serif'}`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';

  // Labels are rotated -60 degrees, so the gap between neighbours measured
  // perpendicular to the text is cw * sin(60 deg).  Below ~11px they collide
  // into an unreadable smear, so thin them out; the tooltip and the table view
  // still name every column.
  const labelStep = Math.max(1, Math.ceil(11 / (HM.cw * 0.866)));

  // Seed from the column just off-screen so scrolling does not paint a
  // spurious organ boundary at the left edge.
  let lastOrgan = first > 0 ? data.cellLines[state.view[first - 1]].organ : null;
  for (let c = first; c <= last; c += 1) {
    const cell = data.cellLines[state.view[c]];
    const x = HM.gutter + c * HM.cw - sx;

    // organ boundary rule - a hairline where the organ changes
    if (state.sort === 'organ' && cell.organ !== lastOrgan) {
      ctx.strokeStyle = axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - 0.5, 0);
      ctx.lineTo(x - 0.5, HM.band);
      ctx.stroke();
      lastOrgan = cell.organ;
    }

    if (c % labelStep === 0) {
      ctx.save();
      ctx.translate(x + HM.cw / 2 + 3, HM.band - 6);
      ctx.rotate(-Math.PI / 3);
      ctx.fillStyle = ink2;
      ctx.fillText(truncate(ctx, cell.name, HM.band - 14), 0, 0);
      ctx.restore();
    }
  }

  // The hovered column's label is drawn last, over a surface chip, so it stays
  // readable even where the labels are thinned out.
  const hoverCol = state.hover && state.hover.col;
  if (hoverCol !== null && hoverCol !== undefined && hoverCol >= first && hoverCol <= last) {
    const label = truncate(ctx, data.cellLines[state.view[hoverCol]].name, HM.band - 14);
    ctx.save();
    ctx.translate(HM.gutter + hoverCol * HM.cw - sx + HM.cw / 2 + 3, HM.band - 6);
    ctx.rotate(-Math.PI / 3);
    const width = ctx.measureText(label).width;
    ctx.fillStyle = surface;
    ctx.fillRect(-3, -labelSize, width + 6, labelSize + 4);
    ctx.fillStyle = ink;
    ctx.fillText(label, 0, 0);
    ctx.restore();
  }
  ctx.restore();

  // --- row labels (genes) ---------------------------------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, HM.band, HM.gutter, vh - HM.band);
  ctx.clip();
  ctx.fillStyle = surface;
  ctx.fillRect(0, HM.band, HM.gutter, vh - HM.band);
  ctx.font = `600 ${Math.max(11, Math.min(13, HM.ch - 8))}px ${cssVar('--font') || 'system-ui, sans-serif'}`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let r = firstRow; r <= lastRow; r += 1) {
    const y = HM.band + r * HM.ch - sy + HM.ch / 2;
    ctx.fillStyle = state.hover && state.hover.row === r ? ink : ink2;
    ctx.fillText(truncate(ctx, data.genes[r].symbol, HM.gutter - 16), HM.gutter - 10, y);
  }
  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(HM.gutter - 0.5, HM.band);
  ctx.lineTo(HM.gutter - 0.5, vh);
  ctx.stroke();
  ctx.restore();

  // --- corner ---------------------------------------------------------------
  ctx.fillStyle = surface;
  ctx.fillRect(0, 0, HM.gutter, HM.band);
  ctx.fillStyle = muted;
  ctx.font = `11px ${cssVar('--font') || 'system-ui, sans-serif'}`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'bottom';
  ctx.fillText('遺伝子 ＼ 細胞株', HM.gutter - 10, HM.band - 8);
}

function truncate(ctx, text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let out = text;
  while (out.length > 1 && ctx.measureText(out + '…').width > maxWidth) out = out.slice(0, -1);
  return out + '…';
}

/** Repaint the existing canvas without re-measuring or resizing it. */
function repaint() {
  const canvas = $('hmCanvas');
  if (!canvas.width) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  paint(ctx, canvas.width / dpr, canvas.height / dpr);
}

let paintQueued = false;
/** Coalesce scroll/hover repaints onto one frame. */
function schedulePaint() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => { paintQueued = false; repaint(); });
}

function setupHeatmapEvents() {
  const viewport = $('hmViewport');
  const tooltip = $('hmTooltip');

  viewport.addEventListener('scroll', schedulePaint, { passive: true });

  viewport.addEventListener('mousemove', (event) => {
    const hit = hitTest(event);
    const previous = state.hover;
    state.hover = hit;
    viewport.style.cursor = hit && (hit.kind === 'header' || hit.kind === 'cell') ? 'pointer' : 'default';
    if (!hit || hit.kind === 'corner') {
      tooltip.style.display = 'none';
    } else {
      renderTooltip(tooltip, hit, event);
    }
    const changed = !previous || !hit
      || previous.kind !== hit.kind || previous.row !== hit.row || previous.col !== hit.col;
    if (changed) schedulePaint();
  });

  viewport.addEventListener('mouseleave', () => {
    state.hover = null;
    tooltip.style.display = 'none';
    schedulePaint();
  });

  viewport.addEventListener('click', (event) => {
    const hit = hitTest(event);
    if (!hit || (hit.kind !== 'header' && hit.kind !== 'cell')) return;
    const cell = state.result.cellLines[state.view[hit.col]];
    window.open(cell.databaseUrl, '_blank', 'noopener');
  });
}

function hitTest(event) {
  const data = state.result;
  if (!data || !state.view.length) return null;
  const viewport = $('hmViewport');
  const rect = viewport.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const col = Math.floor((x - HM.gutter + viewport.scrollLeft) / HM.cw);
  const row = Math.floor((y - HM.band + viewport.scrollTop) / HM.ch);
  const inCols = col >= 0 && col < state.view.length;
  const inRows = row >= 0 && row < data.genes.length;

  if (x < HM.gutter && y < HM.band) return { kind: 'corner' };
  if (x < HM.gutter) return inRows ? { kind: 'gene', row } : null;
  if (y < HM.band) return inCols ? { kind: 'header', col } : null;
  if (inCols && inRows) return { kind: 'cell', row, col };
  return null;
}

function renderTooltip(tooltip, hit, event) {
  const data = state.result;
  const viewport = $('hmViewport');
  const rect = viewport.getBoundingClientRect();

  let html = '';
  if (hit.kind === 'gene') {
    const gene = data.genes[hit.row];
    html = `<div class="t-title">${escapeHtml(gene.symbol)}</div>` +
      (gene.ensemblId ? `<div class="t-row">${escapeHtml(gene.ensemblId)}</div>` : '');
  } else {
    const cell = data.cellLines[state.view[hit.col]];
    html = `<div class="t-title">${escapeHtml(cell.name)}</div>`;
    if (cell.organ) html += `<div class="t-row">由来臓器: ${escapeHtml(cell.organ)}</div>`;
    if (cell.disease) html += `<div class="t-row">疾患: ${escapeHtml(cell.disease)}</div>`;
    if (cell.species) html += `<div class="t-row">種: ${escapeHtml(cell.species)}</div>`;
    if (hit.kind === 'cell') {
      const gene = data.genes[hit.row];
      const value = data.values[hit.row][state.view[hit.col]];
      html += `<div class="t-row" style="margin-top:4px">${escapeHtml(gene.symbol)}: ` +
        `<span class="t-value">${formatValue(value)}</span> ${escapeHtml(data.metric)}</div>`;
    }
    html += '<div class="t-row" style="margin-top:4px;opacity:.75">クリックで Cellosaurus を開く</div>';
  }
  tooltip.innerHTML = html;
  tooltip.style.display = 'block';

  const localX = event.clientX - rect.left + viewport.scrollLeft;
  const localY = event.clientY - rect.top + viewport.scrollTop;
  const width = tooltip.offsetWidth;
  const flip = (event.clientX - rect.left) + width + 28 > viewport.clientWidth;
  tooltip.style.left = (flip ? localX - width - 14 : localX + 14) + 'px';
  tooltip.style.top = (localY + 14) + 'px';
}

function renderLegend() {
  const data = state.result;
  const legend = $('legend');
  if (!data || !data.genes.length) { legend.hidden = true; return; }
  legend.hidden = false;

  const dark = isDarkMode();
  const steps = 13;
  const stops = Array.from({ length: steps }, (_, i) => {
    const t = i / (steps - 1);
    return `${rampColor(t, dark)} ${(t * 100).toFixed(1)}%`;
  });
  $('legendBar').style.background = `linear-gradient(90deg, ${stops.join(', ')})`;

  const ticksEl = $('legendTicks');
  ticksEl.innerHTML = '';
  if (state.scale === 'row') {
    ['低', '', '', '', '高'].forEach((label) => {
      const span = document.createElement('span');
      span.textContent = label;
      ticksEl.append(span);
    });
    $('legendUnit').textContent = `${data.metric}（遺伝子ごとに正規化）`;
  } else {
    const max = scaleMaxima().global;
    [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
      const value = Math.pow(10, t * Math.log10(1 + max)) - 1;
      const span = document.createElement('span');
      span.textContent = formatValue(value);
      ticksEl.append(span);
    });
    $('legendUnit').textContent = `${data.metric}（全遺伝子共通スケール）`;
  }
}

// ---------------------------------------------------------------------------
// table view (the accessible, linkable rendering of the same matrix)
// ---------------------------------------------------------------------------

function renderTable() {
  const data = state.result;
  const table = $('matrixTable');
  const thead = table.querySelector('thead');
  const tbody = table.querySelector('tbody');
  thead.innerHTML = '';
  tbody.innerHTML = '';
  if (!data) return;

  const maxima = data.genes.length ? scaleMaxima() : { global: 0, rows: [] };
  const dark = isDarkMode();

  const headRow = document.createElement('tr');
  [['細胞株', 'name'], ['由来臓器', 'organ'], ['疾患', null], ['種', null]].forEach(([label, key]) => {
    const th = document.createElement('th');
    th.className = 'left';
    th.textContent = label;
    if (key) {
      th.addEventListener('click', () => {
        state.sort = key === 'name' ? 'name' : 'organ';
        $('sortSelect').value = state.sort;
        applySort();
      });
      if ((state.sort === 'name' && key === 'name') || (state.sort === 'organ' && key === 'organ')) {
        const arrow = document.createElement('span');
        arrow.className = 'arrow';
        arrow.textContent = '▾';
        th.append(arrow);
      }
    } else {
      th.style.cursor = 'default';
    }
    headRow.append(th);
  });
  data.genes.forEach((gene, index) => {
    const th = document.createElement('th');
    th.textContent = gene.symbol;
    th.title = `${gene.symbol} でソート（${data.metric}）`;
    th.addEventListener('click', () => {
      state.sortGene = index;
      state.sort = state.sort === 'value-desc' && index === state.sortGene ? 'value-asc' : 'value-desc';
      $('sortSelect').value = state.sort;
      $('sortGene').value = String(index);
      applySort();
    });
    if (state.sort.startsWith('value') && state.sortGene === index) {
      const arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = state.sort === 'value-desc' ? '▾' : '▴';
      th.append(arrow);
    }
    headRow.append(th);
  });
  thead.append(headRow);

  const fragment = document.createDocumentFragment();
  const shown = state.view.slice(0, TABLE_ROW_LIMIT);
  shown.forEach((cellIndex) => {
    const cell = data.cellLines[cellIndex];
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    nameTd.className = 'left name';
    const link = document.createElement('a');
    link.className = 'ext-link';
    link.href = cell.databaseUrl;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = cell.name;
    link.title = cell.cellosaurusId
      ? `Cellosaurus ${cell.cellosaurusId} を開く`
      : 'Cellosaurus で検索';
    nameTd.append(link);
    tr.append(nameTd);

    [cell.organ, cell.disease, cell.species].forEach((value) => {
      const td = document.createElement('td');
      td.className = 'left meta';
      td.textContent = value || '—';
      tr.append(td);
    });

    data.genes.forEach((_, r) => {
      const td = document.createElement('td');
      td.className = 'val';
      const value = data.values[r][cellIndex];
      const max = state.scale === 'row' ? maxima.rows[r] : maxima.global;
      if (value !== null && value !== undefined && value > 0) {
        const t = scalePosition(value, max);
        const bar = document.createElement('div');
        bar.className = 'bar';
        bar.style.width = (t * 100).toFixed(1) + '%';
        // Same ramp as the heatmap so the two views encode magnitude alike.
        bar.style.background = rampColor(t, dark);
        bar.style.opacity = dark ? '0.55' : '0.45';
        td.append(bar);
      }
      const span = document.createElement('span');
      span.textContent = formatValue(value);
      td.append(span);
      tr.append(td);
    });

    fragment.append(tr);
  });
  tbody.append(fragment);
  state.tableDirty = false;

  const truncated = state.view.length - shown.length;
  const notice = $('tableNotice');
  if (truncated > 0) {
    notice.hidden = false;
    notice.textContent =
      `先頭 ${TABLE_ROW_LIMIT.toLocaleString()} 細胞株のみ表示しています` +
      `（該当 ${state.view.length.toLocaleString()} 件、残り ${truncated.toLocaleString()} 件）。` +
      `全件はヒートマップまたは TSV ダウンロードでご確認ください。`;
  } else {
    notice.hidden = true;
  }
}

function setView(which) {
  const heat = which === 'heatmap';
  $('tabHeatmap').setAttribute('aria-selected', String(heat));
  $('tabTable').setAttribute('aria-selected', String(!heat));
  $('viewHeatmap').hidden = !heat;
  $('viewTable').hidden = heat;
  $('legend').hidden = !heat || !state.result || !state.result.genes.length;
  if (heat) renderHeatmap();
  else if (state.tableDirty && state.result) renderTable();
}

// ---------------------------------------------------------------------------
// export & sharing
// ---------------------------------------------------------------------------

async function downloadTsv() {
  const button = $('downloadButton');
  button.disabled = true;
  try {
    const response = await fetch('/api/expression.tsv', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody()),
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'hpa_cell_line_expression.tsv';
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert('ダウンロードに失敗しました: ' + err.message);
  } finally {
    button.disabled = false;
  }
}

function saveToHash() {
  const params = new URLSearchParams();
  params.set('g', geneTokens().join(','));
  params.set('m', state.metric);
  if (state.selected.organs.size) params.set('o', [...state.selected.organs].join('|'));
  if (state.selected.species.size) params.set('s', [...state.selected.species].join('|'));
  if (state.selected.diseases.size) params.set('d', [...state.selected.diseases].join('|'));
  if (state.cellQuery) params.set('q', state.cellQuery);
  history.replaceState(null, '', '#' + params.toString());
}

function restoreFromHash() {
  if (!location.hash || location.hash.length < 2) return;
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get('g')) $('geneInput').value = params.get('g').split(',').join(', ');
  if (params.get('q')) { state.cellQuery = params.get('q'); $('cellQuery').value = state.cellQuery; }
  const metric = params.get('m');
  if (metric) {
    const radio = document.querySelector(`#metricGroup input[value="${CSS.escape(metric)}"]`);
    if (radio) { radio.checked = true; state.metric = metric; }
  }
  const restore = (param, key, containerId) => {
    const raw = params.get(param);
    if (!raw) return;
    const wanted = new Set(raw.split('|'));
    state.selected[key] = new Set();
    $(containerId).querySelectorAll('.facet-item input').forEach((input) => {
      input.checked = wanted.has(input.value);
      if (input.checked) state.selected[key].add(input.value);
    });
  };
  restore('o', 'organs', 'organList');
  restore('s', 'species', 'speciesList');
  restore('d', 'diseases', 'diseaseList');
  if (params.get('g')) { updateGeneChips(); runAnalysis(); }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json();
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    return body.detail || `${response.status} ${response.statusText}`;
  } catch (_) {
    return `${response.status} ${response.statusText}`;
  }
}

function showState(id, html, isError) {
  const element = $(id);
  element.innerHTML = html;
  element.className = 'state' + (isError ? ' error' : '');
  element.style.display = '';
  $('hmViewport').hidden = true;
  $('legend').hidden = true;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function restoreTheme() {
  const saved = localStorage.getItem('hpa-cellexp-theme');
  if (saved === 'dark' || saved === 'light') document.documentElement.dataset.theme = saved;
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('hpa-cellexp-theme', next);
  if (state.result) { renderLegend(); renderTable(); }
  renderHeatmap();
}

init();
