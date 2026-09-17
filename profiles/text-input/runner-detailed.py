"""Instrument real desktop input; control capture via phase.txt (name or name:profile)."""
import cProfile
import json
import pathlib
import runpy
import sys
import time
ROOT = pathlib.Path(__file__).resolve().parent
def main():
    import melty
    from src.lsd.gl_gui import app
    app._hook_main_return = lambda: None
    original_init = app._init_melty
    
    def init():
        original_init()
        from src.lsd.gl_gui.surface import Surface
        original_frame = Surface.frame
        import functools
        import threading
        from src.lsd.gl_gui.view.core_views import text_editor
        from src.lsd.gl_gui.view.core_conversion import code_checks, symbol_roster, new_converters
        spans = (ROOT / 'spans.jsonl').open('a', buffering=1)
        def instrument(module, name):
            original = getattr(module, name)
            @functools.wraps(original)
            def measured(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    end = time.perf_counter()
                    spans.write(json.dumps({'name': name, 'start': start, 'ms': (end-start)*1000,
                        'thread': threading.current_thread().name})+'\n')
            setattr(module, name, measured)
        for name in ('_scope_guide_segments', '_scope_fold_ranges', '_fold_build', '_def_tints',
                     '_completion_pool', '_fim_poll', '_draw_cst_token_views'):
            instrument(text_editor, name)
        instrument(code_checks, 'collect_import_suggestions')
        instrument(symbol_roster, 'extract_table')
        instrument(new_converters, '_common_indent')
        instrument(new_converters, '_region_compile_check')
        for name in ('_ac_lex_state', '_update_line_open', '_fold_reassemble', '_fold_carry', '_call_context', '_ac_import_rows', '_ac_param_suffixes'):
            instrument(text_editor, name)

        from src.lsd.gl_gui.melty import Melty
        instrument(Surface, '_draw_render_hosts')
        for name in ('begin_frame', 'end_frame', 'post_frame', '_drain_render_tasks'):
            instrument(Melty, name)
        for name in ('_module_text_binds', '_suggest_import', '_buffer_bound_names'):
            instrument(code_checks, name)

        trace_output = (ROOT / 'sections.jsonl').open('a', buffering=1)
        def trace(message, **kwargs):
            if message == 'draw_text perf':
                state['edited'] = state.get('edited', False) or kwargs.get('changed', False)
                trace_output.write(json.dumps(dict(time=time.perf_counter(), **kwargs))+'\n')
        text_editor._ptrace = trace
        original_fold_update = text_editor._fold_update_inline
        def fold_update(old, text, built):
            result = original_fold_update(old, text, built)
            edit = text_editor._text_splice(old, text)
            trace_output.write(json.dumps({'fold_update': result is not None, 'edit': edit,
                'header': edit is not None and any(f[0][0] == edit[4] for f in built[2])})+'\n')
            return result
        text_editor._fold_update_inline = fold_update

        import inspect
        line_costs = {}
        line_previous = [None, None]
        monitor = sys.monitoring
        monitor.use_tool_id(3, 'typing-lines')
        raw_text = inspect.unwrap(text_editor.draw_text).__code__
        def line_event(code, line):
            now = time.perf_counter()
            if line_previous[0] is not None:
                line_costs[line_previous[0]] = line_costs.get(line_previous[0], 0) + now-line_previous[1]
            line_previous[:] = [line, now]
        monitor.register_callback(3, monitor.events.LINE, line_event)
        state = {'phase': None, 'profile': None, 'previous': None, 'text': None}
        output = (ROOT / 'frames.jsonl').open('a', buffering=1)
        def frame(surface):
            phase = (ROOT / 'phase.txt').read_text().strip()
            if phase != state['phase']:
                if state['profile']:
                    state['profile'].dump_stats(str(ROOT / (state['phase'].split(':')[0] + '.prof')))
                state['phase'] = phase
                state['profile'] = cProfile.Profile() if phase.endswith(':profile') else None
            monitor.set_local_events(3, raw_text, monitor.events.LINE if phase == 'line_profile' else 0)
            line_previous[:] = [None, None]
            state['edited'] = False
            started = time.perf_counter()
            if state['profile']:
                state['profile'].enable()
            try:
                result = original_frame(surface)
            finally:
                if state['profile']:
                    state['profile'].disable()
            ended = time.perf_counter()
            if phase == 'line_profile':
                (ROOT / 'lines.json').write_text(json.dumps(line_costs))
            module = sys.modules.get('src.lsd.gl_gui.view.playground.open_files')
            entries = list(getattr(module, '_active_editors', {}).values())
            current = next((text for path, pane, text in entries if path == str(ROOT / 'new_core_view.py')), None)
            changed = current is not None and current is not state['text'] and current != state['text']
            output.write(json.dumps({'time': started, 'phase': phase, 'frame_ms': (ended-started)*1000,
                'interval_ms': (started-state['previous'])*1000 if state['previous'] else None,
                'changed': changed, 'edited': state['edited'], 'length': len(current) if current else None})+'\n')
            if changed:
                (ROOT / 'visible-text.txt').write_text(current)
            state['text'] = current
            state['previous'] = started
            return result
        Surface.frame = frame
    app._init_melty = init
    sys.argv = [str(ROOT.parents[1] / 'editor.py'), str(ROOT / 'new_core_view.py')]
    runpy.run_path(sys.argv[0], run_name='__main__')
    app.run()

if __name__ == "__main__":
    main()
