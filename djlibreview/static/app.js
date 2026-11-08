const csrf = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
const maxUploadMb = Number(document.querySelector('meta[name="max-upload-mb"]').getAttribute('content')) || 48;
const state = { review: null, busy: false };
let querySerial = 0;
let searchTimer = 0;

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  const fields = attrs || {};
  Object.keys(fields).forEach((key) => {
    const value = fields[key];
    if (value === null || value === undefined) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'hidden') node.hidden = Boolean(value);
    else if (key === 'onClick') node.addEventListener('click', value);
    else node.setAttribute(key, String(value));
  });
  (children || []).forEach((child) => {
    if (child) node.append(child);
  });
  return node;
}

function setFormError(text) {
  const node = document.getElementById('form-error');
  node.textContent = text || '';
  node.hidden = !text;
}

function setBusy(isBusy) {
  state.busy = isBusy;
  document.getElementById('results').setAttribute('aria-busy', isBusy ? 'true' : 'false');
  document.getElementById('review-button').disabled = isBusy;
  document.getElementById('example-button').disabled = isBusy;
  document.getElementById('export-button').disabled = isBusy || !state.review;
}

async function api(path, options) {
  const opts = options || {};
  const headers = { 'X-CSRF-Token': csrf };
  let body;
  if (opts.form) body = opts.form;
  else if (opts.body) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, {
    method: opts.method || 'GET',
    credentials: 'same-origin',
    headers,
    body,
  });
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) {
    if (!response.ok) throw new Error('The local review process rejected the request.');
    return response;
  }
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || 'The local review process rejected the request.');
  }
  return data;
}

function fillSelect(select, options, current) {
  const empty = select.getAttribute('data-empty') || 'Any';
  select.replaceChildren(el('option', { value: '', text: empty }));
  options.forEach((option) => {
    const node = el('option', { value: option.value, text: option.label });
    if (option.value === current) node.selected = true;
    select.append(node);
  });
}

function fillFilters(data, reset) {
  const version = document.getElementById('version-state');
  const marker = document.getElementById('marker');
  const copy = document.getElementById('copy-hint');
  const search = document.getElementById('q');
  if (reset) {
    search.value = '';
    copy.value = 'any';
  }
  fillSelect(version, data.facets.version_states || [], reset ? '' : version.value);
  const markers = (data.facets.markers || []).map((item) => ({ value: item, label: item }));
  fillSelect(marker, markers, reset ? '' : marker.value);
  document.getElementById('filters').hidden = !(data.total_groups > 0);
}

function applyReview(data, resetFilters, focus) {
  state.review = data;
  document.getElementById('example-banner').hidden = !data.example_data;
  const form = data.form || {};
  if (form.dataset_label) document.getElementById('dataset-label').value = form.dataset_label;
  if (form.scope) document.getElementById('scope').value = form.scope;
  if (form.snapshot_date) document.getElementById('snapshot-date').value = form.snapshot_date;
  document.getElementById('path-column').value = form.path_column || '';
  fillFilters(data, resetFilters);
  document.getElementById('export-note').textContent = '';
  setFormError('');
  render();
  if (focus) {
    const status = document.getElementById('status-line');
    if (status) status.focus();
    const narrow = window.matchMedia('(max-width: 900px)').matches;
    if (narrow) document.getElementById('results').scrollIntoView({ block: 'start' });
  }
}

function renderProvenance(data) {
  const list = el('dl', { class: 'provenance' });
  (data.provenance_rows || []).forEach((row) => {
    list.append(el('dt', { text: row.label }));
    list.append(el('dd', { text: row.value }));
  });
  return list;
}

function renderStats(data) {
  const grid = el('div', { class: 'stats' });
  (data.counts_display || []).forEach((item) => {
    grid.append(el('article', { class: 'stat' }, [
      el('span', { text: item.label }),
      el('strong', { text: String(item.value) }),
    ]));
  });
  (data.assurances || []).forEach((item) => {
    grid.append(el('article', { class: 'stat warn' }, [
      el('span', { text: item.label }),
      el('strong', { text: item.value || 'Not checked' }),
    ]));
  });
  return grid;
}

function renderErrors(data) {
  const block = el('div', { class: 'errors' }, [el('h3', { text: 'Row problems' })]);
  if (!data.error_count) {
    block.append(el('p', { text: 'No row problems in this inventory.' }));
    return block;
  }
  block.append(el('p', { text: data.error_count + ' row problems. Paths in this list were not opened.' }));
  if (data.error_details_truncated) {
    block.append(el('p', { text: 'The list shows the first problems only. The report counts the rest.' }));
  }
  const list = el('ul', { class: 'error-list' });
  (data.errors || []).forEach((problem) => {
    const lead = problem.line ? 'Line ' + problem.line + ': ' + problem.message : problem.message;
    const item = el('li', {}, [el('p', { text: lead })]);
    if (problem.path) item.append(el('p', { class: 'path', text: problem.path }));
    list.append(item);
  });
  block.append(list);
  return block;
}

