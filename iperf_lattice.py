#!/usr/bin/env python3
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
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh
from rich.console import Console
from rich.table import Table

from iperf_lattice_common import Node, load_hosts, ssh_connect, ssh_run


@dataclass
class PairResult:
    client: str
    server: str
    port: int
    bits_per_second: float = 0.0
    retransmits: int = 0
    duration_secs: float = 0.0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------- iperf3 server lifecycle ----------


async def kill_iperf(conn: asyncssh.SSHClientConnection, ports: list[int] | None = None) -> None:
    """Kill any iperf3 processes and optionally wait for ports to be freed."""
    await ssh_run(conn, "pkill -TERM -x iperf3 2>/dev/null; sleep 0.5; pkill -KILL -x iperf3 2>/dev/null; true")
    if not ports:
        return
    port_checks = " || ".join(f"ss -tlnp | grep -q ':{p} '" for p in ports)
    for _ in range(10):
        r = await ssh_run(conn, port_checks, timeout=5)
        if r.returncode != 0:
            return
        await asyncio.sleep(0.5)


async def check_port_free(conn: asyncssh.SSHClientConnection, port: int) -> str | None:
    """Return a description of what's using the port, or None if free."""
    r = await ssh_run(conn, f"ss -tlnp sport = :{port}", timeout=5)
    lines = str(r.stdout or "").strip().splitlines()
    if len(lines) > 1:
        return lines[1]
    return None


async def start_iperf_servers(conn: asyncssh.SSHClientConnection, ports: list[int], affinity: str | None) -> None:
    """Start one iperf3 -s daemon per port on the remote node."""
    aff = f"-A {affinity}" if affinity else ""
    cmds = [f"nohup iperf3 -s -p {p} {aff} >/tmp/iperf3-s-{p}.log 2>&1 &" for p in ports]
    script = " ".join(cmds) + " sleep 1"
    result = await ssh_run(conn, script)
    if result.returncode not in (0, None):
        raise RuntimeError(f"Failed to start iperf3 servers: {result.stderr}")
    checks = " && ".join(f"ss -tlnp | grep -q ':{p} '" for p in ports)
    verify = await ssh_run(conn, checks, timeout=10)
    if verify.returncode != 0:
        logs = []
        for p in ports:
            log = await ssh_run(conn, f"cat /tmp/iperf3-s-{p}.log 2>/dev/null", timeout=5)
            if log.stdout and str(log.stdout).strip():
                logs.append(f"  port {p}: {str(log.stdout).strip()}")
        detail = "\n".join(logs) if logs else "  (no log output)"
        raise RuntimeError(f"iperf3 server not listening on ports {ports}:\n{detail}")


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
    """Build a shell command that optionally waits until start_at_epoch, then runs iperf3 -J.

    The epoch-based wait assumes NTP-synchronized clocks across nodes.
    Skew beyond a few seconds will desynchronize the flood start.
    """
    aff = f"-A {affinity}" if affinity else ""
    iperf = f"iperf3 -c {target_ip} -p {port} -t {duration} -P {parallel} --omit {omit} -J {aff}"
    if start_at_epoch is None:
        return iperf
    return f'python3 -c "import time; t={start_at_epoch}; d=t-time.time();  time.sleep(d) if d>0 else None" && {iperf}'


def parse_iperf_json(stdout: str) -> tuple[float, int, float, dict[str, Any]]:
    """Return (bits_per_second, retransmits, duration_secs, full_parsed_dict) from iperf3 -J output."""
    data = json.loads(stdout)
    end = data.get("end", {})
    sum_sent = end.get("sum_sent") or end.get("sum") or {}
    bps = float(sum_sent.get("bits_per_second", 0.0))
    retrans = int(sum_sent.get("retransmits", 0) or 0)
    duration = float(sum_sent.get("seconds", 0.0))
    return bps, retrans, duration, data


