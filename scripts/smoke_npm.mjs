#!/usr/bin/env node
// Install a packed eudkit tarball into a clean project and import it.
//
// Usage (from ts/): node ../scripts/smoke_npm.mjs eudkit-3.0.0.tgz

import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const argument = process.argv[2];
if (!argument) {
  console.error("usage: node scripts/smoke_npm.mjs <tarball>");
  process.exit(2);
}

const tarball = path.resolve(argument);
const project = mkdtempSync(path.join(os.tmpdir(), "eudkit-npm-smoke-"));
const npm = "npm";
const options = { cwd: project, stdio: "inherit", shell: process.platform === "win32" };

try {
  execFileSync(npm, ["init", "-y"], { ...options, stdio: "ignore" });
  execFileSync(npm, ["install", tarball, "--no-audit", "--no-fund"], options);
  writeFileSync(
    path.join(project, "smoke.mjs"),
    'import { buildModule } from "eudkit";\n' +
      'if (typeof buildModule !== "function") process.exit(1);\n' +
      'console.log("npm import ok");\n',
  );
  execFileSync(process.execPath, ["smoke.mjs"], options);
} finally {
  rmSync(project, { recursive: true, force: true });
}
