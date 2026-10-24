# Driver stderr persistence

Status: testing. Created: 2026-10-24. Baseline: the bridge introduced by PR #5.
Change ID: `bridge-stderr-diagnostics-20261024`.

Driver stderr previously entered a 50-item memory deque. Session stop removed
the session and its only diagnostic copy. Redirecting the bridge process output
to `bridge.log` did not capture that separate pipe.

This change saves a bounded prefix per session before the session is removed.
Fixed-size reads also prevent a line without a newline from growing without bound.
The mechanism applies to any configured stdio MCP driver.

Changed files are `guest_mcp_bridge.py`, `tests/test_stderr_diagnostics.py`,
and `README-bridge.md`. Each log retains at most 1 MiB after credential redaction.
The bridge continues draining excess output. Status records expose lost bytes,
capture failures, EOF, and session closure. Session directories are not deleted.
Operators must archive them before deleting the guest storage.

MCP/server instructions, tool schemas, parameter descriptions, and evaluated-agent
skills remain unchanged because this is an operator diagnostic path. The operator
README documents its limits. No application logic, retries, model calls, or task
instructions are added. No CUA binary or behavior changes.

The change adds bounded local disk writes and one status update per second during
active stderr. It adds no model tokens or network traffic. Unknown or encoded
secrets can remain in trusted driver output; operator access restrictions still apply.
An abrupt bridge exit can leave status incomplete. Capacity limits preserve the
prefix, so later diagnostics can be discarded and are marked as truncated.

Validation: 27 tests ran, with 21 passing and six PowerShell tests skipped because
no PowerShell executable was selected. After narrowing redaction to credential
environment names, all six diagnostic tests passed again.
Tests use a real local fake-driver subprocess for normal stop, timeout,
oversized output without newlines, and environment credential redaction.
Unit tests cover all split points in a secret, failed writes, failed status updates,
and failed driver startup. No VM or model was started. Windows deployment and
cleanup export remain separate integration checks.
