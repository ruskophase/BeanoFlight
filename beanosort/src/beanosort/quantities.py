from decimal import Decimal, InvalidOperation


class DomainError(ValueError):
    pass


RECEIVING_CAPACITY_POLICY = "nominal-capacity-120-percent/v1"


def receiving_capacity_limit_mg(nominal_mg):
    """Receiving only: 120% of nominal mass, rounded down to whole milligrams."""
    return None if nominal_mg is None else nominal_mg * 6 // 5


def mass(value, unit="kg", *, zero=False):
    """Exact integer milligrams. Reject floats and sub-milligram rounding."""
    if isinstance(value, (float, bool)) or unit not in {"kg", "g", "mg"}:
        raise DomainError("Use a decimal string with kg, g or mg")
    try:
        amount = Decimal(value) * {"kg": 1000000, "g": 1000, "mg": 1}[unit]
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise DomainError("Mass must be finite, with at most milligram precision")
        if amount < 0 or (amount == 0 and not zero) or amount > 10**15:
            raise DomainError("Mass outside permitted range")
        return int(amount)
    except (InvalidOperation, TypeError):
        raise DomainError("Enter a valid mass") from None


def kg(value):
    return format(Decimal(value) / 1000000, ".6f").rstrip("0").rstrip(".")


def kg3(value):
    return format(Decimal(value) / 1000000, ".3f")
