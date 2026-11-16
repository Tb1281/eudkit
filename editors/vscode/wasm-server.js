"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { WASI } = require("node:wasi");

async function main() {
  const wasmPath = path.join(__dirname, "eudkit-lsp.wasm");
  const wasmBytes = fs.readFileSync(wasmPath);
  const wasi = new WASI({
    version: "preview1",
    args: ["eudkit-lsp"],
    env: {},
    preopens: {},
  });
  const module = await WebAssembly.compile(wasmBytes);
  const instance = await WebAssembly.instantiate(module, wasi.getImportObject());
  wasi.start(instance);
}

main().catch((error) => {
  process.stderr.write(`eudkit-lsp (wasm): ${error?.stack || error}\n`);
  process.exit(1);
});
