# Overlap gate: implementation plan

Spec: `docs/superpowers/specs/2026-10-28-overlap-gate-design.md`. Executed inline (owner: "pause implementation and make those fixes", 2026-10-28), test first, in worktree `overlap-gate`.

## Tasks

1. **`Gate.writer()`** (`scheduler.py`, class `Gate`). This is a third mode.
   - A writer excludes other writers and `exclusive`, but runs beside `shared` readers (sessions).
   - `exclusive` (verify) still waits for readers and writers. While it waits, it holds back new readers and new writers.
   - `shared` waits only for an active or waiting `exclusive`.

   Tests: a writer enters while a reader holds the gate; two writers never overlap; exclusive waits for a writer and a reader; a waiting exclusive blocks a new reader and a new writer.

2. **Worktree prep leaves the gate** (`scheduler._contract`, the `contract_worktree` call and the leftover check, now at `scheduler.py:637`). It takes `self._refs`, a `threading.Lock` that serialises ATeam's own git ref and worktree creation. Writers take `_refs` too, inside the gate. The rebase paths (`_continue_rebase`, and the `contract_worktree` after a conflict) keep `exclusive`.

   Test: with fake sessions, contract B becomes ready while A's BUILD runs, and B's BUILD starts before A's ends.

3. **Merges are writers, and record their intent commits** (`scheduler._merge`, and the `_refresh` inside it). A context manager, `Scheduler._writing(ctx)`:
   - reads the intent HEAD before;
   - enters `gate.writer()` and `_refs`;
   - after the body, records every commit in `before..HEAD` of the intent repo as the meta key `coordinator-commit:<sha>` (value: plan id) before releasing.

   Test: a verified contract merges while another contract's BUILD runs, and that BUILD's attempt ends `completed`. The recorded keys exist for the merge's commits.

4. **The worker session's intent check** (`runner/run.py`, around line 191). It becomes `intent_tampered(intent, before_head, db) -> bool`:
   - Unchanged HEAD and a clean repo: not tamper.
   - Otherwise it polls every 0.5 s, for up to `INTENT_SETTLE_S` = 60 s, for this condition: the repo is clean, `before` is an ancestor of HEAD, and every commit in `before..HEAD` has a `coordinator-commit:<sha>` key. When the condition holds, it's not tamper.
   - When the poll times out, it is tamper.

   Table-driven tests:
   - unchanged;
   - one recorded commit;
   - two recorded commits;
   - an unrecorded commit;
   - a recorded commit plus an unrecorded one;
   - a non-descendant HEAD;
   - dirty after the session;
   - a commit recorded during the poll.

   The poll interval and timeout are parameters, so the tests run fast.

5. **`max_workers` defaults to 8** (`config.py`), and ATeam's own target config is set to 8.

## Review Focus

- A session whose tamper check passes over a commit a worker made, because it was recorded in the same window as the coordinator's.
- A writer and verify overlapping. Verify's snapshot must see no intent commit during it.
- Deadlock or starvation: the lock order is always gate, then `_refs`. Worktree prep takes `_refs` alone.
- Worktree prep running during another contract's verify: verify's snapshot covers its own contract's store rows, the intent repo and the owner checkout's HEAD, none of which prep writes.
- Crash recovery: a crash between an intent commit and its recording leaves an unrecorded commit. A session running across it reports tamper. Is that acceptable, given the slop-not-adversary rule?
