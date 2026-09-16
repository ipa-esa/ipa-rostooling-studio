# `.rossystem` — complete syntax reference

Grammar authority:
`C:\Users\mae\Downloads\adm-mae\CoreSense\material\code\RosTooling\plugins\de.fraunhofer.ipa.rossystem.xtext\src\de\fraunhofer\ipa\rossystem\RosSystem.xtext`

Validator:
`...\de.fraunhofer.ipa.rossystem.xtext\src\de\fraunhofer\ipa\rossystem\validation\RosSystemValidator.xtend`

All productions below are **verbatim**.

> ## No oracle exists for this language
> Confirmed two ways — archive-entry listing *and* raw byte grep, case-insensitive, across all three
> shipped JARs: **zero matches for `rossystem`.** The grammar has existed since 2018 but was never
> built into a language server. `.rossystem` output can never be machine-validated against shipped
> artifacts, not even after installing a JDK 19. Everything here is source-derived. Treat the
> validator rules below as the specification and the static linter as the only enforcement.

---

## 1. Grammar chain and root

```
grammar de.fraunhofer.ipa.rossystem.RosSystem with de.fraunhofer.ipa.ros.Basics

System returns System:
    RosSystem
;

RosSystem returns System:
    name=EString':'
    BEGIN
        ('fromFile:' fromFile=EString)?
        (
        ('subSystems:'
            BEGIN
            components+=SubSystem*
            END
        ) |
        ('processes:'
            BEGIN
            processes+=Process*
            END
        ) |
        ('nodes:'
            BEGIN
            components+=RosNode*
            END
        ) |

        ('parameters:'
            BEGIN
            parameter+=Parameter*
            END
        ) |
        ('connections:'
            BEGIN
            connections+=Connection*
            END
        )
        )*
    END;
```

Two things to notice.

**The system name is `EString`, not `RosNames`.** Unlike a `.ros2` package name, it *may* be quoted.
It is also not subject to `checkNameConventionsPackage` — that check targets `ros.Package`, and a
`rossystem.System` is a different metaclass. Uppercase here is not an error. Emit lowercase anyway
for consistency.

**Preserve the declared name when regenerating an existing model.** The system name is a
cross-reference target for `subSystems:`, so changing it is a semantic difference, not a style
improvement. Rename only when authoring a genuinely new model.

**Name collisions with a referenced package.** A `System` and a `ros::Package` are different
metaclasses and may legally share a name. Avoid it anyway: `from:` values are resolved by name, and
whether the two namespaces are kept apart under stock Xtext scoping has not been verified here.
When authoring fresh and the natural system name equals a package named in any `from:`, prefer
`<basename>_system`.

**The five blocks are a repeated alternation** — any order, and they may repeat. Convention supplies
the order: `fromFile:` → `subSystems:` → `processes:` → `nodes:` → `parameters:` → `connections:`.
(`fromFile:` alone is grammar-fixed to come first.)

## 2. `fromFile:` — optional in the grammar, mandatory in practice

`fromFileHelper` is a confirmed live bug. Verbatim:

```xtend
  @Check
  def fromFileHelper(System system ) {
      if (!system.fromFile.empty){
              info('The format for the FromFile attribute is: "NameOfThePackage/Path/to/ExecutableLaunchFile.launch.py"'
                  ,null,NOT_IN_THE_SYSTEM)
      }
      if (!system.fromFile.toString.contains("/")){
              error('Path not valid, the format for the FromFile attribute is: "NameOfThePackage/Path/to/ExecutableLaunchFile.launch.py"'
                  ,null,NOT_IN_THE_SYSTEM)
      }
  }
```

Neither dereference is null-guarded, and `SystemImpl.java:70` sets
`protected static final String FROM_FILE_EDEFAULT = null;`. So omitting `fromFile:` — which 32 of 52
corpus models do — throws an NPE out of the `@Check`. Writing `fromFile: ""` skips the NPE but hits
the unconditional ERROR on the second branch.

**Exactly one input traverses this method cleanly**: a non-empty quoted string containing `/`.

```
fromFile: "my_pkg/launch/bringup.launch.py"
```

Always emit it. The `/` and `.` force the quoting anyway.

### What to write when you do not know the launch file

