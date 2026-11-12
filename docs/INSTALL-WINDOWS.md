# Windows 11 installation and conformance

1. Unpack into a separate repository or intended `C:\\test-repos\\quant-analysis` after checking existing files and dirty state. This ZIP is **not** an in-place merge script; it contains files absent from the original archive. Do not overwrite existing source without reviewing the delta.
2. In PowerShell, from the package root, run `py -3.12 -m venv .venv` and `.\\.venv\\Scripts\\python.exe -m pip install -e .` (online pinned MCP SDK dependency). For local backtest/indicator/fetch commands install `.\.venv\Scripts\python.exe -m pip install -e ".[quant]"`; to run all tests use `-e ".[test]"` instead. For stronger reproducibility generate and check in a fully resolved hashes/lockfile in the target environment before production. This package pins MCP direct dependency, **not** transitives.
3. Run `.\\.venv\\Scripts\\python.exe scripts/verify_control_plane.py` and `.\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v`.
4. Add to **user** `~/.codex/config.toml` after verifying absolute path:

```toml
[mcp_servers.quant_free_local]
command = "C:\\test-repos\\quant-analysis\\.venv\\Scripts\\python.exe"
args = ["-m", "quant_free_mcp"]
startup_timeout_sec = 30.0
tool_timeout_sec = 45.0
```

5. Run `codex --version`, `codex mcp list`; verify an actual tool list and benign tool invocation, plus a region-appropriate live read. Run `codex doctor --network-only` when supported. Verify profile/approval behavior, denied destinations and read-only contracts before reliance.
6. Do not copy `.codex/config.toml` over global config. Global file is preserved as `docs/global-config-original.toml` and the recommended extra MCP stanza is above. Existing remote servers may be available, disabled, paid, credentialed, or non-read-only. Their behavior is not changed or certified by this package.

## Recovered quantitative implementation
The three original Skills now have their matching source files and tests in `src/` and `tests/`. They are called as `python src/market_data.py`, `python src/indicators.py`, and `python src/backtest.py` from the package root. Preserve the verified CSV `.meta.json` sidecar between stages. Public live data, Windows compatibility and the Codex MCP handshake still require checks on the target machine. The local MCP adapter provides point-in-time and bounded historical API pulls, not continuous stream reconstruction or a full retrospective trade tape.
