# Export driver diagnostics before VM shutdown

Status: testing. Created: 2026-10-24. Baseline: upstream lifecycle PR #6.
Change ID: `vm-diagnostic-export-20261024`.
Runtime dependency: `bridge-stderr-diagnostics-20261024`, based on upstream PR #5.

The existing metadata archiver copies the remote episode directory and excludes
VM storage. Driver logs inside Windows therefore need an explicit export before
shutdown. Container logs do not contain the driver's separate stderr pipe.

`driver_diagnostics.py` reads the logger's fixed directory through the existing
guest controller. UUIDv4 session IDs and two fixed filenames bound the file scope.
File limits, a session-count limit, and a shared request deadline bound the export.
The host reads each written file back and preserves capture status separately
from export completeness. No content hashes or model-visible tools are added.

`vm.py` calls this exporter before owned-container cleanup. `remote_vm.py`
propagates an incomplete export after saving the binding. Failed export does not
prevent container shutdown; it blocks a successful cleanup report and storage
reclamation. Writable storage remains the recovery source. Each attempt has its
own directory. An absent guest can reuse a complete prior local export.

The mechanism applies to all configured MCP drivers. It does not change CUA,
HCU, model requests, task instructions, evaluator behavior, or application state.
Server instructions, tool parameters, and benchmark skills need no update because
the export stays in the operator lifecycle. The operator README documents the
dependency, limits, return status, and recovery boundary.

Validation: all 74 lifecycle tests passed locally. Twelve new tests execute the
actual guest collection script against local files and use fake controller and
Docker calls. They cover complete and empty logs, no sessions, missing directory,
truncation, write-error metadata, active sessions, redirects, oversized files,
inventory changes, timeouts, owner mismatch, cleanup order, repeat cleanup, and
local/remote failure propagation. The twelve focused tests passed again after
the final local-path check. A combined local test ran the new logger with a real
fake stdio process, stopped it, and exported its files using the actual guest
script. The stage and `elapsed=1.25` remained readable; the token was redacted.
No VM, SSH session, or model was started.

Remaining deployment check: combine the two candidates in a fresh runtime, then
confirm Windows path behavior and guest-to-host export before episode archival.
Frozen final2 and final3 artifacts remain unchanged. No measured latency or task
score improvement is claimed.
