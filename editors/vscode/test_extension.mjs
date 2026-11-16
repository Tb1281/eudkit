import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import Module, { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const require = createRequire(import.meta.url);
const extensionPath = require.resolve("./extension.js");

const state = { vscode: undefined, LanguageClient: undefined };
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === "vscode") {
    return state.vscode;
  }
  if (request === "vscode-languageclient/node") {
    return { LanguageClient: state.LanguageClient, TransportKind: { stdio: "stdio" } };
  }
  return originalLoad.call(this, request, parent, isMain);
};

function loadExtension() {
  delete require.cache[extensionPath];
  return require(extensionPath);
}

function makeVscode({ trusted = true, folders = [], settings = {}, globalSettings = {} } = {}) {
  const outputLines = [];
  const errorMessages = [];
  const configurationScopes = [];
  const vscode = {
    workspace: {
      isTrusted: trusted,
      workspaceFolders: folders,
      getConfiguration(section, scope) {
        configurationScopes.push({ section, scope });
        return {
          get(key, fallback) {
            return Object.hasOwn(settings, key) ? settings[key] : fallback;
          },
          inspect(key) {
            return {
              key,
              globalValue: globalSettings[key],
              workspaceValue: settings[key],
            };
          },
        };
      },
      onDidChangeConfiguration(handler) {
        vscode._configurationHandler = handler;
        return { dispose() {} };
      },
    },
    window: {
      createOutputChannel() {
        return {
          appendLine(line) {
            outputLines.push(line);
          },
          show() {},
          dispose() {},
        };
      },
      showErrorMessage(message) {
        errorMessages.push(message);
        return Promise.resolve();
      },
    },
  };
  return { vscode, outputLines, errorMessages, configurationScopes };
}

class StubLanguageClient {
  static instances = [];
  static behavior = {};

  constructor(id, name, server) {
    this.id = id;
    this.name = name;
    this.server = server;
    this.started = false;
    this.stopped = false;
    StubLanguageClient.instances.push(this);
  }

  async start() {
    const behavior = StubLanguageClient.behavior;
    if (behavior.started) {
      await behavior.started;
    }
    if (behavior.failure) {
      throw new Error(behavior.failure);
    }
    this.started = true;
  }

  async stop() {
    this.stopped = true;
  }
}

function tick(ms = 10) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function reset() {
  StubLanguageClient.instances = [];
  StubLanguageClient.behavior = {};
  state.vscode = undefined;
  state.LanguageClient = StubLanguageClient;
}

function context() {
  return { subscriptions: [] };
}

