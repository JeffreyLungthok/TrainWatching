/* Swiss train map - scheduled positions of every train, moving along OpenStreetMap tracks.
 *
 * Data (built by pipeline/build_data.py):
 *   data/network.json    stops + delta-encoded track geometry of every stop-to-stop leg
 *   data/timetable.json  routes, patterns (stop sequences + legs), timings, service calendars, trips
 *
 * URL parameters: ?time=2026-09-25T07:30 (Swiss local time)  &speed=60  &trail=6  &theme=light|dark
 *                 &view=8.31,47.05,11 (longitude,latitude,zoom)
 */
(() => {
  'use strict';

  const TZ = 'Europe/Zurich';
  const DAY = 86400;
  const params = new URLSearchParams(location.search);
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------------------------------
  // Categories and colours
  // ---------------------------------------------------------------------------------------
  const CATEGORIES = [
    { id: 'long', label: 'Long distance', hint: 'IC · EC · ICE · TGV · RJX',
      match: (c) => /^(IC|ICN|ICE|EC|EN|NJ|TGV|RJ|RJX|FR|EXT|HS|ECE|IC\d+|ICN\d+)$/.test(c) },
    { id: 'ir', label: 'InterRegio · RegioExpress', hint: 'IR · IRE · RE',
      match: (c) => /^(IR|IRE|RE|IR\d+|RE\d+)$/.test(c) },
    { id: 's', label: 'S-Bahn', hint: 'S · SN', match: (c) => /^(S|SN|S\d+)$/.test(c) },
    { id: 'pe', label: 'Panorama & mountain', hint: 'PE · rack railways',
      match: (c, t) => c === 'PE' || t === 107 || t === 116 },
    { id: 'r', label: 'Regional & other', hint: 'R · RB · others', match: () => true },
  ];

  const THEMES = {
    dark: {
      bg: [11, 15, 23], land: [19, 25, 37], landEdge: [52, 63, 86], lake: [17, 38, 62],
      canton: [255, 255, 255, 11], track: [118, 146, 196, 85], station: [200, 210, 228, 150],
      label: [180, 190, 208, 230], labelHalo: [11, 15, 23, 230], dotEdge: [11, 15, 23, 255],
      cats: { long: [255, 92, 92], ir: [255, 181, 71], s: [76, 195, 255], pe: [205, 150, 255], r: [110, 231, 150] },
      glow: 70,
    },
    light: {
      bg: [231, 235, 240], land: [252, 252, 250], landEdge: [170, 180, 196], lake: [196, 222, 245],
      canton: [30, 40, 60, 22], track: [84, 100, 130, 95], station: [70, 80, 100, 170],
      label: [55, 65, 85, 235], labelHalo: [252, 252, 250, 235], dotEdge: [255, 255, 255, 255],
      cats: { long: [214, 40, 57], ir: [219, 130, 0], s: [0, 116, 201], pe: [142, 68, 173], r: [34, 150, 84] },
      glow: 55,
    },
  };

  const CITIES = [
    ['Zürich', 8.5417, 47.3769, 1], ['Genève', 6.1432, 46.2044, 1], ['Basel', 7.5886, 47.5596, 1],
    ['Bern', 7.4474, 46.948, 1], ['Lausanne', 6.6323, 46.5197, 1], ['Luzern', 8.3093, 47.0502, 1],
    ['Lugano', 8.9511, 46.0037, 1], ['St. Gallen', 9.3767, 47.4245, 1], ['Winterthur', 8.7241, 47.4988, 2],
    ['Biel/Bienne', 7.2474, 47.1368, 2], ['Chur', 9.5327, 46.8508, 1], ['Thun', 7.628, 46.758, 2],
    ['Fribourg', 7.1619, 46.8065, 2], ['Neuchâtel', 6.9293, 46.99, 2], ['Sion', 7.3606, 46.2331, 1],
    ['Brig', 7.9878, 46.3159, 2], ['Olten', 7.9077, 47.3519, 2], ['Schaffhausen', 8.6349, 47.6959, 2],
    ['Bellinzona', 9.0244, 46.1946, 2], ['Interlaken', 7.8632, 46.6863, 2], ['Zug', 8.5155, 47.1662, 2],
    ['Aarau', 8.0444, 47.3925, 2], ['Davos', 9.8365, 46.8027, 2], ['Zermatt', 7.7491, 46.0207, 2],
    ['St. Moritz', 9.8385, 46.4983, 2], ['Locarno', 8.7995, 46.1708, 2], ['Montreux', 6.9106, 46.4312, 2],
    ['Yverdon', 6.6412, 46.7785, 2], ['Frauenfeld', 8.8988, 47.5576, 2], ['Solothurn', 7.5372, 47.2088, 2],
  ];

  // ---------------------------------------------------------------------------------------
  // Swiss time helpers (independent of the viewer's own time zone)
  // ---------------------------------------------------------------------------------------
  const zurichFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const dateFmt = new Intl.DateTimeFormat('en-US', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  function swissDate(ms) {
    const p = {};
    for (const part of dateFmt.formatToParts(new Date(ms))) p[part.type] = part.value;
    return `${p.weekday} ${p.day} ${p.month} ${p.year}`;
  }
  const offsetCache = new Map();

  function tzOffsetMs(ms) {
    const key = Math.floor(ms / 900000);
    let off = offsetCache.get(key);
    if (off === undefined) {
      const p = {};
      for (const part of zurichFmt.formatToParts(new Date(ms))) p[part.type] = part.value;
      const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
      off = asUtc - Math.floor(ms / 1000) * 1000;
      offsetCache.set(key, off);
    }
    return off;
  }

  function swissClock(ms) {
    const local = ms + tzOffsetMs(ms);
    const day = Math.floor(local / 86400000);
    return { day, secs: (local - day * 86400000) / 1000 };
  }

  function parseSwissLocal(str) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(str || '');
    if (!m) return null;
    const asUtc = Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
    let ms = asUtc - tzOffsetMs(asUtc);
    ms = asUtc - tzOffsetMs(ms);
    return ms;
  }

  const pad = (n) => String(n).padStart(2, '0');
  function hhmm(secs) {
    const s = ((Math.round(secs) % DAY) + DAY) % DAY;
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}`;
  }
  function hhmmss(secs) {
    const s = ((Math.floor(secs) % DAY) + DAY) % DAY;
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---------------------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------------------
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  const state = {
    theme: params.get('theme') === 'light' || params.get('theme') === 'dark' ? params.get('theme') : (prefersLight ? 'light' : 'dark'),
    themeVersion: 0,
    speed: Math.max(1, Math.min(3600, Number(params.get('speed')) || 1)),
    trailMin: Math.max(1, Math.min(20, Number(params.get('trail')) || 6)),
    enabled: Object.fromEntries(CATEGORIES.map((c) => [c.id, true])),
    anchorSim: parseSwissLocal(params.get('time')) ?? Date.now(),
    anchorPerf: performance.now(),
    zoom: 7.3,
  };
  let D = null;               // prepared data
  let deckgl = null;
  let built = null;           // active trip window
  let baseLayers = [];
  let frameNo = 0;
  let lastUi = 0;
  const perf = { frames: 0, jsMs: 0, rebuilds: 0, rebuildMs: 0 };

  const simNow = (perfNow = performance.now()) => state.anchorSim + (perfNow - state.anchorPerf) * state.speed;
  function reanchor(simMs) {
    state.anchorSim = simMs ?? simNow();
    state.anchorPerf = performance.now();
  }

  // ---------------------------------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------------------------------
  async function loadJSON(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  function categorize(cat, type) {
    const c = String(cat || '').toUpperCase();
    for (const k of CATEGORIES) if (k.match(c, type)) return k.id;
    return 'r';
  }

  function prepare(basemap, net, tt) {
    const legs = net.legs.map((a) => {
      const out = new Float64Array(a.length);
      let x = 0, y = 0;
      for (let i = 0; i < a.length; i += 2) {
        x += a[i]; y += a[i + 1];
        out[i] = x / 1e5; out[i + 1] = y / 1e5;
      }
      return out;
    });
    const timings = tt.timings.map((t) => {
      const rel = new Int32Array(t.length - 1);
      let acc = 0;
      for (let i = 1; i < t.length; i++) { acc += t[i]; rel[i - 1] = acc; }
      return { pattern: t[0], rel, dur: rel[rel.length - 1] };
    });
    let maxDur = 0;
    for (const t of timings) if (t.dur > maxDur) maxDur = t.dur;
    const services = tt.services.map((b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
    const T = tt.trips;
    const [y, m, d] = tt.meta.windowStart.split('-').map(Number);
    return {
      basemap, legs, timings, maxDur, services,
      stops: net.stops,
      patterns: tt.patterns,
      routes: tt.routes.map(([label, cat, type]) => ({ label, cat, type, catId: categorize(cat, type) })),
      headsigns: tt.headsigns,
      trips: {
        n: T.start.length,
        start: Int32Array.from(T.start), timing: Int32Array.from(T.timing),
        service: Int32Array.from(T.service), route: Int32Array.from(T.route),
        headsign: Int32Array.from(T.headsign), number: T.number,
      },
      windowStartDay: Date.UTC(y, m - 1, d) / 86400000,
      ndays: tt.meta.days,
      meta: tt.meta,
      geom: new Array(tt.patterns.length),
    };
  }

  const serviceOn = (svc, dayIdx) => (D.services[svc][dayIdx >> 3] >> (dayIdx & 7)) & 1;

  function lowerBound(arr, v) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < v) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // Geometry of a stop pattern: concatenated legs, cumulative metres, vertex index of each stop.
  function patternGeom(pi) {
    let g = D.geom[pi];
    if (g) return g;
    const [stops, refs] = D.patterns[pi];
    let total = 0;
    for (const r of refs) total += D.legs[Math.abs(r) - 1].length / 2;
    const coords = new Float64Array(total * 2);
    const cum = new Float64Array(total);
    const arrV = new Int32Array(stops.length);
    const depV = new Int32Array(stops.length);
    const kx = 111320 * Math.cos((46.8 * Math.PI) / 180), ky = 111132;
    let v = 0;
    for (let k = 0; k < refs.length; k++) {
      const r = refs[k];
      const leg = D.legs[Math.abs(r) - 1];
      const n = leg.length / 2;
      depV[k] = v;
      for (let i = 0; i < n; i++) {
        const j = r < 0 ? n - 1 - i : i;
        coords[2 * v] = leg[2 * j];
        coords[2 * v + 1] = leg[2 * j + 1];
        if (v > 0) {
          cum[v] = cum[v - 1] + Math.hypot((coords[2 * v] - coords[2 * v - 2]) * kx, (coords[2 * v + 1] - coords[2 * v - 1]) * ky);
        }
        v++;
      }
      arrV[k + 1] = v - 1;
    }
    depV[refs.length] = v - 1;
    g = { coords, cum, arrV, depV, stops };
    D.geom[pi] = g;
    return g;
  }

  // Per-vertex timestamps (seconds in the frame of the current service day), computed only for
  // the legs that overlap [from, to]. Returns the first vertex index and the timestamps.
  function tripSlice(g, rel, t0, from, to) {
    const ns = g.stops.length;
    let k0 = 0;
    while (k0 < ns - 2 && t0 + rel[2 * k0 + 2] < from) k0++;       // first leg arriving after `from`
    let k1 = k0;
    while (k1 < ns - 2 && t0 + rel[2 * k1 + 3] <= to) k1++;        // last leg departing before `to`
    const a = k0 === 0 ? 0 : g.arrV[k0];                            // include the dwell at stop k0
    const b = g.arrV[k1 + 1];
    const ts = new Float32Array(b - a + 1);
    if (k0 > 0) ts[0] = t0 + rel[2 * k0];
    for (let k = k0; k <= k1; k++) {
      const va = g.depV[k], vb = g.arrV[k + 1];
      const tA = t0 + rel[2 * k + 1], tB = t0 + rel[2 * k + 2];
      const cA = g.cum[va], span = g.cum[vb] - cA;
      for (let i = va; i <= vb; i++) {
        const f = span > 0 ? (g.cum[i] - cA) / span : (vb > va ? (i - va) / (vb - va) : 0);
        ts[i - a] = tA + (tB - tA) * f;
      }
    }
    return { a, ts };
  }

  // ---------------------------------------------------------------------------------------
  // Active trips around the current time
  // ---------------------------------------------------------------------------------------
  function rebuild(clock) {
    const trailSec = state.trailMin * 60;
    // look ahead a few minutes (or ~12 s of real time when fast-forwarding); rebuilding is cheap
    const windowSec = Math.max(300, state.speed * 12);
    const from = clock.secs - trailSec - 120;
    const to = clock.secs + windowSec;
    const dayIdx = clock.day - D.windowStartDay;
    const list = [];
    const T = D.trips;
    const theme = THEMES[state.theme];
    for (const off of [-1, 0, 1]) {
      const d = dayIdx + off;
      if (d < 0 || d >= D.ndays) continue;
      const shift = off * DAY;
      const lo = lowerBound(T.start, from - shift - D.maxDur);
      const hi = lowerBound(T.start, to - shift + 1);
      for (let i = lo; i < hi; i++) {
        if (!serviceOn(T.service[i], d)) continue;
        const tm = D.timings[T.timing[i]];
        const t0 = T.start[i] + shift;
        if (t0 + tm.dur < from) continue;
        const route = D.routes[T.route[i]];
        if (!state.enabled[route.catId]) continue;
        const g = patternGeom(tm.pattern);
        const { a, ts } = tripSlice(g, tm.rel, t0, from, to);
        list.push({
          i, g, tm, t0, t1: t0 + tm.dur, route,
          path: g.coords.subarray(2 * a, 2 * (a + ts.length)), ts,
          color: theme.cats[route.catId], pos: [0, 0], hint: 0,
        });
      }
    }
    built = { day: clock.day, from, to, windowSec, list, inWindow: dayIdx >= 0 && dayIdx < D.ndays };
  }

  function positionAt(tr, t) {
    const ts = tr.ts, p = tr.path, n = ts.length;
    let i = tr.hint;
    if (i >= n - 1 || ts[i] > t) i = 0;
    while (i < n - 2 && ts[i + 1] <= t) i++;
    tr.hint = i;
    const span = ts[i + 1] - ts[i];
    const f = span > 0 ? Math.min(1, Math.max(0, (t - ts[i]) / span)) : 1;
    tr.pos[0] = p[2 * i] + (p[2 * i + 2] - p[2 * i]) * f;
    tr.pos[1] = p[2 * i + 1] + (p[2 * i + 3] - p[2 * i + 1]) * f;
  }

  // ---------------------------------------------------------------------------------------
  // Layers
  // ---------------------------------------------------------------------------------------
  function makeBaseLayers() {
    const th = THEMES[state.theme];
    const feats = D.basemap.features;
    const byLayer = (name) => feats.filter((f) => f.properties.layer === name);
    const v = state.themeVersion;
    return [
      new deck.GeoJsonLayer({
        id: `land-${v}`, data: byLayer('country'), filled: true, stroked: true,
        getFillColor: th.land, getLineColor: th.landEdge, lineWidthUnits: 'pixels', getLineWidth: 1.2,
      }),
      new deck.GeoJsonLayer({
        id: `cantons-${v}`, data: byLayer('cantons'), filled: false, stroked: true,
        getLineColor: th.canton, lineWidthUnits: 'pixels', getLineWidth: 1,
      }),
      new deck.GeoJsonLayer({
        id: `lakes-${v}`, data: byLayer('lakes'), filled: true, stroked: false, getFillColor: th.lake,
      }),
      new deck.PathLayer({
        id: `tracks-${v}`, data: D.legs, getPath: (d) => d, positionFormat: 'XY', _pathType: 'open',
        getColor: th.track, widthUnits: 'pixels', getWidth: 1.1, jointRounded: true,
      }),
    ];
  }

  const CITIES_MAJOR = CITIES.filter((c) => c[3] === 1);
  function labelLayers() {
    const th = THEMES[state.theme];
    const v = state.themeVersion;
    const showStops = state.zoom >= 10.5;
    const allCities = state.zoom >= 8.2;
    return [
      new deck.ScatterplotLayer({
        id: `stops-${v}`, data: D.stops, visible: showStops, pickable: showStops,
        getPosition: (d) => [d[1], d[2]], getFillColor: th.bg, getLineColor: th.station,
        stroked: true, lineWidthUnits: 'pixels', getLineWidth: 1.5, radiusUnits: 'pixels', getRadius: 3.5,
      }),
      new deck.TextLayer({
        id: `cities-${v}`, data: allCities ? CITIES : CITIES_MAJOR,
        getPosition: (d) => [d[1], d[2]], getText: (d) => d[0],
        getColor: th.label, getSize: (d) => (d[3] === 1 ? 13 : 11.5), sizeUnits: 'pixels',
        fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif',
        fontWeight: 600, characterSet: 'auto', getTextAnchor: 'start', getAlignmentBaseline: 'center',
        getPixelOffset: [7, 0], fontSettings: { sdf: true, fontSize: 48, buffer: 6 },
        outlineWidth: 3, outlineColor: th.labelHalo,
        updateTriggers: { getSize: allCities },
      }),
    ];
  }

  function trainLayers(secs, running) {
    const th = THEMES[state.theme];
    const z = state.zoom;
    const scale = Math.min(2.2, Math.max(0.85, 0.85 + (z - 7.2) * 0.22));
    const v = state.themeVersion;
    return [
      new deck.TripsLayer({
        id: 'trails', data: built.list,
        getPath: (d) => d.path, getTimestamps: (d) => d.ts, getColor: (d) => d.color,
        positionFormat: 'XY', _pathType: 'open',
        widthUnits: 'pixels', getWidth: 2.6, widthScale: scale, widthMinPixels: 1.4,
        capRounded: true, jointRounded: true,
        trailLength: state.trailMin * 60, currentTime: secs, fadeTrail: true,
        updateTriggers: { getColor: v },
      }),
      new deck.ScatterplotLayer({
        id: 'glow', data: running,
        getPosition: (d) => d.pos, getFillColor: (d) => [...d.color, th.glow],
        radiusUnits: 'pixels', getRadius: 7, radiusScale: scale,
        updateTriggers: { getPosition: frameNo, getFillColor: v },
      }),
      new deck.ScatterplotLayer({
        id: 'trains', data: running, pickable: true, autoHighlight: true, highlightColor: [255, 255, 255, 90],
        getPosition: (d) => d.pos, getFillColor: (d) => d.color, getLineColor: th.dotEdge,
        stroked: true, lineWidthUnits: 'pixels', getLineWidth: 1, radiusUnits: 'pixels', getRadius: 3.4, radiusScale: scale,
        updateTriggers: { getPosition: frameNo, getFillColor: v, getLineColor: v },
      }),
    ];
  }

  // ---------------------------------------------------------------------------------------
  // Tooltip
  // ---------------------------------------------------------------------------------------
  function trainInfo(tr, secs) {
    const T = D.trips;
    const rel = tr.tm.rel;
    const stops = tr.g.stops;
    const t = secs - tr.t0;
    const name = (k) => D.stops[stops[k]][0];
    let status = '';
    for (let k = 1; k < stops.length; k++) {
      const arr = rel[2 * k], dep = rel[2 * k + 1];
      if (t < arr) { status = `Next stop <b>${esc(name(k))}</b> at ${hhmm(tr.t0 + arr)}`; break; }
      if (t <= dep && k < stops.length - 1) { status = `At <b>${esc(name(k))}</b> until ${hhmm(tr.t0 + dep)}`; break; }
    }
    if (!status) status = `Arrived at <b>${esc(name(stops.length - 1))}</b>`;
    const num = T.number[tr.i];
    const c = tr.color;
    return `
      <div class="t-head">
        <span class="t-badge" style="background:rgb(${c[0]},${c[1]},${c[2]})">${esc(tr.route.label)}</span>
        <span class="t-dest">→ ${esc(D.headsigns[T.headsign[tr.i]])}</span>
      </div>
      <div class="t-row">${status}</div>
      <div class="t-row">${esc(name(0))} ${hhmm(tr.t0)} → ${esc(name(stops.length - 1))} ${hhmm(tr.t1)}${num ? ` · train ${esc(num)}` : ''}</div>`;
  }

  // own tooltip element so that it works for mouse hover and for taps on touch screens
  function showTip(info) {
    const tip = $('tip');
    const { object, layer } = info || {};
    let html = null;
    if (object && layer && layer.id === 'trains') html = trainInfo(object, swissClock(simNow()).secs);
    else if (object && layer && layer.id.startsWith('stops')) html = `<b>${esc(object[0])}</b>`;
    if (!html) { tip.hidden = true; return; }
    tip.innerHTML = html;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    const x = Math.min(info.x + 14, window.innerWidth - w - 8);
    const y = info.y + 14 + h > window.innerHeight - 8 ? info.y - h - 14 : info.y + 14;
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }

  // ---------------------------------------------------------------------------------------
  // UI
  // ---------------------------------------------------------------------------------------
  function applyTheme() {
    document.documentElement.dataset.theme = state.theme;
    state.themeVersion++;
    if (!D) return;
    baseLayers = makeBaseLayers();
    if (built) {
      const cats = THEMES[state.theme].cats;
      for (const tr of built.list) tr.color = cats[tr.route.catId];
    }
    buildLegend();
  }

  function buildLegend() {
    const ul = $('legend');
    const cats = THEMES[state.theme].cats;
    ul.innerHTML = '';
    for (const c of CATEGORIES) {
      const li = document.createElement('li');
      const col = cats[c.id];
      li.innerHTML = `<button type="button" aria-pressed="${state.enabled[c.id]}" style="--c: rgb(${col.join(',')})">
          <span class="swatch" aria-hidden="true"></span>
          <span class="name">${esc(c.label)}<small>${esc(c.hint)}</small></span>
          <span class="n" data-count="${c.id}">0</span></button>`;
      li.querySelector('button').addEventListener('click', (e) => {
        state.enabled[c.id] = !state.enabled[c.id];
        e.currentTarget.setAttribute('aria-pressed', String(state.enabled[c.id]));
        built = null;
      });
      ul.appendChild(li);
    }
  }

  function setSpeed(speed) {
    reanchor();
    state.speed = speed;
    for (const b of document.querySelectorAll('.seg button')) b.setAttribute('aria-pressed', String(Number(b.dataset.speed) === speed));
    built = null;
  }

  function wireUi() {
    for (const b of document.querySelectorAll('.seg button')) {
      b.addEventListener('click', () => setSpeed(Number(b.dataset.speed)));
      b.setAttribute('aria-pressed', String(Number(b.dataset.speed) === state.speed));
    }
    $('now').addEventListener('click', () => { reanchor(Date.now()); setSpeed(1); });
    const trail = $('trail');
    trail.value = state.trailMin;
    $('trailOut').textContent = `${state.trailMin} min`;
    trail.addEventListener('input', () => {
      state.trailMin = Number(trail.value);
      $('trailOut').textContent = `${state.trailMin} min`;
      built = null;
    });
    $('theme').addEventListener('click', () => {
      state.theme = state.theme === 'dark' ? 'light' : 'dark';
      applyTheme();
    });
    $('collapse').addEventListener('click', () => {
      const p = $('panel');
      const collapsed = p.classList.toggle('collapsed');
      $('collapse').setAttribute('aria-expanded', String(!collapsed));
    });
    if (window.innerWidth <= 640) $('panel').classList.add('collapsed');
  }

  function updateUi(simMs, clock, counts, total) {
    $('time').textContent = hhmmss(clock.secs);
    $('date').textContent = swissDate(simMs);
    const live = state.speed === 1 && Math.abs(simMs - Date.now()) < 5000;
    const mode = $('mode');
    mode.classList.toggle('live', live);
    $('modeText').textContent = live ? 'Live' : (state.speed === 1 ? 'Timetable' : `${state.speed}× speed`);
    $('count').textContent = total.toLocaleString('en-US');
    for (const el of document.querySelectorAll('[data-count]')) el.textContent = counts[el.dataset.count].toLocaleString('en-US');
    if (built && !built.inWindow) {
      $('feed').textContent = `No timetable data for this day. Data covers ${D.meta.windowStart} + ${D.ndays - 1} days.`;
    } else {
      const gen = (D.meta.generated || '').slice(0, 10);
      $('feed').textContent = `${D.meta.feed}${gen ? ` · built ${gen}` : ''}`;
    }
  }

  // ---------------------------------------------------------------------------------------
  // Main loop
  // ---------------------------------------------------------------------------------------
  function frame(now) {
    const simMs = simNow(now);
    const clock = swissClock(simMs);
    if (!built || clock.day !== built.day || clock.secs > built.to - built.windowSec * 0.25 ||
        clock.secs < built.from + state.trailMin * 60 + 60) {
      const r0 = performance.now();
      rebuild(clock);
      perf.rebuilds++; perf.rebuildMs += performance.now() - r0;
    }
    frameNo++;
    const running = [];
    const counts = Object.fromEntries(CATEGORIES.map((c) => [c.id, 0]));
    for (const tr of built.list) {
      if (clock.secs >= tr.t0 && clock.secs <= tr.t1) {
        positionAt(tr, clock.secs);
        running.push(tr);
        counts[tr.route.catId]++;
      }
    }
    deckgl.setProps({ layers: [...baseLayers, ...trainLayers(clock.secs, running), ...labelLayers()] });
    if (now - lastUi > 200) {
      lastUi = now;
      updateUi(simMs, clock, counts, running.length);
    }
    perf.frames++; perf.jsMs += performance.now() - now;
    requestAnimationFrame(frame);
  }

  function initialView() {
    const v = (params.get('view') || '').split(',').map(Number);
    if (v.length === 3 && v.every(Number.isFinite)) return { longitude: v[0], latitude: v[1], zoom: v[2], pitch: 0, bearing: 0 };
    const el = $('map');
    const w = el.clientWidth || window.innerWidth, h = el.clientHeight || window.innerHeight;
    const small = w <= 640;
    const panelH = $('panel').offsetHeight || 150;
    const padding = small
      ? { top: 44, bottom: Math.min(Math.round(h * 0.45), panelH + 24), left: 12, right: 12 }
      : { top: 40, bottom: 40, left: 340, right: 40 };
    try {
      const vp = new deck.WebMercatorViewport({ width: w, height: h });
      const { longitude, latitude, zoom } = vp.fitBounds([[5.96, 45.82], [10.49, 47.81]], { padding });
      return { longitude, latitude, zoom, pitch: 0, bearing: 0 };
    } catch (e) {
      return { longitude: 8.23, latitude: 46.8, zoom: small ? 6.2 : 7.2, pitch: 0, bearing: 0 };
    }
  }

  async function main() {
    applyTheme();
    wireUi();
    let basemap, net, tt;
    try {
      $('loadMsg').textContent = 'Loading timetable and tracks…';
      [basemap, net, tt] = await Promise.all([loadJSON('basemap.json'), loadJSON('data/network.json'), loadJSON('data/timetable.json')]);
    } catch (err) {
      console.error(err);
      $('loading').classList.add('error');
      $('loadMsg').innerHTML = 'No train data found. Build it first with<br><code>python pipeline/build_data.py</code><br>and serve the <code>web</code> folder over HTTP (see README).';
      return;
    }
    D = prepare(basemap, net, tt);
    applyTheme();
    const view = initialView();
    state.zoom = view.zoom;
    deckgl = new deck.Deck({
      parent: $('map'),
      initialViewState: { ...view, minZoom: 5.5, maxZoom: 15.5 },
      controller: { dragRotate: false, touchRotate: false, keyboard: true },
      views: new deck.MapView({ repeat: false }),
      layers: [],
      pickingRadius: 6,
      onHover: showTip,
      onClick: showTip,
      getCursor: ({ isHovering, isDragging }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'),
      onViewStateChange: ({ viewState }) => { state.zoom = viewState.zoom; $('tip').hidden = true; },
      useDevicePixels: Math.min(2, window.devicePixelRatio || 1),
    });
    $('loading').classList.add('done');
    // small hook for debugging / automated checks
    window.swissTrainMap = {
      deck: () => deckgl, trips: () => (built ? built.list : []), data: () => D, perf,
      benchRebuild: () => { const t = performance.now(); rebuild(swissClock(simNow())); return performance.now() - t; },
    };
    requestAnimationFrame(frame);
  }

  main();
})();
