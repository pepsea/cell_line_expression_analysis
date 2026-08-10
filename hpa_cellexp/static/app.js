/* HPA Cell Line Expression Explorer - front end.
 *
 * No build step and no dependencies: the page is served straight from
 * hpa_cellexp/static.  The heatmap is drawn on a single canvas sized to the
 * viewport with frozen row/column headers, so 200 genes x 1,200 cell lines
 * stays smooth (only visible cells are painted).
 */
'use strict';

// Baked into the script and compared against the server's version on load.
// A stale Docker image or a cached script is otherwise invisible: the page
// looks fine and simply behaves like an older build, which is impossible to
// tell apart from a bug.  Keep in step with hpa_cellexp/__init__.py.
const APP_VERSION = '1.15.1';

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
  groups: null,      // memoised organGroups(); invalidated when the row order changes
  organJa: {},       // {"Lung": "肺"} - display labels for the heatmap gutter
  organRank: {},     // {"Lung": 14} - canonical (anatomical) display order
  tableDirty: true,  // the table view is built lazily - it is the expensive one
  tableToken: 0,     // cancels a chunked table render that a re-sort superseded
  matchCount: null,  // cell lines the current filters match
};

// The table shows every matching cell line.  It used to stop at 500, which
// made rows genuinely disappear depending on the sort order - the one thing
// this view must never do.
//
// Building 1,200 x 200 cells in one go blocks the page for seconds (measured:
// 1,200 x 60 takes ~1.5s), so the body is filled a chunk at a time.  The chunk
// is sized in CELLS, not rows, so a wide table takes smaller bites and each
// one stays near a frame's worth of work.
const TABLE_CELLS_PER_CHUNK = 2500;

const $ = (id) => document.getElementById(id);

// Digit-aware collation: NCI-H2 must come before NCI-H1650, not after it.
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const byName = (a, b) => NATURAL.compare(a, b) || (a < b ? -1 : a > b ? 1 : 0);

/** Canonical (anatomical) position of an organ; unlisted ones sort last. */
function organRank(organ) {
  if (!organ) return 1e6;
  const rank = state.organRank[organ];
  return rank === undefined ? 1e5 : rank;
}

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
function rampRgb(t, dark) {
  const isDark = dark === undefined ? isDarkMode() : dark;
  let clamped = Math.max(0, Math.min(1, t));
  if (isDark) clamped = 1 - clamped;
  const pos = clamped * (RAMP.length - 1);
  const i = Math.floor(pos);
  const j = Math.min(i + 1, RAMP.length - 1);
  const f = pos - i;
  return [0, 1, 2].map((k) => Math.round(RAMP[i][k] + (RAMP[j][k] - RAMP[i][k]) * f));
}

