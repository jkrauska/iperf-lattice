# Plans & Future Improvements

Outstanding suggestions for iperf-lattice, roughly ordered by impact.

## Security

### Default to system known_hosts for SSH host-key verification

All SSH connections currently use `known_hosts=None`, which disables host-key
verification entirely. A safer default would be to use the system
`~/.ssh/known_hosts` (asyncssh's default behavior) and provide a `--no-verify-host-keys`
CLI flag for lab environments where keys rotate frequently. The flag should
print a visible warning when used.

## Robustness

### Better error handling in concurrent gather calls

Several `asyncio.gather()` calls (e.g. initial SSH connections, iperf3 install
checks) don't use `return_exceptions=True`. If one node fails, the entire
gather raises with no indication of *which* node caused the failure. Wrapping
each task or using `return_exceptions=True` with per-result error checking
would give clearer diagnostics. `stop_all_servers` already does this correctly
and can serve as the pattern.

### Clock-skew detection during pre-flight

The flood-mode synchronized start relies on NTP-aligned clocks. The discover
tool could check each node's clock offset (e.g. compare `date +%s` output to
the orchestrator's time) and warn if skew exceeds a threshold (say, 2 seconds).

## Features

### `--reverse` and `--bidir` flag support

iperf3 supports `--reverse` (server-to-client throughput) and `--bidir`
(simultaneous both directions). These are useful for diagnosing asymmetric
link issues. Since the command is built in `build_client_cmd()`, adding a
passthrough would be straightforward.

### Make `PairResult.raw` / `end_summary` opt-in

Every `PairResult` carries a copy of the iperf3 `end` JSON block, which is
included in the output file via `end_summary`. For large runs this bloats the
JSON. Consider a `--verbose-json` flag to include it, keeping the default
output smaller and easier to diff across runs.

### Multi-distro support in `install_iperf.py`

The install script only supports Debian/Ubuntu (`apt-get`). Detecting the
package manager and falling back (e.g. `dnf install -y iperf3` on
RHEL/Fedora, `zypper install -y iperf3` on SUSE) would broaden support.
Alternatively, document the limitation clearly and let users install manually.
