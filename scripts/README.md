# `rosmodel_lint.py` — static linter for `.ros` / `.ros2` / `.rossystem`

Work item **E1**, CoreSense x Humanoid (Fraunhofer IPA).

A standalone Python checker for RosTooling model files. It accepts **`.ros`, `.ros2` and
`.rossystem`**.

It is no longer the *only* feedback available — `tests/oracle/ask_oracle.py` drives the real
language servers for `.ros`, `.ros2` and (since a `.rossystem` server was built locally)
`.rossystem` too. **That needs a Java 21 runtime**: the jars are Java 21 bytecode and fail with
`UnsupportedClassVersionError` on anything older, so check `java -version` and set `ROSMODEL_JAVA`
if the default is older. Where no Java 21 is available the linter is the only check, and output
must be described as linter-checked rather than oracle-validated.

**You no longer have to check that by hand, and `ros_studio.py generate` no longer lets it pass
quietly.** `ask_oracle.py --preflight` parses `java -version` and reports the real reason the
oracle cannot run — *"…is Java 11, but the language server jar needs Java 19+"* — instead of
starting a JVM that dies inside itself and surfacing 45 seconds later as a timed-out LSP
handshake (`NO_INITIALIZE_RESPONSE`). `ROSMODEL_JAVA` now also falls back to `java` on **PATH**,
as `.lsp.json` always did, before the hard-coded Adoptium path. `generate` runs the oracle **by
default** when the preflight passes, `--no-oracle` opts out, `--oracle` makes it mandatory, and a
jar that cannot run is reported loudly on stdout and stderr as a `NOTE` (or, with `--oracle`, an
`ERROR`) rather than passing quietly. `tests/oracle_gate.py` pins that whole failure path and needs
no working jar to do it — it points `ROSMODEL_JAVA` at a nonexistent path, which is reproducible
anywhere. See the "oracle (real language server)" section of `ros_studio.py`'s `cmd_generate` for
the full rationale.

There is now a third caller besides the hook and `ros_studio.py generate`: **CoreSense Studio**,
the local web app (`scripts/studio_server.py`, documented in
[`docs/coresense-studio.md`](../docs/coresense-studio.md)). Its "Check model" runs the generation
gate, then this linter over the freshly generated files, then the oracle when Java allows — the
same three stages, reported as three rows in the browser. Nothing about the rules below changes.

The linter's role is now to give **fast, single-file, offline** feedback
in the edit loop, and to cover the checks the oracle cannot make (house style, rossdl
compatibility, provenance sentinels). Where the two disagree, **the oracle wins** — every ERROR in
this table has been confirmed against it.

`.ros` support was added 2026-07-21, closing a real incoherence: the skill instructs authoring
`.ros` files, and a package that defines its own message types *requires* one, yet the linter used
to reject the extension outright with `RM000`.

---

## Usage

> On Windows, bare `python` is the Microsoft Store stub and exits non-zero — use **`py -3`**.

```bash
python rosmodel_lint.py <file>...
python rosmodel_lint.py --json <file>...
python rosmodel_lint.py --min-severity ERROR <file>...
python rosmodel_lint.py --max-per-rule 0 <file>...    # show every occurrence
python rosmodel_lint.py --quiet "corpus/**/*.ros2"    # summary line only
```

Globs are expanded internally, so quoted `**` patterns work regardless of shell.

| Option | Effect |
|---|---|
| `--json` | Machine-readable output. Never capped. |
| `--min-severity {ERROR,WARNING,INFO}` | Suppress findings below this level. Default `INFO`. |
| `--max-per-rule N` | Text output only: at most `N` findings per rule per file (`0` = unlimited). Default `10`. |
| `--quiet` | Print only the summary line. |
| `--hook` | PostToolUse hook mode — see below. |

### `--hook` mode

```bash
echo '{"tool_input":{"file_path":"x.ros2"}}' | python rosmodel_lint.py --hook
```

Reads the PostToolUse event JSON on stdin, takes the written path from `tool_input.file_path`,
and prints `{"decision": "block", "reason": "..."}` on stdout when an **ERROR** survives. Clean
files, non-model extensions, malformed events and internal errors all exit **0 in silence** — a
linter that breaks the agent's turn on its own bad day is worse than no linter.

`PostToolUse` fires *after* the write succeeds, so blocking cannot prevent a bad file reaching
disk; it forces a corrective edit. The invariant is *"no invalid model survives the turn"*, not
*"no invalid model is ever written"*.

> *Added 2026-07-21.* `hooks/hooks.json` had invoked `--hook` since the plugin was scaffolded, but
> the flag was never implemented — the hook fired, argparse rejected it with exit 2, and **no file
> was ever linted by the hook**. Both the `.rossystem` and (newly added) `.ros2` handlers are now
> functional, verified against a clean file, a two-ERROR file, and a malformed event.

**Exit status:** `1` if any ERROR-severity finding survives `--min-severity` filtering, else `0`.

**Dependencies:** Python 3.8+, standard library, and PyYAML. Without PyYAML the linter still runs
every byte/layout check and reports the structural checks as skipped (verified — see
*Test evidence*), rather than crashing.

### Why layout rules fire per line

The real Xtext validators do not short-circuit either — `checkNameConventionsPackage` emits one
marker *per uppercase character*. A tab-indented file can therefore produce hundreds of identical
RM001s. `--max-per-rule` caps the text output for readability; the summary counts and the JSON
output always report the true totals.

---

## Authorities

Findings are derived from three documents, in this order of precedence:

| Authority | Path | Governs |
|---|---|---|
| Pinned grammar subset | `../docs/grammar-subset.md` | What the pinned oracle can lex at all |
| Validator rules | `../research/validator-rules.md` | Semantic constraints + their real severities |
| Emission profile | `../emission-profile.md` | Normative formatting style |

> **Note on paths.** The E1 task brief located all three under `spec/`. They are actually at
> `docs/grammar-subset.md`, `research/validator-rules.md` and `emission-profile.md` (repository
> root). No `spec/` directory exists. Content, not location, was used.

---

## Rule reference

Severity column is what the linter emits. The "Mirrors" column names the Xtend validator method or
the profile rule the check reproduces.

### Bytes and layout

