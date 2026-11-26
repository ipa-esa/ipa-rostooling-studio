---
name: ros-docker
description: Run ROS 2 nodes and systems in Docker with the RosTooling Docker plugin - add @docker blocks through MCP tools, point a model at an existing compose project or let the plugin generate Dockerfiles and compose files, import existing Docker setups, generate, run, and switch elements between container and native. Use when asked to dockerize or containerize a ROS 2 system, to model an existing compose project, or about anything with an @docker block.
---

# ROS 2 containers with the RosTooling Docker plugin (`ros-docker`)

The plugin generates the container side of a system from the model. You write the model (through
MCP tools). Dockerfiles and compose files the user already has stay theirs.

Model syntax rules are in the `ros-model` skill. This skill is the workflow.

## 0. Check the plugin first

Call `ros_list_plugins`. The Docker tools exist only when `de.fraunhofer.ipa.ros.plugin.docker` is
listed. If it is missing, stop and ask the user to add the plugin JAR to
`rostooling-languages.plugins.paths`, approve it and enable it, then restart the agent session. Do
not write `@docker:` blocks by hand as a substitute. Tool names appear as `<plugin id>__<tool>`.

If generation fails with `NoSuchMethodError` naming `StartupClaim`, the plugin JAR and RosTooling
do not match. Ask the user to rebuild the JAR from the current plugin sources and approve it again
(a rebuilt JAR has a new checksum). Never approve it yourself.

## 1. Design before modelling

Decide per node instance how it starts:

| Mode | Model | The plugin | Use when |
|---|---|---|---|
| Native | no block, or `enabled: false` | nothing; the launch file starts the node | the node runs on the host |
| Referenced | `compose` and `service` | starts and stops that service of the user's compose project; writes only a launch file | a working compose project or image already exists |
| Generated | claimed, without `compose` / `service` | writes the Dockerfile, compose project and container launch files | there are no Docker files yet |

A node is claimed (runs in a container by default) when it has `@docker` or its system has a
system-level `@docker`. One system can mix all three modes.

Show the user the mode per node when the request leaves it open. Do not invent compose files,
service names, images or apt packages.

Check the limits (section 8) now: if the system needs one of them, say so before modelling.

## 2. Where `@docker` goes

| Location | Fields | Tool |
|---|---|---|
| Node instance in `.rossystem` | `enabled`, `compose`, `service`, `service_name` | `docker_set_node` (`node` = instance name) |
| System in `.rossystem` | `enabled`, `compose`, `env_file` | `docker_set_system` |
| Node in `.ros2` (image build info) | `image`, `base_image`, `apt`, `env` | `docker_set_build` (`node` = node or artifact name) |

- Pass `model_path` on every call.
- These three tools merge: omitted fields stay, `clear: ["compose", "service"]` removes fields.
  `set_docker` and `ros_set_extension_block` replace the whole block, so prefer the three tools.
- `compose` and `service` go together. One service runs one node instance.
- `service_name` renames a generated service (default `<system>_<instance>`).
- System-level `@docker` claims every node; a node with `enabled: false` defaults to native. With
  `compose` at system level the whole system is the user's compose project.
- `apt` and `env` replace the whole list. `env` entries are `KEY=value` strings.
- The `.ros2` blocks of all nodes in a package are merged into one image. Give them the same
  `image`, `base_image` and `env` values.
- **Generated mode needs a `.ros2` block**: at least one node of each containerized package needs
  `@docker`, or no Dockerfile is written for that package.
