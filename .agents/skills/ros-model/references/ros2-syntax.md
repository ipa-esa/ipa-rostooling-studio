# `.ros2` — complete syntax reference

Grammar authority (read-only reference clone):
`C:\Users\mae\Downloads\adm-mae\CoreSense\material\code\RosTooling\plugins\`

- `de.fraunhofer.ipa.ros2.xtext\src\de\fraunhofer\ipa\ros2\Ros2.xtext`
- `de.fraunhofer.ipa.ros.xtext\src\de\fraunhofer\ipa\ros\Ros.xtext`
- `de.fraunhofer.ipa.ros.xtext\src\de\fraunhofer\ipa\ros\Basics.xtext`

All productions below are **verbatim** from those files.

---

## 1. Grammar chain and entry rule

```
grammar de.fraunhofer.ipa.ros2.Ros2 with de.fraunhofer.ipa.ros.Ros
```

`Ros2` extends `Ros`, which extends `Basics`. The first rule in `Ros2.xtext` reassigns the entry:

```
@Override
Package returns Package:
    AmentPackage
    ;
```

This is why `.ros2` cannot contain message specs — `Package_Impl` (the rule carrying `msgs:` /
`srvs:` / `actions:`) is no longer reachable.

## 2. `AmentPackage` — the root

```
AmentPackage returns AmentPackage:
    {AmentPackage}
    name=RosNames':'
    BEGIN
        ('fromGitRepo:' fromGitRepo=EString)?
        ('artifacts:'
            BEGIN
            artifact+=Artifact*
            END
        )?
        ('dependencies:' '[' dependency+=Dependency (',' dependency+=Dependency)* ']' )?
    END;
```

This is a **fixed sequence**, not an alternation: `fromGitRepo:` → `artifacts:` → `dependencies:`.
Reordering is a parse error.

`name=RosNames` — the package name can never be quoted, and per `checkNameConventionsPackage` any
uppercase character is an **ERROR**.

`dependencies:` has **0 occurrences across 253 corpus files**. It is an inline bracketed list nested
inside an indented block, a shape never exercised against the indentation lexer. **Avoid.**

## 3. `Artifact` and `Node`

```
Artifact returns Artifact:
    {Artifact}
        name=RosNames':'
        BEGIN
        (node=Node)?
        END;

Node returns Node:
    'node:' name=RosNames
    (
    ('publishers:'
        BEGIN
        publisher+=Publisher*
        END
    )|
    ('subscribers:'
        BEGIN
        subscriber+=Subscriber*
        END
    )|
    ('serviceservers:'
        BEGIN
        serviceserver+=ServiceServer*
        END
    )|
    ('serviceclients:'
        BEGIN
        serviceclient+=ServiceClient*
        END
    )|
    ('actionservers:'
        BEGIN
        actionserver+=ActionServer*
        END
    )|
    ('actionclients:'
        BEGIN
        actionclient+=ActionClient*
        END
    )|
    ('parameters:'
        BEGIN
        parameter+=Parameter*
        END
    )
    )*
    ;
```

An `Artifact` holds **at most one** `Node`. One node per artifact; multiple nodes means multiple
artifacts (or multiple files).

The block list is a repeated alternation `( A | B | … )*` — the grammar permits **any order and even
repetition**. Convention must supply the order. Emit the grammar's own declaration order:
`publishers` → `subscribers` → `serviceservers` → `serviceclients` → `actionservers` →
`actionclients` → `parameters`.

## 4. Interfaces

All six are structurally identical; only the keyword and the cross-reference target differ.

```
@Override
Publisher returns Publisher:
    {Publisher}
        name=EString':'
        BEGIN
            'type:' message=[TopicSpec|EString]
            ('ns:' namespace=Namespace)?
            ('qos:' qos=QualityOfService)?
        END
    ;
