#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import base64
import hashlib
import html as html_module
import re
import struct
import sys
import zlib
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
    sys.modules[spec.name] = module
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
    if builder.timer_urgency_state(3600, "ongoing") != "calm":
        raise AssertionError("timer should stay calm/light green at exactly 1 hour remaining")
    if builder.timer_urgency_state(3599, "ongoing") != "urgent":
        raise AssertionError("timer should become urgent when less than 1 hour remains")
    if builder.timer_urgency_state(600, "ongoing") != "critical":
        raise AssertionError("timer should become critical in the final 10 minutes")

    html = INDEX_PATH.read_text(encoding="utf-8")
    required_markers = [
        "--paper-calm:#f0fbea",
        ".current-detail .timer-card--calm,.current-detail .timer-card--normal{background:var(--paper-calm)",
        ".current-detail .timer-card--urgent{background:var(--paper-urgent)",
        "seconds<=600?'critical':seconds<3600?'urgent':'calm'",
        "const formatNativeAmount=value=>",
        "formatNativeAmount(amount.native)",
        "const refreshLiveSurface=()=>liveRefreshPromise||",
        "if(liveSnapshotBlock&&Number(nextBlock)<Number(liveSnapshotBlock))throw new Error('verified snapshot block regressed')",
        "const LIVE_REFRESH_MS=5000",
        "const LIVE_RECENT_MS=5*60*1000",
        "const LIVE_RETRY_MAX_MS=2*60*1000",
        "const CURRENT_FETCH_TIMEOUT_MS=6000",
        "const ARCHIVE_FETCH_TIMEOUT_MS=45000",
        "const controller=new AbortController()",
        "const generatedUrls=(name,version)=>{const url=new URL(`generated/${name}.json`,document.baseURI)",
        "const assertStatusAttestation=status=>",
        "const canonicalUint=(value,label,minimum=0)=>",
        "const fetchVerifiedGenerated=async(filename,expectedSha,expectedBytes,maxBytes,timeoutMs)=>",
        "crypto.subtle.digest('SHA-256',bytes)",
        "const liveSnapshotPointer=status=>",
        "const archivePointer=status=>",
        "const assertLiveBundle=",
        "const loadLiveSnapshot=async status=>",
        "fetchGenerated('current_auction',block)",
        "fetchGenerated('auction_feed',block)",
        "fetchGenerated('current_auction_bid_history',block)",
        "fetchGenerated('mission3_metrics',block)",
        "const queueArchiveRefresh=context=>",
        "if(target.key!==liveSnapshotKey||targetArchiveKey!==activeArchiveKey)continue",
        "live snapshot pointer changed during verification",
        "if(context&&archiveSnapshotKey!==nextArchiveKey)queueArchiveRefresh(context)",
        "const refreshNow=async()=>",
        "if(document.hidden)return false",
    ]
    for marker in required_markers:
        if marker not in html:
            raise AssertionError(f"generated index.html missing timer urgency marker: {marker}")
    for forbidden in ("raw.githubusercontent.com", "rootLocal", "const updateLiveDots="):
        if forbidden in html:
            raise AssertionError(f"generated index.html contains unsafe/stale live-data marker: {forbidden}")


