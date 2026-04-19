# iperf-lattice

Orchestrate iperf3 throughput tests across a cluster of nodes over SSH.
Produces an NxN throughput matrix so you can quickly spot bad cables, misconfigured NICs,
oversubscribed uplinks, or LAG hash collisions.

## Tools

| Command | Purpose |
|------------------------------|----------------------------------------------|
| `iperf-lattice-discover`     | Pre-flight: verify iperf3, run `lldpctl`, show topology |
| `iperf-lattice-install`      | Install iperf3 on all hosts (Debian/Ubuntu via apt) |
| `iperf-lattice --mode matrix`| Sequential pair-by-pair tests (one flow at a time) |
| `iperf-lattice --mode matrix --concurrent` | Parallel non-overlapping pairs per round |
| `iperf-lattice --mode flood` | Simultaneous NxN all-to-all with synchronized start |

## Requirements

**Orchestrator (where you run this):**

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

**Target nodes:**

- `iperf3` installed and on `$PATH`
- `lldpd` / `lldpctl` for discovery (optional but recommended)
- SSH access from the orchestrator (key-based auth recommended)
- Firewall open on the port range (default 5201+, one port per incoming pair in flood mode)
- NTP or similar time sync — flood mode uses a synchronized epoch start across
  nodes, so clocks must agree within a few seconds (see below)

## Quickstart

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. Discover — check iperf3, see LLDP topology
uv run iperf_lattice_discover.py --hosts hosts.yaml

# 2. Matrix — one pair at a time, easy to attribute blame
uv run iperf_lattice.py --hosts hosts.yaml --mode matrix --duration 10

# 3. Flood — all pairs simultaneously, stress the fabric
uv run iperf_lattice.py --hosts hosts.yaml --mode flood --duration 30 --json-out results.json
```

## Hosts file

Both tools take a single `--hosts` argument pointing to a YAML file.
Two formats are supported:

### Simple — flat list

```yaml
# List of test nodes
- c1
- c2
- c3
- c4
```

Each entry is used as the hostname for SSH, iperf3 target, and display name.

### Advanced — dict with defaults

```yaml
defaults:
  user: jdoe       # omit to use current OS user
  ssh_port: 22

nodes:
  - name: c1
    host: 10.0.0.1
    test_addr: 192.168.1.1    # data-plane NIC
    cpu_affinity: "2"
  - name: c2
    host: 10.0.0.2
    test_addr: 192.168.1.2
```

Per-node fields: `name`, `host`, `test_addr` (defaults to `host`),
`user` (default: current OS user), `ssh_port` (default `22`), `cpu_affinity`.

## CLI reference

### `iperf-lattice-discover`

```
usage: iperf_lattice_discover.py [-h] --hosts HOSTS [--json-out JSON_OUT]

options:
  --hosts HOSTS       YAML hosts file (list of names or advanced dict)
  --json-out JSON_OUT Path to write full JSON discovery results
```

Checks each node for:
- SSH connectivity
- `iperf3 --version` (installed and runnable)
- `lldpctl -f json` (LLDP neighbors / switch ports)

Prints a **readiness table** and an **LLDP topology table**.
Exit code `0` if all nodes have SSH + iperf3; `1` otherwise.

### `iperf-lattice`

```
usage: iperf_lattice.py [-h] --hosts HOSTS --mode {matrix,flood}
                        [--duration DURATION] [--omit OMIT]
                        [--parallel PARALLEL] [--base-port BASE_PORT]
                        [--json-out JSON_OUT] [--timeout TIMEOUT]
                        [--fail-fast] [--concurrent]

options:
  --hosts HOSTS         YAML hosts file (list of names or advanced dict)
  --mode {matrix,flood} Test mode
  --duration DURATION   iperf3 -t seconds (default 30)
  --omit OMIT           iperf3 --omit warmup seconds (default 3)
  --parallel PARALLEL   iperf3 -P streams per pair (default 8)
  --base-port BASE_PORT First iperf3 server port (default 5201)
  --json-out JSON_OUT   Path to write full JSON results
  --timeout TIMEOUT     SSH command timeout per test in seconds (default: duration + omit + 30)
  --fail-fast           Stop on first error (matrix mode)
  --concurrent          Matrix mode: run non-overlapping pairs in parallel
