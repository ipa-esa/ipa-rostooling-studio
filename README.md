# rostooling-modeler

A Claude Code plugin that lets an AI agent generate and check **RosTooling** model files — `.ros2`
(an `AmentPackage`: one ROS 2 package, its artifacts, nodes and interfaces), `.rossystem` (a system
composition wiring node instances together) and `.ros` (message / service / action type
definitions) — for the Fraunhofer IPA ROS 2 modelling toolchain.

Work item **E1**, CoreSense x Humanoid.

The problem it solves: these are **indentation-sensitive Xtext DSLs that look like YAML and are
not**. Indentation is lexed into mandatory `BEGIN`/`END` tokens, `EString` positions must be quoted
while `RosNames` positions must never be, and several constructs corrupt the model *silently*
rather than failing loudly (`value: True` under `type: Boolean` parses as a string; `value: 10`
under `type: Double` parses as an integer). An agent reasoning from YAML or general ROS intuition
gets these wrong every time. The plugin replaces intuition with a transcribed grammar subset, a
validator rule table and an enforced emission profile.

---

## Two tools live in this repository

Read this before anything else, because both are called "studio" and they are not the same thing.

| | What it is | Start it with |
|---|---|---|
| **The plugin** (everything below) | A Claude Code plugin: skill, linter, hooks, language servers, `/ros-plot` (a self-contained, read-only `.rossystem` viewer), and `ros_studio.py`, the deterministic generation/validation engine CoreSense Studio itself is built on. | `/plugin install`, or `py scripts/ros_studio.py …` |
| **CoreSense Studio** | A **local HTTP service** with a project store on disk, a React front end, linked ROS 2 source repositories, and model checking. | `python scripts/studio_server.py --storage-root ~/ros-studio --port 0` |

CoreSense Studio covers the project brief, the Architecture model graph, extracting components
from real ROS 2 source repositories, and checking a model. It does **not** do deployment,
containerization, live ROS telemetry or robot simulation — that is a separate, confidential tool.

It imports `ros_studio.py` as its generation and validation engine, so both halves emit through the
same emitter and the same linter. Full documentation: **[`docs/coresense-studio.md`](docs/coresense-studio.md)**.

Two things that trip people up on first contact, repeated here so they are not buried:

- The front end's React half is **built**. Changes to `web/studio/frontend/src/*.jsx` do nothing
  until you run `npm run build` in `web/studio/frontend/`. The plain scripts next to it
  (`web/studio/app.js` and friends) need no build.
- "Resolve with coding agent" shells out to a coding-agent CLI **already installed on your
  machine** — `codex`, `claude` or `gemini` on `PATH`. There is no API key and no bundled agent.
  With none of them installed the feature fails cleanly with a message saying so, and the rest of
  the app is unaffected.

---

## Everything also works in Google Antigravity

`.agents/skills/` mirrors every skill/command/subagent this plugin has for Claude Code, in
Antigravity's format:

