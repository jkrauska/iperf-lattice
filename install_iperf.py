#!/usr/bin/env python3
"""
install_iperf.py — install iperf3 on all hosts in a YAML hosts file.

Usage:
  uv run install_iperf.py --hosts hosts.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncssh
from rich.console import Console

from iperf_lattice_common import Node, load_hosts, ssh_connect, ssh_run

INSTALL_CMD = (
    "echo 'iperf3 iperf3/start_daemon boolean false' | sudo debconf-set-selections"
    " && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iperf3"
)


async def install_on_host(node: Node, console: Console) -> bool:
    """SSH to one host and install iperf3. Returns True on success."""
    try:
        async with await ssh_connect(node) as conn:
            r = await ssh_run(conn, INSTALL_CMD, timeout=120)
            if r.returncode == 0:
                console.print(f"  [green]OK[/green]  {node.name}")
                return True
            console.print(f"  [red]FAIL[/red] {node.name}: rc={r.returncode}")
            if r.stderr:
                console.print(f"        {str(r.stderr).strip()[:200]}")
    except (asyncssh.Error, OSError) as e:
        console.print(f"  [red]FAIL[/red] {node.name}: {type(e).__name__}: {e}")
    return False


async def amain(args: argparse.Namespace) -> int:
    console = Console()
    nodes = load_hosts(Path(args.hosts))
    console.print(f"Installing iperf3 on {len(nodes)} host(s)...\n")
    results = await asyncio.gather(*(install_on_host(n, console) for n in nodes))
    ok = sum(results)
    console.print(f"\n{ok}/{len(nodes)} succeeded.")
    return 0 if all(results) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Install iperf3 on hosts from a YAML hosts file")
    p.add_argument("--hosts", default="hosts.yaml", help="YAML hosts file (default: hosts.yaml)")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
