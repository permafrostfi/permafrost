#!/usr/bin/env bash
# Permafrost differential campaign.
#
# Each shard is an independent (seed, profile) pair piped straight from the Rust
# generator into the Python reference — nothing touches disk, so the campaign
# size is bounded by time, not by the container's disk allowance.
#
#   ./run_campaign.sh <scenarios-per-shard> <shards>
set -u
PER=${1:-250000}
SHARDS=${2:-12}
GEN=./permafrost-core/target/release/diffgen
LOG=campaign
mkdir -p "$LOG"

echo "campaign: $SHARDS shards x $PER scenarios = $((SHARDS * PER)) total"
date -u

pids=()
for i in $(seq 1 "$SHARDS"); do
  if (( i % 2 == 0 )); then PROF=real; else PROF=wide; fi
  ( $GEN "$PER" "$((1000 + i))" "$PROF" | python3 diffcheck.py > "$LOG/shard-$i-$PROF.txt" 2>&1
    echo "$?" > "$LOG/shard-$i.rc" ) &
  pids+=($!)
  # Two cores; run two shards at a time so neither side of the pipe starves.
  if (( ${#pids[@]} >= 2 )); then wait "${pids[0]}"; pids=("${pids[@]:1}"); fi
done
wait

echo
echo "================ CAMPAIGN SUMMARY ================"
fail=0
for f in "$LOG"/shard-*.rc; do
  [ "$(cat "$f")" != "0" ] && fail=1
done
python3 - <<'EOF'
import glob, re
tot_s = tot_o = tot_m = tot_i = 0
ops = {}
for f in sorted(glob.glob("campaign/shard-*.txt")):
    t = open(f).read()
    m = re.search(r"scenarios\s+:\s+([\d,]+)", t)
    if not m:
        print(f"  !! {f} produced no summary"); continue
    tot_s += int(m.group(1).replace(",", ""))
    tot_o += int(re.search(r"operations\s+:\s+([\d,]+)", t).group(1).replace(",", ""))
    tot_m += int(re.search(r"MISMATCHES\s+:\s+([\d,]+)", t).group(1).replace(",", ""))
    tot_i += int(re.search(r"invariant failures\s+:\s+([\d,]+)", t).group(1).replace(",", ""))
    for k, c, r in re.findall(r"^\s{4}(\w+)\s+([\d,]+)\s+rejected by both:\s+([\d,]+)", t, re.M):
        a, b = ops.get(k, (0, 0))
        ops[k] = (a + int(c.replace(",", "")), b + int(r.replace(",", "")))
print(f"scenarios          : {tot_s:,}")
print(f"operations         : {tot_o:,}")
for k in sorted(ops):
    c, r = ops[k]
    print(f"    {k:<5} {c:>12,}   rejected by both: {r:>10,} ({r/c:5.1%})")
print(f"invariant failures : {tot_i:,}")
print(f"MISMATCHES         : {tot_m:,}")
print()
print("RESULT: " + ("PASS — the Rust core and the Python reference are bit-identical"
                    if tot_m == 0 and tot_i == 0 else "FAIL — see the shard logs"))
EOF
date -u
exit $fail
