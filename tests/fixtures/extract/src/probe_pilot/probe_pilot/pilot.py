"""Fixture rclpy node for tests/extract_golden.py."""

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool, String


class ProbePilot(Node):
    def __init__(self):
        super().__init__("probe_pilot")

        # Literal name and literal type: both emitted.
        self.status_sub = self.create_subscription(String, "/probe/status", self.on_status, 10)
        self.cmd_pub = self.create_publisher(Bool, "/probe/enable", 10)

        # Name built at runtime, type literal: kept under its literal pieces, and flagged.
        ns = f"/probe/{self.get_name()}"
        self.beat_pub = self.create_publisher(Bool, ns + "/heartbeat", 10)

        # Literal declarations, including a real boolean False.
        self.declare_parameter("enabled", False)
        self.declare_parameter("period_sec", 0.5)
        self.declare_parameter("label", "pilot")

    def on_status(self, msg):
        pass


def main():
    rclpy.init()
    rclpy.spin(ProbePilot())
    rclpy.shutdown()
