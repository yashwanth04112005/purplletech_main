/**
 * server.js — Express + WebSocket proxy server for the live dashboard.
 * Connects to the FastAPI WebSocket, forwards events to browser clients,
 * and serves the static dashboard HTML.
 */
const express = require("express");
const http = require("http");
const path = require("path");
const { WebSocketServer, WebSocket } = require("ws");

const API_URL = process.env.API_URL || "http://localhost:8000";
const PORT = process.env.PORT || 3000;

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server });

// Serve static files
app.use(express.static(path.join(__dirname, "public")));

// Health check
app.get("/ping", (_, res) => res.json({ status: "ok" }));

// All store IDs that have connected browsers
const storeConnections = new Map(); // storeId → Set<BrowserWebSocket>

wss.on("connection", (browserWs, req) => {
  // URL format: ws://localhost:3000/live/STORE_BLR_002
  const storeId = req.url.split("/").pop();
  if (!storeId) {
    browserWs.close(1008, "store_id required");
    return;
  }

  console.log(`[dashboard] Browser connected for store: ${storeId}`);

  if (!storeConnections.has(storeId)) {
    storeConnections.set(storeId, new Set());
  }
  storeConnections.get(storeId).add(browserWs);

  // Connect to FastAPI WebSocket
  const apiWsUrl = API_URL.replace("http", "ws") + `/ws/${storeId}`;
  let apiWs;

  function connectToApi() {
    apiWs = new WebSocket(apiWsUrl);

    apiWs.on("open", () => {
      console.log(`[dashboard] Connected to API WS: ${apiWsUrl}`);
    });

    apiWs.on("message", (data) => {
      const clients = storeConnections.get(storeId) || new Set();
      for (const client of clients) {
        if (client.readyState === WebSocket.OPEN) {
          client.send(data.toString());
        }
      }
    });

    apiWs.on("close", () => {
      console.log(`[dashboard] API WS closed for ${storeId}, retrying in 3s...`);
      setTimeout(connectToApi, 3000);
    });

    apiWs.on("error", (err) => {
      console.error(`[dashboard] API WS error: ${err.message}`);
    });
  }

  connectToApi();

  browserWs.on("close", () => {
    const clients = storeConnections.get(storeId);
    if (clients) {
      clients.delete(browserWs);
      if (clients.size === 0 && apiWs) {
        apiWs.close();
        storeConnections.delete(storeId);
      }
    }
    console.log(`[dashboard] Browser disconnected from store: ${storeId}`);
  });
});

server.listen(PORT, () => {
  console.log(`Store Intelligence Dashboard running at http://localhost:${PORT}`);
  console.log(`Open: http://localhost:${PORT}?store=STORE_BLR_002`);
});
