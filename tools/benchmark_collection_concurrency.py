"""Offline latency benchmark. No real credentials, network, model or sender."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_concurrent_collection import ConcurrentTransport, collect
from test_batched_collection import task


def main():
    for latency in (.05, 1.):
        for workers in (1, 4):
            transport = ConcurrentTransport([task(index) for index in range(8)], latency=latency)
            start = time.monotonic()
            collect(transport, workers=workers)
            elapsed = time.monotonic() - start
            starts = [row[2] for row in transport.requests]
            print(json.dumps({
                'kind': 'SYNTHETIC_TRANSPORT_REAL_WALLCLOCK', 'tasks': 8,
                'latency_seconds': latency, 'workers': workers,
                'requests': len(starts), 'elapsed_seconds': round(elapsed, 3),
                'peak_inflight': transport.peak,
                'min_start_gap_seconds': round(min(b - a for a, b in zip(starts, starts[1:])), 4),
            }), flush=True)


if __name__ == '__main__':
    main()
