const $ = (selector) => document.querySelector(selector);

const state = {
  catalog: { maps: [], operators: [], assetVersion: "" },
  sets: [],
  captures: [],
  schedule: [],
  current: null,
  roundIndex: 0,
  floorKey: "",
  tool: "listenerPos",
  dragging: null,
};

const markerInfo = {
  listenerPos: { label: "Listener", color: "#5ab3ff" },
  operatorStartPos: { label: "Runner start", color: "#ffb84d" },
  targetPos: { label: "Target", color: "#45df81" },
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
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

function selectedRound() {
  return state.current?.rounds[state.roundIndex];
}

function captureById(id) {
  return state.captures.find((capture) => capture.id === id);
}

function roundComplete(round) {
  return Boolean(round.operatorId && round.listenerPos && round.operatorStartPos && round.targetPos && captureById(round.captureId)?.status === "approved");
}

function renderSetList() {
  $("#setList").innerHTML = state.sets.length ? state.sets.map((item) => `
    <button class="set-item ${item.id === state.current?.id ? "active" : ""}" data-set-id="${item.id}">
      <strong>${escapeHtml(item.name)}</strong>
      <small>${escapeHtml(item.mapName || item.mapSlug || "No map")}</small>
      <span class="pill">${escapeHtml(item.status)} · v${item.version}</span>
    </button>`).join("") : `<p class="hint">No sets yet.</p>`;
}

function fillMapSelect() {
  $("#mapSelect").innerHTML = state.catalog.maps.map((map) =>
    `<option value="${map.slug}">${escapeHtml(map.name)}</option>`).join("");
}

function fillOperatorSelect() {
  const operators = [...state.catalog.operators].sort((a, b) => a.name.localeCompare(b.name));
  $("#operatorSelect").innerHTML = `<option value="">Choose an operator</option>` + operators.map((operator) =>
    `<option value="${operator.id}">${escapeHtml(operator.name)} · ${escapeHtml(operator.side)}</option>`).join("");
}

function renderEditor() {
  const item = state.current;
  $("#emptyState").hidden = Boolean(item);
  $("#editor").hidden = !item;
  renderSetList();
  if (!item) return;

  $("#setName").value = item.name;
  $("#mapSelect").value = item.mapSlug;
  $("#roundTabs").innerHTML = item.rounds.map((round, index) => `
    <button data-round="${index}" class="${index === state.roundIndex ? "active" : ""} ${roundComplete(round) ? "complete" : ""}">
      Round ${index + 1}
    </button>`).join("");

  const round = selectedRound();
  $("#roundEyebrow").textContent = `ROUND ${state.roundIndex + 1} OF 3`;
  $("#roundHeading").textContent = `Round ${state.roundIndex + 1}`;
  $("#operatorSelect").value = round.operatorId || "";
  $("#captureSelect").innerHTML = `<option value="">Attach processed capture</option>` + state.captures.map((capture) =>
    `<option value="${capture.id}">${escapeHtml(capture.id)} · ${escapeHtml(capture.status)}</option>`).join("");
  $("#captureSelect").value = round.captureId || "";

  const map = selectedMap();
  const floors = map?.floors || [];
  if (!floors.some((floor) => floor.key === state.floorKey)) {
    state.floorKey = round.listenerPos?.floorKey || floors[0]?.key || "";
  }
  $("#floorSelect").innerHTML = floors.map((floor) =>
    `<option value="${floor.key}">${escapeHtml(floor.label)}</option>`).join("");
  $("#floorSelect").value = state.floorKey;
  const floor = floors.find((candidate) => candidate.key === state.floorKey);
  if (floor && $("#mapImage").src !== new URL(floor.imageUrl, location.href).href) {
    $("#mapImage").src = floor.imageUrl;
  }
  renderCapturePreview();
  renderCoordinates();
  renderValidation();
  drawMarkers();
}

function renderCapturePreview() {
  const capture = captureById(selectedRound().captureId);
  $("#capturePreview").innerHTML = capture ? `
    <img src="/media/${encodeURIComponent(capture.id)}/listener.jpg" alt="Listener POV still">
    <audio controls preload="metadata" src="/media/${encodeURIComponent(capture.id)}/listener.m4a"></audio>
  ` : `<div class="preview-empty">Attach a processed capture</div>`;
}

function renderCoordinates() {
  const round = selectedRound();
  $("#coordinates").innerHTML = Object.entries(markerInfo).map(([key, info]) => {
    const position = round[key];
    const value = position
      ? `${position.floorKey} · ${(position.x * 100).toFixed(1)}%, ${(position.y * 100).toFixed(1)}%`
      : "Not placed";
    return `<div class="coordinate"><span>${info.label}</span><strong>${value}</strong></div>`;
  }).join("");
}

function validationErrors() {
  const item = state.current;
  const errors = [];
  if (!item.name.trim()) errors.push("Set name is required");
  if (!item.mapSlug) errors.push("Map is required");
  item.rounds.forEach((round, index) => {
    if (!round.operatorId) errors.push(`Round ${index + 1}: choose an operator`);
    if (!round.listenerPos) errors.push(`Round ${index + 1}: place the listener`);
    if (!round.operatorStartPos) errors.push(`Round ${index + 1}: place the runner start`);
    if (!round.targetPos) errors.push(`Round ${index + 1}: place the target`);
    if (!round.captureId) errors.push(`Round ${index + 1}: attach a capture`);
    else if (captureById(round.captureId)?.status !== "approved") errors.push(`Round ${index + 1}: approve its capture`);
  });
  return errors;
}

function renderValidation() {
  const errors = validationErrors();
  const element = $("#validation");
  element.classList.toggle("invalid", errors.length > 0);
  element.textContent = errors.length ? `${errors.length} item${errors.length === 1 ? "" : "s"} before approval: ${errors.join(" · ")}` : "All three rounds are complete and ready for approval.";
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
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.current) return;
  for (const [key, info] of Object.entries(markerInfo)) {
    const position = selectedRound()[key];
    if (!position || position.floorKey !== state.floorKey) continue;
    const x = position.x * canvas.width;
    const y = position.y * canvas.height;
    context.beginPath();
    context.arc(x, y, 9 * ratio, 0, Math.PI * 2);
    context.fillStyle = info.color;
    context.fill();
    context.lineWidth = 3 * ratio;
    context.strokeStyle = "#07100a";
    context.stroke();
    context.fillStyle = "#07100a";
    context.font = `900 ${9 * ratio}px sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(key === "listenerPos" ? "L" : key === "operatorStartPos" ? "S" : "T", x, y);
  }
}

function pointFromEvent(event) {
  const rect = $("#mapCanvas").getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function nearestMarker(point) {
  const round = selectedRound();
  return Object.keys(markerInfo).find((key) => {
    const marker = round[key];
    return marker?.floorKey === state.floorKey && Math.hypot(marker.x - point.x, marker.y - point.y) < .035;
  });
}

async function refreshData() {
  [state.sets, state.captures, state.schedule] = await Promise.all([
    api("/api/sets"), api("/api/captures"), api("/api/schedule"),
  ]);
  if (state.current) state.current = state.sets.find((item) => item.id === state.current.id) || null;
  renderEditor();
  renderCaptures();
  renderSchedule();
}

async function createSet() {
  const map = state.catalog.maps[0];
  const item = await api("/api/sets", {
    method: "POST",
    body: JSON.stringify({
      name: "Untitled set",
      mapSlug: map?.slug || "",
      mapName: map?.name || "",
      mapAssetVersion: state.catalog.assetVersion,
      rounds: [1, 2, 3].map((position) => ({ position })),
    }),
  });
  await refreshData();
  state.current = state.sets.find((candidate) => candidate.id === item.id);
  state.roundIndex = 0;
  state.floorKey = selectedMap()?.floors[0]?.key || "";
  renderEditor();
}

async function saveSet(status = state.current.status === "approved" ? "draft" : state.current.status) {
  state.current.name = $("#setName").value.trim();
  state.current.status = status;
  const saved = await api(`/api/sets/${state.current.id}`, {
    method: "PUT", body: JSON.stringify(state.current),
  });
  await refreshData();
  state.current = state.sets.find((item) => item.id === saved.id);
  renderEditor();
  toast(status === "approved" ? "Set approved" : "Draft saved");
}

function renderCaptures() {
  $("#captureList").innerHTML = state.captures.length ? state.captures.map((capture) => `
    <div class="capture-item">
      <img src="/media/${encodeURIComponent(capture.id)}/listener.jpg" alt="Listener still">
      <div>
        <strong>${escapeHtml(capture.id)}</strong>
        <small>${escapeHtml(capture.status)} · offset ${Number(capture.alignment.runnerOffsetMs || 0).toFixed(1)} ms · ${Number(capture.durationSeconds || 0).toFixed(2)} s</small>
        <audio controls preload="none" src="/media/${encodeURIComponent(capture.id)}/listener.m4a"></audio>
      </div>
      ${capture.status === "approved" ? `<span class="pill">approved</span>` : `<button type="button" data-approve-capture="${capture.id}">Approve</button>`}
    </div>`).join("") : `<p class="hint">No captures imported yet.</p>`;
}

function renderSchedule() {
  const approved = state.sets.filter((item) => item.status === "approved");
  $("#scheduleSet").innerHTML = approved.length ? approved.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.name)} · v${item.version}</option>`).join("") : `<option value="">No approved sets</option>`;
  $("#scheduleList").innerHTML = state.schedule.length ? state.schedule.map((item) => `
    <div class="schedule-item">
      <strong>${escapeHtml(item.release_date)}</strong>
      <div>${escapeHtml(item.set_name)} <small>version ${item.set_version}</small></div>
      <span class="pill">midnight ET</span>
    </div>`).join("") : `<p class="hint">Nothing scheduled.</p>`;
}

