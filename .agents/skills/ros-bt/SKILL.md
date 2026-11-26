---
name: ros-bt
description: Author ROS 2 behaviour trees with the RosTooling BT plugin, end to end - model BehaviorTree.CPP v4 leaves (action, service and topic wrappers), wire them into bt_orchestrator, generate the code, implement only the leaf logic, write and check the tree XML, and run it. Use when asked to create or change a behaviour tree, a BT leaf, BT ports, or anything with an @bt block.
---

# ROS 2 behaviour trees with the RosTooling BT plugin (`ros-bt`)

The plugin generates every BehaviorTree.ROS2 wrapper from the model. You write three things: the
model (through MCP tools), the logic inside each leaf's `<Leaf>Logic.hpp`, and the tree XML.

Model syntax rules are in the `ros-model` skill. This skill is the workflow.

## 0. Check the plugin first

Call `ros_list_plugins`. The BT tools exist only when `de.fraunhofer.ipa.ros.plugin.bt` is listed.
If it is missing, stop and ask the user to add the plugin JAR to
`rostooling-languages.plugins.paths`, approve it and enable it, then restart the agent session. Do
not write `@bt:` blocks by hand as a substitute. Tool names appear as `<plugin id>__<tool>`.

The workspace also needs the plugin's three catalogue files, either as a catalogue source or copied
beside the models: `behaviortree_cpp.ros2` (the leaf base), `bt_orchestrator.ros2` and
`btcpp_ros2_interfaces.ros`. They are in the plugin repository's `catalogue/` folder; ask for its
location if you cannot find them.

## 1. Design before modelling

List for each leaf: its name, the one ROS interface it wraps, and its ports.

| The leaf does | Wraps | `kind` | Logic to implement |
|---|---|---|---|
| Starts a long task and waits for its result | `ac->` action client | `action` | `set_goal`, `on_result` |
| Calls a service and waits for the response | `sc->` service client | `action` | `set_request`, `on_response` |
| Checks the latest message on a topic | `sub->` subscriber | `condition` | `on_message` |

- Service leaves are `kind: action`. `condition` is only valid with `sub->`.
- A leaf wraps exactly one interface. Split work that needs two interfaces into two leaves and
  compose them in the tree.
- Sequencing, fallbacks, retries and timeouts belong in the tree, not in leaf logic.
- Port types: `String`, `Boolean`, `Integer`, `Double`, or a message type. Only `in` and `inout`
  ports may have a default. `action_name`, `service_name` and `topic_name` are reserved.

Show this list to the user when the request leaves interfaces or types open. Do not invent them.

## 2. Model the leaves

1. Make sure the message, service and action types exist. Add your own with `ros_add_spec`.
2. A new leaf package needs a library artifact first: `ros2_add_artifact` with `is_library: true`.
   The result reports parse errors (`missing RULE_BEGIN`) because the artifact is still empty. That
   is expected; they disappear with the first leaf.
3. One `bt_add_node` per leaf: `model_path`, `artifact`, `name`, `wraps` (for example `ac-> dock`),
   `interface_type` (declares the interface at the same time), `kind` and `ports`. It needs the
   `.ros2` file from step 2 to exist.
4. Change leaves with `bt_add_port` and `bt_set_wraps` (pass `kind` too when the interface kind
   changes). Other edits use the `ros2_*` tools.
5. `validate_ros_model` on the `.ros2`: it must report 0 errors.

## 3. Put the leaves in a system

1. `ros_add_node` with `from: "bt_orchestrator.bt_orchestrator"`.
2. `ros_attach_plugins` with `slot: "bt_leaves"` and every leaf as `<package>.<Leaf>`. The
   `leaf_libraries` parameter is filled in by the generator; do not set it.
3. Add the nodes that serve the wrapped interfaces, if they are part of this system, and wire them
   with `ros_auto_connect`.
4. Set `behavior_trees` with `ros_set_parameter`, passing a list of the folders that hold the XML,
   each as `<package>/<folder>`: `value: ["my_trees/behavior_trees"]`.
5. `validate_ros_model` on the `.rossystem`.

## 4. Generate

