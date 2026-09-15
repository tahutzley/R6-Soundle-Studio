import {
  calculateMapDistance,
  calculateRoundScore,
  formatDistance,
  isTargetFloorCorrect,
  loadScoringConfig,
  targetPositionForFloor,
} from "/game-assets/js/scoring.mjs?v=20260914-alternate-end-floor";

const $ = (selector) => document.querySelector(selector);

const state = {
  catalog: { maps: [], operators: [], assetVersion: "" },
  scoring: null,
  sets: [],
  captures: [],
  publishAttempts: [],
  publisher: { configured: false },
  productionChallenges: { available: false, challenges: [] },
  publishQueue: { running: false, pending: 0, jobs: [] },
  libraryKind: "daily",
  current: null,
  roundIndex: 0,
  floorKey: "",
  tool: "listenerPos",
  dragging: null,
  operatorQuery: "",
  importScan: null,
  importTargetSetId: null,
};

const markerInfo = {
  listenerPos: { label: "Listener", color: "#46a8ff" },
  operatorStartPos: { label: "Runner Start", color: "#ef8a2e" },
  targetPos: { label: "Runner End", color: "#78dca3" },
  guessPos: { label: "Guess", color: "#f2d024" },
};

const RECRUIT_ICON_URL = "/game-assets/operators/svg/recruit_gray.svg";
const STUDIO_API_VERSION = 7;
const mapView = { zoom: 1, minZoom: 1, maxZoom: 6, centerX: .5, centerY: .5 };
const mapPointers = new Map();
let mapGesture = null;
let autoSaveTimer = null;
let autoSavePromise = Promise.resolve();
let activePublishSetId = null;
let publishProgressTimer = null;
let publishQueueTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error(
      `Studio API returned an unexpected response (${response.status}). Restart Studio, then refresh this page.`,
    );
  }
  let result;
  try {
    result = JSON.parse(await response.text());
  } catch {
    throw new Error("Studio API returned invalid data. Restart Studio, then refresh this page.");
  }
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

async function assertServerCompatibility() {
  const health = await api("/api/health");
  if (health.apiVersion !== STUDIO_API_VERSION) {
    throw new Error("Studio was updated while its server was running. Restart Studio, then refresh this page.");
  }
}

function wideCoordinateFrame(image) {
  const wide = image?.wide_crop_box;
  const square = image?.square_crop_box;
  if (!Array.isArray(wide) || wide.length !== 4 || !Array.isArray(square) || square.length !== 4) {
    return { x: 0, y: 0, width: 1, height: 1 };
  }
  const width = wide[2] - wide[0];
  const height = wide[3] - wide[1];
  if (!width || !height) return { x: 0, y: 0, width: 1, height: 1 };
  return {
    x: (square[0] - wide[0]) / width,
    y: (square[1] - wide[1]) / height,
    width: (square[2] - square[0]) / width,
    height: (square[3] - square[1]) / height,
  };
}