$("#newSet").addEventListener("click", () => createSet().catch((error) => toast(error.message)));
$("#emptyNewSet").addEventListener("click", () => createSet().catch((error) => toast(error.message)));
$("#setList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-set-id]");
  if (!button) return;
  state.current = state.sets.find((item) => item.id === button.dataset.setId);
  state.roundIndex = 0;
  state.floorKey = selectedRound()?.listenerPos?.floorKey || selectedMap()?.floors[0]?.key || "";
  renderEditor();
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
  });
  state.floorKey = map?.floors[0]?.key || "";
  renderEditor();
});
$("#floorSelect").addEventListener("change", (event) => { state.floorKey = event.target.value; renderEditor(); });
$("#operatorSelect").addEventListener("change", (event) => { selectedRound().operatorId = event.target.value || null; renderEditor(); });
$("#captureSelect").addEventListener("change", (event) => { selectedRound().captureId = event.target.value || null; renderEditor(); });
$("#markerTools").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tool]");
  if (!button) return;
  state.tool = button.dataset.tool;
  document.querySelectorAll("#markerTools button").forEach((item) => item.classList.toggle("active", item === button));
});
$("#mapCanvas").addEventListener("pointerdown", (event) => {
  const point = pointFromEvent(event);
  state.dragging = nearestMarker(point) || state.tool;
  selectedRound()[state.dragging] = { ...point, floorKey: state.floorKey, ...(state.dragging === "listenerPos" ? { angle: 0 } : {}) };
  event.target.setPointerCapture(event.pointerId);
  renderCoordinates(); drawMarkers(); renderValidation();
});
$("#mapCanvas").addEventListener("pointermove", (event) => {
  if (!state.dragging) return;
  const old = selectedRound()[state.dragging] || {};
  selectedRound()[state.dragging] = { ...old, ...pointFromEvent(event), floorKey: state.floorKey };
  renderCoordinates(); drawMarkers();
});
$("#mapCanvas").addEventListener("pointerup", () => { state.dragging = null; renderValidation(); });
$("#clearRound").addEventListener("click", () => {
  state.current.rounds[state.roundIndex] = { position: state.roundIndex + 1, operatorId: null, listenerPos: null, operatorStartPos: null, targetPos: null, captureId: null };
  renderEditor();
});
$("#saveSet").addEventListener("click", () => saveSet("draft").catch((error) => toast(error.message)));
$("#approveSet").addEventListener("click", () => saveSet("approved").catch((error) => toast(error.message)));
$("#showCaptures").addEventListener("click", () => $("#captureDialog").showModal());
$("#showSchedule").addEventListener("click", () => $("#scheduleDialog").showModal());
$("#importCapture").addEventListener("click", async () => {
  try {
    await api("/api/captures/import", { method: "POST", body: JSON.stringify({ manifestPath: $("#manifestPath").value }) });
    $("#manifestPath").value = "";
    await refreshData();
    toast("Capture imported; review and approve it");
  } catch (error) { toast(error.message); }
});
$("#captureList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-approve-capture]");
  if (!button) return;
  try {
    await api(`/api/captures/${button.dataset.approveCapture}/approve`, { method: "POST", body: "{}" });
    await refreshData();
    toast("Capture approved");
  } catch (error) { toast(error.message); }
});
$("#scheduleSetButton").addEventListener("click", async () => {
  try {
    await api("/api/schedule", { method: "POST", body: JSON.stringify({ releaseDate: $("#releaseDate").value, setId: $("#scheduleSet").value }) });
    await refreshData();
    toast("Set scheduled for midnight ET");
  } catch (error) { toast(error.message); }
});
$("#mapImage").addEventListener("load", resizeCanvas);
new ResizeObserver(resizeCanvas).observe($("#mapStage"));

async function start() {
  try {
    state.catalog = await api("/api/catalog");
    fillMapSelect();
    fillOperatorSelect();
    $("#releaseDate").value = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    await refreshData();
    $("#status").textContent = `${state.catalog.maps.length} maps · ${state.catalog.operators.length} operators`;
  } catch (error) {
    $("#status").textContent = "Studio unavailable";
    toast(error.message);
  }
}

start();
