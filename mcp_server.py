#!/usr/bin/env python3
"""MCP server for code-intel: LSP-backed symbol location, callers, callees, and implementations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lsp_manager import (
    EXT_TO_SPEC,
    LANGUAGE_SPECS,
    SKIP_DIRS,
    LspManager,
    LspProtocolError,
    LspSession,
    LspUnavailableError,
    path_to_uri,
    py_col_to_utf16,
    uri_to_path,
    utf16_to_py_col,
)

SYMBOL_KINDS: dict[int, str] = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}

MANAGER = LspManager()


def _str_arg(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    val = args.get(key)
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _int_arg(
    args: dict[str, Any],
    key: str,
    default: int | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int | None:
    raw = args.get(key)
    if raw is None:
        val = default
    else:
        val = int(raw)
    if val is None:
        return None
    if min_val is not None and val < min_val:
        raise ValueError(f"Parameter {key!r} must be >= {min_val}, got {val}")
    if max_val is not None and val > max_val:
        val = max_val
    return val


def _bool_arg(args: dict[str, Any], key: str, default: bool) -> bool:
    raw = args.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off")
    return bool(raw)


def _read_lines(file_path: Path, cache: dict[Path, list[str]]) -> list[str]:
    resolved = file_path.resolve()
    if resolved not in cache:
        try:
            cache[resolved] = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[resolved] = []
    return cache[resolved]


def _line_snippet(file_path: Path, line0: int, cache: dict[Path, list[str]], max_chars: int = 160) -> str:
    lines = _read_lines(file_path, cache)
    if 0 <= line0 < len(lines):
        text = lines[line0].strip()
        if len(text) > max_chars:
            return text[: max_chars - 3] + "..."
        return text
    return ""


def _display_path(file_path: Path, workspace_root: Path) -> str:
    resolved = file_path.resolve()
    ws = workspace_root.resolve()
    try:
        rel = resolved.relative_to(ws)
        return str(rel)
    except ValueError:
        return str(resolved)


def _is_within_workspace(file_path: Path, workspace_root: Path) -> bool:
    try:
        file_path.resolve().relative_to(workspace_root.resolve())
        return True
    except ValueError:
        return False


@dataclass
class FlatSymbol:
    name: str
    qualified_name: str
    kind: int
    file_path: Path
    range_start_line: int
    range_end_line: int
    sel_line: int
    sel_char: int


def _clean_symbol_name(raw_name: str) -> str:
    name = raw_name.strip()
    paren = name.find("(")
    if paren > 0:
        return name[:paren].strip()
    return name


def _fetch_document_symbols(
    session: LspSession,
    file_path: Path,
    sym_cache: dict[Path, list[FlatSymbol]],
) -> list[FlatSymbol]:
    resolved = file_path.resolve()
    if resolved in sym_cache:
        return sym_cache[resolved]
    if not resolved.is_file():
        sym_cache[resolved] = []
        return []

    try:
        session.ensure_file_synced(resolved)
        raw = session.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path_to_uri(resolved)}},
            timeout=15.0,
        )
    except Exception:
        sym_cache[resolved] = []
        return []

    out: list[FlatSymbol] = []

    def visit_doc_sym(node: dict[str, Any], prefix: str) -> None:
        raw_name = str(node.get("name") or "").strip()
        if not raw_name:
            return
        qname = f"{prefix}.{raw_name}" if prefix else raw_name
        rng = node.get("range") or {}
        sel = node.get("selectionRange") or rng
        r_start = (rng.get("start") or {}).get("line", 0)
        r_end = (rng.get("end") or {}).get("line", r_start)
        s_start = sel.get("start") or {}
        out.append(
            FlatSymbol(
                name=raw_name,
                qualified_name=qname,
                kind=int(node.get("kind") or 0),
                file_path=resolved,
                range_start_line=int(r_start),
                range_end_line=int(r_end),
                sel_line=int(s_start.get("line", r_start)),
                sel_char=int(s_start.get("character", 0)),
            )
        )
        for child in node.get("children") or []:
            if isinstance(child, dict):
                visit_doc_sym(child, qname)

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "location" in item and isinstance(item["location"], dict):
                loc = item["location"]
                rng = loc.get("range") or {}
                r_start = (rng.get("start") or {}).get("line", 0)
                r_end = (rng.get("end") or {}).get("line", r_start)
                s_char = (rng.get("start") or {}).get("character", 0)
                raw_name = str(item.get("name") or "").strip()
                container = str(item.get("containerName") or "").strip()
                qname = f"{container}.{raw_name}" if container else raw_name
                out.append(
                    FlatSymbol(
                        name=raw_name,
                        qualified_name=qname,
                        kind=int(item.get("kind") or 0),
                        file_path=uri_to_path(str(loc.get("uri") or path_to_uri(resolved))),
                        range_start_line=int(r_start),
                        range_end_line=int(r_end),
                        sel_line=int(r_start),
                        sel_char=int(s_char),
                    )
                )
            else:
                visit_doc_sym(item, "")

    sym_cache[resolved] = out
    return out


def _enclosing_symbol_for_line(
    session: LspSession,
    file_path: Path,
    line0: int,
    sym_cache: dict[Path, list[FlatSymbol]],
) -> FlatSymbol | None:
    symbols = _fetch_document_symbols(session, file_path, sym_cache)
    best: FlatSymbol | None = None
    best_span: int | None = None
    for sym in symbols:
        if sym.range_start_line <= line0 <= sym.range_end_line:
            span = sym.range_end_line - sym.range_start_line
            # Prefer callable/type scopes over local variable symbols when spans tie.
            is_callable_or_type = sym.kind in (5, 6, 9, 11, 12, 23)
            if best is None or best_span is None:
                best = sym
                best_span = span
            elif span < best_span and (is_callable_or_type or best.kind not in (6, 9, 12)):
                best = sym
                best_span = span
            elif span == best_span and is_callable_or_type and best.kind not in (6, 9, 12):
                best = sym
    return best


def _symbol_matches(sym: FlatSymbol, query: str) -> bool:
    q = query.strip()
    if sym.qualified_name == q or sym.name == q:
        return True
    clean_name = _clean_symbol_name(sym.name)
    clean_qname = ".".join(_clean_symbol_name(p) for p in sym.qualified_name.split("."))
    if clean_name == q or clean_qname == q:
        return True
    if "." in q and clean_qname.endswith("." + q):
        return True
    return False


def _resolve_target_position(
    session: LspSession,
    file_path: Path,
    symbol: str | None,
    line_1based: int | None,
    col_1based: int | None,
    line_cache: dict[Path, list[str]],
    sym_cache: dict[Path, list[FlatSymbol]],
) -> tuple[int, int, str]:
    """Resolve (line0, utf16_char, display_label) for a target symbol/location."""
    resolved = file_path.resolve()
    lines = _read_lines(resolved, line_cache)
    if not lines:
        raise ValueError(f"File is empty or unreadable: {resolved}")

    if line_1based is not None and line_1based > len(lines):
        raise ValueError(f"Line {line_1based} out of range (file has {len(lines)} lines): {resolved}")

    doc_symbols = _fetch_document_symbols(session, resolved, sym_cache)

    if symbol:
        candidates = [s for s in doc_symbols if _symbol_matches(s, symbol)]
        if candidates:
            if line_1based is not None:
                target_l0 = line_1based - 1
                containing = [
                    s for s in candidates if s.range_start_line <= target_l0 <= s.range_end_line or abs(s.sel_line - target_l0) <= 2
                ]
                if containing:
                    chosen = min(containing, key=lambda s: abs(s.sel_line - target_l0))
                    return chosen.sel_line, chosen.sel_char, chosen.qualified_name
            elif len(candidates) == 1:
                chosen = candidates[0]
                return chosen.sel_line, chosen.sel_char, chosen.qualified_name
            else:
                choices = ", ".join(
                    f"L{c.sel_line + 1} ({SYMBOL_KINDS.get(c.kind, 'Symbol')} {c.qualified_name})"
                    for c in candidates[:12]
                )
                raise ValueError(
                    f"Ambiguous symbol {symbol!r} in {resolved.name}: matches {choices}. "
                    "Pass 'line' (or qualified ClassName.method) to disambiguate."
                )

        short = symbol.split(".")[-1].strip()
        pattern = re.compile(rf"\b{re.escape(short)}\b")

        if line_1based is not None:
            center = line_1based - 1
            search_order = [center] + [
                center + delta for delta in (-1, 1, -2, 2) if 0 <= center + delta < len(lines)
            ]
            for l0 in search_order:
                m = pattern.search(lines[l0])
                if m:
                    return l0, py_col_to_utf16(lines[l0], m.start()), symbol
            raise ValueError(
                f"Symbol {symbol!r} not found near line {line_1based} in {resolved.name}. "
                "Use locate_symbol first to check the exact line."
            )

        line_hits: list[tuple[int, int]] = []
        for idx, text in enumerate(lines):
            m = pattern.search(text)
            if m:
                line_hits.append((idx, m.start()))
        if len(line_hits) == 1:
            l0, py_col = line_hits[0]
            return l0, py_col_to_utf16(lines[l0], py_col), symbol
        if len(line_hits) > 1:
            preview = ", ".join(f"L{idx + 1}" for idx, _ in line_hits[:10])
            raise ValueError(
                f"Symbol {symbol!r} was not a unique declaration in {resolved.name} and appears on multiple lines "
                f"({preview}). Pass 'line' to specify which occurrence."
            )
        raise ValueError(f"Symbol {symbol!r} not found in {resolved}")

    if line_1based is None:
        raise ValueError("Must provide at least 'symbol' or 'line'.")

    l0 = line_1based - 1
    if col_1based is not None:
        utf16_col = py_col_to_utf16(lines[l0], col_1based - 1)
        py_col = utf16_to_py_col(lines[l0], utf16_col)
        m = re.search(r"[A-Za-z_]\w*", lines[l0][py_col:])
        label = m.group(0) if m else f"{resolved.name}:{line_1based}:{col_1based}"
        return l0, utf16_col, label

    on_line = [s for s in doc_symbols if s.sel_line == l0 or s.range_start_line == l0]
    if len(on_line) == 1:
        chosen = on_line[0]
        return chosen.sel_line, chosen.sel_char, chosen.qualified_name

    raise ValueError(
        f"Line {line_1based} in {resolved.name} requires 'symbol' or 'column' to pinpoint the target identifier."
    )


def _normalize_locations(raw: Any) -> list[tuple[Path, int, int]]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[tuple[Path, int, int]] = []
    seen: set[tuple[Path, int, int]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            uri = str(item.get("targetUri") or "")
            rng = item.get("targetSelectionRange") or item.get("targetRange") or {}
        else:
            uri = str(item.get("uri") or "")
            rng = item.get("range") or {}
        if not uri:
            continue
        fpath = uri_to_path(uri)
        start = rng.get("start") or {}
        l0 = int(start.get("line", 0))
        c0 = int(start.get("character", 0))
        key = (fpath, l0, c0)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _sample_files_by_language(workspace_root: Path, requested_lang: str | None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    target_langs = {requested_lang} if requested_lang else {s.lang_key for s in LANGUAGE_SPECS}
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            spec = EXT_TO_SPEC.get(ext)
            if spec and spec.lang_key in target_langs and spec.lang_key not in found:
                found[spec.lang_key] = (Path(root) / fname).resolve()
                if len(found) == len(target_langs):
                    return found
    return found


def _prepare_call_hierarchy(
    session: LspSession,
    file_path: Path,
    line0: int,
    char_utf16: int,
) -> list[dict[str, Any]]:
    raw = session.request(
        "textDocument/prepareCallHierarchy",
        {
            "textDocument": {"uri": path_to_uri(file_path)},
            "position": {"line": line0, "character": char_utf16},
        },
        timeout=20.0,
    )
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("uri")]


def _item_key(item: dict[str, Any]) -> tuple[Path, int, int, str]:
    fpath = uri_to_path(str(item.get("uri") or ""))
    sel = item.get("selectionRange") or item.get("range") or {}
    start = sel.get("start") or {}
    return (
        fpath,
        int(start.get("line", 0)),
        int(start.get("character", 0)),
        str(item.get("name") or ""),
    )


def _format_warnings(session: LspSession) -> list[str]:
    if not session.warnings:
        return []
    return [f"[Warning] {w}" for w in dict.fromkeys(session.warnings)]


def tool_locate_symbol(args: dict[str, Any]) -> str:
    symbol = _str_arg(args, "symbol")
    if not symbol:
        raise ValueError("'symbol' is required.")
    file_arg = _str_arg(args, "file")
    workspace_arg = _str_arg(args, "workspace")
    language_arg = _str_arg(args, "language")
    limit = _int_arg(args, "limit", default=30, min_val=1, max_val=200) or 30

    line_cache: dict[Path, list[str]] = {}
    sym_cache: dict[Path, list[FlatSymbol]] = {}
    short_query = symbol.split(".")[-1].strip().lower()

    if file_arg:
        fpath = Path(file_arg).expanduser().resolve()
        session = MANAGER.get_session_for_file(fpath, workspace_arg)
        doc_syms = _fetch_document_symbols(session, fpath, sym_cache)
        exact: list[FlatSymbol] = []
        partial: list[FlatSymbol] = []
        for s in doc_syms:
            if _symbol_matches(s, symbol):
                exact.append(s)
            elif short_query in s.qualified_name.lower():
                partial.append(s)
        matches = (exact + partial)[:limit]
        lines_out = _format_warnings(session)
        if not matches:
            lines_out.append(f"No symbol matching {symbol!r} found in {_display_path(fpath, session.workspace_root)}.")
            return "\n".join(lines_out)

        lines_out.append(
            f"Symbols matching {symbol!r} in {_display_path(fpath, session.workspace_root)} "
            f"(server={session.server_name}, count={len(matches)}):"
        )
        for s in matches:
            kind_label = SYMBOL_KINDS.get(s.kind, f"Kind({s.kind})")
            snippet = _line_snippet(s.file_path, s.sel_line, line_cache)
            rel = _display_path(s.file_path, session.workspace_root)
            lines_out.append(f"- [{kind_label}] {s.qualified_name} — {rel}:{s.sel_line + 1}:{s.sel_char + 1}")
            if snippet:
                lines_out.append(f"    {snippet}")
        return "\n".join(lines_out)

    ws_root = Path(workspace_arg or os.getcwd()).expanduser().resolve()
    if not ws_root.is_dir():
        raise FileNotFoundError(f"Workspace directory does not exist: {ws_root}")

    samples = _sample_files_by_language(ws_root, language_arg)
    if not samples:
        raise LspUnavailableError(f"No supported source files found under workspace: {ws_root}")

    results: list[tuple[int, str, str, Path, int, int]] = []
    warnings_out: list[str] = []
    unavailable_errors: list[str] = []
    active_servers: list[str] = []

    for lang_key, sample_file in samples.items():
        try:
            session = MANAGER.get_session_for_file(sample_file, str(ws_root))
        except LspUnavailableError as exc:
            if language_arg:
                raise
            unavailable_errors.append(str(exc))
            continue

        active_servers.append(session.server_name)
        warnings_out.extend(_format_warnings(session))
        raw = session.request("workspace/symbol", {"query": symbol.split(".")[-1].strip()}, timeout=20.0)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            container = str(item.get("containerName") or "").strip()
            qname = f"{container}.{name}" if container else name
            loc = item.get("location") or {}
            uri = str(loc.get("uri") or item.get("uri") or "")
            if not uri:
                continue
            fpath = uri_to_path(uri)
            if not _is_within_workspace(fpath, ws_root):
                continue
            rng = loc.get("range") or {}
            start = rng.get("start") or {}
            l0 = int(start.get("line", 0))
            c0 = int(start.get("character", 0))
            kind = int(item.get("kind") or 0)
            clean_n = _clean_symbol_name(name)
            score = 0 if (clean_n == symbol or qname == symbol) else (1 if clean_n.lower() == short_query else 2)
            results.append((score, qname, SYMBOL_KINDS.get(kind, "Symbol"), fpath, l0, c0))

    if not active_servers and unavailable_errors:
        raise LspUnavailableError("; ".join(unavailable_errors))

    results.sort(key=lambda r: (r[0], str(r[3]), r[4]))
    deduped: list[tuple[int, str, str, Path, int, int]] = []
    seen_locs: set[tuple[Path, int, str]] = set()
    for entry in results:
        k = (entry[3], entry[4], entry[1])
        if k not in seen_locs:
            seen_locs.add(k)
            deduped.append(entry)
            if len(deduped) >= limit:
                break

    lines_out = list(dict.fromkeys(warnings_out))
    if unavailable_errors:
        for err_msg in unavailable_errors:
            lines_out.append(f"[Notice] Skipped language without installed LSP: {err_msg}")
    if not deduped:
        lines_out.append(
            f"No workspace symbol matching {symbol!r} found in {ws_root} (servers={', '.join(active_servers)})."
        )
        return "\n".join(lines_out)

    lines_out.append(
        f"Workspace symbols matching {symbol!r} in {ws_root.name} "
        f"(servers={', '.join(active_servers)}, count={len(deduped)}):"
    )
    for _, qname, kind_label, fpath, l0, c0 in deduped:
        rel = _display_path(fpath, ws_root)
        snippet = _line_snippet(fpath, l0, line_cache)
        lines_out.append(f"- [{kind_label}] {qname} — {rel}:{l0 + 1}:{c0 + 1}")
        if snippet:
            lines_out.append(f"    {snippet}")
    return "\n".join(lines_out)


def tool_find_callers(args: dict[str, Any]) -> str:
    file_arg = _str_arg(args, "file")
    if not file_arg:
        raise ValueError("'file' is required.")
    symbol = _str_arg(args, "symbol")
    line_1based = _int_arg(args, "line", default=None, min_val=1)
    col_1based = _int_arg(args, "column", default=None, min_val=1)
    depth = _int_arg(args, "depth", default=1, min_val=1, max_val=5) or 1
    include_non_call_refs = _bool_arg(args, "include_non_call_refs", default=True)
    workspace_arg = _str_arg(args, "workspace")

    fpath = Path(file_arg).expanduser().resolve()
    session = MANAGER.get_session_for_file(fpath, workspace_arg)

    line_cache: dict[Path, list[str]] = {}
    sym_cache: dict[Path, list[FlatSymbol]] = {}
    l0, c0, resolved_label = _resolve_target_position(
        session, fpath, symbol, line_1based, col_1based, line_cache, sym_cache
    )

    lines_out = _format_warnings(session)
    rel_target = _display_path(fpath, session.workspace_root)
    lines_out.append(
        f"Target: {resolved_label} ({rel_target}:{l0 + 1}:{c0 + 1}) "
        f"[server={session.server_name}, workspace={session.workspace_root.name}, depth={depth}]"
    )

    covered_call_sites: set[tuple[Path, int]] = {(fpath, l0)}
    call_hierarchy_used = False
    root_items: list[dict[str, Any]] = []

    if session.supports_call_hierarchy():
        try:
            root_items = _prepare_call_hierarchy(session, fpath, l0, c0)
            call_hierarchy_used = True
        except LspProtocolError as exc:
            lines_out.append(
                f"[Fallback Notice] callHierarchy failed ({exc}); falling back to textDocument/references + documentSymbol."
            )
    else:
        lines_out.append(
            f"[Fallback Notice] {session.server_name} does not advertise callHierarchyProvider; "
            "resolving callers via textDocument/references + documentSymbol (single hop)."
        )

    total_direct_callers = 0
    if call_hierarchy_used and root_items:
        visited: set[tuple[Path, int, int, str]] = {_item_key(it) for it in root_items}
        tree_lines: list[str] = []

        def walk_incoming(item: dict[str, Any], cur_depth: int, indent: str) -> None:
            nonlocal total_direct_callers
            item_file = uri_to_path(str(item.get("uri") or ""))
            if item_file.is_file():
                session.ensure_file_synced(item_file)
            raw_incoming = session.request("callHierarchy/incomingCalls", {"item": item}, timeout=25.0)
            if not isinstance(raw_incoming, list) or not raw_incoming:
                return

            for entry in raw_incoming:
                if not isinstance(entry, dict) or not isinstance(entry.get("from"), dict):
                    continue
                caller = entry["from"]
                caller_key = _item_key(caller)
                caller_file, caller_l0, _, caller_name = caller_key
                caller_kind = SYMBOL_KINDS.get(int(caller.get("kind") or 0), "Symbol")
                caller_rel = _display_path(caller_file, session.workspace_root)

                from_ranges = entry.get("fromRanges") or []
                call_lines0: list[int] = []
                for fr in from_ranges:
                    if isinstance(fr, dict):
                        sl = int((fr.get("start") or {}).get("line", caller_l0))
                        if sl not in call_lines0:
                            call_lines0.append(sl)
                        if cur_depth == 1:
                            covered_call_sites.add((caller_file, sl))
                if not call_lines0:
                    call_lines0 = [caller_l0]
                    if cur_depth == 1:
                        covered_call_sites.add((caller_file, caller_l0))

                if cur_depth == 1:
                    total_direct_callers += 1

                sites_str = ", ".join(f"L{cl + 1}" for cl in call_lines0[:6])
                if len(call_lines0) > 6:
                    sites_str += f" (+{len(call_lines0) - 6} more)"
                is_cycle = caller_key in visited
                cycle_tag = " [cycle]" if is_cycle else ""
                tree_lines.append(
                    f"{indent}- [depth {cur_depth}] [{caller_kind}] {caller_name} "
                    f"({caller_rel}:{caller_l0 + 1}) -> calls at {sites_str}{cycle_tag}"
                )
                first_snippet = _line_snippet(caller_file, call_lines0[0], line_cache)
                if first_snippet:
                    tree_lines.append(f"{indent}    {first_snippet}")

                if cur_depth < depth and not is_cycle:
                    visited.add(caller_key)
                    walk_incoming(caller, cur_depth + 1, indent + "  ")

        for root_it in root_items:
            walk_incoming(root_it, 1, "")

        lines_out.append(f"\nIncoming Call Hierarchy (direct callers={total_direct_callers}):")
        if tree_lines:
            lines_out.extend(tree_lines)
        else:
            lines_out.append("  (none)")
    elif call_hierarchy_used and not root_items:
        lines_out.append(
            "\n[Notice] prepareCallHierarchy returned no callable item at target position; "
            "checking semantic references below."
        )

    if include_non_call_refs or not call_hierarchy_used or not root_items:
        raw_refs = session.request(
            "textDocument/references",
            {
                "textDocument": {"uri": path_to_uri(fpath)},
                "position": {"line": l0, "character": c0},
                "context": {"includeDeclaration": False},
            },
            timeout=25.0,
        )
        ref_locs = _normalize_locations(raw_refs)
        extra_refs: list[tuple[Path, int, int]] = [
            (rf, rl, rc) for (rf, rl, rc) in ref_locs if (rf, rl) not in covered_call_sites
        ]

        header = (
            "\nAdditional Semantic References (callbacks, dispatch tables, imports/re-exports, type refs"
            if (call_hierarchy_used and root_items)
            else "\nSemantic References & Enclosing Scopes (all usages"
        )
        lines_out.append(f"{header}, count={len(extra_refs)}):")
        if not extra_refs:
            lines_out.append("  (none)")
        else:
            for rf, rl, rc in extra_refs:
                enc = _enclosing_symbol_for_line(session, rf, rl, sym_cache)
                scope_label = (
                    f"{SYMBOL_KINDS.get(enc.kind, 'Scope')} {enc.qualified_name}" if enc is not None else "<module>"
                )
                rel_ref = _display_path(rf, session.workspace_root)
                snippet = _line_snippet(rf, rl, line_cache)
                lines_out.append(f"- {rel_ref}:{rl + 1}:{rc + 1} [in {scope_label}]")
                if snippet:
                    lines_out.append(f"    {snippet}")

    return "\n".join(lines_out)


def tool_find_callees(args: dict[str, Any]) -> str:
    file_arg = _str_arg(args, "file")
    if not file_arg:
        raise ValueError("'file' is required.")
    symbol = _str_arg(args, "symbol")
    line_1based = _int_arg(args, "line", default=None, min_val=1)
    col_1based = _int_arg(args, "column", default=None, min_val=1)
    depth = _int_arg(args, "depth", default=1, min_val=1, max_val=5) or 1
    include_external = _bool_arg(args, "include_external", default=False)
    workspace_arg = _str_arg(args, "workspace")

    fpath = Path(file_arg).expanduser().resolve()
    session = MANAGER.get_session_for_file(fpath, workspace_arg)
    if not session.supports_call_hierarchy():
        raise LspUnavailableError(
            f"{session.server_name} does not advertise callHierarchyProvider; outgoing callee tracing is unavailable."
        )

    line_cache: dict[Path, list[str]] = {}
    sym_cache: dict[Path, list[FlatSymbol]] = {}
    l0, c0, resolved_label = _resolve_target_position(
        session, fpath, symbol, line_1based, col_1based, line_cache, sym_cache
    )
    root_items = _prepare_call_hierarchy(session, fpath, l0, c0)
    if not root_items:
        raise ValueError(
            f"No callable symbol for outgoing call hierarchy at {_display_path(fpath, session.workspace_root)}:{l0 + 1}:{c0 + 1} ({resolved_label})."
        )

    lines_out = _format_warnings(session)
    rel_target = _display_path(fpath, session.workspace_root)
    lines_out.append(
        f"Target: {resolved_label} ({rel_target}:{l0 + 1}:{c0 + 1}) "
        f"[server={session.server_name}, workspace={session.workspace_root.name}, depth={depth}]"
    )

    visited: set[tuple[Path, int, int, str]] = {_item_key(it) for it in root_items}
    tree_lines: list[str] = []
    direct_count = 0
    omitted_external = 0

    def walk_outgoing(item: dict[str, Any], cur_depth: int, indent: str) -> None:
        nonlocal direct_count, omitted_external
        item_file = uri_to_path(str(item.get("uri") or ""))
        if item_file.is_file():
            session.ensure_file_synced(item_file)
        raw_outgoing = session.request("callHierarchy/outgoingCalls", {"item": item}, timeout=25.0)
        if not isinstance(raw_outgoing, list) or not raw_outgoing:
            return

        for entry in raw_outgoing:
            if not isinstance(entry, dict) or not isinstance(entry.get("to"), dict):
                continue
            callee = entry["to"]
            callee_key = _item_key(callee)
            callee_file, callee_l0, _, callee_name = callee_key
            callee_kind = SYMBOL_KINDS.get(int(callee.get("kind") or 0), "Symbol")
            in_ws = _is_within_workspace(callee_file, session.workspace_root)
            if not in_ws and not include_external:
                omitted_external += 1
                continue
            callee_rel = _display_path(callee_file, session.workspace_root)
            ext_tag = "" if in_ws else " [external]"

            from_ranges = entry.get("fromRanges") or []
            call_lines0: list[int] = []
            for fr in from_ranges:
                if isinstance(fr, dict):
                    sl = int((fr.get("start") or {}).get("line", 0))
                    if sl not in call_lines0:
                        call_lines0.append(sl)
            sites_str = ", ".join(f"L{cl + 1}" for cl in call_lines0[:6]) if call_lines0 else "?"
            if cur_depth == 1:
                direct_count += 1

            is_cycle = callee_key in visited
            cycle_tag = " [cycle]" if is_cycle else ""
            tree_lines.append(
                f"{indent}- [depth {cur_depth}] [{callee_kind}] {callee_name} "
                f"({callee_rel}:{callee_l0 + 1}){ext_tag} <- called at {sites_str}{cycle_tag}"
            )
            if in_ws:
                snippet = _line_snippet(callee_file, callee_l0, line_cache)
                if snippet:
                    tree_lines.append(f"{indent}    {snippet}")

            if cur_depth < depth and in_ws and not is_cycle:
                visited.add(callee_key)
                walk_outgoing(callee, cur_depth + 1, indent + "  ")

    for root_it in root_items:
        walk_outgoing(root_it, 1, "")

    lines_out.append(f"\nOutgoing Call Hierarchy (direct callees={direct_count}):")
    if tree_lines:
        lines_out.extend(tree_lines)
    else:
        lines_out.append("  (none)")
    if omitted_external:
        lines_out.append(
            f"  (omitted {omitted_external} external/stdlib callees; pass include_external=true to list them)"
        )
    return "\n".join(lines_out)


def tool_find_implementations(args: dict[str, Any]) -> str:
    file_arg = _str_arg(args, "file")
    if not file_arg:
        raise ValueError("'file' is required.")
    symbol = _str_arg(args, "symbol")
    line_1based = _int_arg(args, "line", default=None, min_val=1)
    col_1based = _int_arg(args, "column", default=None, min_val=1)
    workspace_arg = _str_arg(args, "workspace")

    fpath = Path(file_arg).expanduser().resolve()
    session = MANAGER.get_session_for_file(fpath, workspace_arg)

    line_cache: dict[Path, list[str]] = {}
    sym_cache: dict[Path, list[FlatSymbol]] = {}
    l0, c0, resolved_label = _resolve_target_position(
        session, fpath, symbol, line_1based, col_1based, line_cache, sym_cache
    )

    try:
        raw_impls = session.request(
            "textDocument/implementation",
            {
                "textDocument": {"uri": path_to_uri(fpath)},
                "position": {"line": l0, "character": c0},
            },
            timeout=25.0,
        )
    except LspProtocolError as exc:
        raise LspUnavailableError(
            f"{session.server_name} rejected textDocument/implementation ({exc}). "
            "For Python subclass/protocol lookups on servers without implementationProvider, "
            "use find_callers with include_non_call_refs=true on the base class or method."
        ) from exc

    if raw_impls is None and not session.supports_implementation():
        raise LspUnavailableError(
            f"{session.server_name} does not advertise implementationProvider for {session.spec.lang_key!r}. "
            "Use find_callers(include_non_call_refs=true) on the base class/protocol to inspect subclasses and usages."
        )

    locs = [loc for loc in _normalize_locations(raw_impls) if not (loc[0] == fpath and loc[1] == l0)]
    lines_out = _format_warnings(session)
    rel_target = _display_path(fpath, session.workspace_root)
    lines_out.append(
        f"Implementations of {resolved_label} ({rel_target}:{l0 + 1}:{c0 + 1}) "
        f"[server={session.server_name}, count={len(locs)}]:"
    )
    if not locs:
        lines_out.append("  (none)")
        return "\n".join(lines_out)

    for impl_file, impl_l0, impl_c0 in locs:
        enc = _enclosing_symbol_for_line(session, impl_file, impl_l0, sym_cache)
        scope_str = (
            f" [{SYMBOL_KINDS.get(enc.kind, 'Symbol')} {enc.qualified_name}]" if enc is not None else ""
        )
        rel_impl = _display_path(impl_file, session.workspace_root)
        snippet = _line_snippet(impl_file, impl_l0, line_cache)
        lines_out.append(f"- {rel_impl}:{impl_l0 + 1}:{impl_c0 + 1}{scope_str}")
        if snippet:
            lines_out.append(f"    {snippet}")
    return "\n".join(lines_out)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "locate_symbol",
        "description": (
            "Locate exact symbol declarations (functions, methods, classes, interfaces, structs, constants) "
            "with 1-based file:line:column coordinates using the language server. Pass 'file' to list matching "
            "symbols inside a known file, or pass 'workspace' to search across the repository."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol name or qualified ClassName.method to locate.",
                },
                "file": {
                    "type": "string",
                    "description": "Optional path to a specific source file for fast documentSymbol lookup.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root directory when searching across files.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "typescript", "go", "rust", "cpp", "swift"],
                    "description": "Optional language filter for workspace-wide symbol search.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matches to return (1-200, default 30).",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "find_callers",
        "description": (
            "Find all upstream callers and semantic references of a function, method, or symbol via LSP. "
            "Combines callHierarchy/incomingCalls (supporting multi-hop 'depth' for blast-radius analysis) "
            "with textDocument/references + documentSymbol scope mapping so callbacks, dispatch tables, "
            "decorators, and aliased imports/re-exports are never missed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to the file containing the target symbol.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol name (e.g. 'parse_args' or 'LspSession.start'). Preferred over raw column.",
                },
                "line": {
                    "type": "integer",
                    "description": "1-based line number of the symbol (disambiguates overloaded/duplicate names).",
                },
                "column": {
                    "type": "integer",
                    "description": "Optional 1-based column number when 'symbol' is omitted.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Upstream call-hierarchy traversal depth (1-5, default 1).",
                },
                "include_non_call_refs": {
                    "type": "boolean",
                    "description": "Also include non-call semantic references such as callbacks, dict tables, and imports (default true).",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root directory override.",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "find_callees",
        "description": (
            "Trace outgoing calls made inside a function or method via LSP callHierarchy/outgoingCalls, "
            "optionally recursing up to 'depth' hops across workspace functions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to the file containing the caller function/method.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Function or method name (e.g. 'handle_request' or 'LspSession.start').",
                },
                "line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to disambiguate.",
                },
                "column": {
                    "type": "integer",
                    "description": "Optional 1-based column number.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Downstream call-hierarchy traversal depth (1-5, default 1).",
                },
                "include_external": {
                    "type": "boolean",
                    "description": "Include stdlib/third-party callees outside the workspace (default false).",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root directory override.",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "find_implementations",
        "description": (
            "Find concrete implementations of an interface, trait, protocol, or abstract/base method "
            "via LSP textDocument/implementation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Path to the file declaring the interface, trait, or base method.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Interface, trait, or method name.",
                },
                "line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to disambiguate.",
                },
                "column": {
                    "type": "integer",
                    "description": "Optional 1-based column number.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace root directory override.",
                },
            },
            "required": ["file"],
        },
    },
]

TOOL_HANDLERS = {
    "locate_symbol": tool_locate_symbol,
    "find_callers": tool_find_callers,
    "find_callees": tool_find_callees,
    "find_implementations": tool_find_implementations,
}


def handle_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        client_version = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "code-intel", "version": "1.0.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        try:
            text = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error ({type(exc).__name__}): {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                err = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                sys.stdout.write(json.dumps(err, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                continue

            resp = handle_request(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        MANAGER.close_all()


if __name__ == "__main__":
    main()
