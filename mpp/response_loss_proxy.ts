import crypto from "node:crypto";
import { closeSync, fsyncSync, openSync, writeSync } from "node:fs";
import http from "node:http";

const port = Number(process.env.MPP_PROXY_PORT);
const upstreamPort = Number(process.env.MPP_UPSTREAM_PORT);
const faultLog = process.env.MPP_FAULT_LOG;
if (!port || !upstreamPort || !faultLog) throw new Error("proxy port, upstream port, and fault log are required");

let dropped = false;
http.createServer((request, downstream) => {
  const upstream = http.request({
    hostname: "127.0.0.1",
    port: upstreamPort,
    path: request.url,
    method: request.method,
    headers: request.headers,
  }, (response) => {
    const paidPath = request.url === "/paid" || /^\/operations\/[a-f0-9-]+\/pay$/.test(request.url ?? "");
    if (!dropped && request.method === "POST" && paidPath && response.statusCode === 200) {
      dropped = true;
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => {
        const record = JSON.stringify({
          barrier: "upstream 200 body complete, before downstream headers; downstream TCP connection destroyed",
          body_sha256: crypto.createHash("sha256").update(Buffer.concat(chunks)).digest("hex"),
          upstream_receipt_present: response.headers["payment-receipt"] !== undefined,
          happened_at: new Date().toISOString(),
        }) + "\n";
        const fd = openSync(faultLog, "a", 0o600);
        try {
          writeSync(fd, record);
          fsyncSync(fd);
        } finally {
          closeSync(fd);
        }
        downstream.destroy();
      });
    } else {
      downstream.writeHead(response.statusCode ?? 502, response.headers);
      response.pipe(downstream);
    }
  });
  request.pipe(upstream);
  upstream.on("error", () => downstream.destroy());
  request.on("error", () => upstream.destroy());
}).listen(port, "127.0.0.1");