| ID | Severity | Check | Mirrors |
|---|---|---|---|
| `RM000` | ERROR | Unreadable file or unrecognised extension | — |
| `RM001` | WARNING | Tab character in leading whitespace | profile rule 1 (**demoted 2026-07-21** -- oracle ACCEPTS tabs) |
| `RM002` | WARNING | CRLF line endings | profile rule 3 (severity lowered — see *Deviations*) |
| `RM002B` | WARNING | UTF-8 BOM at start of file | — |
| `RM003` | WARNING | Indent width not a multiple of 2 | profile rule 2 |
| `RM004` | WARNING | Trailing whitespace | profile rule 5 |
| `RM005` | WARNING | Missing, or more than one, trailing newline | profile rule 4 |
| `RM006` | WARNING | Blank line inside an indented block | profile rule 5 |
| `RM007` | WARNING | Space before the `:` of a key | profile rule 6 |
| `RM008` | ERROR | YAML parse failure / not UTF-8 / bad root shape | rossdl `yaml.safe_load` compatibility |
| `RM008T` | WARNING | Tab inside a line (not in indentation) | profile rule 1 |
| `RM009` | ERROR | Duplicate key in the same mapping | profile rule 28 (uniqueness) |

### `.ros` structure and message fields

Added 2026-07-21. A `.ros` file is **not** parsed as YAML — `float32 x` nested under an
un-colonned `Pose` is folded by PyYAML into a single multi-line plain scalar, silently discarding
every field. The `.ros` branch therefore runs its own indentation-stack parser
(`parse_ros_indent`), modelled on Xtext's `AbstractIndentationTokenSource`: an increase in indent
opens a `BEGIN`, a decrease closes `END`s, and **absolute column values are never assumed**. The
corpus steps 3/4/5/6/7/9 spaces and the real parser accepts all of it.

`RosValidator.xtend` declares **no `@Check` at all** on `TopicSpec`, `MessagePart` or
`AbstractType`. Apart from `checkNameConventionsPackage` there is no validator to mirror here, so
these rules encode the **grammar** rather than a validator, and every ERROR was confirmed against
`ask_oracle.py` rather than guessed.

| ID | Severity | Check | Mirrors / oracle evidence |
|---|---|---|---|
| `RM070` | ERROR | Not a `PackageSet`: no package, package line indented, or missing trailing `:` | `Ros.xtext:11-14`, `Ros.xtext:25-27` |
| `RM071` | ERROR | Unknown member at package level; (WARNING) empty package or empty spec block | `Ros.xtext:25-46` |
| `RM072` | WARNING | `msgs:`/`srvs:`/`actions:` block repeated in one package | `Ros.xtext:30-45` — parses (repeated alternation), still a generator defect |
| `RM073` | ERROR | Spec-name or body-keyword structure wrong: trailing `:` on a spec name, multi-token spec line, missing/foreign body keyword | `Ros.xtext:81-104`; oracle on a trailing colon: `extraneous input ':' expecting RULE_BEGIN` |
| `RM074` | ERROR | Unknown type token, or an array form that does not exist (`time[]`, `duration[]`, `Header[]`) | `Basics.xtext:210-213`; oracle on `time[]`: `no viable alternative at input '[]'` |
| `RM075` | ERROR | Fixed- or bounded-size array (`float32[3]`, `string<=10`) | `Basics.xtext:296-375` hard-codes `'[]'`; oracle: `Invalid token float32[3` — a **lexer** error |
| `RM076` | WARNING | Quoted spec reference not shaped `<package>/msg/<Type>` | `RosQNP.xtend` qualifies specs as `pkg + "/msg/" + name`; see severity note below |
| `RM077` | ERROR | Field-line token structure wrong: odd token count, stray spaced `=`, constant in a Type position, or a field name that is not a legal `Data` | `Basics.xtext:201-208`; oracle on `uint8 FAN_OFF = 0`: `mismatched input '=' expecting RULE_END` |
| `RM078` | WARNING | More than one field on a line | **Legal** — `MessagePart+=MessagePart*` has no line separator and RosTooling's own `basic_msgs/common_msgs.ros` does it. House style only |
| `RM079` | ERROR | File uses the **brace** serialisation, not the indentation syntax | `RosTooling-NadiaHG` fork's concrete syntax; STATUS.md open question N2 |
| `RM080` | INFO | A `message`/`request`/`response`/`goal`/`result`/`feedback` section has no fields | Legal and common; surfaced because a bodiless body is also what data loss looks like |

`RM009` (duplicate name), `RM010` (uppercase package name, **ERROR**), `RM015`, `RM016`, `RM020`,
`RM021`, `RM040` (unsorted block) and `RM044` (`dependencies:`) are shared with the other two file
types and fire in `.ros` too. So do all the byte/layout rules `RM001`–`RM008T`.

**Two severities chosen deliberately against the obvious reading:**

- **`RM076` is a WARNING, not an ERROR**, even though the oracle reports an unresolvable reference
  as an ERROR (`Couldn't resolve reference to TopicSpec 'AllTypes'`). Linking is cross-file and
  this linter sees one file at a time, so it cannot distinguish "wrong shape" from "defined in a
  file I was not given". The hint says to treat it as an error.
- **`RM078` is a WARNING, not an ERROR.** Multiple fields per line parse fine. Rejecting them
  would fail files the real parser accepts — including the fixtures in `tests/oracle/cases/_deps`,
  which the oracle rates ACCEPTED 0E/0W.

An **unquoted, non-primitive** type token *is* `RM074` at ERROR, and that is not a guess: a spec's
qualified name always contains `/`, which no bare Xtext `ID` can express, so such a token can never
link — not even for a type declared three lines above it in the same package. Confirmed both ways
against the oracle (bare `AllTypes` → rejected; `"probe_msgs/msg/AllTypes"` → ACCEPTED 0E/0W).

