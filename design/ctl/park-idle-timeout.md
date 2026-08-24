# Park idle timeout

Auto-release a keep-alive park after a period of idleness, so a forgotten
`inspect ctl process release` doesn't leak the process forever. Resolves the
"Forgotten release" failure mode named in
[control-channel.md](control-channel.md) ("Failure modes worth naming");
tracked as [meridianlabs-ai/inspect_ai#227](https://github.com/meridianlabs-ai/inspect_ai/issues/227),
where this design was proposed and accepted.

## Problem

A run launched with `--ctl-server=keep` parks its process after the eval so
the control surface stays available (and, with `ctl task add`, so tasks can
be added later). If the operator or agent that launched it forgets the
release, the parked process sits idle indefinitely, holding its resources,
invisible unless someone runs `ctl process list`. Relevance grows as parked
processes become routine (addable runs are parked runs).

## Semantics

**Idle = parked (no eval running) with no control-channel activity.** Any
HTTP request to the control socket — read or mutation — resets the clock.
Implemented as a pure-ASGI middleware (`_ActivityStampMiddleware` in
`_control/server.py`) that stamps `note_control_activity()` on every
request, so future endpoints are covered automatically rather than each
route remembering to reset.

**The timer only runs in the parked state.** It arms when the park starts
and re-arms fresh each time the process returns to park — an eval that ran
for hours without a single control request still gets its full window. Both
parks (standalone eval and eval-set) funnel through
`ControlServer.wait_for_release`, so the deadline is enforced in exactly one
place — a condition-wait bounded by `last_activity + timeout`, where waking
at a stale deadline (activity arrived mid-wait) just re-arms. (A future
`ctl task add` that exits the park to run more work gets timer suspension
for free *provided* it returns from the wait and re-enters a fresh one —
the timer has no existence outside the wait loop.)

**Idle time is monotonic.** The activity stamp and the deadline arithmetic
use `time.monotonic()`, so an NTP step or a suspend/resume can't spuriously
expire a park the moment it wakes — idle means time this *running* process
went unqueried. The wire-facing `park_deadline` converts to a wall-clock
timestamp for display only.

**On expiry, release through the exact same path as `POST /release`**:
`request_release()` clears the intent, the park returns, and the normal
teardown runs (discovery file removed, `done` record emitted, exit 0). Plus:

- a stderr line naming the cause (`print_idle_timeout_release()`), so a
  returning operator can tell timeout from crash;
- `"released": "idle_timeout"` on the `done` record (vs `"released"` for an
  operator release), so `--json` / `--detach` consumers can tell too —
  stderr prose is invisible to them. The `"released"` reason is recorded at
  the release *latch* (the keep→off transition), not at the park's exit, so
  a release that lands mid-run ("exit when done" — the park never runs) is
  reflected in the `done` record too.

## Knobs (three layers)

**1. Launch — the `--ctl-server` value grammar** (not a separate flag):

```
--ctl-server=keep           # park with the default idle timeout (24h)
--ctl-server=keep:4h        # explicit idle timeout (s/m/h/d suffix, or bare seconds)
--ctl-server=keep:forever   # opt out — park indefinitely
```

`resolve_ctl_server()` is deliberately the single source of truth for the
value grammar, shared by the CLI flag, the Python API
(`ctl_server="keep:4h"`), and `INSPECT_EVAL_CTL_SERVER` — one extension
point covers all three entry points, and `--detach`'s verbatim forwarding of
the `--ctl-server` argv token preserves the timeout for free (the click
callback likewise passes keep values through un-normalized rather than
flattening to `"keep"`). A separate flag would need plumbing through
`eval()` / `eval_set()` / `exec_detached()` and could be passed without
`keep` (a dead knob needing its own rejection logic).
`CtlServerConfig` carries the result as `park_idle_timeout: float | None`
(`None` = forever).

Grammar details: `keep:0` is rejected rather than interpreted — ambiguous
between "release immediately" and "never"; the opt-out is spelled `forever`
— consistent with `resolve_ctl_server()`'s fail-loud stance on unknown
values. Values above `MAX_PARK_IDLE_TIMEOUT` (~31.7 years, park-owned twin
of `MAX_GENERATE_CONFIG_OVERRIDE`) are rejected by both the grammar and the
runtime knob, so the two parsers of the one setting enforce the same domain.

**2. Runtime — a process-scoped `ctl config` knob:**

```
inspect ctl config --park-timeout 7200     # seconds
inspect ctl config --park-timeout clear    # restore the launch value / default
```

Rides the existing `PATCH /config` override machinery (same shape as
`--timeout` / `--max-retries`: live override, `clear` restores launch;
strict servers make it version-safe with no client-side gate). It covers
what the launch flag can't: an agent extending the deadline before
detaching; an operator shortening a park they suspect is forgotten; a park
latched at *runtime* via `ctl process keep` (which never saw a launch flag)
getting the default — retunable here. The runtime override cannot express
"forever" (only a launch value can); a huge value stands in.

Useful composition: since any control request resets the clock, `ctl config
--park-timeout` both sets the deadline *and* restarts it — a returning
operator's first command already buys a full window. A shortened window also
wakes the park (`notify_park_change`) so it recomputes rather than sleeping
to the old, later deadline.

Deliberately **not recorded in `EvalLog.config_updates`**: the park is
process lifecycle rather than eval behaviour, and the prime retune moment —
a bare park — has no live eval log to record into.

**3. Visibility — show the deadline, don't just enforce it.** `/tasks` rows
carry `park_deadline` (unix ts; `None` unless parked with a timeout), so
`ctl task list` renders `keep-alive: on · parked — auto-release in 3h12m`
and `ctl process list` shows the countdown in its keep-alive cell. A timeout
nobody can see reads as a crash when it fires; a visible countdown is what
makes a default-on timeout safe. The park notices printed at park entry
mention the window for the same reason. The `/config` view's `park` section
(`timeout` / `override` / `keep_alive` / `deadline`) is the machine-readable
read.

## Default: on, 24 hours

Default-on rather than opt-in:

- The failure mode is *forgetting*. The operators who forget `ctl process
  release` are the same ones who won't pass an opt-in flag — an opt-in
  mitigation for a forgetting failure mode mitigates nothing.
- The activity-reset semantics make default-on low-risk: the timeout only
  fires on a park *nobody has touched at all* for the entire window. Any
  monitoring poll resets it; an agent checking in once a day never hits it.
- 24h clears the human "come back tomorrow morning" workflow (~16h
  overnight) with margin, while bounding a truly abandoned park to a day.
  Deliberately standing parks opt out with `keep:forever`.
- This is a behaviour change to the previous "until release" contract —
  called out in the CHANGELOG, the `--ctl-server` help text, and the docs.

## State model

All park-timeout state is module-level in `_control/server.py`, like the
keep-alive latch itself (the eval-set park binds a FRESH server after the
run's server tears down, so per-server state can't carry across that
boundary): the launch value, the runtime override, the last-activity stamp,
the parked flag, and the release reason. Reset via `reset_keep_alive()` at
the run *start* boundary — not run end — so `park_release_reason()` survives
long enough for the CLI's `done` record (emitted after the run returns) to
read it.

## Non-goals

No timeout while an eval is running (that's `time_limit` territory); no
idle timeout for the non-`keep` default server (it exits with the eval
already); no persistence of the retuned override across processes.
