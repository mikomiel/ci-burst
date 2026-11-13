"""Refusal types with stable reason codes.

Every expected refusal carries a reason code from this module. Reason codes are
part of the operational interface: documentation and tests refer to them, and
status output reports them verbatim. A refusal is not an exception in the
unexpected sense -- the CLI reports it and exits with code 2.
"""


class Refusal(Exception):
    """A complete decision cannot be established, so no mutation may happen."""

    def __init__(self, reason, detail="", uncertain=False):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.uncertain = uncertain

    def as_dict(self):
        data = {"reason": self.reason}
        if self.detail:
            data["detail"] = self.detail
        return data


class ConfigRefusal(Refusal):
    """The configuration is missing, malformed or would silently widen scope."""


class ObservationRefusal(Refusal):
    """The Forgejo queue interface is unqualified, unreadable or incomplete."""


class StateRefusal(Refusal):
    """Durable state is absent, locked, corrupt or contradictory."""


class ProviderRefusal(Refusal):
    """AWS refused or could not answer the requested read or mutation."""


#: The local pass budget ended before the next read, phase or mutation could
#: start. This is not a provider answer, so it has its own reason code.
PASS_DEADLINE_EXCEEDED = "pass_deadline_exceeded"


class DeadlineRefusal(Refusal):
    """The pass's local time budget ended before the next read or phase."""

    def __init__(self, detail=""):
        super().__init__(PASS_DEADLINE_EXCEEDED, detail)


class AdmissionRefusal(Refusal):
    """A live launch was requested but at least one admission condition failed."""