def read_rgba_png(path: Path) -> tuple[int, int, list[int]]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError(f"{path} is not a PNG")

    width = height = bit_depth = color_type = interlace = None
    idat = []
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        offset += length + 12
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break

    if any(value is None for value in (width, height, bit_depth, color_type, interlace)):
        raise AssertionError(f"{path} is missing a valid PNG header")
    assert isinstance(width, int)
    assert isinstance(height, int)
    assert isinstance(bit_depth, int)
    assert isinstance(color_type, int)
    assert isinstance(interlace, int)
    if bit_depth != 8 or color_type != 6 or interlace != 0:
        raise AssertionError(f"{path} must be a non-interlaced 8-bit RGBA PNG")

    bytes_per_pixel = 4
    stride = width * bytes_per_pixel
    raw = zlib.decompress(b"".join(idat))
    expected_size = height * (stride + 1)
    if len(raw) != expected_size:
        raise AssertionError(f"{path} has unexpected PNG scanline data")

    previous = bytearray(stride)
    alphas = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        filtered = raw[cursor:cursor + stride]
        cursor += stride
        reconstructed = bytearray(stride)
        for index, value in enumerate(filtered):
            left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = (value + left) & 0xFF
            elif filter_type == 2:
                decoded = (value + above) & 0xFF
            elif filter_type == 3:
                decoded = (value + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + above - upper_left
                left_distance = abs(predictor - left)
                above_distance = abs(predictor - above)
                diagonal_distance = abs(predictor - upper_left)
                nearest = left if left_distance <= above_distance and left_distance <= diagonal_distance else (
                    above if above_distance <= diagonal_distance else upper_left
                )
                decoded = (value + nearest) & 0xFF
            else:
                raise AssertionError(f"{path} uses unsupported PNG filter {filter_type}")
            reconstructed[index] = decoded
        alphas.extend(reconstructed[3::4])
        previous = reconstructed

    return width, height, alphas


def assert_browser_favicon_asset() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")
    favicon_marker = '<link rel="icon" type="image/png" sizes="32x32" href="favicon-32x32.png">'
    if favicon_marker not in html:
        raise AssertionError("generated index.html is missing the browser favicon")
    forbidden_markers = [
        '<link rel="icon" href="data:,">',
        "apple-touch-icon",
        "site-brand",
        "site-logo",
        "degen-dogs-logo.png",
    ]
    for marker in forbidden_markers:
        if marker in html:
            raise AssertionError(f"generated index.html contains non-favicon branding: {marker}")

    path = ROOT / "public" / "favicon-32x32.png"
    if not path.is_file():
        raise AssertionError("missing public browser favicon")
    width, height, alphas = read_rgba_png(path)
    if (width, height) != (32, 32):
        raise AssertionError(f"favicon dimensions are {(width, height)}, expected (32, 32)")
    alpha_values = set(alphas)
    if not {0, 255}.issubset(alpha_values):
        raise AssertionError("favicon must contain both transparent and opaque pixels")
    if alpha_values - {0, 255}:
        raise AssertionError("favicon contains soft alpha noise instead of crisp pixel transparency")
    corners = [alphas[0], alphas[width - 1], alphas[(height - 1) * width], alphas[-1]]
    if any(corners):
        raise AssertionError("favicon must have transparent outer corners")
    for filename in ("degen-dogs-logo.png", "apple-touch-icon.png"):
        if (ROOT / "public" / filename).exists():
            raise AssertionError(f"non-favicon logo asset must not be published: {filename}")


def assert_content_security_policy_hashes() -> None:
    rendered = INDEX_PATH.read_text(encoding="utf-8")
    csp_match = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', rendered)
    style_match = re.search(r"<style>(.*?)</style>", rendered, re.DOTALL)
    script_match = re.search(r"<script>(.*?)</script>", rendered, re.DOTALL)
    if not csp_match or not style_match or not script_match:
        raise AssertionError("generated index.html is missing CSP or inline assets")
    csp = html_module.unescape(csp_match.group(1))
    style_hash = base64.b64encode(hashlib.sha256(style_match.group(1).encode()).digest()).decode()
    script_hash = base64.b64encode(hashlib.sha256(script_match.group(1).encode()).digest()).decode()
    for directive in (
        "default-src 'none'",
        "connect-src 'self'",
        f"style-src 'sha256-{style_hash}'",
        f"script-src 'sha256-{script_hash}'",
    ):
        if directive not in csp:
            raise AssertionError(f"generated index.html CSP missing valid directive: {directive}")
    if '<meta name="referrer" content="no-referrer">' not in rendered:
        raise AssertionError("generated index.html is missing no-referrer policy")


def assert_creator_popover() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")
    required_markers = [
        '<div class="credit-menu">',
        'class="credit-trigger" aria-haspopup="true"',
        'class="credit-popover" aria-label="Mark Carey profile links"',
        '.credit-menu:hover .credit-popover,.credit-menu:focus-within .credit-popover',
        '.top-actions{display:grid;grid-template-columns:repeat(2,max-content);flex:1 1 100%;width:100%;justify-content:flex-start;align-items:flex-start;gap:6px}',
        '.credit-menu{grid-column:1/-1;margin-left:0;max-width:100%}.credit-trigger{box-shadow:2px 2px 0 var(--ink);white-space:normal;text-align:left}.credit-popover{left:0;right:auto;min-width:min(280px,calc(100vw - 24px));max-width:calc(100vw - 24px)}',
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


def assert_bid_history_card_layout() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")
    required_markers = [
        '<details class="bid-history-menu">',
        '.bid-history-menu{position:relative;align-self:stretch;flex:0 1 158px;min-width:150px;max-width:100%;margin-inline:0',
        '.bid-history-menu summary{list-style:none;cursor:pointer;position:relative;display:flex;min-height:48px;height:100%;flex-direction:column;align-items:center;justify-content:center;text-align:center',
        '.bid-history-list{position:absolute;left:50%;top:calc(100% + 3px);z-index:24;transform:translateX(-50%);width:min(340px,calc(100vw - 24px))',
        '@media (max-width:640px){.bid-history-menu{flex:0 1 150px;min-width:136px}',
        '@media (max-width:380px){.current-detail{display:grid;grid-template-columns:1fr}.current-detail > span,.bid-history-menu{width:100%;max-width:100%}',
    ]
    for marker in required_markers:
        if marker not in html:
            raise AssertionError(f"generated index.html missing bid history layout marker: {marker}")


def main() -> int:
    assert_trait_links()
    assert_timer_urgency_colors()
    assert_browser_favicon_asset()
    assert_content_security_policy_hashes()
    assert_creator_popover()
    assert_no_farcaster_channel_panel()
    assert_bid_history_card_layout()
    print("dashboard_ui_checks=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
