# Oracle run — the real toolchain's verdict

**Run 2026-07-21.** First time anything in this plugin was checked by RosTooling itself rather than
by our Python reimplementation of its rules.

- **Java:** Temurin 21.0.11 LTS (`AppData\Local\Programs\Eclipse Adoptium\jdk-21.0.11.10-hotspot`)
- **Server:** `de.fraunhofer.ipa.ros2.xtext.ide-3.0.0-SNAPSHOT-ls.jar`, `Bundle-Version 3.0.0.202408011124` (built 2024-08-01)
- **Launcher:** `java -cp <jar> org.eclipse.xtext.ide.server.ServerLauncher` — stdio LSP
- **Driver:** `ask_oracle.py`, one server instance per case, each case its own workspace root
- Reproduce: `py ask_oracle.py --all`

## Results

| # | Case | Predicted | Actual | |
|---|---|---|---|---|
| 01 | corpus `turtlesim.ros2` (unmodified) | accepted | **ACCEPTED** 0E/0W | ✅ |
| 02 | corpus `robot_state_publisher.ros2` (unmodified, machine-extracted) | accepted | **ACCEPTED** 0E/0W | ✅ |
| 03 | **our generated `turtlesim.ros2`** | accepted | **ACCEPTED** 0E/0W | ✅ |
| 04 | ours + `lease_duration:` | rejected | **REJECTED** 6E | ✅ |
| 05 | ours + `msgs:` block | rejected | **REJECTED** 1E | ✅ |
| 06 | ours, package `Turtlesim` | ERROR (not warning) | **REJECTED** 1E | ✅ |

Six for six.

## Run 2 — 2026-07-21, defect-fix pass

Seven further cases, added while fixing three confirmed defects. A `.rossystem` language server
had been built locally by then (`build/rossystem-ls/`), so cases 07-09 are the **first
`.rossystem` results ever obtained from the real toolchain**.

| # | Case | Predicted | Actual | |
|---|---|---|---|---|
| 07 | `from: pkg.NODE` + arrow `artifact::iface` | accepted | **ACCEPTED** 0E/0W | ✅ |
| 08 | same, but `from: pkg.ARTIFACT` | rejected | **REJECTED** 2E | ✅ |
| 09 | same, but arrow `node::iface` | rejected | **REJECTED** 2E | ✅ |
| 10 | our regenerated `turtlesim.ros` **with message fields** | accepted | **ACCEPTED** 0E/0W | ✅ |
| 11 | every primitive, array, constant, spec ref, keyword field name, srv, action | accepted | **ACCEPTED** 0E/0W | ✅ |
| 12 | bounded array `float32[3]` / `string<=10` | rejected | **REJECTED** 5E (3.1.0 server; see 25: `float32[3]` is valid now, `string<=10` still is not) | ✅ |
| 25 | fixed and bounded arrays `T[N]`, `T[<=N]` over primitives, `time`, `duration`, `Header`, spec refs; plus `T[]` | accepted | **ACCEPTED** 0E/0W (RosTooling with `SizedArray`, 2026-11-24) | ✅ |
| `_deps` | the shared dependency specs | accepted | **ACCEPTED** 0E/0W | ✅ |

Thirteen for thirteen across both runs. Cases 01-06 are unchanged and still produce the run-1
verdicts, so they remain a working regression gate.

### 07/08/09 — the `from:` defect, settled empirically

The skill used to emit `from: "<package>.<artifact>"`. It is `<package>.<node>`. The three cases
share one `minipkg.ros2` in which artifact and node deliberately differ:

```
minipkg:
  artifacts:
    my_artifact:            <- artifact
      node: my_node         <- node
      publishers:
        'chatter': ...      <- interface
```

| Case | `from:` | arrow target | Verdict |
|---|---|---|---|
| 07 | `"minipkg.my_node"` | `"my_artifact::chatter"` | **ACCEPTED** |
| 08 | `"minipkg.my_artifact"` | `"my_artifact::chatter"` | `Couldn't resolve reference to Node 'minipkg.my_artifact'` |
| 09 | `"minipkg.my_node"` | `"my_node::chatter"` | `Couldn't resolve reference to Publisher 'my_node::chatter'` |

**Case 08 is exactly what the old skill emitted.** It does not link.

