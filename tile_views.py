"""Application workspace and reusable editors offered by the tile picker."""
from meltygui import draw_claude_chat
from meltygui_pro import draw_code_editor
from meltygui_pro.models.open_files import OpenFiles
from editor_settings import settings


def draw_main_editor(input_value: OpenFiles, **kwargs):
    """Apply the app's editor options without adding a render boundary."""
    options = settings["Editor"]
    return draw_code_editor(
        input_value, name="code-editor", disable_scroll=True,
        show_shortcuts=options["show_shortcuts"], show_breadcrumbs=options["show_breadcrumbs"],
        syntax_analysis=options["syntax_analysis"], show_ribbons=options["show_ribbons"],
        min_height=0, **kwargs)


# The Claude Code chat tile: meltygui's view, one call with no render boundary
# of its own. A tile split from an editor inherits its OpenFiles, so new
# conversations start in the selected tab's project (meltygui_pro's
# `chat_project` service). The name keeps tiles of sessions saved as
# `tile_views.draw_chat` resolving.
draw_chat = draw_claude_chat