```

| Rule | `type:` refers to | Lives in `.ros` under |
|---|---|---|
| `Publisher`, `Subscriber` | `[TopicSpec\|EString]` | `msgs:` |
| `ServiceServer`, `ServiceClient` | `[ServiceSpec\|EString]` | `srvs:` |
| `ActionServer`, `ActionClient` | `[ActionSpec\|EString]` | `actions:` |

Member order `type:` → `ns:` → `qos:` is **fixed**. `type:` is mandatory; the other two are optional.

`type:` is a **cross-reference**, not a string. It must resolve to a `TopicSpec`/`ServiceSpec`/
`ActionSpec` declared in a `.ros` file that is reachable in the workspace. If it does not resolve,
`CheckMsgsRefPublisher` (and siblings) emit a WARNING. Conventional value shape:
`'<pkg>/msg/<Type>'`, `'<pkg>/srv/<Type>'`, `'<pkg>/action/<Type>'` — the `/` forces quoting.

`ns:` has **0 corpus occurrences**. Avoid.

## 5. Quality of Service

```
QualityOfService returns QualityOfService:
    {QualityOfService}
        BEGIN
        (('profile:' QoSProfile=('default_qos'|'services_qos'|'sensor_qos'|'parameter_qos'))? &
        ('history:' History=('keep_last'|'keep_all'))? &
        ('depth:' Depth=Integer0)? &
        ('reliability:' Reliability=('best_effort'|'reliable'))? &
        ('durability:' Durability=('transient_local'|'volatile'))? &
        ('lease_duration:' LeaseDuration=(EString | 'infinite'))? &
        ('liveliness:' Liveliness=('automatic'|'manual'))? &
        ('lifespan:' Lifespan=(EString | 'infinite'))? &
        ('deadline:' Deadline=(EString | 'infinite'))?)
        END
    ;
```

`&` is an Xtext unordered group: all nine are optional and may appear in **any order**.

**The last four are HEAD-only and MUST NOT be emitted.** They were added by commit `3d9e5ebd`
(2025-10-16), 14.5 months after the pinned language server was built (2024-08-01). The JAR's token
table has no entry for them, so they fail lexing. Their value literals `infinite`, `automatic`,
`manual` are likewise absent.

Emittable values, with corpus frequency:

| Field | Values | Corpus A | Corpus B |
|---|---|---:|---:|
| `reliability` | `reliable` / `best_effort` | 118 | 57 |
| `durability` | `volatile` / `transient_local` | 106 | 57 |
| `depth` | bare integer | 16 | 0 |
| `history` | `keep_last` / `keep_all` | 15 | 0 |
| `profile` | `default_qos` / `services_qos` / `sensor_qos` / `parameter_qos` | 5 | 0 |

Default to `reliability` + `durability`. The machine generator that produced Corpus B knows the other
three and deliberately comments them out — most plausibly because it could not determine a
trustworthy value.

## 6. Parameters

```
@Override
Parameter returns Parameter:
    {Parameter}
        name=EString':'
        BEGIN
        'type:' type=ParameterType
        ('ns:' namespace=Namespace)?
        ('value:' value=ParameterValue)?
        ('qos:' qos=QualityOfService)?
        END
        ;
```

Order `type:` → `ns:` → `value:` → `qos:` is fixed.

### `default:` — a fifth member, and it belongs to the *type*

This is the single most-missed rule in the language, and it was wrong in every version of this
reference before 2026-07-21. **`default:` is legal on every scalar parameter type, not just on
`Array[T]`.** Verbatim, Basics.xtext:72-110:

```
ParameterIntegerType returns ParameterIntegerType:
    {ParameterIntegerType}
    'Integer'
    ('default:' default=ParameterInteger)?;

ParameterStringType returns ParameterStringType:
    {ParameterStringType}
    'String'
    ('default:' default=ParameterString)?;

ParameterDoubleType returns ParameterDoubleType:
    {ParameterDoubleType}
    'Double'
    ('default:' default=ParameterDouble)?;

