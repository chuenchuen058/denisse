"""Read-only AST inventory; detection policy lives in the companion JSON file.

Run from any directory with --root, --config and --output overrides as needed.
Findings are review candidates, not a claim that every literal is configurable.
The scanned modules are never imported or executed.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import tokenize


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    kind: str
    value: str
    home: str


@dataclass
class Audit:
    files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fingerprint: str = ""


def load_config(path: Path | None = None) -> dict:
    path = path or Path(__file__).with_suffix(".json")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    # Compile before scanning so malformed policy cannot silently omit findings.
    for rule in config["rules"]:
        config["kinds"][rule["kind"]]
        if rule["selector"] not in {"literal", "call", "comparison"}:
            raise ValueError(f"Unknown rule selector: {rule['selector']}")
        if rule["type"] not in {"string", "number"}:
            raise ValueError(f"Unknown literal type: {rule['type']}")
        if rule["selector"] == "call":
            rule["call_pattern"], rule["positions"], rule["keywords"]
        if rule["selector"] == "comparison":
            rule["operators"]
        for key in ("value_pattern", "context_pattern", "call_pattern"):
            if key in rule:
                re.compile(rule[key])
    return config


def discover(root: Path, policy: dict):
    """Prune dependencies, private state, fixtures and other worktrees early."""
    def matches(value, patterns):
        return any(fnmatchcase(value, pattern) for pattern in patterns)

    def linked(path):
        return path.is_symlink() or path.is_junction()

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs
                         if not matches(name, policy["exclude_dirs"])
                         and not linked(base / name))
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if (matches(name, policy["include_files"])
                    and not matches(name, policy["exclude_files"])
                    and not matches(relative, policy["exclude_paths"])
                    and not linked(path)):
                yield path


class Scanner:
    """Structural matching plus conservative, lexical name resolution.

    Bindings are a may-reference set, not execution-order or taint analysis.
    Following local aliases catches named patterns and keyword collections;
    never following imports keeps the audit independent of live services.
    """

    def __init__(self, source: str, path: str, tree: ast.AST, config: dict):
        self.source, self.path, self.config = source, path, config
        self.nodes = list(ast.walk(tree))
        self.parents = {child: node for node in self.nodes for child in ast.iter_child_nodes(node)}
        self.bindings = defaultdict(lambda: defaultdict(list))
        self.imports = defaultdict(dict)
        self.docstrings = set()
        self.found = set()
        for node in self.nodes:
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    self.docstrings.add(node.body[0].value)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[self.scope(node)][alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.imports[self.scope(node)][alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self.bind(target, node.value)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                self.bind(node.target, node.value)
        # Resolve iterator destructuring after collecting assignments, including
        # dictionaries declared after a function that refers to them.
        for node in self.nodes:
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self.bind_iteration(node.target, node.iter)

    def scope(self, node):
        parent = self.parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp,
                                   ast.DictComp, ast.GeneratorExp)):
                return parent
            parent = self.parents.get(parent)
        return None

    def bind(self, target, value):
        if isinstance(target, ast.Name):
            self.bindings[self.scope(target)][target.id].append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
            for index, element in enumerate(target.elts):
                self.bind(element, values[index] if index < len(values) else value)

    def resolved(self, node, seen=None):
        seen = set() if seen is None else seen
        if node in seen:
            return
        seen.add(node)
        if isinstance(node, ast.Name):
            for value in self.lookup(node, self.bindings) or []:
                yield from self.resolved(value, seen)
        else:
            yield node

    def bind_iteration(self, target, expression):
        bound = False
        for value in self.resolved(expression):
            rows = []
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                rows = value.elts
            elif isinstance(value, ast.Dict):
                rows = [key for key in value.keys if key is not None]
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                projection = self.config["mapping_iterators"].get(value.func.attr)
                for mapping in self.resolved(value.func.value):
                    if isinstance(mapping, ast.Dict) and projection:
                        for key, item in zip(mapping.keys, mapping.values):
                            if key is not None:
                                pair = {"key": key, "value": item}
                                parts = [pair[part] for part in projection]
                                rows.append(parts[0] if len(parts) == 1 else ast.Tuple(elts=parts))
            for row in rows:
                self.bind(target, row)
                bound = True
        if not bound:
            self.bind(target, expression)

    def lookup(self, node, table):
        scope = self.scope(node)
        while scope is not None:
            if node.id in table[scope]:
                return table[scope][node.id]
            scope = self.scope(scope)
        return None

    def name(self, node):
        if isinstance(node, ast.Name):
            return self.lookup(node, self.imports) or node.id
        if isinstance(node, ast.Attribute):
            return f"{self.name(node.value)}.{node.attr}"
        return ""

    def origins(self, node, seen=None):
        seen = set() if seen is None else seen
        if node in seen:
            return
        seen.add(node)
        if isinstance(node, ast.Constant):
            if node not in self.docstrings:
                yield node
        elif isinstance(node, ast.Name):
            for value in self.lookup(node, self.bindings) or []:
                yield from self.origins(value, seen)
        elif not isinstance(node, (ast.Attribute, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                yield from self.origins(child, seen)

    def context(self, node):
        """Nearby target/key/call names without unrelated sibling values."""
        words = []
        child = node
        parent = self.parents.get(child)
        while parent is not None:
            if isinstance(parent, ast.keyword):
                words.append(parent.arg or "")
            elif isinstance(parent, ast.Call):
                words.append(self.name(parent.func))
            elif isinstance(parent, ast.Dict):
                for key, value in zip(parent.keys, parent.values):
                    if value is child and isinstance(key, ast.Constant):
                        words.append(str(key.value))
            elif isinstance(parent, ast.Assign):
                words.extend(ast.unparse(target) for target in parent.targets)
            elif isinstance(parent, (ast.AnnAssign, ast.NamedExpr)):
                words.append(ast.unparse(parent.target))
            elif isinstance(parent, ast.arguments):
                positional = parent.posonlyargs + parent.args
                defaults = positional[len(positional) - len(parent.defaults):]
                for arg, value in list(zip(defaults, parent.defaults)) + list(zip(parent.kwonlyargs, parent.kw_defaults)):
                    if value is child:
                        words.append(arg.arg)
            if isinstance(parent, ast.stmt):
                break
            child, parent = parent, self.parents.get(parent)
        return " ".join(words)

    def add(self, node, rule):
        if node in self.docstrings:
            return
        value = node.value
        if rule["type"] == "string" and not isinstance(value, str):
            return
        if rule["type"] == "number" and (isinstance(value, bool) or not isinstance(value, (int, float, complex))):
            return
        if "value_pattern" in rule and not re.search(rule["value_pattern"], str(value)):
            return
        if "context_pattern" in rule and not re.search(rule["context_pattern"], self.context(node)):
            return
        display_node = node
        parent = self.parents.get(node)
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, (ast.USub, ast.UAdd)):
            display_node = parent
        # Use decoded literal values (including f-string fragments); repr keeps
        # multiline values on one table row without shortening evidence.
        display = ast.unparse(display_node) if display_node is not node else repr(value)
        kind = rule["kind"]
        self.found.add(Finding(self.path, node.lineno, node.col_offset, kind,
                               display, self.config["kinds"][kind]["home"]))

    def run(self):
        for rule in self.config["rules"]:
            for node in self.nodes:
                selector = rule["selector"]
                expressions = []
                if selector == "literal" and isinstance(node, ast.Constant):
                    expressions = [node]
                elif selector == "call" and isinstance(node, ast.Call):
                    if re.search(rule["call_pattern"], self.name(node.func)):
                        expressions = [node.args[i] for i in rule["positions"] if i < len(node.args)]
                        expressions += [kw.value for kw in node.keywords if kw.arg in rule["keywords"]]
                elif selector == "comparison" and isinstance(node, ast.Compare):
                    if any(type(op).__name__ in rule["operators"] for op in node.ops):
                        expressions = [node.left, *node.comparators]
                for expression in expressions:
                    for literal in self.origins(expression):
                        self.add(literal, rule)
        # Keep the most specific numeric classification at a source location.
        specific = {(f.line, f.column) for f in self.found
                    if f.kind in self.config["suppress_fallback_when"]}
        return [f for f in self.found
                if f.kind != self.config["fallback_kind"] or (f.line, f.column) not in specific]


def audit(root: Path, config: dict) -> Audit:
    root = root.resolve()
    result = Audit()
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    if not root.is_dir():
        result.errors.append("Scan root is not a directory.")
        return result
    try:
        paths = list(discover(root, config["scan"]))
    except OSError as error:
        result.errors.append(str(error))
        return result
    for path in paths:
        relative = path.relative_to(root).as_posix()
        result.files.append(relative)
        try:
            # Respect Python encoding cookies and UTF-8 BOMs.
            with tokenize.open(path) as source_file:
                source = source_file.read()
            digest.update(relative.encode())
            digest.update(source.encode())
            tree = ast.parse(source, filename=relative)
            result.findings.extend(Scanner(source, relative, tree, config).run())
        except (OSError, SyntaxError, UnicodeError, LookupError) as error:
            result.errors.append(f"{relative}: {error}")
    if not result.files:
        result.errors.append("No Python source files selected.")
    result.findings.sort(key=lambda f: (f.path, f.line, f.column, f.kind))
    result.fingerprint = digest.hexdigest()
    return result


def file_rank(path: str, config: dict):
    for group in config["ranking"]:
        if group["top_level_only"] and len(Path(path).parts) != 1:
            continue
        if any(fnmatchcase(path, pattern) for pattern in group["paths"]):
            return group["rank"], group["label"]
    raise ValueError(f"No ranking rule for {path}")


def cell(value):
    return escape(str(value)).replace("|", "&#124;").replace("\r", "\\r").replace("\n", "\\n")


def render(result: Audit, config: dict) -> str:
    grouped = defaultdict(list)
    for finding in result.findings:
        grouped[finding.path].append(finding)
    lines = ["# Hardcoding audit", "", *config["report"]["notes"], "",
             f"Scanned **{len(result.files)}** Python files; **{len(result.findings)}** candidates; "
             f"**{len(result.errors)}** scan errors.", "",
             f"Source and policy SHA-256: `{result.fingerprint}`.", "",
             "## Coverage and regeneration", "", *config["report"]["usage"], "",
             "Traversal policy (from the JSON configuration):", "", "```json",
             json.dumps(config["scan"], indent=2), "```", "",
             "## Candidate totals", "", "| Kind | Count | Suggested home |",
             "| --- | ---: | --- |"]
    counts = Counter(f.kind for f in result.findings)
    for kind, metadata in config["kinds"].items():
        lines.append(f"| {cell(kind)} | {counts[kind]} | {cell(metadata['home'])} |")
    if result.errors:
        lines += ["", "## INCOMPLETE: scan errors", ""]
        lines.extend(f"- {cell(error)}" for error in result.errors)
    ordered = sorted(result.files, key=lambda path: (file_rank(path, config)[0],
                                                    -len(grouped[path]), path))
    lines += ["", "## Files in review order", "",
              "| File | User impact | Candidates |", "| --- | --- | ---: |"]
    for path in ordered:
        rank, label = file_rank(path, config)
        lines.append(f"| <code>{cell(path)}</code> | {rank}: {cell(label)} | {len(grouped[path])} |")
    for path in ordered:
        rank, label = file_rank(path, config)
        lines += ["", f"## {cell(path)}", "", f"Priority {rank}: {label}.", "",
                  "| Line | Kind | Value | Suggested home |", "| ---: | --- | --- | --- |"]
        if not grouped[path]:
            lines.append("| - | No candidates | - | - |")
        for finding in grouped[path]:
            lines.append(f"| {finding.line} | {cell(finding.kind)} | <code>{cell(finding.value)}</code> | {cell(finding.home)} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, help="Markdown destination, relative to --root unless absolute")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = audit(args.root, config)
        destination = args.root / (args.output or config["report"]["output"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render(result, config), encoding="utf-8")
        print(f"{len(result.files)} files, {len(result.findings)} candidates, "
              f"{len(result.errors)} errors -> {destination}")
        for error in result.errors:
            print(error, file=sys.stderr)
        return int(bool(result.errors))
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        print(f"Audit failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