async def run_one_client(
    conn: asyncssh.SSHClientConnection,
    client: Node,
    server: Node,
    port: int,
    duration: int,
    parallel: int,
    omit: int,
    start_at_epoch: float | None,
    timeout: int | None = None,
) -> PairResult:
    res = PairResult(client=client.name, server=server.name, port=port)
    try:
        cmd = build_client_cmd(
            target_ip=server.test_addr,
            port=port,
            duration=duration,
            parallel=parallel,
            omit=omit,
            affinity=client.cpu_affinity,
            start_at_epoch=start_at_epoch,
        )
        cmd_timeout = timeout if timeout else duration + omit + 30
        r = await ssh_run(conn, cmd, timeout=cmd_timeout)
        if r.returncode != 0:
            detail = str(r.stderr or r.stdout or "")[:300]
            res.error = f"rc={r.returncode}: {detail}"
            return res
        bps, retrans, dur, raw = parse_iperf_json(str(r.stdout))
        res.bits_per_second = bps
        res.retransmits = retrans
        res.duration_secs = dur
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
    console.log(f"Connecting to {len(nodes)} nodes...")
    conns: dict[str, asyncssh.SSHClientConnection] = {}

    async def connect(node: Node) -> None:
        conns[node.name] = await ssh_connect(node)

    await asyncio.gather(*(connect(n) for n in nodes))

    console.log("Checking iperf3 is installed on all nodes...")
    missing: list[str] = []

    async def check_iperf3(node: Node) -> None:
        r = await ssh_run(conns[node.name], "command -v iperf3", timeout=10)
        if r.returncode != 0:
            missing.append(node.name)

    await asyncio.gather(*(check_iperf3(n) for n in nodes))
    if missing:
        raise RuntimeError(f"iperf3 not found on: {', '.join(missing)}")

    console.log("Cleaning up old iperf3 processes...")
    await asyncio.gather(*(kill_iperf(conns[n.name], ports) for n in nodes))

    errors: list[str] = []

    async def _check_one(node: Node, p: int) -> None:
        holder = await check_port_free(conns[node.name], p)
        if holder:
            errors.append(f"  {node.name} port {p}: {holder}")

    await asyncio.gather(*(_check_one(n, p) for n in nodes for p in ports))
    if errors:
        raise RuntimeError("Ports still in use after cleanup:\n" + "\n".join(errors))

    console.log(f"Starting iperf3 servers on {len(nodes)} nodes, ports {ports[0]}-{ports[-1]}...")

    startup_errors: list[str] = []

    async def start_one(node: Node) -> None:
        try:
            await start_iperf_servers(conns[node.name], ports, node.cpu_affinity)
        except RuntimeError as e:
            startup_errors.append(f"  {node.name}: {e}")

    await asyncio.gather(*(start_one(n) for n in nodes))
    if startup_errors:
        raise RuntimeError("Failed to start iperf3 servers:\n" + "\n".join(startup_errors))

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
    timeout: int | None = None,
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
                conn=conns[client.name],
                client=client,
                server=server,
                port=base_port,
                duration=duration,
                parallel=parallel,
                omit=omit,
                start_at_epoch=None,
                timeout=timeout,
            )
            results.append(r)
            if r.error:
                console.log(f"  [red]error:[/red] {r.error}")
                if fail_fast:
                    console.log("[red]Stopping early (--fail-fast)[/red]")
                    break
            else:
                console.log(f"  {fmt_bps(r.bits_per_second)}  retrans={fmt_retrans(r.retransmits, r.duration_secs)}")
    finally:
        await stop_all_servers(conns, console)
    return results


