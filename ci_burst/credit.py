"""Explicit CPU-credit evidence for the supported instance families.

A burstable-performance instance runs in ``standard`` mode, where it is
throttled once its earned credits are spent, or ``unlimited`` mode, where it
keeps bursting and can accrue surplus credits that the provider bills on top of
the instance price. Admission prices compute plus one configured incidental
rate; it cannot see surplus credits, so the plan states that exposure instead of
pretending the estimate bounds it.

The table is deliberately closed. A family outside it reports ``unknown`` rather
than ``not burstable``: the instance-type name does not prove burstability, and
inferring it would be a guess. The classified families are the ones this
project's configuration examples and runner guide name, plus every burstable T
family AWS documents.

AWS EC2 User Guide, "Configure burstable performance instances": T8i, T4g, T3a
and T3 launch as ``unlimited`` and T2 launches as ``standard``. The account
default credit specification can override the family default, which this module
cannot observe offline. Read 2026-11-12.
"""

from dataclasses import dataclass

#: The values the EC2 API accepts for ``CreditSpecification.CpuCredits``.
CREDIT_MODES = ("standard", "unlimited")

#: Burstable families, mapped to the AWS default mode at launch.
BURSTABLE_FAMILIES = {
    "t2": "standard",
    "t3": "unlimited",
    "t3a": "unlimited",
    "t4g": "unlimited",
    "t8i": "unlimited",
}

#: Non-burstable families named by this project's deployment examples and
#: runner guide. Anything else stays unknown until it is added here.
NON_BURSTABLE_FAMILIES = frozenset({"c7g", "m7g", "m8g", "r7g"})


@dataclass(frozen=True)
class CreditSpecification:
    """A launch template's CPU-credit choice, keeping presence from its value.

    ``present`` records whether the template set a specification at all, and
    ``value`` carries the token only when the adapter could represent it as
    bounded evidence. A present specification whose ``value`` is ``None`` is
    present-but-invalid -- empty, non-printable, non-ASCII or over-long -- which
    is unknown evidence, not absence. Collapsing the two would report the family
    default as if the template had chosen it, and let the prerequisite check
    pass a template the provider would refuse.
    """

    present: bool = False
    value: str | None = None

    @property
    def invalid(self):
        """True when the template chose a specification this product cannot use."""
        return self.present and self.value not in CREDIT_MODES


def family(instance_type):
    """Return the family token of an instance type, or ``None``."""
    if not isinstance(instance_type, str):
        return None
    name = instance_type.strip().lower()
    if "." not in name:
        return None
    return name.split(".", 1)[0] or None


def burstable(instance_type):
    """``True``/``False`` for a classified family, ``None`` when unknown."""
    name = family(instance_type)
    if name in BURSTABLE_FAMILIES:
        return True
    if name in NON_BURSTABLE_FAMILIES:
        return False
    return None


def evidence(instance_type, template_mode=CreditSpecification()):
    """One candidate's bounded CPU-credit evidence.

    ``template_mode`` is the launch template's CPU-credit choice. Its three
    states stay distinct: absence means the template sets no specification, so a
    burstable family reports its documented default; a recognized value outranks
    that default with the template as its source; a present value that is
    unrecognized or that the adapter could not represent reports no mode,
    because the provider would refuse the launch and no default applies. A
    non-burstable family reports the credit question as not applicable unless
    the template pins a recognized mode, and an unclassified family reports
    unknown. Surplus credits are never reported as bounded.
    """
    state = burstable(instance_type)
    recognized = template_mode.value in CREDIT_MODES
    if state is True:
        if recognized:
            mode, mode_source = template_mode.value, "template"
        elif template_mode.present:
            mode, mode_source = None, "template-unrecognized"
        else:
            mode, mode_source = BURSTABLE_FAMILIES[family(instance_type)], "aws-default"
        return {
            "burstable": True,
            "mode": mode,
            "mode_source": mode_source,
            # A present specification we cannot read leaves the effective mode
            # unknown, so the surplus-credit exposure cannot be stated either.
            "surplus_credits": "unknown" if mode_source == "template-unrecognized" else "unquantified",
        }
    if state is False:
        if recognized:
            mode, mode_source = template_mode.value, "template"
        else:
            mode, mode_source = None, "not-applicable"
        return {
            "burstable": False,
            "mode": mode,
            "mode_source": mode_source,
            "surplus_credits": "not-applicable",
        }
    return {
        "burstable": None,
        "mode": None,
        "mode_source": "unknown",
        "surplus_credits": "unknown",
    }
