import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const target = "wasm32-wasip1";
const builtWasm = path.join(repoRoot, "target", target, "release", "eudkit-lsp.wasm");
const outWasm = path.join(here, "eudkit-lsp.wasm");

execFileSync("cargo", ["build", "--release", "-p", "eudkit-lsp", "--target", target], {
  cwd: repoRoot,
  stdio: "inherit",
});

if (!fs.existsSync(builtWasm)) {
  throw new Error(`Expected built Wasm binary at ${builtWasm}`);
}

fs.copyFileSync(builtWasm, outWasm);
process.stdout.write(`Bundled ${outWasm} (${fs.statSync(outWasm).size} bytes)\n`);
