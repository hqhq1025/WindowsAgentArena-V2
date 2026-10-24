# Require complete cleanup evidence before reclamation

Status: testing. Created: 2026-10-24.
Change ID: `cleanup-evidence-gate-20261024`.
Baseline: `vm-diagnostic-export-20261024`, Git reference `2c8198c`.

The combined runtime preserved audio stop failures in `audio_capture_error`, but
the final gate checked only driver export. It could therefore report successful
cleanup after failed audio finalization. Container capture exceptions had the
same gap. Docker read failures were saved as text without reporting incomplete
capture to the caller.

`vm.py` now combines driver export, container capture, and audio finalization
status before the final gate. `remote_vm.py` uses the same gate after saving its
binding. All paths still attempt owned-container shutdown. Any capture failure
blocks storage reclamation and makes local and remote cleanup return nonzero.
Successful driver export remains a separate fact.

Container reads use the inspected full Docker ID. The existing capture metadata
now reports completeness. An absent container can reuse only a complete snapshot
bound to this episode, original container ID, current capture directory, and two
existing files. Failed attempts remain recorded. A later successful capture may
resolve a transient capture error. Repeated cleanup cannot erase an audio error.

Validation: all 91 lifecycle tests passed. Eleven new gate tests cover failures,
successful capture, local/remote propagation, and repeat cleanup. Existing tests
now require explicit capture completion and nonzero CLI results on capture failure.
A local combined test used the real audio-aware `stop_binding`, with mocked QMP
and Docker calls: audio stop failed, Docker stop and removal still ran with the
owned ID, the audio error remained, and the final gate raised.

No VM, model, or SSH session was started. Frozen runtimes remain unchanged.
The change adds no model-visible tool, instruction, parameter, or skill; those
contracts need no update. Operator documentation names the unified gate and
recovery rules. No task score or performance improvement is claimed.
