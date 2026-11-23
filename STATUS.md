# STATUS — work item E1, `rostooling-modeler`

**As of 2026-08-14.** (Sections below carry their own datelines; where one says 2026-07-21 it
means what was true then, and later amendments are marked.) Written to be useful to whoever picks this up, not to look finished.

One-line summary (**rewritten 2026-08-14**): **the static half of E1 is done and self-consistent,
and the dynamic half now runs here too** -- Java 21 is installed, the 3.1.0 language server is
built, and all 24 oracle cases execute on this machine. The original summary said the dynamic half
"has never been executed, and cannot be on this machine"; that was true when written and was
overtaken by section 2.1 the same week. Everything below is labelled accordingly.

> *Amended 2026-11-23.* `rosmodel_lint.py` was cut down to what the language server does not
> check: 34 rules that copied the grammar or the validator were retired (see `scripts/README.md`,
> *Retired rules*), and `--server` asks the server itself. Rule counts and rule ids in the dated
> sections below describe the linter as it was then.

---

## 1. DONE and actually verified

"Verified" here means *executed on this machine and the output read*, or *read directly from the
authoritative source file*. It never means "looks right".

### Installable — verified end-to-end 2026-07-23

The plugin installs into Claude Code and every component registers and runs. Not inferred from the
manifest — driven from the *installed* copy under `~/.claude/plugins/cache/`:

- `claude plugin marketplace add` + `claude plugin install rostooling-modeler@rostooling-modeler-marketplace`
  → installed, enabled. The repo self-hosts a marketplace (`.claude-plugin/marketplace.json`).
- Component inventory (from `claude plugin details`): **Skills 1 · Agents 1 · Hooks 1 · LSP 1**,
  ~256 tok always-on.
- The bundled 47 MB LS JAR **travels with the install** and resolves via `${CLAUDE_PLUGIN_ROOT}`;
  started from the installed path it validated turtlesim **ACCEPTED, 0 diagnostics**.
- The `PostToolUse` hook, run from the installed `scripts/`, returned correct
  `{"decision":"block"}` JSON catching `RM010` on an uppercase package name.

Fixes that made this work: `.lsp.json` repointed from the stale 2024 absolute paths to the current
3.1.0 JAR via `${CLAUDE_PLUGIN_ROOT}`, `.rossystem` added, Java made configurable
(`${ROSMODEL_JAVA:-java}`); `marketplace.json` added; README install section rewritten. The two
env vars `ROSMODEL_JAVA` and `ROSMODEL_PYTHON` must be set on this machine (PATH `java` is 1.8,
`python3` is a Store stub) — documented in the README. Marketplaces consolidated to one
(`coresense-local` removed; project `settings.json` realigned to the self-hosting marketplace).

| Deliverable | How it was verified |
|---|---|
| **`scripts/rosmodel_lint.py`** — 81 rules, 2 873 lines (57 rules / 1 647 lines when this row was written) | **Executed** across all 305 corpus files, 0 crashes. Counts reproduce four independent measurements in `emission-profile.md` §1 exactly (RM003×1, RM005×1, RM015×3, RM040×3 on Corpus B; RM058×2 corpus-wide). |
| Linter **positive control** | **Executed.** Both canonical reference outputs from `emission-profile.md` §4 lint completely clean, 0 findings, exit 0. |
| Linter **negative controls** | **Executed.** Hand-built fixtures with planted defects; all caught at the expected severity, and valid constructs in the same files produced no findings. |
| Linter **degradation paths** | **Executed.** Tab-indented files → `RM001` then normalised recovery, never an opaque YAML crash. PyYAML absent (import blocked) → layout checks still run, structural checks reported skipped, exit 0, no traceback. |
| **`--hook` mode** (new, 2026-07-21) | **Executed** against a clean file (silent, exit 0), a two-ERROR file (correct `{"decision":"block"}` JSON, exit 0) and a malformed event (silent, exit 0). |
| **`tests/roundtrip.py`** — semantic harness, 957 lines | **Executed.** `selftest` passes 4/4 checks on 3 fixtures: identity, text perturbation, order perturbation, and negative control (36 planted changes detected on `MT.rossystem`). |
| **Grammar transcription** (`docs/grammar-subset.md`, `research/validator-rules.md`, `emission-profile.md`) | **Read directly** from `Basics/Ros/Ros2/RosSystem.xtext` and both `*Validator.xtend`. Every production quoted verbatim; every severity taken from the actual `error()`/`warning()`/`info()` call, not from its message prose. |
| **Corpus measurements** | **Executed** — indentation histograms, quoting classification, boolean/double counts, ordering, comment placement, stem-vs-model-name. Notably: all 305 stored git blobs are pure LF; the 100 % CRLF working tree is a `core.autocrlf=true` artefact and was correctly discounted. |
| **JAR forensics** | **Read** from extracted class files and manifests. `Bundle-RequiredExecutionEnvironment: JavaSE-19`; `Main-Class` is socket-based on hard-coded port 5008 (`sipush 5008`, `createSocketLauncher`), so `-cp` + the stock Xtext stdio launcher is required. Zero matches for `rossystem` across all three JARs, by archive listing *and* raw byte grep. |
| **4 adversarial regenerations** | **Executed** — turtlesim, `image_system_example`, `MT.rossystem` (463 lines), `MANI01_UR`. All linted; all round-tripped. Outputs in `tests/regenerated/`. |
| **`default:` resolution** (2026-07-21) | **Read** from `Basics.xtext:72-110`. Settled a direct contradiction between verifiers — see §5. Regression-tested: turtlesim now round-trips **SEMANTICALLY EQUAL, 34/34 facts**. |
| **`from:` = `package.node`** (2026-07-21) | **Executed** against the real `.rossystem` server. Oracle cases 07/08/09: `package.node` + `artifact::iface` ACCEPTED; `package.artifact` REJECTED (`Couldn't resolve reference to Node`); `node::iface` REJECTED. Corroborated by a corpus sweep — 52 `from:` node-only vs **0** artifact-only; 201 arrow targets artifact-only vs **0** node-only. See §7. |
| **`.ros` message fields** (2026-07-21) | **Executed.** Regenerated `turtlesim.ros` *with* its fields → oracle **ACCEPTED 0E/0W** (case 10). A vocabulary case covering every primitive, array, constant, spec ref and keyword field name → **ACCEPTED 0E/0W** (case 11). |
| **`.ros` linting** (2026-07-21) | **Executed** over all 31 corpus `.ros` files plus RosTooling's own `basic_msgs` test resources: 0 crashes, and **0 ERRORs from the new `RM070`–`RM080` rules**. Every new ERROR severity was confirmed against `ask_oracle.py`, not inferred. |

