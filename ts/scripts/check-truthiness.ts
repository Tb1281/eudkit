/**
 * Opt-in static truthiness checker for the TypeScript DSL surface.
 *
 * `tsc --noEmit` catches `if (v == 1)` as TS2367, but it cannot see the two
 * other silent outcomes: a runtime condition used as a JS boolean
 * (`if (v.eq(1))`, `while (u.hp)`) and a runtime field compared against
 * `initialValue` (`if (u.hp == u.hp.initialValue)`). This file walks the
 * TypeScript AST (no type checker, no new dependency) and reports those two
 * shapes as `EUDSL-TS-TRUTHIFY`.
 *
 * The real build path erases types with `node --experimental-strip-types`, so
 * this rule is deliberately opt-in: it guards `npm run typecheck`, editors,
 * and future CI, never `eudkit synth`.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

export interface TruthinessDiagnostic {
  readonly file: string;
  readonly line: number; // 1-based
  readonly column: number; // 1-based
  readonly code: "EUDSL-TS-TRUTHIFY";
  readonly message: string;
}

const MESSAGE =
  "runtime condition/value used as a JS boolean; use when([cond], body) / whileLoop([cond], body)";

const CONDITION_CALL_METHODS = new Set([
  "eq",
  "ne",
  "ge",
  "le",
  "gt",
  "lt",
  "atLeast",
  "atMost",
  "exactly",
]);

const CONDITION_CALL_FUNCTIONS = new Set([
  "Always",
  "Never",
  "Deaths",
  "DeathsX",
  "Memory",
  "MemoryX",
  "MemCond",
  "condAll",
  "cond_all",
  "condAny",
  "cond_any",
  "condNot",
  "cond_not",
]);

const VIEW_FACTORIES = new Set(["CUnit", "CSprite", "MemView"]);

const EXCLUDED_FIELD_NAMES = new Set(["length", "size"]);

const COMPARISON_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.EqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken,
  ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsEqualsToken,
  ts.SyntaxKind.LessThanToken,
  ts.SyntaxKind.LessThanEqualsToken,
  ts.SyntaxKind.GreaterThanToken,
  ts.SyntaxKind.GreaterThanEqualsToken,
]);

const IGNORE_MARKER = "eudsl-truthiness: ignore";

/** Remove parentheses, `as`/angle-bracket assertions, and `!` non-null wrappers. */
function unwrapExpression(expression: ts.Expression): ts.Expression {
  let current = expression;
  for (;;) {
    if (ts.isParenthesizedExpression(current)) {
      current = current.expression;
    } else if (ts.isAsExpression(current)) {
      current = current.expression;
    } else if (ts.isTypeAssertionExpression(current)) {
      current = current.expression;
    } else if (ts.isNonNullExpression(current)) {
      current = current.expression;
    } else {
      return current;
    }
  }
}

function isViewFactoryCall(expression: ts.Expression): boolean {
  const call = unwrapExpression(expression);
  if (!ts.isCallExpression(call)) {
    return false;
  }
  const callee = unwrapExpression(call.expression);
  if (ts.isIdentifier(callee)) {
    return VIEW_FACTORIES.has(callee.text);
  }
  return ts.isPropertyAccessExpression(callee) && callee.name.text === "view";
}

