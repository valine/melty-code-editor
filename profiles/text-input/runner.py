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
        from src.lsd.gl_gui.view.core_views import text_editor
        def trace(message, **kwargs):
            if message == 'draw_text perf':
                state['edited'] = state.get('edited', False) or kwargs.get('changed', False)
        text_editor._ptrace = trace
        state = {'phase': None, 'profile': None, 'previous': None, 'text': None}
        output = (ROOT / 'frames.jsonl').open('a', buffering=1)
        def frame(surface):
            phase = (ROOT / 'phase.txt').read_text().strip()
            if phase != state['phase']:
                if state['profile']:
                    state['profile'].dump_stats(str(ROOT / (state['phase'].split(':')[0] + '.prof')))
                state['phase'] = phase
                state['profile'] = cProfile.Profile() if phase.endswith(':profile') else None
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
