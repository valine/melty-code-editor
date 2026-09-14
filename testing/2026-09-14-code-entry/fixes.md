# Code-entry fixes — 2026-09-14

Implemented in the editable `latent-descent` Melty dependency, used directly by this app. Existing unrelated working-tree edits were preserved.

All nine reported behaviors were addressed:

- Automatic suggestions stop at fully typed symbols; Enter inserts a newline normally. Ctrl+Space still explicitly requests alternatives.
- Keyboard and mouse acceptance replace the entire identifier, including the suffix after the caret.
- Escape retains multiline text focus and dismisses assistance. Single-line fields and search retain their existing exit behavior.
- Tab accepts the selected completion; with no menu, it retains AI acceptance/indentation behavior.
- Scope priority precedes tint/popularity. Live assignments are recognized before a parse arrives.
- The fallback scan excludes strings/comments and foreign async function scopes. The observed `files` leak was partly caused by `label = "files"` being scanned as code. Expression positions exclude statement-only keywords.
- Menus are capped at 202 pixels and kept within the editor, above the file tabs. Automatic import suggestions wait for three characters; Ctrl+Space includes them earlier.
- Acceptance reads the menu's painted cursor, so mouse highlight and Enter agree.
- Signature help uses free space after the current line, or docks at the editor bottom. Escape suppresses it for that call until Ctrl+P explicitly requests it again.

AI ghost text is hidden while the completion menu owns input; accepting the menu cannot also accept hidden AI text. This is covered with a fake provider in real editor-frame tests, without an external model dependency.

Validation:

- 14 new regression cases in `latent-descent/tests/test_code_entry.py` pass, including real draw_text input frames, AI/menu arbitration, signature dismissal, and cached completion pools on 20k-line idle frames.
- A focused combined run of completion-entry, FIM, scope rendering, fold selection, global-hotkey, dropdown, and menu tests passed 79 cases; the subsequently added signature case also passed.
- Live mouse/keyboard checks verified newline entry, Tab acceptance, Escape followed by typing, mouse-highlight acceptance, signature placement/dismissal, and a 20,000-line file's rendering/navigation/selection/completion. The bottom-of-file menu stayed above the tabs.
- CPU-only headless render comparison on the same 20,000-line input, with line numbers and widgets: idle repaints were 2.6–2.7 ms before and 2.6–2.8 ms after; selection frames were 2.5–3.1 ms before and 2.7–3.6 ms after. These are short-run timings, not a GPU/end-to-end latency guarantee. No file-sized scans were added to the steady rendering path.
- A cold fallback completion-pool rebuild on a synthetic 20k-line file measured 7.29 ms before and 9.96 ms after (12-run medians). The extra work excludes strings and classifies live bindings; unchanged popup frames reuse the pool.
- `git diff --check` passed.

Two broader checks still fail and were verified against the original editor code: the tokenizer-oracle comparison on text_editor.py, and `test_ibeam_covers_the_same_rows` (outdated five-field action-tuple unpacking). Neither is introduced by these changes.

Primary implementation: `latent-descent/src/lsd/gl_gui/view/core_views/text_editor.py`, with Escape routing in `melty.py`, transient state in `model/core_model/draw_state.py`, and the popup height setting in `toggles.py`.
