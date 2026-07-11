const state = {
  uploadId: null,
  zones: [],
  selected: new Set(),
  segments: [],
  worldBounds: null,
  colors: new Map(),
  previewTimer: null,
  zoom: 1,
  panX: 0,
  panY: 0,
  isPanning: false,
  lastPointerX: 0,
  lastPointerY: 0,
};

const els = {
  fileInput: document.getElementById("fileInput"),
  modeInput: document.getElementById("modeInput"),
  angleInput: document.getElementById("angleInput"),
  spacingInput: document.getElementById("spacingInput"),
  manualSpacingInput: document.getElementById("manualSpacingInput"),
  radiusInput: document.getElementById("radiusInput"),
  minAreaInput: document.getElementById("minAreaInput"),
  offsetStartInput: document.getElementById("offsetStartInput"),
  offsetSpacingInput: document.getElementById("offsetSpacingInput"),
  offsetCountInput: document.getElementById("offsetCountInput"),
  hatchAllInput: document.getElementById("hatchAllInput"),
  clearBtn: document.getElementById("clearBtn"),
  exportBtn: document.getElementById("exportBtn"),
  zoneCount: document.getElementById("zoneCount"),
  selectedCount: document.getElementById("selectedCount"),
  segmentCount: document.getElementById("segmentCount"),
  status: document.getElementById("status"),
  canvas: document.getElementById("canvas"),
};

const ctx = els.canvas.getContext("2d");

async function readJsonResponse(res, fallbackMessage) {
  const raw = await res.text();
  let json = null;
  try {
    json = raw ? JSON.parse(raw) : {};
  } catch {
    if (!res.ok) {
      const compact = raw.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      throw new Error(compact || fallbackMessage);
    }
    throw new Error(fallbackMessage);
  }

  if (!res.ok) {
    throw new Error(json.error || fallbackMessage);
  }
  return json;
}

function setStatus(text, isError = false) {
  els.status.textContent = text;
  els.status.style.color = isError ? "#aa2f2f" : "#2a7f62";
}

function numericControls() {
  return {
    mode: els.modeInput.value || "hatch",
    angle: Number(els.angleInput.value || 45),
    spacing: Number(els.spacingInput.value || 0.2),
    useManualSpacing: els.manualSpacingInput.checked,
    laserRadius: Number(els.radiusInput.value || 0.01),
    minArea: Number(els.minAreaInput.value || 0.3),
    offsetStart: Number(els.offsetStartInput.value || 0.2),
    offsetSpacing: Number(els.offsetSpacingInput.value || 0.2),
    offsetCount: Number(els.offsetCountInput.value || 3),
  };
}

function updateModeControlState() {
  const isContourMode = els.modeInput.value === "contour_offsets";
  els.angleInput.disabled = isContourMode;
  els.spacingInput.disabled = isContourMode || !els.manualSpacingInput.checked;
  els.manualSpacingInput.disabled = isContourMode;
  els.radiusInput.disabled = isContourMode;
  els.minAreaInput.disabled = isContourMode;
  els.offsetStartInput.disabled = !isContourMode;
  els.offsetSpacingInput.disabled = !isContourMode;
  els.offsetCountInput.disabled = !isContourMode;
}

function updateSpacingControlState() {
  const manual = els.manualSpacingInput.checked;
  if (els.modeInput.value !== "contour_offsets") {
    els.spacingInput.disabled = !manual;
  }
  if (manual) {
    els.spacingInput.title = "Manual hatch spacing";
  } else {
    els.spacingInput.title = "Auto spacing = 2 x laser radius";
  }
}

function effectiveSelectedIds() {
  if (!state.uploadId) {
    return [];
  }
  if (els.hatchAllInput.checked) {
    return state.zones.map((z) => z.id);
  }
  return Array.from(state.selected);
}

function zoneColor(id) {
  if (state.colors.has(id)) {
    return state.colors.get(id);
  }
  const base = (Number(id) * 53) % 360;
  const color = `hsla(${base}, 70%, 56%, 0.28)`;
  state.colors.set(id, color);
  return color;
}