This matches `RosQNP.xtend` exactly: a `Node`'s qualified name is
`pkg.name + "." + node_name` (taken from `obj.eContainer.eContainer as Package`, skipping the
artifact), while all six interface kinds and `Parameter` are
`art.name + "::" + interface.name` (from `obj.eContainer.eContainer as Artifact`, skipping the
package). **Two opposite levels of the same file** — which is why the defect survived review.

A corpus sweep run the same day agrees in both directions, with no counter-examples: of 339
`from:` values, 52 resolve as node-only and **0** as artifact-only; of 788 arrow targets, 201
resolve as artifact-only and **0** as node-only. The remainder are files where `artifact == node`
(244 and 499) or whose target is not in the corpus.

### 10/11 — the skill can now express message fields

Case 10 is our regenerated `turtlesim.ros` carrying all five `Pose` fields that an earlier
adversarial test dropped. Case 11 exercises the whole `MessagePart` vocabulary in one file: every
scalar primitive, every array form, constants (`uint8 FAN_OFF=0`), cross-package and
same-package spec references, field names that collide with grammar keywords (`string name`,
`int32 type`), plus a `srvs:` and an `actions:` spec. Both ACCEPTED, 0 diagnostics.

Two facts were established by iterating case 11 against the server rather than by reading:

- **A bare spec reference never resolves, even within its own package.** `AllTypes local_ref`
  produced `Couldn't resolve reference to TopicSpec 'AllTypes'`; `"probe_msgs/msg/AllTypes"
  local_ref` made the file clean. There is no short form — `RosQNP` qualifies every spec as
  `pkg/msg/Name`, which contains a `/` and so cannot be a bare Xtext `ID`.
- **`time`, `duration` and `Header` have no array form.** `time[] stamps` gives
  `no viable alternative at input '[]'`. The grammar lists exactly fourteen `*Array` rules and
  these three are not among them.

### 12 — no bounded or fixed-size arrays

```
line 5  Invalid token float32[3
line 5  mismatched input ']' expecting RULE_END
```

`Invalid token` is a **lexer** error, so one bounded array costs the whole file. ROS IDL's
`float32[3]` and `string<=10` have no production; the grammar hard-codes the literal `'[]'`.
`rosmodel_lint.py` reports this as `RM075` at ERROR on that evidence.

## Run 3 — 2026-07-21, oracle rebuilt from current source

The oracle itself changed. Runs 1 and 2 used the JAR shipped in `vscode-RosTooling/resources/`,
built **2024-08-01**. Run 3 uses `de.fraunhofer.ipa.rostooling.ls-3.1.0-SNAPSHOT-ls.jar`, built
from `ipa-esa/RosTooling@esa/main` commit `5b7d897` — one unified server for `.ros`, `.ros2` and
`.rossystem`.

**15/15**, every case matching its prediction:

```
ACCEPTED  01 02 03 04 07 10 11 13 _deps
REJECTED  05 06 08 09 12 14
```

### 04 — the flip, causally attributed

Case 04 existed only because the 2024 parser had no token for `lease_duration`. Same directory,
same file, only the JAR swapped:

| `ROSMODEL_ORACLE` | Verdict |
|---|---|
| `legacy` | **REJECTED** (6) — `mismatched input 'lease_duration' expecting RULE_END` |
| default | **ACCEPTED** (0) |

That is the pinned-oracle compromise ending. Its name is now a misnomer — kept for traceability.

### 14 — durations really do overflow at 32 bits

New probe, written to test `RM035` *before* deciding whether to delete it alongside `RM031`:

| Field | Value | Verdict |
|---|---|---|
| `lease_duration:` | `"1000"` | clean |
| `lifespan:` | `infinite` | clean |
| `deadline:` | `"5000000000"` (5 s) | **ERROR** — *"should be specified as a string of nanoseconds…"* |

`RosValidator.CheckDuration` uses `Integer.parseInt`, so the maximum expressible duration is
**~2.147 s**. The rule survived, and matters more now than before: these fields only became
emittable with this rebuild, and every human-plausible timeout exceeds the cap.

### 13 — `True` is silently a string

