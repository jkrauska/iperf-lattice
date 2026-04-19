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
iperf_lattice.py — orchestrate iperf3 tests across a cluster.

Two modes:
  matrix  — sequential per-pair tests (one flow at a time); good for
            attributing blame to a specific cable/NIC/port.
  flood   — simultaneous NxN all-to-all; good for fabric stress,
            oversubscription, and MC-LAG hash-collision hunting.

Usage:
  uv run iperf_lattice.py --hosts hosts.yaml --mode flood --duration 30
  uv run iperf_lattice.py --hosts hosts.yaml --mode matrix --duration 20 --json-out results.json

Requirements on nodes:
  - iperf3 installed and reachable on $PATH
  - SSH access from the orchestrator host (key-based auth recommended)
  - Firewall open on the port range used (default 5201-5208)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
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
    host: str  # IP or hostname used for SSH
    test_addr: str  # IP used as the iperf target (often same, but could be a data-plane NIC)
    user: str | None = None  # defaults to current OS user (like ssh)
    ssh_port: int = 22
    cpu_affinity: str | None = None  # e.g. "2" or "2,4" — passed to iperf3 -A


@dataclass
class PairResult:
    client: str
    server: str
    port: int
    bits_per_second: float = 0.0
    retransmits: int = 0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


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

    if len(nodes) < 2:
        sys.exit("Need at least 2 nodes in hosts file.")
    return nodes


# ---------- SSH helpers ----------


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


# ---------- iperf3 server lifecycle ----------


async def kill_iperf(conn: asyncssh.SSHClientConnection) -> None:
    await ssh_run(conn, "pkill -TERM -x iperf3 2>/dev/null; sleep 0.5; pkill -KILL -x iperf3 2>/dev/null; true")


async def start_iperf_servers(conn: asyncssh.SSHClientConnection, ports: list[int], affinity: str | None) -> None:
    """Start one iperf3 -s daemon per port on the remote node."""
    await kill_iperf(conn)
    aff = f"-A {affinity}" if affinity else ""
    cmds = [f"nohup iperf3 -s -p {p} {aff} >/tmp/iperf3-s-{p}.log 2>&1 &" for p in ports]
    script = " ".join(cmds) + " sleep 1"
    result = await ssh_run(conn, script)
    if result.returncode not in (0, None):
        raise RuntimeError(f"Failed to start iperf3 servers: {result.stderr}")
    checks = " && ".join(f"ss -tlnp | grep -q ':{p} '" for p in ports)
    verify = await ssh_run(conn, checks, timeout=10)
    if verify.returncode != 0:
        raise RuntimeError(f"iperf3 server not listening on ports {ports}")


# ---------- iperf3 client runs ----------


def build_client_cmd(
    target_ip: str,
    port: int,
    duration: int,
    parallel: int,
    omit: int,
    affinity: str | None,
    start_at_epoch: float | None = None,
) -> str:
    """Build a shell command that optionally waits until start_at_epoch, then runs iperf3 -J."""
    aff = f"-A {affinity}" if affinity else ""
    iperf = f"iperf3 -c {target_ip} -p {port} -t {duration} -P {parallel} --omit {omit} -J {aff}"
    if start_at_epoch is None:
        return iperf
    return f'python3 -c "import time; t={start_at_epoch}; d=t-time.time();  time.sleep(d) if d>0 else None" && {iperf}'


def parse_iperf_json(stdout: str) -> tuple[float, int, dict[str, Any]]:
    """Return (bits_per_second, retransmits, full_parsed_dict) from iperf3 -J output."""
    data = json.loads(stdout)
    end = data.get("end", {})
    sum_sent = end.get("sum_sent") or end.get("sum") or {}
    bps = float(sum_sent.get("bits_per_second", 0.0))
    retrans = int(sum_sent.get("retransmits", 0) or 0)
    return bps, retrans, data


async def run_one_client(
    client: Node,
    server: Node,
    port: int,
    duration: int,
    parallel: int,
    omit: int,
    start_at_epoch: float | None,
) -> PairResult:
    res = PairResult(client=client.name, server=server.name, port=port)
    try:
        async with await ssh_connect(client) as conn:
            cmd = build_client_cmd(
                target_ip=server.test_addr,
                port=port,
                duration=duration,
                parallel=parallel,
                omit=omit,
                affinity=client.cpu_affinity,
                start_at_epoch=start_at_epoch,
            )
            timeout = duration + omit + 30
            r = await ssh_run(conn, cmd, timeout=timeout)
            if r.returncode != 0:
                detail = str(r.stderr or r.stdout or "")[:300]
                res.error = f"rc={r.returncode}: {detail}"
                return res
            bps, retrans, raw = parse_iperf_json(str(r.stdout))
            res.bits_per_second = bps
            res.retransmits = retrans
            res.raw = {"end_summary": raw.get("end", {})}
    except (asyncssh.Error, OSError, json.JSONDecodeError, ValueError) as e:
        res.error = f"{type(e).__name__}: {e}"
    return res


