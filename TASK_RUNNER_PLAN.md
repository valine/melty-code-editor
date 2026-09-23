# Task runner: design (2026-09-22, simplified) — implemented 2026-09-22 as `tasks.py`

Implemented as designed, plus a table form per task (`cmd` / `cwd` / `env`), an `error`
field for refused starts, elapsed time in the status, and `request_run` waking the
tiles so a menu pick runs at once. Tests: `tests/test_tasks.py`; screenshot:
`screenshots/tasks_tile.png`.

A task is a named shell command of a project. The runner lists them, runs one in the
project's venv, streams the output into a tile, and can stop it. That is all of v1.

## The pieces (one file, `tasks.py` in the app)

**Tasks are a dict, not a type.** `project_tasks(root) -> {name: command}` reads
`[tool.melty.tasks]` from the project's `pyproject.toml`:

```toml
[tool.melty.tasks]
test = "pytest -q"
lint = "ruff check ."
app  = "python app.py"
```

Plain strings, run with `sh -c`. No argv lists, no cwd, no env, no depends. If there is no
table the dict is empty and the tile says so. (Read through `manifest_data` so an unsaved edit
to the table shows up; a Makefile / package.json provider is a later addition to the same dict.)

**One state class.** Injected into the tile, like the Files tile's state:

```python
@no_save('process', 'thread', 'output', 'running', 'exit')
class TaskState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected = None     # task name (persists)
        self.process = None      # subprocess.Popen while running
        self.thread = None       # the reader thread
        self.output = ''         # what the tile shows, capped at 200 000 chars
        self.running = False
        self.exit = None         # exit code of the last run
```

**Three functions.**

- `start(state, root, command)`: `Popen(['sh', '-c', command], cwd=root, env=env,
  stdout=PIPE, stderr=STDOUT, start_new_session=True, close_fds=False)`. `env` is `os.environ`
  with the project's venv (`analysis_project(root).environment`, when set) prepended to `PATH`
  and put in `VIRTUAL_ENV`, `PYTHONUNBUFFERED=1`, and `PYTHONPATH` / `PYTHONHOME` removed.
  A daemon thread reads chunks, appends them to `state.output`, and calls `request_render()`
  (thread-safe). At EOF it records `exit`, clears `running`, renders once more. Starting while
  running first calls `stop`.
- `stop(state)`: `killpg(SIGTERM)`, `SIGKILL` after 3 s. Called by the Stop button and at exit
  (`atexit`), never by closing the tile.
- `draw_tasks(input_value, draw_state, state: TaskState)`: the tile
  (`@render_func(multi_instance=True, display_name="Tasks", icon="")`, offered by the app
  in `multi_instance_renderers` next to `draw_chat`). Top row: a `draw_dropdown` of the task names,
  a Run button, a Stop button while running, then the status (`running`, `exit 0`, `exit 1`).
  Below: `state.output` in a read-only `draw_text(editable=False, wrap=True)` that follows the
  tail. `draw_state.invalidate()` each frame while running, like every streaming view here.

**Which project.** The one the nearest editor tile has selected (`target_editor` from
`project_tree.py`, moved to a small shared helper), else the selected tab's `project_for`.
The tile shows the project name beside the dropdown; that is the only project UI it has.

**Menu.** `Run → Tasks ▸` lists `project_tasks(...)` of that project, rebuilt each frame like
`projects_menu()`; picking one runs it in the first Tasks tile (or opens the tile view as a nested
`Mode.WINDOW` child if there is none, `open_requested` style). `Run Last Task (Ctrl+Shift+R)`
re-runs `state.selected`.

## Order

1. `project_tasks`, `start`, `stop` + a test: a fixture pyproject, `echo`/`sleep` commands, exit
   code, stop mid-run, venv on `PATH`.
2. `draw_tasks` tile and the menu; smoke bench; agent-desktop check on both backends.

## Later, only if wanted

`file:line` links in the output (the terminal view's link regex + `open_in_editor`), a Makefile
/ package.json provider, a PTY mode for interactive tools, tasks in global search.
