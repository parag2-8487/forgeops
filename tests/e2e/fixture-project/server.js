// SPDX-License-Identifier: Apache-2.0
// The fixture service the end-to-end journey containerises.
//
// No framework dependency on purpose: `npm ci` in CI would need a lockfile and a registry round
// trip, and the journey is about ForgeOps generating a Dockerfile for this, not about this running.
const http = require("node:http");

const port = Number(process.env.PORT ?? 3000);

const server = http.createServer((request, response) => {
  if (request.url === "/healthz") {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ status: "ok" }));
    return;
  }
  response.writeHead(404, { "content-type": "application/json" });
  response.end(JSON.stringify({ error: "not found" }));
});

server.listen(port, () => {
  console.log(`fixture service listening on ${port}`);
});
