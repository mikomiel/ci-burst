"""Exact money accounting.

Currency values are integer millionths (micros) of one unit. They are parsed
from decimal strings only: a TOML float such as ``0.03`` cannot represent most
decimal prices, so accepting one would silently change a configured cap.
Formatting is display-only and never feeds an arithmetic decision.
"""

MICROS_PER_UNIT = 1_000_000


def parse_money(value, field):
    """Parse a positive decimal string into exact micros.

    ``value`` must be a string such as ``"0.0425"``. Floats, integers, signs,
    exponents and more than six fractional digits are refused rather than
    rounded, because each would move the configured cap.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a quoted decimal string, not {type(value).__name__}")
    if not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty decimal string without surrounding space")
    whole, dot, fraction = value.partition(".")
    if not whole.isdigit() or (dot and not fraction.isdigit()):
        raise ValueError(f"{field} must be a plain positive decimal string such as \"0.0425\"")
    if len(fraction) > 6:
        raise ValueError(f"{field} has more than six fractional digits")
    micros = int(whole) * MICROS_PER_UNIT
    if dot:
        micros += int(fraction.ljust(6, "0"))
    if micros <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return micros


def format_money(micros):
    """Render micros as a plain decimal string for display only."""
    value = int(micros)
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), MICROS_PER_UNIT)
    return f"{sign}{whole}.{fraction:06d}"


def hourly_cost_micros(price_per_hour_micros, seconds):
    """Charge for ``seconds`` at an hourly price, rounded up to one micro."""
    if price_per_hour_micros < 0 or seconds < 0:
        raise ValueError("price and duration must not be negative")
    return (price_per_hour_micros * seconds + 3599) // 3600