function computeBounds() {
  const points = [];
  for (const zone of state.zones) {
    points.push(...zone.points);
  }
  for (const seg of state.segments) {
    points.push(seg[0], seg[1]);
  }

  if (!points.length) {
    state.worldBounds = null;
    return;
  }

  let minX = points[0][0];
  let minY = points[0][1];
  let maxX = points[0][0];
  let maxY = points[0][1];

  for (const p of points) {
    minX = Math.min(minX, p[0]);
    minY = Math.min(minY, p[1]);
    maxX = Math.max(maxX, p[0]);
    maxY = Math.max(maxY, p[1]);
  }

  state.worldBounds = { minX, minY, maxX, maxY };
}

function resetView() {
  state.zoom = 1;
  state.panX = 0;
  state.panY = 0;
}

function fitCanvasResolution() {
  const dpr = Math.max(window.devicePixelRatio || 1, 1);
  const rect = els.canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (els.canvas.width !== width || els.canvas.height !== height) {
    els.canvas.width = width;
    els.canvas.height = height;
  }
}

function worldToScreen(x, y) {
  const b = state.worldBounds;
  if (!b) {
    return [0, 0];
  }

  const pad = 28;
  const w = els.canvas.width;
  const h = els.canvas.height;
  const sx = (w - pad * 2) / Math.max(1e-9, b.maxX - b.minX);
  const sy = (h - pad * 2) / Math.max(1e-9, b.maxY - b.minY);
  const s = Math.min(sx, sy);

  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  const viewCx = w / 2;
  const viewCy = h / 2;

  const basePx = viewCx + (x - cx) * s;
  const basePy = viewCy - (y - cy) * s;
  const px = viewCx + (basePx - viewCx) * state.zoom + state.panX;
  const py = viewCy + (basePy - viewCy) * state.zoom + state.panY;
  return [px, py];
}

function screenToWorld(px, py) {
  const b = state.worldBounds;
  if (!b) {
    return [0, 0];
  }

  const pad = 28;
  const w = els.canvas.width;
  const h = els.canvas.height;
  const sx = (w - pad * 2) / Math.max(1e-9, b.maxX - b.minX);
  const sy = (h - pad * 2) / Math.max(1e-9, b.maxY - b.minY);
  const s = Math.min(sx, sy);

  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;

  const viewCx = w / 2;
  const viewCy = h / 2;
  const unpannedX = px - state.panX;
  const unpannedY = py - state.panY;
  const unzoomedX = viewCx + (unpannedX - viewCx) / state.zoom;
  const unzoomedY = viewCy + (unpannedY - viewCy) / state.zoom;

  const x = cx + (unzoomedX - viewCx) / s;
  const y = cy - (unzoomedY - viewCy) / s;
  return [x, y];
}

function pointInPolygon(point, points) {
  let inside = false;
  const x = point[0];
  const y = point[1];

  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const xi = points[i][0];
    const yi = points[i][1];
    const xj = points[j][0];
    const yj = points[j][1];

    const intersects = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-12) + xi;
    if (intersects) {
      inside = !inside;
    }
  }

  return inside;
}

function zonesContainingPoint(world) {
  const containing = [];
  for (const zone of state.zones) {
    if (pointInPolygon(world, zone.points)) {
      containing.push(zone);
    }
  }
  containing.sort((a, b) => Number(a.area || 0) - Number(b.area || 0));
  return containing;
}

function draw() {
  fitCanvasResolution();
  ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);

  if (!state.worldBounds) {
    return;
  }

  for (const zone of state.zones) {
    if (zone.points.length < 3) {
      continue;
    }

    ctx.beginPath();
    const first = worldToScreen(zone.points[0][0], zone.points[0][1]);
    ctx.moveTo(first[0], first[1]);

    for (let i = 1; i < zone.points.length; i++) {
      const p = worldToScreen(zone.points[i][0], zone.points[i][1]);
      ctx.lineTo(p[0], p[1]);
    }
    ctx.closePath();

    const selected = els.hatchAllInput.checked || state.selected.has(zone.id);
    ctx.fillStyle = zoneColor(zone.id);
    ctx.fill();
    ctx.lineWidth = selected ? 3 : 1.2;
    ctx.strokeStyle = selected ? "#d35f2b" : "rgba(0,0,0,0.45)";
    ctx.stroke();
  }

  ctx.save();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(14, 42, 29, 0.9)";
  for (const seg of state.segments) {
    const p1 = worldToScreen(seg[0][0], seg[0][1]);
    const p2 = worldToScreen(seg[1][0], seg[1][1]);
    ctx.beginPath();
    ctx.moveTo(p1[0], p1[1]);
    ctx.lineTo(p2[0], p2[1]);
    ctx.stroke();
  }
  ctx.restore();
}

