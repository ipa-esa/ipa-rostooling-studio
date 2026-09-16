# Worked examples

Three complete transformations, from input to emitted file, with the reasoning at each decision
point. Sources are cited; Corpus B (`CS_ros2model_TBs`) is preferred for style.

Throughout: 2-space indent, spaces only, LF, one trailing newline.

---

## 1. Trivial single-node package

**Input.** A ROS 2 package `examples_rclcpp_minimal_publisher` from
`https://github.com/ros2/examples`, containing one node `publisher_lambda` that publishes
`std_msgs/msg/String` on topic `topic`.

**Reasoning.**

1. *Which file?* Interfaces of a package's node → `.ros2`. No message types are being **defined**
   (`std_msgs/msg/String` is an upstream type), so no `.ros` file is needed here. If `std_msgs` had
   no `.ros` model in the workspace, the `type:` reference would not resolve and
   `CheckMsgsRefPublisher` would emit a WARNING — a warning, not an error, so the file is still
   usable.
2. *Package name.* `examples_rclcpp_minimal_publisher` — already `[a-z0-9_]`. Emit bare at column 0.
   Uppercase here would be an ERROR.
3. *Artifact vs. node.* An `Artifact` holds at most one `Node`. One node → one artifact. Name the
   artifact after the node: `publisher_lambda`. Both are `RosNames` → bare.
4. *`fromGitRepo:`.* Available, so emit it. Grammar order is fixed: it must precede `artifacts:`.
   It is string **data** → `"double quotes"`.
5. *Interface name.* `topic` is a bare `ID` and would parse unquoted — but emit `'topic'` anyway.
   Rule 11 of the emission profile: quote `.ros2` interface names unconditionally, which removes a
   per-name decision that Corpus A gets inconsistently right.
6. *Type ref.* `std_msgs/msg/String` contains `/`, which is illegal in `ID`, so quoting is
   **forced**, not optional. Single quotes — it is a reference, not data.
7. *QoS.* Not specified by the input. Omit the block entirely rather than guess. Never invent
   `reliability`/`durability` values.

**Output** — `examples_rclcpp_minimal_publisher.ros2`

```
examples_rclcpp_minimal_publisher:
  fromGitRepo: "https://github.com/ros2/examples"
  artifacts:
    publisher_lambda:
      node: publisher_lambda
      publishers:
        'topic':
          type: 'std_msgs/msg/String'
```

The corpus original
(`material\code\ros-model-examples\pub_sub_ros2\rosnodes\examples_rclcpp_minimal_publisher.ros2`)
is semantically identical but writes `topic:` bare, `"std_msgs/msg/String"` in double quotes, and
has no trailing newline. All three are legal; all three deviate from the profile. **This is the
normalisation the skill exists to perform** — and it is why comparisons against corpus originals must
be semantic, never byte-wise.

The matching subscriber package, same shape with `subscribers:` in place of `publishers:`:

```
examples_rclcpp_minimal_subscriber:
  fromGitRepo: "https://github.com/ros2/examples"
  artifacts:
    subscriber_lambda:
      node: subscriber_lambda
      subscribers:
        'topic':
          type: 'std_msgs/msg/String'
```

Note both files use the **byte-identical** type string `'std_msgs/msg/String'`. That is what lets
`MatchPortMsgs` resolve both endpoints to one shared `TopicSpec` instance when these two nodes are
connected in §3.

---

## 2. Package with services, QoS, and parameters

**Input.** Node `lifecycle_manager_navigation` in package `nav2_lifecycle_manager`, artifact
`lifecycle_manager`. Introspection reports:

- publishes `bond` (`bond/msg/Status`) and `diagnostics` (`diagnostic_msgs/msg/DiagnosticArray`),
  both reliable + volatile
- subscribes `bond` (`bond/msg/Status`)
- serves `lifecycle_manager_navigation/is_active` (`std_srvs/srv/Trigger`) and
  `lifecycle_manager_navigation/manage_nodes` (`nav2_msgs/srv/ManageLifecycleNodes`)
