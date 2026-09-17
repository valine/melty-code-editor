"""Low-overhead slow-frame, thread CPU and GC timeline for real desktop input."""
import functools
import gc
import json
import pathlib
import runpy
import sys
import threading
import time
ROOT = pathlib.Path(__file__).resolve().parent

def main():
    import melty
    from src.lsd.gl_gui import app
    app._hook_main_return = lambda: None
    original_init = app._init_melty
    output = (ROOT / 'timeline.jsonl').open('a', buffering=1)
    def emit(kind, **data):
        output.write(json.dumps(dict(kind=kind, time=time.perf_counter(), thread=threading.current_thread().name, **data))+'\n')
    emit('start', wall=time.time(), pid=__import__('os').getpid())
    gc_start = {}
    def on_gc(phase, info):
        key = threading.get_ident()
        if phase == 'start':
            gc_start[key] = (time.perf_counter(), time.thread_time())
        else:
            start, cpu = gc_start.pop(key, (time.perf_counter(),time.thread_time()))
            elapsed = (time.perf_counter()-start)*1000
            if elapsed > 2:
                emit('gc', start=start, ms=elapsed, cpu_ms=(time.thread_time()-cpu)*1000, **info)
    gc.callbacks.append(on_gc)
    def instrument(module, name):
        original = getattr(module,name)
        @functools.wraps(original)
        def measured(*args, **kwargs):
            start,cpu=time.perf_counter(),time.thread_time()
            try: return original(*args,**kwargs)
            finally:
                end=time.perf_counter();elapsed=(end-start)*1000
                if elapsed>10:
                    emit('span', name=name,start=start,end=end,ms=elapsed,cpu_ms=(time.thread_time()-cpu)*1000)
        setattr(module,name,measured)
    def init():
        original_init()
        from src.lsd.gl_gui.surface import Surface
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views import text_editor
        from src.lsd.gl_gui.view.core_conversion import code_checks,new_converters,symbol_roster
        import ast
        instrument(ast, 'parse')
        import libcst
        instrument(libcst, 'parse_module')
        from src.lsd.gl_gui.view.core_conversion import libcst_conversion
        instrument(libcst_conversion, 'cst_dict_incremental_update')
        from src.lsd.gl_gui.view.core_conversion import core_syntax
        for name in ('reparse_incremental','reparse_reusing','materialize_parse','parse_to_dict'):
            instrument(core_syntax,name)
        instrument(core_syntax._ScanWorker,'scan_extract')
        for module,names in [(Surface,['_draw_render_hosts']), (Melty,['begin_frame','end_frame','post_frame','_drain_render_tasks']), (text_editor,['_completion_pool','_ac_import_rows','_def_tints','_fim_poll','_fold_build','_scope_guide_segments']), (code_checks,['_module_text_binds','_buffer_bound_names','_suggest_import','collect_import_suggestions']), (symbol_roster,['extract_table']), (new_converters,['_run_chain_in','_run_relint','check_source','check_source_incremental','_region_compile_check','_compile_check'])]:
            for name in names: instrument(module,name)
        original_frame=Surface.frame
        state={'edited':False,'sections':[], 'start':None, 'samples':[]}
        def watch():
            while True:
                time.sleep(.005)
                start=state['start']
                if start and time.perf_counter()-start>.025 and len(state['samples'])<12:
                    names={t.ident:t.name for t in threading.enumerate()}
                    stacks={}
                    for tid, stack in sys._current_frames().items():
                        frames=[]
                        while stack and len(frames)<10:
                            frames.append((stack.f_code.co_name,stack.f_code.co_filename,stack.f_lineno))
                            stack=stack.f_back
                        stacks[names.get(tid,str(tid))]=frames
                    state['samples'].append((time.perf_counter(),stacks))
        threading.Thread(target=watch,name='stall-watch',daemon=True).start()
        def trace(message,**kwargs):
            if message=='draw_text perf':
                state['edited'] |= kwargs.get('changed',False)
                if kwargs.get('total_ms',0)>15 or kwargs.get('changed'): state['sections'].append(kwargs)
        text_editor._ptrace=trace
        def frame(surface):
            phase=(ROOT/'phase.txt').read_text().strip()
            state['edited']=False;state['sections']=[]
            start,cpu=time.perf_counter(),time.thread_time()
            state['start']=start;state['samples']=[]
            result=original_frame(surface)
            end=time.perf_counter();elapsed=(end-start)*1000
            state['start']=None
            if state['samples']:emit('samples',start=start,samples=state['samples'])
            emit('frame',start=start,end=end,ms=elapsed,cpu_ms=(time.thread_time()-cpu)*1000,phase=phase,edited=state['edited'],sections=state['sections'])
            return result
        Surface.frame=frame
    app._init_melty=init
    sys.argv=[str(ROOT.parents[2]/'editor.py'),str(ROOT/'new_core_view.py')]
    runpy.run_path(sys.argv[0],run_name='__main__')
    app.run()

if __name__=='__main__':main()