ParameterBooleanType returns ParameterBooleanType:
    {ParameterBooleanType}
    'Boolean'
    ('default:' default=ParameterBoolean)?;

ParameterBase64Type returns ParameterBase64Type:
    {ParameterBase64Type}
    'Base64'
    ('default:' default=ParameterBase64)?;

ParameterArrayType returns ParameterArrayType:
    'Array' '[' type=ParameterType ']'
    ('default:' default=ParameterList)?
;
```

`default:` is a member of `ParameterType`, **not** of `Parameter`. Because
`'type:' type=ParameterType` carries **no `BEGIN`/`END`** around it, the `default:` token lands at
the same indent as `type:` — it reads as a sibling key of `type:`, and it binds immediately after
it, *before* `ns:` / `value:` / `qos:`. The full member order is therefore:

```
'name':
  type: <keyword>
  default: <literal>      # optional, part of the type
  ns: …                   # optional
  value: <literal>        # optional
  qos: …                  # optional
```

**`default:` and `value:` are NOT interchangeable.** `default:` is the type's declared default;
`value:` is the value assigned to this parameter. They occupy different slots in the metamodel and
a file may legally carry both. Rewriting `default:` to `value:` is a **silent semantic change** —
it parses cleanly, the linter stays quiet, and the model now says something different.

> **Preserve whichever key the input uses.** Never normalise one into the other. If a source model
> writes `default: 2.0`, emit `default: 2.0`.

The `default:` literal obeys the same value traps as `value:` — lowercase booleans, and a `.` or
exponent on any `Double` (§6 below).

```
ParameterType returns ParameterType:
    ParameterListType | ParameterStructType | ParameterIntegerType | ParameterStringType | ParameterDoubleType | ParameterBooleanType | ParameterBase64Type | ParameterArrayType; // | ParameterDateType;

ParameterValue returns ParameterValue:
    ParameterString | ParameterBase64 | ParameterInteger | ParameterDouble | ParameterBoolean | ParameterList | ParameterStruct; // | ParameterDate;
```

Note `ParameterDateType` and `ParameterAnyType` are **defined elsewhere in the file but absent from
these two alternations** — `Date`, `ParameterAny` and `Any` are unreachable. Never emit them.

```
ParameterArrayType returns ParameterArrayType:
    'Array' '[' type=ParameterType ']'
    ('default:' default=ParameterList)?
;

ParameterList returns ParameterSequence:
    {ParameterSequence}
        '[' value+=ParameterValue ( ',' value+=ParameterValue )* ']'
;
```

Emittable type keywords, always **bare** (they are grammar keywords — quoting one is a parse error):
`Integer`, `String`, `Double`, `Boolean`, `Base64`, `Array[T]`, `List[…]`, `Struct[…]`.

Recommended: `Integer`, `String`, `Double`, `Boolean`, `Array[T]` (194 corpus occurrences).
Avoid `List`, `Base64`, and especially `Struct` *values* — `ParameterStruct` mixes brackets with
`BEGIN`/`END` indentation tokens and has zero corpus coverage.

### Value terminals — two silent-corruption traps

```
terminal BOOLEAN: 'true'|'false';
terminal DOUBLE returns ecore::EDouble: (DIGIT* | ('-' DIGIT*) ) (('.' DECINT*) | (('.' DIGIT*)? ('E'|'e') ('-'|'+')? DIGIT*));
terminal DECINT: '0' | ('1'..'9' DIGIT*) | ('-''0'..'9' DIGIT*) ;
```

- `True` is not a `BOOLEAN`. It does not error — it falls through the `ParameterValue` alternation
  and is captured by `ParameterString`, so `type: Boolean` + `value: True` silently yields a
  **string**. No validator checks value-against-type. 910 Corpus A occurrences are latently corrupt;
  Corpus B is 48/48 lowercase. **Emit lowercase only, and normalise `True` in any input you read.**
- `DOUBLE` requires a `.` or an exponent. `type: Double` + `value: 10` matches `DECINT` and silently
  becomes a `ParameterInteger`. Emit `10.0`. Corpus violations: 0 — preserve the invariant.

## 7. Names and quoting

```
EString returns ecore::EString:
    STRING | ID;

