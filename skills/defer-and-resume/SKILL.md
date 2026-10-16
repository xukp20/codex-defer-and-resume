---
name: defer-and-resume
description: Run or monitor long-running commands in the background, with optional same-task completion wakes. Use for lengthy or unpredictable builds, remote commands, CI watchers, migrations, and artifact generation; supports background-only work that lets the current turn end immediately.
---

# Defer and Resume

Use the bundled runner as a service-independent completion primitive. Decide which command represents terminal completion; do not encode CI, build-system, or cloud-provider semantics in this Skill.

## Choose a waiting mode

- Run ordinary foreground commands expected to finish within about ten minutes.
- Use same-task deferral for longer or unpredictable work when retaining the current task is valuable.
- Use `start --no-hook` when the command should run independently while the conversation continues. It saves the same logs/results but does not arm Stop Hook waiting, keepalive, or completion wakes for that task.
- Treat the configured cache-keepalive interval as a heuristic, not a provider guarantee. The default Hook interval is 25 minutes and keepalive is enabled by default.
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

On Windows PowerShell, resolve the same script from `$env:CODEX_HOME` and run
it with `python`. The runner accepts an argv array on every platform; use
`cmd.exe /d /c` only when a Windows batch file or shell syntax requires it.

With the default Hook-enabled start, finish the current turn. Do not poll with model tool calls. Keep Codex Desktop and the current task open so the Stop Hook can wait and resume it.

For background-only execution, add `--no-hook` before `--`:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" start \
  --no-hook --name "background backup" --timeout 7200 \
  -- command arg1 arg2
```

Continue independent work or end the turn normally; this task will not automatically wake Codex.
Inspect its status/results when needed, or explicitly `arm --task-dir <path>`
to enable Hook waiting later. Other armed tasks in the same conversation can
still make the Stop Hook wait; `--no-hook` only applies to the new task.

`--timeout` limits the command itself. Optional `--review-after SECONDS` instead
sets a deadline for this armed wait: it survives keepalive continuations and
wakes Codex for review without cancelling the command. Explicit `arm` starts a
new review period. Use this when an external dependency may stall; lack of log
output alone is not evidence that a long-running command is stuck.

## Completion and intervention contracts

Wait on the current producer, not on dependent stages that cannot run until it
succeeds. A custom watcher must return on failure or need for intervention as
well as success. Start dependent stages only after checking the relevant
attempt's outcome. Publish terminal receipts atomically and do not amend them
after consumers pin them; put later commentary in separate files.

A cooperating worker can request intervention while the command remains alive:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" attention \
  --task-dir <path> --reason "Upstream failed; dependent work cannot start" \
  --evidence-path /absolute/path/to/receipt.json
```

The Hook consumes each event once, wakes the lead, and disarms the wait without
killing the command. Review the evidence, resolve the issue, then explicitly
`arm` if continued waiting is appropriate. Signals do not arm a disarmed task;
new events remain pending until an explicit re-arm. The tool does not inspect
agent mailboxes or infer dependency failure from arbitrary logs. Do not put
secrets in event reasons or evidence paths.

## Handle Hook prompts

For a `Cache keepalive wake` with no actionable new information, do not inspect logs or poll status; immediately finish the turn with the shortest useful response. If an explicit failure, blocker, or request for a decision has already arrived from a worker, handle it instead of mechanically returning to waiting; disarm the obsolete wait before recovery. The same registration remains armed and the Stop Hook automatically re-enters local waiting on the continuation turn; do not call `start` again.

For an attention or review-deadline wake, the wait is already disarmed and the command may still be running. Handle the reported issue; do not start a duplicate worker or assume cancellation.

For a completion prompt:

1. Inspect the result and only the bounded log needed for diagnosis:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" inspect --task-dir <path>
   ```

2. Record the required evidence and continue the original task. The Hook has
   already disarmed this one-shot registration, so no acknowledgement is
   needed to prevent another wake.

   `ack` remains available when an external audit needs an explicit evidence
   marker, but it is optional:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" ack --task-dir <path>
   ```

   `ack` also performs a best-effort garbage collection of completed task
   state older than seven days. The acknowledged task is retained because
   its new ack timestamp starts a fresh retention window.

3. Remove consumed state when it is no longer needed. Completed tasks can be
   cleaned directly, with or without `ack`:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" clean --task-dir <path>
   ```

When a user sends a new message or interrupts a waiting turn, the synchronous
`UserPromptSubmit` Hook disarms the registration before the new prompt is
processed. It does not cancel the worker. The Stop Hook also disarms stale
`waiting` state on the next non-continuation invocation as a fallback.

## Operations

```bash
# Current task registrations
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" list

# One registration
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" status --task-dir <path>

# Stop a running command and its process tree
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" cancel --task-dir <path>

# Re-arm a worker after an expired or interrupted wait
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" arm --task-dir <path>

# Disarm waiting without cancelling the worker
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" disarm --task-dir <path>

# Remove completed state older than seven days
python3 "${CODEX_HOME:-$HOME/.codex}/skills/defer-and-resume/scripts/defer.py" gc
```

## Safety

- Start the command immediately in the authorized agent turn. The Stop Hook only observes state.
- Never defer commands requiring interactive input, approval, passwords, or a TTY.
- Do not place secrets in command arguments. Persistent metadata omits full arguments, but the operating system may expose them while the command runs.
- Treat process exit as command completion, not proof that the wider workflow succeeded.
- The runner accepts an argv array and executes it directly; it is not Bash-only. Use an explicit `bash -lc '...'` argv when shell syntax is genuinely required.
- A user interruption disarms the wait registration but does not cancel the worker. Run `arm` to wait again or `cancel` to stop it. For `--no-hook` tasks, only arm when automatic waiting/wakeup is desired.
- Override the keepalive interval with `CODEX_DEFER_KEEPALIVE_SECONDS` only when provider evidence justifies it. Leave a safety margin below the measured cache horizon.
- A missing worker produces exit code `125`, a command timeout `124`, and cancellation `130`.
- Do not force-clean incomplete state unless recovery is intentionally abandoned.
- On Windows, the detached worker survives the invoking terminal or tool
  process. Codex Desktop and the task must still remain open for the Stop Hook
  to wake the model automatically; after an app restart, inspect and re-arm
  the registration from the same task.

## Bundled scripts

- `scripts/defer.py`: start, inspect, list, status, cancel, acknowledge, clean, and garbage-collect generic tasks.
- `scripts/stop_hook.py`: wait for current-task registrations, issue periodic cache keepalives, detect stale workers, and deliver one-shot completion wakes.
- `scripts/user_prompt_hook.py`: disarm active waits when a new user prompt arrives, without cancelling workers.
