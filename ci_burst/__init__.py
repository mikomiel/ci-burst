"""Bounded AWS Spot runner capacity for one Forgejo repository queue.

The package is deliberately small: one repository scope, one Spot backend and
one local SQLite ledger. Forgejo owns job scheduling and results; AWS owns
instances. This controller only decides how many workers, up to the configured
limit, should exist.
"""

__version__ = "0.6.0"
