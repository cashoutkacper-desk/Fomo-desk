"""STEP 11 fees.py: FOMO on Solana, each side, minus fees_ref_discount."""

import pytest

import config
from fees import base_fee_usd, fee_usd, round_trip_fees


@pytest.mark.parametrize("n, fee", [(5, 0.10), (20, 0.40), (47.49, 0.9498), (47.50, 0.95), (100, 0.95),
                                    (190, 0.95), (190.01, 0.95005), (1000, 5.0)])
def test_tiers(n, fee):
    assert base_fee_usd(n) == pytest.approx(fee)


def test_discount_10pct():
    assert fee_usd(1000, 0.10) == pytest.approx(4.5)        # 0.50% -> 0.45%
    assert fee_usd(100, 0.10) == pytest.approx(0.855)
    assert round_trip_fees(47.5, 52.25, 0.0) == pytest.approx(1.90)


def test_spec_default_kept_desk_default_zero():
    assert config.defaults()["fees_ref_discount"] == 0.10        # spec value, for referred owners
    assert config.referral_signup() is False                     # Kacper did not use savipww
    assert config.desk_default("fees_ref_discount") == 0.0       # effective default on this desk
    assert config.coerce("fees_ref_discount", 0) == 0.0


def test_seeded_db_gets_zero_discount(tmp_path):
    import db
    p = tmp_path / "f.db"
    db.init_db(p)
    c = db.connect(p)
    config.seed(c)
    assert config.load(c)["fees_ref_discount"] == 0.0
