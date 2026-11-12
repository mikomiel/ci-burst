# ci-burst runs one command per process; it is not a daemon.
#
# Non-negotiables for every run (docs/container.md owns the operator steps):
#   * one-shot CLI: no daemon, no published ports, no healthcheck. The exit code
#     is the only completion signal (0 completed decision, 2 refusal, 1 provider
#     or cleanup failure).
#   * run with --read-only and a tmpfs for /tmp; no host path is written except
#     the mounted state directory.
#   * the deployment TOML is mounted read-only.
#   * the state DIRECTORY is mounted writable. The SQLite ledger, its .lock and
#     its rollback journal must stay in that one directory: a copied file is a
#     second ledger and a second budget.
#   * cloud credentials are mounted read-only; none is baked into this image,
#     and the SDK's instance-metadata fallback is disabled so a missing or
#     unreadable file fails instead of using a host instance role.
#   * the Forgejo token arrives in an --env-file, which is visible in
#     `podman inspect`. A token-file option is a follow-up, not part of this
#     change.
# The base is pinned by its multi-arch index digest, not the moving
# `python:3.12-slim` tag: the tag resolved to different bytes over time, so the
# same source commit could build against a different interpreter base. The
# digest below was resolved from the registry's manifest response on 2026-11-12;
# bumping it is a reviewed input change, and the refresh build record names the
# FROM line it actually built with.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AWS_EC2_METADATA_DISABLED=true \
    AWS_SHARED_CREDENTIALS_FILE=/etc/ci-burst/aws/credentials \
    AWS_CONFIG_FILE=/etc/ci-burst/aws/config

RUN useradd --create-home --uid 10001 --user-group ci-burst \
 && install -d -o 10001 -g 10001 -m 0700 /var/lib/ci-burst \
 && install -d -o root -g root -m 0755 /etc/ci-burst

WORKDIR /srv/ci-burst
COPY pyproject.toml README.md LICENSE ./
COPY ci_burst ./ci_burst
RUN pip install --no-cache-dir .

USER 10001:10001

# No CMD: a run always names its command, for example
#   podman run ... localhost/ci-burst:latest apply --config /etc/ci-burst/deployment.toml
ENTRYPOINT ["ci-burst"]
