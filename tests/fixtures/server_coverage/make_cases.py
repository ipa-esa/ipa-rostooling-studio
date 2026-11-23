"""Writes one minimal case per linter rule that copies a grammar or validator rule (the cases in
this folder; rerun it to regenerate them).

Each case is the valid base (a .ros, a .ros2, a .rossystem) with one mutation, and a case.json
naming the rule and the file it targets. Usage: make_cases.py <out dir>"""
import json, os, sys

ROS = """probe_msgs:
  msgs:
    Reading
      message
        float64 value
    Pair
      message
        'probe_msgs/msg/Reading' first
"""
ROS2 = """probe_pkg:
  artifacts:
    talker:
      node: talker
      publishers:
        'chatter':
          type: 'probe_msgs/msg/Reading'
          qos:
            reliability: reliable
      parameters:
        'rate':
          type: Integer
          default: 10
    listener:
      node: listener
      subscribers:
        'chatter':
          type: 'probe_msgs/msg/Reading'
"""
SYS = """probe_sys:
  fromFile: "probe_sys/launch/probe.launch.py"  # caller-supplied
  processes:
    "main":
      nodes: [talker, listener]
  nodes:
    "talker":
      from: "probe_pkg.talker"
      interfaces:
        - "chatter_pub": pub-> "talker::chatter"
    "listener":
      from: "probe_pkg.listener"
      interfaces:
        - "chatter_sub": sub-> "listener::chatter"
  connections:
    - ["chatter_pub", "chatter_sub"]
"""
BASE = {"probe_msgs.ros": ROS, "probe_pkg.ros2": ROS2, "probe_sys.rossystem": SYS}

