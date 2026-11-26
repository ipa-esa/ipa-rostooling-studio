# Offline authoring: syntax and ordering rules

Read this file **only when no `rostooling-mcp` server is connected** and you have to write model text
by hand. With the server connected, its tools write quoting, indentation and member order, and
`validate_ros_model` reports violations. Rule numbers match `SKILL.md`.

### 1. Quoting discipline (the #1 failure)

`EString returns ecore::EString: STRING | ID;` — Basics.xtext:391.
`ID` admits `[a-zA-Z_][a-zA-Z0-9_]*` only. So **any name containing `.`, `/`, `::`, `-`, a space,
or that is empty MUST be double- or single-quoted**, or it is a parse error.

Emit quotes as follows:

| Position | Quote | Why |
|---|---|---|
| `.ros2` interface name, parameter name, `type:` **message ref** | `'single'`, always — even when bare would parse | removes a per-name decision |
| `.ros2` parameter `value:` string, `fromGitRepo:` | `"double"` | it is data, not a reference |
| every quoted token in `.rossystem` (`from:`, arrow target, interface label, param ref, `fromFile:`) | `"double"`, always | Corpus B is 100% double there |
| `.ros2` package name (col 0), artifact name, `node:` name | **NEVER quoted** | `RosNames` has no `STRING` alternative |
| parameter **type keywords** (`Boolean`, `String`, `Double`, `Integer`, `Array[String]`) | **NEVER quoted** | they are grammar keywords; quoting one is a parse error |

```
RosNames returns ecore::EString:
    ROS_CONVENTION_A | ID | 'node'          # Basics.xtext:394-396 — no STRING branch
```

Corpus: 253/253 package names bare, 267/267 `node:` names bare, 0 quoted.

### 3. Indentation

Spaces only, **exactly 2 per level**, LF endings, one trailing newline, no tabs, no trailing
whitespace, no blank lines inside an indented block. The corpus is inconsistent (26/51 `.rossystem`
files contain tabs) — **do not imitate it.** Emit the normalised form.

### 6. Node names: never quoted, restricted charset

`node:` takes `RosNames`, so it can never be quoted and can never contain `.`, `-`, `::`, or a
space. Leading/trailing slashes *are* legal (`ROS_CONVENTION_A: ( ('/' ID) | (ID '/') )*`) and are
attested in the corpus (`/light_controller`, `/cam_ns/cam_frame_controller`) — but prefer a plain
`ID`. See the correction note in `ros2-syntax.md`.

### 7. QoS durations — quoted 32-bit ints (moot under the pinned profile)

If `lease_duration:` / `lifespan:` / `deadline:` are ever unpinned, each value must be a **quoted**
digit string parseable by `Integer.parseInt` (max `2147483647` ns ≈ 2.1 s — `"5000000000"` is an
ERROR) or the bare keyword `infinite`. Under the current profile you never emit these fields at all
(§ below), so this rule is documentation for a future toolchain change, not something to implement.

### 9. Ordering (grammar-fixed — violating is a parse error)

| Rule | Mandatory order |
|---|---|
| `AmentPackage` | `fromGitRepo:` → (`pluginbases:` and `artifacts:`, either order) → `dependencies:` |
| `.ros2` plugin base | `class:` (quoted `"pkg::Base"`), `header:`, `loader:` — all three required, any order |
| `.ros2` artifact | `node:` **or** `plugins:` — never both; a plugins artifact holds nothing else |
| `.ros2` node | `node:` → `lifecycle:` (optional) → interface/parameter/`pluginslots:` (`base: <package>.<Base>`, `parameter:` — not also a node parameter — optional `format: class \| library`, `multiple: true \| false`, `default: <package>.<Plugin>`; any order, except the inline `{ ... }` form, which takes them in that order) → trailing `@<extension>:` blocks |
| `.ros2` plugin | `implements: <package>.<Base>` **first** → interface & parameter blocks → trailing `@<extension>:` blocks (no `loader:`; it comes from the base) |
| `.ros2` interface | `type:` → `ns:` → `active_in:` → `qos:` (`active_in:` only valid on lifecycle nodes) |
| `.ros2` parameter | `type:` → **`default:`** → `ns:` → `value:` → `qos:` |
| `.rossystem` node | `from:` → `namespace:` → `slots:` (`<slot>: [<package>.<Plugin>, ...]`; `[]` for none) → `interfaces:` → `parameters:` → trailing `@<extension>:` blocks (also permitted on `System` and indented under `- <iface>: ...` `RosInterface` entries) |
| `.rossystem` **system-level** parameter | `ns:` → `type:` → `default:` → `value:` (no `qos:`) |

Inside `@<extension>:` blocks, cross-references to declared interfaces or parameters use `pub->`, `sub->`, `ss->`, `sc->`, `as->`, `ac->`, or `param->`.

