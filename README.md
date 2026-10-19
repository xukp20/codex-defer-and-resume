# Codex Defer and Resume

Run long-lived commands as independent workers with persistent logs and results, with optional same-task completion wakes.

`defer-and-resume` is a service-independent Codex Skill plus lifecycle Hooks. The agent decides which command represents terminal completion. With Hooks armed, the implementation watches that command and resumes the model when it exits; `start --no-hook` runs it independently for later inspection.

> [!IMPORTANT]
> This is an unofficial, experimental Codex extension. Prompt-cache behavior is provider-specific, and process completion is not proof that a wider deployment or business workflow succeeded.

## Why

Prefer Codex's native background commands for ordinary background execution:
`exec_command` can return a session ID and `write_stdin` can retrieve subsequent
output. This runner is useful when you need an independent worker, a named
task directory, and persistent logs and result files.

Use `start --no-hook` to keep the conversation available while a worker runs.
Query it with `list`, `status`, or `inspect` during an active turn. It does not
automatically wake an idle model or make it periodically check the worker.

Use the default Hook-enabled mode when automatic continuation is needed. Long
builds, CI runs, deployments, migrations, and artifact jobs often spend most
of their time waiting. This mode moves the wait into a local Python process:

```text
Codex starts a detached command
        ↓
the assistant finishes its response
        ↓
the synchronous Stop Hook waits, keeping the Codex turn active
        ↓
the command writes result.json
        ↓
the Hook continues the model in the same Codex task
```

While a command is still running, the Stop Hook performs a short cache-keepalive wake every 25 minutes by default. Unless an actionable worker message has already arrived, the agent immediately finishes its response and the same registration automatically re-enters local waiting on the continuation; no second `start` call is needed. Existing blockers should be handled instead of returning to waiting. Completion is a one-shot wake. The interval is a provider-specific heuristic, not an OpenAI guarantee.

