# Freeform Python code-entry test — 2026-09-14

Tested the running app with mouse clicks and injected keyboard events on reserved desktop desk-2, in a new `/tmp/melty_entry_test_20260914.py`. All Python source was entered through the editor UI, in short typing bursts with pauses, navigation and corrections. No editor implementation changes were made. Existing README.md and editor.py modifications predated this test.

The exercise built a pathlib file-summary function, a filtering function, module-level assignments, comments, strings and a list comprehension. This is an exploratory usability list, not an exhaustive regression suite. Priority reflects disruption to typing.

## Confirmed findings

1. **High: Enter swallows the newline even when completion changes nothing.** Type `enabled = True`, pause, press Enter, then type `next_value = 2`. Result: `enabled = Truenext_value = 2`. Reproduced multiple times, including with only `True` in the popup. The exact-match keyword remains selected and Enter accepts it. Adding a trailing space before Enter avoids this. The same general Enter interception also interrupted blank-line entry after the Path import. Prefer allowing newline on an exact match, or avoiding a selected no-op candidate. Evidence: [before](true-before-enter.png), [after](true-after-enter.png).

2. **High: Completing inside a word duplicates its suffix.** In `files = ...`, click between `fi` and `les`, press Ctrl+Space, select `files` with Down, then Enter. Result: `filesles = ...`. Reproduced twice. The operation replaces only the prefix before the caret, retaining `les`. This makes mouse-positioned corrections hazardous. Evidence: [duplicated suffix](duplicated-suffix.png).

3. **High: Escape can silently leave text entry and expose global letter shortcuts.** With no suggestion popup open, Escape followed by Enter and typing stopped adding code. During the attempted `enabled = True` entry, debug overlays appeared; source inspection confirmed bare E toggles the invalidation tracker when no editor owns text focus. A subsequent mouse click restored typing. This is particularly easy to hit when habitually dismissing assistance that has already vanished. The tracker was switched off afterward. Prefer retaining editing focus, or making the mode change unmistakable. This was observed once as a complete sequence; it merits a dedicated focus regression check.

4. **Medium: Tab does not accept the visibly selected completion.** At `for file_path in fi`, the popup selected `files`; pressing Tab inserted whitespace, and typing `:` left `fi    :`. Enter accepts the popup instead. Source inspection confirms Tab is reserved for AI ghost-text acceptance and otherwise reaches indentation. The key division is intentional, but the popup provides no visible acceptance-key cue. Consider an explicit Enter hint and a predictable policy when no ghost text exists.

5. **Medium: A module function outranks the local identifier being edited.** At `fi|les` in `summarize_files`, Ctrl+Space listed module-level `filter_files` first and the local `files` second. The local name is also used in the loop below. Context should favor that local, especially when repairing an existing identifier. Evidence: [local ranking](local-ranking.png).

6. **Medium: Out-of-scope locals appear in module-level completion.** At a new module-level `scope_check = fi`, the menu offered `files`, although it exists only inside `summarize_files`. Rechecked after repairing earlier syntax errors. No module-level `files` binding exists in the final scratch file. The result list also offers `finally` in this expression position. Evidence: [scope and popup](scope-and-popup.png).

7. **Medium: The popup consumes too much of the working area.** A two-character `fi` prefix produces roughly fifteen visible rows, including unrelated auto-imports such as FieldMeta, FileAnalysis and FileSelectorState. Near the bottom it extends across file tabs and to the window edge; farther up it hides numerous lines of code. A smaller default list with expandable import results would reduce obstruction. Evidence: [scope and popup](scope-and-popup.png), [local ranking](local-ranking.png).

8. **Low/medium: Hover highlight and Enter acceptance can disagree.** With the pointer resting where the popup appeared, FileExistsError was visibly highlighted, but Enter inserted the first candidate, `filter_files`. This may be separate hover and keyboard-selection states rather than selection changing; either way, the visible feedback made acceptance ambiguous. Observed once using Ctrl+Space at module-level `result = fil`; needs a focused repeat before treating it as a deterministic bug.

9. **Low: Parameter help obscures nearby source.** Typing `files = list(root.glob(` displayed `glob(pattern: str, case_sensitive)` above the caret, over the function definition on the preceding line. Helpful content, inconvenient placement when checking the surrounding code. The nested `Path(` call also showed a hint. Consider placement that avoids nearby code, and convenient dismissal that preserves text focus.

## Behaviors that worked and limits

- Completion stayed out of the tested comment and unfinished quoted string.
- Escape dismissed an open completion popup; Left/Right at that dismissed site did not immediately reopen it.
- Indentation after a colon and one-level Backspace dedentation worked in the function/loop exercise.
- Clicking the color swatch opened a color picker; clicking a boolean placed the text caret in this test. No reproducible accidental literal mutation was established.
- AI/FIM ghost text never appeared during this session. Its acceptance, latency and interference remain untested. The top-right `none` badge is a source-tracking indicator, not an AI setting; an early interpretation of it was corrected.
- Red diagnostics appeared while code was incomplete, but their timing was not measured. No latency or dropped-key claim is based on the FPS display or tool injection acknowledgments.
- Fix the Enter/no-op and word-suffix behaviors first, then Escape focus handling, scope/ranking, and popup size/key guidance.