This field is the one place the skill forces you to produce data you may not have — 32 of 52 corpus
models omit `fromFile:` entirely, so the common case is that no launch file is known. That collides
head-on with the anti-invention rule elsewhere in this skill (worked-examples.md §1 step 7: *"Omit
the block entirely rather than guess"*). The conflict is resolved by a **derivation ladder**, not by
guessing:

1. **The caller named a launch file** → use it verbatim.
2. **A file matching `<pkg>/launch/<system>.launch.py` exists on disk** → verify with a `Glob`, then
   use the real path.
3. **Otherwise** → emit the sentinel

   ```
   fromFile: "TODO_PACKAGE/launch/TODO.launch.py"
   ```

   It contains `/`, so it satisfies `fromFileHelper`'s `contains("/")` check and avoids the NPE,
   while remaining unmistakably a placeholder.

**Never invent a plausible-looking real path.** `"cs4mt_bringup/launch/manufacturing_tb.launch.py"`
is worse than the sentinel, not better: it is indistinguishable from a verified path, so it silently
converts a known-unknown into a false fact. Every regeneration run before 2026-07-21 fabricated one
of these. **Rung 2 means the launch file itself, found with `Glob` — not a `fromFile:` line seen in
another model** (a corpus file, a vendored catalogue file, or this plugin's own past output under
`tests/regenerated/`). A path in one of those is that file's author's claim, not something you
verified; copying it is rung 3 wearing rung 2's clothes.

Say which rung on the `fromFile:` line itself, with a trailing comment (SKILL.md rule 5):

```
fromFile: "ur_bringup/launch/ur5e.launch.py"   # caller-supplied
fromFile: "ur_bringup/launch/ur5e.launch.py"   # on disk: /abs/path/ur_bringup/launch/ur5e.launch.py
fromFile: "TODO_PACKAGE/launch/TODO.launch.py"
```

The sentinel needs no comment (`RM066` already marks it); `rosmodel_lint.py`'s `RM096` (WARNING)
flags any other `fromFile:` with no provenance comment.

Whichever rung you land on, if it is not rung 1 or 2, **report it to the caller** as
*fromFile: SYNTHESISED — not extracted from the source, supplied only to avoid the fromFileHelper
NPE* — and if you saw a candidate path somewhere that you correctly rejected as not being rung 1 or
2 evidence, say where. `rosmodel_lint.py` emits `RM066` (INFO) on any `fromFile:` containing `TODO`,
so the placeholder stays visible rather than settling in as if it were real.

## 3. `RosNode`

```
RosNode returns RosNode:
    {RosNode}
    name=EString':'
    BEGIN
    'from:'from=[ros::Node|EString]
    ('namespace:' namespace=EString)?
    ('interfaces:'
        BEGIN
            rosinterfaces+=RosInterface*
        END)?
    ('parameters:'
        BEGIN
            rosparameters+=RosParameter*
        END)?
    END
;
```

Member order `from:` → `namespace:` → `interfaces:` → `parameters:` is **fixed**.

`name=EString` — the node label here **is** quotable (unlike `.ros2`'s `node:`). Quote it
unconditionally with double quotes.

`from:` is a cross-reference to a `ros::Node`, written **`"<packageName>.<nodeName>"`**. The `.`
forces quoting; 296/296 corpus occurrences are quoted, never bare.

**Read both halves from the target model's contents, never from its filename.** `bt_navigator.ros2`
declares package `nav2_bt_navigator`, so the reference is `"nav2_bt_navigator.bt_navigator"`. 76 of
253 `.ros2` files have a stem differing from the declared package.

### SETTLED — the second segment is the NODE, and it is NOT the artifact

Earlier revisions of this file carried an UNRESOLVED box that told the emitter to write
`package.artifact`. **That was wrong.** The qualified name is computed by a custom
`IQualifiedNameProvider`, not by stock Xtext scoping, and it is unambiguous — verbatim from
`material\code\RosTooling\plugins\de.fraunhofer.ipa.ros.xtext\src\de\fraunhofer\ipa\ros\RosQNP.xtend`:

```xtend
class RosQNP extends DefaultDeclarativeQualifiedNameProvider{
  override getFullyQualifiedName(EObject obj) {
    if (obj instanceof Node) {
      val node_name = obj.name
      val pkg = obj.eContainer.eContainer as Package
      return getConverter().toQualifiedName(pkg.name + "." + node_name);
    }
```

`obj.eContainer` is the `Artifact`; `obj.eContainer.eContainer` is the `Package`. **The artifact
level is skipped entirely.** A `Node`'s qualified name is exactly two segments, package then node.
This is also why the stock three-segment `package.artifact.node` derivation never applies —
`RosQNP` overrides it. (`RosSystemScopeProvider.xtend` being an empty class body is irrelevant: the
*scope provider* is stock, the *name provider* is not.)

### The trap: `from:` and the arrow targets use DIFFERENT schemes

This is why the defect survived review. In the same `RosQNP.xtend`, all six interface kinds — and
`Parameter` — resolve through the **artifact**:

```xtend
if (obj instanceof Publisher) {
  val interface = obj as Publisher
  val art = obj.eContainer.eContainer as Artifact
  return getConverter().toQualifiedName(art.name + "::" + interface.name);
}
```

For a `Publisher`, `eContainer` is the `PublisherList` and `eContainer.eContainer` is the
`Artifact` — so the name is `artifact::interface`, with the *package* skipped. Exactly the mirror
image of the `Node` case.

| Position in a `.rossystem` | Qualified name | Skipped level |
|---|---|---|
| `from:` | `package.node` | **artifact** |
| `pub->` / `sub->` / `ss->` / `sc->` / `as->` / `ac->` target | `artifact::interface` | **package** |
| node-level parameter `from` (`- "label": "X::param"`) | `artifact::parameter` | **package** |

So one file legitimately names the same `.ros2` two different ways. Both are correct; neither
generalises to the other.

#### Worked example where the three names all differ

`material\code\ros-model-examples\manipulation\MANI01_UR\pick_and_place.ros2` declares:

```
moveit2_scripts:                                    <- package
  artifacts:
    pick_and_place:                                 <- artifact
      node: simple_pick_and_place_applictation      <- node   (sic; misspelled upstream)
      publishers:
        attached_collision_object:
          type: "moveit_msgs/msg/AttachedCollisionObject"
```

The correct references into it are:

```
from:  "moveit2_scripts.simple_pick_and_place_applictation"     package . NODE
arrow: "pick_and_place::attached_collision_object"              ARTIFACT :: interface
```

Writing `from: "moveit2_scripts.pick_and_place"` — package plus *artifact* — does not resolve.
`material\code\ros-model-examples\manipulation\MANI01_UR\system.rossystem` gets this right, and so
does the MANI01_PILZ variant. **Preserve the upstream node name verbatim, misspelling included** —
it is what the reference has to match.

#### Corpus corroboration — measured, both directions

Every corpus `from:` value and every arrow target was resolved against an index of all 253 `.ros2`
files' artifact names, node names and interface names (measured 2026-07-21):

| Reference | resolves as **NODE** only | resolves as **ARTIFACT** only | ambiguous (`artifact == node`) | target not in corpus |
|---|---|---|---|---|
| `from:` (339 total) | **52** | **0** | 244 | 43 |
| arrow target (788 total) | **0** | **201** | 499 | 88 |

Zero counter-examples in either direction. The 244 / 499 ambiguous cases are why the earlier
reading survived so long: in most models the artifact and the node are spelled the same, so the
majority of the corpus discriminates nothing.

`rossdl` independently agrees on `from:` — it derives the node class from
`from:.strip('"').split('.')[1]`.

`rosmodel_lint.py` emits `RM057` when the second segment is not a single dotted segment. It cannot
check node-vs-artifact from one file — that needs the cross-file index described in `STATUS.md` §3.
**When the artifact and node names of the target differ, say so in your response** and state which
one you used.

**Node labels must be unique within `nodes:`.** `RosSystemConnection` resolves nodes by name, so a
collision makes every connection touching it ambiguous. `MT.rossystem` declares `bt_navigator`
twice (lines 45, 215 — byte-identical) and `robot_state_publisher` twice (lines 3, 242 — *different*
interface sets), and `yaml.safe_load` silently keeps only the last, so a downstream consumer like
rossdl loses a whole node without warning. On collision:

- blocks byte-identical → drop the later one;
- blocks differ → keep both, and suffix the later label with a disambiguator drawn from a
  distinguishing interface (`robot_state_publisher_joint_states`).

Either way, report the collision to the caller. The linter already catches this as `RM009` (ERROR,
duplicate key in the same mapping) — no new rule is needed.

`namespace:` has **0 corpus occurrences**. Avoid.

## 4. Interfaces and the six arrow forms

```
RosInterface returns RosInterface:
    '-'name=EString':' (reference=InterfaceReference)
;

InterfaceReference returns InterfaceReference:
    RosPublisherReference |
    RosSubscriberReference |
    RosServiceServerReference |
    RosServerClientReference |
    RosActionServerReference |
    RosActionClientReference;


RosPublisherReference returns RosPublisherReference:
    "pub->" {RosPublisherReference} from=[ros::Publisher|EString]
;

RosSubscriberReference returns RosSubscriberReference:
    "sub->" {RosSubscriberReference} from=[ros::Subscriber|EString]
;

RosServiceServerReference returns RosServiceServerReference:
    "ss->" {RosServiceServerReference} from=[ros::ServiceServer|EString]
;

RosServerClientReference returns RosServiceClientReference:
    "sc->" {RosServiceClientReference} from=[ros::ServiceClient|EString]
;

RosActionServerReference returns RosActionServerReference:
    "as->" {RosActionServerReference} from=[ros::ActionServer|EString]
;

RosActionClientReference returns RosActionClientReference:
    "ac->" {RosActionClientReference} from=[ros::ActionClient|EString]
;
```

Line shape:

```
- "<local_label>": <arrow>-> "<artifactName>::<interfaceName>"
```

The arrow **target** is `<artifactName>::<interfaceName>` — **the artifact, not the node.**
`artifactName` is the key under `artifacts:` in the target `.ros2`; `interfaceName` is the quoted
interface name inside that artifact's `publishers:` / `subscribers:` / … block. `::` forces
quoting; 622/622 corpus occurrences are quoted.

An earlier revision of this file said `<nodeName>::<interfaceName>`. **That was wrong**, and it is
the mirror image of the `from:` defect — see the SETTLED box in §3. `RosQNP.xtend` computes an
interface's qualified name as `art.name + "::" + interface.name` from
`obj.eContainer.eContainer as Artifact`, for all six interface kinds and for `Parameter`. Corpus:
201 arrow targets resolve as artifact-only, **0** as node-only.

Mnemonic for the whole file: **`from:` climbs past the artifact to the package; the arrows stop at
the artifact.**

**Grammar quirk worth knowing:** the rule is named `RosServerClientReference` but
`returns RosServiceClientReference`. The rule name and the eClass name differ. `checkPortPatterns`
tests `eClass.name`, so it correctly sees `RosServiceClientReference` — the mismatch is cosmetic,
but do not be misled when reading the grammar.

**Local labels must be unique within a node.** `RosSystemConnection` resolves `[RosInterface|EString]`
by name, so duplicates break cross-referencing. When a node both publishes and subscribes `clock`,
Corpus B prefixes the subordinate one: `sub_clock`, `sub_bond`, `sub_cascade_lifecycle_states`.

Group interfaces by kind (pub → sub → ss → sc → as → ac), alphabetical within each kind.

Corpus B orders `as->` **before** `ss->` in several nodes (`controller_server`, `planner_server`,
`behavior_server` in `MT.rossystem`). **Do not adopt it** — use pub, sub, ss, sc, as, ac here too,
matching the `.ros2` block order for the same reason given in worked-examples.md §2 point 2: the
grammar's own declaration order and a 54:5 Corpus A majority both put services first.

**Node order within `nodes:` is free — preserve the source order.** Unlike interface and parameter
entries, the `nodes:` block is *not* sorted. Corpus B's `MT.rossystem` does not sort it, and the
order frequently encodes bring-up sequence (`robot_state_publisher` first; `controller_manager`
before its spawners). `rosmodel_lint.py` deliberately does not apply `RM040` here. Sorting it is a
silent behavioural change if any consumer ever derives launch order from it.

## 5. `RosParameter`

```
RosParameter returns RosParameter:
    '-' name=EString':' from=[ros::Parameter|EString]
    BEGIN
    'value:'value=ParameterValue
    END
;
```

Two lines, the second indented one level:

```
- "publish_frequency": "robot_state_publisher::publish_frequency"
  value: 20.0
```

`from` is `"<artifactName>::<parameterName>"` — the **artifact**, same scheme as the arrow targets
(`RosQNP.xtend` handles `Parameter` in the same `art.name + "::" + interface.name` branch). Not the
node, and not the package. `value:` is **mandatory** here (no `?`), unlike the optional `value:` on
a `.ros2` `Parameter`.

**The boolean trap applies here in full**: lowercase `true` / `false` only.

**The Double trap is NOT locally decidable in a `.rossystem`.** A `RosParameter` has no `type:`
field — the declared type lives in the referenced `.ros2` parameter block, not in this file. So for
`- "rate": "foo::rate"` / `value: 10` you cannot tell from this file whether `10` is correct or
should be `10.0`.

- **Transcribing** → preserve the source literal **exactly**. Do not add a `.0`.
- **Authoring fresh** → open the referenced `.ros2`, read that parameter's `type:`, and only then
  choose between `10` and `10.0`.

`CheckParameterValue` is a ~110-line recursive type-checker that uses **instance fields as loop
counters** (`int i; int j;` at class scope) and recurses from inside those loops, corrupting the
caller's iteration on return. Nested container values produce arbitrary, non-deterministic
diagnostics. **Author scalar values only** — Integer, Double, Boolean, String.

> **This constrains what you AUTHOR, not what you may transcribe.** When a source parameter already
> holds a List, Array or Struct value, **reproduce it as a real `[...]` list** — "verbatim" means
> keep the parameter and every element, not copy a quoting defect. A source `value: "['a', 'b']"`
> is a string; the oracle rejects it against the declared `Array`/`List` type with `Expect a list
> of elements`. Convert it to `value: ["a", "b"]`, and report the conversion (SKILL.md §8, check
> 12) — this is a source defect, not a style to preserve; `rosmodel_lint.py` flags it as `RM095`.
> Never delete a parameter to satisfy this rule — dropping `source_list` to keep the file "clean"
> loses model content and is a far worse outcome than a non-deterministic diagnostic. Warn the
> caller that `CheckParameterValue` may report unstable results for that parameter.

## 5b. System-level `parameters:` — a different shape entirely

The `System` production (§1) carries its own `('parameters:' BEGIN parameter+=Parameter* END)`.
That `Parameter` is **`ros::Parameter` from Basics.xtext, not the `RosParameter` of §5.** The two
look nothing alike and confusing them produces a structurally different model that the linter will
not catch:

| | node-level (§5, `RosParameter`) | system-level (`ros::Parameter`) |
|---|---|---|
| Shape | list — `- "name": "node::param"` | mapping — `"name":` |
| Members | `value:` (mandatory) | `ns:`? → `type:` → `default:`? → `value:`? |
| Carries a `type:` | no | **yes** |

```
  parameters:
    "robot_ip":
      type: String
      default: "192.168.56.2"
    "ur_type":
      type: String
      default: "ur5e"
```

Note the member order: `RosSystem.xtext` extends **`Basics`**, not `Ros2`, so the system-level
`Parameter` is the *unoverridden* Basics rule — `('ns:')? 'type:' ('value:')?` — where `ns:`
precedes `type:` and there is **no `qos:` member at all**. This is the reverse of the `.ros2`
ordering, which comes from `Ros2.xtext`'s `@Override`. `default:` binds to the type exactly as in
`.ros2` (ros2-syntax.md §6).

Names take double quotes per §8. Corpus witnesses:
`ros-model-examples/manipulation/MANI01_UR/pick_and_place.rossystem` and
`MANI02_common/ur5e_cell_moveit.rossystem`.

## 6. Connections

```
//By default the grammar will parser RosSystemConnection, i.e., connections of ports
// explicitly referenced within the system model. RosConnections are also implemented but not used for now.
Connection returns Connection:
    ( => RosSystemConnection) | RosConnection
;

RosSystemConnection returns RosSystemConnection:
    '-''['from=[RosInterface|EString]','to=[RosInterface|EString]']'
;

RosConnection returns RosConnection:
    ( => RosTopicConnection) | ( => RosServiceConnection) | RosActionConnection
;

RosTopicConnection returns RosTopicConnection:
    '-''['from=[ros::Publisher|EString]','to=[ros::Subscriber|EString]']'
;
```

**Emit only `RosSystemConnection`** — `- [label_a , label_b]` referencing local interface labels.

Three separate validator methods (`checkIfInterfaceInSystem`, `checkPortPatterns`, `MatchPortMsgs`)
each open with an unconditional `connection as RosSystemConnectionImpl`. Any `RosConnection` variant
throws `ClassCastException` in all three. The grammar's own comment concedes the branch is unused.

### Direction — `checkPortPatterns` (ERROR)

```xtend
      var List<String> validFromType = newArrayList('RosPublisherReference','RosServiceServerReference','RosActionServerReference')
      var List<String> validToType = newArrayList('RosSubscriberReference','RosServiceClientReference','RosActionClientReference')
```

`from` is **always** the server/publisher. Exactly three legal pairings:

| `from` | `to` |
|---|---|
| `pub->` | `sub->` |
| `ss->` | `sc->` |
| `as->` | `ac->` |

This is fully checkable inside one file: resolve each label to its arrow prefix and apply the table.

### Type identity — `MatchPortMsgs` (ERROR)

```xtend
      if (from_type !== to_type){
          error("A connection can only be formed by interfaces with the same type, "+from_connection.name+" and "+to_connection.name+" have different types.", null, TYPE_NOT_MATCH)
      }
```

`!==` is Xtend **identity**, not `.equals` and not name comparison. Both endpoints must resolve to
the *same* `TopicSpec` / `ServiceSpec` / `ActionSpec` instance. Practically: emit byte-identical
`type:` strings at both ends and let the linker share the instance.

Caution — `from_type` and `to_type` are **class-level fields** on an injected singleton validator,
assigned only inside six `if` branches. If a reference fails to match any branch (an unresolved
proxy), the field retains the value from the *previous* connection and the comparison uses stale
data. Never trust a clean result for a connection whose endpoints might not resolve.

### Membership — `checkIfInterfaceInSystem` / `checkIfNodeInSystem` (ERROR)

Both connection endpoints must be interfaces of nodes declared in this system's `nodes:` block (or
one level inside a referenced subsystem). Every node named in a process's `nodes: [...]` must be
declared in the same file's `nodes:` block.

The `to` check sits in the `else` of the `from` check, so a connection with a bad `from` never gets
its `to` validated — expect to fix these one at a time.

## 7. Processes and subsystems

```
Process returns Process:
    {Process}
      name=EString':'
      BEGIN
      ('nodes:' '['components+=[RosNode|EString] (',' components+=[RosNode|EString])*']')?
      ('threads:'threads=Integer0)?
      END
;

SubSystem returns SubSystem:
    system=[System|EString]
;
```

`processes:` / `threads:` — **1 corpus file**, and that file (`image_system_example.rossystem`) is
itself malformed, indenting its sibling `nodes:` block 5 spaces. **Avoid** when authoring.

### If you must emit `processes:`

"Avoid" is advice for authoring, not permission to refuse. When a source model already declares
processes — or the caller asks for them — emit this normalised form:

```
  processes:
    "process1":
      nodes: ["image_filter", "consumer"]
      threads: 1
```

Four decisions the raw production does not answer, resolved:

- **Process name** is `EString` → **double-quote** it, per §8.
- **`nodes: [...]` members** are `[RosNode|EString]` cross-references. Quoting is optional (a bare
  `ID` parses) but **emit them double-quoted**, matching §8 and the node labels they point at. Keep
  the quoting identical to the corresponding key in the `nodes:` block.
- **`threads:`** is `Integer0` → **bare**, never quoted, exactly like `depth:`.
- **Spacing** — `["a", "b"]`, one space after each comma, none inside the brackets.

Every name in `nodes: [...]` **must** also appear as a key in this file's `nodes:` block, or
`checkIfNodeInSystem` raises an ERROR (`rosmodel_lint.py` mirrors this as `RM054`).

`subSystems:` — 19 files, but every sampled example is low quality (one indents its reference with a
tab and leaves it bare). Emit it whenever composition is requested — see SKILL.md §8d — normalised
to the 2-space ladder, and flag the output as reconstructed rather than copied.

**Quote the subsystem reference with double quotes**, consistent with §8. Bare is legal —
`SubSystem: system=[System|EString]` admits a plain `ID` — but the corpus examples that leave it
bare are defective in other respects and are not a style to follow.
`checkIfInterfaceInSystem` recurses exactly **one** level and casts each subcomponent unconditionally
to `RosNode`, so a subsystem containing another subsystem throws `ClassCastException`, and interfaces
two levels down are silently missing from the membership set. **Keep nesting flat.**

## 8. Quoting summary

Everything quotable in `.rossystem` takes **double quotes**, unconditionally — Corpus B is
191/191 interface labels, 32/32 `from:`, 191/191 arrow targets, 37/37 parameter refs, and ~2000/2000
across both corpora in every reference position.

> **Contradiction flagged.** `docs/grammar-subset.md` §5.5 states *"the clean corpus uses single
> quotes throughout."* That is **wrong for `.rossystem`** and half-right for `.ros2`. Measured:
> Corpus B `.rossystem` is 100% double quotes; Corpus B `.ros2` splits single (names, type refs) vs.
> double (string data). `emission-profile.md` §1.3 rules 13–14 have the correct measurement and
> should be followed. The distinction is style-only in any case — Xtext's `STRING` terminal accepts
> both quote characters, so a validator must never flag the other one.

## 9. Verbatim corpus examples

### Clean — `material\code\CS_ros2model_TBs\eu.coresense.testbeds\MT\MT.rossystem` (first 19 lines)

```
manufacturing_tb:
  nodes:
    robot_state_publisher:
      from: "robot_state_publisher.robot_state_publisher"
      interfaces:
        - "robot_description": pub-> "robot_state_publisher::robot_description"
        - "tf": pub-> "robot_state_publisher::tf"
        - "tf_static": pub-> "robot_state_publisher::tf_static"
        - "clock": sub-> "robot_state_publisher::clock"
        - "kmriiwa/arm/joint_states": sub-> "robot_state_publisher::kmriiwa/arm/joint_states"
      parameters:
        - "frame_prefix": "robot_state_publisher::frame_prefix"
          value: ""
        - "ignore_timestamp": "robot_state_publisher::ignore_timestamp"
          value: false
        - "publish_frequency": "robot_state_publisher::publish_frequency"
          value: 20.0
        - "use_sim_time": "robot_state_publisher::use_sim_time"
          value: true
```

Deviations to **not** copy: this file has no `fromFile:` (trips the NPE of §2), no `connections:`
block at all, and leaves the first node label bare while quoting the other 31.

Note `"kmriiwa/arm/joint_states"` — the interface label is the full topic path, and it appears
identically on both sides of the `::` boundary. That is normal.

### Connections — `material\code\ros-model-examples\pub_sub_ros2\pub_sub_system.rossystem` (complete)

```
system:
  nodes:
    publisher_component:
      from: "examples_rclcpp_minimal_publisher.publisher_lambda"
      interfaces:
       -publisher: pub-> "publisher_lambda::topic"
    subscriber_component:
      from: "examples_rclcpp_minimal_subscriber.subscriber_lambda"
      interfaces:
       -subscriber: sub-> "subscriber_lambda::topic"
  connections:
   -[publisher, subscriber]
```

The **only** thing to take from this file is the connection semantics: `publisher` is the local label
declared `-publisher: pub->`, `subscriber` is the `sub->` label, and `from` is the publisher. Correct
direction.

Everything else is defective and must be normalised: 3-space indents, no space after `-`, bare
labels, no `fromFile:`, no final newline. See `worked-examples.md` §3 for the corrected emission.

### Negative example — `material\code\ros-model-examples\turtlesim\turtlesim_system.rossystem`

```
       - cmd_vel_sub : sub-> "turtlesim_node::cmd_vel"
```

The space before `:` is **legal** (whitespace is hidden) but is a formatting defect — do not emit it,
and do not report it as a syntax error when reading. Half the corpus `.rossystem` files also contain
literal tabs. **The corpus is a semantic reference, not a style reference.** Compare against it
semantically; byte-diffing is meaningless.
