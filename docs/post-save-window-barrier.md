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
Existing frozen runs and their RAW scores retain their original evidence.

A Windows regression passed on 2026-10-24 with source reference `a6cba4b`.
Attempt `waa-post-save-modal-barrier-20261024-a1` used a new synthetic XLSX chart and an isolated LibreOffice profile.
Ctrl+S displayed the real Confirm File Format dialog while the file retained its old cell value.
The final strict activation returned HTTP 503 after 3.430 seconds, with `phase=confirmation`.
The dialog remained open and the edited value remained absent from disk.

After inspecting the screenshot, the operator explicitly selected the existing Excel format for this neutral test.
The complete save sequence and final activation then succeeded; file inspection confirmed the edited cell and unchanged sentinel.
The operator closed the test document and verified that no owned windows remained.
This operator confirmation is test setup, not behavior added to evaluator preparation.
The evidence directory contains `negative-report.json`, `positive-report-r2.json`, controller logs, scripts, and screenshots.
An initial positive-test helper failed before desktop input because optional `psutil` was absent;
its report remains separate from the dependency-free successful retry.
No benchmark reference answer or task metric was used.
