# Design

## Context

The controller owns instances by tag and knows their lifecycle, but it holds no
evidence about what happened inside one. A launch can succeed and the worker
still never register: the image boots, the first-boot script reports a failure
on its console, and the controller's next decision simply sees the slot busy
until the lifetime expires.

The console is the only channel that exists for a worker which never reached
the network, and it is a plain provider read. It is also noisy: the first-boot
script duplicates its output to the serial console, the kernel ring buffer
scrolls, and the buffer may include anything the image printed.

## Decisions

### Read the console, not the queue

The command reads `GetConsoleOutput` for instances this deployment owns. It
never calls Forgejo.

Alternative rejected: list runners through the Forgejo API. An ephemeral runner
deregisters when its job ends, so "registered" would be a race against the
polling interval, and the read needs a token with administrative scope. The
console marker is written once, at registration, and survives.

Alternative rejected: an instance-side agent shipping its own log. It needs a
new credential and a new inbound or outbound path, and it cannot report the
case that matters most — a worker that never got far enough to send anything.

### A closed marker vocabulary, and no console text

The parser recognises only:

- a `STEP <name>` line whose name is one of the shipped script's steps
  (`mount-options`, `register`, `job`);
- `bootstrap exiting rc=<n>`;
- one failure kind from a fixed list (`forgejo_ping`, `ssm_timeout`,
  `apt_failed`, `register_failed`, `runner_error`).

A failure line contributes its kind and nothing else. An earlier draft carried a
truncated copy of the line, which is not a boundary: an image can print a
credential in a line that matches `level=error`, and truncation preserves a
prefix of it. The step set is closed for the same reason — a pattern-matched
name would let an image publish an arbitrary string through the step field.

The console is not a trusted channel. An operator who needs the raw line reads
it with the provider CLI; that read is outside this contract and outside the
published stream.

### At-least-once delivery with a stable identity

Each event carries `id = <instance>:<marker>`, and the dedupe record prevents a
repeat in the ordinary case. A crash between publishing the event and persisting
the record can repeat the event; the identity is what makes the repeat
recognisable to a consumer.

Alternative rejected: persist first, then publish. That converts a repeat into a
loss, and a lost failure event is worse than a duplicated one. Alternative
rejected: one transactional file for both. The event stream is append-only
across runs and the record is replaced; joining them would mean rewriting the
whole stream on every run, which is the rotation problem in a worse form.

### Counters that outlive their records

A terminated instance disappears from `DescribeInstances` after about an hour,
and a per-instance record is pruned after seven days. A counter computed by
counting records would therefore fall — a Prometheus counter decrease that
looks like a process restart.

The state file keeps two things: cumulative totals (`launches`, `registered`,
`unregistered`) that only ever increase, and per-instance records that expire.
The textfile reads the totals.

### A textfile, not a listener

The command writes one Prometheus textfile atomically (temporary file in the
same directory, then `os.replace`). Every deployment that wants these metrics
already runs a textfile collector; a listen port would be a new surface, a new
firewall decision and a new failure mode for a process that runs once a minute.

### The ledger read is optional

Budget series are read from the SQLite ledger in read-only mode. When the ledger
is absent or unreadable, those series are omitted, a note is added to the JSON
result, and the run still succeeds: the console evidence does not depend on the
ledger, and a metrics exporter that fails closed on a missing file is worse than
one that reports what it has.

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Instance pending, console empty | no marker, no error, instance counted as observed |
| `InvalidInstanceID.NotFound` | instance skipped; it aged out of the provider |
| More owned instances than the per-run cap | refusal `console_read_cap_exceeded`, exit 2, nothing written |
| Textfile write fails | temporary file removed, previous textfile intact, exit 1 |
| State file unreadable or structurally invalid | replaced with a fresh one, note in the result, run continues |
| Ledger unreadable | ledger series omitted, note in the result, run continues |

## Bounds

| Bound | Value | Why |
|---|---|---|
| Console reads per run | 16 | the configured fleet limit is four; a day of terminations still fits |
| Bytes read per console | 64 KiB | the API's own ceiling for one call |
| Error kinds per instance | 3 | three distinct failures are already a diagnosis |
| Published console text | none | a kind, a step name, or an exit code |
| Events file | rotated at 4 MiB | a day of markers is a few kilobytes |
| State entries | 7 days | longer than any deployment's interest in one instance |

## Credential boundary

The command uses the deployment's existing provider credential and needs one
additional read permission (`ec2:GetConsoleOutput`). It takes no Forgejo token,
so the unit installs no token env-file and the exporter cannot read the queue.

## Evidence gaps

- The marker vocabulary belongs to the shipped worker image. A different image
  emits nothing, and the command then reports zero observations instead of
  guessing a state from timestamps.
- Instances without the ownership tags — a hand-launched canary — are invisible
  here by design. Reading their console directly remains available.
- A marker that scrolls out of the console buffer before a run is lost; the cap
  and the cadence make that unlikely, not impossible.