function refreshStats() {
  const selectedIds = effectiveSelectedIds();
  els.zoneCount.textContent = String(state.zones.length);
  els.selectedCount.textContent = String(selectedIds.length);
  els.segmentCount.textContent = String(state.segments.length);
  els.exportBtn.disabled = !state.uploadId || selectedIds.length === 0;
}

async function uploadDXF(file) {
  const body = new FormData();
  body.append("file", file);

  setStatus("Uploading and parsing DXF...");
  const res = await fetch("/api/upload", { method: "POST", body });
  const json = await readJsonResponse(res, "Upload failed.");

  state.uploadId = json.uploadId;
  state.zones = json.zones || [];
  state.selected.clear();
  state.segments = [];
  state.colors.clear();
  computeBounds();
  resetView();
  refreshStats();
  draw();

  if (state.zones.length) {
    setStatus(`Detected ${state.zones.length} zones. Click zones in the preview to select.`);
  } else {
    setStatus("No closed zones found in this DXF.");
  }
}

async function requestPreview() {
  const selectedIds = effectiveSelectedIds();
  if (!state.uploadId || selectedIds.length === 0) {
    state.segments = [];
    computeBounds();
    refreshStats();
    draw();
    return;
  }

  const controls = numericControls();
  if (controls.mode === "contour_offsets") {
    if (!(controls.offsetStart >= 0) || !(controls.offsetSpacing >= 0) || !(controls.offsetCount > 0)) {
      setStatus("Contour offset values must be non-negative, with count > 0.", true);
      return;
    }
  } else {
    if (controls.useManualSpacing && !(controls.spacing > 0)) {
      setStatus("Spacing must be greater than 0.", true);
      return;
    }
    if (!controls.useManualSpacing && !(controls.laserRadius > 0)) {
      setStatus("Auto spacing needs laser radius > 0. Or enable manual spacing.", true);
      return;
    }
    if (!(controls.minArea >= 0)) {
      setStatus("Minimum area must be >= 0.", true);
      return;
    }
  }

  const payload = {
    uploadId: state.uploadId,
    selectedIds,
    ...controls,
  };

  const res = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = await readJsonResponse(res, "Preview failed.");

  state.segments = json.segments || [];
  computeBounds();
  refreshStats();
  draw();
  const effectiveSpacing = Number(json.effectiveSpacing || controls.spacing);
  const stats = json.stats || {};
  const filteredSmall = Number(stats.filteredSmall || 0);
  const filteredCentroid = Number(stats.filteredCentroid || 0);
  const filteredNarrow = Number(stats.filteredNarrow || 0);
  if (controls.mode === "contour_offsets") {
    setStatus(
      `Contour offset preview: ${state.segments.length} segments, start ${controls.offsetStart.toFixed(4)}, spacing ${controls.offsetSpacing.toFixed(4)}, count ${controls.offsetCount}.`
    );
  } else {
    setStatus(
      `Preview: ${state.segments.length} segments, spacing ${effectiveSpacing.toFixed(4)}, filtered small ${filteredSmall}, filtered narrow ${filteredNarrow}, centroid-merged ${filteredCentroid}.`
    );
  }
}

function schedulePreview() {
  if (state.previewTimer) {
    clearTimeout(state.previewTimer);
  }
  state.previewTimer = setTimeout(() => {
    requestPreview().catch((err) => setStatus(err.message, true));
  }, 180);
}

async function exportDXF() {
  const selectedIds = effectiveSelectedIds();
  if (!state.uploadId || selectedIds.length === 0) {
    return;
  }

  const controls = numericControls();
  const payload = {
    uploadId: state.uploadId,
    selectedIds,
    ...controls,
  };

  setStatus("Building DXF export...");
  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    await readJsonResponse(res, "Export failed.");
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "hatched_output.dxf";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus("Export complete: hatched_output.dxf");
}

els.fileInput.addEventListener("change", (event) => {
  const input = event.target;
  const file = input.files && input.files[0];
  if (!file) {
    return;
  }

  uploadDXF(file).catch((err) => {
    setStatus(err.message, true);
  });
});