RosNames returns ecore::EString:
    ROS_CONVENTION_A | ID | 'node'
;

terminal ROS_CONVENTION_A:
    ( ('/' ID ) | ( ID '/' ) )* ;
```

`RosNames` has **no `STRING` branch** — the three positions it governs can never be quoted:

| Position | Rule | Corpus |
|---|---|---|
| package name (col 0) | `AmentPackage.name`, Ros2.xtext | 253/253 bare |
| artifact name | `Artifact.name`, Ros.xtext | bare |
| `node:` name | `Node.name`, Ros.xtext | 267/267 bare |

### Correction to the project briefing

The briefing said *"no slashes in node names."* **That is not accurate.** `ROS_CONVENTION_A` matches
repeated `'/' ID` and `ID '/'` groups, and the corpus contains six such node names:

```
/bt_lifecycle_node
/cam_ns/cam_frame_controller
/image_transport_republish
/light_controller
/mimic_controller
/sound_controller
```

The correct rule: a `node:` name **can never be quoted**, and can never contain `.`, `-`, `::`, or a
space. Leading and interior slashes in the `/segment/segment` shape are legal and attested. Prefer a
plain `ID` when you have the choice.

`EString` positions (interface names, parameter names, `type:` refs, `fromGitRepo:`) **must** be
quoted whenever they contain `.`, `/`, `::`, `-`, a space, or are empty. Xtext's `STRING` terminal
accepts both `'single'` and `"double"` quotes — the choice is semantically invisible, so never flag
the other one as an error when *reading*. When *writing*: single quotes for `.ros2` names and type
refs (Corpus B 184/185), double quotes for string data (Corpus B 31/31).

## 8. Namespaces

```
Namespace returns Namespace:
    GlobalNamespace | RelativeNamespace_Impl | PrivateNamespace;
```

Keywords `GlobalNamespace`, `RelativeNamespace`, `PrivateNamespace`. **0 corpus occurrences.** Avoid.

## 9. Comments

```
@Override
terminal SL_COMMENT: '#' !('\n'|'\r')*;
```

`#` to end of line, hidden. No block comments. **Do not author comments** — their interaction with
the indentation lexer is unverified. If unavoidable, place a whole-line `#` comment at the
**parent** indent level, strictly shallower than the content that follows (Corpus B is 171/171
shallower).

### Comments in a file you are TRANSCRIBING — do not silently discard

"Do not author comments" governs what you *add*. It says nothing about a comment that is already
in a source file you are regenerating, and reading it as licence to delete is a real information
loss that no tool can detect: the round-trip harness compares parsed facts, and comments are not
facts. Two corpus cases where the comment was the only record of something:

- `MANI01_UR/pick_and_place.rossystem` — a trailing comment on `controller_manager` naming the four
  controllers it spawns (`joint_state_broadcaster`, `io_and_status_controller`,
  `speed_scaling_state_broadcaster`, `force_torque_sensor_broadcaster`), plus three
  `### Addition from David` provenance markers.
- `CS_ros2model_TBs/MT.rossystem` — ~90 trailing lines of commented-out node definitions
  (`navigation_cognition_master`, `nav_goal_cognitive_module` with 23 parameters), which read as
  deliberately disabled configuration rather than dead text.

**Policy.** Do not carry comments into the emitted model. But every comment you drop must be
reported back to the caller as **DROPPED PROVENANCE**, listing file, line number and the text
verbatim, so the decision to discard it is theirs and not yours. A large commented-out *region* is
a stronger signal still — say plainly that it looks like disabled configuration and ask whether it
should be restored as live model content.

## 10. Verbatim corpus example

