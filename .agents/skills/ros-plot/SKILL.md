---
name: ros-plot
description: Render RosTooling .rossystem models as one self-contained, interactive HTML file (four abstraction levels, draggable graph, no network) via scripts/ros_plot.py. Invoke with file.rossystem path(s), --out PATH, --recursive and/or --open as $ARGUMENTS.
---

## What this skill does

Visualises one or more `.rossystem` models as a single standalone HTML file — vanilla JS,
no external libraries, opens directly as a local `file://` with no network access. It reuses
the validated parser in `scripts/rosmodel_lint.py`, so what it draws is exactly what the model
declares; it never synthesises a connection the file did not.

The page offers four abstraction levels (keys **1–4**, or the `System │ Interfaces │ Full │
Deps` switch): declared connections, port-to-port interfaces, full interface/parameter detail,
and a bipartite dependency view resolved against the vendored catalogue
(`assets/node_index.json`). Boxes are draggable, click a box to isolate its connections, the
legend toggles and filters by interaction kind, and there is a manual light/dark theme toggle.

## How to run it

Shell out to the script from the workspace root, passing the caller's arguments straight
through. `ROSMODEL_PYTHON` is an optional override for machines where bare `python3` is not
the right interpreter:

```bash
"${ROSMODEL_PYTHON:-python3}" scripts/ros_plot.py $ARGUMENTS
```

- With **no file arguments**, the script globs `*.rossystem` in the current directory
  (non-recursively). Add `--recursive` to search subdirectories. If nothing is found it prints
  a friendly message naming where it looked — relay that to the user rather than guessing.
- Multiple input files produce **one HTML with a tab per file**; a single file hides the tab
  bar. `subSystems` references cross-link to another tab when that system is among the inputs.
- Output defaults to `ros-plot.html` next to the first input; override with `--out PATH`.

## Reporting back

1. **Report the printed path.** The script prints the absolute path of the generated HTML to
   stdout. Surface it to the user verbatim.
2. **Do NOT auto-open the file.** Opening is opt-in only, via the `--open` flag, and only when
   the user asked for it. Never add `--open` on your own.
3. **Surface diagnostics verbatim.** Any diagnostics the script prints to stderr (dangling
   connection endpoints, ambiguous names resolved to the first match, a `connections:` block
   recovered by regex fallback, a file that could not be composed, catalogue unavailable) are
   **model facts**, not warnings to soften or hide. Pass them through as-is.

## Non-goals

This skill does not lint or fix models — that is `scripts/rosmodel_lint.py`. In Claude Code
that runs automatically on every write via a `PostToolUse` hook; nothing here re-runs it
automatically, so run it yourself (`"${ROSMODEL_PYTHON:-python3}" scripts/rosmodel_lint.py
FILE...`) if a model may have changed since it was last checked. This skill reads models and
emits a view; it never edits a `.rossystem` file and never invents content that is not
declared in it.
