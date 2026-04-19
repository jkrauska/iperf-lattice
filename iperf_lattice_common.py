"""Shared data model, host loading, and SSH helpers for iperf-lattice tools."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
import yaml


@dataclass
class Node:
    name: str
    host: str  # IP or hostname used for SSH
    test_addr: str  # IP used as the iperf target (often same, but could be a data-plane NIC)
    user: str | None = None  # defaults to current OS user (like ssh)
    ssh_port: int = 22
    cpu_affinity: str | None = None  # e.g. "2" or "2,4" — passed to iperf3 -A


def load_hosts(path: Path) -> list[Node]:
    """Load a YAML hosts file.

    Accepts two shapes:

      # Simple — flat list of hostnames / IPs
      - c1
      - c2

      # Advanced — dict with optional defaults and per-node overrides
      defaults:
        user: jdoe
      nodes:
        - name: c1
          host: 10.0.0.1
          test_addr: 192.168.1.1
    """
    data = yaml.safe_load(path.read_text())
    if data is None:
        sys.exit(f"Empty hosts file: {path}")

    nodes: list[Node] = []

    if isinstance(data, list):
        for entry in data:
            name = str(entry)
            nodes.append(Node(name=name, host=name, test_addr=name))
    elif isinstance(data, dict):
        defaults = data.get("defaults", {})
        for entry in data.get("nodes", []):
            merged = {**defaults, **entry}
            nodes.append(
                Node(
                    name=merged["name"],
                    host=merged["host"],
                    test_addr=merged.get("test_addr", merged["host"]),
                    user=merged.get("user"),
                    ssh_port=merged.get("ssh_port", 22),
                    cpu_affinity=merged.get("cpu_affinity"),
                )
            )
    else:
        sys.exit(f"Unexpected YAML structure in {path} (expected list or dict)")

    if not nodes:
        sys.exit(f"No nodes found in {path}")
    return nodes


async def ssh_connect(node: Node) -> asyncssh.SSHClientConnection:
    kwargs: dict[str, Any] = {
        "host": node.host,
        "port": node.ssh_port,
        "known_hosts": None,  # set to a path in hardened environments
        "keepalive_interval": 15,
    }
    if node.user:
        kwargs["username"] = node.user
    return await asyncssh.connect(**kwargs)


async def ssh_run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: float = 120) -> asyncssh.SSHCompletedProcess:
    return await conn.run(cmd, check=False, timeout=timeout)