def schedule_concurrent_rounds(pairs: list[tuple[Node, Node]]) -> list[list[tuple[Node, Node]]]:
    """Partition pairs into rounds where no node appears more than once per round.

    Uses a greedy first-fit algorithm, which may produce more rounds than the
    optimal edge-coloring solution for the complete directed graph. For typical
    cluster sizes (<20 nodes) the difference is negligible.
    """
    remaining = list(pairs)
    rounds: list[list[tuple[Node, Node]]] = []
    while remaining:
        round_pairs: list[tuple[Node, Node]] = []
        busy: set[str] = set()
        still_remaining: list[tuple[Node, Node]] = []
        for client, server in remaining:
            if client.name not in busy and server.name not in busy:
                round_pairs.append((client, server))
                busy.add(client.name)
                busy.add(server.name)
            else:
                still_remaining.append((client, server))
        rounds.append(round_pairs)
        remaining = still_remaining
    return rounds


async def run_matrix_concurrent(
    nodes: list[Node],
    base_port: int,
    duration: int,
    parallel: int,
    omit: int,
    console: Console,
    fail_fast: bool = False,
    timeout: int | None = None,
) -> list[PairResult]:
    """Concurrent matrix: multiple pairs run in parallel, but no node is used twice in the same round."""
    pairs = pairs_full_mesh(nodes)
    rounds = schedule_concurrent_rounds(pairs)
    ports = [base_port]
    conns = await start_all_servers(nodes, ports, console)
    results: list[PairResult] = []
    done = 0
    total = len(pairs)
    try:
        for round_num, round_pairs in enumerate(rounds, 1):
            labels = ", ".join(f"{c.name}->{s.name}" for c, s in round_pairs)
            console.log(f"Round {round_num}/{len(rounds)} ({len(round_pairs)} pairs): {labels}")
            tasks = [
                asyncio.create_task(
                    run_one_client(
                        conn=conns[client.name],
                        client=client,
                        server=server,
                        port=base_port,
                        duration=duration,
                        parallel=parallel,
                        omit=omit,
                        start_at_epoch=None,
                        timeout=timeout,
                    )
                )
                for client, server in round_pairs
            ]
            round_results = list(await asyncio.gather(*tasks))
            has_error = False
            for r in round_results:
                done += 1
                results.append(r)
                if r.error:
                    console.log(f"  [{done}/{total}] {r.client} -> {r.server}: [red]error:[/red] {r.error}")
                    has_error = True
                else:
                    console.log(
                        f"  [{done}/{total}] {r.client} -> {r.server}: "
                        f"{fmt_bps(r.bits_per_second)}  retrans={fmt_retrans(r.retransmits, r.duration_secs)}"
                    )
            if has_error and fail_fast:
                console.log("[red]Stopping early (--fail-fast)[/red]")
                break
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
    timeout: int | None = None,
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
                    conn=conns[client.name],
                    client=client,
                    server=server,
                    port=pair_port[(client.name, server.name)],
                    duration=duration,
                    parallel=parallel,
                    omit=omit,
                    start_at_epoch=start_at,
                    timeout=timeout,
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


def fmt_retrans(retransmits: int, duration: float) -> str:
    if duration > 0:
        rate = retransmits / duration
        return f"{rate:,.0f}/s"
    return str(retransmits)


def _cell_retrans(r: PairResult | None) -> str:
    if r is None:
        return "?"
    if r.error:
        return "[red]ERR[/red]"
    return fmt_retrans(r.retransmits, r.duration_secs)