- Do not put ROS environment keys (`ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, ...) in `env`. They
  belong in the shell or an env file.

**Do not set `fromFile` on a dockerized system.** Omit `from_file` in `ros_add_node` and
`ros_add_subsystem` (this overrides the general advice in `ros-model`): a generated-mode or
referenced system with `fromFile` cannot be generated, because core writes no launch file for it.

### Paths

`compose` and `env_file` are relative to `ROS_WS_SRC` or absolute. Generated mode builds each
image from `$ROS_WS_SRC/<package>`. The MCP generators write packages to `<ws>/src-gen/`, so:

| The system has | `ROS_WS_SRC` | The user's compose path in the model |
|---|---|---|
| only referenced nodes | `<ws>/src` | `my_stack/compose.yaml` |
| generated nodes (alone or mixed) | `<ws>/src-gen` | `../src/my_stack/compose.yaml` |

If the user keeps the generated packages somewhere else, point `ROS_WS_SRC` at that folder.

### Subsystems

A subsystem is claimed when **its own** system has a system-level `@docker`
(`docker_set_system` on the subsystem's `.rossystem`). The parent's compose project then includes a
copy of it, and the parent starts it. Nested subsystems work the same way. With `compose` in that
block the parent runs the user's whole compose project instead. Without the block the subsystem
starts natively as usual.

In a tree with anything claimed, include a subsystem only once, also through different parents.
`validate_ros_model` can pass while generating the top system fails.

## 3. Import an existing Docker setup

For a project that already has Dockerfiles or compose files, do not read them yourself. The import
adds blocks to **existing** node instances, so model the packages and the system first
(`ros-model`).

1. Scan: `python3 scripts/extract_docker.py <folder> -o scan.json`. It only reads.
2. Plan: `docker_import` with `model_path` (the `.rossystem`), `workspace_dir`, `scan` (the JSON
   object itself, not the file path), `compose_root` (the path of `<folder>` relative to
   `ROS_WS_SRC`, see the table above) and `dry_run: true`.
3. Show the user `written` and the unmatched lists, then repeat without `dry_run`. A second run
   changes nothing.
4. For each entry in `suggestions`, call `docker_set_build` with its `arguments` plus its `node`,
   on the `.ros2` file in `model`. The import never writes `.ros2` files. If a suggestion has no
   `model` and a `note` that the `.ros2` model is not in the resource set, the existing block was
   not merged in: read it with `ros_get_extension_block` and pass the union of old and new `apt`
   and `env`. Report what `skipped` lists.
5. Handle the rest:
   - `unmatched_services`: `no_ros` (no `ros2 run` or `ros2 launch` command, such as a database)
     stays only in the user's compose file. `no_match` or `ambiguous`: set it with
     `docker_set_node` when the instance is clear, otherwise ask.
   - `unmatched_instances`: leave native, or set them up as in section 2.

A service matches an instance by its node name (`__node:=`), otherwise by package and executable.
Report `scan_warnings`.

## 4. Validate and generate

1. `validate_ros_model` on every `.ros2` and `.rossystem` you touched. Errors stop generation.
2. `generate_plugin_outputs` with `ros_distro` set, on **every** containerized package's `.ros2`
   and on **every** `.rossystem`, including each subsystem's own.
3. Read `notices` in each result and act on them.

| Output (below `src-gen/`) | Content |
|---|---|
| `<pkg>/docker/Dockerfile` | generated mode, from the `.ros2` blocks |
| `<sys>/config/de_fraunhofer_ipa_ros_plugin_docker/` | `compose.yaml`, one launch file per generated service, copies for claimed subsystems |
| `<sys>/launch/de_fraunhofer_ipa_ros_plugin_docker/containers.launch.py` | starts and stops the containers; included by the system launch file |

Removing the last `.ros2` block of a package does not delete its earlier Dockerfile.

## 5. User-owned files

- Never edit generated files; they are rewritten on every generation.
- Devices, GPU, extra volumes, `user:` and other extras go in `compose.override.yaml` next to the
  generated `compose.yaml`. The generator never writes it. Rebuild after changing it so that it is
  installed. Ask before creating or changing it.
- The user's compose files, Dockerfiles and env files are never read or written by the plugin. Do
  not edit them to make the model fit; change the model, or ask.
- Subsystem services started by a parent belong to the parent's compose project, so their extras go
  in the parent's override. The subsystem's own override applies when its own project starts them.
- An `env_file` named in the model must exist.

## 6. Run

Build with `build_ros2_package`, source the workspace, then:

```bash
ros2 launch <system> <system>.launch.py ros_ws_src:=<ws>/src-gen
```

Use the `ROS_WS_SRC` folder from section 2, and always give it (argument or environment): from the
installed package the default paths do not reach the sources. Starting builds missing images, which
needs the Docker daemon and usually network access; ask before pulling or building images.

A fully dockerized system (the generation notice says so) also runs without ROS on the host:
`docker compose -f <ws>/src-gen/<system>/config/de_fraunhofer_ipa_ros_plugin_docker/compose.yaml up`.

Check with `docker ps` and `ros2 node list` that each node runs exactly once.

## 7. Switch between container and native

Every claimed element has a launch argument `<system>.<element>_in_docker`, where `<system>` is the
system that **owns** the node or subsystem (characters other than letters, digits and `_` become
`_`). `enabled: false` makes its default `false`.

```bash
ros2 launch top top.launch.py top.camera_in_docker:=false        # one node of top
ros2 launch top top.launch.py top.sub_in_docker:=false           # a whole subsystem
ros2 launch top top.launch.py sub.talker_in_docker:=false        # one node inside subsystem sub
ros2 launch top top.launch.py run_claimed_natively:=true         # everything native
```

- `false` means "start natively on this machine", not "do not start".
- A subsystem switched off starts completely natively, including the subsystems inside it.
- One node switched off inside a containerized subsystem starts natively; the rest of that subsystem
  then runs from the subsystem's own compose project, so those containers carry its name.
- An explicit element argument wins over `run_claimed_natively`.
- A misspelled or unqualified argument is silently ignored. Take the names from `--show-args`, which
  also lists the switches inside subsystems.

## 8. Limits

- **One host.** Everything starts on the machine that runs the launch file. For several machines, a
  separate system model per machine is a workaround that has **not been tested**; say so. Settings
  that exist only in a top system may be missing, and discovery across networks may need DDS
  configuration.
- A generated image contains only the node's own package. Other workspace packages, such as custom
  interface packages, need `apt` packages or a custom `base_image` that provides them.
- Launch arguments given on the command line for system parameters are not forwarded into generated
  containers; they use the model default.
- Nodes inside `processes:` cannot run in containers.
- Generated services use `network_mode: host` and `ipc: host`, with Fast DDS over UDP. Change that
  only in `compose.override.yaml`.

## Diagnostics and notices

| Message or error | Fix |
|---|---|
| `compose` needs `service`, or the reverse | set both or clear both |
| service already used by another node | one service per instance |
| generated service name used twice | give one instance another `service_name` |
| warning: referenced service name equals another one | tell the user; rename if confusing |
| claimed subsystem listed twice, or `includes subsystem ... times in its subsystem tree` | include it once |
| `env` entry must be `KEY=value` | fix the entry |
| warning: ROS environment pinned inconsistently | remove the key from `env` |
| warning: nodes of a package disagree on `image`, `base_image` or `env` | make them equal; the first wins |
| warning: field has no effect here | move it to the location in section 2 |
| cannot register launch include on `fromFile` system | remove `fromFile` |
| notice: no `@docker` block on any node of a package | `docker_set_build`, if the package needs a generated image |
| notice: `compose.override.yaml` is yours | information only |

## Report to the user

- The mode of each node and subsystem, and the claim switches.
- Import results: written, unmatched, skipped values and warnings.
- Validation results and notices left open.
- Whether the system was generated, built and run, and what was not checked.
