import { createServer } from "node:http";
import next from "next";
import {
  acquireWebAdmissionPermit,
  isCompatEnabled,
  loadCompatWebConfig,
  releaseWebAdmissionPermit,
} from "./compat/admission.ts";

const app = next({ dev: process.argv.includes("--dev") });
const handle = app.getRequestHandler();
await app.prepare();

function denyUnavailable(response) {
  if (!response.headersSent) {
    response.writeHead(503, {
      "Cache-Control": "no-store",
      "Content-Type": "application/json",
    });
  }
  response.end(JSON.stringify({ error: "Service unavailable" }));
}

createServer(async (request, response) => {
  const source = request.socket.remoteAddress;
  if (!source) {
    denyUnavailable(response);
    return;
  }

  for (const name of Object.keys(request.headers)) {
    if (
      name === "forwarded" ||
      name === "x-real-ip" ||
      name.startsWith("x-forwarded-") ||
      name === "x-carepulse-generation"
    ) {
      delete request.headers[name];
    }
  }
  // The only HTTP listener replaces any browser-provided source with the TCP peer.
  request.headers["x-carepulse-ingress-source"] = source;

  const urlPath = (request.url ?? "/").split("?")[0];
  if (urlPath === "/health/live" || urlPath === "/health/ready") {
    handle(request, response).catch(() => denyUnavailable(response));
    return;
  }

  let permit;
  if (isCompatEnabled()) {
    try {
      const compatCfg = loadCompatWebConfig();
      if (!compatCfg) {
        denyUnavailable(response);
        return;
      }
      permit = await acquireWebAdmissionPermit(compatCfg);
    } catch {
      denyUnavailable(response);
      return;
    }
  }

  let released = false;
  const finishPermit = (disposition) => {
    if (!permit || released) return;
    released = true;
    releaseWebAdmissionPermit(permit, disposition).catch(() => {});
  };
  if (permit) {
    response.once("finish", () =>
      finishPermit(response.statusCode < 500 ? "COMMITTED" : "ROLLED_BACK"),
    );
    response.once("close", () =>
      finishPermit(response.writableEnded && response.statusCode < 500 ? "COMMITTED" : "ROLLED_BACK"),
    );
  }

  handle(request, response).catch(() => {
    finishPermit("ROLLED_BACK");
    denyUnavailable(response);
  });
}).listen(3000, "0.0.0.0");
