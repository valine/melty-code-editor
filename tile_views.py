"""Application workspace and reusable editors offered by the tile picker."""
from meltygui import imgui
from meltygui.core.core_render import render_func
from meltygui.core.runtime.toggles import Tint
from meltygui.hdr_color import pack_color
from meltygui_pro import draw_code_editor
from meltygui_pro.models.open_files import OpenFiles


def draw_main_editor(input_value: OpenFiles, **kwargs):
    """Apply the app's editor options without adding a render boundary."""
    return draw_code_editor(
        input_value, name="code-editor", disable_scroll=True,
        show_shortcuts=True, show_breadcrumbs=True, min_height=0, **kwargs)


@render_func(multi_instance=True, tint=(0.36, 0.24, 0.44), show_header=False)
def draw_placeholder(input_value: object, draw_state):
    # This intentionally has no model or actions; use it to try tile layouts.
    imgui.get_window_draw_list().add_text(
        draw_state.abs_left + 12, draw_state.abs_top + 12,
        pack_color(*Tint.dd_text(), 1.0), "Placeholder")
    return False, input_value
