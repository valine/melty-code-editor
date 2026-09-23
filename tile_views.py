"""Application workspace and reusable editors offered by the tile picker."""
from meltygui import draw_claude_chat, DrawState
from project_tree import draw_project_tree, project_selection
from meltygui_pro import draw_code_editor
from meltygui_pro.models.open_files import OpenFiles
from editor_settings import settings
from tasks import draw_tasks, request_module_run
from file_editor import draw_file_editor


def draw_main_editor(input_value: OpenFiles, files_view: DrawState[draw_project_tree] = None, **kwargs):
    """Apply the app's editor options without adding a render boundary."""
    options = settings["Editor"]
    return draw_code_editor(
        input_value, name="code-editor", disable_scroll=True,
        show_shortcuts=options["show_shortcuts"], show_breadcrumbs=options["show_breadcrumbs"],
        syntax_analysis=options["syntax_analysis"], show_ribbons=options["show_ribbons"],
        on_run_module=request_module_run, project_source=project_selection(files_view),
        files_view=files_view,
        min_height=0, **kwargs)


# The tile picker's row for the editor: the chat's recipe (`__header_defaults__`
# on a plain function); the tint is the workspace tile's (app_model.py).
draw_main_editor.__header_defaults__ = {"tint": (0.11, 0.12, 0.17), "icon": "\uf121",
                                        "display_name": "Code Editor"}


# The Claude Code chat tile: meltygui's view, one call with no render boundary
# of its own. A tile split from an editor inherits its OpenFiles, so new
# conversations start in the selected tab's project (meltygui_pro's
# `chat_project` service). The name keeps tiles of sessions saved as
# `tile_views.draw_chat` resolving.
draw_chat = draw_claude_chat