### The pinned-oracle compromise is GONE — 2026-07-21

`§2.2` below described pinning the emission profile to a language server built **2024-08-01**,
which predates four QoS fields added to the grammar on 2025-10-16. **That compromise no longer
exists.** The server was rebuilt from current source and the four fields are now legal to emit.

- **New JAR:** `build/ros2-ls/target/de.fraunhofer.ipa.rostooling.ls-3.1.0-SNAPSHOT-ls.jar`
- Built from `ipa-esa/RosTooling` @ `esa/main`, commit `5b7d897`, 366 sources, ~27 s
- One unified server: `ISetup` registers **RosSystem + Ros2 + Ros + Basics** (`.ros1` still absent)
- `ask_oracle.py` defaults to it; `ROSMODEL_ORACLE=legacy` restores the old pairing for A/B

**Causally attributed, not merely observed** — same case directory, same model file, only the JAR
differing:

| Oracle | Verdict |
|---|---|
| legacy 2024-08-01 | **REJECTED** — `mismatched input 'lease_duration' expecting RULE_END` |
| current 3.1.0 | **ACCEPTED** — 0 diagnostics |

Full gate after the change: **15/15**, every case matching its prediction.

**A rule was verified before being removed, and it survived.** `RM035` (QoS durations must fit a
signed 32-bit int) had never been tested. Oracle case 14: `deadline: "5000000000"` (5 s) → **ERROR**;
`lease_duration: "1000"` and `lifespan: infinite` → clean. So `RosValidator.CheckDuration`'s
`Integer.parseInt` really does cap durations at **~2.147 seconds** in nanoseconds. That rule became
*more* load-bearing when the pin lifted — the fields are only emittable at all now, and every
realistic timeout exceeds the limit. Sweeping it away alongside `RM031` would have removed the one
QoS check that actually bites.

`RM031` survives as an **INFO**, not an ERROR: the fields are legal, but a consumer on an older
toolchain build cannot *lex* the keyword, and it fails as a syntax error invalidating the whole
enclosing publisher rather than as a warning. Its old hint hardcoded a rationale that is now false
and was rewritten, not just disabled.

Corpus counts are unchanged by this (32 / 868 / 3 errors) — zero corpus files use these fields, so
portability here is untested in practice.

**Known caveat:** the build is a hybrid — current 3.1.0 plugin classes on the Xtext runtime salvaged
from the 2024 fat jar, since Xtext 2.42 was never downloaded. It compiles and passes 15/15, but it
is not the combination Eshan's CI produces. Recorded here so it is not rediscovered later.

**Naming wart:** case `04-neg-lease-duration` is no longer a negative case. The name is kept for
traceability with the A/B above; do not read the `neg-` prefix as an expectation.

### Settled 2026-08-14 — a multi-entry `subSystems:` block, and the reference it was eating

`components+=SubSystem*` is a repetition, so N subsystem references are N bare lines. `RM093`'s
hint had carried that as an open question — *"the real multi-entry form may be N separate bare
lines instead … verify against ask_oracle.py before relying on it"*. Verified, as a controlled
pair differing only in those two lines (oracle cases `17-subsystems-multi`, `18-neg-subsystems-dash`):

