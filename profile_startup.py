#!/usr/bin/env python3
"""Measure fresh processes through presentation of the selected file's text.

Run on a reserved agent desktop with WAYLAND_DISPLAY set to its socket.
Uses isolated session state; leaves the supplied source file unchanged.
Normal OS/driver/font caches remain. No process or GL context is reused.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def child(args):
    started = float(os.environ['MELTY_T0'])
    source = args.file.resolve()
    expected = source.read_text()
    profiler = None
    if args.profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
    import melty
    from src.lsd.gl_gui import app
    # Explicitly drive run() so profiling and readiness checks include the loop.
    app._hook_main_return = lambda: None
    original_init = app._init_melty
    result = {'first_frame_ms': None, 'text_presented_ms': None, 'frames': 0}

    def init():
        original_init()
        from src.lsd.gl_gui.surface import Surface
        from src.lsd.gl_gui import window_api
        original_frame = Surface.frame

        def frame(surface):
            value = original_frame(surface)
            elapsed = (time.time() - started) * 1000
            result['frames'] += 1
            if result['first_frame_ms'] is None:
                result['first_frame_ms'] = elapsed
            module = sys.modules.get('src.lsd.gl_gui.view.playground.open_files')
            editors = getattr(module, '_active_editors', {})
            if any(path == str(source) and pane is not None and text == expected
                   for path, pane, text in editors.values()):
                result['text_presented_ms'] = elapsed
                for opened in Surface.all:
                    window_api.set_window_should_close(opened.window, True)
            return value
        Surface.frame = frame
    app._init_melty = init
    import runpy
    sys.argv = [str(args.editor), str(source)]
    runpy.run_path(str(args.editor), run_name='__main__')
    app.run()
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    result['events'] = [{'phase': name, 'ms': (stamp - started) * 1000}
                        for name, stamp in app._MARKS]
    result['glfw_imported'] = 'glfw' in sys.modules
    if result['text_presented_ms'] is None:
        raise RuntimeError('Editor closed before presenting the supplied text')
    print('STARTUP_JSON=' + json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--editor', type=Path, default=ROOT / 'editor.py')
    parser.add_argument('--compare-editor', type=Path, help='Alternate with a saved baseline editor')
    parser.add_argument('--runs', type=int, default=8)
    parser.add_argument('--output', type=Path, default=ROOT / 'profiles/startup.json')
    parser.add_argument('--profile', type=Path, help='cProfile output; use --runs 1')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    if args.runs < 1 or (args.profile and (args.runs != 1 or args.compare_editor)):
        parser.error('runs must be positive; profiling requires --runs 1')
    if '-agent-desk-' not in os.environ.get('WAYLAND_DISPLAY', ''):
        parser.error('set WAYLAND_DISPLAY to a reserved agent desktop socket')
    samples = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save(complete):
        args.output.write_text(json.dumps({
            'scenario': 'fresh processes; normal disk caches',
            'complete': complete, 'runs_per_variant': args.runs,
            'source': str(args.file.resolve()), 'python': sys.version,
            'samples': samples}, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='melty-code-profile-') as state:
        editors = [('after', args.editor)]
        if args.compare_editor:
            editors.insert(0, ('before', args.compare_editor))
        jobs = [(variant, editor) for run in range(args.runs)
                for variant, editor in (editors if run % 2 == 0 else editors[::-1])]
        for variant, editor in jobs:
            environment = dict(os.environ, XDG_STATE_HOME=str(Path(state) / variant),
                               MELTY_T0=str(time.time()))
            environment.pop('MELTY_BENCH', None)
            command = [sys.executable, str(Path(__file__).resolve()), str(args.file.resolve()),
                       '--editor', str(editor.resolve()), '--child']
            if args.profile:
                args.profile.parent.mkdir(parents=True, exist_ok=True)
                command += ['--profile', str(args.profile.resolve())]
            process = subprocess.run(command, env=environment, cwd=ROOT,
                                     capture_output=True, text=True, timeout=30)
            if process.returncode:
                raise RuntimeError(process.stdout + process.stderr)
            line = next(line for line in process.stdout.splitlines()
                        if line.startswith('STARTUP_JSON='))
            samples.append(dict(variant=variant, **json.loads(line.split('=', 1)[1])))
            save(False)  # Preserve completed samples if the test seat disappears.
    save(True)
    for variant, _ in editors:
        for phase in ('first_frame_ms', 'text_presented_ms'):
            values = [sample[phase] for sample in samples if sample['variant'] == variant]
            print(f'{variant} {phase}: median {statistics.median(values):.1f} ms; '
                  f'range {min(values):.1f}–{max(values):.1f} ms')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
