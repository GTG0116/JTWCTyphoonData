/**
 * JTWC Typhoon Forecast Viewer — viewer.js
 * Interactive Leaflet.js map for displaying JTWC ATCF storm data.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

const CONFIG = {
  dataUrl: 'data/storms.json',
  refreshInterval: 30 * 60 * 1000,      // 30 minutes
  defaultCenter: [15, 135],             // Western Pacific
  defaultZoom: 4,
  tileUrl: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  tileAttribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
    ' contributors &copy; <a href="https://carto.com/">CARTO</a>',
  maxTileZoom: 19,
};

// ─────────────────────────────────────────────────────────────────────────────
// Intensity helpers (kept in sync with Python script values)
// ─────────────────────────────────────────────────────────────────────────────

const INTENSITY_SCALE = [
  { max: 34,  color: '#7ec8e3', label: 'Tropical Depression',   short: 'TD'  },
  { max: 64,  color: '#00d4ff', label: 'Tropical Storm',        short: 'TS'  },
  { max: 83,  color: '#aaff00', label: 'Typhoon (Cat 1)',        short: 'TY1' },
  { max: 96,  color: '#ffd700', label: 'Typhoon (Cat 2)',        short: 'TY2' },
  { max: 113, color: '#ff8c00', label: 'Typhoon (Cat 3)',        short: 'TY3' },
  { max: 130, color: '#ff3a3a', label: 'Typhoon (Cat 4)',        short: 'TY4' },
  { max: Infinity, color: '#c800ff', label: 'Super Typhoon (Cat 5)', short: 'ST' },
];

function getIntensity(windKt) {
  return INTENSITY_SCALE.find(s => windKt < s.max) || INTENSITY_SCALE.at(-1);
}

function coordStr(lat, lon) {
  const latS = `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'}`;
  const lonS = `${Math.abs(lon).toFixed(1)}°${lon >= 0 ? 'E' : 'W'}`;
  return `${latS}, ${lonS}`;
}

function fmtTime(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  return d.toUTCString().replace(':00 GMT', ' UTC');
}

// ─────────────────────────────────────────────────────────────────────────────
// Map initialisation
// ─────────────────────────────────────────────────────────────────────────────

const map = L.map('map', {
  center: CONFIG.defaultCenter,
  zoom: CONFIG.defaultZoom,
  zoomControl: false,
  attributionControl: true,
});

L.tileLayer(CONFIG.tileUrl, {
  attribution: CONFIG.tileAttribution,
  subdomains: 'abcd',
  maxZoom: CONFIG.maxTileZoom,
}).addTo(map);

// Custom Leaflet zoom control placed bottom-right
L.control.zoom({ position: 'bottomright' }).addTo(map);

// ─────────────────────────────────────────────────────────────────────────────
// Layer groups
// ─────────────────────────────────────────────────────────────────────────────

const trackLayer    = L.layerGroup().addTo(map);
const markerLayer   = L.layerGroup().addTo(map);
const currentLayer  = L.layerGroup().addTo(map);

// Bounds accumulator for fitAllStorms()
let stormBounds = null;

// ─────────────────────────────────────────────────────────────────────────────
// Marker factories
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Create a DivIcon circle for a forecast position.
 * @param {number} windKt
 * @param {boolean} isCurrent - true for tau=0 (uses pulse animation)
 * @param {number} tau        - used to label non-zero forecast positions
 */