| Form | Oracle | Our reader, before |
|---|---|---|
| two bare lines | **ACCEPTED** 0E/0W | `RM008` ERROR — file rejected outright |
| `- item` sequence | **REJECTED**, `mismatched input '-' expecting RULE_END` | `RM093` WARNING — "unverified" |

Both verdicts were backwards. `RM093` is now an ERROR for the dash form, and
`rosmodel_lint.normalise_bare_subsystems` rewrites the bare block in memory, line for line, so
every YAML-based reader in the plugin — lint, `/ros-plot`, `/ros-studio` — can read the one form
the grammar accepts. The emitter already wrote it; only the readers were wrong.

**What this was hiding is the part worth remembering.** Quoted entries fail loudly (PyYAML cannot
compose two consecutive scalars). *Unquoted* entries — which is what the corpus writes — fold into
a single plain scalar and fail **silently**: `cob/MOBI02/cob_navi_robot_nhg.rossystem` was being
read as one subsystem named `'cob4_bringup launch_visual cob_nav2'`, with no diagnostic at all,
and every downstream check believed it. Two corpus files, three references, invisible until now.

Unresolved and not ours to fix: the only form the grammar accepts is unreadable by
`yaml.safe_load`, so a YAML-based consumer such as `rossdl` cannot load a multi-entry
`subSystems:` model **in any legal spelling**. That is a grammar-vs-tooling conflict for upstream,
not something a model author can write around — see §4.

### RM001 demoted — the corpus was never as broken as we reported

**2026-07-21.** `RM001` (tab in leading whitespace) was an ERROR. It is now a WARNING, because the
real toolchain accepts tab-indented files. Controlled experiment: one `.rossystem` and a copy
differing in *nothing* but tabs-for-spaces. Oracle: **both ACCEPTED, 0 diagnostics.** Our linter:
0 errors on the spaces version, **11 on the tabbed one.** The rule's own hint already conceded the
point — *"even though the Xtext lexer tolerates it"* — while still classifying it ERROR.

The real consequence of tabs is downstream, not in the toolchain: YAML forbids tabs in indentation,
so `rossdl`'s `yaml.safe_load` cannot read such a file. That is a genuine reason to emit spaces, and
no reason at all to call an existing corpus file broken.

Corpus ERROR counts, before → after this one severity change:

| Corpus | Before | After |
|---|---|---|
| `ros-model-examples` `.rossystem` (51) | 2741 | **32** |
| `ros-model-examples` `.ros2` (229) | 1595 | **868** |
| `ros-model-examples` `.ros` (31) | 126 | **0** |
| `CS_ros2model_TBs` `.rossystem` (1) | 2 | 2 |
| `CS_ros2model_TBs` `.ros2` (24) | 3 | 3 |
| **total** | **4467** | **905** |

Warning counts rise correspondingly; nothing was suppressed, only reclassified.

**Of the 868 remaining `.ros2` errors, 834 are `RM042`** — and that rule is *correct*, verified the
same day. `terminal BOOLEAN: 'true'|'false'` is lowercase-only and `ParameterString` precedes
`ParameterBoolean` in the `ParameterValue` alternation, so `value: True` under `type: Boolean`
silently becomes a **string**. Oracle probe (case 13): **ACCEPTED, 0 diagnostics** — nothing in the
toolchain detects it. The linter is deliberately stricter here, and should stay that way.

That makes 834 corpus parameters silently mistyped. Worth raising upstream: these `.ros2` files are
largely machine-extracted, and Python emits `True`/`False`, so the extractor is the likely source.

### Corpus regression after this session's changes

Re-run and compared against the pre-change baseline in `scripts/README.md`:

| Corpus | Before | After |
|---|---|---|
| `ros-model-examples` `.ros2` (229) | 1595 E / 1285 W / 91 I | **identical** |
| `ros-model-examples` `.rossystem` (51) | 2741 E / 2138 W / 12 I | 2741 E / **2136 W** / **14 I** |
| `CS_ros2model_TBs` `.ros2` (24) | 3 E / 37 W / 0 I | **identical** |
| `CS_ros2model_TBs` `.rossystem` (1) | 2 E / 15 W / 0 I | 2 E / **10 W** / **5 I** |

The only deltas are the intended `RM012` → `RM067` reclassification (7 warnings became infos). No
other rule changed behaviour.

**Re-verified after the 2026-07-21 defect-fix pass**: all four rows above are **byte-identical**
again (1595/1285/91, 2741/2136/14, 3/37/0, 2/10/5). Adding the `.ros` branch changed nothing for
`.ros2` or `.rossystem` — the only edits to those paths were finding *hints*, which carry no
counts. New `.ros` coverage, previously 32 blanket `RM000`s:

| Corpus | Before | After |
|---|---|---|
| `ros-model-examples` `.ros` (31) | 31 E (all `RM000`, extension rejected) | **0 E** / 263 W / 247 I |

