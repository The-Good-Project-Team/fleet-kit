#!/bin/bash
# philanthropy_prod_diag.sh -- the philanthropy.org prod diagnostic driver (gh#728, AC5).
#
# Contract: scripts/prod_diag_driver.md. Wire its path into FIXER_PROD_DIAG_DRIVER.
#
# This is the thin half of the pair the issue body describes: dino's forced-command deploy key
# to `atlas-serve` refuses a shell (`ssh atlas-serve id` -> "deploy key: command not permitted"),
# so the product repo's own `scripts/lucky2/prod_diag.sh` (which needs a shell) cannot be reached
# directly. It is reached through a new `prod-diag <section>` verb on the product's
# `scripts/box/deploy-receive.sh` instead -- that verb is a separate, cross-repo change (see
# gh#728's own out-of-scope note; it has not landed as of this file's writing). Until it does,
# this driver runs but the deploy key will refuse `prod-diag` the same way it refuses `id`.
#
# Does exactly one thing, on purpose: shell out to the one verb the key accepts for this and
# print its raw output. No section validation here -- deploy-receive.sh's own `prod-diag` verb
# is the allowlist gate (app/pg/svc/log/all), same "reviewed script enforces the constraint, not
# the caller" split prod_diag_driver.md documents.
set -euo pipefail
exec ssh atlas-serve prod-diag "$1"
