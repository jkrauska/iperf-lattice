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
install_iperf.py — install iperf3 on all hosts in a YAML hosts file.

Usage:
  uv run install_iperf.py --hosts hosts.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import asyncssh
import yaml
from rich.console import Console

INSTALL_CMD = (
    "echo 'iperf3 iperf3/start_daemon boolean false' | sudo debconf-set-selections"
    " && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iperf3"
)


def load_hosts(path: Path) -> list[dict[str, Any]]:
    """Return a list of {name, host, user, ssh_port} dicts from the YAML hosts file."""
    data = yaml.safe_load(path.read_text())
    if data is None:
        sys.exit(f"Empty hosts file: {path}")

    hosts: list[dict[str, Any]] = []
    if isinstance(data, list):
        for entry in data:
            name = str(entry)
            hosts.append({"name": name, "host": name, "user": None, "ssh_port": 22})
    elif isinstance(data, dict):
        defaults = data.get("defaults", {})
        for entry in data.get("nodes", []):
            merged = {**defaults, **entry}
            hosts.append(
                {
                    "name": merged["name"],
                    "host": merged.get("host", merged["name"]),
                    "user": merged.get("user"),
                    "ssh_port": merged.get("ssh_port", 22),
                }
            )
    else:
        sys.exit(f"Unexpected YAML structure in {path}")

    if not hosts:
        sys.exit(f"No hosts found in {path}")
    return hosts


async def install_on_host(host: dict[str, Any], console: Console) -> bool:
    """SSH to one host and install iperf3. Returns True on success."""
    name = host["name"]
    kwargs: dict[str, Any] = {
        "host": host["host"],
        "port": host["ssh_port"],
        "known_hosts": None,
        "keepalive_interval": 15,
    }
    if host["user"]:
        kwargs["username"] = host["user"]

    try:
        async with await asyncssh.connect(**kwargs) as conn:
            r = await conn.run(INSTALL_CMD, check=False, timeout=120)
            if r.returncode == 0:
                console.print(f"  [green]OK[/green]  {name}")
                return True
            console.print(f"  [red]FAIL[/red] {name}: rc={r.returncode}")
            if r.stderr:
                console.print(f"        {str(r.stderr).strip()[:200]}")
    except (asyncssh.Error, OSError) as e:
        console.print(f"  [red]FAIL[/red] {name}: {type(e).__name__}: {e}")
    return False


async def amain(args: argparse.Namespace) -> int:
    console = Console()
    hosts = load_hosts(Path(args.hosts))
    console.print(f"Installing iperf3 on {len(hosts)} host(s)...\n")
    results = await asyncio.gather(*(install_on_host(h, console) for h in hosts))
    ok = sum(results)
    console.print(f"\n{ok}/{len(hosts)} succeeded.")
    return 0 if all(results) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Install iperf3 on hosts from a YAML hosts file")
    p.add_argument("--hosts", default="hosts.yaml", help="YAML hosts file (default: hosts.yaml)")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
