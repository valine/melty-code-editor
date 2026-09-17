# Dependency onboarding fixes

2026-09-15. Follow-up to [the original manual review](NOTES.md).

## Implemented

- Function Run uses the owning project's Python when its venv differs from the
  editor's. Import failures retain their traceback instead of becoming a later
  undefined-name error.
- Run → Run file / F5 executes the current buffer, including top-level statements
  and the main guard, with output and interactive stdin in a console.
- Missing packages in explicit imports get install-only fixes. Successful
  installation does not duplicate an existing import.
- Initial diagnostics run on open, including restored parser caches. Throttled
  refreshes schedule a wake, so they do not need another keystroke. Removing an
  import invalidates cross-block name bindings.
- Hover and Alt+Enter expose fixes beside the flagged code. Red highlighting is
  bounded to the source line's text.
- Installation retains status, reports environment creation and live uv output,
  identifies the target project/venv, and offers Save dependency in project and
  Dismiss. Torch saving preserves the installed build and backend index.
- New-file creation transfers focus to the text buffer.

## Real UI verification

Created `/tmp/torch-ux-fixed/main.py` through Ctrl+N, typed Torch code immediately,
installed real Torch into a newly created project venv, inserted its import, and
saved the dependency through the follow-up action. Function Run printed a tensor.
F5 ran top-level print statements and printed a tensor plus Torch's version.
The installed build was `2.14.0+cu130`.

- [New-file focus and successful function output](fixed-new-file-focus.png)
- [Run File output in the editor](fixed-run-file.png)
- [Fresh-open diagnostic and nearby TOML install offer](fixed-startup-lint.png)

The final fresh-open check used `/tmp/torch-ux-import-first/main.py`, with no
venv. The warning appeared without editing. Hover opened the offer with TOML's
`torch>=2.10` taking priority over requirements.txt's pin.

## Regression coverage and limits

Targeted regression suite: 241 cases, covering dependency parsing on downloaded
repos, manifest precedence, environments, installation/import handling, pending
source execution, project-only packages, interactive input, diagnostic startup
and throttled refresh, deleted imports, and exact Torch declaration saving.

Cross-environment function runs currently provide console output rather than
inline tensor visualization; their parameters must be JSON values. Full-file
execution handles ordinary script behavior. Function-only execution loads
selected top-level declarations, so conditional declarations and nested methods
remain limitations of that path.

Progress reflects installer output, not a guaranteed percentage or byte count.
Cancellation and interrupted-network recovery were not manually exercised. A
Torch warning about missing NumPy remains visible as runtime output. The
live-store-owner test module requires Torch in the editor environment and was
not included in the passing suite. Final status-panel positioning and console
close-state synchronization were covered by code review rather than another
full download/install cycle.
