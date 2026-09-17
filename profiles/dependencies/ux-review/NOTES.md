# Manual Torch onboarding UX review

2026-09-15. Real editor, agent desktop, actual keyboard/mouse input. No production
code changes during this review. Initial window 2399×1605; second session 1280×800.
Created empty test directories with the shell, then created and edited all Python
and dependency files through File → New / Ctrl+N. Used a real Torch installation,
not the offline test wheel. Existing editor tabs/settings were present.

## Outcome

**Zero setup → installed Torch and inserted import works. Zero setup → running
code inside the editor is blocked.** The project's Python runs the same function
successfully, while the editor's function Run reports `NameError: name 'torch' is
not defined` with `import torch` visibly present.

[Screenshot of the Run blocker](15-run-blocker.png). Reproduced in both editor
sessions, including after normal save/close and reopening the saved file.

Test projects remain at `/tmp/torch-ux-empty` (working venv and saved Torch TOML)
and `/tmp/torch-ux-import-first` (no venv; manually entered declarations).

## Walkthrough and observations

| Step / case | Observed behavior | UX assessment |
| --- | --- | --- |
| Ctrl+N → `/tmp/torch-ux-empty/main.py` → Enter → type `torch` | File opens, but initial typing does not enter the buffer. Clicking the text area before typing works. | New-file flow needs to transfer keyboard focus into the buffer. Path field starts beside the last opened tab, which can be an unrelated project. |
| Type `torch`, then `.rand(2)` | Red gutter warning visible at the next one-second observation. No install suggestion opens automatically. Hovering the name does nothing. | Warning is discoverable; the actionable fix is not. This was observational timing, not a frame-level latency benchmark. |
| Click gutter warning | Entire line turns red; popup says undefined name, `Known dependencies: torch`, and **Create venv, install torch + import torch**. | Button describes the combined action well. Popup is below the line but aligned to the editor's far right: roughly 1,400 pixels between gutter and popup on the wide window. Red fill spans almost the entire screen for one missing name. See [offer](03-install-offer.png). |
| Click install | Creates `.venv`; runs uv with `--torch-backend auto`. Displays **Installing…** twice. | No phase, package/download progress, size, cancel control, or selected backend explanation. See [progress](04-install-progress.png). |
| Continue editing / switch files while installing | Editing remained responsive. Popup disappeared when editing elsewhere; no persistent progress indicator was visible. Import was present when returning to the original file. | Background work is good; losing the only visible job status is confusing. |
| Installation finishes | Actual Torch `2.14.0+cu130` installed, about 20 seconds in this run. `.venv` uses about 5.2 GB. Missing-name warning clears and import appears. | Good one-click result. Timing includes this machine's network/cache conditions and should not be generalized. No manifest is created by inline installation. See [installed](06-installed.png). |
| Run code | Bare top-level expression has no obvious Run File action. Created `sample()` and clicked its gutter play button. It highlights the call red and reports `NameError: name 'torch' is not defined`. | Highest-priority blocker. User just installed/imported Torch but Run still fails. Error is again at the far right. |
| Verify actual project runtime | Closed editor to flush deferred saves, then ran the saved `sample()` using `.venv/bin/python` and `runpy`. Tensor output and version printed successfully. | Confirms install is working; editor execution is the broken handoff. Torch also warns that NumPy is absent. See [runtime output](venv-run.txt). |
| Save dependency declaration | Added test folder through Projects → Add Folder, selected its shortcut, opened Dependencies, clicked Review dependencies, then Configure PyTorch. | Works, but many disconnected steps after the inline install. Before selecting the shortcut, sidebar still described a different project/environment. |
| Configure PyTorch | One click saved the exact installed build and CUDA index/source in `pyproject.toml`. | Useful action and explanation. Label **Save installed build** would describe its immediate action better than **Configure**, which sounds like it will open a form. See [review](08-dependency-review.png). |
| New empty project, write `import torch` first | No missing-package diagnostic or install action, despite no venv and no manifests. | Common tutorial/copy-paste path loses all onboarding assistance. See [import-first](09-missing-import-no-venv.png). |
| Typo `torhc.rand(2)` | Undefined-name message, no guessed package-install button. | Correct conservative behavior. See [typo](10-typo.png). |
| New file in installed project, write `torch.rand(2)` | Popup offers **import torch**, source **Project environment**. Clicking adds the import. | Existing-install branch works manually. See [import only](11-import-only.png). |
| Replace import-first buffer with bare `torch.rand(2)` | At one point no missing-name warning appeared after replacing the old imports, even after more gutter clicks. | Diagnostic freshness is inconsistent. Root cause not established in this review. See [missing warning](12-requirements-offer.png). |
| Reopen that bare-use file in a new editor process | Initially no missing-name warning. Typing a space caused the warning and install offer to appear. | Diagnostics should run on file open, without requiring a token edit. Restart was a separate test, not a proposed fix. |
| Enter `torch==2.14.0` in requirements.txt through the editor | After reopening/editing the code, proposal uses `requirements.txt: torch==2.14.0`. | Priority works. See [requirements](13-reopened-requirements.png). |
| Add TOML `project.dependencies = ['torch>=2.10']` while requirements still pins 2.14.0 | Returning to code and opening warning shows `pyproject.toml: torch>=2.10`, including the unsaved manifest edit. | TOML precedence and pending-buffer reads work. Did not install a second Torch version. See [TOML](14-toml-priority.png). |

## Recommended order of improvements

1. **Make Run use the owning project's interpreter/environment.** Preserve the
   actual import exception instead of reducing an import failure to a later
   NameError. Source inspection shows the function runner executes imports in
   the editor process and suppresses import exceptions; this is a likely cause,
   not a separately instrumented diagnosis.
2. **Offer installation on unresolved `import torch` too**, and reliably refresh
   diagnostics on file open and after imports change.
3. **Anchor fixes near the name or gutter**, support hover/keyboard discovery,
   and keep the highlighted error span small.
4. **Keep installation status visible outside the popup** with create-env,
   resolve/download/install phases, backend and a useful failure/retry path.
   Remove the duplicate Installing label.
5. **Connect installation to declaration**: after success, offer saving the
   installed build/source directly, using the same Configure PyTorch action.
   Avoid sending the user through project marking and full dependency review.
6. **Focus new files immediately** and distinguish the active file's environment
   from the independently selected sidebar project.

## Limits

No network-failure/cancellation or alternate CUDA-build exercise this time. No
claim of a working end-to-end Run experience inside the app. The successful
runtime verification used the shell after normal editor close; file creation,
typing, install, import, dependency configuration, and attempted Run used the UI.
No production fixes were mixed into the observation session.