Probe confirming `RM042`. `terminal BOOLEAN` is `'true'|'false'` only, and `ParameterString`
precedes `ParameterBoolean` in the `ParameterValue` alternation, so `value: True` under
`type: Boolean` yields a **string**. Oracle verdict: **ACCEPTED, 0 diagnostics** — nothing in the
toolchain detects it. This is why the linter is deliberately stricter than the toolchain here, and
why ~834 corpus parameters are silently mistyped.

## What each proves

### 03 — the deliverable claim

Our generated file is **accepted by the real toolchain with zero diagnostics**, head-to-head with the
human-authored original (01) on the same system. Both clean. This is the first evidence that the
skill emits files RosTooling actually accepts, rather than files that satisfy our own copy of its rules.

### 04 — the pinned-oracle decision was correct

```
line 10  mismatched input 'lease_duration' expecting RULE_END
line 10  mismatched input '"1000"' expecting RULE_BEGIN
```

`mismatched input` is a **parser** error, not a validation error. The keyword is not in the 2024
grammar at all, so the file fails to parse — it never reaches the validator. Confirms the decision to
exclude `lease_duration` / `liveliness` / `lifespan` / `deadline` from the emission profile while the
oracle is pinned to this JAR. Emitting them would produce unparseable files.

Note the cascade: one unknown keyword produced 6 errors, including two spurious
`The required feature 'message' of 'ros.impl.PublisherImpl...'`. A single bad QoS key corrupts the
enclosing publisher. Cheap to get wrong, expensive to read.

### 05 — skill rule 1 confirmed

```
line 3  mismatched input 'msgs:' expecting RULE_END
```

`AmentPackage` genuinely has no `msgs:` block. Message specs belong in `.ros` only. This was our
highest-ranked hallucination risk and it is now a demonstrated hard failure, not a grammar inference.

### 06 — the ERROR/WARNING split is real

```
line 1  The name of a package has to follow the ROS naming conventions: Capital letters are not allowed
```

Verbatim the message in `RosValidator.checkNameConventionsPackage`, and reported at ERROR severity —
confirming the asymmetry we transcribed (packages error; nodes, artifacts and parameters only warn).

## Correction to an earlier claim

We previously described unresolved cross-file type references as *warnings*, on the basis that
`RosValidator.CheckMsgsRef*` calls `warning()`. **That is wrong.** They surface as **ERRORs**, because
resolution fails in Xtext's linking layer before the validator runs. The validator's warning is an
additional hint, not the primary diagnostic.

This matters operationally: a `.ros2` file is only clean if every `type:` it references is defined by
a `.ros` file **in the same workspace**. Validating one file alone will always show errors.

## Fixture dependencies

Each case directory carries the `.ros` specs its model references. Built up empirically — the first
run showed 11 errors, all missing dependencies, and the positive control was only trustworthy once
they were resolved to zero:

| File | Provides | Source |
|---|---|---|
| `ros_core.ros`, `common_msgs.ros` | `std_msgs`, `geometry_msgs`, `sensor_msgs`, `nav_msgs`, … | RosTooling test resources |
| `turtlesim.ros` | `turtlesim` msgs + srvs | `ros-model-examples` |
| `std_srvs.ros` | `Trigger`, `Empty`, `SetBool` | corpus `Trigger` + `Empty`/`SetBool` hand-added |
| `builtin_interfaces.ros` | `Time`, `Duration` | hand-written |
| `tf2_rosgraph.ros` | `tf2_msgs/TFMessage`, `rosgraph_msgs/Clock` | hand-written |

The four hand-written specs are themselves validated: the `_deps` directory lints ACCEPTED 0E/0W.

## Run 4 — 2026-08-13, `subSystems:` reuse verified against the real oracle

Maintainer review of `turtlebot3_navigation.rossystem` pointed out that `from:` reuses one node's
implementation — reusing a whole pre-built `.rossystem` composition is what `subSystems:` is for.
Reading `RosSystemValidator.xtend`'s `checkIfInterfaceInSystem` directly showed this only works
when the referenced system's nodes declare their own `interfaces:` — never derived from the
`.ros2` a node's `from:` points at. Two new probe cases settled the open questions empirically
rather than by source-reading alone.