function renderFile(file) {
  const hint = file.copy_hint ? 'yes' : 'no';
  return el('article', { class: 'file-card' }, [
    el('p', { class: 'file-kicker', text: 'Imported filename' }),
    el('p', { class: 'filename', text: file.filename }),
    el('p', { class: 'file-kicker', text: 'Folder text from the inventory' }),
    el('p', { class: 'path', text: file.folder_text }),
    el('p', { class: 'file-kicker', text: 'Imported path text' }),
    el('p', { class: 'path', text: file.path }),
    el('p', {
      class: 'file-meta',
      text: 'Copy hint: ' + hint + ' · Extension: ' + (file.extension || '(none)') + ' · ' + file.version_state_label,
    }),
  ]);
}

function renderGroup(group) {
  const meta = el('dl', { class: 'meta' });
  const rows = [
    ['Normalized core', group.core],
    ['Version', group.version_state_label],
    ['Group id', group.group_id],
  ];
  rows.forEach((row) => {
    meta.append(el('dt', { text: row[0] }));
    meta.append(el('dd', { text: row[1] }));
  });
  meta.append(el('dt', { text: 'Version markers' }));
  const markerList = el('dd', {}, [el('ul', { class: 'chips' })]);
  const chips = markerList.querySelector('ul');
  if (group.markers && group.markers.length) group.markers.forEach((marker) => chips.append(el('li', { text: marker })));
  else chips.append(el('li', { text: 'none' }));
  meta.append(markerList);
  meta.append(el('dt', { text: 'Retained qualifiers' }));
  const qualifierList = el('dd', {}, [el('ul', { class: 'chips' })]);
  const qualifierChips = qualifierList.querySelector('ul');
  if (group.qualifiers && group.qualifiers.length) {
    group.qualifiers.forEach((qualifier) => qualifierChips.append(el('li', { text: qualifier })));
  } else qualifierChips.append(el('li', { text: 'none' }));
  meta.append(qualifierList);
  const grid = el('div', { class: 'file-grid' });
  (group.files || []).forEach((file) => grid.append(renderFile(file)));
  const children = [
    el('p', { class: 'kicker', text: group.evidence_label }),
    el('h3', { text: 'Filename candidate' }),
    el('p', { text: group.file_count + ' imported paths' }),
    meta,
    el('p', { class: 'note', text: group.comparison_note }),
    grid,
    el('details', { class: 'explain' }, [
      el('summary', { text: 'Why this is only a filename candidate' }),
      el('p', { class: 'note', text: group.uncertainty }),
    ]),
  ];
  if (group.file_count > group.files.length && group.files.length < 100) {
    children.push(el('button', {
      type: 'button',
      class: 'secondary',
      text: 'Show more path text',
      onClick: () => loadMore(group),
    }));
  } else if (group.file_count > group.files.length) {
    children.push(el('p', {
      class: 'note',
      text: 'The screen lists the first 100 paths. The downloaded report lists every path in the group. These are still inventory text, not opened files.',
    }));
  }
  return el('article', { class: 'group' }, children);
}

function render() {
  const data = state.review;
  const results = document.getElementById('results');
  const body = document.getElementById('results-body');
  if (!data) {
    results.dataset.reviewState = 'empty';
    return;
  }
  if (data.status === 'fatal_error') {
    results.dataset.reviewState = 'fatal';
    body.replaceChildren(
      el('h2', { text: 'Inventory not reviewed' }),
      el('p', { class: 'status', id: 'status-line', tabindex: '-1', text: data.status_label }),
      renderProvenance(data),
      renderErrors(data),
    );
    return;
  }
  if (data.matched_groups === 0 && data.total_groups > 0) results.dataset.reviewState = 'filtered-empty';
  else if (!data.total_groups) results.dataset.reviewState = 'empty-groups';
  else results.dataset.reviewState = 'ready';
  const children = [
    el('h2', { text: 'Snapshot review' }),
    el('p', { class: 'status', id: 'status-line', tabindex: '-1', text: data.status_label }),
    renderProvenance(data),
    renderStats(data),
    el('p', { class: 'counts-note', text: data.counts_note }),
    renderErrors(data),
  ];
  if (data.matched_groups > 0) {
    children.push(el('h2', { text: 'Candidate groups' }));
    data.groups.forEach((group) => children.push(renderGroup(group)));
    if (data.page_count > 1) {
      children.push(el('div', { class: 'pager' }, [
        el('button', {
          type: 'button',
          class: 'secondary',
          text: 'Previous',
          disabled: data.page <= 1 ? '' : null,
          onClick: () => runQuery(data.page - 1),
        }),
        el('p', { text: 'Page ' + data.page + ' of ' + data.page_count }),
        el('button', {
          type: 'button',
          class: 'secondary',
          text: 'Next',
          disabled: data.page >= data.page_count ? '' : null,
          onClick: () => runQuery(data.page + 1),
        }),
      ]));
    }
  } else if (data.total_groups > 0) {
    children.push(el('h2', { text: 'No groups match this filter' }));
    children.push(el('p', { text: data.filter_empty_message }));
  } else {
    children.push(el('h2', { text: 'No filename candidate groups' }));
    children.push(el('p', { text: data.empty_library_message }));
  }
  body.replaceChildren(...children);
}

