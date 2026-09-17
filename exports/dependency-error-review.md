# Dependency discrepancy review

| Import | Finding | Source |
|---|---|---|
| `conftest` | Project-local module | `tests/test_auto_state_params.py:17` |
| `evdev` | Optional; guarded by ImportError | `src/lsd/gl_gui/events/touchpad_backend.py:44` |
| `event_backends` | Project-local module | `src/lsd/gl_gui/events/example.py:51` |
| `FolderProxy` | Removed obsolete import and unreachable legacy branch | `src/lsd/gl_gui/melty.py (removed)` |
| `input_handler` | Project-local module | `src/lsd/gl_gui/events/event_backends.py:42` |
| `model` | Project-local module | `server/model/dict_conversion.py:16` |
| `model_server` | Project-local module | `tests/test_launcher_swap.py:25` |
| `profile_render_wrapper` | Project-local module | `tests/profile_draw_text_frames.py:9` |
| `pynput` | Optional; guarded by ImportError | `src/lsd/gl_gui/events/event_backends.py:33` |
| `server_gui` | Project-local module | `server/model_server.py:175` |
| `test_column_edge_solve` | Project-local module | `tests/test_chat_navigation_rows.py:9` |
| `test_column_frame_fit` | Project-local module | `tests/test_column_maxes.py:27` |
| `test_core_syntax` | Project-local module | `tests/test_stall_regressions.py:7` |
| `test_fim` | Project-local module | `tests/test_code_entry.py:144` |
| `test_fim_editor` | Project-local module | `tests/test_code_entry.py:143` |
| `test_os_frame` | Project-local module | `tests/test_native_frame_contacts.py:3` |
| `test_render_func_integration` | Project-local module | `tests/test_auto_state_params.py:18` |
| `test_row_edge_solve` | Project-local module | `tests/test_chat_navigation_rows.py:8` |
| `test_view_func_selection` | Project-local module | `tests/test_anywhere_write_fallback.py:8` |
| `test_wayland_frame_hint` | Project-local module | `tests/test_wayland_move.py:33` |
| `test_wayland_move` | Project-local module | `tests/test_surface_two_roots.py:30` |