```

Exit code `0` if all pairs succeeded, `2` if any pair had an error.

#### `--concurrent` (matrix mode)

By default, matrix mode runs one pair at a time. With `--concurrent`, pairs are
scheduled into rounds where no node appears more than once, and all pairs within
a round run simultaneously. This dramatically speeds up large-cluster matrix runs
while still keeping results attributable (each node is only in one flow per round).

### Clock synchronization (flood mode)

Flood mode schedules all iperf3 clients to start at the same wall-clock instant
by computing a future epoch timestamp on the orchestrator and sleeping on each
remote node until that time arrives. This requires clocks on all nodes to be
reasonably synchronized (within ~2 seconds).

If NTP or PTP is not running, some clients may start immediately (their clock is
ahead) while others sleep too long (clock is behind), skewing the measurement
window. Most Linux systems run `systemd-timesyncd` or `chrony` by default, but
it's worth verifying:

```bash
# Check NTP sync status on each node
timedatectl status | grep -i sync
# or
chronyc tracking
```

Matrix mode does not depend on clock sync — tests are sequenced by the
orchestrator over SSH.

### Choosing `--duration`

| Duration | Use case | Trade-off |
|----------|----------|-----------|
| 5s | Quick smoke test | Warmup noise dominates; misses intermittent issues |
| 10s | Fast iteration | May miss bursty congestion or thermal throttling |
| **20–30s** | **Routine fabric validation** | **Recommended.** Steady-state TCP, meaningful retransmit rates |
| 60s | Chasing intermittent issues | Good for PFC storms, thermal throttling |
| 120s+ | Burn-in / soak testing | Only needed for rare or load-dependent issues |

The `--omit` flag (default 3s) excludes TCP ramp-up from the measurements, so
the effective measurement window is `duration - omit` seconds. A 4-node cluster
(12 pairs) at `--duration 20` completes a matrix run in about 4 minutes.

## Interpreting results

### Throughput (Gb/s)

The headline number is the **aggregate TCP goodput** across all parallel streams
(`-P`, default 8). What "good" looks like depends on your NIC and fabric:

| Link speed | Typical iperf3 result | Notes |
|------------|----------------------|-------|
| 100 Gb/s   | 90–99 Gb/s           | Overhead from TCP, SSH, CPU limits |
| 25 Gb/s    | 23–24.5 Gb/s         | |
| 10 Gb/s    | 9.3–9.9 Gb/s         | |

**What to look for:**

- **Asymmetry** — if A→B is 90 Gb/s but B→A is 40 Gb/s, suspect a bad cable,
  NIC negotiation issue, or flow-control misconfiguration on one side.
- **One node slow everywhere** — NIC driver issue, CPU throttling, or wrong
  NUMA/affinity settings.
- **One pair slow** — possible hash collision on a LAG/ECMP path, or a bad
  cable on a specific switch port.
- **Flood < matrix** — expected. In flood mode all nodes compete for switch
  bandwidth, revealing oversubscription ratios.

### Retransmits

`retrans` is the number of **TCP segment retransmissions** — packets that had to
be re-sent because they were lost, reordered beyond the duplicate-ACK threshold,
or hit an RTO timeout.

| Retransmits | Interpretation |
|-------------|---------------|
| 0–100       | Excellent; clean path |
| 100–5,000   | Normal at high speed; typically transient buffer pressure |
| 5,000–50,000| Elevated; check for micro-congestion, PFC storms, or queue drops |
| 50,000+     | Significant loss; investigate switch port counters, cable CRC errors, or MTU mismatches |

High retransmits with full throughput usually means the TCP stack is recovering
well, but the network is working harder than it needs to. High retransmits with
**reduced** throughput is a stronger signal of a real problem.

### Errors

Common iperf3 errors and what they mean:

- **"unable to connect to server"** — the iperf3 server process on the target
  node died or failed to start. Can happen if another iperf3 instance was
  already bound to the port, or if the process was OOM-killed.
- **"the server is busy running a test"** — iperf3 handles one client at a time
  per port. In matrix mode this shouldn't happen; in flood mode each pair gets
  its own port to avoid this.
- **rc=255 / SSH errors** — SSH connection failed; check key auth, firewall, or
  that the node is reachable.

## Development

```bash
# Install dev tools
uv tool install ruff

# Lint
ruff check .

# Format
ruff format .
```

## How it works

### Discovery (`iperf-lattice-discover`)

1. SSHes into every node in parallel.
2. Runs `iperf3 --version` to confirm iperf3 is installed.
3. Runs `lldpctl -f json` to discover LLDP neighbors (switch name, port, VLAN).
4. Prints a readiness summary and an LLDP topology table.

### Testing (`iperf-lattice`)

1. Reads the YAML hosts file to build a list of nodes.
2. SSHes into every node and starts `iperf3 -s` daemons on the required ports.
3. Runs client tests depending on mode:
   - *matrix*: one pair at a time, sequentially across all (A->B) pairs.
   - *flood*: all pairs concurrently with a synchronized epoch start time.
4. Prints a Rich-formatted throughput matrix to the terminal.
   Optionally writes structured JSON for downstream analysis or CI gating.
5. Kills all `iperf3` processes and closes SSH connections.
