# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change; a deployment installs the unit and grants the read separately.

## 1. Observation surface

- [x] 1.1 Add the `export` command to the CLI with bounded flags and the
  documented exit codes, and keep the default (no subcommand) a usage error.
- [x] 1.2 Add one provider read, `Aws.console_output`, that bounds each call,
  decodes the response and treats an aged-out instance identifier as absent
  rather than as a failure.
- [x] 1.3 Implement the marker parser and the fixed vocabulary, including the
  duplicated serial-console stream and an empty console.

## 2. Durable observation state

- [x] 2.1 Implement the dedupe record with a seven-day prune, a documented
  rebuild path for a missing or corrupt file, and no influence on admission.
- [x] 2.2 Derive launch and registration counters from that record so a
  terminated instance cannot make a counter fall.

## 3. Output contract

- [x] 3.1 Write the metrics textfile atomically, name the deployment on every
  series, and leave the previous file intact on a failed write.
- [x] 3.2 Append new events only, one JSON object per line, and rotate the file
  at the documented size.
- [x] 3.3 Keep ledger-derived budget series optional: omit them and report the
  omission when the ledger cannot be read.

## 4. Deployment artefacts

- [x] 4.1 Ship the one-shot unit and its timer, with the documented mounts, no
  token env-file and no published port.
- [x] 4.2 Add `ec2:GetConsoleOutput` to the scaling policy so the command can
  do nothing else with the console it reads.

## 5. Checks and documentation

- [x] 5.1 Cover the parser, the dedupe record, the textfile writer, the
  degraded-ledger path, the console-read cap and the exit codes with
  standard-library tests and synthetic fixtures.
- [x] 5.2 Extend the deploy-unit suite to the new unit and timer.
- [x] 5.3 Document the command in `docs/operations.md`, the unit in
  `docs/container.md`, the permission in `docs/deployment.md` and the
  product-versus-deployment tunnel wording in `docs/router.md`.
