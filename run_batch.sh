#!/usr/bin/env bash
set -euo pipefail

HOSTS="hosts.yaml"
DURATION=120
LOGDIR="results"
RUNS=3

ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

echo "=== iperf-lattice batch run ==="
echo "    hosts:    $HOSTS"
echo "    duration: ${DURATION}s per test"
echo "    runs:     $RUNS per mode"
echo "    log dir:  $LOGDIR"
echo "    started:  $(ts)"
echo ""

for i in $(seq 1 "$RUNS"); do
    echo ">>> [$(ts)] Matrix run $i/$RUNS (sequential)"
    uv run iperf_lattice.py \
        --hosts "$HOSTS" --mode matrix --duration "$DURATION" \
        --log-dir "$LOGDIR" --fail-fast \
        || echo "  !! [$(ts)] matrix run $i exited with errors"
    echo ""
done

for i in $(seq 1 "$RUNS"); do
    echo ">>> [$(ts)] Matrix-concurrent run $i/$RUNS"
    uv run iperf_lattice.py \
        --hosts "$HOSTS" --mode matrix --concurrent --duration "$DURATION" \
        --log-dir "$LOGDIR" --fail-fast \
        || echo "  !! [$(ts)] matrix-concurrent run $i exited with errors"
    echo ""
done

for i in $(seq 1 "$RUNS"); do
    echo ">>> [$(ts)] Flood run $i/$RUNS"
    uv run iperf_lattice.py \
        --hosts "$HOSTS" --mode flood --duration "$DURATION" \
        --log-dir "$LOGDIR" \
        || echo "  !! [$(ts)] flood run $i exited with errors"
    echo ""
done

echo "=== [$(ts)] All runs complete ==="
echo "Results in $LOGDIR/"
ls -lht "$LOGDIR"/*.json | head -20
