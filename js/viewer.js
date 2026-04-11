/**
 * JTWC Typhoon Forecast Viewer — viewer.js  (v3)
 * Leaflet map with coloured storm tracks, wind-radii circles,
 * interactive popups and a live Data API panel.
 */

'use strict';

// ─── Configuration ────────────────────────────────────────────────────────────

const CONFIG = {
  dataUrl:         'data/storms.json',
  refreshInterval: 30 * 60 * 1000,   // 30 min
  defaultCenter:   [15, 135],
  defaultZoom:     4,
  tileUrl: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  tileAttribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
    ' contributors &copy; <a href="https://carto.com/">CARTO</a>',
};

// ─── Intensity helpers ────────────────────────────────────────────────────────

const INTENSITY = [
  { max: 34,       color: '#7ec8e3', label: 'Tropical Depression',       code: 'TD'  },
  { max: 64,       color: '#00d4ff', label: 'Tropical Storm',            code: 'TS'  },
  { max: 83,       color: '#aaff00', label: 'Typhoon (Category 1)',       code: 'TY1' },
  { max: 96,       color: '#ffd700', label: 'Typhoon (Category 2)',       code: 'TY2' },
  { max: 113,      color: '#ff8c00', label: 'Typhoon (Category 3)',       code: 'TY3' },
  { max: 130,      color: '#ff3a3a', label: 'Typhoon (Category 4)',       code: 'TY4' },
  { max: Infinity, color: '#c800ff', label: 'Super Typhoon (Category 5)', code: 'ST'  },
];

function getIntensity(kt) {
  return INTENSITY.find(s => kt < s.max) || INTENSITY.at(-1);
}

function coordStr(lat, lon) {
  return `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'}, ` +
         `${Math.abs(lon).toFixed(1)}°${lon >= 0 ? 'E' : 'W'}`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toUTCString().replace(':00 GMT', ' UTC');
}

function pressureStr(mb) {
  return mb > 0 ? `${mb} mb` : '—';
}

// ─── Map ──────────────────────────────────────────────────────────────────────

const map = L.map('map', {
  center: CONFIG.defaultCenter, zoom: CONFIG.defaultZoom,
  zoomControl: false, attributionControl: true,
});
L.tileLayer(CONFIG.tileUrl, {
  attribution: CONFIG.tileAttribution, subdomains: 'abcd', maxZoom: 19,
}).addTo(map);
L.control.zoom({ position: 'bottomright' }).addTo(map);

// Layer groups
const trackLayer   = L.layerGroup().addTo(map);
const markerLayer  = L.layerGroup().addTo(map);
const currentLayer = L.layerGroup().addTo(map);
const radiiLayer   = L.layerGroup().addTo(map);   // wind-radii circles

let stormBounds = null;

// ─── Wind-radii rendering ─────────────────────────────────────────────────────

const RADII_STYLES = {
  '064': { color: '#ff3a3a', fillOpacity: 0.06, weight: 1.5, opacity: 0.55 },
  '050': { color: '#ff8c00', fillOpacity: 0.06, weight: 1.2, opacity: 0.45 },
  '034': { color: '#ffd700', fillOpacity: 0.04, weight: 1.0, opacity: 0.35 },
};

function addWindRadii(pos) {
  const radiiData = pos.wind_radii_nm;
  if (!radiiData) return;

  // Draw smallest threshold first (largest circle) → largest threshold on top
  ['034', '050', '064'].forEach(thr => {
    const qd = radiiData[thr];
    if (!qd) return;
    // Use the max of the four quadrants as a conservative circle radius
    const maxNm = Math.max(qd.NE || 0, qd.SE || 0, qd.SW || 0, qd.NW || 0);
    if (maxNm <= 0) return;

    const style = RADII_STYLES[thr] || RADII_STYLES['034'];
    L.circle([pos.lat, pos.lon], {
      radius:      maxNm * 1852,     // nm → metres
      color:       style.color,
      weight:      style.weight,
      opacity:     style.opacity,
      fillColor:   style.color,
      fillOpacity: style.fillOpacity,
      interactive: false,
    }).addTo(radiiLayer);
  });
}

// ─── Marker factory ───────────────────────────────────────────────────────────

function makeIcon(windKt, isCurrent, tau) {
  const { color } = getIntensity(windKt);
  const size = isCurrent ? 22 : 13;
  const pulse = isCurrent ? ' pulse' : '';
  const label = (!isCurrent && tau > 0)
    ? `<span class="tau-label">+${tau}h</span>` : '';

  return L.divIcon({
    className: '',
    html: `<div class="storm-dot${pulse}" style="
      width:${size}px;height:${size}px;
      background:${color};
      border:${isCurrent ? 3 : 2}px solid #fff;
      box-shadow:0 0 ${isCurrent ? 10 : 5}px ${color};
    ">${label}</div>`,
    iconSize:   [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 4)],
  });
}

// ─── Popup builder ────────────────────────────────────────────────────────────

