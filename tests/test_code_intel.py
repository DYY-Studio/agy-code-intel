import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lsp_manager import (
    path_to_uri,
    py_col_to_utf16,
    resolve_python_interpreter,
    uri_to_path,
    utf16_to_py_col,
)


class UnitHelpersTest(unittest.TestCase):
    def test_python_interpreter_resolution_and_validation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="code_intel_venv_") as tmp:
            ws = Path(tmp).resolve()
            sub_py = ws / "plugins" / "ocr" / ".venv" / "bin" / "python"
            sub_py.parent.mkdir(parents=True)
            sub_py.symlink_to(sys.executable)
            target = ws / "plugins" / "ocr" / "worker.py"
            target.write_text("x = 1\n", encoding="utf-8")

            # Preserves symlink path inside sub-plugin .venv rather than dereferencing to system python
            auto_py = resolve_python_interpreter(ws, None, target)
            self.assertEqual(auto_py, sub_py)
            # Explicit venv directory resolves to bin/python inside it
            self.assertEqual(resolve_python_interpreter(ws, str(sub_py.parent.parent)), sub_py)
            # Rejects non-python executable names
            bad = ws / "not_python.sh"
            bad.write_text("#!/bin/sh\n", encoding="utf-8")
            bad.chmod(0o755)
            with self.assertRaises(ValueError):
                resolve_python_interpreter(ws, str(bad))
    def test_utf16_column_roundtrip_with_cjk_and_emoji(self) -> None:
        line = 'prefix = "中文😀" + target_fn(x)'
        py_idx = line.index("target_fn")
        u16 = py_col_to_utf16(line, py_idx)
        # Emoji 😀 takes 2 UTF-16 code units, so u16 must be py_idx + 1.
        self.assertEqual(u16, py_idx + 1)
        self.assertEqual(utf16_to_py_col(line, u16), py_idx)

    def test_uri_roundtrip_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="code intel test ") as tmp:
            f = Path(tmp) / "sub dir" / "mod.py"
            f.parent.mkdir(parents=True)
            f.write_text("x = 1\n", encoding="utf-8")
            uri = path_to_uri(f)
            self.assertTrue(uri.startswith("file://"))
            self.assertEqual(uri_to_path(uri), f.resolve())


@unittest.skipUnless(
    shutil.which("basedpyright-langserver", path=f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin"),
    "basedpyright-langserver not installed",
)
class EndToEndMcpFlowTest(unittest.TestCase):
    def test_callers_dispatch_refs_and_live_did_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="code_intel_py_") as tmp:
            ws = Path(tmp).resolve()
            (ws / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
            core = ws / "core.py"
            api = ws / "api.py"
            core.write_text(
                "def leaf_calc(n: int) -> int:\n"
                "    return n * 2\n\n"
                "def middle_step(v: int) -> int:\n"
                "    return leaf_calc(v) + 1\n",
                encoding="utf-8",
            )
            api.write_text(
                "from core import leaf_calc, middle_step\n\n"
                "DISPATCH = {'leaf': leaf_calc}\n\n"
                "def entry_handler(x: int) -> int:\n"
                "    return middle_step(x)\n",
                encoding="utf-8",
            )

            mcp_cfg = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
            srv = mcp_cfg["mcpServers"]["mcp"]
            proc = subprocess.Popen(
                [srv["command"], *srv["args"]],
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            assert proc.stdin is not None and proc.stdout is not None
            req_id = 0

            def rpc(method: str, params: dict | None = None) -> dict:
                nonlocal req_id
                req_id += 1
                msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
                if params is not None:
                    msg["params"] = params
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                self.assertTrue(line, f"Unexpected EOF on {method}")
                return json.loads(line)

            try:
                rpc("initialize", {"protocolVersion": "2024-11-05"})
                res = rpc(
                    "tools/call",
                    {
                        "name": "find_callers",
                        "arguments": {"file": str(core), "symbol": "leaf_calc", "depth": 2},
                    },
                )["result"]
                self.assertFalse(res["isError"], res)
                text = res["content"][0]["text"]
                # Verify 2-hop call hierarchy (leaf_calc <- middle_step <- entry_handler)
                self.assertIn("middle_step", text)
                self.assertIn("[depth 2]", text)
                self.assertIn("entry_handler", text)
                # Verify non-call dispatch dict reference in api.py
                self.assertIn("DISPATCH = {'leaf': leaf_calc}", text)

                # Modify api.py on disk and verify mtime-triggered didChange picks up the new caller
                time.sleep(0.02)
                api.write_text(
                    "from core import leaf_calc, middle_step\n\n"
                    "def brand_new_caller() -> int:\n"
                    "    return leaf_calc(99)\n",
                    encoding="utf-8",
                )
                res2 = rpc(
                    "tools/call",
                    {
                        "name": "find_callers",
                        "arguments": {"file": str(core), "symbol": "leaf_calc", "depth": 1},
                    },
                )["result"]
                self.assertFalse(res2["isError"], res2)
                text2 = res2["content"][0]["text"]
                self.assertIn("brand_new_caller", text2)
            finally:
                proc.stdin.close()
                proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
