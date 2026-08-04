# Codex Defer and Resume

Run long-lived commands without model polling, periodically preserve prompt-cache locality, and resume the same Codex task when work completes.

`defer-and-resume` is a service-independent Codex Skill plus a Stop Hook. The agent decides which command represents terminal completion; the implementation only watches that command and wakes the same task when it exits.

> [!IMPORTANT]
> This is an unofficial, experimental Codex extension. Prompt-cache behavior is provider-specific, and process completion is not proof that a wider deployment or business workflow succeeded.

## Why

Long builds, CI runs, deployments, migrations, and artifact jobs often spend most of their time waiting. Repeated model polling wastes tokens. This project moves the wait into a local Python process:

```text
Codex starts a detached command
        ↓
the current turn ends
        ↓
the Stop Hook waits locally without model calls
        ↓
the command writes result.json
        ↓
the Hook resumes the same Codex task
```

While a command is still running, the Hook performs a short cache-keepalive wake every 25 minutes by default. The agent immediately ends that turn and the same registration automatically re-enters local waiting on the continuation turn; no second `start` call is needed. Completion is a one-shot wake. If a user interrupts the waiting turn, the registration is disarmed and is not silently resumed on the next user turn. The interval is a provider-specific heuristic, not an OpenAI guarantee.

## Requirements

- Codex Desktop or another Codex surface that supports Stop Hooks
- Python 3.9 or newer
- macOS or Linux
- The Codex app and task must remain open during the wait

## Install

```bash
git clone https://github.com/zibo-chen/codex-defer-and-resume.git
cd codex-defer-and-resume
python3 install.py
```

The installer:

- copies the Skill to `${CODEX_HOME:-~/.codex}/skills/defer-and-resume`;
- merges one Stop Hook into `${CODEX_HOME:-~/.codex}/hooks.json`;
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

Useful operations:

```bash
# Registrations for the current Codex task
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" list

# Inspect one result
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" inspect --task-dir <path>

# Cancel a running command and its process group
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" cancel --task-dir <path>

# Re-arm an existing worker after a disabled keepalive window expired
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" arm --task-dir <path>

# Stop waiting without cancelling the worker
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" disarm --task-dir <path>
```

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

The Hook uses the `stop_hook_active` field supplied by Codex to distinguish an automatic continuation from a new user turn. A normal continuation keeps the registration armed; a manually interrupted turn disarms it. There is no retry loop for a delivered completion wake.

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

Uninstall refuses to proceed while unacknowledged deferred tasks exist. Use `--force` only when intentionally abandoning them. Runtime evidence is retained unless `--purge-runtime` is specified.

## Security

- Deferred commands receive no interactive input or TTY.
- Full command arguments are not stored in persistent metadata.
- Command arguments may still be visible to the operating system while the command runs; do not place secrets in them.
- Runtime directories are owner-only (`0700`), and state/log files are owner-only (`0600`).
- The Hook injects bounded completion metadata; complete output remains on disk.
- Completion wakes are delivered once per registration. `ack` records that the agent consumed the evidence; it no longer controls wake retries.
- Exit code `124` means timeout, `125` means the worker disappeared, and `130` means cancellation.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/defer-and-resume/scripts/defer.py \
  skills/defer-and-resume/scripts/stop_hook.py install.py uninstall.py
```

The Codex Skill validator can additionally validate `skills/defer-and-resume` when `PyYAML` is available.