Source: `material\code\CS_ros2model_TBs\eu.coresense.testbeds\MT\components\transform_listener_impl_5755c11ba8a0.ros2`
(reproduced exactly; the on-disk file is CRLF due to `core.autocrlf=true`, the stored blob is LF)

```
transform_listener_impl:
  artifacts:
    transform_listener_impl:
      node: transform_listener_impl
      subscribers:
        'tf':
          type: 'tf2_msgs/msg/TFMessage'
        'tf_static':
          type: 'tf2_msgs/msg/TFMessage'
```

Note: file stem (`transform_listener_impl_5755c11ba8a0`) ≠ package name
(`transform_listener_impl`). This is normal — **76 of 253** corpus files differ. Always read the
package name from line 1 of the model, never from the filename.

Source: `...\CS_ros2model_TBs\eu.coresense.testbeds\MT\components\gazebo_sensor_B1_controller.ros2`

```
gazebo_sensor_b1_controller:
  artifacts:
    gazebo_sensor_B1_controller:
      node: gazebo_sensor_B1_controller
      publishers:
        'kmriiwa/base/state/LaserB1Scan':
          type: 'sensor_msgs/msg/LaserScan'
          qos:
          # profile:
          # history: UNKNOWN
          # depth:
            reliability: reliable
            durability: volatile
      subscribers:
        'clock':
          type: 'rosgraph_msgs/msg/Clock'
```

This one file demonstrates four rules at once: lowercase package vs. original-case artifact/node
(rule 2), the `'…/…'` quoting forced by slashes (rule 1), the QoS subset, and Corpus B's stock
comment placement — which the emission profile drops.

---

## 11. `.ros` companion files

`.ros2` `type:` values are references. The definitions live in `.ros`, whose entry rule is
**`PackageSet`** — and note the `*`: **one `.ros` file may declare several top-level packages**,
unlike a `.ros2`, which declares exactly one.

```
PackageSet returns PackageSet:            # Ros.xtext:11-14
    {PackageSet}
    package+=Package_Impl*
    ;
```

`tests/oracle/cases/_deps/common_msgs.ros` declares nine packages (`actionlib_msgs`,
`diagnostic_msgs`, `geometry_msgs`, `nav_msgs`, `sensor_msgs`, …) at column 0 in one file and is
ACCEPTED, 0 errors. Each of those is a `Package_Impl`:

```
Package_Impl returns Package:
    {Package}
    name=RosNames':'
    BEGIN
     ('fromGitRepo:' fromGitRepo=EString)?
     ('dependencies:' '[' dependency+=Dependency (',' dependency+=Dependency)* ']' )?
     (('msgs:'
        BEGIN
        spec+=TopicSpec*
        END
     )|
    ('srvs:'
        BEGIN
        spec+=ServiceSpec*
        END
    )|
    ('actions:'
        BEGIN
        spec+=ActionSpec*
        END
    ))*
    END;

TopicSpec returns TopicSpec:
    {TopicSpec}
    name=(EString|'Header'|'String')
    BEGIN
        'message' (BEGIN message=MessageDefinition END)?
    END
    ;

ServiceSpec returns ServiceSpec:
    {ServiceSpec}
    name=EString
    BEGIN
        'request' (BEGIN request=MessageDefinition END)?
        'response' (BEGIN response=MessageDefinition END)?
    END;

ActionSpec returns ActionSpec:
    {ActionSpec}
    name=EString
    BEGIN
        'goal' (BEGIN goal=MessageDefinition END)?
        'result' (BEGIN result=MessageDefinition END)?
        'feedback' (BEGIN feedback=MessageDefinition END)?
    END;
```

Note the spec name has **no trailing `:`** — unlike every other named element in the language. The
message body is optional; a bare `message` / `request` + `response` line suffices to declare the
type exists.

### `MessageDefinition` — field bodies ARE expressible

