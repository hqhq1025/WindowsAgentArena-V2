# Background console startup

Status: testing. Created: 2026-10-24. Change ID: `bridge-console-startup-20261024`.
Combined baseline: WAA postrun runtime `1ef6052`.

The VM launcher starts the bridge with `CREATE_NO_WINDOW`. The diagnostic launcher
uses `DETACHED_PROCESS`. Neither setting applies automatically to descendants.
The bridge previously started its console MCP executable, Python command
supervisor, and PowerShell child without a console creation flag. Redirected
standard streams do not prevent Windows from creating a console window.

`guest_mcp_bridge.py` now passes `CREATE_NO_WINDOW` at all three owned launches.
Non-Windows calls retain creation flags zero and the supervisor's existing
`start_new_session` behavior. Job assignment still occurs before the supervisor
releases its command. The fix does not hide GUI applications started by a task.
It does not set global profiles or change a driver's descendant launch flags.

The implementation applies to any configured console MCP or shell command.
No task or application conditions are introduced. It preserves pipe redirection,
timeouts, interruption, output bounds, and GUI-child lifetime after shell success.
The change adds no retries, model calls, or task instructions. It does not claim
a latency or score improvement.

The operator README documents launch behavior and descendant responsibility.
MCP server instructions, tool schemas, descriptions, and evaluated-agent skills
do not change because the public transport contract remains unchanged.

The diagnostic foreground title matching `computer-use.exe` supports a possible
console interference mechanism. This code audit does not prove that every
historical formal run lost foreground focus. Native Windows tests inspect
console allocation, successful startup, and cleanup. Foreground stability is
not an admission requirement. Linux tests cannot validate Windows console behavior.

Validation: 31 bridge tests completed on Linux, with 29 passing and two native
Windows tests skipped. Six shell tests used the existing PowerShell 7.6.7 Linux
runtime. The checks cover runtime identity, errors, output limits, cancellation,
stdio, and successful child survival. An independent code review found no issue.
Native Windows console, startup, and cleanup evidence remain required before admission.
Current frozen packages, running bridges, and VMs remain unchanged.