function rampColor(t, dark) {
  const c = rampRgb(t, dark);
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** Ink for a value printed on top of a filled cell.
 *
 * Picked from the fill's own relative luminance rather than from a position on
 * the ramp, so the mid-tones - where neither black nor white is comfortable -
 * still get whichever of the two actually has more contrast. */
function inkOn(rgb) {
  const channel = (v) => {
    const x = v / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  };
  const luminance = 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
  return luminance > 0.42 ? '#0b0b0b' : '#ffffff';
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
  showVersion(meta.appVersion);

  meta.facets.organs.forEach((o) => {
    if (o.labelJa) state.organJa[o.value] = o.labelJa;
    if (typeof o.rank === 'number') state.organRank[o.value] = o.rank;
  });
  buildSourcePanel(meta);

  buildMetricRadios(meta.availableMetrics);
  buildFacet('speciesList', meta.facets.species, 'species', {
    preselectAll: true,
    englishOnly: true,
  });
  buildFacet('organList', meta.facets.organs, 'organs');
  buildFacet('diseaseList', meta.facets.diseases, 'diseases');

  $('organSearch').addEventListener('input', (e) => filterFacet('organList', e.target.value));
  $('diseaseSearch').addEventListener('input', (e) => filterFacet('diseaseList', e.target.value));
  $('cellQuery').addEventListener('input', debounce((e) => {
    state.cellQuery = e.target.value;
    filtersChanged();
  }, 250));

  document.querySelectorAll('.facet-actions button').forEach((button) => {
    button.addEventListener('click', () => bulkFacet(button.dataset.facet, button.dataset.action));
  });

  $('geneInput').addEventListener('input', debounce(updateGeneChips, 250));
  $('controls').addEventListener('submit', (e) => { e.preventDefault(); runAnalysis(); });

  $('tabHeatmap').addEventListener('click', () => setView('heatmap'));
  $('tabTable').addEventListener('click', () => setView('table'));
  $('sortSelect').addEventListener('change', (e) => { state.sort = e.target.value; applySort(); });
  $('sortGene').addEventListener('change', (e) => {
    state.sortGene = isAggregate(e.target.value) ? e.target.value : Number(e.target.value);
    applySort();
  });
  $('scaleSelect').addEventListener('change', (e) => { state.scale = e.target.value; renderHeatmap(); renderLegend(); });
  $('downloadButton').addEventListener('click', downloadTsv);

  setupHeatmapEvents();
  window.addEventListener('resize', debounce(renderHeatmap, 120));

  $('sidebarToggle').addEventListener('click', () => setSidebar(!sidebarOpen()));
  setSidebar(localStorage.getItem(SIDEBAR_KEY) !== 'closed', { persist: false });

  restoreFromHash();
  refreshMatchCount();
  updateGeneChips();
}

const SIDEBAR_KEY = 'hpa-cellexp-sidebar';

function sidebarOpen() {
  return !$('controls').hidden;
}

/** Show or hide the filter panel.
 *
 *  Collapsing it hands the whole width to the sheet, which is the point - so
 *  the heatmap has to be re-measured afterwards, and only once the browser has
 *  applied the new grid.  While it is closed the button carries the match
 *  count, because the count itself lives inside the panel.
 */
function setSidebar(open, { persist = true } = {}) {
  const panel = $('controls');
  const button = $('sidebarToggle');
  panel.hidden = !open;
  document.querySelector('.layout').classList.toggle('no-sidebar', !open);
  button.setAttribute('aria-expanded', String(open));
  button.querySelector('.chev').textContent = open ? '◀' : '▶';
  button.title = open ? '絞り込みパネルを隠す' : '絞り込みパネルを表示';
  if (persist) localStorage.setItem(SIDEBAR_KEY, open ? 'open' : 'closed');
  // The grid column only exists after a layout pass.
  requestAnimationFrame(() => renderHeatmap());
}


/** "LUAD 肺腺がん (\u03c1 0.79)" - one TCGA cohort a cell line resembles. */
function tcgaLabel(hit) {
  const name = hit.nameJa || hit.name;
  const rho = typeof hit.spearman === 'number' ? ` (\u03c1 ${hit.spearman.toFixed(2)})` : '';
  return `${hit.cancer}${name ? ` ${name}` : ''}${rho}`;
}

/** Show which build is on screen, and say so loudly when it is not the one
 *  the server is running.
 *
 *  Without this a stale image or a cached script is undiagnosable from the
 *  page: every fixed bug still reproduces and there is nothing to look at.
 *  The badge itself is the primary signal - if it is missing entirely, the
 *  browser is running a script from before this existed.
 */
function showVersion(serverVersion) {
  const badge = $('versionBadge');
  if (!badge) return;
  badge.hidden = false;
  const stale = serverVersion && serverVersion !== APP_VERSION;
  badge.textContent = stale ? `v${APP_VERSION} ≠ v${serverVersion}` : `v${APP_VERSION}`;
  badge.classList.toggle('stale', Boolean(stale));
  badge.title = stale
    ? `表示中のページは v${APP_VERSION}、サーバーは v${serverVersion} です。`
      + '\n古いスクリプトがキャッシュされています。スーパーリロード'
      + '（Mac: Cmd+Shift+R / Win: Ctrl+F5）してください。'
    : `hpa_cellexp v${APP_VERSION}`;
  if (stale) {
    const banner = $('staleBanner');
    if (banner) {
      banner.textContent =
        `このページは古いバージョン (v${APP_VERSION}) を表示しています。`
        + `サーバーは v${serverVersion} です。スーパーリロード（Cmd+Shift+R）してください。`;
      banner.hidden = false;
    }
  }
}

/** "Which files is this built from?" - shown behind the dataset chip. */
function buildSourcePanel(meta) {
  const panel = $('sourcePanel');
  const chip = $('datasetChip');

  const bytes = (n) => {
    if (!n && n !== 0) return '';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let value = n;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  };

  const fileRow = (label, file) => {
    if (!file) return `<dt>${label}</dt><dd class="none">（未使用）</dd>`;
    const detail = [file.bytes ? bytes(file.bytes) : null, file.modified].filter(Boolean).join(' · ');
    return `<dt>${label}</dt><dd>` +
      `<span class="file">${escapeHtml(file.name || '')}</span>` +
      (detail ? ` <span class="none">(${escapeHtml(detail)})</span>` : '') +
      (file.path && file.path !== file.name ? `<span class="path">${escapeHtml(file.path)}</span>` : '') +
      '</dd>';
  };

  const sources = meta.sources || {};
  const metadataFiles = sources.metadata || [];
  const metadataRows = metadataFiles.length
    ? metadataFiles.map((f, i) => fileRow(i === 0 ? '細胞株メタデータ' : '　〃', f)).join('')
    : fileRow('細胞株メタデータ', null);

  panel.innerHTML =
    '<h2>このサイトが使用しているデータ</h2>' +
    (meta.isDemo
      ? '<p class="warn" style="margin:0 0 10px">⚠️ デモデータです。発現値は合成値で、実測値ではありません。</p>'
      : '') +
    '<dl>' +
      fileRow('発現マトリクス', sources.expression) +
      metadataRows +
      fileRow('Cellosaurus', sources.cellosaurus) +
      fileRow('TCGA比較', sources.tcga) +
    '</dl><hr><dl>' +
      `<dt>リリース</dt><dd>${escapeHtml(meta.release || '-')}</dd>` +
      `<dt>収録内容</dt><dd>${meta.geneCount.toLocaleString()} 遺伝子 × ` +
        `${meta.cellLineCount.toLocaleString()} 細胞株 / ` +
        `${meta.expressionRows.toLocaleString()} 行</dd>` +
      `<dt>指標</dt><dd>${escapeHtml((meta.metrics || []).join(', ') || '-')}</dd>` +
      (meta.builtAt ? `<dt>構築日時</dt><dd>${escapeHtml(meta.builtAt)} (UTC)</dd>` : '') +
      `<dt>データベース</dt><dd><span class="path">${escapeHtml(meta.databasePath || '-')}</span></dd>` +
    '</dl>';

  const close = () => { panel.hidden = true; chip.setAttribute('aria-expanded', 'false'); };
  chip.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = panel.hidden;
    panel.hidden = !open;
    chip.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', (event) => {
    if (!panel.hidden && !panel.contains(event.target)) close();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
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
    input.addEventListener('change', () => {
      state.metric = metric;
      if (state.result) rerunFromFilters();
    });
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
  values.forEach(({ value, count, labelJa, searchTerms }) => {
    const item = document.createElement('label');
    item.className = 'facet-item';
    // Searchable in either language: the English value is the filter key, the
    // Japanese label is what most users here will actually type.  searchTerms
    // adds related words that are not on screen - typing 脳 has to surface
    // 末梢神経系 too, because that is where a neuroblastoma line is filed.
    item.dataset.value = [value, labelJa, searchTerms].filter(Boolean).join(' ').toLowerCase();

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
      filtersChanged();
    });

    const name = document.createElement('span');
    name.className = 'name';
    // Sentinel values (e.g. the "unassigned organ" bucket) have no real name
    // to show alongside the label.
    const sentinel = value.startsWith('__');
    if (options.englishOnly && !sentinel) {
      // Species: the scientific name is the name.  Showing "ヒト Homo sapiens"
      // read as two separate entries for the same species.
      name.textContent = value;
    } else if (labelJa && !sentinel) {
      name.textContent = labelJa;
      const original = document.createElement('span');
      original.className = 'name-en';
      original.textContent = value;
      name.append(' ', original);
    } else {
      name.textContent = labelJa || value;
    }
    name.title = labelJa && !sentinel ? `${labelJa} (${value})` : (labelJa || value);
    if (sentinel) item.dataset.value = (labelJa || '').toLowerCase();

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
  filtersChanged();
}

function currentFilters() {
  return {
    organs: [...state.selected.organs],
    species: [...state.selected.species],
    diseases: [...state.selected.diseases],
    cellLineQuery: state.cellQuery || null,
  };
}

/** Called whenever a cell line filter changes.
 *
 * Updating only the match count made the filters look broken: the number moved
 * but the chart kept showing the previous selection until you pressed the run
 * button again.  Once a result is on screen the filters re-run it.
 */
function filtersChanged() {
  refreshMatchCount();
  if (state.result) rerunFromFilters();
}

const rerunFromFilters = debounce(() => {
  if (state.result && geneTokens().length) runAnalysis({ keepScroll: true });
}, 350);

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
    state.matchCount = data.total;
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

async function runAnalysis(options = {}) {
  const tokens = geneTokens();
  if (!tokens.length) return;

  $('runButton').disabled = true;
  // A live re-run driven by a filter change leaves the current chart on screen
  // rather than flashing the placeholder, so adjusting filters feels like
  // filtering rather than like re-running.
  const keepScroll = Boolean(options.keepScroll) && Boolean(state.result);
  const scrollTop = keepScroll ? $('hmViewport').scrollTop : 0;
  if (!keepScroll) {
    showState('placeholder', '解析中…');
    $('hmViewport').hidden = true;
  }

  try {
    const data = await fetchJSON('/api/expression', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody()),
    });
    state.result = data;
    state.maxima = null;
    state.groups = null;
    // Aggregate bases survive a re-run; they do not name a specific gene.
    if (!keepScroll && !isAggregate(state.sortGene)) state.sortGene = 0;
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
    if (keepScroll) $('hmViewport').scrollTop = scrollTop;
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

/* Aggregate sort bases.
 *
 * A plain arithmetic mean is dominated by whichever gene has the largest
 * absolute values - add GAPDH to the list and the ranking becomes a GAPDH
 * ranking.  The other two bases avoid that in different ways:
 *
 *   share-mean: for each gene, the cell line's share of that gene's total over
 *               the cell lines on screen; averaged over the genes.  Every gene
 *               contributes exactly 1.0 spread across the cell lines, so a
 *               high-abundance gene carries no more weight than a faint one.
 *               (Two other readings of "share" are degenerate and deliberately
 *               not used: a cell line's share across the selected genes always
 *               averages to 1/N, and a share of the transcriptome - nTPM/1e6 -
 *               reproduces the arithmetic mean exactly.)
 *   norm-min  : the SMALLEST per-gene normalised value - the bottleneck gene,
 *               so a cell line only ranks high when EVERY gene is high.
 *               This is the one to use for "全遺伝子が満遍なく発現".
 *
 * Genes that are flat zero across the whole selection carry no information and
 * are skipped, otherwise they would drag every cell line's minimum to 0.
 */
const SORT_MEAN = 'mean';
const SORT_SHARE_MEAN = 'share-mean';
const SORT_NORM_MIN = 'norm-min';

const SORT_BASES = [
  { value: SORT_NORM_MIN,
    label: '全遺伝子が満遍なく（最小値）',
    help: '遺伝子ごとに0-1へ正規化し、その最小値で並べます。最も弱い遺伝子でも高い細胞株が上位に来ます。' },
  { value: SORT_SHARE_MEAN,
    label: '発現量割合の平均',
    help: '遺伝子ごとに「表示中の細胞株の合計に対するその細胞株の割合」を求め、'
        + '遺伝子間で平均します。各遺伝子の合計が 1 になるため、'
        + '高発現遺伝子でも低発現遺伝子でも同じ重みで効きます。' },
  { value: SORT_MEAN,
    label: '全遺伝子の平均（絶対値）',
    help: '生の発現量の算術平均。GAPDH のような高発現遺伝子に強く影響されます。' },
];

const AGGREGATE_BASES = new Set(SORT_BASES.map((b) => b.value));

function isAggregate(basis) {
  return AGGREGATE_BASES.has(basis);
}

function populateSortGene(data) {
  const select = $('sortGene');
  const wanted = data.genes.map((g) => g.symbol).join('|');

  // Only rebuild when the gene list actually changed.  Rebuilding on every
  // sort reset the visible selection to the first option while state.sortGene
  // still pointed elsewhere - after which re-picking that first gene fired no
  // 'change' event at all, so it could never be chosen again.
  if (select.dataset.genes !== wanted) {
    select.dataset.genes = wanted;
    select.innerHTML = '';
    if (data.genes.length > 1) {
      const group = document.createElement('optgroup');
      // Named after the control, like the static selects: the heading is bold
      // and unselectable, so it labels without taking toolbar space.
      group.label = '基準遺伝子 · 全遺伝子をまとめて';
      SORT_BASES.forEach((basis) => {
        const option = document.createElement('option');
        option.value = basis.value;
        option.textContent = basis.label;
        option.title = basis.help;
        group.append(option);
      });
      select.append(group);
      const genes = document.createElement('optgroup');
      genes.label = '基準遺伝子 · 個別の遺伝子';
      select.append(genes);
    }
    let geneParent = select.lastElementChild;
    if (!geneParent || geneParent.tagName !== 'OPTGROUP') {
      // One gene: no aggregate bases, but the heading still names the control.
      geneParent = document.createElement('optgroup');
      geneParent.label = '基準遺伝子';
      select.append(geneParent);
    }
    data.genes.forEach((gene, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = gene.symbol;
      geneParent.append(option);
    });
  }

  // Keep the control showing what the view is actually sorted by.
  const value = isAggregate(state.sortGene) ? state.sortGene : String(state.sortGene);
  if (select.value !== value) select.value = value;
  if (select.selectedIndex < 0) {
    select.selectedIndex = 0;
    state.sortGene = isAggregate(select.value) ? select.value : Number(select.value);
  }
  const basis = SORT_BASES.find((b) => b.value === state.sortGene);
  select.title = basis ? basis.help : '選んだ遺伝子の発現量で並べます。';

  const multi = data.genes.length > 1;
  const byValue = state.sort.startsWith('value');
  select.hidden = !(multi && byValue);
}

/** Per-cell-line value the "expression" sorts order by: one gene, or the mean
 *  across every gene in the result. */
function sortKeys(data) {
  const keys = new Array(data.cellLines.length).fill(null);
  if (!data.genes.length) return keys;

  const basis = state.sortGene;
  if (isAggregate(basis)) {
    const maxima = scaleMaxima();
    // A gene that is zero everywhere in the current selection says nothing
    // about any cell line; including it would pin every minimum to 0.
    let genes = [];
    for (let r = 0; r < data.genes.length; r += 1) {
      if (maxima.rows[r] > 0) genes.push(r);
    }
    if (!genes.length) genes = data.genes.map((_, r) => r);

    for (let c = 0; c < keys.length; c += 1) {
      let sum = 0;
      let seen = 0;
      let lowest = Infinity;
      for (let i = 0; i < genes.length; i += 1) {
        const r = genes[i];
        const v = data.values[r][c];
        if (v === null || v === undefined) continue;   // no data: not a zero
        let x;
        if (basis === SORT_MEAN) x = v;
        else if (basis === SORT_SHARE_MEAN) {
          const total = maxima.totals[r];
          x = total > 0 ? v / total : 0;
        } else x = scalePosition(v, maxima.rows[r]);   // per-gene 0-1, for the minimum
        sum += x;
        seen += 1;
        if (x < lowest) lowest = x;
      }
      if (!seen) { keys[c] = null; continue; }
      keys[c] = basis === SORT_NORM_MIN ? lowest : sum / seen;
    }
    return keys;
  }

  const g = Math.min(Math.max(Number(state.sortGene) || 0, 0), data.genes.length - 1);
  for (let c = 0; c < keys.length; c += 1) keys[c] = data.values[g][c];
  return keys;
}

function applySort() {
  const data = state.result;
  if (!data) return;
  const n = data.cellLines.length;
  const order = Array.from({ length: n }, (_, i) => i);

  if (state.sort === 'name') {
    order.sort((a, b) => byName(data.cellLines[a].name, data.cellLines[b].name));
  } else if (state.sort === 'organ') {
    order.sort((a, b) => {
      const A = data.cellLines[a], B = data.cellLines[b];
      // Group in the same anatomical order the labels are listed in, so the
      // sequence on screen matches the labels the reader actually sees.
      return organRank(A.organ) - organRank(B.organ)
        || (A.organ || '').localeCompare(B.organ || '')
        || byName(A.name, B.name);
    });
  } else if (data.genes.length) {
    const sign = state.sort === 'value-desc' ? -1 : 1;
    const keys = sortKeys(data);
    order.sort((a, b) => {
      const va = keys[a], vb = keys[b];
      // Missing values always sink to the bottom, in both directions.
      if (va === null && vb === null) return byName(data.cellLines[a].name, data.cellLines[b].name);
      if (va === null) return 1;
      if (vb === null) return -1;
      return sign * (va - vb) || byName(data.cellLines[a].name, data.cellLines[b].name);
    });
  }

  state.view = order;
  state.groups = null;
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

/* Heatmap geometry.
 *
 * Orientation: cell lines are ROWS, genes are COLUMNS.  A run typically has a
 * handful of genes against up to ~1,200 cell lines, so the tall layout is the
 * one that fits: cell line names sit in the left gutter where they read
 * horizontally, and the sheet scrolls vertically.
 *
 *   organCol   nameCol            cw   cw   cw
 *  |---------|----------|band| [gene][gene][gene]
 *  | Liver   |  HEP G2  |     |  .    .     .     ch
 *  |         |  HUH-7   |     |  .    .     .     ch
 *  | Lung    |  A-549   |     |  .    .     .     ch
 */
const HM = {
  organCol: 0,   // organ group label column (only when sorted by organ)
  nameCol: 158,  // cell line name column
  gutter: 158,   // organCol + nameCol
  band: 46,      // gene label band across the top
  cw: 64,        // width of one gene column
  ch: 20,        // height of one cell line row
  rotated: false,
};

const ORGAN_COL_WIDTH = 104;

// Share of the width beside the gutter that the gene columns spread over
// while a column can still be comfortably wide.
const PLOT_WIDTH_FRACTION = 1 / 2;

// The sheet is capped at the width on screen: adding genes narrows the columns
// instead of pushing the heatmap off the right edge, so a laptop never has to
// scroll sideways to see the last gene.  COMFORT_COL_WIDTH is only the point
// at which the compact half-width block gives up and the columns spread over
// the whole row; MIN_COL_WIDTH is the floor that keeps a column from vanishing
// entirely at the 200-gene limit.
const MIN_COL_WIDTH = 3;
const COMFORT_COL_WIDTH = 44;
const MAX_COL_WIDTH = 132;

/** Width of one gene column for `cols` genes in `available` pixels.
 *
 *  Never returns more than `available / cols`, which is what keeps the sheet
 *  inside the viewport.  Kept as a plain function so the browser tests can
 *  exercise both regimes - compact block, then filling the row - directly.
 */
function geneColumnWidth(available, cols) {
  if (cols <= 0) return MAX_COL_WIDTH;
  // A handful of genes: a compact block rather than a few enormous columns.
  const compact = Math.floor((available * PLOT_WIDTH_FRACTION) / cols);
  if (compact >= COMFORT_COL_WIDTH) return Math.min(MAX_COL_WIDTH, compact);
  return Math.max(MIN_COL_WIDTH, Math.min(MAX_COL_WIDTH, Math.floor(available / cols)));
}

/** Label every Nth gene, so narrow columns thin the labels out instead of
 *  overprinting them.  Rotated text needs roughly 14px of column pitch to
 *  clear its neighbour; the tooltip still names every column on hover. */
function geneLabelStep(columnWidth) {
  return Math.max(1, Math.ceil(14 / columnWidth));
}

/** Row and global maxima, memoised - paint() runs on every mousemove and
 *  scanning 200 x 1,200 values each time would make hovering feel sticky. */
function scaleMaxima() {
  if (state.maxima) return state.maxima;
  const data = state.result;
  const globalMax = { global: 0, rows: [], totals: [] };
  data.genes.forEach((_, r) => {
    let rowMax = 0;
    let rowTotal = 0;
    const row = data.values[r];
    for (let c = 0; c < row.length; c += 1) {
      const v = row[c];
      if (v === null || v === undefined) continue;
      if (v > rowMax) rowMax = v;
      rowTotal += v;
    }
    globalMax.rows.push(rowMax);
    globalMax.totals.push(rowTotal);
    if (rowMax > globalMax.global) globalMax.global = rowMax;
  });
  state.maxima = globalMax;
  return globalMax;
}

/** Contiguous runs of equal organ over the current row order. */
function organGroups() {
  if (state.groups) return state.groups;
  const data = state.result;
  const groups = [];
  state.view.forEach((cellIndex, row) => {
    const organ = data.cellLines[cellIndex].organ || null;
    const last = groups[groups.length - 1];
    if (last && last.organ === organ) last.end = row;
    else groups.push({ organ, start: row, end: row });
  });
  state.groups = groups;
  return groups;
}

/** Where to draw one organ group's label, or null if it has no room on screen.
 *
 *  The label is sticky: it follows a long group down so it stays readable
 *  while that group scrolls past.  The hard rule is that it must never leave
 *  its OWN rows.  It used to be clamped to the viewport edges instead, and
 *  because lastRow is computed with ceil() the group starting just below the
 *  fold was still drawn - its label pinned to the bottom edge, right beside
 *  the last row of the group above.  On screen that reads as "LNCAP is filed
 *  under 乳腺", which is exactly how it was reported.
 */
function groupLabelY(top, bottom, band, vh) {
  const visibleTop = Math.max(top, band);
  const visibleBottom = Math.min(bottom, vh);
  // Less than a line's worth of the group is on screen: no honest place to
  // put the label, so leave it out rather than park it on someone else's row.
  if (visibleBottom - visibleTop < 12) return null;
  return Math.min(
    Math.max((top + bottom) / 2, visibleTop + 9),
    visibleBottom - 9,
  );
}

function showOrganColumn() {
  return state.sort === 'organ' && organGroups().some((g) => g.organ);
}

// Thickness of the rule drawn at every organ boundary.  A hairline, but in
// the muted ink rather than the axis color - what made the boundaries
// invisible at a 16px row pitch was the weight of the color, not the width.
// Kept at exactly 1 so the half-pixel offset below lands the line on a device
// pixel; anything else renders as a soft two-pixel smear.
const GROUP_RULE_WIDTH = 1;

/** Rows that start a new organ, i.e. where a rule is drawn. */
function groupBoundaryRows() {
  if (!showOrganColumn()) return [];
  return organGroups().filter((g) => g.start > 0).map((g) => g.start);
}

function renderHeatmap() {
  const data = state.result;
  const viewport = $('hmViewport');
  const canvas = $('hmCanvas');
  if (!data || !data.genes.length || !state.view.length) return;

  const rows = state.view.length;      // cell lines
  const cols = data.genes.length;      // genes

  HM.organCol = showOrganColumn() ? ORGAN_COL_WIDTH : 0;
  HM.gutter = HM.organCol + HM.nameCol;

  // The viewport's only in-flow child is the sticky canvas, so its height has
  // to be set explicitly - otherwise element and canvas size each other in a
  // circle and collapse to the CSS min-height.
  const maxHeight = Math.max(320, window.innerHeight - 280);

  // One readable line per cell line; grow the rows when there are only a few.
  HM.ch = Math.max(16, Math.min(30, Math.floor((maxHeight - 46) / rows)));

  // The gene columns get the room beside the gutter and no more: the sheet is
  // capped at the viewport, so adding genes narrows the columns rather than
  // extending the heatmap to the right.
  const available = Math.max(viewport.clientWidth - HM.gutter - 2, 160);
  HM.cw = geneColumnWidth(available, cols);
  // Gene symbols are short; keep them upright while the columns are wide
  // enough, and only fall back to rotated labels when they are not.
  HM.rotated = HM.cw < 56;
  HM.band = HM.rotated ? 116 : 46;

  const contentW = HM.gutter + cols * HM.cw;
  const contentH = HM.band + rows * HM.ch;
  $('hmSizer').style.width = contentW + 'px';
  $('hmSizer').style.height = contentH + 'px';

  const scrollbarH = contentW > viewport.clientWidth ? 14 : 0;
  viewport.style.height = Math.min(contentH + scrollbarH, maxHeight) + 'px';

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
  const rows = state.view.length;
  const cols = data.genes.length;
  const viewport = $('hmViewport');
  const sx = viewport.scrollLeft;
  const sy = viewport.scrollTop;

  const font = cssVar('--font') || 'system-ui, sans-serif';
  const surface = cssVar('--surface');
  const sunk = cssVar('--surface-sunk');
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
  const firstRow = Math.max(0, Math.floor(sy / HM.ch));
  const lastRow = Math.min(rows - 1, Math.ceil((sy + vh - HM.band) / HM.ch));
  const firstCol = Math.max(0, Math.floor(sx / HM.cw));
  const lastCol = Math.min(cols - 1, Math.ceil((sx + vw - HM.gutter) / HM.cw));

  const rowY = (r) => HM.band + r * HM.ch - sy;
  const colX = (c) => HM.gutter + c * HM.cw - sx;

  // --- cells ---------------------------------------------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(HM.gutter, HM.band, vw - HM.gutter, vh - HM.band);
  ctx.clip();

  for (let r = firstRow; r <= lastRow; r += 1) {
    const y = rowY(r);
    const cellIndex = state.view[r];
    for (let c = firstCol; c <= lastCol; c += 1) {
      const v = data.values[c][cellIndex];
      const max = state.scale === 'row' ? maxima.rows[c] : maxima.global;
      ctx.fillStyle = v === null || v === undefined
        ? nodata
        : (v === 0 ? zero : rampColor(scalePosition(v, max), dark));
      // 1px surface gap between fills keeps adjacent cells legible.
      ctx.fillRect(colX(c), y, HM.cw - 1, HM.ch - 1);
    }
  }

  // Value labels, printed straight into the cells once they are roomy enough.
  if (HM.cw >= 52 && HM.ch >= 15) {
    ctx.font = `11px ${font}`;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let r = firstRow; r <= lastRow; r += 1) {
      const cellIndex = state.view[r];
      const y = rowY(r) + (HM.ch - 1) / 2;
      for (let c = firstCol; c <= lastCol; c += 1) {
        const v = data.values[c][cellIndex];
        if (v === null || v === undefined) continue;
        if (v === 0) {
          ctx.fillStyle = muted;
        } else {
          const max = state.scale === 'row' ? maxima.rows[c] : maxima.global;
          ctx.fillStyle = inkOn(rampRgb(scalePosition(v, max), dark));
        }
        ctx.fillText(formatValue(v), colX(c) + HM.cw - 7, y);
      }
    }
  }

  // hover ring
  if (state.hover && state.hover.kind === 'cell') {
    const { row, col } = state.hover;
    if (row >= firstRow && row <= lastRow && col >= firstCol && col <= lastCol) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = ink;
      ctx.strokeRect(colX(col) + 0.5, rowY(row) + 0.5, HM.cw - 2, HM.ch - 2);
    }
  }
  ctx.restore();

  // --- left gutter: organ groups + cell line names --------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, HM.band, HM.gutter, vh - HM.band);
  ctx.clip();
  ctx.fillStyle = surface;
  ctx.fillRect(0, HM.band, HM.gutter, vh - HM.band);

  if (HM.organCol) {
    ctx.font = `600 11px ${font}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';

    // One tinted block per group, ending exactly where the group ends, so the
    // rows a label covers are visible and not inferred from its position.
    // The rule drawn across the sheet below is the boundary; this is what ties
    // the label to the rows the rule encloses.
    organGroups().forEach((group) => {
      if (group.end < firstRow || group.start > lastRow) return;
      const top = rowY(group.start);
      const bottom = rowY(group.end) + HM.ch;
      const clippedTop = Math.max(top, HM.band);
      const clippedBottom = Math.min(bottom, vh);
      if (clippedBottom <= clippedTop) return;

      ctx.fillStyle = sunk;
      ctx.fillRect(0, clippedTop, HM.organCol, clippedBottom - clippedTop);

      if (!group.organ) return;
      const y = groupLabelY(top, bottom, HM.band, vh);
      if (y === null) return;
      const label = state.organJa[group.organ] || group.organ;
      ctx.fillStyle = ink2;
      ctx.fillText(truncate(ctx, label, HM.organCol - 18), 12, y);
    });
  }

  ctx.font = `${Math.max(11, Math.min(13, HM.ch - 7))}px ${font}`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  const nameRight = HM.gutter - 12;
  for (let r = firstRow; r <= lastRow; r += 1) {
    const cell = data.cellLines[state.view[r]];
    const hovered = state.hover && state.hover.row === r;
    const y = rowY(r) + (HM.ch - 1) / 2;
    if (hovered) {
      ctx.fillStyle = sunk;
      ctx.fillRect(HM.organCol, rowY(r), HM.nameCol, HM.ch - 1);
    }
    ctx.fillStyle = hovered ? ink : ink2;
    ctx.fillText(truncate(ctx, cell.name, HM.nameCol - 20), nameRight, y);
  }

  ctx.strokeStyle = axis;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(HM.gutter - 0.5, HM.band);
  ctx.lineTo(HM.gutter - 0.5, vh);
  ctx.stroke();
  ctx.restore();

  // --- organ boundaries -------------------------------------------------------
  // Drawn last of the body, in one pass across the FULL width: a rule that
  // stops at the cell line names does not read as a division of the list, and
  // one drawn earlier gets painted over by the gutter's own background.
  // Heavier than a gridline on purpose - at a 16px row pitch a hairline was
  // missed entirely, which is what made the grouping unreadable.
  if (HM.organCol) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, HM.band, vw, vh - HM.band);
    ctx.clip();
    ctx.strokeStyle = muted;
    ctx.lineWidth = GROUP_RULE_WIDTH;
    ctx.beginPath();
    groupBoundaryRows().forEach((start) => {
      if (start < firstRow || start > lastRow + 1) return;
      const y = rowY(start) - GROUP_RULE_WIDTH / 2;
      ctx.moveTo(0, y);
      ctx.lineTo(vw, y);
    });
    ctx.stroke();
    ctx.restore();
  }

  // --- top band: gene labels -------------------------------------------------
  ctx.save();
  ctx.beginPath();
  ctx.rect(HM.gutter, 0, vw - HM.gutter, HM.band);
  ctx.clip();
  ctx.fillStyle = surface;
  ctx.fillRect(HM.gutter, 0, vw - HM.gutter, HM.band);
  ctx.font = `600 12px ${font}`;

  const labelStep = geneLabelStep(HM.cw);
  for (let c = firstCol; c <= lastCol; c += 1) {
    const gene = data.genes[c];
    const hovered = state.hover && state.hover.col === c;
    // Columns too narrow for every label get every Nth one, plus whichever is
    // under the pointer - overprinted symbols are worse than fewer of them.
    if (c % labelStep !== 0 && !hovered) continue;
    ctx.fillStyle = hovered ? ink : ink2;
    if (HM.rotated) {
      ctx.save();
      ctx.translate(colX(c) + HM.cw / 2 + 4, HM.band - 10);
      ctx.rotate(-Math.PI / 3);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(truncate(ctx, gene.symbol, HM.band - 18), 0, 0);
      ctx.restore();
    } else {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(truncate(ctx, gene.symbol, HM.cw - 8), colX(c) + (HM.cw - 1) / 2, HM.band - 16);
    }
  }
  ctx.strokeStyle = axis;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(HM.gutter, HM.band - 0.5);
  ctx.lineTo(vw, HM.band - 0.5);
  ctx.stroke();
  ctx.restore();

  // --- corner ----------------------------------------------------------------
  ctx.fillStyle = surface;
  ctx.fillRect(0, 0, HM.gutter, HM.band);
  ctx.fillStyle = muted;
  ctx.font = `11px ${font}`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'bottom';
  ctx.fillText('細胞株 ＼ 遺伝子', HM.gutter - 12, HM.band - 14);
  ctx.fillStyle = muted;
  ctx.font = `10px ${font}`;
  ctx.fillText(state.result.metric, HM.gutter - 12, HM.band - 3);
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
    const clickable = hit && (hit.row !== undefined || hit.kind === 'gene');
    viewport.style.cursor = clickable ? 'pointer' : 'default';
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
    if (!hit) return;
    // A gene label opens that gene in the Human Protein Atlas - the source
    // this data comes from - mirroring the cell line link into Cellosaurus.
    if (hit.kind === 'gene') {
      const gene = state.result.genes[hit.col];
      if (gene && gene.hpaUrl) window.open(gene.hpaUrl, '_blank', 'noopener');
      return;
    }
    // Any hit that identifies a cell line - its name in the gutter or a value
    // cell in its row - opens that cell line in Cellosaurus.
    if (hit.row === undefined) return;
    window.open(state.result.cellLines[state.view[hit.row]].databaseUrl, '_blank', 'noopener');
  });
}

function hitTest(event) {
  const data = state.result;
  if (!data || !state.view.length) return null;
  const viewport = $('hmViewport');
  const rect = viewport.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const row = Math.floor((y - HM.band + viewport.scrollTop) / HM.ch);
  const col = Math.floor((x - HM.gutter + viewport.scrollLeft) / HM.cw);
  const inRows = row >= 0 && row < state.view.length;
  const inCols = col >= 0 && col < data.genes.length;

  if (y < HM.band && x < HM.gutter) return { kind: 'corner' };
  if (y < HM.band) return inCols ? { kind: 'gene', col } : null;
  if (x < HM.gutter) return inRows ? { kind: 'cellLine', row } : null;
  if (inRows && inCols) return { kind: 'cell', row, col };
  return null;
}

function renderTooltip(tooltip, hit, event) {
  const data = state.result;
  const viewport = $('hmViewport');
  const rect = viewport.getBoundingClientRect();

  let html = '';
  if (hit.kind === 'gene') {
    const gene = data.genes[hit.col];
    html = `<div class="t-title">${escapeHtml(gene.symbol)}</div>` +
      (gene.ensemblId ? `<div class="t-row">${escapeHtml(gene.ensemblId)}</div>` : '') +
      (gene.hpaUrl
        ? '<div class="t-row" style="opacity:.7;font-size:11px">クリックで Human Protein Atlas へ</div>'
        : '');
  } else {
    const cell = data.cellLines[state.view[hit.row]];
    html = `<div class="t-title">${escapeHtml(cell.name)}</div>`;
    if (cell.organ) {
      const organ = cell.organJa ? `${cell.organJa} (${cell.organ})` : cell.organ;
      html += `<div class="t-row">由来臓器: ${escapeHtml(organ)}</div>`;
      if (cell.organSource) {
        html += `<div class="t-row" style="opacity:.7;font-size:11px">└ ${escapeHtml(cell.organSource)}</div>`;
      }
    }
    if (cell.disease) html += `<div class="t-row">疾患: ${escapeHtml(cell.disease)}</div>`;
    if (cell.species) html += `<div class="t-row">種: ${escapeHtml(cell.species)}</div>`;
    if (cell.tcga && cell.tcga.length) {
      // How closely this cell line resembles real patient tumours - a read on
      // how good a model it is.  Not where it came from: 由来臓器 above is.
      html += `<div class="t-row">類似がん種 (TCGA): ${escapeHtml(tcgaLabel(cell.tcga[0]))}</div>`;
      const rest = cell.tcga.slice(1).map(tcgaLabel).join(' / ');
      if (rest) {
        html += `<div class="t-row" style="opacity:.7;font-size:11px">└ 次点: ${escapeHtml(rest)}</div>`;
      }
    }
    if (hit.kind === 'cell') {
      const gene = data.genes[hit.col];
      const value = data.values[hit.col][state.view[hit.row]];
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
  const height = tooltip.offsetHeight;
  const flipX = (event.clientX - rect.left) + width + 28 > viewport.clientWidth;
  const flipY = (event.clientY - rect.top) + height + 28 > viewport.clientHeight;
  tooltip.style.left = (flipX ? localX - width - 14 : localX + 14) + 'px';
  tooltip.style.top = (flipY ? localY - height - 14 : localY + 14) + 'px';
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
  [['細胞株', 'name'], ['由来臓器', 'organ'], ['疾患', null], ['種', null],
   ['類似がん種 (TCGA)', null]].forEach(([label, key]) => {
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
    if (gene.hpaUrl) {
      // The header itself still sorts, so the link out to HPA is its own
      // target rather than the symbol - otherwise sorting by a gene would
      // navigate away from the page instead.
      const out = document.createElement('a');
      out.className = 'th-link';
      out.href = gene.hpaUrl;
      out.target = '_blank';
      out.rel = 'noopener';
      out.textContent = '↗';
      out.title = `${gene.symbol} を Human Protein Atlas で開く`;
      out.addEventListener('click', (event) => event.stopPropagation());
      th.append(' ', out);
    }
    th.addEventListener('click', () => {
      // Compare against the PREVIOUS basis: assigning first made this test
      // always true, so clicking a different gene flipped the direction
      // instead of sorting that gene descending.
      const sameGene = state.sortGene === index;
      state.sort = (sameGene && state.sort === 'value-desc') ? 'value-asc' : 'value-desc';
      state.sortGene = index;
      $('sortSelect').value = state.sort;
      applySort();
    });
    if (state.sort.startsWith('value') && state.sortGene === index) {  // eslint-disable-line
      const arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = state.sort === 'value-desc' ? '▾' : '▴';
      th.append(arrow);
    }
    headRow.append(th);
  });
  thead.append(headRow);

  // Filled in chunks below; a token cancels a run that a re-sort superseded.
  const token = (state.tableToken += 1);
  const rows = state.view;
  const notice = $('tableNotice');
  const perChunk = Math.max(20, Math.ceil(TABLE_CELLS_PER_CHUNK / (data.genes.length + 4)));

  const buildRow = (cellIndex) => {
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

    const organTd = document.createElement('td');
    organTd.className = 'left meta';
    organTd.textContent = cell.organJa || cell.organ || '—';
    if (cell.organ) {
      organTd.title = cell.organSource
        ? `${cell.organ}\n判定根拠: ${cell.organSource}`
        : cell.organ;
    }
    tr.append(organTd);

    [cell.disease, cell.species].forEach((value) => {
      const td = document.createElement('td');
      td.className = 'left meta';
      td.textContent = value || '—';
      tr.append(td);
    });

    const tcgaTd = document.createElement('td');
    tcgaTd.className = 'left meta';
    const hits = cell.tcga || [];
    tcgaTd.textContent = hits.length ? tcgaLabel(hits[0]) : '—';
    if (hits.length) {
      // The runners-up would make the column unreadable inline, but they are
      // the part that says whether the top match is actually decisive.
      tcgaTd.title = hits.map((h, i) => `${i + 1}. ${tcgaLabel(h)}`).join('\n');
    }
    tr.append(tcgaTd);

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
    return tr;
  };

  let next = 0;
  let pending = document.createDocumentFragment();
  const appendChunk = () => {
    if (token !== state.tableToken) return;   // superseded by a newer render
    const stop = Math.min(next + perChunk, rows.length);
    for (; next < stop; next += 1) pending.append(buildRow(rows[next]));

    // The first chunk goes in straight away so the view is never blank; the
    // rest is held back and inserted once at the end.  Appending into a live
    // table makes the browser re-measure every row already in it, which turns
    // 1,200 x 60 from ~1.6s into ~15s.
    if (!tbody.childElementCount || next >= rows.length) {
      tbody.append(pending);
      pending = document.createDocumentFragment();
    }

    if (next < rows.length) {
      notice.hidden = false;
      notice.textContent =
        `${next.toLocaleString()} / ${rows.length.toLocaleString()} 細胞株を描画中…`;
      requestAnimationFrame(appendChunk);
      return;
    }
    notice.hidden = true;
    state.tableDirty = false;
  };

  // The first chunk is synchronous so a small table is complete on return and
  // the view never flashes empty.
  appendChunk();
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
    // Stamped with the local date and time so repeated exports do not
    // overwrite each other and stay traceable to when they were taken.
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
      + `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    anchor.download = `hpa_cell_line_expression_${stamp}.tsv`;
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
