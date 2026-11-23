# Changelog

All notable changes to the **ipa-rostooling-studio** toolchain and skills will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **`rosmodel_lint.py` no longer copies the grammar or the validator.** 34 rules that did were retired (`RETIRED_RULES`; table in `scripts/README.md`): the RosTooling language server reports every case of them (`tests/fixtures/server_coverage`), and `--server` asks it (`RM100`). What stays is what the server does not check: provenance, catalogue and subsystem reuse, style, and values that parse but change meaning. `RM033` keeps only its negative-QoS-depth warning and `RM039` only its order warnings. The linter reads the Phase 4 plugin syntax (`pluginbases:`, `implements: <package>.<Base>`, `slots:`) without errors; judging it is the server's.
- **`ros_plot.py`** reads `slots:` and shows each node's plugins per slot.
- **Agent skills** (`ros-model`, `ros-modeler`, `ros-system-builder`, `ros-node-developer`) describe the new plugin syntax and the `ros2_add_plugin_base` and `ros_attach_plugins` MCP tools.

### Added
- **`rosmodel_lint.py --server {auto,require,off}`** (default `auto`): asks the real RosTooling language server once per run and reports its diagnostics as `RM100`; a run that did not ask it says so with `RM101` (INFO, WARNING or ERROR by mode). The Java/jar discovery and LSP client moved from `tests/oracle/ask_oracle.py` to `scripts/ros_server.py`, which the oracle CLI now wraps. The hook and the extractors pass `--server off`.
- `tests/fixtures/server_coverage/`: one case per retired check, with the language server's finding for each.
- `tests/fixtures/plugins_phase4/`: 27 plugin-syntax cases with the language server's verdicts; a test holds the linter to the same verdicts.

## [0.3.0] - 2026-11-18

### Added
- **Generic `@` Extension Block Support**:
  - Supported parsing and linting generic `@<name>:` extension blocks in `rosmodel_lint.py` and `ros_plot.py`.
  - Updated agent skill guides (`ros-model`, `ros-modeler`) to document generic `@` extension blocks and typed plugin MCP tools.

## [0.2.2] - 2026-11-13

### Changed
- **Semantic MCP-First Agent Skills**:
  - Updated `ros-model`, `ros-modeler`, `ros-system-builder`, `ros-node-developer`, and `ros-plot` under `.agents/skills/` (plus `skills/ros-model/SKILL.md`) to author and modify `.ros`, `.ros2`, and `.rossystem` models via `rostooling-mcp` (`ros_add_spec`, `ros2_*`, `ros_*`) rather than synthesizing DSL text and validating afterward.
  - Designated `validate_ros_model` (backed by the Java Xtext language server) as the authoritative model validator and `rosmodel_lint.py` as secondary/offline provenance checking.
  - Replaced machine-specific `file://` cross-skill links in `ros-system-builder` and `ros-node-developer` with portable relative paths.

## [0.2.1] - 2026-11-06

### Added
- **ROS 2 Launch Substitution Validation (Rule RM098)**:
  - Added rule `RM098` in `rosmodel_lint.py` to validate syntax and balanced parentheses for launch substitution expressions (`$(find-pkg-share ...)`, `$(var ...)`, `$(command ...)`) within parameter string values.
  - Documented rule `RM098` in `scripts/README.md` and added substitution syntax guidelines in `skills/ros-model/`.
- **Flexible Connection Delimiters**:
  - Supported semicolon separators in rossystem connection definitions (`- [from; to]`, `- from; to`, and trailing semicolons).
- **Subsystem Connection Endpoint Resolution**:
  - Handled subsystem prefix resolution for connection endpoints in composite systems.
  - Silenced `RM053` (`fromFile:` missing) for systems targeted for launch file generation.

## [0.2.0] - 2026-10-31

### Added
- Initial packaging via `pyproject.toml` with hatchling build backend.
- CLI entry points for `rosmodel-lint`, `ros-studio`, `ros-plot`, `extract-ros2-interfaces`, and `coresense-studio`.
