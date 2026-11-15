//! A small tokenizer for the DSL source text the diagnostics scan.
//!
//! The diagnostics never parse the program: they only need to find call and
//! Trigger-capacity shapes while ignoring text that is not code. Treating
//! string literals as single tokens and skipping comments is what makes that
//! sound: a comma, bracket, or brace inside a string literal or a comment
//! must not change an argument index, an item count, or a bracket match.
//!
//! Comment and string handling is language-aware (see [`Language`]):
//! Python documents additionally skip `#` line comments and treat
//! `"""`/`'''` as multi-line string delimiters, while `//` and `/* */`
//! stop being comments there (in Python they are operators). Every other
//! language tokenizes exactly as before, so TypeScript `#private` fields
//! are never hidden by a comment skip.
//!
//! The tokenizer is deliberately not a parser: it knows identifiers, numbers,
//! string literals, and single-character punctuation, and nothing about
//! grammar, types, or bindings. Receiver selection in completions stays a
//! documented heuristic (see the crate root docs); binding tracking would
//! need a typechecker, which this module does not provide.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenKind {
    /// An identifier (`[A-Za-z0-9_]+`, Unicode-alphanumeric aware).
    Ident,
    /// A numeric literal (`0x1F`, `1_000`, `70000`).
    Number,
    /// A string literal, quotes included in the span but not in `text`.
    Str,
    /// Any other single character (`(`, `)`, `[`, `]`, `{`, `}`, `,`, ...).
    Punct(char),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Token {
    pub kind: TokenKind,
    /// The token text as written, except string literals, whose text is the
    /// unquoted content (escapes left as written).
    pub text: String,
    /// Byte offset of the first byte of the token (the opening quote for
    /// string literals).
    pub start: usize,
    /// Byte offset one past the last byte of the token (the closing quote
    /// for string literals).
    pub end: usize,
}

impl Token {
    pub fn is_punct(&self, ch: char) -> bool {
        self.kind == TokenKind::Punct(ch)
    }

    pub fn is_ident(&self, name: &str) -> bool {
        self.kind == TokenKind::Ident && self.text == name
    }
}

/// The document language, as reported by the editor via `languageId`.
///
/// Only Python changes tokenization: `#` starts a line comment and
/// `"""`/`'''` delimit multi-line strings. Every other language — including
/// TypeScript, where `#` starts a private field name — tokenizes exactly as
/// before, so a blanket `#` skip can never hide TS code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Language {
    Python,
    #[default]
    Other,
}

impl Language {
    pub fn from_language_id(id: &str) -> Self {
        if id == "python" {
            Language::Python
        } else {
            Language::Other
        }
    }

    pub fn is_python(self) -> bool {
        matches!(self, Language::Python)
    }
}

/// Tokenize `text`, skipping whitespace and comments.
///
/// An unterminated single- or double-quoted string ends at the next newline
/// (or end of input); an unterminated backtick string or block comment ends
/// at end of input. Either way the remainder is consumed as one token or
/// skipped, so a broken literal can never make later code look like it is
/// inside a string.
pub fn tokenize(text: &str) -> Vec<Token> {
    tokenize_with_language(text, Language::Other)
}

