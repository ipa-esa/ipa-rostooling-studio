---
name: ros-node-developer
description: Develop ROS 2 node domain logic using RosTooling generated wrappers. Use when implementing or updating a ROS 2 node's core algorithmic behavior (in C++ or Python) from a .ros2 model without writing boilerplate ROS 2 middleware code (publishers, subscribers, executors, QoS, CMakeLists.txt). Focuses purely on *Algorithm.hpp / *_logic.py.
---

# ROS 2 Node Domain Logic Developer (`ros-node-developer`)

Implement and maintain the **core domain logic** of a ROS 2 node using RosTooling's decoupled architecture.

In RosTooling, all ROS 2 middleware plumbing (node lifecycle, executors, QoS, topic publishers/subscriptions, service servers/clients, action servers/clients, parameter declarations, and `CMakeLists.txt` / `package.xml`) is **deterministically generated into communication wrappers**.

Your role as an agent is to implement **only the domain/business logic** in the protected algorithm skeleton.

---

## The Cardinal Rule: Domain Logic Shield

1. **NEVER edit or inspect generated communication wrappers**:
   - Python: `<pkg>/<node_name>.py`
   - C++: `src/<node_name>/<NodeName>Wrapper.cpp`, `include/<pkg>/<NodeName>Wrapper.hpp`
   These are middleware plumbing details. Reading them wastes context tokens; editing them will be overwritten on re-generation.
2. **Implement ONLY inside the logic skeleton**:
   - Python: `<pkg>/<node_name>_logic.py`
   - C++: `include/<pkg>/<NodeName>Algorithm.hpp` and `src/<node_name>/<NodeName>Algorithm.cpp`
   These files are protected by the **Domain Logic Shield**. Once generated, RosTooling will **never** overwrite them.
3. **ZERO ROS 2 Middleware Imports in Logic Files**:
   - Do NOT import `rclcpp` or `rclpy`.
   - Do NOT pass `rclcpp::Node::SharedPtr` or `Node` references.
   - Interface data messages (e.g., `std_msgs`, `sensor_msgs`) are imported for their plain data structures only.
   - All runtime services (timers, clocks, logging, rate limiters, parameters) are **injected** into `self` (Python) or `this` (C++).

---

## 4-Step Development Workflow

```
1. Contract Review (.ros2) ──► 2. Generate Wrappers ──► 3. Implement Pure Logic ──► 4. Build & Verify
     (ports, types, lang)        (MCP / RosTooling)        (*Algorithm / *_logic)      (colcon build)
```

### Step 1: Review the Model Contract (`.ros2`)

Inspect the `.ros2` model file (following [`ros-modeler`](../ros-modeler/SKILL.md) and [`ros-model`](../ros-model/SKILL.md)).
If any interface, parameter, or QoS policy needs to be added or adjusted before generating wrappers, update the `.ros2` model through `rostooling-mcp` (`ros2_add_artifact`, `ros2_add_interface`, `ros2_add_parameter`, `ros2_set_qos`, `ros2_set_property`) and verify with `validate_ros_model` — do not edit `.ros2` DSL text manually.
Identify:
* The package name (`CatkinPackage` / package key)
* The node and artifact name
* The interfaces: `publishers:`, `subscribers:`, `serviceservers:`, `serviceclients:`, `actionservers:`, `parameters:`
* The target implementation language (`cpp` or `python`)

Example contract (`laser_filter.ros2`):
```yaml
laser_filter:
  fromGitRepo: "https://github.com/example/laser_filter"
  artifacts:
    filter_node:
      node: filter_node
      publishers:
        filtered_scan:
          type: 'sensor_msgs/msg/LaserScan'
      subscribers:
        raw_scan:
          type: 'sensor_msgs/msg/LaserScan'
      parameters:
        min_range:
          type: Double
          default: 0.2
```

### Step 2: Trigger Wrapper Generation

Call the RosTooling MCP tool `generate_ros2_wrappers` (or run via the CLI):

```json
{
  "model_path": "path/to/laser_filter.ros2",
  "language": "python",
  "ros_distro": "jazzy"
}
```

This generates into `src-gen/<pkg>/`:
* The communication wrapper
* The build files (`CMakeLists.txt`, `package.xml`, `setup.py`)
* The initial logic skeleton: `<pkg>/<node_name>_logic.py` (Python) or `include/<pkg>/<NodeName>Algorithm.hpp` (C++)

### Step 3: Implement Pure Business Logic

Open **only** the logic skeleton file. All method names follow strict naming conventions directly derived from the `.ros2` model:

#### Interface Method Signatures

| Contract in `.ros2` | Inbound Hook to Implement (Override) | Outbound Helper to Call |
| :--- | :--- | :--- |
| `publishers: <name>:` | — | `self.publish_<name>(msg)` / `publish_<name>(msg)` |
| `subscribers: <name>:` | `def on_<name>_received(self, msg):` / `void on_<name>_received(const Msg & msg)` | — |
| `serviceservers: <name>:` | `def handle_<name>(self, request, response):` / `void handle_<name>(const Req &, Resp &)` | — |
| `serviceclients: <name>:` | — | `self.call_<name>(request)` / `call_<name>(request)` |
| `actionservers: <name>:` | `def handle_<name>_goal(self, goal):` / `def execute_<name>(self, goal_handle):` | `self.publish_<name>_feedback(feedback)` |

#### Injected Platform Services (Zero ROS 2 Dependencies)

The wrapper automatically injects platform utilities into `self` (Python) or `this` (C++):

* **Logging**:
  * Python: `self.log_info("text")`, `self.log_warn("text")`, `self.log_error("text")`, `self.log_debug("text")`
  * C++: `log_info("text")`, `log_warn("text")`, `log_error("text")`, `log_debug("text")`
* **Periodic Timers** (simulation-clock `/clock` aware):
  * Python: `self.create_timer(period_seconds, callback_fn)`
  * C++: `create_timer(std::chrono::milliseconds(ms), callback_fn)` or `create_timer(seconds_double, callback_fn)`
* **Clock & Simulation Time**:
  * Python: `self.now()` (returns current timestamp in float seconds), `self.sleep_for(seconds)` (respects simulation clock)
  * C++: `now()`, `sleep_for(duration)`
* **Rate Limiter**:
  * Python: `rate = self.create_rate(frequency_hz)` then `rate.sleep()`
* **Parameters**:
  * Python: `self.get_parameter("min_range")`
  * Validation hook: Override `validate_parameter(self, name, proposed_val)` returning `ValidationResult.ok()` or `ValidationResult.reject("reason")`
  * Change hook: Override `on_parameter_changed(self, name, new_val)`
* **Lifecycle Hooks**:
  * `on_configure()`, `on_activate()`, `on_deactivate()`, `on_cleanup()`, `on_shutdown()`

### Step 4: Build & Verify

Verify the implementation by invoking the MCP tool `build_ros2_package`:

```json
{
  "package_name": "laser_filter",
  "workspace_dir": "path/to/workspace"
}
```
Or run directly via bash:
```bash
colcon build --packages-select <package_name>
```

---

## Unit Testing Without ROS 2 (Fast CI)

Because the logic classes have **zero ROS 2 runtime dependencies**, you can write and run lightning-fast unit tests using standard `pytest` or `ctest` **without running ROS 2 nodes, daemons, or executors**.

### Python Example (`test_laser_filter_logic.py`):
```python
from laser_filter.filter_node_logic import filter_nodeLogic
from sensor_msgs.msg import LaserScan

def test_laser_filter_crops_near_points():
    logic = filter_nodeLogic()
    logic.set_parameter("min_range", 0.5)

    published = []
    # Mock outbound publisher with a plain lambda!
    logic.set_filtered_scan_publisher(lambda msg: published.append(msg))

    # Feed input scan
    scan = LaserScan()
    scan.ranges = [0.1, 0.4, 0.8, 1.2]
    logic.on_raw_scan_received(scan)

    assert len(published) == 1
    # Check that points under min_range are filtered (set to inf)
    assert published[0].ranges[0] == float('inf')
    assert published[0].ranges[2] == 0.8
```
* **Runs in < 0.1s**.
* **Zero ROS 2 daemon required**.
* **Ideal for headless GitHub Actions and CI pipelines**.

---

## Checklist for Agents

- [ ] `.ros2` model was authored/updated via `rostooling-mcp` (`ros2_*`) and passes `validate_ros_model` with 0 errors.
- [ ] Called `generate_ros2_wrappers` to produce the boilerplate.
- [ ] Looked ONLY at `<node_name>_logic.py` or `<NodeName>Algorithm.hpp`.
- [ ] Implemented handlers (`on_<topic>_received`, `handle_<service>`).
- [ ] Used injected platform methods (`self.log_info`, `self.create_timer`, `self.now()`) instead of importing `rclpy`/`rclcpp`.
- [ ] Ran `build_ros2_package` or `colcon build` to confirm compilation.
