"""Application workspace and reusable editors offered by the tile picker."""
import pathlib

from meltygui.core.core_render import render_func
from meltygui_pro import draw_code_editor
from meltygui_pro.models.open_files import OpenFiles
from meltygui_pro.models.projects import project_for
from melty_claude import draw_claude_chat
from editor_settings import settings

# The chat tile's tint: its row in the tile picker (melty-claude's window tint).
CHAT_TINT = (0.35, 0.38, 0.44)


def draw_main_editor(input_value: OpenFiles, **kwargs):
    """Apply the app's editor options without adding a render boundary."""
    options = settings["Editor"]
    return draw_code_editor(
        input_value, name="code-editor", disable_scroll=True,
        show_shortcuts=options["show_shortcuts"], show_breadcrumbs=options["show_breadcrumbs"],
        syntax_analysis=options["syntax_analysis"], show_ribbons=options["show_ribbons"],
        min_height=0, **kwargs)


def chat_project(open_files):
    """Where a chat tile's new conversation starts: the selected tab's project
    (a marked project or the nearest repository root), else the tab's folder;
    None (the cwd) without a file tab."""
    path = open_files.active_path if isinstance(open_files, OpenFiles) else None
    if not isinstance(path, str) or path.startswith(OpenFiles.GIT_DIFF_PREFIX):
        return None
    root = project_for(path)
    return str(root if root else pathlib.Path(path).parent)


@render_func(multi_instance=True, tint=CHAT_TINT, icon=f"", display_name="Claude Code",
             show_header=False)
def draw_chat(input_value: object, draw_state, layout_frame=None):
    """melty-claude's window as a tile: the same sidebar, transcript, composer
    and detached conversations. A tile split from an editor inherits its
    ``OpenFiles``, so new conversations start in the selected tab's project;
    any other input leaves the project to the chat's own rules. The tile's
    ``layout_frame`` keeps the chat's columns inside the tile."""
    draw_claude_chat(default_project=chat_project(input_value), layout_frame=layout_frame,
                     width=draw_state.width, height=draw_state.height)
    return False, input_value