/// Tokenize `text` for the given document language.
///
/// `Language::Other` behaves exactly like [`tokenize`]; `Language::Python`
/// additionally skips `#` line comments and treats `"""`/`'''` as
/// multi-line string delimiters, while `//` and `/* */` stop being comments
/// (in Python they are operators, not comment syntax).
pub fn tokenize_with_language(text: &str, language: Language) -> Vec<Token> {
    let bytes = text.as_bytes();
    let python = language.is_python();
    let mut tokens = Vec::new();
    let mut i = 0usize;
    while i < bytes.len() {
        let ch = text[i..].chars().next().unwrap();
        if ch.is_whitespace() {
            i += ch.len_utf8();
            continue;
        }
        // Python line comment: skip to (not past) the newline. This branch
        // is Python-only on purpose: in TypeScript `#` starts a private
        // field name, so skipping it there would hide real code.
        if python && ch == '#' {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        // Line comment: skip to (not past) the newline. Not a comment in
        // Python, where `//` is floor division: a call after it on the
        // same line is still code.
        if !python && ch == '/' && bytes.get(i + 1) == Some(&b'/') {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        // Block comment: skip to past `*/`, or end of input if unterminated.
        // Likewise not a comment in Python.
        if !python && ch == '/' && bytes.get(i + 1) == Some(&b'*') {
            i += 2;
            while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                i += 1;
            }
            i = (i + 2).min(bytes.len());
            continue;
        }
        // Backtick strings exist only outside Python; in Python a backtick
        // falls through to punctuation below.
        if ch == '\'' || ch == '"' || (ch == '`' && !python) {
            // Python triple-quoted string: the delimiter is three quotes,
            // newlines are allowed inside, and a lone quote (or two) inside
            // never ends it early — only the matching triple does.
            if python
                && (ch == '\'' || ch == '"')
                && bytes.get(i + 1) == Some(&(ch as u8))
                && bytes.get(i + 2) == Some(&(ch as u8))
            {
                let quote = ch;
                let quote_byte = ch as u8;
                let start = i;
                i += 3;
                let mut content_end = i;
                let mut closed = false;
                while i < bytes.len() {
                    let c = text[i..].chars().next().unwrap();
                    if c == '\\' {
                        // Skip the escaped character, if any.
                        i += 1;
                        if i < bytes.len() {
                            i += text[i..].chars().next().unwrap().len_utf8();
                        }
                        continue;
                    }
                    if c == quote
                        && bytes.get(i + 1) == Some(&quote_byte)
                        && bytes.get(i + 2) == Some(&quote_byte)
                    {
                        content_end = i;
                        i += 3;
                        closed = true;
                        break;
                    }
                    i += c.len_utf8();
                    content_end = i;
                }
                // An unterminated triple-quoted string consumes the rest
                // of the input, so later text can never leak out as code.
                if !closed && i >= bytes.len() {
                    content_end = bytes.len();
                }
                tokens.push(Token {
                    kind: TokenKind::Str,
                    text: text[start + 3..content_end].to_string(),
                    start,
                    end: i,
                });
                continue;
            }
            let quote = ch;
            let start = i;
            i += 1;
            let mut content_end = i;
            let mut closed = false;
            while i < bytes.len() {
                let c = text[i..].chars().next().unwrap();
                if c == '\\' {
                    // Skip the escaped character, if any.
                    i += 1;
                    if i < bytes.len() {
                        i += text[i..].chars().next().unwrap().len_utf8();
                    }
                    continue;
                }
                if c == quote {
                    content_end = i;
                    i += 1;
                    closed = true;
                    break;
                }
                if quote != '`' && c == '\n' {
                    content_end = i;
                    break;
                }
                i += c.len_utf8();
                content_end = i;
            }
            // An unterminated literal at end of input keeps the content
            // consumed so far; a literal closed exactly at end of input
            // keeps the closing quote out of the content.
            if !closed && i >= bytes.len() {
                content_end = bytes.len();
            }
            tokens.push(Token {
                kind: TokenKind::Str,
                text: text[start + 1..content_end].to_string(),
                start,
                end: i,
            });
            continue;
        }
        if ch.is_alphanumeric() || ch == '_' {
            let start = i;
            let is_number = ch.is_ascii_digit();
            while i < bytes.len() {
                let c = text[i..].chars().next().unwrap();
                if c.is_alphanumeric() || c == '_' {
                    i += c.len_utf8();
                } else {
                    break;
                }
            }
            tokens.push(Token {
                kind: if is_number {
                    TokenKind::Number
                } else {
                    TokenKind::Ident
                },
                text: text[start..i].to_string(),
                start,
                end: i,
            });
            continue;
        }
        tokens.push(Token {
            kind: TokenKind::Punct(ch),
            text: ch.to_string(),
            start: i,
            end: i + ch.len_utf8(),
        });
        i += ch.len_utf8();
    }
    tokens
}

/// Index of the token closing the bracket opened at `open`, or `None`.
///
/// Only punctuation tokens participate, so brackets inside string literals
/// (already single `Str` tokens) can never close a bracket early.
pub fn matching(tokens: &[Token], open: usize, open_ch: char, close_ch: char) -> Option<usize> {
    let mut depth = 0i32;
    for (idx, tok) in tokens.iter().enumerate().skip(open) {
        if tok.is_punct(open_ch) {
            depth += 1;
        } else if tok.is_punct(close_ch) {
            depth -= 1;
            if depth == 0 {
                return Some(idx);
            }
        }
    }
    None
}

/// Split the token range `tokens[start..end]` at top-level commas.
///
/// A segment is the source slice between separators (or the range ends),
/// trimmed. An empty range yields no segments; a trailing comma yields no
/// trailing empty segment, matching the old `split_top_level` behaviour for
/// the shapes the diagnostics check.
pub fn split_top_level(text: &str, tokens: &[Token], start: usize, end: usize) -> Vec<String> {
    if start >= end {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut seg_start = tokens[start].start;
    for idx in start..end {
        let tok = &tokens[idx];
        match tok.kind {
            TokenKind::Punct('(') | TokenKind::Punct('[') | TokenKind::Punct('{') => depth += 1,
            TokenKind::Punct(')') | TokenKind::Punct(']') | TokenKind::Punct('}') => depth -= 1,
            TokenKind::Punct(',') if depth == 0 => {
                out.push(text[seg_start..tok.start].trim().to_string());
                seg_start = tok.end;
            }
            _ => {}
        }
    }
    let tail = text[seg_start..tokens[end - 1].end].trim();
    if !tail.is_empty() {
        out.push(tail.to_string());
    }
    out
}

/// Count the top-level comma-separated items in `tokens[start..end]`.
///
/// A segment containing at least one token counts as one item; an empty
/// range counts as zero. String literals are single tokens, so a comma or
/// bracket inside one never changes the count.
pub fn count_top_level_items(tokens: &[Token], start: usize, end: usize) -> usize {
    if start >= end {
        return 0;
    }
    let mut depth = 0i32;
    let mut count = 0usize;
    let mut has_item = false;
    for tok in &tokens[start..end] {
        match tok.kind {
            TokenKind::Punct('(') | TokenKind::Punct('[') | TokenKind::Punct('{') => {
                depth += 1;
                has_item = true;
            }
            TokenKind::Punct(')') | TokenKind::Punct(']') | TokenKind::Punct('}') => depth -= 1,
            TokenKind::Punct(',') if depth == 0 => {
                if has_item {
                    count += 1;
                    has_item = false;
                }
            }
            _ => has_item = true,
        }
    }
    if has_item {
        count += 1;
    }
    count
}
