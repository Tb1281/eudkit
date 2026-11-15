//! Content-Length framed JSON-RPC 2.0 stdio server.
//!
//! The protocol surface is intentionally small: initialize/initialized,
//! didOpen/didChange/didClose, completion, hover, shutdown/exit. All editor
//! intelligence is delegated to the pure functions in the crate root so it can
//! be unit-tested without a client.

use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Write};

use serde_json::{Value, json};

use crate::lexer::Language;
use crate::{completions, diagnostics_for, hover};

/// Editor documents keyed by URI.
///
/// The language is stored per document at `didOpen` time because `didChange`
/// carries no `languageId`: without the stored value, a later edit could not
/// reuse the document's language-aware diagnostics. `didClose` removes both
/// the text and the language, so reopening the same URI without a
/// `languageId` falls back to language-neutral behaviour.
#[derive(Debug, Default)]
pub struct LspState {
    pub documents: HashMap<String, String>,
    pub languages: HashMap<String, Language>,
    pub shutdown_requested: bool,
}

impl LspState {
    pub fn text(&self, uri: &str) -> Option<&str> {
        self.documents.get(uri).map(String::as_str)
    }

    pub fn language(&self, uri: &str) -> Language {
        self.languages.get(uri).copied().unwrap_or_default()
    }

    fn open(&mut self, uri: String, text: String, language: Language) {
        self.documents.insert(uri.clone(), text);
        self.languages.insert(uri, language);
    }

    fn change(&mut self, uri: &str, text: String) {
        self.documents.insert(uri.to_string(), text);
    }

    fn close(&mut self, uri: &str) {
        self.documents.remove(uri);
        self.languages.remove(uri);
    }
}

/// Handle one decoded JSON-RPC message.
///
/// Returns zero or more messages to write back: a response for requests and a
/// `textDocument/publishDiagnostics` notification for document changes.
pub fn handle_message(state: &mut LspState, msg: &Value) -> Vec<Value> {
    let method = msg.get("method").and_then(Value::as_str).unwrap_or("");
    let id = msg.get("id").cloned();
    let params = msg.get("params").cloned().unwrap_or(Value::Null);

    match method {
        "initialize" => vec![response(
            id,
            json!({
                "capabilities": {
                    "textDocumentSync": 1,
                    "completionProvider": {
                        "triggerCharacters": [".", "(", ":"],
                        "resolveProvider": false
                    },
                    "hoverProvider": true
                },
                "serverInfo": { "name": "eudkit-lsp", "version": env!("CARGO_PKG_VERSION") }
            }),
        )],
        "initialized" | "$/setTrace" | "workspace/didChangeConfiguration" => Vec::new(),
        "shutdown" => {
            state.shutdown_requested = true;
            vec![response(id, Value::Null)]
        }
        "exit" => Vec::new(),
        "textDocument/didOpen" => {
            let Some((uri, text)) = open_document(&params) else {
                return Vec::new();
            };
            let language = params
                .pointer("/textDocument/languageId")
                .and_then(Value::as_str)
                .map(Language::from_language_id)
                .unwrap_or_default();
            state.open(uri.clone(), text, language);
            diagnostics_notification(state, &uri)
        }
        "textDocument/didChange" => {
            let uri = params
                .pointer("/textDocument/uri")
                .and_then(Value::as_str)
                .map(str::to_string);
            let text = params
                .get("contentChanges")
                .and_then(Value::as_array)
                .and_then(|changes| changes.last())
                .and_then(|change| change.get("text"))
                .and_then(Value::as_str)
                .map(str::to_string);
            let (Some(uri), Some(text)) = (uri, text) else {
                return Vec::new();
            };
            state.change(&uri, text);
            diagnostics_notification(state, &uri)
        }
        "textDocument/didClose" => {
            if let Some(uri) = params
                .pointer("/textDocument/uri")
                .and_then(Value::as_str)
                .map(str::to_string)
            {
                state.close(&uri);
                return vec![notification(
                    "textDocument/publishDiagnostics",
                    json!({ "uri": uri, "diagnostics": [] }),
                )];
            }
            Vec::new()
        }
        "textDocument/completion" => {
            let Some((uri, line, character)) = document_position(&params) else {
                return vec![response(id, json!([]))];
            };
            let Some(text) = state.text(&uri) else {
                return vec![response(id, json!([]))];
            };
            let items: Vec<Value> = completions(text, line, character)
                .into_iter()
                .filter_map(|item| serde_json::to_value(item).ok())
                .collect();
            vec![response(id, json!({ "isIncomplete": false, "items": items }))]
        }
        "textDocument/hover" => {
            let Some((uri, line, character)) = document_position(&params) else {
                return vec![response(id, Value::Null)];
            };
            let Some(text) = state.text(&uri) else {
                return vec![response(id, Value::Null)];
            };
            match hover(text, line, character) {
                Some(contents) => vec![response(
                    id,
                    json!({ "contents": { "kind": "markdown", "value": contents } }),
                )],
                None => vec![response(id, Value::Null)],
            }
        }
        _ => {
            if id.is_some() {
                vec![error_response(
                    id,
                    -32601,
                    format!("method not found: {method}"),
                )]
            } else {
                Vec::new()
            }
        }
    }
}

