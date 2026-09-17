# False Torch size keyword diagnostic

Reproduced on an agent desktop through the Hyprland MCP server, 2026-09-15.
The editor displayed `rand() got an unexpected keyword argument 'size'` for
`torch.rand(size=(10,10,10))`. Running the function printed
`torch.Size([10, 10, 10])`.

The isolated fixture loads the source module before opening it in the editor,
so live-callable signature analysis can inspect Torch. The fallback parsed
Torch's `rand(*size, ...)` docstring as a precise Python signature, although
Torch also accepts the size keyword. Native variadic docstrings now cause the
checker to abstain; real inspectable Python signatures are still checked.

35 targeted tests passed, including real Torch execution, full-file and span
lint, fixed native signatures, and rejection of invalid Python keywords.
The corrected test launch showed no error and successfully ran the function.
The already-open process retained the old checker; live replacement was not
verified. No claim that restarting repairs diagnostics independently of the fix.

[Before](before.png) · [After](after.png)