Run `generate_plugin_outputs` on the leaf `.ros2` and on the `.rossystem`, with `ros_distro` set.
Read `notices` in each result.

Per leaf the plugin writes `<Leaf>Ports.hpp` and `<Leaf>BtNode.hpp` (rewritten every time) and
`<Leaf>Logic.hpp` (written once, then yours). Per package it writes the library registration and
`plugins/de_fraunhofer_ipa_ros_plugin_bt/<pkg>_tree_nodes.xml`, the node palette for Groot2.

When ports or the wrapped interface change after the first generation, the logic file is left as it
is and a notice names it, for example `DockRobot: port attempts (in, int) added; update
DockRobotLogic.hpp`. Act on every such notice.

## 5. Implement the logic

Edit only `<Leaf>Logic.hpp`. Take the types and field names from `<Leaf>Ports.hpp`
(`Inputs`, `Outputs`, `Outcome`, `Goal` / `Result`, `Request` / `Response` or `Message`) and keep
the method signatures the skeleton declares. The skeleton returns success without doing anything,
so every leaf needs a real implementation.

- `set_goal` / `set_request` return `false` to fail the leaf without sending.
- `on_result` / `on_response` / `on_message` fill `outputs` and return `Outcome::SUCCESS` or
  `Outcome::FAILURE`.
- An action whose result code is not "succeeded" makes the leaf fail whatever `on_result` returns.
- A topic condition fails while no message has arrived.
- Do not block or sleep in these methods: they run inside the tree tick.

Never edit `*BtNode.hpp`, `*Ports.hpp` or the generated registration source.

## 6. Write the tree

Generated files are not the place for trees. Keep the XML in a package of your own, for example
`my_trees/behavior_trees/dock.xml`, whose `CMakeLists.txt` installs it:

```cmake
install(DIRECTORY behavior_trees DESTINATION share/${PROJECT_NAME})
```

```xml
<root BTCPP_format="4">
  <BehaviorTree ID="DockTree">
    <Sequence>
      <IsCharged topic_name="battery" threshold="0.3"/>
      <IsDockFree service_name="check_dock" dock_id="{dock_id}"/>
      <DockRobot action_name="dock" dock_id="{dock_id}" docked="{docked}"/>
    </Sequence>
  </BehaviorTree>
</root>
```

- Node IDs are the leaf names. Use only ports listed in the generated `<pkg>_tree_nodes.xml`.
- Set `action_name`, `service_name` or `topic_name` on every leaf instance: the generated default is
  empty.
- `{name}` reads or writes a blackboard entry. Every entry that is read must be written earlier or
  provided with the goal.

## 7. Check the tree

Call `bt_validate_tree` with `model_path` (the leaf `.ros2`) and `tree_paths`; pass further subtree
files in `other_trees` or `tree_paths`.

`data.generatedLeaves` must list your leaves. If the tool finds none, it cannot check the tree: make
sure `model_path` is the leaf `.ros2` and that `behaviortree_cpp.ros2` is in the workspace. An older
RosTooling or plugin build instead reports "passed" with an empty list; treat that as not checked and
say so. Do not move the base into the leaf package.

Fix every error. Warnings for built-in nodes such as `Sequence` are normal; a warning naming one of
your leaves, or a misspelling of one, is not.

## 8. Build and run

BehaviorTree.ROS2 (`behaviortree_ros2`, `btcpp_ros2_interfaces`) is not available from apt. The
workspace needs it as source at the commit pinned in the plugin's `tools/distro-matrix/pins.env`,
together with `orchestrator/bt_orchestrator`. It does not build on Rolling; Humble, Jazzy, Kilted
and Lyrical are supported.

Build with `build_ros2_package`, then start the system and send the tree:

```bash
ros2 launch <system> <system>.launch.py
```

```bash
ros2 action send_goal /bt_execution btcpp_ros2_interfaces/action/ExecuteTree "{target_tree: DockTree}"
```

The action name is the orchestrator's `action_name` parameter (default `bt_execution`).

## Report to the user

- The leaves, what each wraps, and which logic files you implemented.
- Validation results, including whether `bt_validate_tree` actually checked the leaves.
- Any hand edits you made to models.
- Generation notices left open, and whether the packages were built and the tree was run.