function tempWorkspace(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-vscode-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function fileWorkspace(directory) {
  return [{ uri: { scheme: "file", fsPath: directory } }];
}

test.beforeEach(reset);

test("trusted workspace resolves a target/release binary", (t) => {
  const directory = tempWorkspace(t);
  const binary = path.join(directory, "target", "release", "eudkit-lsp");
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  fs.writeFileSync(binary, "");
  const { vscode } = makeVscode({ trusted: true, folders: fileWorkspace(directory) });
  state.vscode = vscode;
  const extension = loadExtension();
  assert.equal(extension.resolveServerCommand(), binary);
});

test("untrusted workspace ignores workspace settings and binaries", (t) => {
  const directory = tempWorkspace(t);
  const binary = path.join(directory, "target", "release", "eudkit-lsp");
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  fs.writeFileSync(binary, "");
  const { vscode } = makeVscode({
    trusted: false,
    folders: fileWorkspace(directory),
    settings: { "lsp.path": "/workspace/only/eudkit-lsp" },
  });
  state.vscode = vscode;
  const extension = loadExtension();
  const options = extension.resolveServerOptions({ wasmPath: path.join(directory, "missing.wasm") });
  assert.equal(options.command, process.platform === "win32" ? "eudkit-lsp.exe" : "eudkit-lsp");
});

test("resolves executable on PATH before bundled wasm fallback", (t) => {
  const directory = tempWorkspace(t);
  const binDir = path.join(directory, "bin");
  fs.mkdirSync(binDir, { recursive: true });
  const binaryName = process.platform === "win32" ? "eudkit-lsp.exe" : "eudkit-lsp";
  const onPath = path.join(binDir, binaryName);
  fs.writeFileSync(onPath, "");
  fs.chmodSync(onPath, 0o755);

  const prevPath = process.env.PATH;
  process.env.PATH = binDir;
  t.after(() => {
    process.env.PATH = prevPath;
  });

  const { vscode } = makeVscode({ trusted: true, folders: [] });
  state.vscode = vscode;
  const extension = loadExtension();
  const options = extension.resolveServerOptions({
    wasmPath: path.join(directory, "eudkit-lsp.wasm"),
    runnerPath: path.join(directory, "wasm-server.js"),
  });
  assert.equal(options.command, onPath);
});

test("falls back to bundled wasm server when no native binary is found", (t) => {
  const directory = tempWorkspace(t);
  const wasmPath = path.join(directory, "eudkit-lsp.wasm");
  const runnerPath = path.join(directory, "wasm-server.js");
  fs.writeFileSync(wasmPath, "stub");
  fs.writeFileSync(runnerPath, "stub");

  const prevPath = process.env.PATH;
  process.env.PATH = "";
  t.after(() => {
    process.env.PATH = prevPath;
  });

  const { vscode } = makeVscode({ trusted: true, folders: fileWorkspace(directory) });
  state.vscode = vscode;
  const extension = loadExtension();
  const options = extension.resolveServerOptions({ wasmPath, runnerPath });
  assert.equal(options.module, runnerPath);
  assert.equal(options.transport, "stdio");
  assert.deepEqual(options.options, { execArgv: ["--no-warnings"] });
});

test("untrusted workspace falls back to the global setting", (t) => {
  const directory = tempWorkspace(t);
  const { vscode } = makeVscode({
    trusted: false,
    folders: fileWorkspace(directory),
    settings: { "lsp.path": "/workspace/only/eudkit-lsp" },
    globalSettings: { "lsp.path": "/global/eudkit-lsp" },
  });
  state.vscode = vscode;
  const extension = loadExtension();
  assert.equal(extension.resolveServerCommand(), "/global/eudkit-lsp");
});

test("trusted workspace honors the scoped setting", (t) => {
  const directory = tempWorkspace(t);
  const { vscode, configurationScopes } = makeVscode({
    trusted: true,
    folders: fileWorkspace(directory),
    settings: { "lsp.path": "/workspace/eudkit-lsp" },
  });
  state.vscode = vscode;
  const extension = loadExtension();
  assert.equal(extension.resolveServerCommand(), "/workspace/eudkit-lsp");
  assert.ok(
    configurationScopes.some((call) => call.scope === vscode.workspace.workspaceFolders[0].uri),
    "getConfiguration should be scoped to the workspace folder URI",
  );
});

test("start failures are logged once instead of popping modal errors", async () => {
  StubLanguageClient.behavior = { failure: "missing binary" };
  const { vscode, outputLines, errorMessages } = makeVscode();
  state.vscode = vscode;
  const extension = loadExtension();
  const subscriptions = context();
  extension.activate(subscriptions);
  await tick();
  vscode._configurationHandler({ affectsConfiguration: () => true });
  await tick();
  await extension.deactivate();

  assert.deepEqual(errorMessages, []);
  const warnings = outputLines.filter((line) => line.includes("Failed to start"));
  assert.equal(warnings.length, 1);
});

test("deactivate during a pending start does not leave a running client", async () => {
  let release;
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  StubLanguageClient.behavior = { started: pending };
  const { vscode, outputLines } = makeVscode();
  state.vscode = vscode;
  const extension = loadExtension();
  extension.activate(context());
  await tick();
  const stopping = extension.deactivate();
  release();
  await stopping;
  await tick();

  assert.equal(StubLanguageClient.instances.length, 1);
  assert.equal(StubLanguageClient.instances[0].stopped, true);
  assert.equal(
    outputLines.some((line) => line.includes("is running")),
    false,
  );
});

test("concurrent restart requests are coalesced", async () => {
  const { vscode } = makeVscode();
  state.vscode = vscode;
  const extension = loadExtension();
  extension.activate(context());
  await tick();
  vscode._configurationHandler({ affectsConfiguration: () => true });
  vscode._configurationHandler({ affectsConfiguration: () => true });
  await tick();
  await extension.deactivate();

  assert.equal(StubLanguageClient.instances.length, 2);
});

test("bundled wasm-server speaks JSON-RPC LSP over stdio when built", async (t) => {
  const extDir = path.dirname(extensionPath);
  const wasmPath = path.join(extDir, "eudkit-lsp.wasm");
  const runnerPath = path.join(extDir, "wasm-server.js");
  if (!fs.existsSync(wasmPath)) {
    t.skip("eudkit-lsp.wasm not built (run `npm run build:wasm`)");
    return;
  }

  function frame(message) {
    const body = Buffer.from(JSON.stringify(message), "utf8");
    return Buffer.concat([
      Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, "utf8"),
      body,
    ]);
  }

  const child = spawn(process.execPath, ["--no-warnings", runnerPath], {
    stdio: ["pipe", "pipe", "pipe"],
  });

  const stdoutChunks = [];
  const stderrChunks = [];
  child.stdout.on("data", (chunk) => stdoutChunks.push(chunk));
  child.stderr.on("data", (chunk) => stderrChunks.push(chunk));

  child.stdin.write(frame({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }));
  child.stdin.write(frame({ jsonrpc: "2.0", method: "initialized", params: {} }));
  child.stdin.write(
    frame({
      jsonrpc: "2.0",
      method: "textDocument/didOpen",
      params: {
        textDocument: {
          uri: "file:///test.ts",
          languageId: "typescript",
          version: 1,
          text: "const s = FixedString(0);\n",
        },
      },
    }),
  );
  child.stdin.write(frame({ jsonrpc: "2.0", id: 2, method: "shutdown", params: null }));
  child.stdin.write(frame({ jsonrpc: "2.0", method: "exit", params: null }));
  child.stdin.end();

  const exitCode = await new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", resolve);
  });

  const stderr = Buffer.concat(stderrChunks).toString("utf8");
  const stdout = Buffer.concat(stdoutChunks).toString("utf8");
  assert.equal(exitCode, 0, `unexpected stderr: ${stderr}`);
  assert.ok(stdout.includes('"serverInfo"'), `missing serverInfo: ${stdout}`);
  assert.ok(stdout.includes("EUDSL-BOUNDS"), `missing diagnostics: ${stdout}`);
});