| Antigravity skill | Ported from | What changed |
|---|---|---|
| `.agents/skills/ros-model/` | `skills/ros-model/` | `argument-hint:` folded into `description` (not part of Antigravity's schema); every `${CLAUDE_PLUGIN_ROOT}` path made workspace-relative; the `references/type-catalogue.md`/`node-catalogue.md` mentions marked explicitly as the repo-root `references/` folder, not this skill's own (Antigravity gives `references/` inside a skill folder an official meaning, which this repo's layout happens to collide with); an explicit "run the linter yourself" note added, replacing an implication that a `PostToolUse` hook auto-enforces it |
| `.agents/skills/ros-plot/` | `commands/ros-plot.md` | `${CLAUDE_PLUGIN_ROOT}` (Claude-only) replaced with a workspace-relative path; `allowed-tools:` dropped (not part of Antigravity's schema) |
| `.agents/skills/update-ros-catalog/` | `commands/update-ros-catalog.md` | the "launch a cheap `Agent` sub-call with `model: haiku`" step, which is Claude Code's own subagent-spawning mechanism, replaced with a plain instruction to fetch the sources directly, plus a separate "Cost note" telling whoever is steering that this task is cheap enough to hand to a smaller model or sub-agent if one is available — not a conditional inside the steps themselves, which an agent has no way to evaluate |
| `.agents/skills/ros-modeler/` | `agents/ros-modeler.md` | Claude subagent-only frontmatter (`model`, `effort`, `maxTurns`, `tools`, `skills`) dropped — Antigravity skills only take `name`/`description`. Without those fields this skill lost its whole reason to exist as a *separate* thing (a locked-down tool list, a cheaper model, its own context) and its description started overlapping `ros-model`'s enough that Antigravity's auto-discovery could plausibly pick either for the same prompt — so the `description` was narrowed to "pre-flight checklist, use *in addition to* `ros-model`, never instead of it," and the body now opens with an explicit instruction to read `ros-model`'s `SKILL.md` first, rather than asserting it's "preloaded" the way the Claude subagent guaranteed |

Antigravity's `SKILL.md` format (frontmatter `name` + `description`, a `references/`
subfolder, `$ARGUMENTS` substitution, `/<skill-name>` as an explicit slash command) is the
same standard Claude Code uses, so none of the rule content needed rewriting — only the
Claude-specific plumbing (env vars, tool-restriction fields, subagent frontmatter) that has no
Antigravity equivalent. Antigravity discovers skills at
`<workspace-root>/.agents/skills/<name>/SKILL.md` (the currently-recommended path; it also
still reads the older singular `.agent/skills/`, but new work should target the plural form).

Two things this does **not** cover, on purpose:

- **The auto-lint hook.** In Claude Code, `hooks/hooks.json` fires `rosmodel_lint.py` after
  every write to a `.ros2`/`.rossystem` file automatically. Antigravity has no equivalent
  auto-run mechanism, so every ported skill that touches a model file — including `ros-model`
  itself — now says explicitly to run the linter by hand.
- **Workflows.** Antigravity has a separate Workflow mechanism (`*.md` files, most resembling
  Claude commands) that is being retired in favor of Skills on 2026-11-01 — so the two
  commands were ported as Skills (above), not Workflows, deliberately.
- **Using this outside this repo.** Every ported skill resolves `scripts/…` and `assets/…`
  relative to the open workspace's root, the same way the Claude Code versions resolve them
  relative to the plugin root. That's fine as long as the open Antigravity workspace *is* this
  repo. Copying just `.agents/skills/` into some other ROS project gives that workspace a
  skill whose extractors, linter and catalogue indexes are missing — it would need `scripts/`
  and `assets/` vendored alongside it too.

Two copies of each file is a real drift risk: an edit to, say, `skills/ros-model/SKILL.md`
will not automatically reach `.agents/skills/ros-model/SKILL.md`. There was no portable way
around that here — a symlink would need `core.symlinks` set up correctly on every clone, and
this repo is used from Windows too (see below).

---

## Prerequisites — read this first

| Requirement | Needed for | Status on this machine |
|---|---|---|
| **Python 3.8+** | linter, round-trip harness | ✅ 3.12.6 |
| PyYAML | structural checks (linter degrades gracefully without it) | ✅ 6.0.3 |
| **Java 21 (Temurin LTS)** | LSP validation of `.ros` / `.ros2` / `.rossystem` | ✅ Temurin 21.0.11 |

### ✅ There is a working oracle — use it

`tests/oracle/ask_oracle.py` drives the real RosTooling language servers over stdio and reports
their diagnostics. All 26 cases<!--@count:oracle_cases--> currently behave as documented; the
rejections are negative controls that confirm the hard exclusions, plus three that settled the
`from:` reference form. The per-case verdicts live with the runs that produced them, in
[`tests/oracle/RESULTS.md`](tests/oracle/RESULTS.md).

```bash
py tests/oracle/ask_oracle.py --all
```

Two things worth knowing:

- **The shipped `ros2` JAR validates `.ros` files too.** It registers `RosIdeSetup` and
  `BasicsIdeSetup` alongside `Ros2IdeSetup`, so message specs get real diagnostics, not just
  packages.
- **`.rossystem` has no *shipped* server, but one has been built from source** at
  `build/rossystem-ls/`, and `ask_oracle.py` selects it per case by file extension. Ten case
  directories now carry a `.rossystem` and have been through it — the 52 corpus systems still
  have not.

One limit that no JDK lifts:

- **The oracle is stale.** The shipped `ros2` JAR was built **2024-08-01**; RosTooling HEAD added
  `lease_duration` / `liveliness` / `lifespan` / `deadline` on 2025-10-16, *after* that build, so
  the JAR cannot even lex them. The project deliberately **pins the emission profile to the
  JAR-accepted subset** and never emits those four fields. Cost of the exclusion is zero — 0 of 253
  corpus `.ros2` files use any of them — and because the JAR's token set is a strict subset of
  HEAD's, output valid under the pinned profile is also valid at HEAD.

---

## Installing

The plugin is self-hosting: its repo is also a Claude Code marketplace
(`.claude-plugin/marketplace.json`), so `/plugin install` works directly.

**Persistent install** (survives across sessions):

```bash
# add this repo as a marketplace (local path, git URL, or GitHub owner/repo all work)
claude plugin marketplace add C:/Users/mae/Downloads/adm-mae/CoreSense/rostooling-plugin
claude plugin install rostooling-modeler@rostooling-modeler-marketplace
```

**Single-session dev load** (no marketplace, no install):

```bash
claude --plugin-dir C:/Users/mae/Downloads/adm-mae/CoreSense/rostooling-plugin
```

Validate the manifest at any time:

```bash
claude plugin validate C:/Users/mae/Downloads/adm-mae/CoreSense/rostooling-plugin
```

### Two environment variables you must set

The plugin bundles its own language-server JAR (`build/ros2-ls/target/…-ls.jar`, referenced via
`${CLAUDE_PLUGIN_ROOT}` so it travels with the plugin) but it cannot bundle a JDK or a Python
interpreter. Both `.lsp.json` and `hooks/hooks.json` read an env var with a POSIX default, so on a
machine where the default resolves correctly you need to set nothing. On **this** machine both
defaults are wrong, so set both:

1. **`ROSMODEL_JAVA`** — the LSP command is `"${ROSMODEL_JAVA:-java}"`. Bare `java` on `PATH` here is
   1.8 and the JARs need JDK 19+, so the server would start under Java 8 and die. Point it at
   Temurin 21:

   ```
   ROSMODEL_JAVA=C:\Users\mae\AppData\Local\Programs\Eclipse Adoptium\jdk-21.0.11.10-hotspot\bin\java.exe
   ```

2. **`ROSMODEL_PYTHON`** — the hook command is `"${ROSMODEL_PYTHON:-python3}" .../rosmodel_lint.py`.
   Bare `python`/`python3` here is the Microsoft Store stub, which prints a "not found" notice and
   exits non-zero — the hook would then silently never lint. Point it at the real interpreter:

   ```
   ROSMODEL_PYTHON=C:\Users\mae\AppData\Local\Programs\Python\Python312\python.exe
   ```

On Linux/macOS the defaults (`java`, `python3`) are usually correct, provided `java -version`
reports 19+.

---

## Component layout

```
.claude-plugin/plugin.json      manifest
.lsp.json                       ros2 + ros language servers (auto-discovered at plugin root)
hooks/hooks.json                PostToolUse linter for .ros2 and .rossystem (auto-discovered)
commands/update-ros-catalog.md  slash command: refresh popular-ROS-packages reference (cheap agent)
commands/ros-plot.md            slash command: render .rossystem models as one interactive HTML file (read-only)
agents/ros-modeler.md           subagent, preloads the ros-model skill
skills/ros-model/
  SKILL.md                      entry point: decision tree, hard rules, self-check
  references/ros2-syntax.md     .ros2 + .ros grammar, verbatim productions
  references/rossystem-syntax.md .rossystem grammar + RosSystemValidator behaviour
  references/worked-examples.md  3 full transformations, failure catalogue, source-defect policy
.agents/skills/                 every skill/command/subagent above, in Antigravity's format (see below)
  ros-model/                    mirrors skills/ros-model/
  ros-plot/                     mirrors commands/ros-plot.md
  update-ros-catalog/           mirrors commands/update-ros-catalog.md
  ros-modeler/                  mirrors agents/ros-modeler.md
scripts/rosmodel_lint.py        static linter, 81 rules (.ros / .ros2 / .rossystem)
scripts/ros_plot.py             /ros-plot backend: .rossystem -> self-contained interactive HTML (read-only)
scripts/ros_studio.py           deterministic engine: init (seed) / generate (emit + lint, oracle) / diff
scripts/_studio_common.py       shared HTML/CSS/JS primitives + emit vocabulary (imported by both ros_plot and ros_studio)
scripts/README.md               rule reference + deviations + test evidence
scripts/studio_server.py        CoreSense Studio: the local HTTP service (routes live in _route)
scripts/studio_store.py         its project store: projects, revisions, evidence, recovery
scripts/studio_runtime.py       coding-agent handoff + the headless flag-resolution job system
scripts/studio_repositories.py  linking/fetching ROS source repos, *.repos deps, Git submodules
scripts/studio_source.py        static extraction of ROS contracts from source
scripts/studio_catalogue.py     the reusable component catalogue
web/studio/                     its front end: plain scripts + a built React bundle (dist/)
docs/coresense-studio.md        CoreSense Studio documentation — start here for the web app
tests/roundtrip.py              semantic round-trip harness
tests/studio_roundtrip.py       ros_studio.py seed -> generate: round-trip, orphan gate, planted-field checks
tests/fixtures/manifest.md      fixture inventory
tests/regenerated/              adversarial regeneration outputs (4 corpus targets)
tests/oracle/ask_oracle.py      drives the REAL language servers; 25 cases
tests/oracle/RESULTS.md         what the toolchain actually said
tests/docs_check.py             holds the PROSE to the code: cited paths, rule ids, marked counts
emission-profile.md             NORMATIVE formatting profile, 37 numbered rules
docs/grammar-subset.md          what the pinned JAR can lex; the HEAD-vs-JAR token diff
research/validator-rules.md     transcription of both *Validator.xtend files, with severities
```

`plugin.json` deliberately declares no component path fields — `.lsp.json` at the root,
`hooks/hooks.json`, `agents/` and `skills/` are all discovered by default, and adding explicit
paths would only create a second place to keep in sync.

### Document precedence

When these disagree, resolve in this order:

1. **The grammar itself** — `RosTooling/plugins/.../{Basics,Ros,Ros2,RosSystem}.xtext` and the two
   `*Validator.xtend` files, in the read-only reference clone. Always the authority.
2. `docs/grammar-subset.md` — what the *pinned* oracle can lex, which is narrower than the grammar.
3. `research/validator-rules.md` — semantic constraints and their real severities.
4. `emission-profile.md` — normative style.
5. `skills/ros-model/**` — the operational restatement of all of the above.

> **Note on paths.** The E1 briefing located the three specs under `spec/`. There is no `spec/`
> directory; they are at `docs/grammar-subset.md`, `research/validator-rules.md` and
> `emission-profile.md` (plugin root). Anything hardcoding the briefing's paths will fail.

---

## Running the linter standalone

```bash
python scripts/rosmodel_lint.py <file>...
python scripts/rosmodel_lint.py --json <file>...
python scripts/rosmodel_lint.py --min-severity ERROR <file>...
python scripts/rosmodel_lint.py --quiet "corpus/**/*.ros2"     # summary line only
```

**On Windows use `py -3`, not `python`** (Store stub, see above). Globs are expanded internally, so
quoted `**` patterns work regardless of shell. Exit status is `1` if any ERROR survives
`--min-severity` filtering, else `0`.

Accepts **`.ros`, `.ros2` and `.rossystem`**. `.ros` support was added 2026-07-21; it does not go
through YAML (PyYAML folds message field lines into a single scalar and loses every field) but
through a dedicated indentation-stack parser.

Full rule reference, deliberate deviations from the specs, and test evidence:
[`scripts/README.md`](scripts/README.md).

## Running the round-trip harness standalone

The harness compares two models **semantically** — it parses both to a canonical fact set and diffs
that. Byte-diffing against corpus originals is meaningless here: 26 of 51 corpus `.rossystem` files
contain literal tabs, and indent widths of 3, 4, 5, 6 and 7 spaces all occur. The corpus is a
*semantic* reference, not a style reference.

```bash
python tests/roundtrip.py compare <original> <regenerated>
python tests/roundtrip.py selftest <fixture>...
python tests/roundtrip.py order <file>...
```

**The subcommand is required** — `roundtrip.py <a> <b>` is an argparse error.

`compare` exits `1` on *any* semantic difference, including deviations the skill deliberately
mandates (an added `fromFile:`, a renamed duplicate node label). **A non-zero exit is not by itself
a failure** — read the diff.

`selftest` runs four checks per fixture: identity, text perturbation (whitespace/quote-style
changes must not alter the fact set), order perturbation (block reordering must not either), and a
negative control (a planted semantic change *must* be detected).

## Running both together

```bash
py -3 scripts/rosmodel_lint.py out/my_pkg.ros2 out/my_system.rossystem
py -3 tests/roundtrip.py compare corpus/my_pkg.ros2 out/my_pkg.ros2
```

## ros_studio.py — deterministic generation and validation

`/ros-plot` **reads** a `.rossystem`, read-only. `ros_studio.py` is the deterministic generation
and validation engine underneath CoreSense Studio (see above) — it seeds a `project.json`, emits
`.ros2` / `.rossystem` / companion `.ros` files from it, and validates the result. It has no UI of
its own; CoreSense Studio is the interface for authoring and checking a model.

```bash
# seed a project.json from an existing system (interface types from the sibling .ros2, message
# fields from the sibling .ros, comments from the source text)
py scripts/ros_studio.py init path/to/system.rossystem --out project.json
# ...several sources, or a whole tree, merge into ONE project (ids re-issued, colliding node
# labels renamed, a subSystems: ref to a system also being merged collapsed onto it):
py scripts/ros_studio.py init path/to/models/ --out project.json --name combined
# ...or start blank:  py scripts/ros_studio.py init --out project.json

# emit + validate:
py scripts/ros_studio.py generate project.json --outdir generated              # emits, lints, and asks
                                                                               # the real language server
                                                                               # when Java 19+ and the jar
                                                                               # are available -- and says
                                                                               # loudly when they are not
py scripts/ros_studio.py generate project.json --outdir generated --oracle     # REQUIRE the real server:
                                                                               # fail if it cannot be run
py scripts/ros_studio.py generate project.json --outdir generated --no-oracle  # RM rules only, no report

# what changed since the seed, at the model level (nodes, exposures, connections, parameters):
py scripts/ros_studio.py diff project.json
```

`generate` emits `.ros2` / `.rossystem` / companion `.ros` **deterministically** from the linter's
own grammar vocabulary (block names, arrow pairs, quoting), always runs `rosmodel_lint`, and with
`--oracle` stages catalogue dependencies (`collect_deps`) and asks the real server (`ask_oracle`).
On a generation-gate or lint ERROR nothing is written and the diagnostics are printed; on an oracle
rejection or an incomplete validation the reason is printed as a `NOTE` or `ERROR` on stdout/stderr
rather than as a side file. CoreSense Studio imports this same module directly (`generate_files`,
`validate_project`, `run_lint`, …), so the web app and this CLI can never disagree about what a
model means.

---

## What the hook can and cannot do

`hooks/hooks.json` runs the linter on every `Write`/`Edit` of a `.ros`, `.ros2` or `.rossystem`
file.

`PostToolUse` fires *after* a tool call succeeds, and the capability table reads
`PostToolUse | No | Shows stderr to Claude; the tool already ran`. **The file is on disk by the time
the linter runs.** Returning `{"decision": "block", "reason": "..."}` does not undo the write — it
blocks further processing and forces a corrective edit. The invariant bought is *"no invalid model
survives the turn"*, not *"no invalid model is ever written"*.

Doing better needs `PreToolUse`, which *can* block — but it would have to lint `tool_input.content`
before the file exists, and for `Edit` it would have to apply the diff itself to know the post-edit
content. That is a real option worth taking later.

`matcher` filters on **tool name only** (`"Write|Edit"`); path narrowing is done by the separate
`if` field, whose syntax embeds the tool name (`Write(...)` / `Edit(...)`), so each tool needs its
own handler. Hence six handlers for three extensions.

**Plugin-shipped agents cannot declare hooks** (`hooks`, `mcpServers` and `permissionMode` are
unsupported there for security reasons), and whether a plugin's own hooks fire inside a subagent
context is not documented either way. **Verify before relying on it.** If they do not fire, the
subagent must call the linter itself, which means adding `Bash` back to its tool list — it is
currently restricted to `Read, Write, Edit, Glob, Grep, Skill`.

## Why `-cp` and not `-jar` in `.lsp.json`

The JAR ships two usable launchers, both confirmed by extracting and reading the class files:

- `de.fraunhofer.ipa.ros2.ide.launch.ServerLauncher` is the manifest `Main-Class` and is
  **socket-based** — its constant pool holds `java/net/InetSocketAddress`, `localhost` and
  `createSocketLauncher`, and a `sipush 5008` gives the port. Its `main(String[])` never reads
  `args`. So `java -jar <jar>` yields a server on `localhost:5008`: not stdio, not configurable.
- `org.eclipse.xtext.ide.server.ServerLauncher` is also present and is the stock Xtext **stdio**
  launcher (`-noValidate`, `-trace`, `-log`, `logStandardStreams` in its constant pool).

Claude Code's LSP transport defaults to `stdio`, and while `transport: "socket"` is documented, no
documented field carries a port. So `.lsp.json` bypasses `Main-Class` and names the stdio launcher
explicitly. **This is unverified** — the stock launcher discovers languages via `ISetup`
service-loader registration on the classpath, and the ros2 setup is bundled, so it *should* work,
but it has not been run. If it fails, `claude --debug` reports why; the fallback is to run the
socket launcher out of band and switch to `transport: "socket"`.

`.ros1` is deliberately not wired — a third JAR ships for it, but it is out of E1 scope.
`.rossystem` is not wired into `.lsp.json` because no server for it *ships*; the locally built one
is reached through `ask_oracle.py` instead.

---

## Status and honesty

See [`STATUS.md`](STATUS.md) for a blunt assessment: what is verified and how, what is built but
unverified and precisely why, what is not done, every open assumption with the person who must
answer it, and the effort reckoning against the 3 PD WBS budget.