async function runQuery(page) {
  if (state.busy || !state.review) return;
  const serial = ++querySerial;
  try {
    const data = await api('/api/query', { method: 'POST', body: currentFilters(page) });
    if (serial !== querySerial || !state.review) return;
    state.review.groups = data.groups;
    state.review.page = data.page;
    state.review.page_count = data.page_count;
    state.review.matched_groups = data.matched_groups;
    state.review.total_groups = data.total_groups;
    state.review.empty_library_message = data.empty_library_message;
    state.review.filter_empty_message = data.filter_empty_message;
    render();
  } catch (error) {
    if (serial === querySerial) setFormError(error.message);
  }
}

function currentFilters(page) {
  return {
    q: document.getElementById('q').value,
    version_state: document.getElementById('version-state').value,
    marker: document.getElementById('marker').value,
    copy_hint: document.getElementById('copy-hint').value,
    page,
  };
}

async function loadMore(group) {
  if (state.busy) return;
  try {
    const data = await api('/api/group', {
      method: 'POST',
      body: { group_id: group.group_id, offset: 0, limit: Math.min(100, group.files.length + 20) },
    });
    group.files = data.files;
    group.file_count = data.file_count;
    render();
  } catch (error) {
    setFormError(error.message);
  }
}

async function startReview() {
  if (state.busy) return;
  const fileInput = document.getElementById('inventory-file');
  const file = fileInput.files && fileInput.files[0];
  const dataset = document.getElementById('dataset-label').value.trim();
  const scope = document.getElementById('scope').value.trim();
  const snapshot = document.getElementById('snapshot-date').value;
  if (!dataset || !scope || !snapshot || !file) {
    setFormError('Add the dataset label, scope, snapshot date, and a CSV file. The scope and snapshot date are required and are not guessed.');
    return;
  }
  if (file.size > maxUploadMb * 1024 * 1024) {
    setFormError('That CSV is larger than the ' + maxUploadMb + ' MB local limit. It was not sent.');
    return;
  }
  const form = new FormData();
  form.append('dataset_label', dataset);
  form.append('scope', scope);
  form.append('snapshot_date', snapshot);
  form.append('path_column', document.getElementById('path-column').value.trim());
  form.append('inventory', file, file.name);
  const button = document.getElementById('review-button');
  const previous = button.textContent;
  setBusy(true);
  button.textContent = 'Reading inventory text...';
  try {
    const data = await api('/api/review', { method: 'POST', form });
    applyReview(data, true, true);
  } catch (error) {
    setFormError(error.message);
  } finally {
    button.textContent = previous;
    setBusy(false);
  }
}

async function loadExample() {
  if (state.busy) return;
  const button = document.getElementById('example-button');
  const previous = button.textContent;
  setBusy(true);
  button.textContent = 'Loading example...';
  try {
    const data = await api('/api/demo', { method: 'POST', body: {} });
    document.getElementById('inventory-file').value = '';
    applyReview(data, true, true);
  } catch (error) {
    setFormError(error.message);
    if (!state.review) document.getElementById('results').dataset.reviewState = 'error';
  } finally {
    button.textContent = previous;
    setBusy(false);
  }
}

async function exportReport() {
  if (state.busy || !state.review) return;
  const button = document.getElementById('export-button');
  const previous = button.textContent;
  setBusy(true);
  button.textContent = 'Preparing report...';
  try {
    const response = await fetch('/api/export', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
      body: '{}',
    });
    const type = response.headers.get('content-type') || '';
    if (!response.ok || type.includes('application/json')) {
      let message = 'The report was not created.';
      if (type.includes('application/json')) {
        const data = await response.json();
        message = data.error || message;
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'filename-candidate-review.zip';
    link.rel = 'noopener';
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    document.getElementById('export-note').textContent = 'Report downloaded in this browser. No music file was written or changed.';
    setFormError('');
  } catch (error) {
    setFormError(error.message);
  } finally {
    button.textContent = previous;
    setBusy(false);
  }
}

document.getElementById('import-form').addEventListener('submit', (event) => {
  event.preventDefault();
  startReview();
});
document.getElementById('example-button').addEventListener('click', loadExample);
document.getElementById('export-button').addEventListener('click', exportReport);
document.getElementById('filters').addEventListener('submit', (event) => {
  event.preventDefault();
  runQuery(1);
});
document.getElementById('q').addEventListener('input', () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => runQuery(1), 250);
});
document.getElementById('version-state').addEventListener('change', () => runQuery(1));
document.getElementById('marker').addEventListener('change', () => runQuery(1));
document.getElementById('copy-hint').addEventListener('change', () => runQuery(1));

async function boot() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('example') === '1') {
    await loadExample();
    return;
  }
  try {
    const existing = await api('/api/review');
    if (existing.accepted) applyReview(existing, false, false);
  } catch (error) {
    setFormError(error.message);
  }
}

boot();
