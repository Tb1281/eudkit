//! JSON-RPC framing and lifecycle tests against the `eudkit-lsp` binary.

use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{Value, json};

struct Client {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Client {
    fn spawn() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_eudkit-lsp"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn eudkit-lsp");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            stdin,
            stdout,
        }
    }

    fn send(&mut self, message: &Value) {
        let body = serde_json::to_vec(message).unwrap();
        write!(self.stdin, "Content-Length: {}\r\n\r\n", body.len()).unwrap();
        self.stdin.write_all(&body).unwrap();
        self.stdin.flush().unwrap();
    }

    fn recv(&mut self) -> Value {
        let mut length = None;
        loop {
            let mut line = String::new();
            let read = self.stdout.read_line(&mut line).unwrap();
            assert!(read > 0, "server closed stdout");
            let header = line.trim_end_matches(['\r', '\n']);
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':') {
                if name.eq_ignore_ascii_case("content-length") {
                    length = Some(value.trim().parse::<usize>().unwrap());
                }
            }
        }
        let len = length.expect("Content-Length");
        let mut body = vec![0u8; len];
        self.stdout.read_exact(&mut body).unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    fn initialize(&mut self) -> Value {
        self.send(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": { "processId": null, "rootUri": null, "capabilities": {} }
        }));
        self.recv()
    }

    fn shutdown(mut self) {
        self.send(&json!({ "jsonrpc": "2.0", "id": 99, "method": "shutdown" }));
        let reply = self.recv();
        assert_eq!(reply["id"], 99);
        self.send(&json!({ "jsonrpc": "2.0", "method": "exit" }));
        drop(self.stdin);
        let status = self.child.wait().unwrap();
        assert!(status.success(), "server exited with {status}");
    }
}

/// Open `text` in a fresh client and return the `textDocument/hover` response
/// for `position` (line, character).
fn hover(uri: &str, language_id: &str, text: &str, position: (u32, u32)) -> Value {
    let mut client = Client::spawn();
    client.initialize();
    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text
            }
        }
    }));
    let _ = client.recv();
    client.send(&json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/hover",
        "params": {
            "textDocument": { "uri": uri },
            "position": { "line": position.0, "character": position.1 }
        }
    }));
    let response = client.recv();
    client.shutdown();
    response
}

#[test]
fn server_initializes_and_publishes_domain_diagnostics() {
    let mut client = Client::spawn();
    let init = client.initialize();
    assert_eq!(init["id"], 1);
    let caps = &init["result"]["capabilities"];
    assert_eq!(caps["textDocumentSync"], 1);
    assert!(caps["hoverProvider"].as_bool().unwrap());
    assert!(
        caps["completionProvider"]["triggerCharacters"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v == ".")
    );
    assert_eq!(init["result"]["serverInfo"]["name"], "eudkit-lsp");

    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": "file:///demo.ts",
                "languageId": "typescript",
                "version": 1,
                "text": "const s = new FixedString(70000);\n"
            }
        }
    }));
    let diagnostics = client.recv();
    assert_eq!(diagnostics["method"], "textDocument/publishDiagnostics");
    assert_eq!(diagnostics["params"]["uri"], "file:///demo.ts");
    let list = diagnostics["params"]["diagnostics"].as_array().unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0]["code"], "EUDSL-BOUNDS");
    assert_eq!(list[0]["severity"], 1);
    assert_eq!(list[0]["source"], "eudkit-lsp");

    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": { "uri": "file:///demo.ts", "version": 2 },
            "contentChanges": [{ "text": "const s = new FixedString(8);\n" }]
        }
    }));
    let diagnostics = client.recv();
    assert!(
        diagnostics["params"]["diagnostics"]
            .as_array()
            .unwrap()
            .is_empty()
    );

    client.shutdown();
}