The 263 warnings are real style findings on hand-written corpus files — 126 `RM001` (tabs, all in
the four `slam_toolbox_msgs.ros` copies, reported as ERROR), 106 `RM003` (odd indent widths), 92
`RM004` (trailing whitespace), 31 `RM002` (CRLF), 11 `RM040` (unsorted). The 247 `RM080` infos are
bodiless `response` sections, which are legal. **No corpus file gets an ERROR from any of the new
structural rules.**

**Re-verified 2026-08-14**, after the `subSystems:` rules (`RM090`-`RM093`), `load_local_system`
and the whole `/ros-studio` line of work. Every ERROR count in this section still holds exactly —
32 / 868 / 0 / 2 / 3, total **905**. The `.ros` warnings moved 263 → 389 and infos 247 → 247:
that corpus is unchanged, the rules that read it grew. Full five-row tables for both catalogue
modes are in `scripts/README.md`, whose own pre-demotion table was stale until this run and is
now replaced.

---

## 2. BUILT BUT UNVERIFIED — and precisely why

### 2.1 The Java blocker — **RESOLVED 2026-07-21**

~~`java -version` → `1.8.0_481`.~~ Temurin 21.0.11 LTS is now installed at
`AppData\Local\Programs\Eclipse Adoptium\jdk-21.0.11.10-hotspot` (user-scope; the machine-wide
registry step failed for lack of admin rights, which is harmless — `.lsp.json` pins the absolute
path). **The oracle has now been run. See `tests/oracle/RESULTS.md`.**

Six cases, six predictions confirmed:

- corpus `turtlesim.ros2` and corpus `robot_state_publisher.ros2` — **ACCEPTED**, 0E/0W
- **our generated `turtlesim.ros2` — ACCEPTED, 0E/0W**, head-to-head with the human-authored original
- `+lease_duration:` → **REJECTED**, `mismatched input` — a *parser* error, confirming the pin decision
- `+msgs:` block → **REJECTED**, confirming skill rule 1
- uppercase package → **REJECTED at ERROR severity**, verbatim the validator's message