| Case | What it tests | Verdict |
|---|---|---|
| `15-subsystems-fixture` | `subSystems: "turtlebot"` + a connection into a subsystem-owned interface (`odom` → a local sink of the same type), no local re-declaration | **ACCEPTED** 0E/0W |
| `16-subsystems-tf-ambiguity` | a connection naming `tf`, a label `turtlebot.rossystem` declares on BOTH `turtlebot_node` and `robot_state_publisher` | **ACCEPTED** 0E/0W — resolves silently to one of the two, unspecified which |
| `17-subsystems-multi` | **two** `subSystems:` entries as two bare lines, with a connection reaching each | **ACCEPTED** 0E/0W |
| `18-neg-subsystems-dash` | the same file with the entries written as a `- item` block sequence | **REJECTED** — line 4, `mismatched input '-' expecting RULE_END` |
| `19-neg-col0-comment` | a full-line comment at **column 0** between `from:` and `interfaces:` | **REJECTED** — line 7, `missing EOF at ''` |
| `20-subsystems-label-collision` | a node declared locally under `nodes:` whose label is ALSO reachable through `subSystems:` | **ACCEPTED** 0E/0W |
| `21-neg-liveliness` | `liveliness: banana` | **REJECTED** — line 9, `no viable alternative at input 'banana'` |

### Settled 2026-08-14: three more, two of which we had backwards

**Case 19 — column 0 ends the model.** `AbstractIndentationTokenSource` emits the END tokens for
every open block when it sees a line at column 0, so a comment dedented that far terminates the
model and everything after it is unreachable input. Indent the same comment, or move its text to
the end of the previous line, and the file is ACCEPTED 0E/0W. YAML does not care — a comment is
not a node — so composition succeeded and every other check passed on a file the toolchain cannot
load. `RM094` now reports it, and it is the only rule that can: this is invisible to a
YAML-shaped reader.

**Case 20 — `RM090` was wrong.** We reported ERROR on this; the server accepts it silently. The
two nodes live in different Xtext resources, so the old hint's "two distinct RosNode objects
answer to the same name in this file's scope" is not what happens. Demoted to WARNING, which is
still worth saying: which of the two a `connections:` endpoint binds to is unspecified and a
reader cannot tell from the file alone. Worth noting this false positive had already shaped
design — the multi-file merge renames and collapses nodes specifically to avoid RM090.

**Case 21 — `liveliness:` was never checked.** `Liveliness=('automatic'|'manual')` is a keyword
alternation, but the field sat outside `QOS_ENUMS` *and* behind `RM031`'s early `continue`, so
any value at all passed. `RM031` speaks to whether the FIELD needs a recent toolchain; it says
nothing about the value, and the two checks are now independent. `liveliness: automatic` is
ACCEPTED and stays clean.

### Settled 2026-08-14: how a MULTI-entry `subSystems:` block is written

Cases 17 and 18 are a controlled pair — identical but for the two subsystem lines — and they
answer the question `RM093`'s hint had been carrying as open since the rule was written. **N
entries are N bare lines; the dash form is a syntax error.** There is no third option: the
grammar has no bracket or list wrapper for this production at all.

That makes the one legal form the one form a YAML parser cannot read. Two consecutive scalars
are not composable (`expected <block end>, but found '<scalar>'`), so every reader in this
plugin — `rosmodel_lint`, `/ros-plot`, `/ros-studio` — used to reject case 17's file outright
with an `RM008` ERROR while the real server accepted it clean. `rosmodel_lint.normalise_bare_subsystems`
now rewrites the block in memory, line for line, so composition succeeds and diagnostics still
point at the author's own lines. Nothing on disk changes and the emitter still writes bare
lines, because that is what the grammar takes.

Worth recording as an upstream conflict rather than a model-authoring mistake: a multi-entry
`subSystems:` model cannot be read by `yaml.safe_load` **in any form the grammar accepts**, so
a YAML-based consumer such as `rossdl` cannot consume one at all. No author can write their way
out of that; it needs a grammar or a tooling decision upstream.

**The quiet half of this defect is worse than the loud one.** Whether the entries are quoted
decides which way it fails:

| Source | PyYAML | What we reported before |
|---|---|---|
| `"turtlebot"` / `"extra"` (quoted) | compose error | `RM008` ERROR — loud, and wrong |
| `cob4_bringup` / `launch_visual` (bare) | folds into ONE plain scalar | **one subsystem named `'cob4_bringup launch_visual cob_nav2'`** — no error at all |

