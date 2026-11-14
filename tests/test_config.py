"""STEP 10 settings: defaults, ranges, seeding, aliases."""

import pytest

import config
import db


def test_step10_count_and_defaults():
    step10 = [s for s in config.SPECS if s.step10]
    assert len(step10) == 49   # scanner 7, auditor 8, analyst 6, rules 6, executor 7, risk 7, desk 8
    d = config.defaults()
    assert d["scanner.min_liquidity_usd"] == 40000 and d["risk.stop_pct"] == 6 and d["mode"] == "PAPER"
    assert d["executor.min_ticket_usd"] == 47.50 and d["paper_start_stack"] == 200


def test_out_of_range_refused_with_range():
    with pytest.raises(config.SettingError, match="between 0.5 and 90"):
        config.coerce("risk.stop_pct", 95)
    with pytest.raises(config.SettingError, match="one of bump, skip"):
        config.coerce("executor.below_min", "maybe")


def test_seed_keeps_existing(tmp_path):
    p = tmp_path / "s.db"
    db.init_db(p)
    c = db.connect(p)
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('analyst_mode', 'jev')")
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('risk.stop_pct', '7')")
    config.seed(c)
    cfg = config.load(c)
    assert cfg["analyst_mode"] == "jev" and cfg["risk.stop_pct"] == 7.0 and cfg["scanner.poll_s"] == 60


def test_set_writes_change_log_and_ticket(tmp_path):
    p = tmp_path / "s.db"
    db.init_db(p)
    c = db.connect(p)
    config.seed(c)
    r = config.set_value(c, "below_min", "skip", who="owner")
    assert r["ticket"]["level"] == "skip"
    assert c.execute("SELECT key, old, new, who FROM change_log").fetchall()[-1][:] == \
        ("executor.below_min", "bump", "skip", "owner")
    with pytest.raises(config.SettingError):
        config.set_value(c, "poll_s", 30)   # ambiguous: scanner.poll_s / risk.poll_s
