# Changelog

All notable changes to the **ipa-rostooling-studio** toolchain and skills will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
