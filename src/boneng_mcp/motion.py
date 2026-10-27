"""Engineering-unit motion checks and the inspected PC command encodings."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from .paths import UserInputError


def decimal_number(value: Any, label: str) -> Decimal:
    if type(value) not in {int, float, str}:
        raise UserInputError(label + " must be a finite number.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise UserInputError(label + " must be a finite number.") from error
    if not number.is_finite():
        raise UserInputError(label + " must be a finite number.")
    return number


def duration_ms(value: float | None) -> int:
    if value is None:
        return 0
    number = decimal_number(value, "Duration")
    if number <= 0 or number * 1000 > 2_147_483_647:
        raise UserInputError("Duration must be positive and at most 2147483.647 seconds, or null for continuous running.")
    return max(1, int(number * 1000))


def speed_encoding(speed: Any, maximum: Any, unit: str, limits: dict[str, Any], *, allow_zero: bool = False,
                   fixed_encoding: dict[str, Any] | None = None) -> dict[str, Any]:
    requested = decimal_number(speed, "Speed")
    ceiling = decimal_number(maximum, "Maximum speed")
    if unit not in {"rpm", "Hz"}:
        raise UserInputError("Use rpm or Hz.")
    rated = decimal_number(limits["rated_rpm"], "Motor rated speed")
    motor_max = decimal_number(limits["motor_max_rpm"], "Motor maximum speed")
    base = decimal_number(limits["base_rpm"], "Motor speed base") if unit == "rpm" else decimal_number(limits["rated_frequency_raw"], "Motor frequency base") / 100
    allowed = min(rated, motor_max)
    if unit == "Hz":
        allowed = allowed / decimal_number(limits["base_rpm"], "Motor speed base") * base
    if min(rated, motor_max, base, allowed) <= 0 or ceiling <= 0 or ceiling > allowed:
        raise UserInputError("Maximum speed exceeds the live motor rated/maximum speed limit.")
    if abs(requested) > ceiling or (not allow_zero and requested == 0):
        raise UserInputError("Speed must be within the authorized maximum; the initial speed must be nonzero.")
    # Fix the encoding for the entire session. Changing the unit bit and value
    # in separate frames while running would create a transient wrong setpoint.
    if fixed_encoding is not None:
        wire_unit = fixed_encoding["wire_unit"]
        divisor = int(fixed_encoding["divisor"])
    elif ceiling * 100 <= 32767:
        wire_unit, divisor = unit, 100
    else:
        wire_unit = "percent"
        divisor = 100000 if ceiling / base * 100000 <= 32767 else 10000
    if wire_unit == "percent":
        scale = Decimal(divisor) / base
        encoded_speed = requested * divisor / base
        encoded_maximum = ceiling * divisor / base
        unit_bits = 0
    elif wire_unit == unit and divisor in {100, 1000}:
        scale = Decimal(divisor)
        encoded_speed, encoded_maximum = requested * divisor, ceiling * divisor
        unit_bits = 2 if unit == "rpm" else 1
    else:
        raise UserInputError("Unsupported session speed encoding.")
    if wire_unit == "percent" and divisor not in {10000, 100000}:
        raise UserInputError("Unsupported percentage divisor.")
    raw = int(encoded_speed.to_integral_value(rounding=ROUND_DOWN))
    maximum_raw = int(encoded_maximum.to_integral_value(rounding=ROUND_DOWN))
    if not 0 < maximum_raw <= 32767 or abs(raw) > maximum_raw or (requested != 0 and raw == 0):
        raise UserInputError("Speed cannot be represented by this session's 16-bit command encoding.")
    decimal_bit = int(divisor in {1000, 100000})
    effective = Decimal(raw) * base / divisor if wire_unit == "percent" else Decimal(raw) / divisor
    return {"wire_unit": wire_unit, "divisor": divisor, "unit_bits": unit_bits, "decimal_bit": decimal_bit,
            "control_idle": 0x847E | unit_bits << 11 | decimal_bit << 13,
            "raw": raw, "maximum_raw": maximum_raw, "requested_speed": float(requested),
            "effective_speed": float(effective), "resolution": float(1 / scale), "engineering_unit": unit,
            "rounding": "toward_zero; effective magnitude never exceeds requested magnitude"}


def position_pulses(value: Any, unit: str, pulses_per_revolution: int | None) -> int:
    number = decimal_number(value, "Position")
    if unit == "pulses":
        scaled = number
    elif unit in {"revolutions", "degrees"} and type(pulses_per_revolution) is int and pulses_per_revolution > 0:
        scaled = number * pulses_per_revolution / (360 if unit == "degrees" else 1)
    else:
        raise UserInputError("Use pulses, or supply a positive pulses_per_revolution for revolutions/degrees.")
    if scaled != scaled.to_integral_value() or not -2_147_483_648 <= scaled <= 2_147_483_647:
        raise UserInputError("Position must resolve to an exact signed 32-bit number of command pulses.")
    return int(scaled)