**Corpus behaviour.** Over the 31 `.ros` files in `ros-model-examples`, the structural rules
`RM070`–`RM080` produce **zero** ERRORs. As of 2026-07-21 `RM001` is a WARNING, so there are now **zero** `.ros` ERRORs. (Previously 126 `RM001` tabs, all in the four
`slam_toolbox_msgs.ros` copies. RosTooling's own `basic_msgs/common_msgs.ros` and `ros_core.ros`
lint with **0 errors**.

### Names

| ID | Severity | Check | Mirrors |
|---|---|---|---|
| `RM010` | **ERROR** | Uppercase in a package name | `checkNameConventionsPackage` (R3) |
| `RM011` | WARNING | Uppercase in an artifact name | `checkNameConventionsArtifact` (R2) |
| `RM012` | WARNING | Uppercase in a node name | `checkNameConventionsNode` (R1) |
| `RM013` | WARNING | Uppercase in a parameter name's **final dot-segment** | `checkNameConventionsParameters` (R4) |
| `RM014` | WARNING | `/` in a node/artifact name (grammar-legal) | rossdl launch generation |
| `RM015` | ERROR | Empty interface/parameter name (`'':`) | profile rule 20 |
| `RM016` | ERROR | RosNames position not expressible as `ROS_CONVENTION_A \| ID` | `Basics.xtext:394` |

The ERROR/WARNING split in RM010–RM013 is the single most important severity distinction in the
toolchain: four structurally identical Xtend loops, but **only the package one calls `error()`**.
The message wording tracks it precisely ("are not allowed" vs "are not recommended"). RM013 is a
WARNING despite its validator message saying "has to follow" — the call is `warning()`, and the
call wins over the prose.

RM013 implements the dot-suffix carve-out exactly: an uppercase character at index `i` is forgiven
iff `name[i:]` still contains a `.`. So `Foo.bar` is clean and `foo.Bar` warns.

### Quoting

| ID | Severity | Check | Mirrors |
|---|---|---|---|
| `RM020` | ERROR | Unquoted `EString` containing `.` `/` `::` `-` or space | profile rules 10, 12, 13, 14 |
| `RM021` | ERROR | Quoted name in a `RosNames` position | profile rule 7 |
| `RM022` | WARNING | `.ros2`/`.rossystem` name unquoted where a bare `ID` would parse | profile rules 11, 12 |

`RM020` and `RM021` are opposites and both are ERRORs, because `EString = STRING | ID` while
`RosNames = ROS_CONVENTION_A | ID | 'node'` has **no `STRING` alternative**. Quoting is mandatory
in one position and a parse error in the other.

Per profile rule 15, the linter **never** flags the opposite quote character — Xtext's `STRING`
accepts both `'` and `"`, so the difference vanishes at parse time.

### `.ros2` structure

| ID | Severity | Check | Mirrors |
|---|---|---|---|
| `RM030` | ERROR | `msgs:` / `srvs:` / `actions:` block in a `.ros2` file | grammar-subset §5.2 |
| `RM031` | INFO | Newer QoS field (`lease_duration`, `liveliness`, `lifespan`, `deadline`) -- legal since the pin was lifted 2026-07-21; flagged for consumer portability only | profile rule 32 |
| `RM032` | ERROR | Unknown QoS field | `Ros2.xtext:30-42` |
| `RM033` | ERROR | Bad or quoted QoS enum value; non-integer `depth` | `Ros2.xtext:32-37` |
| `RM034` | WARNING | `profile:` / `history:` / `depth:` used | profile rule 33 |
| `RM035` | ERROR | Duration not a quoted int32 digit-string nor `infinite` | `CheckQoS`→`CheckDuration` (R14) |
| `RM036` | ERROR | Unknown key in a node block, interface, or parameter | `Ros.xtext:121-159`, `Ros2.xtext:49-124` |
| `RM037` | ERROR | Unknown key at package/system level | `Ros2.xtext:13-24` |
| `RM038` | ERROR | Missing mandatory member (`node:`, `type:`, `from:`, `value:`) | grammar (non-optional assignments) |
| `RM039` | WARNING | Member/block ordering deviates from canonical | profile rules 21–25, 27 |
| `RM040` | WARNING | Entries within a block not alphabetically sorted | profile rule 26 |
| `RM041` | ERROR | Unreachable, unknown, or quoted parameter type | grammar-subset §5.3, profile rule 16 |
| `RM042` | ERROR | `True`/`False` instead of `true`/`false` | profile rule 18 (**silent corruption**) |
| `RM043` | ERROR | `type: Double` with an integer literal | profile rule 19 (**silent corruption**) |
| `RM044` | WARNING | Zero-corpus-support construct (`dependencies:`, `ns:`, `namespace:`) | profile §3 |
| `RM045` | WARNING | `Struct` / `List` / `Base64` parameter type | grammar-subset §5.4, profile §3 |
| `RM046` | INFO | `Array [` with a space before the bracket | profile rule 17 |
| `RM095` | WARNING (`.ros2`) / ERROR (`.rossystem`) | Parameter value is a quoted string shaped like `[...]`/`{...}` instead of a real list | `CheckParameterValue` (validator-rules §2.6), added 2026-09-03 (**silent corruption**) |

### `.rossystem` structure

| ID | Severity | Check | Mirrors |
|---|---|---|---|
| `RM050` | ERROR | Connection endpoint is not a declared interface | `checkIfInterfaceInSystem` (S3) |
| `RM051` | ERROR | Bad connection direction / mismatched pairing | `checkPortPatterns` (S4) |
| `RM052` | ERROR | `fromFile:` missing `/`, or empty string | `fromFileHelper` (S2) |
| `RM053` | WARNING | `fromFile:` absent — triggers the NPE bug | `fromFileHelper` (S2) |
| `RM054` | ERROR | Process references an undeclared node | `checkIfNodeInSystem` (S1) |
| `RM055` | ERROR | Missing or unknown arrow prefix | `RosSystem.xtext:81-112` |
| `RM056` | WARNING | Arrow target / parameter ref not `artifact::name` | profile rule 30 |
| `RM057` | WARNING | `from:` not `<package>.<node>` | profile rule 29 |
| `RM058` | ERROR | Duplicate interface local name **within one node** | profile rule 28 |
| `RM059` | WARNING | Top-level block order | profile rule 25 |
| `RM060` | ERROR | Connection is not a 2-element list | `RosSystem.xtext:126-127`, validator-rules §3.4 |
| `RM061` | INFO | `subSystems:` used — demoted from WARNING 2026-09-03; the reuse checks RM090-RM093 apply, but presence alone is not a defect and must not be read as a reason to omit a requested subsystem (SKILL.md §8d) | profile §3, validator-rules §3.5 |
| `RM062` | WARNING | `/` in a `.rossystem` node name | rossdl launch generation |
| `RM063` | WARNING | `processes:` used | profile §3 |
| `RM064` | INFO | Connected interfaces have differing trailing names | `MatchPortMsgs` (S5), advisory only |
| `RM065` | WARNING | Connection endpoint name is owned by several nodes | profile rule 28 |
| `RM066` | INFO | `fromFile:` contains the `TODO` placeholder sentinel | rossystem-syntax §2 derivation ladder (exercised by `tests/oracle/cases/tb3-fresh/`) |
| `RM067` | INFO | Uppercase in a `.rossystem` node **label** | house style only — no validator |
| `RM096` | WARNING | Non-sentinel `fromFile:` with no `# caller-supplied` / `# on disk:` provenance comment on the same line (files under `assets/rosmodelscatalog/` exempt) | SKILL.md rule 5, added 2026-09-03 |
| `RM097` | WARNING (INFO when all closed) | Items the extractors could not read that are still open — counts `# FLAG`/`# OPEN`/`# UNRESOLVED`, and reports them against the `# EXTRACTOR-FLAGS: N` stamp the scripts write, so the original count survives any rewording | SKILL.md "Converting real source" + §8e, self-check 18 |

`RM051` enforces the only three legal pairings, with `from` always the server/publisher side:

| `from` | `to` |
|---|---|
| `pub->` | `sub->` |
| `ss->` | `sc->` |
| `as->` | `ac->` |

### Catalogue checks (added 2026-07-23)

Two real catalogues are vendored under `../assets/`: `roscommonobjects/` (message/service/action
**type** specs, 53 packages) and `rosmodelscatalog/` (standard package **node** models, 48
nodes across 9 domains — Nav2, TurtleBot 3, arms, cameras, etc.). `build_type_index.py` /
`build_node_index.py` index them into `assets/type_index.json` / `assets/node_index.json`; these
rules consume that index. Disable all twelve (RM081-RM092) with `--no-catalogue` (or `ROSMODEL_NO_CATALOGUE=1`
in `--hook` mode) for a workspace whose references are heavily project-local. See `SKILL.md` §8c.

| ID | Severity | Check | Applies to |
|---|---|---|---|
| `RM081` | WARNING | Quoted `type:` ref not indexed in `type_index.json` at all | `.ros`, `.ros2` |
| `RM082` | ERROR | Package indexed, but this type name isn't — suggests nearest match | `.ros`, `.ros2` |
| `RM083` | INFO | Consolidated: which vendored type file(s) this model needs | `.ros`, `.ros2` |
| `RM084` | WARNING | `from:` package not indexed in `node_index.json` at all | `.rossystem` |
| `RM085` | ERROR | Package indexed, but this node name isn't — suggests nearest match | `.rossystem` |
| `RM086` | ERROR | Arrow/parameter target resolves to a catalogued node, but that interface name isn't among its real interfaces — suggests nearest match | `.rossystem` |
| `RM087` | INFO | Consolidated: which vendored node file(s) this model needs | `.rossystem` |
| `RM088` | WARNING | `from:` reference resolves against `node_index.json`, but its source line doesn't name the resolved file | `.rossystem` |
| `RM089` | WARNING | `type:` reference resolves against `type_index.json`, but its source line doesn't name the resolved file | `.ros`, `.ros2` |
| `RM090` | ERROR | A node label is declared directly under `nodes:` **and** is also reachable through a `subSystems:` entry | `.rossystem` |
| `RM091` | WARNING | A `subSystems:` entry doesn't resolve, itself declares another `subSystems:` (nesting risk), or resolves but exposes zero `interfaces:` on any node | `.rossystem` |
| `RM092` | WARNING | A local node and a node reachable via `subSystems:` resolve the same `from:` under different labels — likely the same real node modelled twice | `.rossystem` |
| `RM094` | ERROR | Full-line comment at column 0 inside an indented block | `AbstractIndentationTokenSource` closes every open block; oracle case `19-neg-col0-comment`: `missing EOF at ''` |
| `RM093` | ERROR | `subSystems:` written as a bracket list `[...]` or as a `- item` block sequence — the grammar takes neither (settled 2026-08-14: oracle cases `17-subsystems-multi` / `18-neg-subsystems-dash`, `mismatched input '-' expecting RULE_END`). N entries are N bare lines | `.rossystem` |

`RM081`/`RM084` are WARNING, not ERROR, for the same reason `RM076` is: a genuinely
project-local package/node is legitimate, and the linter cannot distinguish that from a typo of a
standard one. `RM082`/`RM085`/`RM086` are ERROR because in those cases the package/node *is*
known-complete in the catalogue, so a name that doesn't match it is a real defect, not a
plausible gap — exactly the same reasoning `RM082`'s type-side counterpart already used.

**Why this exists**: a TurtleBot 2 navigation example model (`turtlebot2_navigation.rossystem`, demo content kept outside this repository) originally referenced
`nav2_amcl.amcl`, `nav2_bt_navigator.bt_navigator`, `nav2_planner.planner_server` and
`nav2_controller.controller_server` — all invented from general Nav2 knowledge with a plausible
but wrong `nav2_`-style package prefix (the real ones have no prefix at all: `amcl.amcl`,
`bt_navigator.bt_navigator`, etc.). Nothing before RM081-087 existed could have caught that
short of a full oracle run with hand-assembled dependencies.

**`RM088`/`RM089` (added 2026-07-23)** close a related but separate gap: RM081-087 make a
reference *resolve correctly*, but say nothing about whether a reader of the file itself can tell
it resolved, or to what. A user reviewing `turtlebot3_navigation.rossystem` asked exactly this —
"you only generated two `.ros2` files, what's your approach for the other seven?" — because seven
of the nine nodes point at pre-existing catalogue files with no in-file indication of that fact.
RM088 (node `from:`) and RM089 (`type:` refs) fire a WARNING whenever a reference resolves against
the catalogue but its own source line doesn't contain the resolved file's name, so the fix is
mechanically checked rather than left to memory the next time a model is authored or edited.

**`RM090`/`RM091`/`RM092` (added 2026-08-13)** close the gap a maintainer review of
`turtlebot3_navigation.rossystem` found: `from:` reuses a *node*, but reusing a whole pre-built
*system* (e.g. the base-platform bundle in `robots/turtlebot3/robot/turtlebot.rossystem`) is what
`subSystems:` is for — re-declaring nodes under `nodes:` that a `subSystems:` entry already
provides is exactly the "duplicate model definitions confusing the validator" the review flagged.
`build_node_index.py` now indexes each catalogued `.rossystem`'s nodes and their declared
`interfaces:` (never derived from the target `.ros2` — `checkIfInterfaceInSystem` doesn't either),
so `rosmodel_lint.py` can resolve a `subSystems:` reference the same way the real validator does,
one level deep. RM090 is the hard case: the same node label reachable two ways in one file. RM091
covers three ways a `subSystems:` entry itself is a problem, the most important of which is real
and common — the target resolves but declares **zero** `interfaces:` on any node (true of every
node in the vendored `turtlebot3_navigation2.rossystem`), which makes the reference grammatically
valid but useless, since nothing in it can ever be a `connections:` endpoint. RM092 is the soft
case — same `from:`, different label, possibly two genuine instances rather than a duplicate.

---

## Companion scripts

| Script | Purpose |
|---|---|
| `build_type_index.py` | Rebuilds `assets/type_index.json` and `references/type-catalogue.md` from `assets/roscommonobjects/`. Run after `sync_catalogue.sh` changes anything. |
| `build_node_index.py` | Rebuilds `assets/node_index.json` and `references/node-catalogue.md` from `assets/rosmodelscatalog/`. Run after `sync_catalogue.sh` changes anything. |
| `collect_deps.py <model>... <case-dir>` | Lints the given model(s) with the catalogue enabled and copies every file their RM083/RM087 findings named into `<case-dir>` — the automated replacement for hand-copying `tests/oracle/cases/_deps/` files. |
| `sync_catalogue.sh` | Re-copies both vendored catalogues from the local `material/code` checkouts, diffs against the vendored copy, and reports what changed. Does **not** rebuild the indexes or update `PROVENANCE.md` itself — both are printed as next steps when it detects a change. |
| `extract_ros2_interfaces.py <path>... -o DIR` | **Phase 1** — turns raw ROS 2 source into draft `.ros2` node-interface models, deterministically — no LLM. See below. |
| `extract_rossystem.py <launch>... --models DIR -o F` | **Phase 2** — turns ROS 2 launch files into a draft `.rossystem`. See below. |

### `extract_ros2_interfaces.py` — source → draft `.ros2`

```bash
python extract_ros2_interfaces.py <ros2-package-or-workspace> -o ros_model_draft --json record.json
```

Walks every `package.xml` under the given paths, decides the language **per file** (`.py` vs
`.cpp`/`.hpp`/`.cc`; `package.xml`'s `<build_type>` is only a hint, because a package can be
mixed), and emits one `.ros2` per package named after the **declared** package name, per
SKILL.md's output-layout rule.

- **Python** is read with the stdlib `ast` module: `create_publisher`, `create_subscription`,
  `create_service`, `create_client`, `ActionServer`/`ActionClient`, `declare_parameter` and
  `declare_parameters`. Message classes are resolved through the file's own
  `from pkg.msg import Type` imports (aliases included) and through `pkg.msg.Type` attribute
  chains. Node names come from `super().__init__("name")`.
- **C++** is read with **tree-sitter-cpp** (`pip install tree_sitter tree_sitter_cpp`), not
  regex: the same six call kinds plus `rclcpp_action::create_server`/`create_client`, reading
  the template argument for the type and the first pure string-literal argument for the name.
  `using X = a::b::C;` / `typedef` aliases are collected package-wide, so
  `create_publisher<RobotStatus_Msg>` resolves. Node names come from a
  `class X : public rclcpp::Node` constructor's `Node("name")` initialiser.
- **Types** resolve against `assets/type_index.json` first (the resolved vendored file is named
  in a trailing comment, per SKILL.md §8c); then against `.msg`/`.srv`/`.action` files found in
  the scanned tree (emitted as `# project-local, defined by …`); otherwise the reference is kept
  and marked with a `# TODO unresolved:` comment rather than guessed.
- **`generate_parameter_library`** is a real declaration source, not a gap. When `CMakeLists.txt`
  names a params YAML, it is parsed and its parameters emitted with their declared types and
  `default_value`s. Dynamic `__map_*` groups, whose real names are built at runtime from another
  parameter's value, are flagged instead. (`custom_joint_trajectory_controller` declares nothing
  via `declare_parameter()`; this recovers 16 real parameters it would otherwise be missing.)
- **`--emit-msgs DIR`** writes a companion `.ros` per project-local message package the models
  reference, transcribed from the package's own `.msg`/`.srv`/`.action` files, following
  message-to-message references to closure. Without it, a model referencing project-local types
  is **not loadable on its own** — an unresolved `type:` is a linking-layer ERROR. Per-field
  defaults and bounded arrays have no `.ros` form and are dropped with a report (SKILL.md §8b).
- **Package, artifact and node stay three distinct names** (SKILL.md §2a). The artifact is the
  *build target* — `add_executable(...)`/`add_library(...)` in `CMakeLists.txt` (with
  `${PROJECT_NAME}` resolved) or a `console_scripts` entry in `setup.py` — and the node is the
  literal name the source constructs. Deriving the artifact from the node name instead collapses
  two of the three, which is exactly what rule 2a warns against.
- **A source-declared default is emitted as `default:`, not `value:`.** A compiled-in
  `declare_parameter()` or `generate_parameter_library` default *is* a default (SKILL.md rule 9,
  where `default:` is a member of `ParameterType` and sits immediately after `type:`), and this
  leaves `value:` free for the deployed value, which belongs in the `.rossystem`.
- **An empty-list default omits the slot** rather than writing `value: "[]"`. The list production
  needs at least one element, so `[]` is a parse error and a quoted `"[]"` is a `ParameterString`
  — i.e. a lie the linter flags as RM095. `default:` is optional, so saying nothing is the honest
  form; the parameter is still declared, with a comment explaining why it has no default.
- **A computed name is flagged with its evidence, never resolved.** When a topic name is built
  from literals plus one identifier that is a constructor parameter, the flag reports the literal
  values every *visible* construction site passes and the candidate names they produce — as
  evidence, explicitly not as a declaration. Deciding whether the visible sites are all of them
  is a completeness claim a per-package parser cannot make, and emitting a plausible subset would
  turn a visible unknown into an invisible one.

**Nothing is guessed.** A declaration is emitted only when its name *and* its type are literal in
the source. A topic built from a parameter, an f-string, `get_name() + "/…"`, string
concatenation or a launch-time remap becomes a wrapped `# FLAG` comment in the file header —
kind, reason, `file:line`, and the source expression — for a human or an LLM to resolve. Header
comments sit at column 0 *above* the model, so they never trip RM094.

The generated files are passed through `rosmodel_lint.py` automatically (`--no-lint` to skip);
a non-zero exit means a generated file has a lint ERROR.

Two things it deliberately does **not** do:

- **No `.rossystem`.** Inferring `connections:`/`subSystems:` needs cross-package topic matching
  and architectural judgement, which is exactly what this script refuses to do. Phase 2 or a
  manual/LLM step.
- **No launch-file or config merging.** Node names overridden by `launch_ros`'s `name=`,
  parameters set in a `controllers.yaml`, and controller instances spawned by
  `controller_manager` are all invisible to a source-only reader. When a package's ROS calls sit
  on a `get_node()` handle or an injected node with no literal name, the artifact falls back to
  the package name and the file says so in a `# NOTE`. A package using
  `generate_parameter_library` gets a `# NOTE` pointing at its YAML, since those parameters are
  never `declare_parameter()` calls.

`--emit-qos` adds `qos:`/`depth:` for literal integer depths. It is **off** by default: RM034
warns against inventing those fields and no corpus model uses them. The depths are always in the
`--json` record either way.

`assets/roscommonobjects/PROVENANCE.md` and `assets/rosmodelscatalog/PROVENANCE.md` record each
catalogue's source, pinned commit (where readable), and sync date — read those before assuming
either vendored copy is current.

### `extract_rossystem.py` — launch files → draft `.rossystem` (Phase 2)

```bash
python extract_rossystem.py <launch-file>... --models ros_model/rosnodes -o system.rossystem
```

Parses launch files with `ast` — **never executes them** — and emits the composition Phase 1
deliberately leaves out.

**The type/instance split is what makes this work without rewriting anything.** A `.rossystem`
node is `"<label>": from: "<package>.<node>"`, where the label is free text and only `from:` is
a cross-reference (rossystem-syntax.md §3). So a launch file's `name="g1_loco_motion"` over a
source that says `Node("Loco_motion")` needs no rename anywhere: the label carries the
deployment name, `from:` carries the type. One controller plugin spawned three times is three
labels sharing one `from:`. Phase 1's files are never touched, so re-running Phase 1 cannot
silently break a `.rossystem`'s references.

- Follows `TimerAction` and `GroupAction`, resolves `var = Node(...)` assignments, and emits
  **only** nodes reachable from `LaunchDescription([...])`, in that list's order (`RM040`
  deliberately does not fire on a `.rossystem` `nodes:` block, so sorting it would silently
  discard the bring-up sequence the order encodes).