fn open_document(params: &Value) -> Option<(String, String)> {
    let uri = params.pointer("/textDocument/uri")?.as_str()?.to_string();
    let text = params
        .pointer("/textDocument/text")?
        .as_str()?
        .to_string();
    Some((uri, text))
}

fn document_position(params: &Value) -> Option<(String, usize, usize)> {
    let uri = params.pointer("/textDocument/uri")?.as_str()?.to_string();
    let line = params.pointer("/position/line")?.as_u64()? as usize;
    let character = params.pointer("/position/character")?.as_u64()? as usize;
    Some((uri, line, character))
}

fn diagnostics_notification(state: &LspState, uri: &str) -> Vec<Value> {
    let Some(text) = state.text(uri) else {
        return Vec::new();
    };
    let diagnostics: Vec<Value> = diagnostics_for(text, state.language(uri))
        .into_iter()
        .filter_map(|diag| serde_json::to_value(diag).ok())
        .collect();
    vec![notification(
        "textDocument/publishDiagnostics",
        json!({ "uri": uri, "diagnostics": diagnostics }),
    )]
}

fn response(id: Option<Value>, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id.unwrap_or(Value::Null), "result": result })
}

fn error_response(id: Option<Value>, code: i64, message: String) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id.unwrap_or(Value::Null),
        "error": { "code": code, "message": message }
    })
}

fn notification(method: &str, params: Value) -> Value {
    json!({ "jsonrpc": "2.0", "method": method, "params": params })
}

/// Read one `Content-Length` framed message, or `None` on EOF.
pub fn read_message<R: BufRead>(reader: &mut R) -> io::Result<Option<Value>> {
    let mut content_length: Option<usize> = None;
    loop {
        let mut line = String::new();
        let read = reader.read_line(&mut line)?;
        if read == 0 {
            return Ok(None);
        }
        let header = line.trim_end_matches(['\r', '\n']);
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().ok();
            }
        }
    }
    let len = content_length.ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidData, "missing Content-Length header")
    })?;
    let mut body = vec![0u8; len];
    reader.read_exact(&mut body)?;
    let value = serde_json::from_slice(&body)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    Ok(Some(value))
}

/// Write one `Content-Length` framed message.
pub fn write_message<W: Write>(writer: &mut W, message: &Value) -> io::Result<()> {
    let body = serde_json::to_vec(message)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    write!(writer, "Content-Length: {}\r\n\r\n", body.len())?;
    writer.write_all(&body)?;
    writer.flush()
}

