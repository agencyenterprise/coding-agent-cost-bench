#!/usr/bin/env python3
"""Print one run's API generation seconds (calls_s), reusing aggregate.py's parsers.

    python3 run_stats.py <harness> <outdir>   ->  "123.45"

opencode logs step_start/step_finish per request; Claude Code reports duration_api_ms.
bench.sh calls this as each job finishes to build the live calls_s / tools_s table.
tools_s (local tool + overhead) is just elapsed - calls_s, computed by the caller."""
import sys

from aggregate import claude_stats, log_stats

harness, outdir = sys.argv[1], sys.argv[2]
stats = claude_stats(outdir) if harness == "claude" else log_stats(outdir)
print(f"{stats.get('call_s', 0.0):.2f}")