The second row is not hypothetical: it is `cob/MOBI02/cob_navi_robot_nhg.rossystem` and
`cob_navi_robot.rossystem` in `ros-model-examples`, where YAML's multi-line plain scalar folding
turned three references into one nonsense name and every check downstream believed it. The
catalogued corpus sweep moves `RM091` from 23 to 26 warnings for exactly that reason — three
references that had been invisible are now each reported on their own line. No other rule
changes, and no ERROR moves.

### Settled: `subSystems:` syntax and connection scope

`components+=SubSystem*` is a repetition, not a bracket list — `subSystems: ["turtlebot"]` is a
parse error (`mismatched input '-' expecting RULE_END` when written with a leading `-`, too). The
corpus form is one bare, quoted-if-you-like name per indented line:

```
subSystems:
  "turtlebot"
```

A `connections:` endpoint **does** resolve one level into a referenced subsystem's own interface
labels, using the subsystem's exact spelling (`odom`, not a re-suffixed `odom_pub`) — confirmed by
case 15 going from `REJECTED` (`odom`/`cmd_vel` deliberately type-mismatched, to isolate the
scoping question) to **ACCEPTED** once paired with a type-matched local sink.

### Settled, and a real trap: ambiguous labels resolve silently, not with an error

`turtlebot.rossystem` declares the bare label `"tf"` on both `turtlebot_node` and
`robot_state_publisher`. A connection naming `tf` **does not error** — Xtext's default scoping
picks one of the two candidates silently. Which one is unspecified and not something a model
author controls through this mechanism. `rosmodel_lint.py`'s `RM065` (pre-existing rule, now also
firing on subsystem-merged interfaces) is the only signal a model has this problem; the oracle
gives no hint. `turtlebot3_navigation.rossystem` v4 keeps its one `tf` connection (real data, from
whichever of the two resolves) but does not attempt a second connection to the other tf source,
since there is no way to address it once both are exposed under the same shared label.

### `turtlebot3_navigation2.rossystem` — grammatically valid, functionally inert

Confirmed by reading the file directly, and by indexing it with the extended
`build_node_index.py`: all 14 nodes (amcl, bt_navigator, planner_server, controller_server, and
10 more) declare `from:` only, zero `interfaces:`. `subSystems: "turtlebot3_navigation2"` resolves
without error but exposes nothing a `connections:` block could reference — not tested against the
oracle directly (no connection could be constructed to test), but the conclusion follows
deterministically from `checkIfInterfaceInSystem`'s source: it reads `rosinterfaces` off each
subsystem node, which is populated only from that file's own declarations.

`turtlebot3_navigation.rossystem` was rewritten to `subSystems: "turtlebot"` for the base-platform
nodes (the reusable case) and kept its four Nav2-stack nodes as explicit `nodes:` (the inert case)
— re-verified end-to-end: `collect_deps.py` (now `subSystems:`-aware, staging the referenced
`.rossystem` and its transitive `.ros2`/`.ros` deps automatically) into `cases/tb3-v4/`, oracle
**ACCEPTED, 0E/0W**.

## Run 5 — 2026-08-19, both `parameters:` slots verified against the real oracle

`case 22-parameters` — the generated output of `tests/fixtures/params/param_probe.rossystem`,
staged with `collect_deps.py`. **ACCEPTED, 0E/0W.**

This is the first oracle run over `parameters:` in a `.rossystem`, because until this change the
studio emitted neither slot. Both are now covered in one file:

| slot | grammar | what the case proves |
|---|---|---|
| system-level `parameters:` | `Parameter`, Basics.xtext:41-49 | `type:` alone, `type:`+`default:`, `ns:`+`type:`+`value:`, and Integer/Double/Boolean/String values all parse |
| node-level `parameters:` | `RosParameter`, RosSystem.xtext:78-82 | `- "label": "artifact::name"` + `value:` parses, and the label may differ from the artifact parameter name |

