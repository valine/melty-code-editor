"""The code editor's user settings: a class of plain values the render
functions read directly (`CodeEditorSettings.Editor.show_breadcrumbs`).

editor.py mirrors the class as a CodeDict and hands it to the root window
(`@glfw_window(settings=...)`): the title bar's cog opens it, each nested
class is a folder of the settings window. An edit there lands on the live
class at once (Hotswap) and in `~/.config/melty-code-editor/
launch_overrides.json` (LaunchOverride), which the next launch applies
before this module's importers see it. The values below stay the defaults;
the user's choices never edit this file.

To add a setting: add the attribute here with its default, read it where
the option is passed (tile_views.py). Nothing else to register.
"""


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
