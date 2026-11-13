const $ = (id) => document.getElementById(id);
let points = [],
  result = null,
  editing = null,
  busy = false,
  searchRevision = 0,
  importRevision = 0;
function el(tag, text) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  return n;
}
function svg(tag, attrs, text) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}
function status(text) {
  $('status').textContent = text;
  $('records-status').textContent = text;
}
function numeric(id) {
  if (!$(id).value.trim()) throw Error('Fill every coordinate and radius field.');
  return Number($(id).value);
}
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Request failed.');
  return data;
}
function draw() {
  const fragment = document.createDocumentFragment(),
    x = (lon) => ((lon + 180) / 360) * 1000,
    y = (lat) => ((90 - lat) / 180) * 500;
  for (let lon = -150; lon <= 150; lon += 30) {
    fragment.append(
      svg('line', {
        x1: x(lon),
        x2: x(lon),
        y1: 0,
        y2: 500,
        stroke: '#c2d5ca',
        'stroke-width': lon === 0 ? 1.5 : 1,
      }),
      svg('text', { x: x(lon) + 5, y: 490 }, `${lon}°`),
    );
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    fragment.append(
      svg('line', {
        x1: 0,
        x2: 1000,
        y1: y(lat),
        y2: y(lat),
        stroke: '#c2d5ca',
        'stroke-width': lat === 0 ? 1.5 : 1,
      }),
      svg('text', { x: 7, y: y(lat) - 5 }, `${lat}°`),
    );
  }
  if (result) {
    for (const box of result.bounds)
      fragment.append(
        svg('rect', {
          x: x(box.west),
          y: y(box.north),
          width: x(box.east) - x(box.west),
          height: y(box.south) - y(box.north),
          fill: '#9cb9a6',
          'fill-opacity': 0.4,
          stroke: '#6f9a80',
          'stroke-width': 1.5,
          'stroke-dasharray': '5 4',
        }),
      );
    const cx = x(result.center.longitude),
      cy = y(result.center.latitude);
    fragment.append(
      svg('path', {
        d: `M${cx - 8} ${cy}h16 M${cx} ${cy - 8}v16`,
        stroke: '#275d51',
        'stroke-width': 2,
      }),
    );
  }
  const matching = new Set(result?.matches.map((p) => p.id) || []);
  for (const point of points.filter((p) => p.active)) {
    const mark = svg('circle', {
      cx: x(point.longitude),
      cy: y(point.latitude),
      r: matching.has(point.id) ? 5 : 3.5,
      fill: matching.has(point.id) ? '#cc8b47' : '#608977',
      tabindex: 0,
      role: 'button',
      'aria-label': `Edit ${point.name}`,
    });
    mark.append(svg('title', {}, `${point.name}: ${point.latitude}, ${point.longitude}`));
    mark.addEventListener('click', (event) => {
      event.stopPropagation();
      edit(point);
    });
    mark.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        edit(point);
      }
    });
    fragment.append(mark);
  }
  $('map').replaceChildren(fragment);
}
function renderResult() {
  draw();
  $('export-results').disabled = !result;
  $('metrics').replaceChildren();
  $('matches').replaceChildren();
  if (!result) {
    $('model').textContent = 'Run a radius search to inspect candidate bounds and exact matches.';
    $('match-count').textContent = 'No current search.';
    return;
  }
  for (const [value, label] of [
    [result.total, 'active records'],
    [result.candidates, 'index candidates'],
    [result.matches.length, 'exact matches'],
  ]) {
    const item = el('span');
    item.append(el('strong', value), el('span', label));
    $('metrics').append(item);
  }
  $('model').textContent = result.model;
  for (const point of result.matches.slice(0, 100)) {
    const row = el('article');
    row.className = 'match';
    const button = el('button', point.name);
    button.addEventListener('click', () => edit(point));
    const distance = el('strong', `${point.distance_km.toFixed(3)} km`);
    const coordinates = el(
      'small',
      `${point.latitude.toFixed(4)}°, ${point.longitude.toFixed(4)}°`,
    );
    row.append(button, distance, coordinates);
    $('matches').append(row);
  }
  $('match-count').textContent =
    `Showing ${Math.min(100, result.matches.length)} of ${result.matches.length} matches, ordered by distance. JSON exports all matches.`;
}
function invalidateSearch() {
  searchRevision++;
  result = null;
  renderResult();
}
async function search() {
  const ticket = ++searchRevision;
  try {
    const request = {
      latitude: numeric('lat'),
      longitude: numeric('lon'),
      radius: numeric('radius'),
    };
    status('Searching the spatial index…');
    const answer = await api('/api/search', request);
    if (ticket !== searchRevision) return;
    result = answer;
    renderResult();
    status(
      `${result.matches.length} exact matches from ${result.candidates} candidates across ${result.bounds.length} bounding rectangle${result.bounds.length === 1 ? '' : 's'}.`,
    );
  } catch (e) {
    if (ticket === searchRevision) {
      result = null;
      renderResult();
      status(e.message);
    }
  }
}
function records() {
  const filter = $('filter').value.trim().toLowerCase(),
    visible = points.filter(
      (p) => ($('archived').checked || p.active) && p.name.toLowerCase().includes(filter),
    );
  $('records').replaceChildren(
    ...visible.slice(0, 100).map((point) => {
      const row = el('div');
      row.className = 'record';
      const text = el('div');
      text.append(
        el('strong', point.name),
        el(
          'small',
          `${point.latitude.toFixed(4)}, ${point.longitude.toFixed(4)} · v${point.version}${point.active ? '' : ' · archived'}`,
        ),
      );
      const buttons = el('div');
      buttons.className = 'pair';
      const select = el('button', 'Edit'),
        archive = el('button', point.active ? 'Archive' : 'Restore');
      select.setAttribute('aria-label', `Edit record ${point.name}`);
      select.disabled = busy;
      archive.setAttribute('aria-label', `${point.active ? 'Archive' : 'Restore'} ${point.name}`);
      archive.dataset.write = '';
      archive.disabled = busy;
      select.addEventListener('click', () => edit(point));
      archive.addEventListener('click', () =>
        mutate(async () => {
          await api(`/api/points/${point.id}/${point.active ? 'archive' : 'restore'}`, {
            version: point.version,
          });
          if (editing?.id === point.id) clearEditor();
        }),
      );
      buttons.append(select, archive);
      row.append(text, buttons);
      return row;
    }),
  );
  $('record-count').textContent =
    `Showing ${Math.min(100, visible.length)} of ${visible.length} matching records · ${points.length} total, including archived.`;
  $('samples').disabled = busy || points.length > 0;
  $('samples').hidden = points.length > 0;
  $('export-points').disabled = !points.some((p) => p.active);
}
function edit(point) {
  if (busy) return;
  editing = { ...point };
  $('edit-title').textContent = `Edit #${point.id} · version ${point.version}`;
  $('name').value = point.name;
  $('point-lat').value = point.latitude;
  $('point-lon').value = point.longitude;
  $('save').textContent = 'Save changes';
  if (!$('records-panel').open) $('records-panel').showModal();
  $('name').focus();
}
function clearEditor() {
  editing = null;
  $('edit-title').textContent = 'Add a location';
  $('name').value = '';
  $('point-lat').value = 0;
  $('point-lon').value = 0;
  $('save').textContent = 'Add point';
}
async function refresh() {
  points = (await api('/api/points')).points;
  records();
  draw();
}
async function mutate(operation) {
  if (busy) return;
  busy = true;
  importRevision++;
  invalidateSearch();
  for (const id of ['name', 'point-lat', 'point-lon', 'cancel']) $(id).disabled = true;
  document.querySelectorAll('[data-write]').forEach((b) => (b.disabled = true));
  try {
    await operation();
    await refresh();
    await search();
  } catch (e) {
    status(e.message);
    try {
      await refresh();
    } catch {}
  } finally {
    busy = false;
    for (const id of ['name', 'point-lat', 'point-lon', 'cancel']) $(id).disabled = false;
    document.querySelectorAll('[data-write]').forEach((b) => (b.disabled = false));
    records();
  }
}
$('save').addEventListener('click', () => {
  let point;
  try {
    point = {
      name: $('name').value,
      latitude: numeric('point-lat'),
      longitude: numeric('point-lon'),
    };
  } catch (e) {
    status(e.message);
    return;
  }
  const selected = editing ? { ...editing } : null;
  mutate(async () => {
    if (selected)
      await api(`/api/points/${selected.id}/edit`, { version: selected.version, point });
    else await api('/api/points', { points: [point] });
    clearEditor();
  });
});
$('cancel').addEventListener('click', clearEditor);
$('samples').addEventListener('click', () =>
  mutate(async () => {
    const samples = await api('/api/samples');
    await api('/api/points', samples);
  }),
);
$('records-open').addEventListener('click', () => {
  $('records-status').textContent = '';
  $('records-panel').showModal();
});
$('records-close').addEventListener('click', () => $('records-panel').close());
$('search').addEventListener('click', search);
for (const id of ['lat', 'lon', 'radius']) $(id).addEventListener('input', invalidateSearch);
$('archived').addEventListener('change', records);
$('filter').addEventListener('input', records);
$('refresh').addEventListener('click', async () => {
  try {
    await refresh();
    await search();
  } catch (e) {
    status(e.message);
  }
});
$('preset').addEventListener('change', () => {
  const [lat, lon, radius] = {
    europe: [51, 0, 400],
    date: [-17, 179.9, 100],
    pole: [90, 0, 60],
    world: [0, 0, 20100],
  }[$('preset').value];
  $('lat').value = lat;
  $('lon').value = lon;
  $('radius').value = radius;
  invalidateSearch();
  search();
});
$('map').addEventListener('click', (event) => {
  const rect = $('map').getBoundingClientRect();
  $('lon').value = (((event.clientX - rect.left) / rect.width) * 360 - 180).toFixed(4);
  $('lat').value = (90 - ((event.clientY - rect.top) / rect.height) * 180).toFixed(4);
  invalidateSearch();
  search();
});
function download(name, value) {
  const url = URL.createObjectURL(
      new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }),
    ),
    a = el('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('export-results').addEventListener('click', () => download('spatial-search.json', result));
$('export-points').addEventListener('click', () =>
  download('spatial-points.json', {
    points: points
      .filter((p) => p.active)
      .map(({ name, latitude, longitude }) => ({ name, latitude, longitude })),
  }),
);
$('import').addEventListener('change', async () => {
  const file = $('import').files[0];
  if (!file) return;
  const ticket = ++importRevision;
  try {
    if (file.size > 400000) throw Error('Imports must be at most 400 KB.');
    const body = JSON.parse(await file.text());
    if (
      !body ||
      Array.isArray(body) ||
      Object.keys(body).length !== 1 ||
      !Object.hasOwn(body, 'points')
    )
      throw Error('Import JSON needs a points array only.');
    if (ticket !== importRevision) return;
    if (busy) throw Error('Wait for the current save before importing.');
    await mutate(() => api('/api/points', body));
  } catch (e) {
    status(e.message);
  } finally {
    $('import').value = '';
  }
});
refresh()
  .then(search)
  .catch((e) => status(e.message));