`default:` belongs to `ParameterType`, not to `Parameter`, and is legal on **every** scalar type —
not only on `Array[T]`. It has no `BEGIN`/`END` of its own, so it sits as a sibling of `type:`
immediately after it. **`default:` and `value:` are different slots and are never interchangeable**
— preserve whichever the input uses.

**When the input is source code there is no slot to preserve, so choose by meaning: a compiled-in
`declare_parameter()` or `generate_parameter_library` default is a `default:`.** That keeps
`value:` for the *deployed* value, which comes from a launch file or a controller config and
belongs in the `.rossystem`, not here. The extractors emit `default:` for this reason; a
transcribed corpus file using `value:` for the same thing is preserved as-is per the rule above,
so the two can legitimately differ between a generated and a transcribed model of one package. See `ros2-syntax.md` §6.

The two `parameters:` shapes in a `.rossystem` are different rules and are easy to confuse — the
node-level one is a **list** (`- "name": "node::param"` + `value:`), the system-level one is a
**mapping** with a `type:`. See `rossystem-syntax.md` §5 and §5b.

Convention (grammar permits any order): node blocks go `publishers` → `subscribers` →
`serviceservers` → `serviceclients` → `actionservers` → `actionclients` → `parameters`; interface
labels unique within a node (`sub_clock` when `clock` is already taken by a publisher).

**Sorting is scoped per file type — it is not a blanket rule:**

| Block | Order |
|---|---|
| `.ros2` interface entries, parameter entries, `artifacts:` | **sort alphabetically** |
| `.rossystem` interface labels within a node | **sort alphabetically**, grouped by kind |
| `.rossystem` `nodes:` | **preserve source order** — it often encodes bring-up sequence |

A `.rossystem` built with the MCP tools follows the same split where it can: nodes appear in the
order you added them (add them in source order), and each node's interfaces in the order they were
first connected — leave the tool's order as it is rather than re-sorting it by hand.

---

## Pinned-profile exclusion

**Updated 2026-07-21 — this restriction has been lifted.** It previously said *never emit* the four
newer QoS fields, because our only validator was a JAR built 2024-08-01, predating them. We rebuilt
the language server from current source, so `lease_duration:`, `liveliness:`, `lifespan:` and
`deadline:` now parse and validate correctly.

**Prefer the five long-standing fields anyway** — `profile:`, `history:`, `depth:`, `reliability:`,
`durability:`. Emit one of the four newer fields only when the input specifically calls for it.
Two reasons: no corpus model uses them, and anyone consuming your output on an older toolchain
build cannot lex the keyword — it fails as a **syntax error that invalidates the whole publisher**,
not as a warning.

**If you do emit a duration** (`lease_duration:`, `lifespan:`, `deadline:`), the value is in
**nanoseconds** and must fit a signed 32-bit int — max ≈ **2.147 seconds** — or be the literal
`infinite`. `deadline: "5000000000"` (5 s) is a validator ERROR. This is the easiest QoS mistake to
make, because every realistic timeout exceeds the limit.

The complete emittable QoS vocabulary:

```
qos:
  profile:     default_qos | services_qos | sensor_qos | parameter_qos
  history:     keep_last | keep_all
  depth:       <bare integer>
  reliability: best_effort | reliable
  durability:  transient_local | volatile
```

Default to **`reliability:` and `durability:` only** — the only two Corpus B uses. Emit
`profile:` / `history:` / `depth:` solely when the caller supplies a concrete value.

Cost of the exclusion is zero: **0 of 253** corpus `.ros2` files use any of the four fields.
Because the JAR's token set is a strict subset of HEAD's, output valid under this profile is also
valid at HEAD.

Also never emit: `Any`, `ParameterAny`, `Date` (defined but not members of the `ParameterType` /
`ParameterValue` alternations — unreachable), or nested `Struct` **values**.

## Syntax self-check

1. Line 1 package name: lowercase `[a-z0-9_]`, **unquoted**, ends with `:`.
2. No tabs. Every indent is a multiple of 2. LF endings. Exactly one trailing newline.
3. Every `.ros2` interface/parameter name is `'single-quoted'`; every `type:` message ref is
   `'single-quoted'` and contains `/`.
4. Every parameter type keyword is bare. Every string `value:` is `"double-quoted"`.
5. No `msgs:` / `srvs:` / `actions:` in a `.ros2` file.
6. `lease_duration` / `liveliness` / `lifespan` / `deadline` are **legal** — emit them when the
   source has them (the restriction was lifted 2026-07-21; see §11). Never delete one to satisfy
   this checklist: that would drop a concrete value the source carried. Two value rules do apply:
   a duration is a nanosecond string and must be under ~2.147 s (`Integer.parseInt`), and
   `liveliness:` is `automatic` or `manual`, bare — any other value is a parse error.
8. Block order matches §9. `.ros2` entries alphabetical; `.rossystem` `nodes:` in **source order**.
   Any `default:` in the input is still a `default:` in the output, never rewritten to `value:`.
