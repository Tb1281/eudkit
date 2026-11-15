import assert from "node:assert/strict";
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
  assert.equal(extension.resolveServerCommand(), "eudkit-lsp");
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
