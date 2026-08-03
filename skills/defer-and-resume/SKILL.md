---
name: defer-and-resume
description: Run or monitor long-running commands without repeated model polling, then resume the same Codex task when background work finishes. Use for lengthy or unpredictable local builds, remote wait commands, CI watchers, deployments, migrations, artifact generation, and any agent-selected non-interactive command whose completion should wake Codex.
---

# Defer and Resume

Use the bundled runner as a service-independent completion primitive. Decide which command represents terminal completion; do not encode CI, build-system, or cloud-provider semantics in this Skill.

## Choose a waiting mode

- Run ordinary foreground commands expected to finish within about ten minutes.
- Use same-task deferral for longer or unpredictable work when retaining the current task is valuable.
- Treat the configured cache-keepalive interval as a heuristic, not a provider guarantee. The default Hook interval is 25 minutes.
- Before a wait likely to outlive prompt caching, write a concise checkpoint containing the objective, workspace, branch or commit, background task directory, completed work, and next actions.
- If the current Codex surface exposes a safe explicit compaction action, compact before a long wait and verify that context usage fell. Do not start a competing app-server or mutate an active task through an unowned connection.

## Start and pause

Register a command that stays alive until the desired condition is terminal:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" start \
  --name "descriptive task name" \
  --cwd "$PWD" \
  --timeout 7200 \
  -- command arg1 arg2
```

Then finish the current turn. Do not poll with model tool calls. Keep Codex Desktop and the current task open so the Stop Hook can wait and resume it.

`--timeout` is optional and limits the command itself. The Hook observation interval is separate.

## Handle Hook prompts

For a `Cache keepalive wake` prompt, do not inspect, poll, or call tools. Immediately finish the turn with the shortest useful response so the Stop Hook continues local waiting.

For a completion prompt:

1. Inspect the result and only the bounded log needed for diagnosis:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" inspect --task-dir <path>
   ```

2. Record required evidence, then acknowledge the wake so it is not retried:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" ack --task-dir <path>
   ```

3. Continue the original task. Remove consumed state when it is no longer needed:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" clean --task-dir <path>
   ```

## Operations

```bash
# Current task registrations
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" list

# One registration
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" status --task-dir <path>

# Stop a running command and its process group
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" cancel --task-dir <path>

# Remove acknowledged state older than seven days
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" gc
```

## Safety

- Start the command immediately in the authorized agent turn. The Stop Hook only observes state.
- Never defer commands requiring interactive input, approval, passwords, or a TTY.
- Do not place secrets in command arguments. Persistent metadata omits full arguments, but the operating system may expose them while the command runs.
- Treat process exit as command completion, not proof that the wider workflow succeeded.
- Override the keepalive interval with `CODEX_DEFER_KEEPALIVE_SECONDS` only when provider evidence justifies it. Leave a safety margin below the measured cache horizon.
- A missing worker produces exit code `125`, a command timeout `124`, and cancellation `130`.
- Do not force-clean unacknowledged state unless recovery is intentionally abandoned.

## Bundled scripts

- `scripts/defer.py`: start, inspect, list, status, cancel, acknowledge, clean, and garbage-collect generic tasks.
- `scripts/stop_hook.py`: wait for current-task registrations, issue periodic cache keepalives, detect stale workers, and retry unacknowledged completion wakes.
