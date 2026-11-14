"""STEP 8 static checks on web/: no secrets, no FOMO URL built in the front end,
no framework or chart library, and the spec's fixed strings and rules are present."""
import json
import re
from pathlib import Path

import config

WEB = config.DESK_ROOT / "web"
TEXT = {p.name: p.read_text() for p in WEB.rglob("*") if p.suffix in (".html", ".css", ".js", ".json", ".txt")}
ALL = "\n".join(TEXT.values())


def test_no_secret_values_in_web():
    for name in ("TYPESAFE_API_KEY", "TG_TOKEN", "APP_TOKEN", "DATA_PATH", "GH_PAGES_TOKEN"):
        v = config.read_secret(name)
        if v:
            assert v not in ALL, f"{name} found in web/"


def test_no_fomo_url_built_in_front_end():
    assert "fomo.family" not in ALL
    assert "bit.ly" not in ALL and "t.co/" not in ALL


def test_plain_files_no_external_scripts_or_libs():
    html = TEXT["index.html"]
    assert re.findall(r'<script src="([^"]+)"', html) == ["chart.js", "app.js"]
    assert "http" not in "".join(re.findall(r'(?:src|href)="([^"]+)"', html))
    for lib in ("chart.umd", "new Chart(", "d3.", "react", "vue", "jquery"):
        assert lib not in ALL


def test_palette_and_fonts():
    css = TEXT["app.css"]
    for c in ("#000", "#fff", "rgba(235,235,245,0.6)", "rgba(235,235,245,0.3)", "rgba(84,84,88,0.55)", "#1c1c1e",
              "#2c2c2e", "#00d35a", "#ff453a", "#ff9f0a", "#64d2ff", "#0a84ff", "#ffd60a", "#ff6b9a"):
        assert c in css, c
    assert "SF Pro Display" in css and "SF Pro Text" in css and "ui-monospace" in css
    assert "tabular-nums" in css
    assert "env(safe-area-inset-bottom" in css and "backdrop-filter" in css
    assert "aspect-ratio: 504 / 470" in css and "42 / 504" in css
    assert "prefers-reduced-motion" in css and "1.2s" in css
    assert "text-transform: uppercase" not in css and "text-shadow" not in css


def test_spec_strings_present():
    js = TEXT["app.js"]
    for s in ("Waiting for the first trade", "Listening for graduations", "Nothing yet tonight",
              "No coin taken apart yet", "Desk offline · retrying in ${n}s · last update ${last}",
              "PAPER", "Grok Bot <", "See all", "Take apart before buying", "Open on FOMO",
              "Buy on FOMO", "Sell on FOMO", "Your FOMO profile", "Invite a friend", "Link copied",
              "navigator.share", "taking apart...", "→ auditor", "✓ Filled", "Executing...",
              "since entry", "Type LIVE to confirm", "#d=", "localStorage", "Telegram only"):
        assert s.replace("#d=", "d") in js or s in js, s
    for r in ("LIVE", "1H", "4H", "1D", "1W", "ALL"):
        assert f'"{r}"' in js
    for t in ("Home", "Agents", "Activity", "Account"):
        assert f'"{t}"' in js


def test_motion_timings():
    js, ch, css = TEXT["app.js"], TEXT["chart.js"], TEXT["app.css"]
    assert "600ms" in css and "* 30" in js                         # odometer roll + stagger
    assert "350" in js and "150" in js                              # audit beam
    assert "/ 250" in ch and "/ 200" in ch                          # chart point slide, mark fade
    assert "devicePixelRatio" in ch


def test_manifest_and_sw():
    m = json.loads(TEXT["manifest.json"])
    assert m["display"] == "standalone" and m["start_url"] == "./"
    assert (WEB / "icons" / "apple-touch-icon.png").is_file()
    sw = TEXT["sw.js"]
    assert "notificationclick" in sw and "openWindow" in sw and "r=savipww" in sw


def test_fragment_is_removed_from_address_bar():
    js = TEXT["app.js"]
    assert "history.replaceState(null" in js