An earlier version of this reference stopped at `message=MessageDefinition` and told the emitter to
drop all field content. **That was wrong** and cost real information on every `.ros` file
regenerated before 2026-07-21. The production expands, verbatim:

```
MessageDefinition returns MessageDefinition:      # Ros.xtext:107-109
    {MessageDefinition}
        MessagePart+=MessagePart*;

MessagePart returns primitives::MessagePart:      # Basics.xtext:201-204
    Type = AbstractType
    Data =(KEYWORD | MESSAGE_ASIGMENT | EString)
;

terminal MESSAGE_ASIGMENT:                        # Basics.xtext:206-208
    ((ID|STRING)'='(ID|STRING|INT|'-'INT))
;
```

A field is exactly two tokens: **a type, then a name.**

`MessagePart+=MessagePart*` has **no line separator**, so the grammar permits any number of fields
on one line. RosTooling's own test resources do exactly that —
`plugins\de.fraunhofer.ipa.ros.xtext.testsesourcesasic_msgs\common_msgs.ros` writes

```
    GoalID
     message
       time stamp string id
```

which is two fields, `time stamp` and `string id`. That file is ACCEPTED by the oracle. **One
field per line is a house-style rule, not a grammar rule** — emit one per line, but never treat a
multi-field line in a source file as an error.

#### Nesting depth — exactly four levels

The grammar opens a `BEGIN` at four points, so a field line sits **four levels deep**, i.e. at
column 8 on the normalised 2-space ladder:

```
turtlesim:              level 0   package        (BEGIN 1 opens after the ':')
  msgs:                 level 1   block keyword  (BEGIN 2 opens after the ':')
    Pose                level 2   spec name      (BEGIN 3 opens — note NO trailing ':')
      message           level 3   body keyword   (BEGIN 4 opens)
        float32 x       level 4   FIELD
```

`srvs:` / `actions:` are identical; only the level-3 keyword changes (`request` / `response`,
`goal` / `result` / `feedback`). The spec name at level 2 is the one named element in the whole
language with **no trailing `:`** — the `BEGIN` is synthesised from the indent alone.

#### Legal `AbstractType` tokens (Basics.xtext:210-213), all bare keywords

| Kind | Tokens |
|---|---|
| Scalar | `bool` `int8` `uint8` `int16` `uint16` `int32` `uint32` `int64` `uint64` `float32` `float64` `string` `byte` `char` `time` `duration` `Header` |
| Array | `bool[]` `int8[]` `uint8[]` `int16[]` `uint16[]` `int32[]` `uint32[]` `int64[]` `uint64[]` `float32[]` `float64[]` `string[]` `byte[]` `char[]` |
| Ref to a spec | `SpecBaseRef` = `[TopicSpec\|EString]` — a quoted **fully qualified** `"pkg/msg/Type"` |
| Array of refs | `ArraySpecRef` = `[TopicSpec\|EString]'[]'` — `"pkg/msg/Type"[]` |

Three traps in that table:

- **`time`, `duration` and `Header` have NO array form.** The alternation lists exactly fourteen
  `…Array` rules and `timeArray` / `durationArray` / `HeaderArray` are not among them. `time[]` is
  a parse error.
- The keyword is `string`, not `string0` — `string0` is the *rule* name (`string0 returns
  primitives::string: {primitives::string} 'string';`). Same for `char` (rule `char`, eClass
  `char0`).
- **`Header` is capitalised** and is its own primitive, not a spec reference.

#### There are no bounded or fixed-size arrays

`ArraySpecRef` and the fourteen `…Array` rules hard-code the literal `'[]'`. ROS IDL's
`float32[3]` and `string<=10` have no production at all. **Verified against the oracle** — both
are lexer errors:

```
line 5  Invalid token float32[3
line 5  mismatched input ']' expecting RULE_END
```

Emit the unbounded form and mention the lost bound in your response.

#### Referencing a type from another package — always fully qualified

