"""STEP 6 · links.py is the only builder of FOMO URLs (STEP 13 item 1 rules)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from links import CHAIN_ID, SIGNUP_URL, coin_url, is_valid_fomo_url, profile_url

ROOT = Path(__file__).resolve().parent.parent
MINT = "4Ye6Y6Vaic8cyPdfW2f9PcLhXkysBtSmV84G8X7spump"


def test_chain_ids():
    assert CHAIN_ID == {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}


def test_coin_url_exact_string_and_default_chain_solana():
    expect = (f"https://fomo.family/coin?address={MINT}"
              "&chainId=1399811149&r=savipww&source=share_link")
    assert coin_url(MINT) == expect                  # default chain = solana (pumpfun coins)
    assert coin_url(MINT, "solana") == expect
    assert coin_url(MINT, chain="solana") == expect


def test_every_chain_keeps_referral():
    for chain, cid in CHAIN_ID.items():
        u = coin_url("0xabc", chain)
        assert f"chainId={cid}" in u and "r=savipww" in u and u.startswith("https://fomo.family/coin?")


def test_unknown_chain_returns_signup_and_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="desk.links"):
        assert coin_url(MINT, "ethereum") == SIGNUP_URL
    assert SIGNUP_URL == "https://fomo.family/r/savipww"
    assert any("unknown chain" in r.getMessage() and "ethereum" in r.getMessage() for r in caplog.records)


def test_profile_url_strips_at_and_keeps_referral():
    expect = "https://fomo.family/profile/Cashoutkacper?source=share_link&r=savipww"
    assert profile_url("Cashoutkacper") == expect
    assert profile_url("@Cashoutkacper") == expect
    assert profile_url("@@Cashoutkacper") == expect


def test_validator():
    assert is_valid_fomo_url(coin_url(MINT)) and is_valid_fomo_url(SIGNUP_URL)
    assert is_valid_fomo_url(profile_url("@x"))
    assert not is_valid_fomo_url(f"https://fomo.family/coin?address={MINT}&chainId=1399811149")
    assert not is_valid_fomo_url("https://bit.ly/abc")
    assert not is_valid_fomo_url("https://t.co/x?u=" + coin_url(MINT))


def _code_files():
    for p in list(ROOT.glob("*.py")) + list((ROOT / "agents").glob("*.py")) + \
            [p for p in (ROOT / "web").rglob("*") if p.is_file()]:
        yield p


def test_no_fomo_url_written_anywhere_else_in_code():
    offenders = [p.name for p in _code_files() if p.name != "links.py"
                 and "fomo.family" in p.read_text(errors="ignore")]
    assert offenders == []


def test_no_shortener_or_redirect_in_code():
    pat = re.compile(r"https?://(t\.co|bit\.ly|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|cutt\.ly|rb\.gy)/", re.I)
    offenders = [p.name for p in _code_files() if p.name != "links.py" and pat.search(p.read_text(errors="ignore"))]
    assert offenders == []


def test_every_fomo_url_in_links_py_keeps_referral():
    src = (ROOT / "links.py").read_text()
    urls = re.findall(r"https://fomo\.family/[^\"'\s]*", src)
    assert urls and all("savipww" in u for u in urls)


def test_db_signal_link_comes_from_links(tmp_path):
    import db
    p = tmp_path / "l.db"
    db.init_db(p)
    c = db.connect(p)
    c.execute("INSERT INTO coins(mint) VALUES (?)", (MINT,))
    db.insert_signal(c, MINT, price=1.0, mode="PAPER", status="sent")
    assert c.execute("SELECT link FROM signals").fetchone()[0] == coin_url(MINT, "solana")
