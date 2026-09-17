"""Profile collection without opening a window or executing repository code."""
import argparse
import cProfile
import json
from pathlib import Path
import pstats
import statistics
import time

from meltygui_pro.models.project_dependencies import assemble_dependencies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', nargs='+', type=Path)
    parser.add_argument('--environment', type=Path)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for root in args.roots:
        request = (str(root.resolve()), str(args.environment.absolute()) if args.environment else None, 0)
        timings = []
        for iteration in range(args.repeat):
            start = time.perf_counter()
            snapshot = assemble_dependencies((*request[:2], iteration))
            timings.append(time.perf_counter() - start)
        profiler = cProfile.Profile()
        profiler.runcall(assemble_dependencies, request)
        profiler.dump_stats(str(args.output / (root.name + '.prof')))
        with (args.output / (root.name + '.txt')).open('w') as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(45)
        result = dict(root=str(root.resolve()), seconds=timings, median=statistics.median(timings),
                      imports=len(snapshot['imported']), rows=len(snapshot.get('table', [])), errors=snapshot['errors'])
        results.append(result)
        print(json.dumps(result), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