# rule, target file, old text, new text  (old=None: replace the whole file with new)
CASES = [
    ("RM010", "probe_pkg.ros2", "probe_pkg:\n", "Probe_pkg:\n"),
    ("RM011", "probe_pkg.ros2", "    talker:\n      node: talker\n", "    Talker:\n      node: talker\n"),
    ("RM012", "probe_pkg.ros2", "      node: listener\n", "      node: Listener\n"),
    ("RM013", "probe_pkg.ros2", "'rate':", "'Rate':"),
    ("RM016", "probe_pkg.ros2", "      node: listener\n", "      node: listen-er\n"),
    ("RM020", "probe_pkg.ros2", "      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n", "      subscribers:\n        'chatter':\n          type: probe_msgs/msg/Reading\n"),
    ("RM021", "probe_pkg.ros2", "      node: listener\n", "      node: \"listener\"\n"),
    ("RM030", "probe_pkg.ros2", "probe_pkg:\n", "probe_pkg:\n  msgs:\n    Extra\n      message\n        float64 x\n"),
    ("RM032", "probe_pkg.ros2", "            reliability: reliable\n", "            reliability: reliable\n            speed: fast\n"),
    ("RM033", "probe_pkg.ros2", "reliability: reliable", "reliability: sometimes"),
    ("RM035", "probe_pkg.ros2", "            reliability: reliable\n", "            reliability: reliable\n            deadline: \"soon\"\n"),
    ("RM036", "probe_pkg.ros2", "      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n", "      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n          typo: 1\n"),
    ("RM037", "probe_pkg.ros2", "probe_pkg:\n", "probe_pkg:\n  version: 1\n"),
    ("RM038", "probe_sys.rossystem", "      from: \"probe_pkg.listener\"\n", ""),
    ("RM039", "probe_sys.rossystem", "        - \"chatter_sub\": sub-> \"listener::chatter\"\n", "        - \"chatter_sub\": sub-> \"listener::chatter\"\n      namespace: \"ns1\"\n"),
    ("RM041", "probe_pkg.ros2", "type: Integer", "type: Float"),
    ("RM047", "probe_pkg.ros2", "probe_pkg:\n", "probe_pkg:\n  pluginbases:\n    Leaf:\n      class: \"probe::Leaf\"\n      header: \"probe/leaf.hpp\"\n      loader: bt\n"),
    ("RM048", "probe_pkg.ros2", None, ROS2.replace("probe_pkg:\n  artifacts:\n", "probe_pkg:\n  pluginbases:\n    Planner:\n      class: \"probe::Planner\"\n      header: \"probe/planner.hpp\"\n      loader: pluginlib\n  artifacts:\n").replace("          default: 10\n", "          default: 10\n      pluginslots:\n        planner:\n          base: probe_pkg.Planner\n          parameter: rate\n")),
    ("RM050", "probe_sys.rossystem", "    - [\"chatter_pub\", \"chatter_sub\"]\n", "    - [\"chatter_pub\", \"nope\"]\n"),
    ("RM051", "probe_sys.rossystem", "    - [\"chatter_pub\", \"chatter_sub\"]\n", "    - [\"chatter_sub\", \"chatter_pub\"]\n"),
    ("RM052", "probe_sys.rossystem", "fromFile: \"probe_sys/launch/probe.launch.py\"", "fromFile: \"probe.launch.py\""),
    ("RM053", "probe_sys.rossystem", "  fromFile: \"probe_sys/launch/probe.launch.py\"  # caller-supplied\n", ""),
    ("RM054", "probe_sys.rossystem", "nodes: [talker, listener]", "nodes: [talker, ghost]"),
    ("RM055", "probe_sys.rossystem", "pub-> \"talker::chatter\"", "pubx-> \"talker::chatter\""),
    ("RM058", "probe_sys.rossystem", "        - \"chatter_pub\": pub-> \"talker::chatter\"\n", "        - \"chatter_pub\": pub-> \"talker::chatter\"\n        - \"chatter_pub\": pub-> \"talker::chatter\"\n"),
    ("RM060", "probe_sys.rossystem", "    - [\"chatter_pub\", \"chatter_sub\"]\n", "    - [\"chatter_pub\"]\n"),
    ("RM070", "probe_msgs.ros", "probe_msgs:\n", "probe_msgs\n"),
    ("RM071", "probe_msgs.ros", "probe_msgs:\n", "probe_msgs:\n  bogus:\n"),
    ("RM073", "probe_msgs.ros", "    Reading\n", "    Reading:\n"),
    ("RM074", "probe_msgs.ros", "float64 value", "float65 value"),
    ("RM075", "probe_msgs.ros", "float64 value", "float64[3] value"),
    ("RM076", "probe_msgs.ros", "'probe_msgs/msg/Reading' first", "'probe_msgs/Reading' first"),
    ("RM077", "probe_msgs.ros", "        float64 value\n", "        float64 value\n        uint8 MODE = 1\n"),
    ("RM079", "probe_msgs.ros", None, "probe_msgs {\n  msgs {\n    Reading {\n      message {\n        float64 value\n      }\n    }\n  }\n}\n"),
    ("RM093", "probe_sys.rossystem", "  processes:\n", "  subSystems: [\"other\"]\n  processes:\n"),
    ("RM094", "probe_sys.rossystem", "    \"listener\":\n", "# a comment at column 0 inside nodes:\n    \"listener\":\n"),
    # Further checks the removed rule ids bundled, probed one by one (2026-11-23, step 2).
    ("RM036-active-in-on-plain-node", "probe_pkg.ros2", "      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n", "      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n          active_in: [Active]\n"),
    ("RM033-bad-lifecycle-state", "probe_pkg.ros2", "      node: listener\n      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n", "      node: listener\n      lifecycle: true\n      subscribers:\n        'chatter':\n          type: 'probe_msgs/msg/Reading'\n          active_in: [Configuring]\n"),
    ("RM033-quoted-lifecycle", "probe_pkg.ros2", "      node: listener\n", "      node: listener\n      lifecycle: \"true\"\n"),
    ("RM033-bad-block-ref-prefix", "probe_pkg.ros2", "          default: 10\n", "          default: 10\n      @audit:\n        broken: foo->bar\n"),
    ("RM036-block-on-nodeless-artifact", "probe_pkg.ros2", "    listener:\n", "    lib_only:\n      @audit:\n        channel: \"bad\"\n    listener:\n"),
    ("RM039-block-before-from", "probe_sys.rossystem", "    \"listener\":\n      from: \"probe_pkg.listener\"\n", "    \"listener\":\n      @audit:\n        channel: \"early\"\n      from: \"probe_pkg.listener\"\n"),
    ("RM035-duration-overflow", "probe_pkg.ros2", "            reliability: reliable\n", "            reliability: reliable\n            deadline: \"99999999999\"\n"),
    ("RM073-srv-without-response", "probe_msgs.ros", "probe_msgs:\n", "probe_msgs:\n  srvs:\n    Ping\n      request\n        string q\n"),
    ("RM041-array-without-element", "probe_pkg.ros2", "type: Integer\n          default: 10", "type: Array"),
    ("RM038-system-parameter-without-value", "probe_sys.rossystem", "        - \"chatter_pub\": pub-> \"talker::chatter\"\n", "        - \"chatter_pub\": pub-> \"talker::chatter\"\n      parameters:\n        - \"rate\": \"talker::rate\"\n"),
]

out = sys.argv[1]
for rule, target, old, new in CASES:
    files = dict(BASE)
    if old is None:
        files[target] = new
    else:
        assert old in files[target], (rule, old)
        files[target] = files[target].replace(old, new, 1)
    d = os.path.join(out, rule)
    os.makedirs(d, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)
    with open(os.path.join(d, "case.json"), "w") as fh:
        json.dump({"rule": rule.split("-")[0], "file": target}, fh)
base = os.path.join(out, "base")
os.makedirs(base, exist_ok=True)
for name, text in BASE.items():
    with open(os.path.join(base, name), "w") as fh:
        fh.write(text)
print(len(CASES), "cases")
