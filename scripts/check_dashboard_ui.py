#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_dashboard.py"
INDEX_PATH = ROOT / "index.html"
DOUBLE_ENCODED_QUOTE = "%25" + "22"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_dashboard", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load scripts/build_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_trait_url(builder, trait_type: str, trait_value: str, expected_query: str) -> None:
    url = builder.opensea_trait_url(trait_type, trait_value)
    expected = f"{builder.OPENSEA_COLLECTION_URL}{expected_query}"
    if url != expected:
        raise AssertionError(f"OpenSea trait URL mismatch for {trait_type}/{trait_value}:\nactual:   {url}\nexpected: {expected}")
    if DOUBLE_ENCODED_QUOTE in url:
        raise AssertionError(f"OpenSea trait URL is double encoded for {trait_type}/{trait_value}: {url}")


def assert_trait_links() -> None:
    builder = load_builder()
    assert_trait_url(builder, "Background", "Halo", "?traits=[{%22traitType%22:%22Background%22,%22values%22:[%22Halo%22]}]")
    assert_trait_url(builder, "Eyes", "BlueLaserEyes", "?traits=[{%22traitType%22:%22Eyes%22,%22values%22:[%22BlueLaserEyes%22]}]")
    assert_trait_url(builder, "Hat", "BaseballCap", "?traits=[{%22traitType%22:%22Hat%22,%22values%22:[%22BaseballCap%22]}]")
    assert_trait_url(builder, "Background", "Blue Sky", "?traits=[{%22traitType%22:%22Background%22,%22values%22:[%22Blue%20Sky%22]}]")

    html = INDEX_PATH.read_text(encoding="utf-8")
    if DOUBLE_ENCODED_QUOTE in html:
        raise AssertionError("generated index.html contains double-encoded OpenSea trait quotes")
    expected_prefix = f"{builder.OPENSEA_COLLECTION_URL}?traits=[{{%22traitType%22:%22"
    if expected_prefix not in html:
        raise AssertionError("generated index.html missing single-encoded OpenSea trait URL")


def assert_timer_urgency_colors() -> None:
    builder = load_builder()
    if builder.timer_urgency_state(7201, "ongoing") != "calm":
        raise AssertionError("timer should be calm/light green when more than 1 hour remains")
    if builder.timer_urgency_state(3599, "ongoing") != "urgent":
        raise AssertionError("timer should become urgent when less than 1 hour remains")
    if builder.timer_urgency_state(600, "ongoing") != "critical":
        raise AssertionError("timer should become critical in the final 10 minutes")

    html = INDEX_PATH.read_text(encoding="utf-8")
    required_markers = [
        "--paper-calm:#eff8df",
        ".current-detail .timer-card--calm,.current-detail .timer-card--normal{background:var(--paper-calm)",
        ".current-detail .timer-card--urgent{background:var(--paper-urgent)",
        "seconds<=600?'critical':seconds<=3600?'urgent':'calm'",
    ]
    for marker in required_markers:
        if marker not in html:
            raise AssertionError(f"generated index.html missing timer urgency marker: {marker}")


def assert_creator_popover() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")
    required_markers = [
        '<div class="credit-menu">',
        'class="credit-trigger" aria-haspopup="true"',
        'class="credit-popover" aria-label="Mark Carey profile links"',
        '.credit-menu:hover .credit-popover,.credit-menu:focus-within .credit-popover',
        'visibility:visible',
        'pointer-events:auto',
    ]
    for marker in required_markers:
        if marker not in html:
            raise AssertionError(f"generated index.html missing creator popover marker: {marker}")

    if 'top:calc(100% + 8px)' in html:
        raise AssertionError("creator popover has a physical hover gap between trigger and popup")
    if '.credit-menu::after' not in html and 'padding-bottom:8px' not in html:
        raise AssertionError("creator popover lacks an invisible hover bridge/padded hover area")



def assert_no_farcaster_channel_panel() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")
    blocked_markers = [
        'farcaster-channel-panel',
        'farcaster-feed',
        'farcaster_degendogs_channel.json',
        'LIVE SOCIAL FEED',
        'Latest discussion from the /degendogs Farcaster channel.',
        'Farcaster channel snapshot unavailable. Open /degendogs on Farcaster.',
        'loadFarcasterSnapshot',
    ]
    for marker in blocked_markers:
        if marker in html:
            raise AssertionError(f"generated index.html still contains reverted Farcaster panel marker: {marker}")


def main() -> int:
    assert_trait_links()
    assert_timer_urgency_colors()
    assert_creator_popover()
    assert_no_farcaster_channel_panel()
    print("dashboard_ui_checks=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
