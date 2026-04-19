#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncssh>=2.17",
#   "pyyaml>=6.0",
#   "rich>=13.7",
# ]
# ///
"""
iperf-lattice-discover — pre-flight check for iperf-lattice.

SSHes into every node and:
  1. Verifies iperf3 is installed (iperf3 --version).
  2. Runs lldpctl -f json to discover LLDP neighbors and identify
     which interfaces are connected to switches.

Prints a readiness summary and an LLDP topology table.

Usage:
  uv run iperf_lattice_discover.py --hosts hosts.yaml
  uv run iperf_lattice_discover.py --hosts hosts.yaml --json-out discovery.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
import yaml
from rich.console import Console
from rich.table import Table

# ---------- data model ----------


@dataclass
class Node:
    name: str
    host: str
    test_addr: str
    user: str | None = None  # defaults to current OS user (like ssh)
    ssh_port: int = 22


@dataclass
class LldpNeighbor:
    local_port: str
    remote_system: str
    remote_port: str
    remote_descr: str = ""
    vlan: str = ""


@dataclass
class NodeDiscovery:
    name: str
    iperf3_ok: bool = False
    iperf3_version: str = ""
    iperf3_error: str = ""
    lldp_ok: bool = False
    lldp_error: str = ""
    neighbors: list[LldpNeighbor] | None = None
    ssh_error: str = ""


# ---------- inventory ----------


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
                )
            )
    else:
        sys.exit(f"Unexpected YAML structure in {path} (expected list or dict)")

    if not nodes:
        sys.exit("No nodes found in hosts file.")
    return nodes


# ---------- SSH helpers ----------


async def ssh_connect(node: Node) -> asyncssh.SSHClientConnection:
    kwargs: dict[str, Any] = {
        "host": node.host,
        "port": node.ssh_port,
        "known_hosts": None,
        "keepalive_interval": 15,
    }
    if node.user:
        kwargs["username"] = node.user
    return await asyncssh.connect(**kwargs)


async def ssh_run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: float = 30) -> asyncssh.SSHCompletedProcess:
    return await conn.run(cmd, check=False, timeout=timeout)


# ---------- LLDP parsing ----------


def parse_lldpctl_json(raw: str) -> list[LldpNeighbor]:
    """Parse the JSON output of `lldpctl -f json` into LldpNeighbor records."""
    data = json.loads(raw)
    lldp = data.get("lldp", data)
    ifaces = lldp.get("interface", {})

    if isinstance(ifaces, dict):
        ifaces = [ifaces]

    neighbors: list[LldpNeighbor] = []
    for iface_block in ifaces:
        if isinstance(iface_block, str):
            iface_block = {iface_block: ifaces[iface_block]}  # type: ignore[index]
        for local_port, details in iface_block.items():
            chassis_block = details.get("chassis", {})
            port_block = details.get("port", {})

            if isinstance(chassis_block, dict):
                remote_sys = ""
                for v in chassis_block.values():
                    if isinstance(v, dict):
                        remote_sys = v.get("name", v.get("value", ""))
                        break
                    remote_sys = str(v)
                    break
                if not remote_sys:
                    remote_sys = str(next(iter(chassis_block), ""))
            else:
                remote_sys = str(chassis_block)

            remote_port = port_block.get("id", {}).get("value", "") if isinstance(port_block, dict) else str(port_block)
            remote_descr = port_block.get("descr", "") if isinstance(port_block, dict) else ""

            vlan = ""
            vlan_block = details.get("vlan", {})
            if isinstance(vlan_block, dict):
                vlan = vlan_block.get("vlan-id", vlan_block.get("pvid", ""))
            elif isinstance(vlan_block, list) and vlan_block:
                vlan = vlan_block[0].get("vlan-id", "")

            neighbors.append(
                LldpNeighbor(
                    local_port=local_port,
                    remote_system=str(remote_sys),
                    remote_port=str(remote_port),
                    remote_descr=str(remote_descr),
                    vlan=str(vlan),
                )
            )
    return neighbors


# ---------- per-node discovery ----------


async def discover_node(node: Node) -> NodeDiscovery:
    """SSH to a single node and check iperf3 + lldpctl."""
    result = NodeDiscovery(name=node.name)
    try:
        async with await ssh_connect(node) as conn:
            # --- iperf3 check ---
            r = await ssh_run(conn, "iperf3 --version 2>&1", timeout=10)
            if r.returncode == 0:
                result.iperf3_ok = True
                result.iperf3_version = str(r.stdout).strip().splitlines()[0] if r.stdout else ""
            else:
                result.iperf3_error = f"rc={r.returncode}: {str(r.stderr or r.stdout)[:200]}"

            # --- lldpctl check ---
            r = await ssh_run(conn, "sudo lldpctl -f json", timeout=15)
            if r.returncode == 0:
                result.lldp_ok = True
                result.neighbors = parse_lldpctl_json(str(r.stdout))
            else:
                result.lldp_error = f"rc={r.returncode}: {str(r.stderr or r.stdout)[:200]}"

    except (asyncssh.Error, OSError) as e:
        result.ssh_error = f"{type(e).__name__}: {e}"
    return result


# ---------- orchestrator ----------


async def discover_all(nodes: list[Node], console: Console) -> list[NodeDiscovery]:
    console.log(f"Discovering {len(nodes)} node(s)...")
    results = list(await asyncio.gather(*(discover_node(n) for n in nodes)))
    return results


# ---------- reporting ----------


def print_readiness(discoveries: list[NodeDiscovery], console: Console) -> None:
    """Print a summary table showing iperf3 and LLDP readiness per node."""
    table = Table(title="Node Readiness")
    table.add_column("Node", style="bold")
    table.add_column("SSH")
    table.add_column("iperf3")
    table.add_column("iperf3 version")
    table.add_column("lldpctl")
    table.add_column("LLDP neighbors")

    for d in discoveries:
        if d.ssh_error:
            table.add_row(d.name, "[red]FAIL[/red]", "--", "--", "--", d.ssh_error[:60])
            continue
        iperf_status = "[green]OK[/green]" if d.iperf3_ok else "[red]FAIL[/red]"
        lldp_status = "[green]OK[/green]" if d.lldp_ok else "[red]FAIL[/red]"
        neighbor_count = str(len(d.neighbors)) if d.neighbors is not None else "--"
        version = d.iperf3_version or d.iperf3_error[:40]
        table.add_row(d.name, "[green]OK[/green]", iperf_status, version, lldp_status, neighbor_count)

    console.print(table)


def print_topology(discoveries: list[NodeDiscovery], console: Console) -> None:
    """Print an LLDP topology table for nodes that have neighbor data."""
    has_lldp = [d for d in discoveries if d.neighbors]
    if not has_lldp:
        console.print("[dim]No LLDP neighbor data available.[/dim]")
        return

    table = Table(title="LLDP Topology")
    table.add_column("Node", style="bold")
    table.add_column("Local Port")
    table.add_column("Remote System")
    table.add_column("Remote Port")
    table.add_column("Description")
    table.add_column("VLAN")

    for d in has_lldp:
        for i, n in enumerate(d.neighbors):  # type: ignore[union-attr]
            label = d.name
            table.add_row(label, n.local_port, n.remote_system, n.remote_port, n.remote_descr, n.vlan)

    console.print(table)


def build_json_payload(nodes: list[Node], discoveries: list[NodeDiscovery]) -> dict[str, Any]:
    return {
        "meta": {"tool": "iperf-lattice-discover", "num_nodes": len(nodes)},
        "nodes": [
            {
                "name": d.name,
                "ssh_ok": not bool(d.ssh_error),
                "ssh_error": d.ssh_error or None,
                "iperf3_ok": d.iperf3_ok,
                "iperf3_version": d.iperf3_version or None,
                "iperf3_error": d.iperf3_error or None,
                "lldp_ok": d.lldp_ok,
                "lldp_error": d.lldp_error or None,
                "lldp_neighbors": [
                    {
                        "local_port": nb.local_port,
                        "remote_system": nb.remote_system,
                        "remote_port": nb.remote_port,
                        "remote_descr": nb.remote_descr,
                        "vlan": nb.vlan,
                    }
                    for nb in (d.neighbors or [])
                ],
            }
            for d in discoveries
        ],
    }


# ---------- main ----------


async def amain(args: argparse.Namespace) -> int:
    console = Console()
    nodes = load_hosts(Path(args.hosts))
    console.log(f"Loaded {len(nodes)} node(s): {', '.join(n.name for n in nodes)}")

    discoveries = await discover_all(nodes, console)

    console.print()
    print_readiness(discoveries, console)
    console.print()
    print_topology(discoveries, console)

    if args.json_out:
        payload = build_json_payload(nodes, discoveries)
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        console.log(f"Wrote JSON to {args.json_out}")

    all_ok = all(d.iperf3_ok and not d.ssh_error for d in discoveries)
    if not all_ok:
        console.print("\n[yellow]Some nodes are not ready for testing.[/yellow]")
    return 0 if all_ok else 1


def main() -> None:
    p = argparse.ArgumentParser(description="iperf-lattice-discover: pre-flight node checks (iperf3 + LLDP)")
    p.add_argument("--hosts", required=True, help="YAML hosts file (list of names or advanced dict)")
    p.add_argument("--json-out", help="optional path to write full JSON discovery results")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
