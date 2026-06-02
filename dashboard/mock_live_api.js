const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.PORT || 8000);
const SAMPLE_PATH = path.join(__dirname, "..", "data", "sample_events.jsonl");

const stores = new Map();

function loadTemplates() {
  const raw = fs.readFileSync(SAMPLE_PATH, "utf8");
  const baseEvents = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));

  const byStore = new Map();
  for (const evt of baseEvents) {
    if (!byStore.has(evt.store_id)) byStore.set(evt.store_id, []);
    byStore.get(evt.store_id).push(evt);
  }

  const sample002 = byStore.get("STORE_BLR_002") || baseEvents;
  const sample003 = sample002.map((evt, index) => ({
    ...evt,
    store_id: "STORE_BLR_003",
    visitor_id: `${evt.visitor_id}_3`,
    event_id: crypto.randomUUID(),
    camera_id: evt.camera_id.replace("002", "003"),
    metadata: {
      ...(evt.metadata || {}),
      session_seq: typeof evt.metadata?.session_seq === "number" ? evt.metadata.session_seq : index + 1,
      queue_depth: typeof evt.metadata?.queue_depth === "number" ? Math.max(0, evt.metadata.queue_depth - 1) : null,
    },
  }));

  return new Map([
    ["STORE_BLR_002", sample002],
    ["STORE_BLR_003", sample003.length ? sample003 : sample002],
  ]);
}

const templates = loadTemplates();

function getStoreState(storeId) {
  if (!stores.has(storeId)) {
    stores.set(storeId, {
      events: [],
      cursor: 0,
      lastEventAt: null,
      clients: new Set(),
    });
  }
  return stores.get(storeId);
}

function cloneEvent(evt, timestamp = new Date().toISOString()) {
  return {
    ...evt,
    event_id: crypto.randomUUID(),
    timestamp,
    metadata: { ...(evt.metadata || {}) },
  };
}

function appendEvent(storeId, evt) {
  const state = getStoreState(storeId);
  state.events.push(evt);
  state.lastEventAt = new Date(evt.timestamp);
  if (state.events.length > 400) {
    state.events.splice(0, state.events.length - 400);
  }
}

function distinctCount(events, predicate) {
  const ids = new Set();
  for (const evt of events) {
    if (predicate(evt)) ids.add(evt.visitor_id);
  }
  return ids.size;
}

function currentQueueDepth(events) {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const evt = events[i];
    if (evt.event_type === "BILLING_QUEUE_JOIN" && typeof evt.metadata?.queue_depth === "number") {
      return evt.metadata.queue_depth;
    }
  }
  return 0;
}

function computeMetrics(storeId) {
  const state = getStoreState(storeId);
  const events = state.events;
  const customers = events.filter((evt) => !evt.is_staff);
  const entries = distinctCount(customers, (evt) => evt.event_type === "ENTRY" || evt.event_type === "REENTRY");
  const billing = distinctCount(customers, (evt) => evt.event_type === "BILLING_QUEUE_JOIN");
  const abandons = distinctCount(customers, (evt) => evt.event_type === "BILLING_QUEUE_ABANDON");
  const converted = Math.max(0, billing - abandons);
  const dwellEvents = customers.filter((evt) => evt.dwell_ms > 0);
  const avgDwell = dwellEvents.length
    ? dwellEvents.reduce((sum, evt) => sum + Number(evt.dwell_ms || 0), 0) / dwellEvents.length
    : 0;

  const zoneGroups = new Map();
  for (const evt of customers) {
    if (!evt.zone_id) continue;
    if (!zoneGroups.has(evt.zone_id)) zoneGroups.set(evt.zone_id, []);
    zoneGroups.get(evt.zone_id).push(evt);
  }

  const zoneDwell = [...zoneGroups.entries()]
    .map(([zoneId, zoneEvents]) => ({
      zone_id: zoneId,
      avg_dwell_ms: round(zoneEvents.reduce((sum, evt) => sum + Number(evt.dwell_ms || 0), 0) / zoneEvents.length),
      visit_count: zoneEvents.length,
    }))
    .sort((a, b) => b.avg_dwell_ms - a.avg_dwell_ms);

  const uniqueVisitors = entries;
  const conversionRate = uniqueVisitors > 0 ? converted / uniqueVisitors : 0;
  const abandonmentRate = billing > 0 ? abandons / billing : 0;

  return {
    store_id: storeId,
    window_start: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    window_end: new Date().toISOString(),
    unique_visitors: uniqueVisitors,
    converted_visitors: converted,
    conversion_rate: round(conversionRate, 4),
    avg_dwell_ms: round(avgDwell, 2),
    zone_dwell: zoneDwell,
    queue_depth: currentQueueDepth(events),
    abandonment_rate: round(abandonmentRate, 4),
    data_confidence: confidence(uniqueVisitors),
  };
}

