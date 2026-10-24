# Reject blocked document preparation after saving

The save-only evaluator preparation now ends with strict activation of the same document window.
This uses the existing bounded activation controller; it adds no button-selection or application-specific save helper.
It does not reopen a missing document after the save request.

A save command's zero exit code proves that its input script finished.
It does not prove that the application accepted its format or wrote the edited document.
For example, a foreign-format confirmation can block the document after Ctrl+S returns.
If the final activation fails, `DesktopEnv.evaluate()` propagates that preparation error before calling any result getter or metric.
The harness therefore records an evaluator error with no score, instead of grading an old file as if preparation had completed.
The model's timeout or completion status remains separate from evaluator preparation.

The final activation verifies the original document's window readiness at that point.
It does not prove `fsync`, complete persistence, file contents, or task success.
It does not choose a file format or dismiss an unknown dialog.
The original task metric remains responsible for judging the file after preparation succeeds.

## Scope and validation

The change applies to the same 38 save-only file configurations listed in
[the saved-file audit](saved-file-postconfig.md): 22 Calc tasks and 16 Writer tasks.
Each receives one final `activate_window` step with its original title and `strict: true`.
The final step has no `open_file_if_missing` option.
The other 103 selected configurations retain their existing preparation.
Task instructions, initial setup, expected outputs, and metrics remain unchanged.

On 2026-10-24, 31 host tests passed.
Tests exercise the actual `DesktopEnv.evaluate()` boundary with simulated controller responses:
a blocked or unknown final window state prevents both the file getter and metric from running;
a ready document reaches the unchanged metric only after the save and final activation.
The full 141-task configuration audit checks all 38 final barriers.
These tests do not replace a Windows modal-dialog regression, which is pending.
Existing frozen runs and their RAW scores retain their original evidence.