for (const input of [
  els.modeInput,
  els.angleInput,
  els.spacingInput,
  els.radiusInput,
  els.minAreaInput,
  els.offsetStartInput,
  els.offsetSpacingInput,
  els.offsetCountInput,
]) {
  input.addEventListener("input", () => {
    if (input === els.modeInput) {
      if (els.modeInput.value === "contour_offsets") {
        els.hatchAllInput.checked = true;
      }
      updateModeControlState();
      updateSpacingControlState();
      refreshStats();
      draw();
    }
    schedulePreview();
  });
}

els.manualSpacingInput.addEventListener("change", () => {
  updateSpacingControlState();
  schedulePreview();
});

els.hatchAllInput.addEventListener("change", () => {
  refreshStats();
  draw();
  schedulePreview();
});

els.clearBtn.addEventListener("click", () => {
  state.selected.clear();
  schedulePreview();
  refreshStats();
  draw();
});

els.exportBtn.addEventListener("click", () => {
  exportDXF().catch((err) => setStatus(err.message, true));
});

els.canvas.addEventListener("click", (event) => {
  if (!state.zones.length || !state.worldBounds || els.hatchAllInput.checked) {
    return;
  }

  const rect = els.canvas.getBoundingClientRect();
  const dpr = els.canvas.width / Math.max(1, rect.width);
  const px = (event.clientX - rect.left) * dpr;
  const py = (event.clientY - rect.top) * dpr;
  const world = screenToWorld(px, py);

  const containing = zonesContainingPoint(world);
  const hit = containing.length ? containing[0].id : null;

  if (!hit) {
    return;
  }

  if (state.selected.has(hit)) {
    state.selected.delete(hit);
  } else {
    state.selected.add(hit);
  }

  schedulePreview();
  refreshStats();
  draw();
});

els.canvas.addEventListener(
  "wheel",
  (event) => {
    if (!state.zones.length || !state.worldBounds) {
      return;
    }

    const rect = els.canvas.getBoundingClientRect();
    const dpr = els.canvas.width / Math.max(1, rect.width);
    const px = (event.clientX - rect.left) * dpr;
    const py = (event.clientY - rect.top) * dpr;
    event.preventDefault();

    if (!event.altKey) {
      const before = screenToWorld(px, py);
      const factor = event.deltaY > 0 ? 0.9 : 1.1;
      state.zoom = Math.min(25, Math.max(0.4, state.zoom * factor));
      const afterScreen = worldToScreen(before[0], before[1]);
      state.panX += px - afterScreen[0];
      state.panY += py - afterScreen[1];
      draw();
      setStatus(`Zoom ${state.zoom.toFixed(2)}x. Alt+wheel cycles overlapping zones.`);
      return;
    }

    if (els.hatchAllInput.checked) {
      return;
    }

    const world = screenToWorld(px, py);
    const containing = zonesContainingPoint(world);

    if (containing.length <= 1) {
      return;
    }

    const ids = containing.map((z) => z.id);
    const currentIndex = ids.findIndex((id) => state.selected.has(id));
    const baseIndex = currentIndex >= 0 ? currentIndex : 0;
    const direction = event.deltaY > 0 ? 1 : -1;
    const nextIndex = (baseIndex + direction + ids.length) % ids.length;

    if (currentIndex >= 0) {
      state.selected.delete(ids[currentIndex]);
    }
    state.selected.add(ids[nextIndex]);

    schedulePreview();
    refreshStats();
    draw();
    setStatus(
      `Wheel selected overlapping zone ${ids[nextIndex]} (${nextIndex + 1}/${ids.length}).`
    );
  },
  { passive: false }
);

els.canvas.addEventListener("mousedown", (event) => {
  if (event.button !== 1) {
    return;
  }
  event.preventDefault();
  state.isPanning = true;
  state.lastPointerX = event.clientX;
  state.lastPointerY = event.clientY;
});

window.addEventListener("mousemove", (event) => {
  if (!state.isPanning) {
    return;
  }
  const rect = els.canvas.getBoundingClientRect();
  const dpr = els.canvas.width / Math.max(1, rect.width);
  state.panX += (event.clientX - state.lastPointerX) * dpr;
  state.panY += (event.clientY - state.lastPointerY) * dpr;
  state.lastPointerX = event.clientX;
  state.lastPointerY = event.clientY;
  draw();
});

window.addEventListener("mouseup", () => {
  state.isPanning = false;
});

els.canvas.addEventListener("dblclick", () => {
  resetView();
  draw();
  setStatus("View reset.");
});

window.addEventListener("resize", () => {
  draw();
});

refreshStats();
updateModeControlState();
updateSpacingControlState();
draw();