- Understands the ros2_control spawner idiom — `package="controller_manager",
  executable="spawner", arguments=["left_arm_controller"]` — reading the implementation package
  from `controllers.yaml`'s `controller_manager: ros__parameters: <name>: type:`. The controllers
  file is located by resolving the launch file's own `controllers_file` argument through
  `DeclareLaunchArgument` → `PathJoinSubstitution` → `FindPackageShare`, or given with
  `--controllers-file`.
- Resolves same-file `LaunchConfiguration("x")` one hop to its own `DeclareLaunchArgument`
  default and **discloses it** on the value line as overridable on the command line. Launch
  argument defaults are always *strings*, so values are converted against the `.ros2`'s declared
  type — `"false"` under `type: Boolean` becomes `false`, never `true` by truthiness.
- Emits a parameter override only when the target `.ros2` actually declares that parameter,
  since `"artifact::param"` has to resolve. Otherwise it is flagged, not emitted.
- **Never inlines a robot description.** The guard is on the parameter *name*, not on
  `Command(...)` happening to be non-literal, so a future smarter resolver cannot start inlining
  URDF (SKILL.md "When not to use").
- A node resolving to neither a `--models` file nor `assets/node_index.json` is **skipped with a
  `# FLAG`** rather than emitted with a dangling `from:` — an unresolved reference is a
  linking-layer ERROR that stops the whole file loading, so one missing node beats a file that
  will not open. A catalogue entry found under a different package name than the launch file
  used (`moveit_ros_move_group` vs the catalogue's `move_group`) is emitted *with* a flag saying
  so, never substituted silently.

**`connections:` is never emitted.** SKILL.md hard rule 4, and `MatchPortMsgs` compares types by
object identity. Candidate pairs (same interface name, same type string, legal direction,
self-loops excluded) are computed, printed and written to the `--json` record for a human or an
LLM to accept one at a time.

Exit status is non-zero when anything was flagged — a partial model is a result, not a success.

---

## Deviations from the specs, and why

Each of these is a deliberate judgment call, not an oversight.

**1. `RM002` (CRLF) is a WARNING, though emission-profile rule 3 says MUST.**
The same document states that working-tree CRLF is a `core.autocrlf=true` checkout artefact and
"MUST NOT be treated as evidence" — all 305 stored blobs are pure LF. Making this an ERROR would
fail every file in both corpora for a git setting rather than a model defect. The rule is still
reported so a generator emitting genuine CRLF is caught.

**2. `RM031` is primary over `RM035` for QoS durations.**
The task brief asked for duration-value validation (quoted digits, signed 32-bit, or `infinite`).
But grammar-subset §5.1 states those four fields must **never be emitted** — they are absent from
the pinned parser's token set, so they fail *lexing*, and §5.1 explicitly calls the duration
constraint "moot under the pinned profile … retained in documentation, not implemented as a
validator". Both are implemented: `RM031` (INFO -- it was an ERROR only while the oracle was
pinned to the 2024 build; the rule table above is authoritative) fires on the field's presence,
and `RM035`
additionally validates the value, so the check exists and is ready for the day the oracle is
rebuilt. Note that `validator-rules.md` §0.3 confirms `CheckQoS` is **not even present in the
shipped JAR bytecode** — so `RM035` mirrors a HEAD-only rule that the pinned oracle would neither
enforce nor reward.

