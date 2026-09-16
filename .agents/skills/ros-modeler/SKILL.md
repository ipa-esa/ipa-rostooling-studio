---
name: ros-modeler
description: Pre-flight checklist of the 12 rules that most often silently corrupt a RosTooling .ros2/.rossystem model (wrong quoting, wrong member order, silently-mistyped booleans/doubles, illegal nesting). Use immediately before emitting or editing such a file, IN ADDITION TO the `ros-model` skill, never instead of it — this is a checklist, not the specification.
---

You author RosTooling DSL files for the Fraunhofer IPA ROS 2 modelling toolchain. Two file
types are in scope: `.ros2` (an `AmentPackage` — one ROS package, its artifacts, nodes and
interfaces) and `.rossystem` (a system composition wiring node instances together).

**Before emitting or editing anything, read `.agents/skills/ros-model/SKILL.md` and the files
in its `references/` folder.** That skill carries the pinned grammar subset, the validator
rule table and the normative emission profile; it is the authority for anything not restated
here. This file is a pre-flight checklist of the rules most likely to silently corrupt a
model, not a substitute for reading the specification. Do not reason from general ROS or YAML
intuition — these grammars are indentation-sensitive Xtext grammars that only superficially
resemble YAML.

## Non-negotiable rules

These cause hard errors or silent semantic corruption. Never violate them.

1. Indentation is structural. Spaces only, exactly 2 per nesting level, LF endings, one
   trailing newline. Never a tab, never CRLF, never a blank line inside an indented block.
2. The `.ros2` package name on line 1 must be lowercase `[a-z0-9_]`. Any uppercase letter is an
   **ERROR** (`checkNameConventionsPackage`) — the only ERROR-severity naming rule in the
   toolchain. Node and artifact names should be lowercase too, but there it is only a warning,
   so do not mangle an upstream name like `gazebo_sensor_B1_controller` to satisfy it.
3. Package, artifact and `node:` names are `RosNames`, which has no `STRING` alternative —
   emit them **bare**, never quoted. Everything else that is an `EString` and contains `.`,
   `/`, `::`, `-`, a space, or is empty **must** be quoted.
4. Booleans are lowercase `true`/`false` only. Uppercase `True` does not error — it falls
   through the `ParameterValue` alternation into `ParameterString`, silently giving a `Boolean`
   parameter a string value. `Double` values need a `.` or exponent; a bare `10` silently
   becomes an Integer.
5. Prefer not to emit `lease_duration:`, `liveliness:`, `lifespan:` or `deadline:` -- LEGAL since
   the pin was lifted 2026-07-21, but no corpus model uses them and older toolchain builds
   cannot lex them. If you emit a duration it is NANOSECONDS, signed-32-bit max (~2.147 s), or
   `infinite`.
6. Never emit `msgs:`, `srvs:` or `actions:` inside a `.ros2` file. `AmentPackage` is the entry
   rule there and admits only `fromGitRepo:` / `artifacts:` / `dependencies:`. Message specs
   live in `.ros` files.
7. Grammar-fixed member order: `AmentPackage` is `fromGitRepo:` → `artifacts:` →
   `dependencies:`; a `.rossystem` node is `from:` → `namespace:` → `interfaces:` →
   `parameters:`; an interface is `type:` → `ns:` → `qos:`; a `.ros2` parameter is `type:` →
   `ns:` → `value:` → `qos:`. Violating any of these is a parse error.
8. Connections: `from` is always the server/publisher. Only `pub->`/`sub->`, `ss->`/`sc->`,
   `as->`/`ac->` pair legally, and both endpoints must carry byte-identical `type:` strings —
   the validator compares object identity, not type names.
9. `from:` in a `.rossystem` is `<packageName>.<nodeName>` — the **node**, never the artifact —
   read from the target model's contents, never from the `.ros2` filename (these differ in 76 of
   253 corpus files). In the same file, arrow and parameter targets are
   `<artifactName>::<name>` — the **artifact**. Two opposite levels; do not unify them.
10. Always emit `fromFile:` in a `.rossystem`, quoted and containing at least one `/`. This is
    bug avoidance, not a real rule: `fromFileHelper` dereferences `fromFile` without a null
    guard, so omitting it throws inside the validator.
11. Every `type:`/`from:` reference that resolves against the vendored catalogues
    (`assets/type_index.json` / `assets/node_index.json`) gets a trailing comment naming the
    exact file it resolved to (`# assets/rosmodelscatalog/navigation/amcl.ros2`); every
    project-local one gets a comment saying so and pointing at its companion `.ros`/`.ros2`.
    Never leave a reader to guess which lines are real catalogue nodes and which are files this
    session wrote. `rosmodel_lint.py`'s RM088/RM089 check this mechanically.
12. `from:` reuses one node's implementation; `subSystems:` reuses a whole pre-built
    `.rossystem` composition — do not fake the latter by re-declaring, one by one, nodes that
    a catalogued system already composes. But check first: a subsystem's connectable interfaces
    are exactly what its own `nodes:` block declares under `interfaces:`, never derived from the
    `.ros2` its `from:` points at. `assets/node_index.json`'s `_systems` entries record this per
    node — a system where every node's `"interfaces"` is empty (e.g. the vendored
    `turtlebot3_navigation2.rossystem`) exposes nothing through `subSystems:`, and its nodes stay
    explicit `nodes:` declarations, with a comment saying why. Never let one node be reachable
    both directly under `nodes:` and through a `subSystems:` entry in the same file.
    `rosmodel_lint.py`'s RM090 (ERROR: label collision)/RM091 (unresolved, nested, or
    zero-interface target)/RM092 (same `from:`, different label) check this mechanically.

## Things to route around

Emit only scalar parameter values (Integer, Boolean, Double, String). List, Array and Struct
values reach a recursive type-checker that uses instance fields as loop counters and produces
non-deterministic diagnostics. Avoid `dependencies:`, `namespace:`, `ns:`, `processes:` and
**nested** subsystems (a `subSystems:` entry that itself resolves to a system which has its own
`subSystems:` block) — `checkIfInterfaceInSystem` casts unconditionally to `RosNode` one level
down, so two levels of nesting throws `ClassCastException` in the real validator (RM091 catches
it). A single, flat `subSystems:` entry is fine and tested — see rule 12. Use only the
`RosSystemConnection` form `- [from_iface , to_iface]`; the `RosConnection` branch throws
`ClassCastException` in three separate checks. Do not emit comments by default.

## Verification posture

`.ros2` files get a real but stale oracle — the shipped Xtext language server, pinned to a
2024-08-01 build. If it is running, treat its diagnostics as authoritative for `.ros2` and fix
what it reports.

`.rossystem` has **no language server at all** — none was ever built. Its only check is
`scripts/rosmodel_lint.py`, a modelled reimplementation of `RosSystemValidator`, not the real
thing. In Claude Code that runs automatically after every write via a `PostToolUse` hook;
nothing here re-runs it for you, so run it yourself
(`"${ROSMODEL_PYTHON:-python3}" scripts/rosmodel_lint.py FILE...`) after writing or editing a
model, and again after fixing anything it reports — a blocked check means the bad content is
still on disk until a follow-up edit clears it.

Never claim a file has been validated against the real toolchain unless you have actually seen
diagnostics come back. Say "emitted per the pinned profile, not executed" instead.

## Working style

Read before writing — check whether the target file, and any `.ros2` file a `.rossystem`
references, already exists. Prefer an in-place edit over a full rewrite on existing files so
you do not silently drop content. When information needed to fill a field is genuinely absent,
ask rather than invent a topic name, message type or package name.
