"""The code editor's user settings: a class of plain values the render
functions read directly (`CodeEditorSettings.Editor.show_breadcrumbs`).

editor.py mirrors the class as a CodeDict and hands it to the root window
(`@glfw_window(settings=...)`): the title bar's cog opens it, each nested
class is a folder of the settings window. An edit there lands on the live
class at once (Hotswap) and in `~/.config/melty-code-editor/
launch_overrides.json` (LaunchOverride), which the next launch applies
before this module's importers see it. The values below stay the defaults;
the user's choices never edit this file.

Render functions read `settings[...]`, the dict, not the class: the read
is what tells the dict which view to repaint when that value changes.

To add a setting: add the attribute here with its default, read it from
`settings` where the option is passed (tile_views.py). Nothing else to
register.
"""
from meltygui.model.code_dict_model import CodeDict, Hotswap, LaunchOverride


class CodeEditorSettings:

    class Editor:
        # The file browser's shortcuts column (home, the XDG folders, the
        # marked projects) to the left of the editor.
        show_shortcuts = True
        # The path strip along the top of the selected file's column.
        show_breadcrumbs = True
        # Python syntax analysis: folds, symbol colours, autocomplete.
        syntax_analysis = True
        # The change ribbons beside the scrollbar.
        show_ribbons = True

    class FileTree:
        # Dotfiles and dot-folders in the Files tile (and its search).
        show_hidden = False


# CodeEditorSettings as a dict: the root window's settings window edits it,
# the render functions read it.
settings = CodeDict(CodeEditorSettings, write_to=(LaunchOverride, Hotswap))