So `.lsp.json`'s launcher choice is no longer a guess: `java -cp <jar>
org.eclipse.xtext.ide.server.ServerLauncher` starts and serves diagnostics over stdio.

**One earlier claim in this document is now corrected:** unresolved cross-file type references were
described as warnings. They are **ERRORs** — resolution fails in Xtext's linking layer before the
validator runs. A `.ros2` is only clean when every `type:` it references is defined by a `.ros` in
the same workspace.

What remains unverified below is narrower than it was, but §2.3 (`.rossystem` has no oracle at all)
is untouched by this and remains the largest gap.

Unverified as a direct consequence:

- **`.lsp.json` in its entirety.** The `-cp` + `org.eclipse.xtext.ide.server.ServerLauncher` choice
  is a *reasoned inference* from reading class-file constant pools. The stock launcher discovers
  languages through `ISetup` service-loader registration and the ros2 setup is bundled, so it
  should work — but "should" is the operative word. If it fails, `claude --debug` reports why and
  the fallback is the socket launcher with `transport: "socket"`.
- **Every `.ros2` file this plugin emits.** Correct per the transcribed grammar; never accepted by
  a parser. Always report as *unverified-pending-Java*.
- **Two open lexer questions** the emission profile is deliberately conservative about: whether
  comment-line indentation affects `BEGIN`/`END` synthesis (rules 35-36), and whether blank lines
  inside indented blocks are tolerated (rule 5).
- ~~**Whether `from:` resolves as `package.artifact` or `package.node`**~~ — **settled 2026-07-21,
  see §7.** It is `package.node`, and the arrow targets are `artifact::interface`.

**The negative control has been run.** `lease_duration:` and `msgs:` are both rejected by the
server, so the two hard exclusions in `SKILL.md` are fact, not inference.

### 2.2 The stale pinned oracle (permanent, by decision)

The shipped `ros2` JAR was built **2024-08-01** (`Bundle-Version 3.0.0.202408011124`). RosTooling
HEAD (2025-11-27) added `lease_duration` / `liveliness` / `lifespan` / `deadline` on 2025-10-16 —
**14.5 months after** the JAR was built. `InternalRos2Parser.tokens` contains `reliability:` and
`durability:` but not those four, so they fail *lexing*, not validation.

**Project decision: pin the emission profile to the JAR-accepted subset and document the
exclusion.** This is deliberate and it is cheap — 0 of 253 corpus `.ros2` files use any of the four
fields, so the exclusion is justified twice over. Because the JAR's token set is a strict subset of
HEAD's, output valid under the pinned profile is also valid at HEAD.

**What the pin costs:** even with Java 21 installed, the oracle validates a 2024 language. It can
never confirm anything about HEAD-only constructs, and `CheckQoS` is not even present in the
shipped bytecode — so `RM035` mirrors a rule the pinned oracle would neither enforce nor reward.
Lifting this means rebuilding the language servers from RosTooling HEAD.

### 2.3 `.rossystem` — no *shipped* oracle; a locally built one now exists

No `rossystem` language server was ever **shipped**; that finding stands. But one has since been
**built from source** in a parallel workstream (`build/rossystem-ls/`,
`de.fraunhofer.ipa.rossystem.xtext.ide-3.0.0-SNAPSHOT-ls.jar`), and `ask_oracle.py` now selects it
per case by file extension. Oracle cases 07-09 are the first `.rossystem` diagnostics ever obtained
from the real toolchain, and they settled the `from:` question (§7).

**What this does not yet cover:** ten case directories now carry a `.rossystem` and have been through it (2026-08-14), all of
them synthetic or hand-built. The 52
corpus `.rossystem` models have not been validated, and `scripts/rosmodel_lint.py` remains a
*reimplementation* of `RosSystemValidator`'s ten `@Check` methods rather than the thing itself.
Running the corpus through the built server is now cheap and is the obvious next step.

### 2.4 Plugin wiring not exercised in a running install

`claude plugin validate --strict` has **not** been run. Specifically unconfirmed:

- Whether the agent frontmatter `skills:` field wants `ros-model` or the namespaced
  `rostooling-modeler:ros-model`. The docs do not state which form plugin-scoped skills require.
- **Whether plugin-shipped hooks fire inside a plugin-shipped subagent.** Undocumented either way.
  Plugin agents cannot declare hooks themselves. If they do not fire, `agents/ros-modeler.md` must
  regain `Bash` and call the linter directly — its tool list is currently
  `Read, Write, Edit, Glob, Grep, Skill`.

---

## 3. NOT DONE, and what it would take

| Gap | What it would take |
|---|---|
| **No cross-file resolution in the linter.** R5–R10 (`CheckMsgsRef*`), S5 (`MatchPortMsgs`), S6 (`CheckParameter`) all need a linking environment. `MatchPortMsgs` compares with Xtend `!==` — *object identity* — which one file cannot decide. This is also why `.ros` `RM076` is a WARNING rather than the ERROR the oracle reports. | Build a workspace index: parse every `.ros`/`.ros2` in a directory tree and resolve `type:` and `from:` refs against it. ~0.5–1 PD. **Lower priority than it was** — the oracle now does real linking for all three file types, so the index is a convenience, not the only route. |
| **The corpus has never been through the `.rossystem` server.** Ten case directories now carry one (2026-08-14), but they are all synthetic or hand-built. | Point `ask_oracle.py` at the 52 corpus systems with their `.ros2`/`.ros` dependencies staged. The dependency staging is the actual work. ~0.3 PD. |
| **`.ros` linting has no cross-package spec index.** `RM076` cannot tell "wrong shape" from "defined in a file I was not given". | Falls out of the workspace index above. |
| **`compare` has no expected-diff baseline.** It exits 1 on *any* semantic difference, including the deviations the skill mandates (added `fromFile:`, renamed duplicate labels). Mandated normalisations read as failures. | Add `--allow <baseline.json>`. ~0.2 PD. |
| **`roundtrip.py` discards duplicate mapping keys.** Its YAML reader keeps only the last of a duplicated key. On `MT.rossystem` this dropped an entire node from the *original*'s fact set, so 24 of 27 reported "differences" were the harness losing data, not the output diverging. It prints a correct NOTE but still folds them into the headline count and the exit code. | Parse to a multimap, or partition the report into "differences" and "unrepresentable in the original" and exclude the latter from `RESULT` and the exit code. ~0.3 PD. **Until then, do not trust the headline count on any file with duplicate keys.** |
| **`PreToolUse` blocking.** The current hook cannot prevent a bad write, only force a corrective edit. | Lint `tool_input.content` pre-write; for `Edit`, apply the diff first to know post-edit content. ~0.3 PD. |
| ~~**`RM066` never fired by a fixture.**~~ **CLOSED 2026-08-14** — `tests/oracle/cases/tb3-fresh/turtlebot3_guided_navigation.rossystem` carries the `TODO_PACKAGE` sentinel and the rule fires on it. | — |
| **SysML mapping.** Descoped — see §4. | Unscoped. Needs Bjoern's sign-off before any estimate is meaningful. |

---

## 4. Unconfirmed assumptions — and who must answer

Nothing here is answerable from the material on disk. Each needs a named person.

### Nadia

| # | Question | Why it blocks |
|---|---|---|
| N1 | **Is the SysML mapping in or out of E1?** | It is currently descoped and *no work has been done on it*. If it is in, the estimate in §6 is wrong by a wide margin. Needs Bjoern's sign-off (B1) but Nadia owns the content. |
| N2 | **Which `ros2model` fork is canonical?** | Everything here is transcribed from the `RosTooling` clone at `material/code/`, HEAD 2025-11-27. If the canonical fork differs, the grammar subset, validator rules and emission profile all need re-derivation against it. **This invalidates the most work of any open question.** |
| N3 | **Is the `fromFile` validator bug known upstream, and will it be fixed?** | `fromFileHelper` dereferences `system.fromFile` twice with **no null guard**, and `SystemImpl.java:70` defaults the field to `null` — so omitting `fromFile:` throws an NPE out of the `@Check`. 32 of 52 corpus models omit it. This bug is the *sole reason* the skill emits a field the source often does not contain, and the sole reason it must sometimes fabricate a placeholder path. **If upstream fixes it, `SKILL.md` rule 5 should be deleted outright.** |

### Eshan

| # | Question | Why it blocks |
|---|---|---|
| E1 | **Does `rossdl` generate Docker artefacts, and does that constrain the model?** | `rossdl` parses these DSLs with `yaml.safe_load` and derives the node class from `from:.split('.')[1]`. If Docker generation adds requirements (image names, entrypoints), the emission profile may need fields it currently omits. |
| E2 | **Target ROS 2 distro?** | Determines which message packages `type:` refs must resolve against, and therefore which `.ros` files must exist in the workspace for `CheckMsgsRef*` to stay quiet. |

### Bjoern

| # | Question | Why it blocks |
|---|---|---|
| B1 | **Sign-off on the E1 rescope and the SysML descope.** | The delivered scope is *"generate valid RosTooling model files"*, with SysML explicitly excluded. That is a narrowing of the original E1 statement and it has not been formally accepted. |
| B2 | **Is the pinned-oracle shortcut accepted?** | §2.2. Pinning to the 2024-08-01 JAR is what makes E1 deliverable near the 3 PD budget. Rejecting it means rebuilding the language servers from HEAD and re-deriving the grammar subset — see §6. |

### Open technical question with no owner yet

*(The `from:` question that stood here has been settled — see §7.)*

**T1 — a multi-entry `subSystems:` model cannot be YAML-loaded, in any legal spelling.** The
grammar accepts only N bare lines; PyYAML either refuses them (quoted) or folds them into one
scalar (unquoted). So `rossdl`'s `yaml.safe_load` cannot consume such a model at all, and no
model author can write around it. This plugin works around it internally
(`normalise_bare_subsystems`), which is enough for our readers and does nothing for anyone
else's. Needs a decision upstream — quote-aware grammar, a list production, or a documented
"one subsystem per file" rule. Evidence: §1 (2026-08-14) and `tests/oracle/RESULTS.md`.

`N1`, `N2`, `N3`, `E1`, `E2`, `B1`, `B2` and now `T1` still need their named owners.

## 5. Verifier findings this session REJECTED

Recorded because rejecting a majority is the kind of decision that should be auditable.

**Three of four adversarial verifiers reported that `default:` on a scalar parameter is illegal and
must be rewritten to `value:`. All three were wrong, and the fix they proposed would have
entrenched a silent data-corrupting bug.**

`Basics.xtext:72-110` attaches `('default:' default=…)?` to **every** scalar `ParameterType` rule —
`Integer`, `String`, `Double`, `Boolean`, `Base64` — as well as to `ParameterArrayType`. Because
`'type:' type=ParameterType` carries no `BEGIN`/`END`, the token lands as a sibling of `type:`.
`default:` (the type's declared default) and `value:` (the parameter's assigned value) are
**distinct metamodel slots**. Rewriting one into the other parses cleanly, produces no diagnostic
from any validator or linter, and changes what the model says.

The linter had modelled this correctly all along (`param_keys = ["type", "default", "ns", "value",
"qos"]`, with a source comment explaining the binding) — **the spec documents were the ones out of
step**, and the verifiers, reasoning correctly from a wrong reference, converged on the wrong
answer. Only the turtlesim verifier caught it, by cross-reading the diff against the linter source.

Two other rejections:

- **"Sort `.rossystem` `nodes:` alphabetically, and add a linter check."** Rejected. It contradicts
  `scripts/README.md` deviation 4 and Corpus B's own `MT.rossystem`, and node order frequently
  encodes bring-up sequence. The skill now says **preserve source order**.
- **"Duplicate node labels go completely undetected; add a new ERROR rule."** Rejected as stated —
  `RM009` (ERROR, duplicate key in the same mapping) already catches them at
  `rosmodel_lint.py:1123`. The genuine gap was that the *skill* never taught the rule; that has been
  fixed without adding a redundant check.

---

## 6. Effort reckoning — honest

**WBS budget: 3 PD. Independent review: 4–4.5 PD unless the pinned-oracle shortcut is taken.**

The shortcut **was** taken (§2.2), which is what kept this near budget. The 1–1.5 PD the review
identified as extra was almost entirely *"stand up a working oracle and validate against it"* —
which is precisely the work the Java blocker made impossible anyway. So the plan and the
circumstances happened to agree, for different reasons.

### What exists

*Amended 2026-08-14: the reckoning below counts the state at 2026-07-21 and has not been
re-costed since. For scale, `scripts/` is now 10 830 lines (ros_studio 3 209, rosmodel_lint 2 873,
_studio_editor 2 679, ros_plot 1 239) and the harnesses 2 233 -- most of that is the /ros-studio
line of work, which the estimate below predates entirely.*

~6 400 lines of deliverable: 1 885 lines of derived specification, 1 897 lines of skill,
1 647 lines of linter (57 rules), 957 lines of test harness, plus plugin wiring and 4 adversarial
regeneration records.

### Consumed vs. remaining

| Phase | Estimate |
|---|---|
| Grammar/validator transcription + corpus measurement | ~1.0 PD |
| Emission profile derivation | ~0.5 PD |
| Linter + harness | ~1.0 PD |
| Skill authoring + plugin wiring | ~0.5 PD |
| Adversarial verification + this reconciliation pass | ~0.5 PD |
| **Consumed (estimated)** | **~3.5 PD** |

> **This is a reconstruction from the work products, not a timesheet.** I did not observe the hours
> and cannot certify them. Treat as ±0.5 PD and confirm against actuals before reporting upward.

**Remaining to a defensible "E1 complete":**

| Item | Estimate |
|---|---|
| Install Java 21 (Temurin), run the negative control, verify `.lsp.json` starts | 0.25 PD |
| Settle the `from:` question against the running oracle | 0.25 PD |
| `.ros` linting support | 0.3 PD |
| `roundtrip.py` duplicate-key fix + `--allow` baseline | 0.5 PD |
| Run `claude plugin validate --strict`; confirm hooks fire in the subagent | 0.25 PD |
| **Total** | **~1.5 PD** |

So: **~3.5 PD consumed, ~1.5 PD remaining, against a 3 PD budget** — landing at ~5 PD, above even
the independent review's 4–4.5 PD estimate. The overrun is not in the modelling work, which came in
roughly on plan. It is in two things the WBS did not price: **the environment blocker** (a full
verification phase that has to be re-scheduled rather than dropped) and **the adversarial
verification pass**, which found the `default:` defect and a wired-but-unimplemented `--hook` — both
of which would otherwise have shipped as silent failures.

If 1.5 PD is not available, the honest minimum is the **0.25 PD Java 21 negative control**. Without
it, the two hard exclusions at the centre of the emission profile remain inferences, and the
plugin's core claim — that it emits files the toolchain accepts — is untested.

---

## 7. Settled 2026-07-21 — `from:` is `package.node`, arrows are `artifact::interface`

Recorded in full because the wrong answer shipped in `SKILL.md`, `emission-profile.md`,
`rossystem-syntax.md`, `worked-examples.md`, `agents/ros-modeler.md`, the linter's hints and one
regenerated fixture, and because the *mirror* half of the defect had gone entirely unnoticed.

**What was wrong.** The skill emitted `from: "<package>.<artifact>"`. It must be
`"<package>.<node>"`. Separately — and this was not in the defect report — every document also
described the arrow target as `<node>::<interface>`. That is wrong too, in the opposite direction:
it is `<artifact>::<interface>`.

**Why.** `RosQNP.xtend` is a custom `IQualifiedNameProvider`, so stock Xtext scoping never applies:

| Object | Qualified name | Level skipped |
|---|---|---|
| `Node` | `pkg.name + "." + node_name`, from `obj.eContainer.eContainer as Package` | the **artifact** |
| `Publisher`/`Subscriber`/`ServiceServer`/`ServiceClient`/`ActionServer`/`ActionClient`/`Parameter` | `art.name + "::" + interface.name`, from `obj.eContainer.eContainer as Artifact` | the **package** |

Same expression shape, different class, opposite level. In almost every corpus model the artifact
and the node are spelled identically, so nothing ever surfaced the difference.

**How it was settled — three independent ways, no counter-evidence:**

1. **The source**, above.
2. **The oracle.** Cases 07/08/09 against the built `.rossystem` server:
   `package.node` + `artifact::iface` → ACCEPTED 0E/0W; `package.artifact` →
   `Couldn't resolve reference to Node 'minipkg.my_artifact'`; `node::iface` →
   `Couldn't resolve reference to Publisher 'my_node::chatter'`. **Case 08 is verbatim what the
   old skill emitted.**
