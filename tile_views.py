"""Application workspace and reusable editors offered by the tile picker."""
from meltygui import imgui
from meltygui.core.core_render import render_func
from meltygui.core.layout.tile_manager_core import TileManagerState, draw_tiles
from meltygui.core.runtime.toggles import Tint
from meltygui.hdr_color import pack_color
from meltygui_pro import draw_code_editor
from meltygui_pro.models.open_files import OpenFiles
from app_model import EditorAppModel


@render_func(multi_instance=True, tint=(0.134, 0.257, 0.317), show_header=False)
def draw_main_editor(input_value: OpenFiles, draw_state, instance=0, layout_frame=None):
    changed, _ = draw_code_editor(
        input_value, name="code-editor", instance=instance, layout_frame=layout_frame,
        disable_scroll=True, show_shortcuts=True, show_breadcrumbs=True,
        width=draw_state.width, height=draw_state.height, min_height=0)
    return changed, input_value


@render_func(multi_instance=True, tint=(0.36, 0.24, 0.44), show_header=False)
def draw_placeholder(input_value: object, draw_state):
    # This intentionally has no model or actions; use it to try tile layouts.
    imgui.get_window_draw_list().add_text(
        draw_state.abs_left + 12, draw_state.abs_top + 12,
        pack_color(*Tint.dd_text(), 1.0), "Placeholder")
    return False, input_value


@render_func(tint=(0.11, 0.12, 0.17), closable=True, auto_resize=False,
             show_header=False, disable_scroll=True, frame_pinned=True)
def draw_editor_workspace(input_value: EditorAppModel, draw_state,
                          tile_state: TileManagerState = None,
                          multi_instance_renderers=()):
    changed = draw_tiles(input_value.tiles, draw_state, tile_state=tile_state,
                         multi_instance_renderers=multi_instance_renderers)
    return changed, input_value