function computeFunnel(storeId) {
  const state = getStoreState(storeId);
  const events = state.events.filter((evt) => !evt.is_staff);
  const entered = distinctCount(events, (evt) => evt.event_type === "ENTRY" || evt.event_type === "REENTRY");
  const visitedZone = distinctCount(events, (evt) => evt.event_type === "ZONE_ENTER" || evt.event_type === "ZONE_DWELL");
  const billing = distinctCount(events, (evt) => evt.event_type === "BILLING_QUEUE_JOIN");
  const purchased = Math.max(0, billing - distinctCount(events, (evt) => evt.event_type === "BILLING_QUEUE_ABANDON"));

  return {
    store_id: storeId,
    sessions: entered,
    stages: buildStages([
      ["Entry", entered],
      ["Zone Visit", visitedZone],
      ["Billing Queue", billing],
      ["Purchase", purchased],
    ]),
  };
}

function computeHeatmap(storeId) {
  const state = getStoreState(storeId);
  const events = state.events.filter((evt) => !evt.is_staff && evt.zone_id);
  const groups = new Map();
  for (const evt of events) {
    if (!groups.has(evt.zone_id)) groups.set(evt.zone_id, []);
    groups.get(evt.zone_id).push(evt);
  }

  const rows = [...groups.entries()].map(([zoneId, zoneEvents]) => {
    const avg = zoneEvents.reduce((sum, evt) => sum + Number(evt.dwell_ms || 0), 0) / zoneEvents.length;
    return {
      zone_id: zoneId,
      visit_frequency: zoneEvents.length,
      avg_dwell_ms: round(avg, 2),
      normalised_score: 0,
      data_confidence: zoneEvents.length >= 20 ? "HIGH" : "LOW",
    };
  });

  const maxVisit = Math.max(...rows.map((row) => row.visit_frequency), 1);
  const maxDwell = Math.max(...rows.map((row) => row.avg_dwell_ms), 1);
  for (const row of rows) {
    const freqScore = (row.visit_frequency / maxVisit) * 50;
    const dwellScore = (row.avg_dwell_ms / maxDwell) * 50;
    row.normalised_score = round(Math.min(100, freqScore + dwellScore), 1);
  }

  rows.sort((a, b) => b.normalised_score - a.normalised_score);

  return {
    store_id: storeId,
    generated_at: new Date().toISOString(),
    total_sessions: distinctCount(events, (evt) => evt.event_type === "ENTRY"),
    cells: rows,
    data_confidence: rows.length >= 20 ? "HIGH" : "LOW",
  };
}

