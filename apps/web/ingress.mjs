import { createServer } from "node:http";
import next from "next";

const app = next({ dev: process.argv.includes("--dev") });
const handle = app.getRequestHandler();
await app.prepare();

createServer((request, response) => {
  const source = request.socket.remoteAddress;
  if (!source) {
    response.writeHead(503).end();
    return;
  }

  for (const name of Object.keys(request.headers)) {
    if (name === "forwarded" || name === "x-real-ip" || name.startsWith("x-forwarded-")) {
      delete request.headers[name];
    }
  }
  // The only HTTP listener replaces any browser-provided source with the TCP peer.
  request.headers["x-carepulse-ingress-source"] = source;
  handle(request, response).catch(() => {
    if (!response.headersSent) response.writeHead(503);
    response.end();
  });
}).listen(3000, "0.0.0.0");