**3. `default:` is accepted in parameters — the linter was right and the specs were wrong.**
Linting the corpus surfaced `default:` in real `.ros2` parameters, which the linter initially
rejected as an unknown key. Reading `Basics.xtext:72-110` showed it is legal: `default:` belongs to
the **`ParameterType`** rule (`ParameterIntegerType ::= 'Integer' ('default:' default=…)?`, and
likewise for `String`, `Double`, `Boolean`, `Base64` and `Array[T]`), not to `Parameter`. Because
`'type:' type=ParameterType` has no `BEGIN`/`END` around it, the default lands as a YAML *sibling*
of `type:`.

*Resolved 2026-07-21.* This was a genuine gap in all three spec documents, and it caused real
damage before it was closed: because the skill documented `default:` only under
`ParameterArrayType`, three of four adversarial regeneration runs concluded it was illegal on a
scalar parameter and **rewrote `default:` to `value:`** — a silent semantic change (they are
different metamodel slots) that parsed cleanly and that the linter, correctly, never complained
about. The gap is now closed in `emission-profile.md` rule 23, `SKILL.md` §9,
`skills/ros-model/references/ros2-syntax.md` §6 and the `worked-examples.md` §4 failure catalogue, and
`tests/roundtrip.py` now extracts `.default` as its own fact category so the rewrite surfaces as a
difference instead of vanishing.