function computeAnomalies(storeId) {
  const state = getStoreState(storeId);
  const metrics = computeMetrics(storeId);
  const anomalies = [];

  if (metrics.queue_depth >= 5) {
    anomalies.push({
      anomaly_id: crypto.randomUUID(),
      store_id: storeId,
      anomaly_type: "BILLING_QUEUE_SPIKE",
      severity: metrics.queue_depth >= 8 ? "CRITICAL" : "WARN",
      detected_at: new Date().toISOString(),
      description: `Billing queue depth is ${metrics.queue_depth}`,
      suggested_action: "Open another billing counter and redirect a floor associate.",
      metadata: { queue_depth: metrics.queue_depth },
    });
  }

  if (metrics.conversion_rate < 0.2 && metrics.unique_visitors >= 3) {
    anomalies.push({
      anomaly_id: crypto.randomUUID(),
      store_id: storeId,
      anomaly_type: "CONVERSION_DROP",
      severity: "WARN",
      detected_at: new Date().toISOString(),
      description: `Conversion rate is currently ${pct(metrics.conversion_rate)}.`,
      suggested_action: "Check the billing queue and POS flow.",
      metadata: { conversion_rate: metrics.conversion_rate },
    });
  }

  const lastEventAt = state.lastEventAt;
  if (lastEventAt && (Date.now() - lastEventAt.getTime()) / 60000 > 10) {
    anomalies.push({
      anomaly_id: crypto.randomUUID(),
      store_id: storeId,
      anomaly_type: "STALE_FEED",
      severity: "WARN",
      detected_at: new Date().toISOString(),
      description: "No fresh events have been received recently.",
      suggested_action: "Check the camera feed and ingestion pipeline.",
      metadata: { lag_minutes: round((Date.now() - lastEventAt.getTime()) / 60000, 1) },
    });
  }

  if (metrics.unique_visitors === 0) {
    anomalies.push({
      anomaly_id: crypto.randomUUID(),
      store_id: storeId,
      anomaly_type: "EMPTY_STORE",
      severity: "INFO",
      detected_at: new Date().toISOString(),
      description: "No active visitors detected.",
      suggested_action: "Normal if the store is closed or off-peak.",
      metadata: { active_visitors: 0 },
    });
  }

  return { store_id: storeId, anomalies };
}

function computeHealth() {
  const storeFeeds = [];
  for (const storeId of ["STORE_BLR_002", "STORE_BLR_003"]) {
    const state = getStoreState(storeId);
    const last = state.lastEventAt;
    const lagMinutes = last ? (Date.now() - last.getTime()) / 60000 : null;
    storeFeeds.push({
      store_id: storeId,
      last_event_at: last ? last.toISOString() : null,
      lag_minutes: lagMinutes === null ? null : round(lagMinutes, 1),
      status: last ? (lagMinutes > 10 ? "STALE_FEED" : "OK") : "NO_DATA",
    });
  }

  return {
    status: "ok",
    version: "demo",
    db_connected: true,
    cache_connected: true,
    store_feeds: storeFeeds,
    checked_at: new Date().toISOString(),
  };
}

function buildStages(namedCounts) {
  return namedCounts.map(([stage, count], index) => {
    const previous = index > 0 ? namedCounts[index - 1][1] : count;
    const dropOffPct = previous > 0 ? round((1 - count / previous) * 100, 2) : 0;
    return { stage, count, drop_off_pct: dropOffPct };
  });
}

function confidence(uniqueVisitors) {
  if (uniqueVisitors >= 50) return "HIGH";
  if (uniqueVisitors >= 20) return "MEDIUM";
  return "LOW";
}

