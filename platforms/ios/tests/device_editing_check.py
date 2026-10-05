"""Opt-in full-editor device check; projects/sessions stay in Library/Caches."""
import json
from pathlib import Path
import sys
import time
import traceback


def create_app(config, host):
    output = Path(config['cache']) / 'editing-check'
    sandbox = output / str(time.time_ns())
    project = sandbox / 'Documents/Projects/Editing'
    project.mkdir(parents=True)
    (output / 'result.json').unlink(missing_ok=True)
    Path.home = classmethod(lambda cls: sandbox)
    source = project / 'main.py'
    source.write_text('value = 1\n')
    sys.argv[:] = ['melty-code-editor', str(source)]
    from meltygui.core.runtime import app
    from meltygui.core.runtime.native_app import NativeApplication
    from meltygui.core.graphics.metal_renderer import MetalRenderer
    import _melty_metal
    application = NativeApplication(config, host, lambda: MetalRenderer(_melty_metal))
    app.install_native_host(application)
    import editor
    app.run()
    return CheckedApplication(application, editor, output, project)


class CheckedApplication:
    def __init__(self, application, editor, output, project):
        self.application, self.editor = application, editor
        self.output, self.project = output, project
        self.phase = 'warmup'
        self.phase_frame = 0
        self.started = self.phase_time = time.monotonic()
        self.report = {}
        self.tap_index = 0
        self.raised = False

    def advance(self, phase):
        self.phase = phase
        self.phase_frame = self.application.frames
        self.phase_time = time.monotonic()

    def frame(self, info, events):
        from meltygui.core.melty import Melty
        from meltygui.core.runtime import app as runtime
        app, editor = self.application, self.editor
        original = None
        try:
            age = app.frames - self.phase_frame
            injected = []
            if self.phase == 'failure' and age == 0:
                original = editor.draw_file_editor_comparisons

                def fail(*args, **kwargs):
                    self.raised = True
                    raise NameError('device editing check: intentional caught view failure')

                editor.draw_file_editor_comparisons = fail
                Melty.cache.invalidate_all()
            elif self.phase == 'tap' and age in (0, 1):
                x, y = self.tap
                injected = [dict(kind='touch_begin' if age == 0 else 'touch_end',
                                 touch_id=100+self.tap_index, x=x, y=y)]
            elif self.phase == 'tap' and age == 3:
                assert self.field.text_cursor_pos == self.position
                injected = [dict(kind='text', text='Z')]
            elif self.phase == 'commit' and age == 0:
                self.field.text_selection_start = 0
                self.field.text_selection_end = len(editor.new_draft)
                self.field.invalidate_up()
                injected = [dict(kind='text', text=str(self.project / 'created.py'))]
            elif self.phase == 'commit' and age == 2:
                injected = [dict(kind='text', text='\n')]
            elif self.phase == 'edit' and age == 0:
                injected = [dict(kind='text', text='value = 41\nprint(value + 1)')]
            more = app.frame(info, [*events, *injected])
            if original is not None:
                editor.draw_file_editor_comparisons = original
                original = None
                Melty.cache.invalidate_all()
            elapsed = time.monotonic() - self.phase_time
            if self.phase == 'warmup' and app.frames >= 12:
                self.advance('failure')
            elif self.phase == 'failure' and age >= 4:
                assert self.raised and not runtime._state.get('failed')
                self.report['caught_view_recovered'] = True
                editor.request_new()
                Melty.cache.invalidate_all()
                self.advance('filename')
            elif self.phase == 'filename' and elapsed >= 1.2:
                self.field = Melty.text_focused_ds
                assert self.field.name == 'new-file'
                assert self.field._stack_trace is None, repr(self.field._stack_trace)
                self.report.update(display_scale=info['scale'], glyph_advance=self.field._diff_char_w,
                                   filename_length=len(editor.new_draft), keyboard_height=info['height'])
                self.prepare_tap()
            elif self.phase == 'tap' and age >= 5 and elapsed >= .35:
                expected = self.before[:self.position]+'Z'+self.before[self.position:]
                assert editor.new_draft == expected, (expected, editor.new_draft)
                self.tap_index += 1
                if self.tap_index < 3:
                    self.prepare_tap()
                else:
                    self.report['filename_touch_edits'] = self.tap_index
                    self.advance('commit')
            elif self.phase == 'commit' and elapsed >= .8:
                assert editor.new_draft is None
                assert (self.project / 'created.py').exists()
                assert Melty.text_focused_ds.text_cursor_pos == 0
                self.advance('edit')
            elif self.phase == 'edit' and elapsed >= .5:
                pane = Melty.text_focused_ds
                assert pane._stack_trace is None, repr(pane._stack_trace)
                text = pane._return_value[1]
                assert text == 'value = 41\nprint(value + 1)', repr(text)
                compile(text, str(self.project / 'created.py'), 'exec')
                self.report['created_file_keyboard_and_compile'] = True
                app.suspend()
                app.resume()
                self.report['checkpoint_after_caught_exception'] = True
                self.report['status'] = 'passed'
                self.finish()
            if self.phase != 'done' and time.monotonic()-self.started > 35:
                raise TimeoutError(self.phase)
            return more or self.phase != 'done'
        except Exception:
            self.report.update(status='failed', phase=self.phase, traceback=traceback.format_exc())
            self.finish()
            return False
        finally:
            if original is not None:
                editor.draw_file_editor_comparisons = original

    def prepare_tap(self):
        field = self.field
        self.before = self.editor.new_draft
        x = (120, 280, 190)[self.tap_index]
        self.position = round((x-field.abs_left-field._diff_origin_x_off)/field._diff_char_w)
        self.tap = (field.abs_left + field._diff_origin_x_off + self.position*field._diff_char_w,
                    field.abs_top + field._diff_top_inset + field._diff_line_px/2)
        self.advance('tap')

    def finish(self):
        self.report['frames'] = self.application.frames
        (self.output / 'result.json').write_text(json.dumps(self.report, indent=2))
        print('DEVICE_EDITING_CHECK ' + json.dumps(self.report))
        self.phase = 'done'

    def presented(self): self.application.presented()
    def suspend(self): self.application.suspend()
    def resume(self): self.application.resume()
    def close(self): self.application.close()