**3a. `RM066` / `RM067` are linter-originated, not validator-derived.**
`RM066` (INFO) flags a `fromFile:` still holding the `TODO_PACKAGE/launch/TODO.launch.py` sentinel,
so a knowingly-synthesised path stays visible rather than passing as verified. `RM067` (INFO)
replaces a former misuse of `RM012` on `.rossystem` node labels: `checkNameConventionsNode` targets
`ros::Node`, whereas a `rossystem::RosNode` label is a different metaclass, and
`RosSystemValidator.xtend` declares **no** name-convention `@Check` at all (its only `@Check`
methods are `checkIfNodeInSystem`, `fromFileHelper`, `checkIfInterfaceInSystem`,
`checkPortPatterns`, `MatchPortMsgs`, `CheckParameter`, `BinaryHelp`, `ArrayHelp`, `ListHelp`,
`StructHelp`). Reporting it as a WARNING under a validator-derived id produced 5 spurious warnings
on `MT.rossystem` for corpus-correct names such as `static_transform_publisher_vS7Rn4YQfVmDReBi`.

**4. `RM040` is not applied to a `.rossystem` `nodes:` block.**
Profile rule 26's evidence base is `.ros2` interface and parameter blocks. Corpus B's own
`MT.rossystem` does not sort its `nodes:` block, and Corpus B is the weighted-decisive authority,
so sorting nodes is not a house rule.

