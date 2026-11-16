# Job-account cache environment: the one definition.
#
# The runtime bootstrap starts every job with these variables, so a cache that a
# warm step fills at any other path is a cache no job reads. The clean-up of the
# warm-cache layer keeps only the `cache` directory, so every value must lie
# under /var/lib/forgejo-runner/cache.
#
# One NAME=value assignment per line, no quoting, no expansion and no export.
# This file is data that three scripts carry a copy of:
#
#   - deploy/runner/bootstrap.sh is published launch-template user data with a
#     pinned hash and cannot read a repository file, so it keeps inline
#     assignments;
#   - deploy/runner/image/provision.sh writes this file into the image from an
#     embedded copy and runs its probe with the installed file;
#   - deploy/runner/image/warm-cache.sh runs as lone user data on a builder
#     whose image may predate this file, so it keeps an inline copy.
#
# tests/test_cache_environment.py parses every copy and fails, naming the
# variable, when a name is missing or a value differs from this file. Change a
# value here and in each copy together; a bootstrap change is a new
# launch-template version and its own change.
XDG_CACHE_HOME=/var/lib/forgejo-runner/cache
UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv
NPM_CONFIG_CACHE=/var/lib/forgejo-runner/cache/npm
npm_config_cache=/var/lib/forgejo-runner/cache/npm
PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip
CI_PROFILE_CACHE_ROOT=/var/lib/forgejo-runner/cache/validation