function makeIcon(windKt, isCurrent, tau) {
  const { color } = getIntensity(windKt);
  const size = isCurrent ? 22 : 13;
  const pulseClass = isCurrent ? ' pulse' : '';
  const label = (!isCurrent && tau > 0)
    ? `<span class="tau-label">+${tau}h</span>`
    : '';

  const html = `
    <div class="storm-dot${pulseClass}" style="
      width:${size}px;height:${size}px;
      background:${color};
      border:${isCurrent ? 3 : 2}px solid #fff;
      box-shadow:0 0 ${isCurrent ? 10 : 5}px ${color};
    ">${label}</div>`;

  return L.divIcon({
    className: '',
    html,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 4)],
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Popup builder
// ─────────────────────────────────────────────────────────────────────────────

function buildPopup(pos, storm) {
  const { color, label } = getIntensity(pos.wind_kt);
  const isInitial = pos.tau === 0;
  const tauLabel  = isInitial
    ? '<span class="popup-tag current-tag">Current / Initial</span>'
    : `<span class="popup-tag forecast-tag">+${pos.tau}h Forecast</span>`;

  const stormName = storm.name
    ? `${storm.name} <small>(${storm.id})</small>`
    : storm.id;

  return `
    <div class="popup-wrap">
      <div class="popup-header" style="border-left:4px solid ${color}">
        <div class="popup-storm-name">${stormName}</div>
        <div class="popup-basin">${storm.basin_name || storm.basin}</div>
      </div>

      <div class="popup-badges">
        ${tauLabel}
        <span class="popup-tag intensity-tag" style="background:${color}20;color:${color};border:1px solid ${color}40">
          ${label}
        </span>
      </div>

      <table class="popup-table">
        <tr>
          <td>Time (UTC)</td>
          <td>${fmtTime(pos.datetime)}</td>
        </tr>
        <tr>
          <td>Position</td>
          <td>${coordStr(pos.lat, pos.lon)}</td>
        </tr>
        <tr>
          <td>Max Wind</td>
          <td>
            <strong style="color:${color}">${pos.wind_kt} kt</strong>
            <span class="unit-alt">${pos.wind_mph} mph / ${pos.wind_kmh} km/h</span>
          </td>
        </tr>
        <tr>
          <td>Pressure</td>
          <td>${pos.pressure_mb > 0 ? `<strong>${pos.pressure_mb} mb</strong>` : '—'}</td>
        </tr>
      </table>
    </div>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Storm rendering
// ─────────────────────────────────────────────────────────────────────────────

function renderStorm(storm) {
  if (!storm.forecast || storm.forecast.length === 0) return;

  const positions = storm.forecast;

  // ── Track polyline (colour-coded by leading intensity)
  const latLngs = positions.map(p => [p.lat, p.lon]);

  // Build coloured segments between consecutive positions
  for (let i = 0; i < latLngs.length - 1; i++) {
    const { color } = getIntensity(positions[i].wind_kt);
    L.polyline([latLngs[i], latLngs[i + 1]], {
      color,
      weight: 2.5,
      opacity: 0.85,
      dashArray: positions[i].tau > 0 ? '5,5' : null,
    }).addTo(trackLayer);
  }

  // ── Forecast position markers
  positions.forEach(pos => {
    const isCurrent = pos.tau === 0;
    const marker = L.marker([pos.lat, pos.lon], {
      icon: makeIcon(pos.wind_kt, isCurrent, pos.tau),
      zIndexOffset: isCurrent ? 1000 : 0,
    });

    marker.bindPopup(buildPopup(pos, storm), {
      maxWidth: 320,
      className: 'custom-popup',
    });

    if (isCurrent) {
      marker.addTo(currentLayer);
    } else {
      marker.addTo(markerLayer);
    }
  });

  // Accumulate bounds
  const bounds = L.latLngBounds(latLngs);
  stormBounds = stormBounds ? stormBounds.extend(bounds) : bounds;
}

// ─────────────────────────────────────────────────────────────────────────────
// Sidebar storm card builder
// ─────────────────────────────────────────────────────────────────────────────

function buildStormCard(storm) {
  const cur = storm.current;
  if (!cur) return '';

  const { color, label, short } = getIntensity(cur.wind_kt);
  const name = storm.name ? storm.name : `Storm ${storm.number}`;

  return `
    <div class="storm-card" onclick="focusStorm(${cur.lat},${cur.lon},'${storm.id}')">
      <div class="storm-card-badge" style="background:${color}20;border:1px solid ${color}40">
        <span class="badge-code" style="color:${color}">${short}</span>
        <span class="badge-label">${label}</span>
      </div>
      <div class="storm-card-body">
        <div class="storm-card-name">${name}
          <span class="storm-card-id">${storm.id}</span>
        </div>
        <div class="storm-card-basin">${storm.basin_name || storm.basin}</div>
        <div class="storm-card-stats">
          <div class="stat">
            <svg viewBox="0 0 20 20" fill="${color}"><circle cx="10" cy="10" r="8" fill="none" stroke="${color}" stroke-width="2"/>
              <path d="M10 6v4l3 3" stroke="${color}" stroke-width="1.5" stroke-linecap="round" fill="none"/>
            </svg>
            <span>${fmtTime(cur.datetime)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Wind:</span>
            <strong style="color:${color}">${cur.wind_kt} kt</strong>
            <small>(${cur.wind_mph} mph)</small>
          </div>
          ${cur.pressure_mb > 0 ? `
          <div class="stat">
            <span class="stat-label">Pressure:</span>
            <strong>${cur.pressure_mb} mb</strong>
          </div>` : ''}
          <div class="stat">
            <span class="stat-label">Position:</span>
            <span>${coordStr(cur.lat, cur.lon)}</span>
          </div>
        </div>
      </div>
    </div>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Data loading
// ─────────────────────────────────────────────────────────────────────────────

async function loadData() {
  const refreshBtn = document.getElementById('btn-refresh');
  if (refreshBtn) refreshBtn.classList.add('spinning');

  try {
    const res = await fetch(`${CONFIG.dataUrl}?_=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderAll(data);
  } catch (err) {
    console.error('Failed to load storm data:', err);
    showError(err.message);
  } finally {
    if (refreshBtn) refreshBtn.classList.remove('spinning');
  }
}

function renderAll(data) {
  // Clear previous layers
  trackLayer.clearLayers();
  markerLayer.clearLayers();
  currentLayer.clearLayers();
  stormBounds = null;

  // Update header
  const updEl = document.getElementById('update-time');
  if (updEl) updEl.textContent = fmtTime(data.generated);

  // Update storm count badge
  const countBadge = document.getElementById('storm-count-badge');
  if (countBadge) countBadge.textContent = data.storm_count ?? data.storms.length;

  // Render storms
  const storms = data.storms || [];

  if (storms.length === 0) {
    const listEl = document.getElementById('storm-list');
    if (listEl) {
      listEl.innerHTML = `
        <div class="no-storms">
          <div class="no-storm-icon">🌤</div>
          <p>No active tropical cyclones</p>
          <p class="no-storm-sub">The JTWC is not tracking any active storms at this time.</p>
        </div>`;
    }
  } else {
    const cards = storms.map(buildStormCard).join('');
    const listEl = document.getElementById('storm-list');
    if (listEl) listEl.innerHTML = cards;

    storms.forEach(renderStorm);

    // Fit map to all storms
    if (stormBounds) {
      map.fitBounds(stormBounds.pad(0.25), { maxZoom: 6 });
    }
  }

  // Update API section
  updateApiSection(data);
}

function showError(msg) {
  const listEl = document.getElementById('storm-list');
  if (listEl) {
    listEl.innerHTML = `
      <div class="no-storms error-state">
        <div class="no-storm-icon">⚠️</div>
        <p>Could not load storm data</p>
        <p class="no-storm-sub">${msg}</p>
        <p class="no-storm-sub">Data refreshes automatically. Try again shortly.</p>
      </div>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// API section
// ─────────────────────────────────────────────────────────────────────────────

function updateApiSection(data) {
  const apiUrl = `${location.origin}${location.pathname.replace(/\/[^/]*$/, '')}/data/storms.json`;

  const urlEl = document.getElementById('api-url-display');
  if (urlEl) urlEl.textContent = apiUrl;

  const snippet = document.getElementById('snippet-display');
  if (snippet) {
    snippet.textContent = `fetch('${apiUrl}')
  .then(r => r.json())
  .then(data => {
    // data.storms — array of active storms
    // data.generated — last update timestamp
    data.storms.forEach(storm => {
      const cur = storm.current;
      console.log(
        storm.name, storm.id,
        cur.wind_kt + ' kt', cur.pressure_mb + ' mb',
        cur.lat + '°, ' + cur.lon + '°'
      );
    });
  });`;
  }
}

function copyApiUrl() {
  const urlEl = document.getElementById('api-url-display');
  if (!urlEl) return;
  navigator.clipboard.writeText(urlEl.textContent.trim())
    .then(() => {
      const btn = document.getElementById('copy-btn');
      if (btn) {
        btn.textContent = '✓ Copied!';
        setTimeout(() => { btn.innerHTML = `<svg viewBox="0 0 20 20" fill="currentColor"><path d="M8 3a1 1 0 000 2h2a1 1 0 100-2H8z"/><path d="M6 4a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2V6a2 2 0 00-2-2h-1a1 1 0 110-2h1a4 4 0 014 4v8a4 4 0 01-4 4H6a4 4 0 01-4-4V6a4 4 0 014-4h1a1 1 0 000 2H6z"/></svg> Copy`; }, 2000);
      }
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Map interactions
// ─────────────────────────────────────────────────────────────────────────────

function focusStorm(lat, lon, stormId) {
  map.setView([lat, lon], 6, { animate: true });
  // Open popup for this storm's current marker
  currentLayer.eachLayer(layer => {
    const ll = layer.getLatLng();
    if (Math.abs(ll.lat - lat) < 0.5 && Math.abs(ll.lng - lon) < 0.5) {
      layer.openPopup();
    }
  });
}

function fitAllStorms() {
  if (stormBounds) {
    map.fitBounds(stormBounds.pad(0.25), { maxZoom: 6, animate: true });
  } else {
    map.setView(CONFIG.defaultCenter, CONFIG.defaultZoom, { animate: true });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sidebar toggle (mobile)
// ─────────────────────────────────────────────────────────────────────────────

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// ─────────────────────────────────────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────────────────────────────────────

// Initial load
loadData();

// Auto-refresh
setInterval(loadData, CONFIG.refreshInterval);