function buildRadiiHtml(radiiData) {
  if (!radiiData || !Object.keys(radiiData).length) return '';
  const rows = ['064', '050', '034']
    .filter(t => radiiData[t])
    .map(t => {
      const q = radiiData[t];
      const avg = Math.round((q.NE + q.SE + q.SW + q.NW) / 4);
      const max = Math.max(q.NE, q.SE, q.SW, q.NW);
      return `<tr>
        <td>${t} kt winds</td>
        <td>NE ${q.NE} / SE ${q.SE} / SW ${q.SW} / NW ${q.NW} nm
          <span class="unit-alt">avg ${avg} nm · max ${max} nm</span></td>
      </tr>`;
    }).join('');
  if (!rows) return '';
  return `<tr><td colspan="2" class="popup-section-head">Wind Radii</td></tr>${rows}`;
}

function buildPopup(pos, storm) {
  const { color, label } = getIntensity(pos.wind_kt);
  const isInit   = pos.tau === 0;
  const tauTag   = isInit
    ? '<span class="popup-tag current-tag">Current / Initial</span>'
    : `<span class="popup-tag forecast-tag">+${pos.tau}h Forecast</span>`;
  const finalTag = storm.is_final_warning && isInit
    ? '<span class="popup-tag final-tag">Final Warning</span>' : '';

  const stormName = storm.name
    ? `${storm.name} <small>(${storm.id})</small>` : storm.id;

  return `
    <div class="popup-wrap">
      <div class="popup-header" style="border-left:4px solid ${color}">
        <div class="popup-storm-name">${stormName}</div>
        <div class="popup-basin">${storm.basin_name || storm.basin}</div>
      </div>
      <div class="popup-badges">
        ${tauTag}${finalTag}
        <span class="popup-tag intensity-tag"
          style="background:${color}20;color:${color};border:1px solid ${color}40">
          ${label}
        </span>
      </div>
      <table class="popup-table">
        <tr><td>Time (UTC)</td><td>${fmtTime(pos.datetime)}</td></tr>
        <tr><td>Position</td><td>${coordStr(pos.lat, pos.lon)}</td></tr>
        <tr>
          <td>Max Wind</td>
          <td><strong style="color:${color}">${pos.wind_kt} kt</strong>
            <span class="unit-alt">${pos.wind_mph} mph / ${pos.wind_kmh} km/h</span></td>
        </tr>
        <tr>
          <td>Pressure</td>
          <td>${pressureStr(pos.pressure_mb)}</td>
        </tr>
        ${buildRadiiHtml(pos.wind_radii_nm)}
      </table>
    </div>`;
}

// ─── Storm rendering ──────────────────────────────────────────────────────────

function renderStorm(storm) {
  if (!storm.forecast || !storm.forecast.length) return;
  const positions = storm.forecast;

  // Colour-coded track segments (dashed for forecast, solid for current pos)
  for (let i = 0; i < positions.length - 1; i++) {
    const { color } = getIntensity(positions[i].wind_kt);
    L.polyline([[positions[i].lat, positions[i].lon],
                [positions[i + 1].lat, positions[i + 1].lon]], {
      color, weight: 2.5, opacity: 0.85,
      dashArray: positions[i].tau > 0 ? '5 5' : null,
    }).addTo(trackLayer);
  }

  // Wind radii around the current (tau=0) position
  const cur = positions.find(p => p.tau === 0);
  if (cur) addWindRadii(cur);

  // Markers at every forecast position
  positions.forEach(pos => {
    const isCur = pos.tau === 0;
    const marker = L.marker([pos.lat, pos.lon], {
      icon: makeIcon(pos.wind_kt, isCur, pos.tau),
      zIndexOffset: isCur ? 1000 : 0,
    }).bindPopup(buildPopup(pos, storm), {
      maxWidth: 340, className: 'custom-popup',
    });
    marker.addTo(isCur ? currentLayer : markerLayer);
  });

  // Accumulate map bounds
  const lls = positions.map(p => [p.lat, p.lon]);
  stormBounds = stormBounds
    ? stormBounds.extend(L.latLngBounds(lls))
    : L.latLngBounds(lls);
}

// ─── Sidebar card builder ─────────────────────────────────────────────────────