- parameters including `/bond_disable_heartbeat_timeout` (bool false), `autostart` (bool true),
  `bond_respawn_max_duration` (double 10.0), `diagnostic_updater.period` (double 1.0),
  `node_names` (string array), `use_sim_time` (bool false)

**Reasoning.**

1. *Package vs. artifact vs. node — all three differ.* Package `nav2_lifecycle_manager`, artifact
   `lifecycle_manager`, node `lifecycle_manager_navigation`. Do not collapse them. The `.rossystem`
   `from:` reference will be `"nav2_lifecycle_manager.lifecycle_manager"` — package + **artifact**,
   not the node name.
2. *Block order.* Grammar permits any order; convention is the declaration order. Here:
   `publishers` → `subscribers` → `serviceservers` → `parameters`. Corpus B places action blocks
   before service blocks in 5 files; that placement is **not** adopted (contradicted by both the
   grammar's declaration order and a 54:5 Corpus A majority).
3. *Duplicate name across kinds.* `bond` is both published and subscribed. In `.ros2` this is fine —
   they live in different blocks, so there is no collision. The uniqueness rule (`sub_bond`) applies
   only to `.rossystem` interface **labels**, which share one namespace per node.
4. *Service names contain `/`* → quoting forced.
5. *QoS.* Only `reliability` and `durability` are known, so emit only those. `profile`, `history`,
   `depth` are JAR-supported but unknown here — omit. `lease_duration`, `liveliness`, `lifespan`,
   `deadline` are **never** emitted: absent from the pinned parser's token table, they fail lexing.
6. *Parameter member order* is fixed: `type:` → `value:`. Type keywords bare, names quoted.
7. *Value traps.* Booleans lowercase — `false`, never `False`, which would silently parse as a
   *string* under a `Boolean` type. `bond_respawn_max_duration` is a Double: emit `10.0`, never `10`,
   which would silently become a `ParameterInteger`.
8. *`'/bond_disable_heartbeat_timeout'`* keeps its leading slash — that is the real parameter name.
   The `/` forces quoting. It sorts first in byte order.
9. *`'diagnostic_updater.period'`* contains a `.` → quoting forced. Its lowercase-after-dot shape
   also keeps `checkNameConventionsParameters` quiet.
10. *`node_names`* is an array of strings → `Array[String]`, bare keyword, no space before `[`.
    Elements are string data → double quotes. (Array *values* are the one container form with real
    corpus support: 194 occurrences.)
11. *Sorting.* Entries alphabetical within each block.

**Output** — `lifecycle_manager_navigation.ros2`

```
nav2_lifecycle_manager:
  artifacts:
    lifecycle_manager:
      node: lifecycle_manager_navigation
      publishers:
        'bond':
          type: 'bond/msg/Status'
          qos:
            reliability: reliable
            durability: volatile
        'diagnostics':
          type: 'diagnostic_msgs/msg/DiagnosticArray'
          qos:
            reliability: reliable
            durability: volatile
      subscribers:
        'bond':
          type: 'bond/msg/Status'
      serviceservers:
        'lifecycle_manager_navigation/is_active':
          type: 'std_srvs/srv/Trigger'
        'lifecycle_manager_navigation/manage_nodes':
          type: 'nav2_msgs/srv/ManageLifecycleNodes'
      parameters:
        '/bond_disable_heartbeat_timeout':
          type: Boolean
          value: false
        'autostart':
          type: Boolean
          value: true
        'bond_respawn_max_duration':
          type: Double
          value: 10.0
        'diagnostic_updater.period':
          type: Double
          value: 1.0
        'node_names':
          type: Array[String]
          value: ["map_server", "amcl", "controller_server", "planner_server", "behavior_server", "bt_navigator"]
        'use_sim_time':
          type: Boolean
          value: false
```

Source:
`material\code\CS_ros2model_TBs\eu.coresense.testbeds\MT\components\lifecycle_manager_navigation.ros2`.
Two intentional deviations from the original: the three stock `# profile:` / `# history: UNKNOWN` /
`# depth:` placeholder comments are dropped (they carry no model semantics and their indentation
interaction with the `BEGIN`/`END` lexer is unverified), and the array elements are re-quoted from
`'single'` to `"double"` because they are string **data**, not references.

---

## 3. Multi-node system with connections

**Input.** Compose the two packages from §1 into one system: `publisher_lambda` publishes `topic`,
`subscriber_lambda` subscribes to it, wire them together. Launch file is
`examples_rclcpp_minimal_publisher/launch/pub_sub.launch.py`.

**Reasoning.**

1. *Which file?* Composition + wiring → `.rossystem`.
2. *`fromFile:` is mandatory in practice.* The grammar marks it optional, but `fromFileHelper`
   dereferences `system.fromFile` twice with no null guard and the field defaults to `null`, so
   omitting it throws an NPE out of the `@Check`. `fromFile: ""` skips the NPE but hits the
   unconditional `contains("/")` ERROR. Emit a quoted path containing `/`. The corpus original omits
   it — do not copy that.
3. *`from:` references.* `"<packageName>.<nodeName>"` — the **node**, not the artifact — both read
   from the **model contents** of the `.ros2` files, never from their filenames. Here:
   `"examples_rclcpp_minimal_publisher.publisher_lambda"`, where `publisher_lambda` is the `node:`
   value. (`artifact == node` in this file, so it does not discriminate; see rossystem-syntax.md §3.)
4. *Node labels.* `EString` here (unlike `.ros2`'s `node:`), so quote them with double quotes.
5. *Interface labels.* Must be unique **within a node**. Across nodes they may repeat, but distinct
   labels make connections readable, so use `"publisher"` and `"subscriber"`. Quote unconditionally.
6. *Arrow targets.* `<artifactName>::<interfaceName>` — the **artifact**, which is the opposite
   level from `from:` in point 3. Here the `.ros2` names its artifact `publisher_lambda` too, so
   both happen to read the same; when artifact and node differ, the arrow takes the artifact.
   `topic` is the interface name inside it. The `::` forces quoting.
7. *Connection direction.* `checkPortPatterns` requires `from` = server/publisher. The publisher
   label goes first: `- ["publisher" , "subscriber"]`. Reversing it is an ERROR
   (*"The input port (to) must be a Subscriber"*).
8. *Type identity.* `MatchPortMsgs` compares with `!==` — object identity. Both `.ros2` files declare
   `type: 'std_msgs/msg/String'` byte-identically, so both endpoints resolve to the same `TopicSpec`
   instance. Matching *strings* is what makes matching *objects* happen.
9. *Connection form.* `RosSystemConnection` only — `- [label , label]` over local interface labels.
   The `RosTopicConnection` alternative (referencing `ros::Publisher` directly) throws
   `ClassCastException` in three separate validator methods.
10. *Block order.* `fromFile:` → `nodes:` → `connections:`. No `subSystems:`, no `processes:`
    (1 malformed corpus file — avoid), no system-level `parameters:`.

**Output** — `pub_sub_system.rossystem`

```
pub_sub_system:
  fromFile: "examples_rclcpp_minimal_publisher/launch/pub_sub.launch.py"
  nodes:
    "publisher_component":
      from: "examples_rclcpp_minimal_publisher.publisher_lambda"
      interfaces:
        - "publisher": pub-> "publisher_lambda::topic"
    "subscriber_component":
      from: "examples_rclcpp_minimal_subscriber.subscriber_lambda"
      interfaces:
        - "subscriber": sub-> "subscriber_lambda::topic"
  connections:
    - ["publisher" , "subscriber"]
```

Corpus original: `material\code\ros-model-examples\pub_sub_ros2\pub_sub_system.rossystem`.
Semantically equivalent; every difference is a deliberate normalisation:

| Original | Emitted | Why |
|---|---|---|
| 3-space indents | 2-space | profile rule 2 |
| `-publisher:` (no space after `-`) | `- "publisher":` | readability; whitespace is hidden either way |
| bare labels | quoted | profile rule 12 |
| no `fromFile:` | present, quoted, with `/` | avoids the `fromFileHelper` NPE |
| name `system:` | `pub_sub_system:` | **authoring a new model here** — see the caution below |
| no final newline | one trailing LF | profile rule 4 |

> **Caution on the rename.** This example *authors* a new system from two packages, so picking a
> descriptive name is free. **When regenerating an existing model, preserve its declared name
> verbatim** — the name is a cross-reference target for `subSystems:`, so changing it is a semantic
> difference rather than a style fix, and the round-trip harness will correctly report it as one.
> `MANI01_UR/system.rossystem` is literally named `system:`; leave it that way.

### Adding a service connection

Same file, extended. If `node_a` serves `'reset'` (`std_srvs/srv/Empty`) and `node_b` calls it, the
`.ros2` sides declare:

```
      serviceservers:
        'reset':
          type: 'std_srvs/srv/Empty'
```

```
      serviceclients:
        'reset':
          type: 'std_srvs/srv/Empty'
```

and the system wires them with the `ss->` / `sc->` pair — **not** `pub->`/`sub->`:

```
    "node_a_component":
      from: "pkg_a.node_a"
      interfaces:
        - "reset_server": ss-> "node_a::reset"
    "node_b_component":
      from: "pkg_b.node_b"
      interfaces:
        - "reset_client": sc-> "node_b::reset"
  connections:
    - ["reset_server" , "reset_client"]
```

Server first. `- ["reset_client" , "reset_server"]` is an ERROR on two counts: `sc->` is not in
`validFromType`, and the `to` is not a `RosServiceClientReference`.

Actions follow the identical pattern with `as->` / `ac->` and a `type:` of
`'<pkg>/action/<Type>'`, resolving to an `ActionSpec` declared under `actions:` in a `.ros` file.

---

## 4. Failure catalogue

Each line below is a real defect the emitter must not produce. Severity per the validators.

| Wrong | Right | Consequence |
|---|---|---|
| `My_Package:` | `my_package:` | **ERROR** ×2 (one per uppercase char) |
| `node: 'my_node'` | `node: my_node` | parse error — `RosNames` has no `STRING` branch |
| `type: std_msgs/msg/String` | `type: 'std_msgs/msg/String'` | parse error — `/` illegal in `ID` |
| `type: 'Boolean'` | `type: Boolean` | parse error — type names are keywords |
| `value: True` | `value: true` | **silent corruption** — parses as a string under a Boolean type |
| `value: 10` under `type: Double` | `value: 10.0` | **silent corruption** — becomes a ParameterInteger |
| `msgs:` inside a `.ros2` | put it in a `.ros` file | parse error — unreachable from `AmentPackage` |
| `deadline: "1000000"` | omit the field | parse error — no token in the pinned JAR |
| `deadline: "5000000000"` (if unpinned) | ≤ `"2147483647"` | **ERROR** — exceeds 32-bit `Integer.parseInt` |
| `- [sub_label , pub_label]` | `- [pub_label , sub_label]` | **ERROR** — `from` must be the publisher |
| `fromFile:` omitted | `fromFile: "pkg/launch/x.launch.py"` | NPE escaping the `@Check` |
| `fromFile: ""` | same as above | **ERROR** — `"".contains("/")` is false |
| `artifacts:` before `fromGitRepo:` | `fromGitRepo:` first | parse error — fixed sequence |
| `value:` before `type:` | `type:` first | parse error — fixed sequence |
| a literal tab | 2 spaces | corrupts `BEGIN`/`END` synthesis |
| `'':` as a name | omit the entry | extraction defect; breaks sorting and references |
| inlined URDF/XML | `type: String` + quoted path, or `""` | not expressible in the grammar |
| rewriting `default: 2.0` → `value: 2.0` | keep `default: 2.0` | **silent semantic change** — different metamodel slots; `default:` is legal on every scalar type (Basics.xtext:72-110) |
| dropping `.ros` message fields | emit `float32 x` etc. | information loss — `MessagePart` is expressible (ros2-syntax.md §11) |
| inventing `fromFile: "pkg/launch/real_looking.launch.py"` | `"TODO_PACKAGE/launch/TODO.launch.py"` | a fabricated path is indistinguishable from a verified one |
| synthesising `connections:` the source did not declare | emit none, list candidates | `MatchPortMsgs` needs object identity — unverifiable in one file |
| sorting a `.rossystem` `nodes:` block | preserve source order | may encode bring-up sequence |
| silently deleting a container-valued `.rossystem` parameter | transcribe it, warn | loses model content |

## 5. Defects in the *source*

§4 catalogues defects the emitter must not **produce**. This section covers defects the input
already **contains** — the dominant case in practice, since most work with this skill is
transcription rather than greenfield authoring. Observed classes, all from the corpus:

| Class | Example |
|---|---|
| Truncated cross-reference | `pub-> "robot_state_publisher::c"` (`MT.rossystem`) — a bare `c` where every sibling is a full topic name, *and* semantically impossible (gazebo publishes `/clock`, not `robot_state_publisher`) |
| Duplicate mapping keys | `bt_navigator` and `robot_state_publisher` each declared twice in one `nodes:` block |
| Blank lines inside an indented block | two in `MT.rossystem`'s `nodes:` |
| Large commented-out regions | ~90 trailing lines of disabled node definitions |
| Misspelled identifiers | `simple_pick_and_place_applictation` (`MANI01_UR/system.rossystem`) |
| Structural malformation | `image_system_example.rossystem` indents a sibling block 5 spaces |
| Empty names | `'':` in three Corpus B `.ros2` files |

**Policy — one rule, applied in this order:**

1. **Preserve verbatim by default.** A defect you do not understand is information you do not own.
   A misspelled node name is still the name the cross-reference resolves against; "fixing" it
   breaks the link.
2. **Drop only when the token cannot parse**, or when the content is demonstrably corrupt *and*
   preserving it would emit an invalid file.
3. **Normalise formatting freely** — indentation, quoting, line endings, ordering-within-a-block.
   That is what this skill is for and it is fact-preserving.
4. **Report every deviation back to the caller**, itemised. This is not optional: the linter cannot
   see most of these, and the round-trip harness compares parsed facts, so a dropped comment or a
   silently repaired name leaves no trace anywhere.

When a defect forces a judgement call — as the truncated `::c` target does — state the reasoning
and the choice explicitly rather than quietly picking one. The caller may know that `c` was
`clock`.

---

## 6. Verification status of everything above

**None of these outputs has been executed against the language server.** This machine has Java
1.8.0_481; the shipped JARs declare `Bundle-RequiredExecutionEnvironment: JavaSE-19`.

- §1 and §2 (`.ros2`) *can* be verified once a JDK 19+ is installed — see SKILL.md for the stdio
  launch procedure. Mark them **unverified-pending-Java-19** until then.
- §3 (`.rossystem`) can **never** be verified against shipped artifacts: no `rossystem` language
  server was ever built. Confirmed by archive listing and raw byte grep of all three JARs.
  Verification would require building `de.fraunhofer.ipa.rossystem.xtext` from source.

The single highest-value test to run once unblocked is the **negative control**: emit one `.ros2`
using `lease_duration:` and one using `msgs:`, and confirm the server rejects both. That converts
the two hard exclusions in SKILL.md from inference to fact.