`SpecBaseRef` is a cross-reference, and `RosQNP.xtend` gives every `TopicSpec` the qualified name
`pkg_name + "/msg/" + spec_name` (`/srv/`, `/action/` for the other two). So a reference is written
`"package/msg/Type"`, quoted — the `/` forces it.

**This applies to types in the *same* file too.** A bare `AllTypes` does not resolve even when
`AllTypes` is defined three lines above it in the same package; there is no short form. Verified
against the oracle:

```
line 38  Couldn't resolve reference to TopicSpec 'AllTypes'.
```

Writing `"probe_msgs/msg/AllTypes"` instead made the same file ACCEPTED, 0 errors.

#### Constants

A **constant** uses the `MESSAGE_ASIGMENT` terminal, which is lexed as a single token — so there
must be **no spaces around the `=`**:

```
uint8 FAN_OFF=0
uint8 FAN_ON=1
string MODE="auto"
```

`uint8 FAN_OFF = 0` splits into three tokens and does not parse.

#### Field names that collide with keywords are fine

`Data` accepts the `KEYWORD` rule (Basics.xtext:377), which is
`'goal' | 'message' | 'result' | 'feedback' | 'name' | 'value' | 'service' | 'type' | 'action' |
'duration' | 'time'`. So `string name`, `string value` and `int32 type` are all legal field lines
and need no quoting. Verified against the oracle.

#### An empty body is still legal

`'message' (BEGIN message=MessageDefinition END)?` — the parenthesised group is optional, so a
bare `message` / `request` + `response` line declares that the type exists with no fields. A
`response` with no fields is extremely common (see `SetPen` below).

#### Complete worked example — the emittable form of `turtlesim.ros`

`material\code\ros-model-examples\turtlesim\msgs\turtlesim.ros`, normalised to the 2-space ladder
and alphabetised. **This exact text was fed to the language server and came back ACCEPTED, 0
errors** (oracle case `10-ours-turtlesim-msgs`). The on-disk original uses ragged 4/6/7/9-space
indents and has trailing spaces on most field lines; the parser tolerates both, but do not imitate
them.

```
turtlesim:
  msgs:
    Color
      message
        uint8 r
        uint8 g
        uint8 b
    Pose
      message
        float32 x
        float32 y
        float32 theta
        float32 linear_velocity
        float32 angular_velocity
  srvs:
    Kill
      request
        string name
      response
    SetPen
      request
        uint8 r
        uint8 g
        uint8 b
        uint8 width
        uint8 off
      response
    Spawn
      request
        float32 x
        float32 y
        float32 theta
        string name
      response
        string name
    TeleportAbsolute
      request
        float32 x
        float32 y
        float32 theta
      response
    TeleportRelative
      request
        float32 linear
        float32 angular
      response
```

Note `Kill`, `SetPen`, `TeleportAbsolute` and `TeleportRelative` all keep a bodiless `response` —
that is the correct transcription, not an omission.

Spec references and constants, from
`material\code\ros-model-examples\mobile_base_system\msgs\puma_motor_msgs.ros`:

```
    MultiFeedback
      message
        Header header
        "puma_motor_msgs/msg/Feedback"[] drivers_feedback
```

**Emit the fields whenever the input supplies them.** Fall back to the bodiless form only when the
field list is genuinely unknown — and when you do, say so in your response: *"message bodies
omitted, field definitions were not available in the input."*

Verbatim, `material\code\ros-model-examples\cob\MOBI01\ros1_msgs\dynamic_reconfigure.ros`
(abridged — first two msgs and the srv):

```
dynamic_reconfigure:
  msgs:
    BoolParameter
      message
    Config
      message
  srvs:
    Reconfigure
      request
      response
```

And the complete minimal case,
`material\code\ros-model-examples\cob\MOBI01\ros1_msgs\cob_base_controller_utils.ros`:

```
cob_base_controller_utils:
  msgs:
    WheelCommands
      message
```