function buildStormCard(storm) {
  const cur = storm.current;
  if (!cur) return '';
  const { color, label, code } = getIntensity(cur.wind_kt);
  const name = storm.name || `Storm ${storm.number}`;
  const finalBadge = storm.is_final_warning
    ? '<span class="final-inline-badge">FINAL WARNING</span>' : '';

  return `
    <div class="storm-card" onclick="focusStorm(${cur.lat},${cur.lon})">
      <div class="storm-card-badge"
           style="background:${color}20;border:1px solid ${color}40">
        <span class="badge-code" style="color:${color}">${code}</span>
        <span class="badge-label">${label}</span>
      </div>
      <div class="storm-card-body">
        <div class="storm-card-name">${name}
          <span class="storm-card-id">${storm.id}</span>
          ${finalBadge}
        </div>
        <div class="storm-card-basin">${storm.basin_name || storm.basin}</div>
        <div class="storm-card-stats">
          <div class="stat">
            <span class="stat-label">Advisory:</span>
            <span>${fmtTime(storm.advisory_time)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Wind:</span>
            <strong style="color:${color}">${cur.wind_kt} kt</strong>
            <small>(${cur.wind_mph} mph / ${cur.wind_kmh} km/h)</small>
          </div>
          <div class="stat">
            <span class="stat-label">Pressure:</span>
            <strong>${pressureStr(cur.pressure_mb)}</strong>
          </div>
          <div class="stat">
            <span class="stat-label">Position:</span>
            <span>${coordStr(cur.lat, cur.lon)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Forecast steps:</span>
            <span>${storm.forecast.map(p => p.tau === 0 ? '0' : `+${p.tau}h`).join(' · ')}</span>
          </div>
        </div>
      </div>
    </div>`;
}

// ─── Data loading ─────────────────────────────────────────────────────────────

async function loadData() {
  const btn = document.getElementById('btn-refresh');
  if (btn) btn.classList.add('spinning');
  try {
    const res = await fetch(`${CONFIG.dataUrl}?_=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderAll(await res.json());
  } catch (e) {
    console.error(e);
    showError(e.message);
  } finally {
    if (btn) btn.classList.remove('spinning');
  }
}

function renderAll(data) {
  trackLayer.clearLayers();
  markerLayer.clearLayers();
  currentLayer.clearLayers();
  radiiLayer.clearLayers();
  stormBounds = null;

  const el = id => document.getElementById(id);
  if (el('update-time'))       el('update-time').textContent = fmtTime(data.generated);
  if (el('storm-count-badge')) el('storm-count-badge').textContent =
    data.storm_count ?? data.storms.length;

  const storms = data.storms || [];
  const listEl = el('storm-list');

  if (!storms.length) {
    if (listEl) listEl.innerHTML = `
      <div class="no-storms">
        <div class="no-storm-icon">🌤</div>
        <p>No active tropical cyclones</p>
        <p class="no-storm-sub">The JTWC is not tracking any active storms.</p>
      </div>`;
  } else {
    if (listEl) listEl.innerHTML = storms.map(buildStormCard).join('');
    storms.forEach(renderStorm);
    if (stormBounds) map.fitBounds(stormBounds.pad(0.3), { maxZoom: 6 });
  }

  updateApiSection();
}

function showError(msg) {
  const el = document.getElementById('storm-list');
  if (el) el.innerHTML = `
    <div class="no-storms">
      <div class="no-storm-icon">⚠️</div>
      <p>Could not load storm data</p>
      <p class="no-storm-sub">${msg}</p>
    </div>`;
}

// ─── API panel ────────────────────────────────────────────────────────────────

function updateApiSection() {
  const base = location.origin +
    location.pathname.replace(/\/[^/]*$/, '');
  const apiUrl = `${base}/data/storms.json`;

  const urlEl = document.getElementById('api-url-display');
  if (urlEl) urlEl.textContent = apiUrl;

  const snip = document.getElementById('snippet-display');
  if (snip) snip.textContent =
`fetch('${apiUrl}')
  .then(r => r.json())
  .then(data => {
    // data.storms — array of active storms
    // data.generated — ISO timestamp of last update
    data.storms.forEach(s => {
      const c = s.current;
      console.log(
        s.name, s.id,
        c.wind_kt + ' kt', c.pressure_mb + ' mb',
        c.lat + '°, ' + c.lon + '°'
      );
      // c.wind_radii_nm — { '034': {NE,SE,SW,NW}, '050': ..., '064': ... }
      // s.forecast — array of positions at +0h, +12h, +24h … +120h
    });
  });`;
}

function copyApiUrl() {
  const urlEl = document.getElementById('api-url-display');
  if (!urlEl) return;
  navigator.clipboard.writeText(urlEl.textContent.trim()).then(() => {
    const btn = document.getElementById('copy-btn');
    if (btn) {
      const orig = btn.innerHTML;
      btn.textContent = '✓ Copied!';
      setTimeout(() => { btn.innerHTML = orig; }, 2000);
    }
  });
}

// ─── Map interactions ─────────────────────────────────────────────────────────

function focusStorm(lat, lon) {
  map.setView([lat, lon], 6, { animate: true });
  currentLayer.eachLayer(layer => {
    const ll = layer.getLatLng();
    if (Math.abs(ll.lat - lat) < 0.5 && Math.abs(ll.lng - lon) < 0.5) {
      layer.openPopup();
    }
  });
}

function fitAllStorms() {
  if (stormBounds) map.fitBounds(stormBounds.pad(0.3), { maxZoom: 6, animate: true });
  else map.setView(CONFIG.defaultCenter, CONFIG.defaultZoom, { animate: true });
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────

loadData();
setInterval(loadData, CONFIG.refreshInterval);