def print_matrix(nodes: list[Node], results: list[PairResult], console: Console, show_totals: bool = False) -> None:
    lookup: dict[tuple[str, str], PairResult] = {(r.client, r.server): r for r in results}

    # -- throughput table --
    table = Table(title="Throughput matrix (rows=client/source, cols=server/dest)")
    table.add_column("src \\ dst", style="bold")
    for n in nodes:
        table.add_column(n.name, justify="right")
    if show_totals:
        table.add_column("TX total", justify="right", style="bold cyan")

    for src in nodes:
        row = [src.name]
        row_bps = 0.0
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
                row_bps += r.bits_per_second
        if show_totals:
            row.append(fmt_bps(row_bps).strip())
        table.add_row(*row)

    if show_totals:
        footer = ["[bold]RX total[/bold]"]
        for dst in nodes:
            col_bps = sum(
                r.bits_per_second
                for src in nodes
                if src.name != dst.name
                for r in [lookup.get((src.name, dst.name))]
                if r and not r.error
            )
            footer.append(f"[bold cyan]{fmt_bps(col_bps).strip()}[/bold cyan]")
        footer.append("")
        table.add_row(*footer)

    console.print(table)

    # -- retransmits table --
    rt = Table(title="Retransmits/s matrix (rows=client/source, cols=server/dest)")
    rt.add_column("src \\ dst", style="bold")
    for n in nodes:
        rt.add_column(n.name, justify="right")

    for src in nodes:
        row = [src.name]
        for dst in nodes:
            if src.name == dst.name:
                row.append("--")
            else:
                row.append(_cell_retrans(lookup.get((src.name, dst.name))))
        rt.add_row(*row)

    console.print(rt)

    ok = [r for r in results if not r.error]
    if not ok:
        console.print("[red]No successful runs.[/red]")
        return
    total_bps = sum(r.bits_per_second for r in ok)
    total_retrans = sum(r.retransmits for r in ok)
    total_duration = sum(r.duration_secs for r in ok)
    console.print(
        f"\n[bold]Summary:[/bold] "
        f"{len(ok)}/{len(results)} pairs OK; "
        f"aggregate={fmt_bps(total_bps)}; "
        f"total retransmits={fmt_retrans(total_retrans, total_duration)}"
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
                "retransmits_per_sec": r.retransmits / r.duration_secs if r.duration_secs > 0 else 0,
                "duration_secs": r.duration_secs,
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
    if len(nodes) < 2:
        sys.exit("Need at least 2 nodes in hosts file.")
    console.log(f"Loaded {len(nodes)} nodes: {', '.join(n.name for n in nodes)}")

    start_ts = time.time()
    if args.mode == "matrix" and args.concurrent:
        results = await run_matrix_concurrent(
            nodes,
            args.base_port,
            args.duration,
            args.parallel,
            args.omit,
            console,
            fail_fast=args.fail_fast,
            timeout=args.timeout,
        )
    elif args.mode == "matrix":
        results = await run_matrix(
            nodes,
            args.base_port,
            args.duration,
            args.parallel,
            args.omit,
            console,
            fail_fast=args.fail_fast,
            timeout=args.timeout,
        )
    elif args.mode == "flood":
        results = await run_flood(
            nodes,
            args.base_port,
            args.duration,
            args.parallel,
            args.omit,
            console,
            timeout=args.timeout,
        )
    else:
        sys.exit(f"Unknown mode: {args.mode}")
    elapsed = time.time() - start_ts

    print_matrix(nodes, results, console, show_totals=(args.mode == "flood"))

    now = datetime.now(UTC)
    mode_label = args.mode
    if args.mode == "matrix" and args.concurrent:
        mode_label = "matrix-concurrent"
    meta = {
        "timestamp": now.isoformat(),
        "hostname": socket.gethostname(),
        "mode": mode_label,
        "duration": args.duration,
        "omit": args.omit,
        "parallel": args.parallel,
        "base_port": args.base_port,
        "elapsed_seconds": elapsed,
        "num_nodes": len(nodes),
        "num_pairs": len(results),
        "num_errors": sum(1 for r in results if r.error),
    }

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{ts}_{mode_label}.json"
    write_json(log_path, nodes, results, meta)
    console.log(f"Results saved to {log_path}")

    if args.json_out:
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
    p.add_argument("--json-out", help="optional extra path to write full JSON results")
    p.add_argument("--log-dir", default="results", help="directory for automatic run logs (default: results/)")
    p.add_argument("--timeout", type=int, default=None, help="SSH command timeout per test in seconds (default: duration + omit + 30)")
    p.add_argument("--fail-fast", action="store_true", help="stop on first error (matrix mode)")
    p.add_argument("--concurrent", action="store_true", help="matrix mode: run non-overlapping pairs in parallel")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