# ---------- test patterns ----------


def pairs_full_mesh(nodes: list[Node]) -> list[tuple[Node, Node]]:
    """Every ordered pair (A,B) where A != B — directional full mesh."""
    return [(a, b) for a in nodes for b in nodes if a.name != b.name]


async def start_all_servers(
    nodes: list[Node], ports: list[int], console: Console
) -> dict[str, asyncssh.SSHClientConnection]:
    console.log(f"Starting iperf3 servers on {len(nodes)} nodes, ports {ports[0]}-{ports[-1]}...")
    conns: dict[str, asyncssh.SSHClientConnection] = {}

    async def setup(node: Node) -> None:
        conn = await ssh_connect(node)
        conns[node.name] = conn
        await start_iperf_servers(conn, ports, node.cpu_affinity)

    await asyncio.gather(*(setup(n) for n in nodes))
    return conns


async def stop_all_servers(conns: dict[str, asyncssh.SSHClientConnection], console: Console) -> None:
    console.log("Stopping iperf3 servers...")

    async def teardown(conn: asyncssh.SSHClientConnection) -> None:
        try:
            await kill_iperf(conn)
        finally:
            conn.close()
            await conn.wait_closed()

    await asyncio.gather(*(teardown(c) for c in conns.values()), return_exceptions=True)


async def run_matrix(
    nodes: list[Node],
    base_port: int,
    duration: int,
    parallel: int,
    omit: int,
    console: Console,
    fail_fast: bool = False,
) -> list[PairResult]:
    """Sequential per-pair matrix. One flow at a time; attributable."""
    ports = [base_port]
    conns = await start_all_servers(nodes, ports, console)
    results: list[PairResult] = []
    try:
        pairs = pairs_full_mesh(nodes)
        for i, (client, server) in enumerate(pairs, 1):
            console.log(f"[{i}/{len(pairs)}] {client.name} -> {server.name}")
            r = await run_one_client(
                client=client,
                server=server,
                port=base_port,
                duration=duration,
                parallel=parallel,
                omit=omit,
                start_at_epoch=None,
            )
            results.append(r)
            if r.error:
                console.log(f"  [red]error:[/red] {r.error}")
                if fail_fast:
                    console.log("[red]Stopping early (--fail-fast)[/red]")
                    break
            else:
                console.log(f"  {fmt_bps(r.bits_per_second)}  retrans={r.retransmits}")
    finally:
        await stop_all_servers(conns, console)
    return results


async def run_flood(
    nodes: list[Node],
    base_port: int,
    duration: int,
    parallel: int,
    omit: int,
    console: Console,
) -> list[PairResult]:
    """Simultaneous full-mesh. Every ordered pair runs at the same moment.

    To avoid port collisions when many clients hit the same server at once,
    each client->server pair gets its own destination port. We allocate ports
    on each server by enumerating its incoming pairs.
    """
    pairs = pairs_full_mesh(nodes)
    incoming_idx: dict[str, int] = {n.name: 0 for n in nodes}
    pair_port: dict[tuple[str, str], int] = {}
    for client, server in pairs:
        pair_port[(client.name, server.name)] = base_port + incoming_idx[server.name]
        incoming_idx[server.name] += 1

    max_incoming = max(incoming_idx.values())
    ports = [base_port + i for i in range(max_incoming)]

    conns = await start_all_servers(nodes, ports, console)
    results: list[PairResult] = []
    try:
        lead_seconds = max(5.0, 0.05 * len(pairs))
        start_at = time.time() + lead_seconds
        console.log(
            f"Flooding {len(pairs)} pairs simultaneously; "
            f"synchronized start in {lead_seconds:.1f}s "
            f"(duration={duration}s, omit={omit}s, parallel={parallel})"
        )

        tasks = [
            asyncio.create_task(
                run_one_client(
                    client=client,
                    server=server,
                    port=pair_port[(client.name, server.name)],
                    duration=duration,
                    parallel=parallel,
                    omit=omit,
                    start_at_epoch=start_at,
                )
            )
            for client, server in pairs
        ]
        results = list(await asyncio.gather(*tasks))
    finally:
        await stop_all_servers(conns, console)
    return results


# ---------- reporting ----------