async function loadWideCatalog() {
  const catalog = await api("/api/catalog");
  const response = await fetch("/game-assets/maps/blueprint_manifest_wide_upscaled.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`Wide map manifest failed to load (${response.status})`);
  const manifest = await response.json();
  const mapsBySlug = new Map(catalog.maps.map((map) => [map.slug, map]));
  for (const image of manifest.images || []) {
    const file = image.ai_output_file;
    if (image.selector_enabled === false || !file || !file.startsWith("wide-upscaled/")) continue;
    const floor = mapsBySlug.get(image.map_slug)?.floors.find((item) => item.key === image.floor_key);
    if (!floor) continue;
    floor.imageUrl = `/game-assets/maps/${file}`;
    floor.coordinateFrame = wideCoordinateFrame(image);
    floor.coordinateSize = Number(image.coordinate_size) || 2048;
    floor.assetWidth = Number(image.final_output_width || image.output_width) || floor.coordinateSize;
    floor.assetHeight = Number(image.final_output_height || image.output_height) || floor.coordinateSize;
    floor.pixelsPerMeter = Number(image.pixels_per_meter) || floor.pixelsPerMeter;
  }
  catalog.assetVersion = String(manifest.settings?.refreshedAt || catalog.assetVersion || "wide-upscaled");
  return catalog;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function selectedMap() {
  return state.catalog.maps.find((map) => map.slug === state.current?.mapSlug);
}

function selectedFloor() {
  return selectedMap()?.floors.find((floor) => floor.key === state.floorKey);
}

function floorControlLabel(floor) {
  if (floor.key === "basement") return "B";
  if (floor.key === "tunnel") return "T";
  if (/^floor-\d+$/.test(floor.key)) return `${Number(floor.key.split("-")[1])}F`;
  if (/^\d+f$/i.test(floor.key)) return floor.key.toUpperCase();
  return floor.label;
}

function floorStatusLabel(floorKey, floors = selectedMap()?.floors || []) {
  return floors.find((floor) => floor.key === floorKey)?.label || floorKey || "Unassigned";
}

function markerToolStatus(position, floors) {
  return position ? floorStatusLabel(position.floorKey, floors) : "Unassigned";
}

function targetToolStatus(round, floors) {
  const primary = markerToolStatus(round?.targetPos, floors);
  return round?.alternateTargetFloorKey
    ? `${primary} + ${floorStatusLabel(round.alternateTargetFloorKey, floors)}`
    : primary;
}

function listenerDirectionStatus(listener) {
  if (!listener) return "N/A";
  return `${Number(listener.angle || 0).toFixed(1)}°`;
}

function markerToolDetails(round = selectedRound(), floors = selectedMap()?.floors || []) {
  const details = [
    ["listenerPos", "listener", "Listener", markerToolStatus(round?.listenerPos, floors)],
    ["listenerAngle", "direction", "Direction", listenerDirectionStatus(round?.listenerPos)],
    ["operatorStartPos", "start", "Runner Start", markerToolStatus(round?.operatorStartPos, floors)],
    ["targetPos", "target", "Runner End", targetToolStatus(round, floors)],
  ];
  if (state.current?.kind === "example") {
    details.push(["guessPos", "guess", "Guess", markerToolStatus(round?.guessPos, floors)]);
  }
  return details;
}

function exampleResult(round = selectedRound()) {
  if (state.current?.kind !== "example" || !state.scoring || !round?.guessPos || !round?.targetPos) {
    return null;
  }
  const target = targetPositionForFloor(round, round.guessPos.floorKey);
  const floor = selectedMap()?.floors.find((item) => item.key === target.floorKey);
  if (!floor) return null;
  const distance = calculateMapDistance(round.guessPos, target, floor);
  const floorCorrect = isTargetFloorCorrect(round, round.guessPos.floorKey);
  return {
    distance,
    floorCorrect,
    ...calculateRoundScore(state.scoring, distance, floorCorrect),
  };
}

function renderExampleResult(round = selectedRound()) {
  const element = $("#exampleResult");
  const result = exampleResult(round);
  element.hidden = !result;
  if (!result) return;
  $("#exampleDistance").textContent = `${formatDistance(result.distance)} m`;
  $("#examplePoints").textContent = `${result.points.toLocaleString()} pts`;
  const penalty = $("#exampleFloorPenalty");
  penalty.hidden = result.floorCorrect;
  penalty.textContent = result.floorCorrect
    ? ""
    : `Wrong floor −${result.floorPenalty.toLocaleString()} pts`;
  element.setAttribute(
    "aria-label",
    `${formatDistance(result.distance)} meters from target, ${result.points} points${result.floorCorrect ? "" : ", wrong floor penalty applied"}`,
  );
}

function updateMarkerToolStatuses() {
  const details = new Map(markerToolDetails().map(([tool, , label, status]) => [tool, { label, status }]));
  document.querySelectorAll("#markerTools [data-tool]").forEach((button) => {
    const detail = details.get(button.dataset.tool);
    if (!detail) return;
    const statusElement = button.querySelector("small");
    if (statusElement) statusElement.textContent = detail.status;
    button.setAttribute("aria-label", `${detail.label}: ${detail.status}`);
  });
}

function coordinateFrame() {
  return selectedFloor()?.coordinateFrame || { x: 0, y: 0, width: 1, height: 1 };
}

function coordinateBounds(frame = coordinateFrame()) {
  return {
    minX: -frame.x / frame.width,
    maxX: (1 - frame.x) / frame.width,
    minY: -frame.y / frame.height,
    maxY: (1 - frame.y) / frame.height,
  };
}

function toAssetPoint(position) {
  const frame = coordinateFrame();
  return {
    ...position,
    x: frame.x + position.x * frame.width,
    y: frame.y + position.y * frame.height,
  };
}

function toMapPoint(position) {
  const frame = coordinateFrame();
  const bounds = coordinateBounds(frame);
  return {
    x: clamp((position.x - frame.x) / frame.width, bounds.minX, bounds.maxX),
    y: clamp((position.y - frame.y) / frame.height, bounds.minY, bounds.maxY),
  };
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(maximum, Math.max(minimum, value));
}

function viewportPoint(position) {
  const rect = $("#mapCanvas").getBoundingClientRect();
  return {
    x: rect.width / 2 + (position.x - mapView.centerX) * rect.width * mapView.zoom,
    y: rect.height / 2 + (position.y - mapView.centerY) * rect.height * mapView.zoom,
  };
}

function assetPointFromClient(clientX, clientY) {
  const rect = $("#mapCanvas").getBoundingClientRect();
  return {
    x: mapView.centerX + (clientX - rect.left - rect.width / 2) / (rect.width * mapView.zoom),
    y: mapView.centerY + (clientY - rect.top - rect.height / 2) / (rect.height * mapView.zoom),
  };
}

function constrainMapView() {
  const half = .5 / mapView.zoom;
  mapView.centerX = clamp(mapView.centerX, half, 1 - half);
  mapView.centerY = clamp(mapView.centerY, half, 1 - half);
}

function applyMapView(zoom, centerX, centerY) {
  mapView.zoom = clamp(zoom, mapView.minZoom, mapView.maxZoom);
  mapView.centerX = centerX;
  mapView.centerY = centerY;
  constrainMapView();
  drawMarkers();
  updateMapZoomControls();
}

function zoomMapAt(zoom, screenPoint = null) {
  const rect = $("#mapCanvas").getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const point = screenPoint || { x: rect.width / 2, y: rect.height / 2 };
  const anchor = {
    x: mapView.centerX + (point.x - rect.width / 2) / (rect.width * mapView.zoom),
    y: mapView.centerY + (point.y - rect.height / 2) / (rect.height * mapView.zoom),
  };
  const nextZoom = clamp(zoom, mapView.minZoom, mapView.maxZoom);
  applyMapView(
    nextZoom,
    anchor.x - (point.x - rect.width / 2) / (rect.width * nextZoom),
    anchor.y - (point.y - rect.height / 2) / (rect.height * nextZoom),
  );
}

function resetMapView() {
  applyMapView(1, .5, .5);
}

function updateMapZoomControls() {
  const atMinimum = mapView.zoom <= mapView.minZoom + .001;
  const atMaximum = mapView.zoom >= mapView.maxZoom - .001;
  $("#mapZoomIn").disabled = atMaximum;
  $("#mapZoomIn").ariaLabel = atMaximum ? "Map is fully zoomed in" : "Zoom in on map";
  $("#mapZoomOut").disabled = atMinimum;
  $("#mapZoomOut").ariaLabel = atMinimum ? "Map is fully zoomed out" : "Zoom out on map";
}

function operatorById(id) {
  return state.catalog.operators.find((operator) => operator.id === id);
}

function operatorIconUrl(operator) {
  if (operator?.iconUrl) return operator.iconUrl;
  const path = String(operator?.svgPath || "").replace(/^assets\//, "");
  return path ? `/game-assets/${path}` : "";
}

function selectedRound() {
  return state.current?.rounds[state.roundIndex];
}

function captureById(id) {
  return state.captures.find((capture) => capture.id === id);
}

function roundComplete(round) {
  const exampleReady = state.current?.kind !== "example" || round.guessPos;
  return Boolean(round.operatorId && round.listenerPos && round.operatorStartPos && round.targetPos && exampleReady && captureById(round.captureId));
}

function visibleSets() {
  return state.sets.filter((item) => (item.kind || "daily") === state.libraryKind);
}

function renderLibraryChrome() {
  const examples = state.libraryKind === "example";
  $("#libraryTitle").textContent = examples ? "Examples" : "Daily sets";
  $("#toggleLibrary").textContent = examples ? "Daily sets" : "Examples";
  $("#toggleLibrary").title = examples ? "Show daily sets" : "Show How to Play examples";
  $("#toggleLibrary").setAttribute("aria-label", $("#toggleLibrary").title);
  $("#newSet").title = examples ? "New example" : "New daily set";
  $("#newSet").setAttribute("aria-label", $("#newSet").title);
  $("#sidebarFooter").hidden = examples;
  $("#emptyCount").textContent = examples ? "01" : "03";
  $("#emptyTitle").textContent = examples ? "Build a How to Play example" : "Build one complete game day";
  $("#emptyCopy").textContent = examples
    ? "Create a one-round example, place its markers, and attach one processed synced capture."
    : "Create a set, place the listener and runner markers, then attach one processed capture to each round.";
  $("#emptyNewSet").textContent = examples ? "Create an example" : "Create a daily set";
}

function renderSetList() {
  renderLibraryChrome();
  const items = visibleSets();
  $("#setList").innerHTML = items.length ? items.map((item) => `
    <div class="set-item ${item.id === state.current?.id ? "active" : ""}">
      <button class="set-item-select" type="button" data-set-id="${escapeHtml(item.id)}">
        <strong>${escapeHtml(item.name)}</strong>
        <small>${escapeHtml(item.mapName || item.mapSlug || "No map")}</small>
        <span class="pill ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      </button>
      <button class="set-item-delete" type="button" data-delete-set-id="${escapeHtml(item.id)}" aria-label="Delete ${escapeHtml(item.name)}" title="Delete set">×</button>
    </div>`).join("") : `<p class="hint">No ${state.libraryKind === "example" ? "examples" : "daily sets"} yet.</p>`;
}

function fillMapSelect() {
  $("#mapSelect").innerHTML = state.catalog.maps.map((map) =>
    `<option value="${map.slug}">${escapeHtml(map.name)}</option>`).join("");
}

function renderAlternateTargetFloorField(round, floors) {
  const select = $("#alternateTargetFloor");
  const targetFloorKey = round?.targetPos?.floorKey;
  const primaryLabel = floorStatusLabel(targetFloorKey, floors);
  select.innerHTML = `<option value="">${targetFloorKey
    ? `Only the marked floor (${escapeHtml(primaryLabel)})`
    : "Place runner end first"}</option>` + floors
    .filter((floor) => floor.key !== targetFloorKey)
    .map((floor) => `<option value="${escapeHtml(floor.key)}">Also ${escapeHtml(floor.label)}</option>`)
    .join("");
  select.disabled = !targetFloorKey || floors.length < 2;
  select.value = round?.alternateTargetFloorKey || "";
}

function renderEditor() {
  const item = state.current;
  $("#emptyState").hidden = Boolean(item);
  $("#editor").hidden = !item;
  renderSetList();
  if (!item) return;

  $("#setName").value = item.name;
  const example = item.kind === "example";
  $(".map-toolbar").classList.toggle("example-tools", example);
  $("#setNameLabel").textContent = example ? "Example name" : "Set name";
  $("#previewDateField").hidden = example;
  $("#previewSet").hidden = example;
  $("#importDailySet").hidden = example;
  $("#approveSet").textContent = example ? "Approve example" : "Approve set";
  $("#mapSelect").value = item.mapSlug;
  $("#roundTabs").innerHTML = item.rounds.map((round, index) => {
    const operator = operatorById(round.operatorId);
    const iconUrl = operatorIconUrl(operator);
    const visual = iconUrl
      ? `<img class="round-tab-icon" src="${escapeHtml(iconUrl)}" alt="">`
      : `<span class="round-tab-icon round-tab-placeholder" aria-hidden="true">0${index + 1}</span>`;
    const meta = roundComplete(round) ? "Complete" : operator?.name || "Unassigned";
    return `
      <button data-round="${index}" aria-label="Round ${index + 1}: ${escapeHtml(meta)}" class="${index === state.roundIndex ? "active" : ""} ${roundComplete(round) ? "complete" : ""}">
        ${visual}
        <span class="round-tab-title">Round ${index + 1}</span>
        <span class="round-tab-meta">${escapeHtml(meta)}</span>
      </button>`;
  }).join("");

  const round = selectedRound();
  $("#roundEyebrow").textContent = `ROUND ${state.roundIndex + 1} OF ${item.rounds.length}`;
  $("#roundHeading").textContent = `Round ${state.roundIndex + 1}`;
  renderOperatorField();
  $("#captureSelect").innerHTML = `<option value="">Attach processed capture</option>` + state.captures.map((capture) =>
    `<option value="${capture.id}">${escapeHtml(capture.id)}</option>`).join("");
  $("#captureSelect").value = round.captureId || "";

  const map = selectedMap();
  const floors = map?.floors || [];
  renderAlternateTargetFloorField(round, floors);
  if (!floors.some((floor) => floor.key === state.floorKey)) {
    state.floorKey = round.listenerPos?.floorKey || floors[0]?.key || "";
  }
  $("#floorPicker").innerHTML = floors.map((floor) => {
    const active = floor.key === state.floorKey;
    return `<button class="floor-tab${active ? " active" : ""}" type="button" data-floor="${escapeHtml(floor.key)}" role="tab" aria-selected="${active}">${escapeHtml(floorControlLabel(floor))}</button>`;
  }).join("");
  const floor = floors.find((candidate) => candidate.key === state.floorKey);
  if (floor && $("#mapImage").src !== new URL(floor.imageUrl, location.href).href) {
    $("#mapImage").src = floor.imageUrl;
  }
  const toolDetails = markerToolDetails(round, floors);
  if (!toolDetails.some(([tool]) => tool === state.tool)) state.tool = "listenerPos";
  $("#markerTools").innerHTML = toolDetails.map(([tool, icon, label, status]) => `
    <button data-tool="${tool}" class="${state.tool === tool ? "active" : ""}" type="button" aria-label="${label}: ${status}">
      <i class="${icon}" aria-hidden="true"></i><span>${label}</span><small>${escapeHtml(status)}</small>
    </button>`).join("");
  renderCapturePreview();
  drawMarkers();
}

function renderOperatorField() {
  const operator = operatorById(selectedRound()?.operatorId);
  const iconUrl = operatorIconUrl(operator);
  const trigger = $("#operatorTrigger");
  trigger.classList.toggle("has-operator", Boolean(operator));
  $("#operatorTriggerVisual").outerHTML = iconUrl
    ? `<img class="operator-trigger-icon" id="operatorTriggerVisual" src="${escapeHtml(iconUrl)}" alt="">`
    : `<span class="operator-trigger-empty" id="operatorTriggerVisual" aria-hidden="true">?</span>`;
  $("#operatorTriggerText").textContent = operator?.name || "Choose an operator";
  $("#operatorTriggerMeta").textContent = operator ? operator.side : "Search all attackers and defenders";
  renderOperatorOptions(state.operatorQuery);
}

function renderOperatorOptions(query = "") {
  const normalized = query.trim().toLocaleLowerCase();
  const operators = [...state.catalog.operators]
    .filter((operator) => !normalized || [operator.name, operator.side, operator.unit]
      .some((value) => String(value || "").toLocaleLowerCase().includes(normalized)))
    .sort((a, b) => a.name.localeCompare(b.name));
  const selectedId = selectedRound()?.operatorId;
  $("#operatorList").innerHTML = operators.length ? operators.map((operator) => `
    <button class="operator-option" type="button" role="option" data-operator-id="${escapeHtml(operator.id)}" aria-selected="${operator.id === selectedId}">
      <img src="${escapeHtml(operatorIconUrl(operator))}" alt="">
      <strong>${escapeHtml(operator.name)}</strong>
      <small>${escapeHtml(operator.side)}</small>
    </button>`).join("") : `<p class="operator-empty">No operators match “${escapeHtml(query)}”.</p>`;
}

function setOperatorPicker(open) {
  $("#operatorPicker").hidden = !open;
  $("#operatorTrigger").setAttribute("aria-expanded", String(open));
  if (open) {
    $("#operatorSearch").focus();
    $("#operatorSearch").select();
  }
}

function renderCapturePreview() {
  const capture = captureById(selectedRound().captureId);
  if (!capture) {
    $("#capturePreview").innerHTML = `<div class="preview-empty">Attach a processed capture</div>`;
    return;
  }
  const captureId = encodeURIComponent(capture.id);
  const captureVersion = encodeURIComponent(capture.contentFingerprint || capture.updatedAt || "");
  const versionQuery = captureVersion ? `?v=${captureVersion}` : "";
  const replaySource = `/media/${captureId}/replay.mp4${versionQuery}`;
  $("#capturePreview").innerHTML = `
    <img src="/media/${captureId}/listener.jpg${versionQuery}" alt="Listener POV still">
    ${audioPlayerMarkup(replaySource)}`;
  setupCaptureAudioPlayer($("#capturePreview .studio-audio-player"));
}

function audioPlayerMarkup(source, { videoBelowControls = false } = {}) {
  return `
    <div class="studio-audio-player${videoBelowControls ? " studio-audio-player--video-below" : ""}">
      <video class="studio-runner-video" preload="metadata" playsinline src="${escapeHtml(source)}" aria-label="Synced runner replay with listener audio"></video>
      <div class="studio-audio-controls">
      <div class="studio-audio-primary-cluster">
        <button class="studio-audio-primary" data-audio-action="play" type="button" aria-label="Play audio">
          <img class="studio-audio-play-icon" src="/game-assets/icons/heroicons/play.svg" alt="">
          <span class="studio-audio-play-label">Play audio</span>
        </button>
        <span class="studio-audio-time studio-audio-elapsed">0:00</span>
      </div>
      <div class="studio-audio-waveform">
        <canvas class="studio-audio-waveform-base" aria-hidden="true"></canvas>
        <span class="studio-audio-progress" aria-hidden="true"><canvas class="studio-audio-waveform-played"></canvas></span>
        <span class="studio-audio-baseline" aria-hidden="true"></span>
        <span class="studio-audio-playhead" aria-hidden="true"></span>
        <input class="studio-audio-seek" type="range" min="0" max="1000" value="0" aria-label="Audio position" aria-valuetext="0:00 of 0:00">
      </div>
      <span class="studio-audio-time studio-audio-total">0:00</span>
      <div class="studio-audio-secondary-controls" aria-label="Additional audio controls">
        <button class="studio-audio-restart" data-audio-action="restart" type="button" aria-label="Restart audio" title="Restart audio">
          <img src="/game-assets/icons/heroicons/arrow-path.svg" alt="">
        </button>
        <div class="volume-popover">
          <span class="studio-audio-iconbtn volume-icon" aria-hidden="true">
            <img src="/game-assets/icons/heroicons/speaker-wave.svg" alt="">
          </span>
          <label class="volume-control" aria-label="Audio volume, up to 500 percent">
            <span class="volume-value">100%</span>
            <input class="volume" type="range" min="0" max="500" step="10" value="100" aria-label="Audio volume, up to 500 percent">
          </label>
        </div>
      </div>
      </div>
    </div>`;
}

function audioTime(seconds) {
  const safe = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

function waveformBins(samples, count) {
  if (!samples.length || count <= 0) return [];
  return Array.from({ length: count }, (_, index) => {
    const position = count === 1 ? 0 : index / (count - 1) * (samples.length - 1);
    const left = Math.floor(position);
    const right = Math.min(samples.length - 1, left + 1);
    const mix = position - left;
    return samples[left] * (1 - mix) + samples[right] * mix;
  });
}

function paintCaptureWaveform(root, samples) {
  const waveform = root.querySelector(".studio-audio-waveform");
  if (!waveform) return;
  const rect = waveform.getBoundingClientRect();
  const width = Math.floor(rect.width);
  const height = Math.floor(rect.height);
  if (!width || !height) return;
  const gap = width < 170 ? 1 : 1.35;
  const barWidth = width < 170 ? 1 : 1.2;
  const bars = waveformBins(samples, Math.max(24, Math.floor((width + gap) / (barWidth + gap))));
  waveform.style.setProperty("--waveform-width", `${width}px`);
  const paint = (canvas, upperColor, lowerColor) => {
    const density = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
    canvas.width = Math.max(1, Math.round(width * density));
    canvas.height = Math.max(1, Math.round(height * density));
    const context = canvas.getContext("2d");
    context.setTransform(density, 0, 0, density, 0, 0);
    context.clearRect(0, 0, width, height);
    const baseline = Math.round(height * .65) + .5;
    const upperSpace = Math.max(4, baseline - 3);
    const lowerSpace = Math.max(3, height - baseline - 3);
    bars.forEach((amplitude, index) => {
      const shaped = Math.pow(amplitude, .68);
      const x = Math.min(width - barWidth, index * (barWidth + gap));
      context.fillStyle = upperColor;
      context.fillRect(x, baseline - Math.max(2, shaped * upperSpace), barWidth, Math.max(2, shaped * upperSpace));
      context.fillStyle = lowerColor;
      context.fillRect(x, baseline + 2, barWidth, Math.max(1.5, shaped * lowerSpace));
    });
  };
  paint(root.querySelector(".studio-audio-waveform-base"), "rgba(244, 246, 248, .62)", "rgba(244, 246, 248, .25)");
  paint(root.querySelector(".studio-audio-waveform-played"), "rgba(228, 86, 96, 1)", "rgba(228, 86, 96, .5)");
}

async function decodeCaptureWaveform(source, root, fallback) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;
  let audioContext;
  try {
    const response = await fetch(source, { cache: "force-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    audioContext = new AudioContextClass();
    const decoded = await audioContext.decodeAudioData(await response.arrayBuffer());
    if (!root.isConnected) return;
    const count = 512;
    const samplesPerBar = Math.max(1, Math.floor(decoded.length / count));
    const amplitudes = Array.from({ length: count }, (_, barIndex) => {
      const start = barIndex * samplesPerBar;
      const end = Math.min(decoded.length, start + samplesPerBar);
      const stride = Math.max(1, Math.floor((end - start) / 1800));
      let sumSquares = 0;
      let sampled = 0;
      for (let sampleIndex = start; sampleIndex < end; sampleIndex += stride) {
        let mixed = 0;
        for (let channel = 0; channel < decoded.numberOfChannels; channel += 1) mixed += decoded.getChannelData(channel)[sampleIndex] || 0;
        mixed /= decoded.numberOfChannels;
        sumSquares += mixed * mixed;
        sampled += 1;
      }
      return sampled ? Math.sqrt(sumSquares / sampled) : 0;
    });
    const loudest = Math.max(...amplitudes);
    if (loudest) paintCaptureWaveform(root, amplitudes.map((amplitude) => Math.pow(amplitude / loudest, .62)));
  } catch (error) {
    if (root.isConnected) {
      console.warn("Capture waveform could not be decoded; using the fallback shape.", error);
      paintCaptureWaveform(root, fallback);
    }
  } finally {
    audioContext?.close().catch(() => {});
  }
}

function setupCaptureAudioPlayer(root) {
  const source = root.querySelector("video").src;
  const audio = root.querySelector("video");
  const seek = root.querySelector(".studio-audio-seek");
  const waveform = root.querySelector(".studio-audio-waveform");
  const playButton = root.querySelector('[data-audio-action="play"]');
  const playIcon = root.querySelector(".studio-audio-play-icon");
  const playLabel = root.querySelector(".studio-audio-play-label");
  const elapsed = root.querySelector(".studio-audio-elapsed");
  const total = root.querySelector(".studio-audio-total");
  const restartButton = root.querySelector('[data-audio-action="restart"]');
  const volumePopover = root.querySelector(".volume-popover");
  const volume = root.querySelector(".volume");
  const volumeValue = root.querySelector(".volume-value");
  const fallback = [.18, .34, .55, .82, .46, .91, .64, .28, .59, .86, .68, .37, .73, .49, .31, .19, .42, .78, .52, .88, .35, .63, .95, .57];
  let seeking = false;
  let waveformFrame = null;
  let volumePercent = 100;
  let playbackAudioContext = null;
  let playbackGain = null;
  const update = (preview = false) => {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    const currentTime = preview && duration ? duration * clamp(Number(seek.value) / 1000) : audio.currentTime;
    if (!seeking && !audio.seeking && !preview) seek.value = duration ? String(Math.round(currentTime / duration * 1000)) : "0";
    const progress = duration ? clamp(currentTime / duration) : 0;
    waveform.style.setProperty("--wave-progress", `${progress * 100}%`);
    elapsed.textContent = audioTime(currentTime);
    total.textContent = audioTime(duration);
    seek.setAttribute("aria-valuetext", `${audioTime(currentTime)} of ${audioTime(duration)}`);
  };
  const syncPlayback = () => {
    const playing = !audio.paused && !audio.ended;
    playIcon.src = playing ? "/game-assets/icons/heroicons/pause.svg" : "/game-assets/icons/heroicons/play.svg";
    playLabel.textContent = playing ? "Stop audio" : "Play audio";
    playButton.ariaLabel = playing ? "Stop audio" : "Play audio";
    cancelAnimationFrame(waveformFrame);
    const tick = () => {
      update();
      if (!audio.paused && !audio.ended) waveformFrame = requestAnimationFrame(tick);
    };
    if (playing) tick();
    else update();
  };
  const seekTo = (value) => {
    const duration = Number(audio.duration);
    if (Number.isFinite(duration) && duration > 0) audio.currentTime = duration * clamp(Number(value) / 1000);
  };
  const togglePlayback = () => {
    if (audio.ended) audio.currentTime = 0;
    if (audio.paused || audio.ended) audio.play().catch((error) => toast(error.message));
    else audio.pause();
  };
  const enableVolumeBoost = () => {
    if (playbackGain) return true;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return false;
    try {
      playbackAudioContext = new AudioContextClass();
      const mediaSource = playbackAudioContext.createMediaElementSource(audio);
      playbackGain = playbackAudioContext.createGain();
      mediaSource.connect(playbackGain).connect(playbackAudioContext.destination);
      audio.volume = 1;
      return true;
    } catch (error) {
      console.warn("Studio volume boost is unavailable", error);
      return false;
    }
  };
  const updateVolume = () => {
    volume.value = String(volumePercent);
    const volumeRange = Number(volume.max) - Number(volume.min);
    const volumeLevel = volumeRange > 0
      ? (volumePercent - Number(volume.min)) / volumeRange * 100
      : 0;
    const trackLength = parseFloat(getComputedStyle(volume).height) || 94;
    const thumbRadius = 6;
    const unfilledLength = thumbRadius +
      (trackLength - thumbRadius * 2) * (1 - volumeLevel / 100);
    volume.style.setProperty("--volume-level", `${volumeLevel}%`);
    volume.style.setProperty("--volume-unfilled", `${unfilledLength}px`);
    volumeValue.textContent = `${volumePercent}%`;
    volume.setAttribute("aria-valuetext", `${volumePercent}%`);
  };
  const setVolume = (value) => {
    let percent = Math.round(clamp(Number(value), 0, 500));
    if (percent > 100 && !enableVolumeBoost()) percent = 100;
    volumePercent = percent;
    const gain = percent / 100;
    if (playbackGain) {
      playbackGain.gain.value = gain;
      playbackAudioContext?.resume?.().catch(() => {});
    } else {
      audio.volume = Math.min(1, gain);
    }
    audio.muted = gain === 0;
    updateVolume();
  };
  const setSeekFromPointer = (event) => {
    const rect = seek.getBoundingClientRect();
    if (!rect.width) return;
    seek.value = String(Math.round(clamp((event.clientX - rect.left) / rect.width) * 1000));
    seekTo(seek.value);
    update(true);
  };
  playButton.onclick = togglePlayback;
  audio.onclick = togglePlayback;
  restartButton.onclick = () => {
    audio.currentTime = 0;
    audio.play().catch((error) => toast(error.message));
  };
  volumePopover.onpointerleave = () => {
    if (document.activeElement === volume) volume.blur();
  };
  volume.oninput = (event) => setVolume(event.target.value);
  seek.onpointerdown = (event) => {
    if (event.button !== 0) return;
    seeking = true;
    seek.setPointerCapture?.(event.pointerId);
    setSeekFromPointer(event);
  };
  seek.onpointermove = (event) => { if (seeking) setSeekFromPointer(event); };
  seek.onpointerup = (event) => {
    if (!seeking) return;
    setSeekFromPointer(event);
    seeking = false;
  };
  seek.onpointercancel = () => { seeking = false; };
  seek.oninput = (event) => {
    seeking = true;
    seekTo(event.target.value);
    update(true);
  };
  seek.onchange = (event) => {
    seekTo(event.target.value);
    seeking = false;
    update();
  };
  audio.onplay = syncPlayback;
  audio.onpause = syncPlayback;
  audio.onended = syncPlayback;
  audio.ontimeupdate = () => update();
  audio.onseeked = () => update();
  audio.onloadedmetadata = () => update();
  audio.onvolumechange = updateVolume;
  paintCaptureWaveform(root, fallback);
  decodeCaptureWaveform(source, root, fallback);
  const waveformObserver = typeof ResizeObserver === "function"
    ? new ResizeObserver(() => paintCaptureWaveform(root, fallback))
    : null;
  waveformObserver?.observe(waveform);
  updateVolume();
}

function resizeCanvas() {
  const canvas = $("#mapCanvas");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  drawMarkers();
}

function drawMarkers() {
  const canvas = $("#mapCanvas");
  const context = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.width / ratio;
  const height = canvas.height / ratio;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const imageX = width / 2 - mapView.centerX * width * mapView.zoom;
  const imageY = height / 2 - mapView.centerY * height * mapView.zoom;
  $("#mapImage").style.transform = `translate(${imageX}px, ${imageY}px) scale(${mapView.zoom})`;
  if (!state.current) {
    $("#mapMarkers").innerHTML = "";
    return;
  }
  const round = selectedRound();
  drawExampleResultLine(context, round);
  const listener = round.listenerPos?.floorKey === state.floorKey ? round.listenerPos : null;
  if (listener) drawFacingCone(context, viewportPoint(toAssetPoint(listener)), width, listener.angle);
  const operator = operatorById(round.operatorId);
  const iconUrl = operatorIconUrl(operator);
  const markerKeys = state.current.kind === "example"
    ? Object.keys(markerInfo)
    : Object.keys(markerInfo).filter((key) => key !== "guessPos");
  $("#mapMarkers").innerHTML = markerKeys.map((key) => {
    const position = key === "targetPos"
      ? targetPositionForFloor(round, round.guessPos?.floorKey)
      : round[key];
    if (!position || position.floorKey !== state.floorKey) return "";
    const point = viewportPoint(toAssetPoint(position));
    const style = `left:${point.x.toFixed(2)}px;top:${point.y.toFixed(2)}px`;
    if (key === "listenerPos") {
      return `<div class="map-marker map-marker-listener" style="${style}"><img src="${RECRUIT_ICON_URL}" alt=""><span class="map-marker-tag">LISTENER</span></div>`;
    }
    if (key === "guessPos") {
      return `<div class="map-marker map-marker-guess" style="${style}"><img src="/game-assets/icons/guess-ping.svg" alt=""><span class="map-marker-tag">GUESS</span></div>`;
    }
    const runnerEnd = key === "targetPos";
    const className = runnerEnd ? "map-marker-end" : "map-marker-start";
    const label = runnerEnd ? "RUNNER END" : "RUNNER START";
    const visual = iconUrl
      ? `<img src="${escapeHtml(iconUrl)}" alt="">`
      : `<span class="map-marker-placeholder" aria-hidden="true">${runnerEnd ? "RE" : "RS"}</span>`;
    return `<div class="map-marker map-marker-runner ${className}" style="${style}">${visual}<span class="map-marker-tag">${label}</span></div>`;
  }).join("");
  renderExampleResult(round);
}

function drawExampleResultLine(context, round) {
  if (state.current?.kind !== "example" || !round?.guessPos || !round?.targetPos) return;
  const target = targetPositionForFloor(round, round.guessPos.floorKey);
  const onResultFloor = [round.guessPos.floorKey, target.floorKey].includes(state.floorKey);
  if (!onResultFloor) return;
  const guess = viewportPoint(toAssetPoint(round.guessPos));
  const targetPoint = viewportPoint(toAssetPoint(target));
  context.save();
  context.beginPath();
  context.setLineDash([8, 7]);
  context.strokeStyle = "#8ed8ff";
  context.lineWidth = 3;
  context.globalAlpha = isTargetFloorCorrect(round, round.guessPos.floorKey) ? .95 : .48;
  context.moveTo(guess.x, guess.y);
  context.lineTo(targetPoint.x, targetPoint.y);
  context.stroke();
  context.restore();
}

function drawFacingCone(context, position, width, angleValue) {
  const x = position.x;
  const y = position.y;
  const angle = ((Number(angleValue) || 0) - 90) * Math.PI / 180;
  const radius = Math.max(24, Math.min(94, width * .12));
  const spread = .46;
  context.save();
  context.globalAlpha = .7;
  context.beginPath();
  context.moveTo(x, y);
  context.arc(x, y, radius, angle - spread, angle + spread);
  context.closePath();
  const glow = context.createRadialGradient(x, y, 6, x, y, radius);
  glow.addColorStop(0, "#46a8ff75");
  glow.addColorStop(.72, "#46a8ff2b");
  glow.addColorStop(1, "#46a8ff08");
  context.fillStyle = glow;
  context.fill();
  context.strokeStyle = "#78c0ffab";
  context.lineWidth = 1.8;
  context.stroke();
  context.beginPath();
  context.setLineDash([5, 4]);
  context.arc(x, y, radius * .68, angle - spread, angle + spread);
  context.globalAlpha = .476;
  context.lineWidth = 1;
  context.stroke();
  context.setLineDash([]);
  context.globalAlpha = .7;
  context.beginPath();
  context.moveTo(x, y);
  context.lineTo(x + Math.cos(angle) * radius, y + Math.sin(angle) * radius);
  context.strokeStyle = "#46a8ffcc";
  context.lineWidth = 2.4;
  context.stroke();
  if (state.tool === "listenerAngle") {
    context.beginPath();
    context.arc(x + Math.cos(angle) * radius, y + Math.sin(angle) * radius, 5, 0, Math.PI * 2);
    context.fillStyle = "#46a8ff";
    context.fill();
    context.strokeStyle = "#07090d";
    context.lineWidth = 2;
    context.stroke();
  }
  context.restore();
}

function pointFromEvent(event) {
  return toMapPoint(assetPointFromClient(event.clientX, event.clientY));
}

function nearestMarker(event) {
  const round = selectedRound();
  const rect = $("#mapCanvas").getBoundingClientRect();
  return Object.keys(markerInfo).find((key) => {
    const marker = round[key];
    if (marker?.floorKey !== state.floorKey) return false;
    const point = viewportPoint(toAssetPoint(marker));
    return Math.hypot(point.x - (event.clientX - rect.left), point.y - (event.clientY - rect.top)) < 32;
  });
}

function updateListenerAngle(event) {
  const listener = selectedRound().listenerPos;
  if (!listener || listener.floorKey !== state.floorKey) return false;
  const rect = $("#mapCanvas").getBoundingClientRect();
  const point = viewportPoint(toAssetPoint(listener));
  const dx = event.clientX - rect.left - point.x;
  const dy = event.clientY - rect.top - point.y;
  if (Math.hypot(dx, dy) < 2) return true;
  listener.angle = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360) % 360;
  updateMarkerToolStatuses();
  drawMarkers();
  return true;
}

async function refreshData() {
  [state.sets, state.captures, state.publishAttempts, state.publisher, state.productionChallenges,
    state.publishQueue] = await Promise.all([
    api("/api/sets"), api("/api/captures"),
    api("/api/publish-attempts"), api("/api/publisher/status"), api("/api/production/challenges"),
    api("/api/publish-queue"),
  ]);
  if (queueIsActive()) startQueuePolling();
  if (state.current) state.current = state.sets.find((item) => item.id === state.current.id) || null;
  renderEditor();
  renderPublishing();
}

async function createSet() {
  if (autoSaveTimer) await persistDraft();
  const map = state.catalog.maps[0];
  const kind = state.libraryKind;
  const roundCount = kind === "example" ? 1 : 3;
  const item = await api("/api/sets", {
    method: "POST",
    body: JSON.stringify({
      kind,
      name: kind === "example" ? "Untitled example" : "Untitled set",
      mapSlug: map?.slug || "",
      mapName: map?.name || "",
      mapAssetVersion: state.catalog.assetVersion,
      rounds: Array.from({ length: roundCount }, (_, index) => ({ position: index + 1 })),
    }),
  });
  await refreshData();
  state.current = state.sets.find((candidate) => candidate.id === item.id);
  state.roundIndex = 0;
  state.floorKey = selectedMap()?.floors[0]?.key || "";
  resetMapView();
  renderEditor();
}

async function switchLibrary() {
  if (autoSaveTimer) await persistDraft();
  await autoSavePromise.catch(() => {});
  state.libraryKind = state.libraryKind === "daily" ? "example" : "daily";
  state.current = null;
  state.roundIndex = 0;
  state.floorKey = "";
  resetMapView();
  renderEditor();
}

function scheduleDraftSave() {
  if (!state.current) return;
  state.current.status = "draft";
  renderSetList();
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => persistDraft(), 450);
}

async function persistDraft() {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = null;
  if (!state.current) return;
  const setId = state.current.id;
  const payload = structuredClone(state.current);
  payload.name = $("#setName").value.trim();
  payload.status = "draft";
  autoSavePromise = autoSavePromise.catch(() => {}).then(() => api(`/api/sets/${setId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  }));
  try {
    const saved = await autoSavePromise;
    const index = state.sets.findIndex((item) => item.id === setId);
    if (index >= 0) state.sets[index] = saved;
    if (state.current?.id === setId) {
      state.current.version = saved.version;
      state.current.status = "draft";
      state.current.updatedAt = saved.updatedAt;
      renderSetList();
    }
  } catch (error) {
    toast(error.message);
  }
}

async function saveSet(status) {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = null;
  await autoSavePromise.catch(() => {});
  state.current.name = $("#setName").value.trim();
  // Capture the visible value at the approval boundary as well as on change,
  // so the persisted request always matches what the editor is displaying.
  selectedRound().alternateTargetFloorKey = $("#alternateTargetFloor").value || null;
  state.current.status = status;
  const saved = await api(`/api/sets/${state.current.id}`, {
    method: "PUT",
    body: JSON.stringify(state.current),
  });
  await refreshData();
  state.current = state.sets.find((item) => item.id === saved.id);
  renderEditor();
  toast(state.current.kind === "example" ? "Example approved" : "Set approved");
}

async function deleteSet(setId) {
  const item = state.sets.find((candidate) => candidate.id === setId);
  if (!item) return;
  const setName = item.name || "Untitled set";
  const warning = item.kind === "example"
    ? "This removes it from the How to Play examples library."
    : "Any immutable production publication history is retained.";
  if (!window.confirm(`Delete “${setName}”? ${warning}`)) return;
  if (state.current?.id === setId) {
    clearTimeout(autoSaveTimer);
    autoSaveTimer = null;
  } else if (autoSaveTimer) {
    await persistDraft();
  }
  await autoSavePromise.catch(() => {});
  await api(`/api/sets/${setId}`, { method: "DELETE" });
  if (state.current?.id === setId) state.current = null;
  await refreshData();
  toast("Set deleted");
}

const importStateCopy = {
  empty: ["Enter a daily-set folder name", "Type number-mapname, such as 1-bank. Studio looks inside the daily sets directory automatically."],
  scanning: ["Scanning three rounds…", "Studio is validating manifests, media hashes, durations, identities, and the current map catalog."],
  valid: ["Three rounds are ready", "Choose a new or compatible existing draft, then import all three captures in one transaction."],
  partially_invalid: ["The daily set is incomplete or invalid", "Correct every listed slot or directory error, then scan the same directory again."],
  legacy_unindexed: ["Legacy daily set needs preparation", "Generate capture-v1 manifests for this set, then scan it again."],
  conflict: ["The import conflicts with Studio data", "Resolve the listed identity or draft conflict. No database rows have been changed."],
  reprocessed: ["Reprocessed capture content found", "Importing will replace changed capture metadata and return affected drafts to review."],
  stale: ["A published draft will become stale", "Confirm the stale transition before import. The affected set must be reviewed, approved, and uploaded again."],
  importing: ["Importing three rounds…", "Studio is revalidating the scan and committing captures plus the draft as one transaction."],
  success: ["Daily set imported", "All three captures are attached and ready to use. Complete the manual fields and review the attached media before approving the set."],
  retry: ["Import stopped safely", "Nothing was partially imported. Review the message, scan again, and retry."],
};

function renderImportReport(report = { state: "empty" }) {
  const view = $("#importReport");
  const stateName = report.state || "retry";
  const [title, defaultMessage] = importStateCopy[stateName] || importStateCopy.retry;
  const source = report.source?.mapSet ? ` <span class="pill">${escapeHtml(report.source.mapSet)} · ${escapeHtml(report.source.mapName)}</span>` : "";
  const slots = (report.slots || []).map((slot) => `
    <article class="import-slot ${["invalid", "legacy"].includes(slot.state) ? slot.state : ""}">
      <strong>Round ${slot.slot} · ${escapeHtml(slot.relation || slot.state)}</strong>
      <small>${escapeHtml(slot.captureId || slot.summary || "No valid capture")}</small>
      ${slot.errors?.length ? `<ul>${slot.errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul>` : ""}
      ${slot.manualFields?.length ? `<ul>${slot.manualFields.map((field) => `<li>${escapeHtml(field)}</li>`).join("")}</ul>` : ""}
    </article>`).join("");
  const conflicts = report.conflicts?.length
    ? `<ul class="import-conflicts">${report.conflicts.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : "";
  const legacyCommand = report.legacyIndexCommand
    ? `<div class="legacy-command"><span>One-set preparation command</span><code>${escapeHtml(report.legacyIndexCommand)}</code></div>` : "";
  view.dataset.state = stateName;
  view.innerHTML = `
    <div class="import-report-heading">
      <span class="import-state-icon" aria-hidden="true">${["valid", "success"].includes(stateName) ? "✓" : ["scanning", "importing"].includes(stateName) ? "…" : ["empty"].includes(stateName) ? "○" : "!"}</span>
      <div><strong>${escapeHtml(title)}</strong>${source}<p>${escapeHtml(report.message || defaultMessage)}</p>${conflicts}${legacyCommand}</div>
    </div>
    ${slots ? `<div class="import-slot-grid">${slots}</div>` : ""}`;

  const controls = $("#importCommitControls");
  const canCommit = Boolean(report.canCommit && !["scanning", "importing", "success", "retry"].includes(stateName));
  controls.hidden = !canCommit;
  if (!canCommit) return;
  const options = report.draftOptions || [];
  $("#importTargetSet").innerHTML = `<option value="">Create a new draft</option>` + options.map((item) =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}${item.exactMatch ? " · current import" : ""}</option>`).join("");
  const preferredTarget = options.some((item) => item.id === state.importTargetSetId)
    ? state.importTargetSetId : report.draftMatch?.id;
  $("#importTargetSet").value = preferredTarget || "";
  $("#staleConfirmation").hidden = !report.requiresStaleConfirmation;
  $("#allowStaleImport").checked = false;
  $("#commitDailySet").textContent = report.isIdempotentRetry ? "Confirm unchanged import" : "Import three rounds";
  updateImportCommitAvailability();
}

function updateImportCommitAvailability() {
  const requiresConfirmation = !$("#staleConfirmation").hidden;
  $("#commitDailySet").disabled = requiresConfirmation && !$("#allowStaleImport").checked;
}

function latestPublishAttempt(setId) {
  const selected = state.sets.find((item) => item.id === setId);
  if (!selected) return null;
  return state.publishAttempts.find((item) =>
    item.set_id === setId && Number(item.set_version) === Number(selected.version)) || null;
}

function publishProgress(attempt) {
  if (!attempt) return { text: "Connecting to production…", detail: "Preparing the publish request." };
  const objects = attempt.objects || [];
  const verified = objects.filter((item) => item.status === "verified").length;
  const transferred = objects.filter((item) => ["uploaded", "verified"].includes(item.status)).length;
  if (verified === 9) {
    return {
      text: "Finalizing challenge…",
      detail: "All 9 media objects are verified. Production is making the challenge live.",
    };
  }
  if (transferred > 0) {
    return {
      text: `Uploading media (${transferred}/9)…`,
      detail: `${transferred} of 9 media objects transferred; verification follows automatically.`,
    };
  }
  return { text: "Preparing media…", detail: "Production is authorizing the 9 media uploads." };
}

function availableSets() {
  const approved = state.sets.filter((item) => (item.kind || "daily") === "daily" && item.status === "approved");
  const completed = new Set(state.publishAttempts
    .filter((item) => item.remote_release_version_id && ["released", "scheduled", "emergency_unavailable"].includes(item.remote_state || item.state))
    .map((item) => `${item.set_id}:${item.set_version}`));
  return approved.filter((item) => !completed.has(`${item.id}:${item.version}`));
}

const QUEUE_STATE_LABEL = {
  queued: "Queued",
  running: "Uploading…",
  succeeded: "Live",
  failed: "Failed",
};

function queueIsActive() {
  return (state.publishQueue.jobs || [])
    .some((job) => job.state === "queued" || job.state === "running");
}

function startQueuePolling() {
  if (publishQueueTimer) return;
  let pollRunning = false;
  publishQueueTimer = window.setInterval(async () => {
    if (pollRunning) return;
    pollRunning = true;
    try {
      const wasActive = queueIsActive();
      [state.publishQueue, state.publishAttempts] = await Promise.all([
        api("/api/publish-queue"), api("/api/publish-attempts"),
      ]);
      if (queueIsActive()) {
        renderPublishing();
      } else {
        stopQueuePolling();
        // The finished sets are now live, so reload the library and the
        // production challenge list before the final render.
        if (wasActive) await refreshData();
        else renderPublishing();
      }
    } catch {
      // Progress polling is best-effort; the queue keeps running on the server.
    } finally {
      pollRunning = false;
    }
  }, 1500);
}

function stopQueuePolling() {
  if (publishQueueTimer) window.clearInterval(publishQueueTimer);
  publishQueueTimer = null;
}

function renderPublishQueue(available) {
  const jobs = state.publishQueue.jobs || [];
  const active = queueIsActive();
  const queueAll = $("#publishQueueAll");
  queueAll.textContent = available.length ? `Upload all (${available.length})` : "Upload all";
  queueAll.disabled = active || Boolean(activePublishSetId) ||
    !state.publisher.configured || !available.length;
  const clear = $("#publishQueueClear");
  clear.hidden = !jobs.length;
  clear.textContent = active ? "Stop after this set" : "Clear queue";
  const list = $("#publishQueue");
  list.hidden = !jobs.length;
  list.innerHTML = jobs.map((job) => `
    <div class="publish-queue-item" data-state="${escapeHtml(job.state)}">
      <div><strong>${escapeHtml(job.name)}</strong>${job.error
        ? `<small>${escapeHtml(job.error)}</small>`
        : job.challengeId ? `<small>${escapeHtml(job.challengeId)}</small>` : ""}</div>
      <span class="publish-queue-state">${escapeHtml(QUEUE_STATE_LABEL[job.state] || job.state)}</span>
    </div>`).join("");
}

function renderPublishing() {
  const previousSelection = activePublishSetId || $("#publishSet").value;
  const available = availableSets();
  $("#publishSet").innerHTML = available.length ? available.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("") : `<option value="">No approved unpublished sets</option>`;
  if (available.some((item) => item.id === previousSelection)) $("#publishSet").value = previousSelection;
  const selectedSetId = activePublishSetId || $("#publishSet").value;
  const attempt = latestPublishAttempt(selectedSetId);
  const progress = publishProgress(attempt);
  const allMediaVerified = (attempt?.objects || []).length === 9 &&
    attempt.objects.every((item) => item.status === "verified");
  renderPublishQueue(available);
  const button = $("#publishSetButton");
  button.disabled = Boolean(activePublishSetId) || !state.publisher.configured ||
    !available.length || queueIsActive();
  button.textContent = activePublishSetId
    ? progress.text
    : (attempt?.state === "upload_failed"
      ? (allMediaVerified ? "Retry finalization" : "Resume upload")
      : "Upload to production");
  const status = $("#publishStatus");
  status.hidden = !activePublishSetId && attempt?.state !== "upload_failed";
  if (!status.hidden) {
    status.dataset.state = attempt?.state === "upload_failed" && !activePublishSetId ? "error" : "working";
    status.innerHTML = activePublishSetId
      ? `<strong>${escapeHtml(progress.text)}</strong><span>${escapeHtml(progress.detail)}</span>`
      : `<strong>Production publish did not finish</strong><span>${escapeHtml(attempt.error_summary || "Production rejected the final publish step.")} ${allMediaVerified ? "The media is already verified, so retrying will resume at finalization." : "Retrying will resume the saved upload."}</span>`;
  }
  const productionAvailable = state.publisher.configured && state.productionChallenges.available;
  $("#publisherUnavailable").hidden = productionAvailable;
  $("#publisherUnavailable").textContent = state.publisher.configured
    ? (state.productionChallenges.error || "Production challenges could not be loaded. Check the publisher connection.")
    : "Production publishing is not configured. Set the production publisher URL and credential, then restart Studio.";
  const challenges = state.productionChallenges.challenges || [];
  $("#publishList").innerHTML = productionAvailable && challenges.length ? challenges.map((challenge) => {
    return `
    <div class="publication-item">
      <div><strong>${escapeHtml(challenge.title || "Production challenge")}</strong><small>${escapeHtml(challenge.mapName || challenge.mapSlug)} · Challenge ${escapeHtml(challenge.number)}</small></div>
      <button type="button" class="danger-subtle" data-remove-challenge-id="${escapeHtml(challenge.id)}">Remove</button>
    </div>`;
  }).join("") : `<p class="hint">${productionAvailable ? "No challenges are currently live in production." : "Production challenges are unavailable."}</p>`;
}

$("#toggleLibrary").addEventListener("click", () => switchLibrary().catch((error) => toast(error.message)));
$("#newSet").addEventListener("click", () => createSet().catch((error) => toast(error.message)));
$("#emptyNewSet").addEventListener("click", () => createSet().catch((error) => toast(error.message)));
$("#setList").addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-set-id]");
  if (deleteButton) {
    deleteSet(deleteButton.dataset.deleteSetId).catch((error) => toast(error.message));
    return;
  }
  const button = event.target.closest("[data-set-id]");
  if (!button) return;
  if (autoSaveTimer) await persistDraft();
  state.current = state.sets.find((item) => item.id === button.dataset.setId);
  state.roundIndex = 0;
  state.floorKey = selectedRound()?.listenerPos?.floorKey || selectedMap()?.floors[0]?.key || "";
  resetMapView();
  renderEditor();
});
$("#setName").addEventListener("input", (event) => {
  state.current.name = event.target.value.trim();
  scheduleDraftSave();
});
$("#roundTabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-round]");
  if (!button) return;
  state.roundIndex = Number(button.dataset.round);
  state.floorKey = selectedRound()?.listenerPos?.floorKey || selectedMap()?.floors[0]?.key || "";
  renderEditor();
});
$("#mapSelect").addEventListener("change", (event) => {
  state.current.mapSlug = event.target.value;
  const map = selectedMap();
  state.current.mapName = map?.name || event.target.value;
  state.current.mapAssetVersion = state.catalog.assetVersion;
  state.current.rounds.forEach((round) => {
    round.listenerPos = round.operatorStartPos = round.targetPos = null;
    round.alternateTargetFloorKey = null;
    if ("guessPos" in round) round.guessPos = null;
  });
  state.floorKey = map?.floors[0]?.key || "";
  resetMapView();
  renderEditor();
  scheduleDraftSave();
});
$("#floorPicker").addEventListener("click", (event) => {
  const button = event.target.closest("[data-floor]");
  if (!button) return;
  state.floorKey = button.dataset.floor;
  renderEditor();
});
$("#operatorTrigger").addEventListener("click", () => setOperatorPicker($("#operatorPicker").hidden));
$("#operatorSearch").addEventListener("input", (event) => {
  state.operatorQuery = event.target.value;
  renderOperatorOptions(state.operatorQuery);
});
$("#operatorList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-operator-id]");
  if (!button) return;
  selectedRound().operatorId = button.dataset.operatorId;
  state.operatorQuery = "";
  $("#operatorSearch").value = "";
  setOperatorPicker(false);
  renderEditor();
  scheduleDraftSave();
});
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".operator-field")) setOperatorPicker(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#operatorPicker").hidden) {
    setOperatorPicker(false);
    $("#operatorTrigger").focus();
  }
});
$("#captureSelect").addEventListener("change", (event) => {
  selectedRound().captureId = event.target.value || null;
  renderEditor();
  scheduleDraftSave();
});
$("#alternateTargetFloor").addEventListener("change", (event) => {
  selectedRound().alternateTargetFloorKey = event.target.value || null;
  renderEditor();
  scheduleDraftSave();
});
$("#markerTools").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tool]");
  if (!button) return;
  state.tool = button.dataset.tool;
  document.querySelectorAll("#markerTools button").forEach((item) => item.classList.toggle("active", item === button));
  drawMarkers();
});
function canvasPoint(event) {
  const rect = $("#mapCanvas").getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function setMarkerPosition(key, event) {
  const previous = selectedRound()[key];
  selectedRound()[key] = {
    ...previous,
    ...pointFromEvent(event),
    floorKey: state.floorKey,
    ...(key === "listenerPos" ? { angle: Number(previous?.angle) || 0 } : {}),
  };
  if (
    key === "targetPos" &&
    selectedRound().alternateTargetFloorKey === state.floorKey
  ) {
    selectedRound().alternateTargetFloorKey = null;
  }
  if (key === "targetPos") {
    renderAlternateTargetFloorField(selectedRound(), selectedMap()?.floors || []);
  }
  updateMarkerToolStatuses();
  drawMarkers();
}

function startMapPinch() {
  const [first, second] = [...mapPointers.values()].slice(0, 2);
  if (!first || !second) return;
  const midpoint = { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
  const rect = $("#mapCanvas").getBoundingClientRect();
  mapGesture = {
    type: "pinch",
    startZoom: mapView.zoom,
    startDistance: Math.max(1, Math.hypot(first.x - second.x, first.y - second.y)),
    anchor: {
      x: mapView.centerX + (midpoint.x - rect.width / 2) / (rect.width * mapView.zoom),
      y: mapView.centerY + (midpoint.y - rect.height / 2) / (rect.height * mapView.zoom),
    },
  };
  $("#mapCanvas").classList.add("dragging");
}

$("#mapCanvas").addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  event.preventDefault();
  $("#mapCanvas").focus({ preventScroll: true });
  const point = canvasPoint(event);
  mapPointers.set(event.pointerId, point);
  event.target.setPointerCapture(event.pointerId);
  if (mapPointers.size >= 2) {
    startMapPinch();
    return;
  }
  if (state.tool === "listenerAngle") {
    if (!updateListenerAngle(event)) {
      toast("Place the Listener on this floor before setting its direction");
      mapGesture = null;
      return;
    }
    mapGesture = { type: "direction", pointerId: event.pointerId };
    return;
  }
  const marker = nearestMarker(event);
  mapGesture = marker
    ? { type: "marker", pointerId: event.pointerId, marker }
    : {
      type: "pending",
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      startCenterX: mapView.centerX,
      startCenterY: mapView.centerY,
    };
});

$("#mapCanvas").addEventListener("pointermove", (event) => {
  const pointer = mapPointers.get(event.pointerId);
  if (!pointer) return;
  const point = canvasPoint(event);
  Object.assign(pointer, point);
  if (mapPointers.size >= 2) {
    if (mapGesture?.type !== "pinch") startMapPinch();
    const [first, second] = [...mapPointers.values()].slice(0, 2);
    const midpoint = { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
    const distance = Math.max(1, Math.hypot(first.x - second.x, first.y - second.y));
    const nextZoom = clamp(mapGesture.startZoom * distance / mapGesture.startDistance, mapView.minZoom, mapView.maxZoom);
    const rect = $("#mapCanvas").getBoundingClientRect();
    applyMapView(
      nextZoom,
      mapGesture.anchor.x - (midpoint.x - rect.width / 2) / (rect.width * nextZoom),
      mapGesture.anchor.y - (midpoint.y - rect.height / 2) / (rect.height * nextZoom),
    );
    return;
  }
  if (!mapGesture || mapGesture.pointerId !== event.pointerId) return;
  if (mapGesture.type === "pending" && Math.hypot(point.x - mapGesture.startX, point.y - mapGesture.startY) > 6) {
    mapGesture.type = "pan";
    $("#mapCanvas").classList.add("dragging");
  }
  if (mapGesture.type === "pan") {
    const rect = $("#mapCanvas").getBoundingClientRect();
    applyMapView(
      mapView.zoom,
      mapGesture.startCenterX - (point.x - mapGesture.startX) / (rect.width * mapView.zoom),
      mapGesture.startCenterY - (point.y - mapGesture.startY) / (rect.height * mapView.zoom),
    );
  } else if (mapGesture.type === "marker") {
    setMarkerPosition(mapGesture.marker, event);
  } else if (mapGesture.type === "direction") {
    updateListenerAngle(event);
  }
});

function finishMapPointer(event, cancelled = false) {
  const pointer = mapPointers.get(event.pointerId);
  if (!pointer) return;
  const point = canvasPoint(event);
  const gesture = mapGesture;
  const tapped = !cancelled && gesture?.type === "pending" && gesture.pointerId === event.pointerId &&
    Math.hypot(point.x - gesture.startX, point.y - gesture.startY) <= 6;
  mapPointers.delete(event.pointerId);
  if (event.target.hasPointerCapture?.(event.pointerId)) event.target.releasePointerCapture(event.pointerId);
  if (tapped) setMarkerPosition(state.tool, event);
  if (!cancelled && ["marker", "direction"].includes(gesture?.type)) {
    scheduleDraftSave();
  } else if (tapped) {
    scheduleDraftSave();
  }
  if (mapPointers.size >= 2) {
    startMapPinch();
  } else if (mapPointers.size === 1) {
    const [pointerId, remaining] = mapPointers.entries().next().value;
    mapGesture = {
      type: "pan",
      pointerId,
      startX: remaining.x,
      startY: remaining.y,
      startCenterX: mapView.centerX,
      startCenterY: mapView.centerY,
    };
  } else {
    mapGesture = null;
    $("#mapCanvas").classList.remove("dragging");
  }
}

$("#mapCanvas").addEventListener("pointerup", (event) => finishMapPointer(event));
$("#mapCanvas").addEventListener("pointercancel", (event) => finishMapPointer(event, true));
$("#mapCanvas").addEventListener("wheel", (event) => {
  event.preventDefault();
  const rect = $("#mapCanvas").getBoundingClientRect();
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1;
  zoomMapAt(mapView.zoom * Math.exp(-event.deltaY * unit * .0015), canvasPoint(event));
}, { passive: false });
$("#mapCanvas").addEventListener("keydown", (event) => {
  if (["+", "="].includes(event.key)) {
    event.preventDefault();
    zoomMapAt(mapView.zoom + .5);
  } else if (["-", "_"].includes(event.key)) {
    event.preventDefault();
    zoomMapAt(mapView.zoom - .5);
  } else if (event.key === "0") {
    event.preventDefault();
    resetMapView();
  }
});
$("#mapZoomIn").addEventListener("click", () => zoomMapAt(mapView.zoom + .5));
$("#mapZoomOut").addEventListener("click", () => zoomMapAt(mapView.zoom - .5));
$("#clearRound").addEventListener("click", () => {
  const round = {
    position: state.roundIndex + 1,
    operatorId: null,
    listenerPos: null,
    operatorStartPos: null,
    targetPos: null,
    alternateTargetFloorKey: null,
    captureId: null,
  };
  if (state.current.kind === "example") round.guessPos = null;
  state.current.rounds[state.roundIndex] = round;
  renderEditor();
  scheduleDraftSave();
});
$("#approveSet").addEventListener("click", () => saveSet("approved").catch((error) => toast(error.message)));
$("#previewSet").addEventListener("click", async () => {
  if (!state.current) return;
  const previewWindow = window.open("about:blank", "_blank");
  try {
    if (autoSaveTimer) await persistDraft();
    await autoSavePromise.catch(() => {});
    const result = await api("/api/previews", {
      method: "POST",
      body: JSON.stringify({
        schemaVersion: 1,
        setId: state.current.id,
        setVersion: state.current.version,
        displayDate: $("#previewDate").value,
      }),
    });
    if (!previewWindow) {
      toast("Allow pop-ups for Studio, then select Preview again");
      return;
    }
    previewWindow.location.replace(new URL(result.url, location.href).href);
    const errors = result.issues.filter((issue) => issue.severity === "error").length;
    toast(errors ? `Preview opened with ${errors} issue${errors === 1 ? "" : "s"}` : "Preview opened in an isolated session");
  } catch (error) {
    previewWindow?.close();
    toast(error.message);
  }
});
function openImportDialog(targetSetId = null) {
  state.importTargetSetId = targetSetId;
  $("#captureDialog").showModal();
  requestAnimationFrame(() => {
    const target = state.sets.find((item) => item.id === targetSetId);
    renderImportReport(state.importScan || {
      state: "empty",
      message: target
        ? `Scan a processed directory to attach all three rounds to ${target.name}.`
        : undefined,
    });
  });
}
$("#showImport").addEventListener("click", () => openImportDialog());
$("#importDailySet").addEventListener("click", async () => {
  try {
    if (autoSaveTimer) await persistDraft();
    openImportDialog(state.current?.id || null);
  } catch (error) { toast(error.message); }
});
$("#showPublish").addEventListener("click", () => $("#publishDialog").showModal());
$("#scanDailySet").addEventListener("click", async () => {
  const directoryPath = $("#dailySetPath").value.trim();
  if (!directoryPath) {
    state.importScan = { state: "empty", message: "Enter a folder name such as 1-bank before scanning." };
    renderImportReport(state.importScan);
    return;
  }
  renderImportReport({ state: "scanning" });
  try {
    state.importScan = await api("/api/imports/scan", {
      method: "POST",
      body: JSON.stringify({ directoryPath }),
    });
    renderImportReport(state.importScan);
  } catch (error) {
    state.importScan = { state: "retry", message: error.message };
    renderImportReport(state.importScan);
  }
});
$("#allowStaleImport").addEventListener("change", updateImportCommitAvailability);
$("#commitDailySet").addEventListener("click", async () => {
  if (!state.importScan?.scanId) return;
  const prior = state.importScan;
  renderImportReport({ ...prior, state: "importing", canCommit: false });
  try {
    const result = await api("/api/imports/commit", {
      method: "POST",
      body: JSON.stringify({
        scanId: prior.scanId,
        targetSetId: $("#importTargetSet").value || null,
        allowStale: $("#allowStaleImport").checked,
      }),
    });
    state.importScan = {
      state: "success",
      source: prior.source,
      slots: prior.slots,
      message: result.idempotent
        ? "This directory was already attached; no capture or draft rows changed."
        : `Imported ${result.insertedCaptures} new and ${result.updatedCaptures} reprocessed captures into ${result.set.name}.`,
    };
    await refreshData();
    state.importTargetSetId = result.set.id;
    state.current = state.sets.find((item) => item.id === result.set.id) || state.current;
    renderEditor();
    renderImportReport(state.importScan);
    toast(result.idempotent ? "Daily set already up to date" : "Three rounds imported; review media and manual fields");
  } catch (error) {
    state.importScan = { ...prior, state: "retry", canCommit: false, message: error.message };
    renderImportReport(state.importScan);
  }
});
$("#importCapture").addEventListener("click", async () => {
  try {
    await api("/api/captures/import", { method: "POST", body: JSON.stringify({ manifestPath: $("#manifestPath").value }) });
    $("#manifestPath").value = "";
    await refreshData();
    toast("Capture imported and ready to use");
  } catch (error) { toast(error.message); }
});
$("#publishSetButton").addEventListener("click", async () => {
  const button = $("#publishSetButton");
  if (button.disabled) return;
  try {
    const setId = $("#publishSet").value;
    const selected = state.sets.find((item) => item.id === setId);
    if (!selected) throw new Error("Choose an approved set to publish");
    if (!window.confirm(`Upload “${selected.name}” to production and make it playable immediately?`)) return;
    activePublishSetId = setId;
    renderPublishing();
    let pollRunning = false;
    publishProgressTimer = window.setInterval(async () => {
      if (pollRunning) return;
      pollRunning = true;
      try {
        state.publishAttempts = await api("/api/publish-attempts");
        renderPublishing();
      } catch {
        // The primary publish request reports connection failures; progress polling is best-effort.
      } finally {
        pollRunning = false;
      }
    }, 750);
    await api("/api/publish", { method: "POST", body: JSON.stringify({ setId }) });
    await refreshData();
    toast("Nine media objects verified; challenge is live in production");
  } catch (error) {
    try {
      state.publishAttempts = await api("/api/publish-attempts");
    } catch {
      // Keep the original publish error when Studio itself cannot refresh progress.
    }
    toast(error.message);
  } finally {
    if (publishProgressTimer) window.clearInterval(publishProgressTimer);
    publishProgressTimer = null;
    activePublishSetId = null;
    renderPublishing();
  }
});
$("#publishSet").addEventListener("change", renderPublishing);
$("#publishQueueAll").addEventListener("click", async () => {
  const button = $("#publishQueueAll");
  if (button.disabled) return;
  const count = availableSets().length;
  if (!window.confirm(
    `Upload ${count} ${count === 1 ? "set" : "sets"} to production? They publish one after another and each becomes playable as it finishes.`,
  )) return;
  button.disabled = true;
  try {
    state.publishQueue = await api("/api/publish-queue", { method: "POST", body: JSON.stringify({}) });
    renderPublishing();
    startQueuePolling();
    toast(`Queued ${state.publishQueue.added} ${state.publishQueue.added === 1 ? "set" : "sets"} for production`);
  } catch (error) {
    toast(error.message);
    renderPublishing();
  }
});
$("#publishQueueClear").addEventListener("click", async () => {
  try {
    state.publishQueue = await api("/api/publish-queue/clear", { method: "POST", body: "{}" });
    renderPublishing();
    if (!queueIsActive()) stopQueuePolling();
  } catch (error) { toast(error.message); }
});
$("#publishList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-challenge-id]");
  if (!button) return;
  const challenge = (state.productionChallenges.challenges || [])
    .find((item) => item.id === button.dataset.removeChallengeId);
  if (!challenge) return;
  const reason = window.prompt(
    `Why are you removing “${challenge.title}” from production?`,
    "Removed from production in Studio",
  );
  if (reason === null) return;
  if (!reason.trim()) {
    toast("Enter a reason for the production audit log");
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/production/challenges/${encodeURIComponent(challenge.id)}/remove`, {
      method: "POST",
      body: JSON.stringify({ reason: reason.trim() }),
    });
    await refreshData();
    toast("Challenge removed from production");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
});
$("#mapImage").addEventListener("load", resizeCanvas);
new ResizeObserver(resizeCanvas).observe($("#mapStage"));

async function start() {
  try {
    await assertServerCompatibility();
    [state.catalog, state.scoring] = await Promise.all([
      loadWideCatalog(),
      loadScoringConfig("/game-assets/data/scoring.json"),
    ]);
    fillMapSelect();
    const easternDate = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    $("#previewDate").value = easternDate;
    await refreshData();
    $("#status").textContent = `${state.catalog.maps.length} maps · ${state.catalog.operators.length} operators`;
  } catch (error) {
    const message = String(error?.message || error);
    $("#status").textContent = message.includes("Restart Studio")
      ? "Restart Studio, then refresh"
      : "Studio unavailable";
    toast(message);
  }
}

start();
