"""Zero-dependency LSP subprocess manager and JSON-RPC client for code-intel."""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import unquote, urlparse

IDLE_TIMEOUT_SEC = 600.0
MAX_SESSIONS_PER_WORKSPACE_LANG = 3
MAX_PREOPEN_JS_FILES = 80
MAX_PREOPEN_FILE_BYTES = 512 * 1024
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", "vendor", ".next", ".nuxt", ".mypy_cache", ".pytest_cache",
})
PYRIGHT_FOUND_FILES_RE = re.compile(r"Found \d+ source files?")
TSSERVER_CRASH_RE = re.compile(r"\[tsserver\]\s+Exited\b", re.IGNORECASE)


class LspUnavailableError(RuntimeError):
    """Raised when no supported language server is available for a file."""


class LspProtocolError(RuntimeError):
    """Raised when an LSP request fails or times out."""


@dataclass(frozen=True)
class LanguageSpec:
    lang_key: str
    extensions: tuple[str, ...]
    candidates: tuple[tuple[str, tuple[str, ...]], ...]
    root_markers: tuple[str, ...]
    install_hint: str


LANGUAGE_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        lang_key="python",
        extensions=(".py", ".pyi"),
        candidates=(
            ("basedpyright-langserver", ("--stdio",)),
            ("pyright-langserver", ("--stdio",)),
        ),
        root_markers=("pyrightconfig.json", "pyproject.toml", "setup.py", ".venv", "venv"),
        install_hint="uv tool install basedpyright",
    ),
    LanguageSpec(
        lang_key="typescript",
        extensions=(".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
        candidates=(
            ("typescript-language-server", ("--stdio",)),
            ("vtsls", ("--stdio",)),
        ),
        root_markers=("tsconfig.json", "jsconfig.json", "package.json"),
        install_hint="npm install -g 'typescript@^6' typescript-language-server",
    ),
    LanguageSpec(
        lang_key="go",
        extensions=(".go",),
        candidates=(("gopls", ()),),
        root_markers=("go.work", "go.mod"),
        install_hint="go install golang.org/x/tools/gopls@latest",
    ),
    LanguageSpec(
        lang_key="rust",
        extensions=(".rs",),
        candidates=(("rust-analyzer", ()),),
        root_markers=("Cargo.toml",),
        install_hint="rustup component add rust-analyzer",
    ),
    LanguageSpec(
        lang_key="cpp",
        extensions=(".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"),
        candidates=(("clangd", ("--background-index",)),),
        root_markers=("compile_commands.json", "compile_flags.txt", "CMakeLists.txt"),
        install_hint="Install clangd via Xcode Command Line Tools or brew install llvm",
    ),
    LanguageSpec(
        lang_key="swift",
        extensions=(".swift",),
        candidates=(("sourcekit-lsp", ()),),
        root_markers=("Package.swift",),
        install_hint="Install Xcode or Swift toolchain (sourcekit-lsp)",
    ),
)

EXT_TO_SPEC: dict[str, LanguageSpec] = {
    ext: spec for spec in LANGUAGE_SPECS for ext in spec.extensions
}


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    s = str(uri or "").strip()
    if s.startswith("file:"):
        parsed = urlparse(s)
        raw = unquote(parsed.path)
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        if parsed.netloc and parsed.netloc != "localhost":
            raw = f"//{parsed.netloc}{raw}"
        return Path(raw).resolve()
    return Path(s).expanduser().resolve()


def py_col_to_utf16(line_text: str, py_col: int) -> int:
    clamped = max(0, min(len(line_text), py_col))
    return len(line_text[:clamped].encode("utf-16-le")) // 2


def utf16_to_py_col(line_text: str, utf16_col: int) -> int:
    if utf16_col <= 0 or not line_text:
        return 0
    raw = line_text.encode("utf-16-le")
    byte_offset = min(len(raw), utf16_col * 2)
    if byte_offset % 2:
        byte_offset -= 1
    decoded = raw[:byte_offset].decode("utf-16-le", errors="ignore")
    return len(decoded)


