"""FOMO fee model for PAPER PnL (STEP 11). FOMO on Solana, per side:

    $5 .. $47.50   -> 2% of the ticket
    $47.50 .. $190 -> flat $0.95
    above $190     -> 0.50%
then minus fees_ref_discount (10% off through the savipww referral). Gas = 0
(FOMO pays it).

NOTE (Kacper): his FOMO account did NOT sign up through
links.SIGNUP_URL (the savipww referral), so the referral discount is unverified / not
applicable to him. Decided 2026-11-13: fees_ref_discount = 0 on this desk
(desk.db value 0, and config.desk_default() gives 0 because
bootstrap-settings.json has signup_referral_savipww=false). The spec default
0.10 is only used for an owner who did sign up through the referral.
"""

from __future__ import annotations

TIER1_MAX = 47.50
TIER2_MAX = 190.0
TIER1_RATE = 0.02
TIER2_FLAT = 0.95
TIER3_RATE = 0.005


def base_fee_usd(notional_usd: float) -> float:
    """Fee for one side before any discount. Below $5 is outside FOMO's
    stated range; the 2% tier is applied."""
    n = max(0.0, float(notional_usd))
    if n <= 0:
        return 0.0
    if n < TIER1_MAX:
        return n * TIER1_RATE
    if n <= TIER2_MAX:
        return TIER2_FLAT
    return n * TIER3_RATE


def fee_usd(notional_usd: float, discount: float = 0.0) -> float:
    """Fee for one side, minus the referral discount (0.10 = 10% off)."""
    return base_fee_usd(notional_usd) * (1.0 - float(discount))


def round_trip_fees(entry_usd: float, exit_usd: float, discount: float = 0.0) -> float:
    return fee_usd(entry_usd, discount) + fee_usd(exit_usd, discount)
