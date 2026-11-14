"""THE ONLY place FOMO URLs are built (STEP 6, non-negotiable).

Every FOMO link the desk produces (Buy/Sell buttons, every push on every
channel, Activity rows, Coin/Auditor screens, Account tab, morning Page, chat
answers) goes through these functions. Never drop r=savipww. Never shorten or
wrap a FOMO link (no t.co, bit.ly, redirect page or tracking proxy): the URL
on a button is the exact string returned here. pumpfun coins are Solana =
1399811149.
"""

from __future__ import annotations

import logging

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family/r/savipww"

log = logging.getLogger("desk.links")


def coin_url(address, chain="solana"):
    if chain not in CHAIN_ID:
        log.warning("coin_url: unknown chain %r for %s, returning SIGNUP_URL", chain, address)
        return SIGNUP_URL
    return f"https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link"


def profile_url(handle):
    h = handle.lstrip("@")
    return f"https://fomo.family/profile/{h}?source=share_link&r=savipww"


# ---- guards used by notifier.py (and later the API / Page) --------------------

FOMO_HOST = "fomo.family"
SHORTENERS = ("t.co/", "bit.ly/", "tinyurl.com/", "goo.gl/", "ow.ly/", "is.gd/", "buff.ly/",
              "rebrand.ly/", "cutt.ly/", "t.ly/", "rb.gy/", "shorturl.at/", "lnkd.in/")


def is_valid_fomo_url(url: str) -> bool:
    """A FOMO URL as links.py builds it: on FOMO_HOST over https, carries r=savipww
    (SIGNUP_URL carries it as /r/savipww), no wrapper/redirect around it."""
    if not isinstance(url, str) or not url.startswith(f"https://{FOMO_HOST}/"):
        return False
    return url == SIGNUP_URL or "r=savipww" in url