3. **The corpus**, resolved against an index of all 253 `.ros2` files: 52 `from:` values node-only
   vs **0** artifact-only; 201 arrow targets artifact-only vs **0** node-only.

Note the corpus figures correct an earlier claim in this document that only `MANI01_UR`
discriminated. Many more files do; the sweep that missed them was not stripping trailing comments
from artifact lines, so artifacts written `foo: # comment` were never indexed.

**Fixed in:** `SKILL.md` (new rule 4b + self-check 10), `skills/ros-model/references/rossystem-syntax.md` §3 and §4
and §5, `emission-profile.md` rules 29/30, `skills/ros-model/references/worked-examples.md`, `agents/ros-modeler.md`,
`scripts/rosmodel_lint.py` (RM020/RM056/RM057 hints), `scripts/README.md`, and
`tests/regenerated/mani-ur/system.rossystem` — the one regenerated fixture carrying the bad form.

**Still not checkable in one file.** `RM057` can only verify that `from:` has exactly one `.`; it
cannot tell a node name from an artifact name without the cross-file index in §3. The skill is
therefore instructed to *report* the ambiguity whenever a target's artifact and node names differ.

---

## 8. Defect-fix pass 2026-07-21 — what changed, what is proven

Three confirmed defects, all fixed and all verified by execution.