#[test]
fn completion_and_hover_answer_direct_property_access() {
    let mut client = Client::spawn();
    client.initialize();
    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": "file:///demo.ts",
                "languageId": "typescript",
                "version": 1,
                "text": "u.hp"
            }
        }
    }));
    let _ = client.recv();

    client.send(&json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/completion",
        "params": {
            "textDocument": { "uri": "file:///demo.ts" },
            "position": { "line": 0, "character": 2 }
        }
    }));
    let completion = client.recv();
    assert_eq!(completion["id"], 2);
    let items = completion["result"]["items"].as_array().unwrap();
    let hp = items.iter().find(|item| item["label"] == "hp").unwrap();
    assert_eq!(hp["kind"], 10);
    assert!(hp["detail"].as_str().unwrap().contains("dword"));

    let response = hover("file:///demo.ts", "typescript", "u.hp", (0, 3));
    let contents = response["result"]["contents"]["value"].as_str().unwrap();
    assert!(contents.contains("CUnit hit points"), "{contents}");

    assert_eq!(
        hover("file:///demo.ts", "typescript", "u.hp", (0, 0))["result"],
        Value::Null
    );

    client.shutdown();
}

#[test]
fn unknown_requests_return_method_not_found_and_shutdown_is_required() {
    let mut client = Client::spawn();
    client.initialize();
    client.send(&json!({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "workspace/unknownThing"
    }));
    let reply = client.recv();
    assert_eq!(reply["error"]["code"], -32601);
    client.shutdown();
}

#[test]
fn did_close_publishes_empty_diagnostics() {
    let mut client = Client::spawn();
    client.initialize();
    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": "file:///demo.py",
                "languageId": "python",
                "version": 1,
                "text": "s = FixedString(0)\n"
            }
        }
    }));
    let opened = client.recv();
    assert_eq!(opened["params"]["diagnostics"].as_array().unwrap().len(), 1);

    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": { "textDocument": { "uri": "file:///demo.py" } }
    }));
    let closed = client.recv();
    assert!(
        closed["params"]["diagnostics"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    client.shutdown();
}

#[test]
fn did_open_replays_lexer_diagnostics_through_the_protocol() {
    // The Python Trigger kwargs form and a FixedString call inside a string
    // literal, replayed end to end: exactly one diagnostic (the Python
    // Trigger overflow), and nothing for the literal. The literal is
    // renamed to FixedString so it actually exercises literal-skipping of
    // a real diagnostic name (unrenamed it would pass vacuously).
    let mut client = Client::spawn();
    client.initialize();
    let conds = (0..17).map(|_| "Always()").collect::<Vec<_>>().join(", ");
    let text = format!(
        "s = \"new FixedString(0)\"\nt = Trigger(conds=[{conds}], acts=[])\n"
    );
    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": "file:///demo.py",
                "languageId": "python",
                "version": 1,
                "text": text
            }
        }
    }));
    let diagnostics = client.recv();
    assert_eq!(diagnostics["method"], "textDocument/publishDiagnostics");
    let list = diagnostics["params"]["diagnostics"].as_array().unwrap();
    assert_eq!(list.len(), 1);
    assert!(list[0]["message"]
        .as_str()
        .unwrap()
        .contains("conds has 17 entries"));
    client.shutdown();
}

#[test]
fn did_change_keeps_python_comment_handling() {
    // End to end: a Python `#` comment is skipped at didOpen, and the
    // stored language still applies to a later didChange (which carries
    // no languageId), so only the real call is flagged.
    let mut client = Client::spawn();
    client.initialize();
    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": "file:///demo.py",
                "languageId": "python",
                "version": 1,
                "text": "# new FixedString(0)\n"
            }
        }
    }));
    let opened = client.recv();
    assert!(
        opened["params"]["diagnostics"]
            .as_array()
            .unwrap()
            .is_empty()
    );

    client.send(&json!({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": { "uri": "file:///demo.py", "version": 2 },
            "contentChanges": [{ "text": "# new FixedString(0)\nFixedString(0)\n" }]
        }
    }));
    let changed = client.recv();
    let list = changed["params"]["diagnostics"].as_array().unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0]["code"], "EUDSL-BOUNDS");
    client.shutdown();
}

#[test]
fn build_module_hover_documents_the_root_entry_export() {
    let ts_hover = hover("file:///entry.ts", "typescript", "buildModule", (0, 2));
    let contents = ts_hover["result"]["contents"]["value"].as_str().unwrap();
    assert!(contents.contains("root source"), "{contents}");
    assert!(contents.contains("`module`"), "{contents}");

    let py_hover = hover("file:///entry.py", "python", "build_module", (0, 2));
    let contents = py_hover["result"]["contents"]["value"].as_str().unwrap();
    assert!(contents.contains("root source"), "{contents}");
    assert!(contents.contains("`module`"), "{contents}");
}
