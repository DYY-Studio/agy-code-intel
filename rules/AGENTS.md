# Semantic Symbol Navigation & Call Graph (`code-intel`)

When analyzing impact, refactoring, renaming, deleting, or changing the signature/behavior of functions, methods, classes, or interfaces in Python, TypeScript/JavaScript (`.ts`/`.tsx`/`.js`/`.jsx`/`.mjs`/`.cjs`), Go, Rust, C/C++, or Swift, **always use `code-intel_mcp` MCP tools instead of relying solely on `rg` text search**:

1. **Mandatory Caller & Reference Check Before Breaking Changes**:
   - Before modifying a function/method signature, return structure, exception contract, or deleting/renaming a symbol, call `find_callers({ file, symbol, line?, depth? })` first.
   - `rg` misses aliased imports (`import x as y`), overloaded/polymorphic method receivers, and truncates common method names (`run`, `open`, `parse`, `handle`). `find_callers` combines LSP `callHierarchy/incomingCalls` with `textDocument/references` + `documentSymbol` scope resolution so direct callers, callbacks, dispatch dicts, decorators, and re-exports are all surfaced.

2. **Tool Workflow**:
   - `locate_symbol({ symbol, file?, workspace?, language?, limit? })`: Locate exact `file:line:column` declarations inside a file or across a workspace when you only know the symbol name.
   - `find_callers({ file, symbol?, line?, column?, depth?: 1..5, include_non_call_refs?: true, workspace? })`: Pass `symbol` (e.g. `"parse_args"` or `"ClassName.method"`) and optional `line` to disambiguate; set `depth: 2` or `3` when tracing multi-hop upstream blast radius.
   - `find_callees({ file, symbol?, line?, depth?: 1..5, include_external?: false, workspace? })`: Inspect downstream workspace functions called by a routine without reading every file manually.
   - `find_implementations({ file, symbol?, line?, workspace? })`: List concrete struct/class/object implementations of an interface, trait, or base method before changing its contract.

3. **Explicit Diagnostics & Cross-Config Checks**:
   - If `code-intel_mcp` reports an `LspUnavailableError` or `[Fallback Notice]`, surface that notice rather than assuming zero callers; supplement with `rg` only for non-code references (HTML templates, JSON/YAML configs, or dynamic string reflection).