**5. Interface local-name reuse *across* nodes is not an error.**
Profile rule 28 scopes uniqueness to *within a node*, and Corpus B declares `clock` in nearly every
node. An early version flagged cross-node reuse and produced 77 false errors on the reference file.
The linter now flags it only via `RM065`, when a connection actually references an ambiguous name.

**6. `RM042`/`RM043` are ERRORs although no validator detects them.**
These are the two documented silent-corruption traps: `True` falls through the `ParameterValue`
alternation into `ParameterString`, and a bare `10` under `type: Double` becomes a
`ParameterInteger`. The oracle will report neither. Silent type corruption is a worse failure mode
than a loud parse error, so the linter is deliberately stricter than the toolchain here.

**7. Tab recovery uses tab-stop 8, not a flat substitution.**
`RM001` reports tabs as a WARNING, then the file is normalised so structural checks can still run.
Expansion must advance to the next multiple of 8: a naive `\t` → `··` turns two tabs into 4 columns,
which is *shallower* than a parent indented with 6 spaces, inverting the nesting and producing
spurious "expected `<block end>`" errors. Switching to real tab-stop semantics cut YAML parse
failures on the messy `.rossystem` corpus from 14 to 7.

---

## Known limitations

Constraints that are **not statically checkable** in a single file, and are therefore not
implemented (severities per `validator-rules.md`):

- **R5–R10** (`CheckMsgsRef*`, WARNING) — all six test `ref.eContainer === null`, i.e. "did this
  cross-reference resolve". Needs a linking environment: the `.ros` file declaring the spec plus
  the dependency list.
- **S5** (`MatchPortMsgs`, ERROR) — Xtend `!==` is *identity* on the `EObject`, not name equality.
  Both endpoints must resolve to the **same** spec instance. `RM064` reports a name mismatch as
  INFO only; it cannot confirm or deny the real constraint.
- **S6** (`CheckParameter`, ERROR) — requires resolving `rosparam.from` into the `.ros2` parameter
  declaration. Given the defects in `CheckParameterValue` (it recurses while using *instance fields*
  as loop counters), the safe strategy is to emit scalar parameter values only; `RM045` nudges
  toward that.
- **S1/S3** are implemented only within a single file. Cross-file and subsystem-spanning cases are
  not resolved.

The linter also does **not** verify indentation against Xtext's `AbstractIndentationTokenSource`.
It checks indentation as YAML plus the profile's 2-space rule. Whether comment-only lines and blank
lines affect `BEGIN`/`END` synthesis is an open question that needs a JDK 19 to settle; `RM006` is
conservative for that reason.

---

## Test evidence

Everything below was **executed**. *Amended 2026-08-14:* the Java blocker referred to here is
long resolved — given a Java 21 runtime, `tests/oracle/ask_oracle.py` drives the real language
servers, and all 24 checked-in cases run on this machine (see the prerequisites table at the top
of this file, and `tests/oracle/RESULTS.md`). Read the sentence below as "not run against the
oracle *at the time these counts were taken*"; every ERROR in the rule table has since been
confirmed against it.

Environment: Python 3.12.6, PyYAML 6.0.3 (`py -3` on Windows).

**Rule coverage: all 55 rule ids were fired at least once** across the corpus plus hand-built
fixtures. The rule table above and the implementation were cross-checked mechanically — every id in
the script appears in this README and vice versa, with no orphans in either direction.

> *Amended 2026-07-21.* The rule set is now **57** ids. `RM066` and `RM067` were added after that
> sweep and are **not** covered by the "fired at least once" claim above; `RM067` has been observed
> firing 5× on `tests/regenerated/manufact/MT.rossystem`, `RM066` has **not** been exercised by a
> fixture. The whole-corpus counts below also predate both rules and the `RM034` hint rewording —
> they have not been re-run.

> *Amended 2026-07-23.* The rule set is now **66** ids — `RM081`-`RM089` (catalogue checks) were
> added and are similarly **not** covered by the whole-corpus counts below. All nine were verified
> against hand-built fixtures (a resolvable ref, an unresolvable-but-plausible project-local ref,
> a typo of a real catalogued package/node/type/interface for each of RM081/082, RM084/085, and
> RM086, plus a resolvable ref with and without a disclosing comment for RM088/089) rather than a
> corpus sweep — see the commit that introduced them for the exact cases.
>
> *Amended 2026-08-13.* The rule set was **70** ids at that date — `RM090`-`RM093` (`subSystems:` reuse
> checks) were added, also not covered by the whole-corpus counts below. Verified against six
> hand-built fixtures (a genuine label collision — including the harder case where the colliding
> subsystem node itself exposes zero interfaces, caught by an independent review after the first
> pass missed it — a same-`from:`/different-label pair, an unresolved `subSystems:` name, a
> resolved-but-zero-interfaces target, and the bracket-list/dash-block `subSystems:` forms) plus a
> full re-lint of every catalogued `.rossystem`, `tests/regenerated/mani-ur/system.rossystem`, and
> a TurtleBot 3 navigation example model (`turtlebot3_navigation.rossystem`, demo content kept outside this repository) — 0 new errors, 0 crashes, `--no-catalogue` confirmed
> to suppress RM090-RM092 (RM093 is a pure grammar check and fires regardless).

> *Amended 2026-09-03.* Two new rules, `RM095` and `RM096`, close gaps a live validation round
> found: a quoted string shaped like a list (`value: "['a', 'b']"`) passed through where the
> declared or requested type was a real list, and a confident, non-sentinel `fromFile:` path with
> no stated provenance. `RM061` (`subSystems:` used) is demoted from WARNING to INFO in the same
> pass — its old wording ("every corpus example is low-quality") had become a third nudge, beside
> §8d and self-check 15, toward omitting a subsystem the caller actually asked to reuse; presence
> alone was never itself a defect. Neither corpus count below (336-file or catalogued) has been
> re-run against these two additions; the whole-corpus figures that follow predate them.

### Whole-corpus run — 336 files, 0 crashes

**Re-measured 2026-08-14** against the current script. It emits **85** distinct ids -- RM000-RM097
plus the `RM002B` and `RM008T` variants -- which is **84 rules**<!--@count:rules-->, RM000 being the internal
read/parse failure rather than a rule. (RM094, the column-0 comment check, and the RM090/RM033
severity corrections landed after this sweep and change none of its counts: no corpus file
exhibits any of the three.) This supersedes the
counts the three amendments above call stale, and adds the `.ros` corpus, which the old 305-file
run predates. Both catalogue modes are given: the published table was always `--no-catalogue`
(confirmed — the one-file `CS_ros2model_TBs` `.rossystem` row reproduces its historic 2/10/5
exactly), and a catalogued workspace sees more.