function round(value, digits = 2) {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function pct(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function sendJson(res, code, payload) {
  res.writeHead(code, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  });
  res.end(JSON.stringify(payload));
}

function broadcast(storeId, payload) {
  const state = getStoreState(storeId);
  const message = JSON.stringify(payload);
  for (const client of state.clients) {
    if (client.readyState === 1) {
      client.send(message);
    }
  }
}

function tickStore(storeId) {
  const templatesForStore = templates.get(storeId) || [];
  if (templatesForStore.length === 0) return;

  const state = getStoreState(storeId);
  const template = templatesForStore[state.cursor % templatesForStore.length];
  state.cursor += 1;

  const evt = cloneEvent(template);
  appendEvent(storeId, evt);
  broadcast(storeId, { type: "event", data: evt });
  broadcast(storeId, { type: "metrics_snapshot", data: computeMetrics(storeId) });
}

function ingestEvents(events) {
  const accepted = [];
  for (const raw of events) {
    if (!raw || !raw.store_id) continue;
    const evt = cloneEvent(raw, raw.timestamp || new Date().toISOString());
    appendEvent(raw.store_id, evt);
    accepted.push(evt);
    broadcast(raw.store_id, { type: "event", data: evt });
  }
  for (const evt of accepted) {
    broadcast(evt.store_id, { type: "metrics_snapshot", data: computeMetrics(evt.store_id) });
  }
  return accepted.length;
}

for (const storeId of ["STORE_BLR_002", "STORE_BLR_003"]) {
  const initialTemplates = templates.get(storeId) || [];
  const warmup = initialTemplates.slice(0, 4);
  for (const template of warmup) {
    appendEvent(storeId, cloneEvent(template));
  }
  setInterval(() => tickStore(storeId), 1500);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "content-type",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    });
    res.end();
    return;
  }

  if (req.method === "GET" && url.pathname === "/health") {
    sendJson(res, 200, computeHealth());
    return;
  }

  const metricsMatch = url.pathname.match(/^\/stores\/([^/]+)\/metrics$/);
  if (req.method === "GET" && metricsMatch) {
    sendJson(res, 200, computeMetrics(metricsMatch[1]));
    return;
  }

  const funnelMatch = url.pathname.match(/^\/stores\/([^/]+)\/funnel$/);
  if (req.method === "GET" && funnelMatch) {
    sendJson(res, 200, computeFunnel(funnelMatch[1]));
    return;
  }

  const heatmapMatch = url.pathname.match(/^\/stores\/([^/]+)\/heatmap$/);
  if (req.method === "GET" && heatmapMatch) {
    sendJson(res, 200, computeHeatmap(heatmapMatch[1]));
    return;
  }

  const anomaliesMatch = url.pathname.match(/^\/stores\/([^/]+)\/anomalies$/);
  if (req.method === "GET" && anomaliesMatch) {
    sendJson(res, 200, computeAnomalies(anomaliesMatch[1]));
    return;
  }

  if (req.method === "POST" && url.pathname === "/events/ingest") {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk.toString();
      if (body.length > 2_000_000) req.destroy();
    });
    req.on("end", () => {
      try {
        const parsed = JSON.parse(body || "{}");
        const events = Array.isArray(parsed.events) ? parsed.events : [];
        const accepted = ingestEvents(events);
        sendJson(res, 207, {
          accepted,
          rejected: events.length - accepted,
          duplicates: 0,
          errors: [],
          trace_id: crypto.randomUUID(),
        });
      } catch (error) {
        sendJson(res, 422, { error: String(error) });
      }
    });
    return;
  }

  sendJson(res, 404, { error: "not_found" });
});

const wss = new WebSocketServer({ noServer: true });

server.on("upgrade", (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  const match = url.pathname.match(/^\/ws\/([^/]+)$/);
  if (!match) {
    socket.destroy();
    return;
  }

  const storeId = match[1];
  wss.handleUpgrade(request, socket, head, (ws) => {
    wss.emit("connection", ws, request, storeId);
  });
});

wss.on("connection", (ws, request, storeId) => {
  const state = getStoreState(storeId);
  state.clients.add(ws);

  const snapshot = computeMetrics(storeId);
  ws.send(JSON.stringify({ type: "metrics_snapshot", data: snapshot }));

  const recentEvents = state.events.slice(-12);
  for (const evt of recentEvents) {
    ws.send(JSON.stringify({ type: "event", data: evt }));
  }

  ws.on("close", () => {
    state.clients.delete(ws);
  });
});

server.listen(PORT, () => {
  console.log(`Mock live API running at http://localhost:${PORT}`);
  console.log("Use this with the dashboard server for live demo updates.");
});