**The run caught a real emitter bug on its first attempt** — worth recording, because nothing else
would have found it. `ns:` takes a `Namespace`, which is one of three bare KEYWORDS
(`GlobalNamespace` | `RelativeNamespace` | `PrivateNamespace`, each optionally followed by a
`GraphName` list — Basics.xtext:13-32). It is *not* an `EString`. The emitter quoted it like every
other string slot and the server answered:

```
REJECTED — 1 error(s)
  ERROR  line 62  no viable alternative at input '"/probe"'
```

`rosmodel_lint` did not flag it (RM044 only warns that `ns:` has zero corpus support), and the
round-trip suite could not: both sides of the parity check agreed on the same wrong bytes. Fixed
by writing the keyword through verbatim, never quoted and never invented.

Note the rule cannot express an actual namespace string — only those three keywords — which is
consistent with RM044's "0 occurrences across all 253 corpus files".

## Run 6 — 2026-08-19, a subsystem that actually has internal wiring

`case 23-subsystem-wired` — the generated output of `tests/fixtures/subsysgraph/uses_wired.rossystem`.
**ACCEPTED, 0E/0W.**

This closes an assumption the subsystem abstraction views rest on and that **no corpus file could
test**, because every system referenced through `subSystems:` anywhere in this repo declares zero
connections of its own:

| referenced system | nodes | interfaces | internal connections |
|---|---|---|---|
| `turtlebot` (catalogue, the largest) | 3 | 7 | **0** |
| `robot_base` / `sensor_base` / `labelled_base` | 1 | 1–2 | **0** |
| `extra` | 1 | 1 | **0** |
| `ur5e_cell_moveit_config` | 0 | 0 | **0** |

The case proves the shape is legal rather than merely untested: a referenced `.rossystem` may
carry its own `connections:` block, and the referencing system may connect **across the boundary**
to an interface the subsystem exposes — here `detections` (published by `detector`, inside
`wired_base`) into `detections_in` on the parent's own `planner`. Two of the subsystem's edges are
internal and invisible to the parent; the third crosses.

Without this run, "collapse a subsystem to one box" and "frame its internals" would have been
built against a corpus in which no subsystem has any internals worth drawing.

## Run 7 — 2026-09-03, a list value written as a quoted string

`case 24-neg-list-as-string` — a minimal `Array[String]` parameter, exposed from a `.rossystem`
node with its value written as the quoted string `"['a', 'b']"` instead of a real list
`["a", "b"]`. **REJECTED, 1E** — `Expect a list of elements; format { , , }`.

This is not a synthetic worst case. A live validation round of the `ros-model` skill found this
exact defect in a real generated file: the skill had transcribed a `type:Array [String]` /
`value: "['x', 'y']"` pair verbatim from a real project's own (defective) `.ros2` source, following
its documented "preserve the source literal" guidance to the letter — the guidance had no
exception for a source that is simply wrong. This run confirms the real server does reject the
copy, which justifies `RM095` as ERROR on the `.rossystem` side (`rosmodel_lint.py`) and closes the
gap: SKILL.md §8 and `skills/ros-model/references/rossystem-syntax.md` §5 now say the
declared/requested type wins and the string form must be converted, not preserved.

## Still not covered

- ~~**`.rossystem` — entirely.**~~ **Closed 2026-07-21** by the locally built
  `de.fraunhofer.ipa.rossystem.xtext.ide` server — see run 2, cases 07-09. `rosmodel_lint.py` is
  still a reimplementation, but it is no longer the *only* check available for `.rossystem`.
- **HEAD-only constructs.** The pinned JAR validates a 2024 language. It can say nothing about the
  four newer QoS fields except that it does not know them.
- **The `.rossystem` oracle is locally built, not shipped.** Cases 07-09 ran against
  `build/rossystem-ls/`, compiled from RosTooling source in a parallel workstream — not against a
  released artefact. The results are real, but reproducing them requires that build.
- **Corpus-scale `.rossystem` validation.** Only the synthetic cases above have been run
  through the `.rossystem` server. The 52 corpus systems have not.
- **Nested `subSystems:` (two levels deep).** `RM091` flags it from source-reading
  (`checkIfInterfaceInSystem` casts unconditionally one level down, so a second level throws
  `ClassCastException`) but this has not been reproduced against the real oracle — no corpus or
  vendored file currently nests, so no fixture exists to run it against.
