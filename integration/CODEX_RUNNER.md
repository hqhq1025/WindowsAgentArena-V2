# Native Codex Windows runner

Candidate `waa-guest-codex-20261026-r1` adds an explicit `--agent-backend codex` path. It runs official Codex CLI 0.164.0 inside each task's Windows guest. The existing Responses loop remains a separate backend.

The selected configuration binds the guest CLI, PowerShell, driver runtime roots, official instruction files, and guest gateway. Each episode receives a fresh workspace and CODEX_HOME. Only the original instruction, generic completion contract, selected driver instructions, and runtime helpers enter that workspace. Parent project instruction files cause admission to fail.

Both arms use GPT-5.4 with xhigh reasoning and native PowerShell. The task route has 100 completed model responses, a separate 400-request safeguard, four consecutive failures, and a 1800-second episode limit. Codex transport retries and unbounded connection retries are disabled. The shared upstream can have its own bounded retries; the relay setting does not describe those retries.

Host and guest must observe the same relay instance, configuration, ledger, and canonical task budget. PowerShell admission executes the selected binary and records its actual process path. The CLI starts the selected official MCP directly.

Cleanup tracks Windows process identities and observed ancestry. It stops agent runtime processes and commands, waits for the exact supervisor, and retains observed task GUI applications. Unknown surviving processes stop dispatch and retain the VM. Codex's own child-job behavior still requires a real Windows canary.

Evidence includes full CLI stdout/stderr, native session rollouts, gateway counters, process cleanup, and final declarations. Expanded evidence above 256 MiB or incomplete download retains the VM. CLI success and completion declarations are not task scores. The existing evaluator supplies an independent raw result; no Judge runs.

Host validation: 70 integration tests ran successfully, including 12 platform/dependency skips. Official 0.164.0 JSON Schema accepted the generated TOML. Windows driver, shell, lifecycle, and dual-arm task readiness remain subject to real guest canaries.

## Validated revisions

Revision r2 made control JSON independent of the Windows console encoding. Revision r3 added exact system-console-host identity tracking. In the shared neutral Windows guest, r3 passed 25 cleanup tests, including five real process/Job cases. Real Codex runs then completed with both HCU and official CUA, using native PowerShell and screenshot interpretation. Both task documents remained open after the CLI exited. The model gateway counted seven HCU responses and ten CUA responses. These are integration checks, not benchmark scores.

Revision r4 changes host finalization only. Missing output files require a successful guest stat query and recorded original download failure. Native trace metrics recognize the login-policy preflight error and prefer native duration fields. Runner manifests record their terminal execution state. Guest Codex execution, supervision, driver selection, and model-visible instructions are unchanged from the tested r3. The updated host suite ran 78 integration tests with 13 skips, plus 20 artifact-export tests. The missing-file evidence also passed the reclaimer's interface check.
