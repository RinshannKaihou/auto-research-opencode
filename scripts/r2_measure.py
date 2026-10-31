#!/usr/bin/env python3
"""Summarize local invocation metadata; never interpret missing usage as zero."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def summarize(paths):
    starts, ends = {}, {}
    incomplete = invalid = 0
    attempts = Counter()
    retry_attempts = 0
    host_fields = 0
    host_field_samples = 0
    bindings, expansions, usage = {}, {}, {}
    dispatches, external, gaps, retries = [], [], [], []
    for path in paths:
        for line in Path(path).read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\n"):
                incomplete += 1
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                invalid += 1
                continue
            if not isinstance(row, dict) or row.get("version") != "invocation/1":
                invalid += 1
                continue
            phase = row.get("phase")
            if phase=='binding':
                bindings[row['invocation_id']] = row['context']; continue
            if phase=='expansion':
                expansions[row['invocation_id']] = row; continue
            if phase=='dispatch_result':
                dispatches.append(row); continue
            if phase=='external_execution':
                external.append(row); continue
            if phase=='coverage_gap':
                gaps.append(row); continue
            if phase=='provider_retry':
                retries.append(row); continue
            if phase in {'model_request','native_aggregate'}:
                continue
            if phase=='native_usage':
                key = row['source_key']
                previous = usage.pop(row.get('request_key'),{}) if row.get('request_key') else usage.get(key,{})
                usage[key] = {**previous,**row}
                continue
            required = {"start": {"invocation_id", "input_fields", "input_chars"},
                        "end": {"invocation_id", "outcome", "duration_ms"},
                        "transport": {"operation_id", "attempt"}}
            if phase not in required or not required[phase].issubset(row):
                invalid += 1
                continue
            if phase == "start":
                starts[row["invocation_id"]] = row
            elif phase == "end":
                if row["outcome"] == "failed" and "error_code" not in row:
                    invalid += 1
                    continue
                ends[row["invocation_id"]] = row
            elif phase == "transport":
                attempts[row["operation_id"]] += 1
                retry_attempts += int(row['attempt']>1)
                if row['attempt']==1:
                    if 'host_fields' in row:
                        host_fields += row['host_fields']
                        host_field_samples += 1
    outcomes = Counter(ends[key]["outcome"] if key in ends else "unknown" for key in starts)
    durations = sorted(ends[key]["duration_ms"] for key in starts if key in ends)
    known_tokens,known_cost=[],[]
    for row in usage.values():
        token = row.get('tokens')
        if token is not None:
            known_tokens.append(token['input']+token['output']+token['reasoning']+token['cache']['read']+token['cache']['write'])
        if row.get('cost') is not None: known_cost.append(row['cost'])
    direct = [row for row in dispatches if row.get('invocation_id') in starts and
              starts[row['invocation_id']].get('operation_id') == row['operation_id']]
    return {
        "invocations": len(starts), "outcomes": dict(outcomes),
        "rejections": dict(Counter(ends[key]["error_code"] for key in starts
                                   if key in ends and ends[key]["outcome"] == "failed")),
        "duration_ms": {"median": statistics.median(durations) if durations else None,
                        "p90": durations[max(0, (9 * len(durations) + 9) // 10 - 1)] if durations else None},
        "input_fields": {"total": sum(row["input_fields"] for row in starts.values())},
        "input_chars": {"total": sum(row["input_chars"] for row in starts.values())},
        "transport_attempts": sum(attempts.values()),
        'transport_retries':retry_attempts,
        'provider_retry_events':len(retries),
        'provider_retry_decisions':sum(row.get('will_retry') is True for row in retries),
        'replayed':sum(row['replayed'] is True for row in direct),
        'deduplicated':sum(row['deduplicated'] is True for row in direct),
        'wire_host_metadata_fields':host_fields if host_field_samples else None,
        'wire_host_metadata_samples':host_field_samples,
        'applied_fields':sum(row['applied_fields'] for row in expansions.values()),
        'external_executions':len([row for row in external if row['status']=='started']),
        'model_usage':{'tokens_observed':sum(known_tokens) if known_tokens else None,
                       'cost_observed':sum(known_cost) if known_cost else None,
                       'sources':len(usage),'missing_tokens':len(usage)-len(known_tokens),
                       'missing_cost':len(usage)-len(known_cost),
                       'model_unknown':sum(not row.get('model') for row in usage.values())},
        "coverage": {"missing_end": outcomes["unknown"], "orphan_end": len(ends.keys() - starts.keys()),
                     "incomplete_lines": incomplete, "invalid_lines": invalid,
                     'binding_unknown':len(starts.keys()-bindings.keys()),'explicit_gaps':len(gaps),
                     'replay_unknown':sum(row['replayed'] is None for row in direct),
                     "model_usage": "observed_subset" if usage else "not_collected", "transport": "plugin_dispatch_chain",
                     'scope':'available log files; unlogged I/O failure cannot be ruled out'},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.paths), ensure_ascii=False, indent=2))
