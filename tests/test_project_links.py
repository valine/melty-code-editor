"""Files selections flow through the tile framework, without sibling discovery."""
from unittest.mock import Mock

from meltygui.core.layout.tile_links import prepare_endpoints, resolve_parameters, set_binding
from meltygui.model.tile_model import Split, Tile
from meltygui.core.melty import Melty
from meltygui.core.cache.parameter_dependencies import ParameterDependencies
from meltygui_pro.models.open_files import OpenFiles
from project_tree import draw_project_tree, project_selection, project_editors
from tile_views import draw_main_editor
from file_editor import draw_file_editor, FileEditorState, adopt_selection
from tasks import draw_tasks, tile_project
from app_model import EditorAppModel


def test_all_consumers_resolve_files_and_unlink_persists(monkeypatch):
    monkeypatch.setattr(Melty, 'draw_state_registry', {})
    model = EditorAppModel()
    tiles = [Tile(render_func=view) for view in
             (draw_project_tree, draw_main_editor, draw_file_editor, draw_tasks)]
    model.tiles = Split('x', tiles)
    files = OpenFiles()
    model.bind_open_files(files)
    endpoints = prepare_endpoints(model.tiles)
    source = endpoints[tiles[0].id]
    selection = project_selection(source.draw_state)
    selection.selected_project = '/selected'
    for tile in tiles[1:]:
        endpoint = endpoints[tile.id]
        assert resolve_parameters(endpoint, endpoints)['files_view'] is source.draw_state
    assert tile_project(source.draw_state, files) == '/selected'
    set_binding(endpoints[tiles[1].id], 'files_view', None)
    model.bind_open_files(files)
    assert resolve_parameters(endpoints[tiles[1].id], endpoints)['files_view'] is None
    model.tiles.children.remove(tiles[0])
    remaining = prepare_endpoints(model.tiles)
    assert resolve_parameters(remaining[tiles[2].id], remaining)['files_view'] is None


def test_file_editor_uses_selected_project_and_updates_source(tmp_path, monkeypatch):
    from project_tree import ProjectPanelState
    monkeypatch.setattr(Melty, 'cache', None)
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    a, b = str(first / 'a.txt'), str(second / 'b.txt')
    files, state, selection = OpenFiles(), FileEditorState(), ProjectPanelState()
    files.open_paths = [a, b]
    selection.selected_project = str(first)
    assert adopt_selection(files, state, 3, selection) == [a]
    files.jump_to_instance, files.jump_to_path = 3, b
    monkeypatch.setattr('meltygui_pro.models.projects.project_for', lambda path: second)
    assert adopt_selection(files, state, 3, selection) == [b]
    assert selection.selected_project == str(second)


def test_file_open_routes_through_injection_subscriptions(monkeypatch):
    monkeypatch.setattr(Melty, 'draw_state_registry', {})
    tree = Split(children=[Tile(render_func=view) for view in
                           (draw_project_tree, draw_file_editor, draw_tasks)])
    endpoints = list(prepare_endpoints(tree).values())
    source, editor, tasks = (endpoint.draw_state for endpoint in endpoints)
    editor._view_func, tasks._view_func = draw_file_editor, draw_tasks
    editor._kwargs = {'instance': 'editor'}
    dependencies = ParameterDependencies()
    dependencies.bind(editor, {'files_view': source})
    dependencies.bind(tasks, {'files_view': source})
    monkeypatch.setattr(Melty, 'cache', Mock(parameter_dependencies=dependencies))
    assert project_editors(source) == [editor]
    dependencies.bind(editor, {})
    assert project_editors(source) == []


def test_diff_with_links_views_without_sharing_editor_state(monkeypatch):
    from meltygui.core.rendering.injected_state import owned_state
    from file_editor import comparison_pairs
    monkeypatch.setattr(Melty, 'draw_state_registry', {})
    tree = Split(children=[Tile(render_func=draw_file_editor), Tile(render_func=draw_file_editor)])
    endpoints = prepare_endpoints(tree)
    first, second = endpoints.values()
    assert 'diff_with' in first.parameters
    assert 'file_editor_state' not in first.parameters
    private_a = owned_state(first.draw_state, 'file_editor_state', FileEditorState)
    private_b = owned_state(second.draw_state, 'file_editor_state', FileEditorState)
    private_a.version, private_b.version = 'current', 'HEAD'
    from meltygui.core.layout.tile_links import candidates
    identity, target = next(candidates(first, 'diff_with', endpoints))
    set_binding(first, 'diff_with', identity)
    first.draw_state._kwargs = resolve_parameters(first, endpoints)
    second.draw_state._kwargs = resolve_parameters(second, endpoints)
    live = {first.tile.id: (first.draw_state, private_a), second.tile.id: (second.draw_state, private_b)}
    assert target is second.draw_state
    assert len(comparison_pairs(live)) == 1
    assert private_a is not private_b and (private_a.version, private_b.version) == ('current', 'HEAD')
    set_binding(first, 'diff_with', None)
    first.draw_state._kwargs = resolve_parameters(first, endpoints)
    assert comparison_pairs(live) == []


def test_old_comparison_choice_migrates_once(monkeypatch):
    from file_editor import migrate_comparison_link
    from meltygui.core.rendering.injected_state import owned_state
    from meltygui.core.layout.tile_links import bindings_for
    monkeypatch.setattr(Melty, 'draw_state_registry', {})
    tile = Tile(render_func=draw_file_editor)
    endpoint = next(iter(prepare_endpoints(Split(children=[tile])).values()))
    state = owned_state(endpoint.draw_state, 'file_editor_state', FileEditorState)
    state.sibling_tile_id = 'previous-target'
    migrate_comparison_link(tile)
    assert bindings_for(endpoint)['diff_with'][0] == 'previous-target'
    assert 'sibling_tile_id' not in vars(state)
    set_binding(endpoint, 'diff_with', None)
    migrate_comparison_link(tile)
    assert bindings_for(endpoint).get('diff_with') is None
