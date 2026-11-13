# Code Intelligence (`code-intel`)

A zero-dependency, LSP-backed **semantic symbol navigation and call-graph MCP plugin** for **Antigravity 2.0**, designed to eliminate missed callers and noisy text-search loops during refactoring and impact analysis.

Unlike raw `rg` text searches—which miss aliased imports, polymorphic method receivers, and callback/dispatch-table registrations, or drown context in string/comment matches—`code-intel` drives local Language Servers on demand and combines **`callHierarchy/incomingCalls`** with **`textDocument/references` + `documentSymbol`** scope resolution.

## Features

1. **Four Focused Read-Only MCP Tools (`code-intel_mcp`)**:
   - `locate_symbol`: Find exact `file:line:column` symbol declarations inside a file (`textDocument/documentSymbol`) or across a repository (`workspace/symbol`).
   - `find_callers`: Trace upstream callers up to `depth` (`1–5`) hops via `callHierarchy/incomingCalls`, **plus** non-call semantic references (`textDocument/references` mapped to enclosing functions/classes/modules) so dispatch dicts (`HANDLERS = {"x": fn}`), callbacks, decorators, and re-exports are never missed.
   - `find_callees`: Trace downstream workspace callees (`callHierarchy/outgoingCalls`), omitting noisy stdlib/third-party calls by default (`include_external: false`) with an explicit count.
   - `find_implementations`: Locate concrete implementations of interfaces, traits, or abstract/base methods (`textDocument/implementation`).

2. **Zero Workspace Pollution & Automatic Sync**:
   - Pure Python 3 standard library implementation; writes **zero** dot-directories or cache files into target repositories.
   - Automatically detects workspace roots, Python `.venv` interpreters, global `tsserver.js` fallback paths, and unconfigured `.mjs`/`.js` repositories.
   - Tracks open document `(mtime_ns, size)` on disk so edits made mid-session immediately sync via `textDocument/didChange`, and evicts idle LSP subprocesses after 10 minutes.
   - **No silent fallbacks**: missing language servers or unsupported LSP capabilities are reported explicitly.

3. **Built-in Agent Guardrail (`rules/AGENTS.md`)**:
   - Instructs the Agent to run `find_callers` before changing function signatures, return contracts, or deleting/renaming symbols.

## Supported Languages & Language Servers

| Language | Extensions | Language Server Binary | Install Command |
| :--- | :--- | :--- | :--- |
| **Python** | `.py`, `.pyi` | `basedpyright-langserver` / `pyright-langserver` | `uv tool install basedpyright` |
| **TypeScript / JS** | `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs` | `typescript-language-server` / `vtsls` | `npm install -g 'typescript@^6' typescript-language-server` |
| **Go** | `.go` | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| **Rust** | `.rs` | `rust-analyzer` | `rustup component add rust-analyzer` |
| **C / C++** | `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hpp`, `.hh` | `clangd` | Xcode Command Line Tools / `brew install llvm` |
| **Swift** | `.swift` | `sourcekit-lsp` | Xcode / Swift Toolchain |

## Installation

1. Clone or symlink into your Antigravity plugins folder:

   ```bash
   ln -s /path/to/code-intel ~/.gemini/config/plugins/code-intel
   ```

2. Open **Customizations → Plugins** in Antigravity 2.0 and ensure **Code Intelligence** is enabled.

## Testing

```bash
python3 -m unittest discover tests
```

## License

[MIT](LICENSE)