/** Collect `const u = CUnit(...)` / `const u = cp.view(CUnit)` handles per file. */
function collectViewVariables(sourceFile: ts.SourceFile): ReadonlySet<string> {
  const names = new Set<string>();
  const visit = (node: ts.Node): void => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer !== undefined &&
      isViewFactoryCall(node.initializer)
    ) {
      names.add(node.name.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return names;
}

function isInitialValueAccess(expression: ts.Expression): boolean {
  const access = unwrapExpression(expression);
  return (
    ts.isPropertyAccessExpression(access) && access.name.text === "initialValue"
  );
}

function isViewFieldRead(
  expression: ts.Expression,
  viewVariables: ReadonlySet<string>,
): boolean {
  const access = unwrapExpression(expression);
  if (!ts.isPropertyAccessExpression(access)) {
    return false;
  }
  if (EXCLUDED_FIELD_NAMES.has(access.name.text)) {
    return false;
  }
  const receiver = unwrapExpression(access.expression);
  if (isViewFactoryCall(receiver)) {
    return true;
  }
  return ts.isIdentifier(receiver) && viewVariables.has(receiver.text);
}

function isConditionShapedCall(expression: ts.Expression): boolean {
  const call = unwrapExpression(expression);
  if (!ts.isCallExpression(call)) {
    return false;
  }
  const callee = unwrapExpression(call.expression);
  if (ts.isPropertyAccessExpression(callee)) {
    return CONDITION_CALL_METHODS.has(callee.name.text);
  }
  return ts.isIdentifier(callee) && CONDITION_CALL_FUNCTIONS.has(callee.text);
}

function isRuntimeComparison(
  expression: ts.Expression,
  viewVariables: ReadonlySet<string>,
): boolean {
  const binary = unwrapExpression(expression);
  if (
    !ts.isBinaryExpression(binary) ||
    !COMPARISON_OPERATORS.has(binary.operatorToken.kind)
  ) {
    return false;
  }
  const left = binary.left;
  const right = binary.right;
  return (
    isInitialValueAccess(left) ||
    isInitialValueAccess(right) ||
    isViewFieldRead(left, viewVariables) ||
    isViewFieldRead(right, viewVariables)
  );
}

/** True when the expression is one of the three offending test shapes. */
function isOffendingExpression(
  expression: ts.Expression,
  viewVariables: ReadonlySet<string>,
): boolean {
  return (
    isConditionShapedCall(expression) ||
    isRuntimeComparison(expression, viewVariables) ||
    isViewFieldRead(expression, viewVariables)
  );
}

/** Parse one source text and return diagnostics. Pure; no type checker. */
export function checkSource(
  fileName: string,
  source: string,
): TruthinessDiagnostic[] {
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
  );
  const viewVariables = collectViewVariables(sourceFile);
  const diagnostics: TruthinessDiagnostic[] = [];

  const lines = source.split(/\r\n|\r|\n/);
  const markerLines = new Set<number>();
  const standaloneMarkerLines = new Set<number>();
  for (let line = 0; line < lines.length; line++) {
    const text = lines[line];
    if (text.includes(IGNORE_MARKER)) {
      markerLines.add(line);
      const trimmed = text.trimStart();
      if (trimmed.startsWith("//") || trimmed.startsWith("/*")) {
        standaloneMarkerLines.add(line);
      }
    }
  }

  const isIgnored = (node: ts.Node): boolean => {
    const line = sourceFile.getLineAndCharacterOfPosition(
      node.getStart(sourceFile),
    ).line;
    return markerLines.has(line) || standaloneMarkerLines.has(line - 1);
  };

  const report = (node: ts.Node): void => {
    const start = sourceFile.getLineAndCharacterOfPosition(
      node.getStart(sourceFile),
    );
    diagnostics.push({
      file: fileName,
      line: start.line + 1,
      column: start.character + 1,
      code: "EUDSL-TS-TRUTHIFY",
      message: MESSAGE,
    });
  };

  /**
   * Check one test expression. Returns true when the expression was either
   * reported or explicitly ignored, so the caller does not walk into it again.
   */
  const checkTest = (
    expression: ts.Expression,
    statement: ts.Node,
  ): boolean => {
    if (isIgnored(expression) || isIgnored(statement)) {
      return true;
    }
    if (isOffendingExpression(expression, viewVariables)) {
      report(expression);
      return true;
    }
    return false;
  };

  const visit = (node: ts.Node): void => {
    if (ts.isIfStatement(node)) {
      if (!checkTest(node.expression, node)) {
        visit(node.expression);
      }
      visit(node.thenStatement);
      if (node.elseStatement !== undefined) {
        visit(node.elseStatement);
      }
      return;
    }

    if (ts.isWhileStatement(node) || ts.isDoStatement(node)) {
      if (!checkTest(node.expression, node)) {
        visit(node.expression);
      }
      visit(node.statement);
      return;
    }

    if (ts.isConditionalExpression(node)) {
      if (!checkTest(node.condition, node)) {
        visit(node.condition);
      }
      visit(node.whenTrue);
      visit(node.whenFalse);
      return;
    }

    if (
      ts.isPrefixUnaryExpression(node) &&
      node.operator === ts.SyntaxKind.ExclamationToken
    ) {
      if (isIgnored(node)) {
        return;
      }
      if (isOffendingExpression(node.operand, viewVariables)) {
        report(node);
        return;
      }
      visit(node.operand);
      return;
    }

    ts.forEachChild(node, visit);
  };

  visit(sourceFile);
  return diagnostics;
}