/// Serve the language server over stdin/stdout until `exit` or EOF.
pub fn run_stdio() -> io::Result<()> {
    let stdin = io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut writer = stdout.lock();
    let mut state = LspState::default();

    while let Some(message) = read_message(&mut reader)? {
        let is_exit = message.get("method").and_then(Value::as_str) == Some("exit");
        for reply in handle_message(&mut state, &message) {
            write_message(&mut writer, &reply)?;
        }
        if is_exit {
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frames_round_trip() {
        let message = json!({ "jsonrpc": "2.0", "id": 1, "method": "initialize" });
        let mut bytes = Vec::new();
        write_message(&mut bytes, &message).unwrap();
        let mut reader = Cursor::new(bytes);
        assert_eq!(read_message(&mut reader).unwrap(), Some(message));
        assert_eq!(read_message(&mut reader).unwrap(), None);
    }

    #[test]
    fn initialize_advertises_completion_and_hover() {
        let mut state = LspState::default();
        let replies = handle_message(
            &mut state,
            &json!({ "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {} }),
        );
        assert_eq!(replies.len(), 1);
        let caps = &replies[0]["result"]["capabilities"];
        assert!(caps["hoverProvider"].as_bool().unwrap());
        let triggers = caps["completionProvider"]["triggerCharacters"]
            .as_array()
            .unwrap();
        assert!(triggers.iter().any(|t| t == "."));
    }

    #[test]
    fn did_open_publishes_diagnostics_for_the_document() {
        let mut state = LspState::default();
        let replies = handle_message(
            &mut state,
            &json!({
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///demo.ts",
                        "languageId": "typescript",
                        "version": 1,
                        "text": "const s = new FixedString(0);"
                    }
                }
            }),
        );
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0]["method"], "textDocument/publishDiagnostics");
        assert_eq!(replies[0]["params"]["uri"], "file:///demo.ts");
        assert_eq!(replies[0]["params"]["diagnostics"][0]["code"], "EUDSL-BOUNDS");
    }

    #[test]
    fn completion_and_hover_use_the_open_document() {
        let mut state = LspState::default();
        let uri = "file:///demo.ts";
        state.open(uri.to_string(), "u.hp".to_string(), Language::Other);

        let replies = handle_message(
            &mut state,
            &json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": { "uri": uri },
                    "position": { "line": 0, "character": 3 }
                }
            }),
        );
        assert!(
            replies[0]["result"]["contents"]["value"]
                .as_str()
                .unwrap()
                .contains("CUnit hit points")
        );

        state.open(
            uri.to_string(),
            "const s = new FixedStr".to_string(),
            Language::Other,
        );
        let replies = handle_message(
            &mut state,
            &json!({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "textDocument/completion",
                "params": {
                    "textDocument": { "uri": uri },
                    "position": { "line": 0, "character": 22 }
                }
            }),
        );
        let items = replies[0]["result"]["items"].as_array().unwrap();
        assert!(items.iter().any(|item| item["label"] == "FixedString"));
    }

    #[test]
    fn shutdown_sets_the_flag() {
        let mut state = LspState::default();
        let replies = handle_message(
            &mut state,
            &json!({ "jsonrpc": "2.0", "id": 9, "method": "shutdown" }),
        );
        assert!(state.shutdown_requested);
        assert_eq!(replies[0]["result"], Value::Null);
    }

    #[test]
    fn did_change_reuses_the_language_stored_at_did_open() {
        // `didChange` carries no `languageId`, so the Python handling
        // must come from the language stored at `didOpen` time.
        let mut state = LspState::default();
        let replies = handle_message(
            &mut state,
            &json!({
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
            }),
        );
        assert!(
            replies[0]["params"]["diagnostics"]
                .as_array()
                .unwrap()
                .is_empty()
        );

        let replies = handle_message(
            &mut state,
            &json!({
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": { "uri": "file:///demo.py", "version": 2 },
                    "contentChanges": [{ "text": "# new FixedString(0)\nFixedString(0)\n" }]
                }
            }),
        );
        let list = replies[0]["params"]["diagnostics"].as_array().unwrap();
        assert_eq!(list.len(), 1, "only the real call is flagged: {list:?}");
    }

    #[test]
    fn did_close_clears_the_stored_language() {
        let mut state = LspState::default();
        let open = |state: &mut LspState, text: &str, language: Option<&str>| {
            let mut doc = json!({
                "uri": "file:///demo.py",
                "version": 1,
                "text": text
            });
            if let Some(language) = language {
                doc["languageId"] = json!(language);
            }
            handle_message(
                state,
                &json!({
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": { "textDocument": doc }
                }),
            )
        };
        let replies = open(&mut state, "# new FixedString(0)\n", Some("python"));
        assert!(
            replies[0]["params"]["diagnostics"]
                .as_array()
                .unwrap()
                .is_empty()
        );

        handle_message(
            &mut state,
            &json!({
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": { "textDocument": { "uri": "file:///demo.py" } }
            }),
        );

        // Reopening without a `languageId` falls back to language-neutral
        // behaviour, where `#` is not a comment and the call is flagged.
        let replies = open(&mut state, "# new FixedString(0)\n", None);
        assert_eq!(
            replies[0]["params"]["diagnostics"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
}