def fmt_bps(bps: float) -> str:
    if bps >= 1e9:
        return f"{bps / 1e9:7.2f} Gb/s"
    if bps >= 1e6:
        return f"{bps / 1e6:7.2f} Mb/s"
    if bps >= 1e3:
        return f"{bps / 1e3:7.2f} Kb/s"
    return f"{bps:7.0f}  b/s"


def print_matrix(nodes: list[Node], results: list[PairResult], console: Console) -> None:
    lookup: dict[tuple[str, str], PairResult] = {(r.client, r.server): r for r in results}
    table = Table(title="Throughput matrix (rows=client/source, cols=server/dest)")
    table.add_column("src \\ dst", style="bold")
    for n in nodes:
        table.add_column(n.name, justify="right")
    for src in nodes:
        row = [src.name]
        for dst in nodes:
            if src.name == dst.name:
                row.append("--")
                continue
            r = lookup.get((src.name, dst.name))
            if r is None:
                row.append("?")
            elif r.error:
                row.append("[red]ERR[/red]")
            else:
                row.append(fmt_bps(r.bits_per_second).strip())
        table.add_row(*row)
    console.print(table)

    ok = [r for r in results if not r.error]
    if not ok:
        console.print("[red]No successful runs.[/red]")
        return
    total_bps = sum(r.bits_per_second for r in ok)
    total_retrans = sum(r.retransmits for r in ok)
    console.print(
        f"\n[bold]Summary:[/bold] "
        f"{len(ok)}/{len(results)} pairs OK; "
        f"aggregate={fmt_bps(total_bps)}; "
        f"total retransmits={total_retrans}"
    )
    errs = [r for r in results if r.error]
    if errs:
        console.print(f"\n[red]{len(errs)} errors:[/red]")
        for r in errs[:10]:
            console.print(f"  {r.client} -> {r.server}: {r.error}")
        if len(errs) > 10:
            console.print(f"  ... and {len(errs) - 10} more")


def write_json(path: Path, nodes: list[Node], results: list[PairResult], meta: dict) -> None:
    payload = {
        "meta": meta,
        "nodes": [{"name": n.name, "host": n.host, "test_addr": n.test_addr} for n in nodes],
        "results": [
            {
                "client": r.client,
                "server": r.server,
                "port": r.port,
                "bits_per_second": r.bits_per_second,
                "retransmits": r.retransmits,
                "error": r.error,
                "end_summary": r.raw.get("end_summary") if r.raw else None,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))


# ---------- main ----------


async def amain(args: argparse.Namespace) -> int:
    console = Console()
    nodes = load_hosts(Path(args.hosts))
    console.log(f"Loaded {len(nodes)} nodes: {', '.join(n.name for n in nodes)}")

    start_ts = time.time()
    if args.mode == "matrix":
        results = await run_matrix(
            nodes,
            args.base_port,
            args.duration,
            args.parallel,
            args.omit,
            console,
            fail_fast=args.fail_fast,
        )
    elif args.mode == "flood":
        results = await run_flood(
            nodes,
            args.base_port,
            args.duration,
            args.parallel,
            args.omit,
            console,
        )
    else:
        sys.exit(f"Unknown mode: {args.mode}")
    elapsed = time.time() - start_ts

    print_matrix(nodes, results, console)

    if args.json_out:
        meta = {
            "mode": args.mode,
            "duration": args.duration,
            "omit": args.omit,
            "parallel": args.parallel,
            "base_port": args.base_port,
            "elapsed_seconds": elapsed,
            "num_nodes": len(nodes),
            "num_pairs": len(results),
        }
        write_json(Path(args.json_out), nodes, results, meta)
        console.log(f"Wrote JSON to {args.json_out}")

    return 0 if all(r.error is None for r in results) else 2


def main() -> None:
    p = argparse.ArgumentParser(description="iperf-lattice: iperf3 mesh orchestrator (matrix or flood)")
    p.add_argument("--hosts", required=True, help="YAML hosts file (list of names or advanced dict)")
    p.add_argument("--mode", choices=["matrix", "flood"], required=True)
    p.add_argument("--duration", type=int, default=30, help="iperf3 -t seconds (default 30)")
    p.add_argument("--omit", type=int, default=3, help="iperf3 --omit warmup seconds (default 3)")
    p.add_argument("--parallel", type=int, default=8, help="iperf3 -P streams per pair (default 8)")
    p.add_argument("--base-port", type=int, default=5201, help="first iperf3 server port")
    p.add_argument("--json-out", help="optional path to write full JSON results")
    p.add_argument("--fail-fast", action="store_true", help="stop on first error (matrix mode)")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