def language_id_for_path(path: Path, lang_key: str) -> str:
    ext = path.suffix.lower()
    if lang_key == "typescript":
        if ext == ".tsx":
            return "typescriptreact"
        if ext == ".jsx":
            return "javascriptreact"
        if ext in (".js", ".mjs", ".cjs"):
            return "javascript"
        return "typescript"
    if lang_key == "cpp":
        return "c" if ext == ".c" else "cpp"
    return lang_key


def spec_for_file(path: Path) -> LanguageSpec:
    ext = path.suffix.lower()
    spec = EXT_TO_SPEC.get(ext)
    if spec is None:
        supported = ", ".join(sorted(EXT_TO_SPEC.keys()))
        raise LspUnavailableError(
            f"Unsupported file extension {ext!r} for {path}. Supported extensions: {supported}"
        )
    return spec


def _same_mount(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def detect_workspace_root(target: Path, spec: LanguageSpec, explicit_workspace: str | None = None) -> Path:
    if explicit_workspace:
        ws = Path(explicit_workspace).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"Workspace directory does not exist: {ws}")
        return ws

    start_dir = target.resolve().parent if target.is_file() or not target.is_dir() else target.resolve()
    ancestors: list[Path] = [start_dir]
    cur = start_dir
    while cur.parent != cur and _same_mount(cur.parent, start_dir):
        cur = cur.parent
        ancestors.append(cur)

    if spec.lang_key == "go":
        for d in ancestors:
            if (d / "go.work").exists():
                return d
    elif spec.lang_key == "rust":
        top_workspace: Path | None = None
        for d in ancestors:
            cargo = d / "Cargo.toml"
            if cargo.is_file():
                try:
                    if "[workspace]" in cargo.read_text(encoding="utf-8", errors="ignore"):
                        top_workspace = d
                except OSError:
                    pass
        if top_workspace is not None:
            return top_workspace

    for d in ancestors:
        for marker in spec.root_markers:
            if (d / marker).exists():
                return d

    for d in ancestors:
        if (d / ".git").exists():
            return d

    return start_dir