function formatDiagnostic(diagnostic: ts.Diagnostic): string {
  return ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
}

function loadProjectFileNames(configPath?: string): string[] {
  const resolved =
    configPath === undefined
      ? path.resolve(import.meta.dirname, "..", "tsconfig.json")
      : path.resolve(configPath);
  const configFile = ts.readConfigFile(resolved, ts.sys.readFile);
  if (configFile.error !== undefined) {
    throw new Error(formatDiagnostic(configFile.error));
  }
  const parsed = ts.parseJsonConfigFileContent(
    configFile.config,
    ts.sys,
    path.dirname(resolved),
    undefined,
    resolved,
  );
  if (parsed.errors.length > 0) {
    throw new Error(parsed.errors.map(formatDiagnostic).join("\n"));
  }
  return parsed.fileNames.filter((fileName) => !isSkipped(fileName));
}

function isSkipped(fileName: string): boolean {
  const normalized = fileName.replaceAll("\\", "/");
  if (
    normalized.includes("/node_modules/") ||
    normalized.includes("/dist/") ||
    normalized.includes("/tests/fixtures/")
  ) {
    return true;
  }
  return /\.d\.[cm]?ts$/.test(normalized);
}

function collectDiagnostics(
  fileNames: readonly string[],
): TruthinessDiagnostic[] {
  const diagnostics: TruthinessDiagnostic[] = [];
  for (const fileName of fileNames) {
    const source = ts.sys.readFile(fileName);
    if (source !== undefined) {
      diagnostics.push(...checkSource(fileName, source));
    }
  }
  return diagnostics;
}

/**
 * Load a tsconfig (default `tsconfig.json` in the `ts/` package root), walk
 * every non-declaration source file, and return diagnostics. Skips paths
 * containing `/node_modules/`, `/dist/`, and `/tests/fixtures/`.
 */
export function checkProject(configPath?: string): TruthinessDiagnostic[] {
  return collectDiagnostics(loadProjectFileNames(configPath));
}

function parseArgs(args: string[]): { project?: string } {
  let project: string | undefined;
  for (let index = 0; index < args.length; index++) {
    const argument = args[index];
    if (argument === "--project") {
      project = args[++index];
      if (project === undefined) {
        throw new Error("--project requires a tsconfig path");
      }
    } else if (argument.startsWith("--project=")) {
      project = argument.slice("--project=".length);
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
  }
  return { project };
}

function main(): void {
  try {
    const { project } = parseArgs(process.argv.slice(2));
    const fileNames = loadProjectFileNames(project);
    const diagnostics = collectDiagnostics(fileNames);
    if (diagnostics.length > 0) {
      for (const diagnostic of diagnostics) {
        process.stdout.write(
          `${diagnostic.file}:${diagnostic.line}:${diagnostic.column} ${diagnostic.code} ${diagnostic.message}\n`,
        );
      }
      process.exitCode = 1;
      return;
    }
    process.stdout.write(`truthiness: clean (${fileNames.length} files)\n`);
    process.exitCode = 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`truthiness: ${message}\n`);
    process.exitCode = 2;
  }
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (invokedDirectly) {
  main();
}