| # | Defect | Status | Evidence |
|---|---|---|---|
| 1 | `from:` emitted `package.artifact` | **Fixed** | Oracle 07/08/09 + `RosQNP.xtend` + corpus sweep. §7. |
| 1b | Arrow targets documented as `node::interface` (found while fixing 1, **not** in the report) | **Fixed** | Oracle 09; corpus 201-vs-0. §7. |
| 2 | Skill could not express message fields; a `.ros` came back with type names and no fields | **Fixed** | `ros2-syntax.md` §11 expanded; new `SKILL.md` rule 8b; regenerated `turtlesim.ros` with all fields → oracle **ACCEPTED 0E/0W** (case 10); vocabulary case 11 ACCEPTED. |
| 3 | `.ros` files rejected by the linter with `RM000` | **Fixed** | 11 new rules `RM070`–`RM080`; 0 ERRORs across 31 corpus `.ros` files and RosTooling's own test resources; every ERROR severity confirmed against the oracle. |
| — | Suspected single-vs-double quote inconsistency | **Not a defect** | Audited: `SKILL.md`, `emission-profile.md` rules 13/14 and the linter already agree (single for references/names, double for data). The linter has no quote-character rule at all, which is rule 15 working as designed. Note added to rule 13 so it is not re-opened. |

