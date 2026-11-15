"use strict";

const fs = require("fs");
const path = require("path");
const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

const CLIENT_ID = "eudkit-lsp";
const CLIENT_NAME = "eudkit LSP";
const CONFIG_SECTION = "eudkit";
const PATH_SETTING = "lsp.path";

/** @type {import("vscode-languageclient/node").LanguageClient | undefined} */
let client;

/** @type {vscode.OutputChannel | undefined} */
let output;

let disposed = false;
let warningLogged = false;

// Serialize start/stop/restart so deactivate() cannot race a restart that is
// already in flight. Lifecycle tokens make a start that loses the race abort
// instead of resurrecting the client.
let lifecycle = 0;
let queue = Promise.resolve();
let restartPromise;
let restartRequested = false;

function serverBinaryName() {
  return process.platform === "win32" ? "eudkit-lsp.exe" : "eudkit-lsp";
}

function workspaceIsTrusted() {
  return vscode.workspace.isTrusted === true;
}

/**
 * Read "eudkit.lsp.path" scoped to a resource. Untrusted workspaces may only
 * use the user-level (global) value, never a value supplied by the workspace.
 */
function configuredServerPath(resourceUri) {
  const configuration = vscode.workspace.getConfiguration(CONFIG_SECTION, resourceUri);
  let value;
  if (workspaceIsTrusted()) {
    value = configuration.get(PATH_SETTING, "");
  } else {
    const inspected = configuration.inspect(PATH_SETTING);
    value = inspected?.globalValue ?? "";
  }
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Resolve the eudkit-lsp command: explicit setting, then (trusted workspaces
 * only) a workspace target/{release,debug} build, then whatever is on PATH.
 */
function resolveServerCommand() {
  const folders = vscode.workspace.workspaceFolders ?? [];

  for (const folder of folders) {
    const configured = configuredServerPath(folder.uri);
    if (configured) {
      return configured;
    }
  }
  if (folders.length === 0) {
    const configured = configuredServerPath(undefined);
    if (configured) {
      return configured;
    }
  }

  const binary = serverBinaryName();
  if (workspaceIsTrusted()) {
    for (const folder of folders) {
      if (folder.uri.scheme !== "file") {
        continue;
      }
      for (const profile of ["release", "debug"]) {
        const candidate = path.join(folder.uri.fsPath, "target", profile, binary);
        if (fs.existsSync(candidate)) {
          return candidate;
        }
      }
    }
  }

  // Fall back to PATH. On Windows this must keep the .exe suffix.
  return binary;
}

function log(message) {
  output?.appendLine(message);
}

function errorMessage(error) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return String(error);
}

function enqueue(task) {
  const run = queue.then(task, task);
  queue = run.catch(() => {});
  return run;
}

async function stopClient() {
  const current = client;
  client = undefined;
  if (!current) {
    return;
  }
  try {
    await current.stop();
    log(`Stopped ${CLIENT_NAME}.`);
  } catch (error) {
    log(`Failed to stop ${CLIENT_NAME}: ${errorMessage(error)}`);
  }
}

async function startClient(token) {
  if (disposed || token !== lifecycle) {
    return;
  }

  const command = resolveServerCommand();
  log(`Starting ${CLIENT_NAME}: ${command}`);

  let next;
  try {
    next = new LanguageClient(
      CLIENT_ID,
      CLIENT_NAME,
      {
        command,
        args: [],
        transport: TransportKind.stdio,
      },
      {
        documentSelector: [
          { scheme: "file", language: "typescript" },
          { scheme: "file", language: "python" },
        ],
        outputChannel: output,
      },
    );

    client = next;

    await next.start();
    if (disposed || token !== lifecycle) {
      // deactivate()/a newer restart won the race while start() was pending.
      if (client === next) {
        client = undefined;
      }
      await next.stop().catch(() => {});
      return;
    }
    warningLogged = false;
    log(`${CLIENT_NAME} is running.`);
  } catch (error) {
    if (client === next) {
      client = undefined;
    }
    if (!warningLogged) {
      warningLogged = true;
      log(
        `Failed to start ${CLIENT_NAME} (${command}): ${errorMessage(error)}. ` +
          'Build it with "cargo build -p eudkit-lsp" or set "eudkit.lsp.path" ' +
          "to the binary. This warning is reported once per session.",
      );
      output?.show(true);
    }
  }
}

function restartClient() {
  if (disposed) {
    return Promise.resolve();
  }
  if (restartRequested) {
    return restartPromise;
  }
  restartRequested = true;
  restartPromise = enqueue(async () => {
    restartRequested = false;
    const token = ++lifecycle;
    await stopClient();
    if (disposed || token !== lifecycle) {
      return;
    }
    await startClient(token);
  });
  return restartPromise;
}

function activate(context) {
  disposed = false;
  warningLogged = false;
  restartRequested = false;
  lifecycle += 1;

  output = vscode.window.createOutputChannel(CLIENT_NAME);
  context.subscriptions.push(output);

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration(`${CONFIG_SECTION}.${PATH_SETTING}`)) {
        void restartClient();
      }
    }),
  );

  const token = lifecycle;
  void enqueue(() => startClient(token));
}

async function deactivate() {
  disposed = true;
  lifecycle += 1;
  await enqueue(stopClient);
  output?.dispose();
  output = undefined;
}

module.exports = { activate, deactivate, resolveServerCommand };