`--no-catalogue` — comparable to every earlier table in this file:

| Corpus | Files | Files with ERROR | E | W | I |
|---|---:|---:|---:|---:|---:|
| `CS_ros2model_TBs` `.ros2` | 24 | 3 | 3 | 37 | 0 |
| `CS_ros2model_TBs` `.rossystem` | 1 | 1 | 2 | 10 | 5 |
| `ros-model-examples` `.ros2` | 229 | 142 | 868 | 2012 | 91 |
| `ros-model-examples` `.rossystem` | 51 | 18 | 32 | 4845 | 14 |
| `ros-model-examples` `.ros` | 31 | 0 | 0 | 389 | 247 |

Nothing regressed: every delta from the old table is a **severity reclassification already
recorded in `STATUS.md`**, not a change in what the linter sees. The `RM001` demotion (tabs are
accepted by the real language server) moves 727 `.ros2` and 2709 `.rossystem` findings from E to
W, and `RM012` → `RM067` moves 5 from W to I on the `CS` `.rossystem`. Error totals match
`STATUS.md`'s own post-demotion table row for row (32 / 868 / 0 / 2 / 3). **834 of the 868
remaining `.ros2` errors are still `RM042`** — `value: True` silently parsing as a string.

Catalogue on (default), same 336 files, 0 crashes:

| Corpus | Files | Files with ERROR | E | W | I |
|---|---:|---:|---:|---:|---:|
| `CS_ros2model_TBs` `.ros2` | 24 | 4 | 4 | 221 | 24 |
| `CS_ros2model_TBs` `.rossystem` | 1 | 1 | 38 | 41 | 6 |
| `ros-model-examples` `.ros2` | 229 | 145 | 924 | 3870 | 291 |
| `ros-model-examples` `.rossystem` | 51 | 22 | 43 | 5053 | 30 |
| `ros-model-examples` `.ros` | 31 | 0 | 0 | 414 | 258 |

The extra errors are catalogue-resolution findings on corpora the vendored catalogue was never
built from — `RM082`=57, `RM086`=43, `RM085`=4 — and of the extra 2,306 warnings
`RM089` (undisclosed catalogue provenance) alone accounts for 2,019, with `RM084`=127,
`RM088`=86, `RM081`=48 and `RM091`=26 making up the rest. Read them as "these corpora are outside the
catalogue", not as newly discovered defects; `--no-catalogue` is the right mode for them.

*Amended 2026-08-14, `normalise_bare_subsystems`.* One cell moved: catalogued
`ros-model-examples` `.rossystem` warnings, 5050 → **5053**. All three are `RM091`, and they are
findings that had been **missing**, not new noise: `cob/MOBI02/cob_navi_robot.rossystem` and
`cob_navi_robot_nhg.rossystem` write their several `subSystems:` entries unquoted, one per line,
which PyYAML folds into a single plain scalar — so the linter had been resolving one subsystem
called `'cob4_bringup launch_visual cob_nav2'` and reporting one warning where three references
exist. Nothing else in either table moves, and no ERROR count changes.

### Independent corroboration of the emission profile

On the clean Corpus B `.ros2` set the linter reproduced four counts that `emission-profile.md`
measured independently, exactly:

| Finding | Linter | Profile |
|---|---:|---|
| `RM003` indent not multiple of 2 | 1 | §1.1 table: one width-5 line |
| `RM005` missing final newline | 1 | §1.2: only `amcl.ros2` |
| `RM015` empty name | 3 | §1.5: 3 named files |
| `RM040` unsorted block | 3 | §1.5: 3 blocks, all caused by the empty name |

`RM058` fired exactly **2** times across the messy corpus, matching profile §1.5's "only 2
duplicate local names exist corpus-wide, both in Corpus A".

### Positive control

The two canonical reference outputs from `emission-profile.md` §4 (`nav2_amcl.ros2` and
`manufacturing_tb.rossystem`, the latter with `fromFile:` added per rule 7) lint **completely
clean — 0 findings, exit 0**. The linter agrees with the profile's own normative examples.

### Negative controls

Hand-built fixtures with planted defects. All were caught with the expected severity, and the
*valid* constructs in the same files produced no findings:

- `.ros2`: uppercase package (ERROR) vs uppercase artifact/node (WARNING); unquoted `/`-bearing
  type ref; bad QoS enum; quoted QoS enum; negative `depth`; `lease_duration`/`liveliness`;
  `True` under `type: Boolean`; `10` under `type: Double`; `type: Any`; `msgs:` block; quoted
  package name; `my.artifact` and `some/deep/name` in RosNames positions; `deadline: "5000000000"`
  correctly flagged as int32 overflow while `lifespan: "1000"` was not.
- `.rossystem`: `fromFile` without `/`; duplicate local name within a node; unquoted arrow target
  and `from:`; process referencing a ghost node; reversed connection (`sub->` in the `from` slot);
  mismatched pairing (`pub->`→`sc->`); unknown endpoint; 1-element connection. The three legal
  connections (`pub`→`sub`, `ss`→`sc`, `as`→`ac`) produced **no** findings.

### Degradation paths

- **Tab-indented files** (25 `.rossystem`, 24 `.ros2` have leading tabs): reported as `RM001`
  (**WARNING** since the 2026-07-21 demotion -- the oracle ACCEPTS tabs; the line below predates it)
  ERROR with a clear message, then normalised so structural checks still run — never an opaque
  YAML crash.
- **PyYAML absent** (simulated by blocking the import): layout checks run in full, structural
  checks reported as skipped via an INFO finding, exit 0, no traceback.

### Genuine defects found in the corpus

Not linter bugs — real problems, verified by reading the source lines:

- `MT.rossystem` (the *clean* reference file) declares **`bt_navigator` twice** (lines 45, 215) and
  **`robot_state_publisher` twice** (lines 3, 242) in the same `nodes:` mapping. `yaml.safe_load`
  silently keeps only the last, so rossdl loses a node without any warning.
- `pickQrCode.rossystem:162` contains a C-style `/* … */` comment inside a flow sequence. The
  grammar has `SL_COMMENT` (`#`) only and **no block-comment syntax**, so this is invalid in both
  YAML and the DSL.
- Four Corpus B `.ros2` files order `actionservers:` before `serviceservers:` — the deviation
  `emission-profile.md` §5 row 24 deliberately declines to adopt. `RM039` correctly reports them.