### Also corrected while fixing defect 2

Two grammar facts every document had wrong, both caught by reading `Ros.xtext` against files the
oracle accepts:

- **A `.ros` file may declare several packages.** The entry rule is `PackageSet`
  (`package+=Package_Impl*`), not `Package_Impl`. `tests/oracle/cases/_deps/common_msgs.ros`
  declares nine and is ACCEPTED.
- **Several fields may share a line.** `MessagePart+=MessagePart*` has no line separator, and
  RosTooling's own `basic_msgs/common_msgs.ros` writes `time stamp string id`. One field per line
  is house style, so `RM078` is a WARNING — treating it as an error would reject files the real
  parser accepts.

### Verification actually run

| Check | Result |
|---|---|
| `rosmodel_lint.py` over 305 corpus `.ros2`/`.rossystem` | **Identical to baseline**, 0 crashes |
| `rosmodel_lint.py` over 31 corpus `.ros` (newly in scope) | 0 ERRORs from `RM070`–`RM080`; 0 crashes |
| `rosmodel_lint.py` over all 60 `.ros` in `material/code` incl. the NadiaHG brace fork | 0 crashes; brace files get one explanatory `RM079` |
| Negative control: hand-built bad `.ros` | Every new rule fires at the intended severity |
| `ask_oracle.py --all` (13 cases) | 01,02,03 ACCEPTED; 04,05,06 REJECTED — **gate intact**; 07,10,11,`_deps` ACCEPTED; 08,09,12 REJECTED |
| `roundtrip.py selftest` (3 fixtures, incl. the edited `system.rossystem`) | 4/4 each, 0 failures |
| `--hook` mode on a `.ros` | Blocks on ERROR, silent on clean |

### Not fixed, found along the way

- **`ask_oracle.py` was modified concurrently** by another workstream mid-session (JAR selection
  per file extension, `.rossystem` support). Not reverted — it is an improvement, and cases 07-09
  depend on it. Worth knowing that two sessions were editing `tests/oracle/` at once.
- **`RM075` vs `RM077` on `string<=10`.** A bounded *string* is reported as a stray-`=` (`RM077`)
  rather than a bounded array (`RM075`). Both are ERROR and the verdict is right; the rule id is
  arguably mislabelled. Cosmetic.
- **The regenerated `turtlesim.ros` drops one source comment** — `string name # Optional. A unique
  name will be created and returned if this is empty` on `Spawn.request`. That is the emission
  profile's documented comment policy, and `SKILL.md` rule 12 requires reporting it; recorded here
  so the provenance is not lost.
- **`README.md` still describes the Java blocker as live** in its prerequisites table and says the
  linter accepts `.ros2`/`.rossystem` only. Both are now stale. Updated in this pass, but the file
  has more stale-by-drift prose than one pass can safely rewrite — read it against §2 before
  trusting it.