Steer sent during a synchronous Stop Hook can remain queued until that Hook
returns. `UserPromptSubmit` disarms waits when Codex consumes a user prompt;
it is not an immediate message-arrival callback. See
[Messages and interruptions](#messages-and-interruptions) before using this
mode in a conversation that needs prompt interaction.

## Requirements

- Codex Desktop or another Codex surface that supports Stop Hooks
- Python 3.9 or newer
- Windows, macOS, or Linux
- The Codex app and task must remain open for automatic same-task wakeups

## Install

```bash
git clone https://github.com/xukp20/codex-defer-and-resume.git
cd codex-defer-and-resume
python3 install.py
```

On Windows PowerShell, run `python install.py` instead. The installer writes
both the portable Hook command and Codex's Windows-specific `commandWindows`
override, using the Python interpreter that performed the installation.

The installer:

- copies the Skill to `${CODEX_HOME:-~/.codex}/skills/defer-and-resume`;
- merges one Stop Hook and one `UserPromptSubmit` Hook into `${CODEX_HOME:-~/.codex}/hooks.json`;
- preserves unrelated hooks;
- replaces older `defer-and-resume` hook entries instead of duplicating them;
- backs up an existing installed Skill and Hook configuration before replacement.

Start a new Codex task after installation so the Skill is discovered reliably.

To install into a non-default Codex home:

```bash
python3 install.py --codex-home /path/to/codex-home
```

## Use

Ask Codex naturally:

```text
Use defer-and-resume for this build and continue when it finishes.
```

The agent can register any non-interactive command:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" start \
  --name "release build" \
  --cwd "$PWD" \
  --timeout 7200 \
  -- ./gradlew assembleRelease
```

On Windows, use the same argv-oriented interface from PowerShell:

```powershell
$defer = Join-Path $env:CODEX_HOME 'skills\defer-and-resume\scripts\defer.py'
python $defer start --name 'release build' --cwd $PWD.Path --timeout 7200 -- cmd.exe /d /c gradlew.bat assembleRelease
```

The Windows worker is launched detached, in a new process group, and with a
job breakaway flag. This keeps it independent from the invoking terminal or
Codex tool process. Closing Codex still prevents the Stop Hook from waking the
task automatically; reopen the same task and re-arm the registration if that
happens.

Useful operations:

```bash
# Registrations for the current Codex task
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" list

# Inspect one result
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" inspect --task-dir <path>

# Cancel a running command and its process tree
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" cancel --task-dir <path>

# Re-arm an existing worker after a disabled keepalive window expired
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" arm --task-dir <path>

# Stop waiting without cancelling the worker
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" disarm --task-dir <path>
```

Acknowledging a completed task also performs a best-effort garbage collection
of completed task state older than seven days. The acknowledged task is kept
because its fresh acknowledgement timestamp starts a new retention window.

## Messages and interruptions

Stopping a waiting turn ends its Stop Hook, while the independent worker
continues. A subsequent consumed user prompt disarms the wait through
`UserPromptSubmit`; the next non-continuation Stop Hook also disarms stale
waiting state as a fallback. Interruption may clear pending steer, so resend
any message that was not processed.

To preserve queued input, run `defer.py disarm --task-dir <path>` from a
separate terminal or another active context, using the waiting task's
`CODEX_HOME` and any `CODEX_DEFER_RUNTIME` override. The waiting Hook observes the
disarmed state and returns without cancelling the worker. If multiple armed
registrations keep the conversation waiting, disarm all of them. Query results
afterward with `status` or `inspect`, or explicitly `arm` to wait again. Use
`cancel` to stop the worker itself.

## Configuration

Environment variables are read by the runner or Stop Hook:

| Variable | Default | Purpose |
|---|---:|---|
| `CODEX_HOME` | `~/.codex` | Codex configuration and runtime root |
| `CODEX_DEFER_RUNTIME` | `$CODEX_HOME/runtime/defer-and-resume` | Override task-state storage |
| `CODEX_DEFER_KEEPALIVE_ENABLED` | `true` | Emit continuation turns while work is still running; set to `false` to wait once and disarm when the local Hook window expires |
| `CODEX_DEFER_KEEPALIVE_SECONDS` | `1500` | Maximum local wait before a cache-keepalive wake |
| `CODEX_DEFER_POLL_SECONDS` | `0.5` | Local filesystem polling interval |
| `CODEX_DEFER_HOOK_MAX_WAIT` | `CODEX_DEFER_KEEPALIVE_SECONDS` | Backward-compatible alias for the local Hook wait window |

Keep the cache-keepalive interval below the cache horizon measured for your provider. Lower values cause more model calls; higher values increase the risk of a cold prompt.

The Stop Hook uses the `stop_hook_active` field supplied by Codex to distinguish an automatic continuation from a new user turn. `UserPromptSubmit` disarms active registrations when Codex consumes user input, subject to the synchronous-wait limitation above. A normal continuation keeps the registration armed. Stopping the turn does not cancel the worker; `cancel` explicitly stops it. There is no retry loop for a delivered completion wake.

## Update

```bash
git pull --ff-only
python3 install.py
```

The install operation is idempotent and does not add duplicate Hook entries.

## Uninstall

```bash
python3 uninstall.py
```

Uninstall refuses to proceed while incomplete deferred tasks exist. Completed
tasks do not need an acknowledgement before uninstall. Use `--force` only when
intentionally abandoning incomplete work. Runtime evidence is retained unless
`--purge-runtime` is specified.

## Security

- Deferred commands receive no interactive input or TTY.
- Full command arguments are not stored in persistent metadata.
- Command arguments may still be visible to the operating system while the command runs; do not place secrets in them.
- On macOS and Linux, runtime directories are owner-only (`0700`) and
  state/log files are owner-only (`0600`). On Windows, files inherit the
  current user's profile ACLs.
- The Hook injects bounded completion metadata; complete output remains on disk.
- Completion wakes are emitted once per registration and immediately disarm
  the wait. `ack` is an optional evidence marker; it does not control wake
  retries or whether a completed task can be cleaned. Calling `ack` also runs
  the default seven-day completed-state garbage collection.
- Exit code `124` means timeout, `125` means the worker disappeared, and `130` means cancellation.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/defer-and-resume/scripts/defer.py \
  skills/defer-and-resume/scripts/stop_hook.py \
  skills/defer-and-resume/scripts/user_prompt_hook.py \
  install.py uninstall.py
```

The interruption regression test kills an isolated Stop Hook process, invokes
the user-prompt Hook, and checks that the original worker completes naturally
with its log and result intact. It does not simulate Desktop's UI or assert
that steer invokes the user-prompt Hook while Stop is waiting.

The Codex Skill validator can additionally validate `skills/defer-and-resume` when `PyYAML` is available.

## Intervention and bounded waiting

Use `start --review-after 1800` to request a review after 30 minutes of armed
waiting, without killing the command. Keepalives do not reset this deadline;
explicit `arm` starts a fresh review period. This differs from `--timeout`,
which limits command execution. Both are optional.

A worker can publish an explicit blocker:

```bash
python3 "$HOME/.codex/skills/defer-and-resume/scripts/defer.py" attention \
  --task-dir /path/to/task --reason "Upstream failed; recovery needed" \
  --evidence-path /path/to/receipt.json
```

Attention events are immutable and consumed once. The Hook combines a
simultaneously observed completion and attention in one wake, disarms waiting,
and does not kill the worker. New events on a disarmed task wait for `arm`.
Avoid secrets in notification text and paths. This is an explicit signal API,
not automatic agent-mailbox or workflow-DAG monitoring.

Use `start --no-hook` for background-only commands: no automatic waiting or
wakeups until explicitly armed. This preserves the locally deployed behavior.

For dependent workflows, wait for the current stage's success, failure or need
for intervention, not for all downstream result files. A completed failure is
not permission to run the next success-dependent stage. Existing actionable
worker messages take precedence over a routine keepalive's no-poll guidance.