def resolve_python_interpreter(
    workspace_root: Path,
    explicit_python_path: str | None = None,
    target_file: Path | None = None,
) -> Path | None:
    """Resolve Python interpreter or venv directory without dereferencing venv bin/python symlinks."""
    if explicit_python_path:
        raw = Path(os.path.abspath(os.path.expanduser(explicit_python_path.strip())))
        if raw.is_dir():
            for rel in (("bin", "python"), ("bin", "python3"), ("Scripts", "python.exe"), ("python.exe",)):
                cand = Path(os.path.abspath(raw.joinpath(*rel)))
                if cand.is_file() and os.access(cand, os.X_OK):
                    return cand
            raise ValueError(f"No executable Python interpreter found inside directory: {raw}")
        if not raw.is_file() or not os.access(raw, os.X_OK):
            raise ValueError(f"Python interpreter does not exist or is not executable: {raw}")
        if not re.match(r"^python(\d+(\.\d+)*)?(\.exe)?$", raw.name, re.IGNORECASE):
            raise ValueError(f"Invalid python_path executable name {raw.name!r} (expected python/python3): {raw}")
        return raw

    rel_candidates = (
        (".venv", "bin", "python"),
        ("venv", "bin", "python"),
        (".venv", "Scripts", "python.exe"),
        ("venv", "Scripts", "python.exe"),
    )
    search_roots: list[Path] = []
    ws_resolved = workspace_root.resolve()
    if target_file is not None:
        cur = target_file.resolve().parent
        while True:
            search_roots.append(cur)
            if cur == ws_resolved or cur.parent == cur or not _same_mount(cur.parent, cur):
                break
            try:
                cur.relative_to(ws_resolved)
            except ValueError:
                break
            cur = cur.parent

    if ws_resolved not in search_roots:
        search_roots.append(ws_resolved)
    if ws_resolved.parent != ws_resolved and _same_mount(ws_resolved.parent, ws_resolved):
        search_roots.append(ws_resolved.parent)

    for root in search_roots:
        for parts in rel_candidates:
            candidate = Path(os.path.abspath(root.joinpath(*parts)))
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _build_augmented_env(workspace_root: Path, python_path: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    home = Path.home()
    extra_dirs: list[Path] = []
    if python_path is not None:
        extra_dirs.append(python_path.parent)
    extra_dirs.extend([
        workspace_root / ".venv" / "bin",
        workspace_root / "venv" / "bin",
        workspace_root / ".venv" / "Scripts",
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        home / "go" / "bin",
        home / ".cargo" / "bin",
    ])
    gopath = env.get("GOPATH")
    if gopath:
        extra_dirs.append(Path(gopath) / "bin")
    cargo_home = env.get("CARGO_HOME")
    if cargo_home:
        extra_dirs.append(Path(cargo_home) / "bin")

    existing = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    seen = set(existing)
    prefix: list[str] = []
    for d in extra_dirs:
        s = str(d)
        if s not in seen and d.is_dir():
            prefix.append(s)
            seen.add(s)
    env["PATH"] = os.pathsep.join(prefix + existing)
    return env


def _resolve_lsp_command(spec: LanguageSpec, workspace_root: Path, env: dict[str, str]) -> list[str]:
    path_str = env.get("PATH", "")
    for binary, args in spec.candidates:
        found = shutil.which(binary, path=path_str)
        if found:
            if binary == "rust-analyzer":
                try:
                    probe = subprocess.run(
                        [found, "--version"],
                        env=env,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    if probe.returncode != 0:
                        rustup = shutil.which("rustup", path=path_str)
                        if rustup:
                            via = subprocess.run(
                                [rustup, "which", "rust-analyzer"],
                                env=env,
                                capture_output=True,
                                text=True,
                                timeout=5,
                                check=False,
                            )
                            if via.returncode == 0 and via.stdout.strip():
                                found = via.stdout.strip()
                            else:
                                continue
                        else:
                            continue
                except (OSError, subprocess.TimeoutExpired):
                    continue
            return [found, *args]

    tried = ", ".join(b for b, _ in spec.candidates)
    raise LspUnavailableError(
        f"Language server for {spec.lang_key!r} not found (searched for: {tried}). "
        f"Install with: {spec.install_hint}"
    )


def _find_fallback_tsserver(lsp_executable: str, env: dict[str, str]) -> str | None:
    candidates: list[Path] = []
    try:
        resolved_bin = Path(lsp_executable).resolve()
        # e.g. <prefix>/lib/node_modules/typescript-language-server/lib/cli.mjs
        for ancestor in resolved_bin.parents:
            if ancestor.name == "node_modules":
                candidates.append(ancestor / "typescript" / "lib" / "tsserver.js")
                break
    except OSError:
        pass

    tsc_bin = shutil.which("tsc", path=env.get("PATH", ""))
    if tsc_bin:
        try:
            tsc_resolved = Path(tsc_bin).resolve()
            candidates.append(tsc_resolved.parent.parent / "lib" / "tsserver.js")
        except OSError:
            pass

    candidates.extend([
        Path("/opt/homebrew/lib/node_modules/typescript/lib/tsserver.js"),
        Path("/usr/local/lib/node_modules/typescript/lib/tsserver.js"),
    ])
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


@dataclass
class _OpenDocState:
    mtime_ns: int
    size: int
    version: int
    language_id: str


class LspSession:
    """Manages a single language server child process over stdio JSON-RPC."""

    def __init__(
        self,
        spec: LanguageSpec,
        workspace_root: Path,
        python_path: Path | None = None,
    ) -> None:
        self.spec = spec
        self.workspace_root = workspace_root.resolve()
        self.python_path = python_path if spec.lang_key == "python" else None
        self.env = _build_augmented_env(self.workspace_root, self.python_path)
        self.cmd = _resolve_lsp_command(spec, self.workspace_root, self.env)
        self.server_name = Path(self.cmd[0]).name

        self._proc: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._op_lock = threading.RLock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        self._progress_lock = threading.Lock()
        self._active_tokens: set[str] = set()
        self._quiescent = threading.Event()
        self._quiescent.set()
        self._initial_scan_done = threading.Event()
        self._crash_reason: str | None = None

        self.server_capabilities: dict[str, Any] = {}
        self.registered_methods: set[str] = set()
        self.warnings: list[str] = []
        self._open_docs: dict[Path, _OpenDocState] = {}
        self._js_preopened = False
        self.last_used = time.monotonic()
        self._closed = False

    def is_alive(self) -> bool:
        return not self._closed and self._proc is not None and self._proc.poll() is None

    def supports_call_hierarchy(self) -> bool:
        if self.server_capabilities.get("callHierarchyProvider"):
            return True
        return bool(
            {"textDocument/callHierarchy", "textDocument/prepareCallHierarchy"} & self.registered_methods
        )

    def supports_implementation(self) -> bool:
        if self.server_capabilities.get("implementationProvider"):
            return True
        return "textDocument/implementation" in self.registered_methods

    def start(self) -> None:
        with self._op_lock:
            if self.is_alive():
                return
            self._proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.workspace_root),
                env=self.env,
            )
            threading.Thread(target=self._stdout_loop, name=f"lsp-out-{self.spec.lang_key}", daemon=True).start()
            threading.Thread(target=self._stderr_loop, name=f"lsp-err-{self.spec.lang_key}", daemon=True).start()

            init_params = self._build_initialize_params()
            res = self.request("initialize", init_params, timeout=30.0)
            if isinstance(res, dict):
                self.server_capabilities = res.get("capabilities") or {}
            self.notify("initialized", {})

            if self.spec.lang_key == "python" and self.python_path is not None:
                self.notify(
                    "workspace/didChangeConfiguration",
                    {"settings": {"python": {"pythonPath": str(self.python_path)}}},
                )
            if self.spec.lang_key in ("python", "go", "rust"):
                # Wait briefly for initial workspace scan on servers that index eagerly on startup.
                self._wait_initial_ready(timeout=12.0, start_grace=0.8)

    def _build_initialize_params(self) -> dict[str, Any]:
        root_uri = path_to_uri(self.workspace_root)
        init_options: dict[str, Any] = {}
        if self.spec.lang_key == "python":
            init_options = {
                "exclude": ["**/__pycache__", "**/.venv", "**/venv", "**/node_modules", "**/build", "**/dist"],
            }
            if self.python_path is not None:
                init_options["python"] = {"pythonPath": str(self.python_path)}
        elif self.spec.lang_key == "typescript":
            init_options = {"disableAutomaticTypingAcquisition": True}
            fb_tsserver = _find_fallback_tsserver(self.cmd[0], self.env)
            if fb_tsserver:
                init_options["tsserver"] = {"fallbackPath": fb_tsserver}

        return {
            "processId": os.getpid(),
            "clientInfo": {"name": "antigravity-code-intel", "version": "1.0.0"},
            "locale": "en",
            "rootPath": str(self.workspace_root),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.workspace_root.name}],
            "initializationOptions": init_options,
            "capabilities": {
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "didChangeWatchedFiles": {"dynamicRegistration": True},
                    "symbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
                "textDocument": {
                    "synchronization": {"dynamicRegistration": True, "didSave": True},
                    "definition": {"dynamicRegistration": True},
                    "implementation": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "callHierarchy": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "window": {"workDoneProgress": True},
            },
        }

    def _send_payload(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise LspProtocolError(f"LSP server {self.server_name} is not running ({self._crash_reason or 'exited'})")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            try:
                proc.stdin.write(header + body)
                proc.stdin.flush()
            except BrokenPipeError as exc:
                raise LspProtocolError(f"Broken pipe writing to {self.server_name}") from exc

    def notify(self, method: str, params: Any) -> None:
        self.last_used = time.monotonic()
        self._send_payload({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout: float = 25.0) -> Any:
        self.last_used = time.monotonic()
        if self._crash_reason:
            raise LspProtocolError(f"{self.server_name} crashed: {self._crash_reason}")

        with self._pending_lock:
            self._next_id += 1
            req_id = self._next_id
            ev = threading.Event()
            box: dict[str, Any] = {}
            self._pending[req_id] = (ev, box)

        try:
            self._send_payload({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            if not ev.wait(timeout=timeout):
                raise LspProtocolError(f"LSP request {method!r} timed out after {timeout:.0f}s on {self.server_name}")
            if "error" in box:
                err = box["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                code = err.get("code", "") if isinstance(err, dict) else ""
                raise LspProtocolError(f"LSP {method} error ({code}): {msg}")
            return box.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while True:
                content_length: int | None = None
                while True:
                    raw_line = stream.readline()
                    if not raw_line:
                        return
                    line = raw_line.decode("ascii", errors="replace").strip()
                    if not line:
                        break
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            content_length = None
                if content_length is None or content_length < 0:
                    continue
                chunks: list[bytes] = []
                remaining = content_length
                while remaining > 0:
                    chunk = stream.read(remaining)
                    if not chunk:
                        return
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw_body = b"".join(chunks)
                try:
                    msg = json.loads(raw_body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch_incoming(msg)
        finally:
            self._fail_all_pending(f"{self.server_name} stdout closed")

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for _ in proc.stderr:
            pass

    def _fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            for ev, box in self._pending.values():
                if "result" not in box and "error" not in box:
                    box["error"] = {"code": -32099, "message": reason}
                    ev.set()

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        method = msg.get("method")

        if has_id and method is None:
            req_id = msg["id"]
            if isinstance(req_id, str) and req_id.isdigit():
                req_id = int(req_id)
            with self._pending_lock:
                entry = self._pending.get(req_id)
                if entry is not None:
                    ev, box = entry
                    if "error" in msg and msg["error"] is not None:
                        box["error"] = msg["error"]
                    else:
                        box["result"] = msg.get("result")
                    ev.set()
            return

        if has_id and method is not None:
            self._handle_server_request(msg["id"], str(method), msg.get("params"))
            return

        if method is not None:
            self._handle_notification(str(method), msg.get("params"))

    def _handle_server_request(self, req_id: Any, method: str, params: Any) -> None:
        result: Any = None
        try:
            if method == "client/registerCapability" and isinstance(params, dict):
                for reg in params.get("registrations") or []:
                    if isinstance(reg, dict) and reg.get("method"):
                        self.registered_methods.add(str(reg["method"]))
            elif method == "window/workDoneProgress/create" and isinstance(params, dict):
                token = str(params.get("token", ""))
                with self._progress_lock:
                    self._active_tokens.add(token)
                    self._quiescent.clear()
                result = {}
            elif method == "workspace/configuration" and isinstance(params, dict):
                items = params.get("items") or []
                out: list[dict[str, Any]] = []
                for item in items:
                    section = item.get("section") if isinstance(item, dict) else None
                    if section == "python" and self.python_path is not None:
                        out.append({"pythonPath": str(self.python_path)})
                    elif section == "" and self.python_path is not None:
                        out.append({"python": {"pythonPath": str(self.python_path)}})
                    else:
                        out.append({})
                result = out
            elif method == "workspace/workspaceFolders":
                result = [{"uri": path_to_uri(self.workspace_root), "name": self.workspace_root.name}]
            elif method == "workspace/executeClientCommand":
                result = []
            elif method == "workspace/applyEdit":
                result = {"applied": False, "failureReason": "code-intel is read-only"}
        except Exception:
            result = None

        try:
            self._send_payload({"jsonrpc": "2.0", "id": req_id, "result": result})
        except LspProtocolError:
            pass

    def _handle_notification(self, method: str, params: Any) -> None:
        if method == "$/progress" and isinstance(params, dict):
            token = str(params.get("token", ""))
            val = params.get("value") if isinstance(params.get("value"), dict) else {}
            kind = val.get("kind")
            with self._progress_lock:
                if kind == "begin":
                    self._active_tokens.add(token)
                    self._quiescent.clear()
                elif kind == "end":
                    self._active_tokens.discard(token)
                    if not self._active_tokens:
                        self._quiescent.set()
                        self._initial_scan_done.set()
        elif method == "pyright/beginProgress":
            with self._progress_lock:
                self._active_tokens.add("__pyright__")
                self._quiescent.clear()
        elif method == "pyright/endProgress":
            with self._progress_lock:
                self._active_tokens.discard("__pyright__")
                if not self._active_tokens:
                    self._quiescent.set()
                self._initial_scan_done.set()
        elif method == "experimental/serverStatus" and isinstance(params, dict):
            if params.get("quiescent") is True:
                self._initial_scan_done.set()
        elif method == "window/logMessage" and isinstance(params, dict):
            text = str(params.get("message", ""))
            if PYRIGHT_FOUND_FILES_RE.search(text):
                self._initial_scan_done.set()
            if params.get("type") == 1 and TSSERVER_CRASH_RE.search(text):
                self._crash_reason = text
                self._fail_all_pending(text)

    def _wait_initial_ready(self, timeout: float = 12.0, start_grace: float = 0.6) -> None:
        deadline = time.monotonic() + start_grace
        while time.monotonic() < deadline:
            if self._initial_scan_done.is_set():
                break
            with self._progress_lock:
                if self._active_tokens:
                    break
            time.sleep(0.05)

        if self._initial_scan_done.is_set():
            with self._progress_lock:
                if not self._active_tokens:
                    return

        with self._progress_lock:
            has_tokens = bool(self._active_tokens)

        if has_tokens:
            if not self._quiescent.wait(timeout=timeout):
                self.warnings.append(
                    f"{self.server_name} background indexing did not finish within {timeout:.0f}s; results may be partial."
                )
        elif self.spec.lang_key == "python":
            self._initial_scan_done.wait(timeout=min(timeout, 3.0))

    def ensure_file_synced(self, file_path: Path) -> None:
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")

        with self._op_lock:
            for open_path in list(self._open_docs.keys()):
                if open_path == resolved:
                    continue
                if not open_path.is_file():
                    self._open_docs.pop(open_path, None)
                    self.notify("textDocument/didClose", {"textDocument": {"uri": path_to_uri(open_path)}})
                else:
                    self._sync_single_file(open_path)

            if self.spec.lang_key == "typescript" and not self._js_preopened:
                self._js_preopened = True
                self._maybe_preopen_unconfigured_js()

            was_first_open = len(self._open_docs) == 0
            self._sync_single_file(resolved)

            if was_first_open and self.spec.lang_key in ("typescript", "cpp", "swift", "python"):
                self._wait_initial_ready(timeout=12.0, start_grace=0.5)

    def _sync_single_file(self, resolved: Path) -> None:
        st = resolved.stat()
        prev = self._open_docs.get(resolved)
        if prev is not None and prev.mtime_ns == st.st_mtime_ns and prev.size == st.st_size:
            return

        text = resolved.read_text(encoding="utf-8", errors="replace")
        uri = path_to_uri(resolved)
        if prev is None:
            lang_id = language_id_for_path(resolved, self.spec.lang_key)
            self._open_docs[resolved] = _OpenDocState(
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
                version=1,
                language_id=lang_id,
            )
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": lang_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
        else:
            next_ver = prev.version + 1
            prev.mtime_ns = st.st_mtime_ns
            prev.size = st.st_size
            prev.version = next_ver
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": next_ver},
                    "contentChanges": [{"text": text}],
                },
            )

    def _maybe_preopen_unconfigured_js(self) -> None:
        if (self.workspace_root / "tsconfig.json").is_file() or (self.workspace_root / "jsconfig.json").is_file():
            return
        js_files: list[Path] = []
        overflow = False
        for root, dirs, files in os.walk(self.workspace_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in self.spec.extensions:
                    fpath = Path(root) / fname
                    try:
                        if fpath.stat().st_size <= MAX_PREOPEN_FILE_BYTES:
                            js_files.append(fpath.resolve())
                            if len(js_files) > MAX_PREOPEN_JS_FILES:
                                overflow = True
                                break
                    except OSError:
                        continue
            if overflow:
                break

        for fpath in js_files[:MAX_PREOPEN_JS_FILES]:
            try:
                self._sync_single_file(fpath)
            except OSError:
                continue

        if overflow:
            self.warnings.append(
                f"Workspace {self.workspace_root} has >{MAX_PREOPEN_JS_FILES} JS/TS files without tsconfig.json or "
                "jsconfig.json; add a jsconfig.json/tsconfig.json to guarantee full cross-file caller coverage."
            )

    def close(self) -> None:
        with self._op_lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is None:
                try:
                    self.request("shutdown", None, timeout=2.0)
                    self.notify("exit", None)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass


class LspManager:
    """Pool of lazy-started, idle-evicted LSP sessions keyed by (workspace_root, lang_key, python_path)."""

    def __init__(self, idle_timeout_sec: float = IDLE_TIMEOUT_SEC) -> None:
        self.idle_timeout_sec = idle_timeout_sec
        self._sessions: dict[tuple[Path, str, Path | None], LspSession] = {}
        self._lock = threading.Lock()
        atexit.register(self.close_all)

    def get_session_for_file(
        self,
        file_path: Path,
        explicit_workspace: str | None = None,
        explicit_python_path: str | None = None,
    ) -> LspSession:
        resolved = file_path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Target file does not exist: {resolved}")
        spec = spec_for_file(resolved)
        ws_root = detect_workspace_root(resolved, spec, explicit_workspace)
        py_path = (
            resolve_python_interpreter(ws_root, explicit_python_path, resolved)
            if spec.lang_key == "python"
            else None
        )
        session = self._get_or_start(ws_root, spec, py_path)
        session.ensure_file_synced(resolved)
        return session

    def _get_or_start(self, ws_root: Path, spec: LanguageSpec, py_path: Path | None = None) -> LspSession:
        self._evict_idle()
        key = (ws_root, spec.lang_key, py_path)
        evicted_lru: list[LspSession] = []
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.is_alive():
                existing.last_used = time.monotonic()
                return existing
            if existing is not None:
                self._sessions.pop(key, None)

            same_ws_lang = [
                (k, s) for k, s in self._sessions.items() if k[0] == ws_root and k[1] == spec.lang_key
            ]
            if len(same_ws_lang) >= MAX_SESSIONS_PER_WORKSPACE_LANG:
                same_ws_lang.sort(key=lambda pair: pair[1].last_used)
                for old_key, old_sess in same_ws_lang[: len(same_ws_lang) - MAX_SESSIONS_PER_WORKSPACE_LANG + 1]:
                    self._sessions.pop(old_key, None)
                    evicted_lru.append(old_sess)

            session = LspSession(spec, ws_root, python_path=py_path)
            self._sessions[key] = session

        for old_sess in evicted_lru:
            old_sess.close()

        try:
            session.start()
        except Exception:
            with self._lock:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
            session.close()
            raise
        return session

    def _evict_idle(self) -> None:
        now = time.monotonic()
        stale: list[LspSession] = []
        with self._lock:
            for key, sess in list(self._sessions.items()):
                if not sess.is_alive() or (now - sess.last_used) > self.idle_timeout_sec:
                    stale.append(self._sessions.pop(key))
        for sess in stale:
            sess.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            sess.close()
