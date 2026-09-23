"""The application's persisted tile layout shares its existing open-file model."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.layout.tile_manager_core import walk
from meltygui.model.tile_model import Split, Tile


class EditorAppModel(DictConversion):
    def __init__(self):
        from tile_views import draw_main_editor
        super().__init__()
        self.tiles = Split("x", [Tile("Code Editor", tint=(0.11, 0.12, 0.17),
                                      render_func=draw_main_editor)])

    def bind_open_files(self, open_files):
        """Retain the existing app's saved tabs when adopting the tile layout."""
        for _path, tile in walk(self.tiles):
            if isinstance(tile, Tile):
                tile.input_value = open_files

    def reconcile_editors(self, open_files):
        from tile_views import draw_main_editor, draw_file_editor
        editors = [tile for _path, tile in walk(self.tiles)
                   if isinstance(tile, Tile) and tile.render_func in (draw_main_editor, draw_file_editor)]
        instances = {tile.id for tile in editors}
        open_files.primary_instance = editors[0].id if editors else 0
        if open_files.active_instance not in instances:
            open_files.active_instance = open_files.primary_instance
            open_files.active_path = None

    def file_editor_ids(self):
        from file_editor import draw_file_editor
        return {tile.id for _path, tile in walk(self.tiles)
                if isinstance(tile, Tile) and tile.render_func is draw_file_editor}

    def ensure_tasks(self, open_files):
        """A queued run must have a visible output tile, even in a fresh layout."""
        from tasks import draw_tasks
        if any(isinstance(tile, Tile) and tile.render_func is draw_tasks
               for _path, tile in walk(self.tiles)):
            return False
        tile = Tile('Tasks', tint=(0.36, 0.47, 0.42),
                    render_func=draw_tasks, input_value=open_files)
        self.tiles = Split('y', [self.tiles, tile])
        return True
