# Design

## Decision

The worker writes one fixed marker to its serial console; the controller's
existing console reader turns it into a `spot-interruption` worker-event on the
controller. The job account's runner process is signalled `SIGTERM` so the job
stops before the instance is reclaimed.

## Why this channel

The worker→controller channels available today are:

| Channel | Verdict |
|---|---|
| Serial console, read by `export` | Taken. Already the worker's boot channel, already shipped, no credential. |
| Direct push from the worker to the log store | Declined. Puts a write credential on a host that runs untrusted job code, and needs a new ingress path. |
| SSM parameter or object store from the worker | Declined. Needs an IAM change on the worker role, which is operator-owned. |
| The job's own steps or log | Refused. The head controls them, so the evidence would be forgeable by the code under test. |

The console is not trusted either, which is why the marker carries no free
text: `action` is one of four fixed values and `host` must match the shipped
hostname pattern before anything is published. The cost of a forged marker is
bounded at the consumer, not here: a marker is evidence, and the consumer applies
its own rules before acting on it.

## Why root, and why the job account is still denied

The bootstrap blocks IMDS for the `runner` account because job code must not read
instance credentials. The watcher therefore runs as root in the bootstrap, which
already runs as root and already reaches the registration parameter store. The
denial stays: a job still cannot read the notice, the instance identity or the
region.

## Ending the job

On a notice the watcher signals the one process it can prove belongs to the job
account: it walks `/proc`, keeps only processes owned by `runner` whose command
line names `forgejo-runner`, and sends `SIGTERM`. It does not kill by name alone,
it does not signal another account's process, and it never calls Forgejo.

The job then ends with the runner's status, the existing `bootstrap one-job`
marker reports it, and the bootstrap's EXIT path still removes the registration
file and shuts the instance down. A signal that cannot be delivered is not fatal:
the instance is reclaimed either way.

## What this does not claim

- Forgejo's verdict timing is unchanged. The server's lost-runner sweep still
  fails the job on its own schedule; the marker only gives a consumer earlier
  evidence than that sweep.
- A metadata read that fails writes nothing and changes nothing: the worker keeps
  the lifetime and job-timeout bounds it had before.
- The marker is not a job result and not proof of infrastructure loss on its own.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| IMDS token or notice read fails | No marker, no signal; the worker runs as before. |
| Notice body has no documented action | The marker is written with `action=unknown`. |
| Hostname unavailable in the marker line | The line is not a marker and is ignored by the reader. |
| Watcher outlives the bootstrap | The EXIT trap kills it before shutdown. |
| Reader sees no marker because the console was lost | Nothing is published; the instance record still ends on its provider state. |

## Alternatives

- Publish the marker without the hostname. Declined: the consumer would need a
  provider call to join the marker to a job, and the hostname is the value the
  job's own attribution line already records.
- Publish the runner UUID instead of the hostname. Declined for v1: it is only
  readable from the registration file after registration, so a notice during
  boot would carry no identifier, while the hostname always exists.
- Treat the marker as a job verdict inside this product. Refused: this product
  observes, it does not adjudicate Forgejo results.

## Rollback

Revert the change and publish the next template version. The reader keeps
validating old event lines, so a rollback republishes a stream it still accepts;
workers on the previous version simply never write the marker.

## Evidence gaps

- No live Spot reclaim has exercised the watcher yet. The source and policy tests
  prove the shape, not the behaviour on a reclaimed instance; runtime
  verification needs one real interruption observed in the event stream.
