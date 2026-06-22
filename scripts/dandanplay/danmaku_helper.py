#!/usr/bin/env python3
# danmaku_helper.py — search/fetch/convert danmaku from dandanplay.net
#
# Used by main.lua (mpv side). All commands print machine-readable
# output (paths/JSON) on stdout; human messages go to stderr.
#
# Commands:
#   match-jellyfin <title> [season] [episode]
#       → resolve to dandanplay episodeId; prints "EPID:<id>" or "NONE"
#   match-file <path>
#       → parse filename for title+S+E, then same as match-jellyfin
#   search <query>
#       → list candidate animes/episodes as JSON lines (for manual menu)
#   fetch <episodeId> <output.ass>
#       → download comments, render ASS, write to output.ass; prints
#         "OK:<count>" or "ERROR:<msg>"
#   load-xml <input.xml> <output.ass>
#       → render a local Bilibili-format XML (for users who already have
#         a danmaku file on disk) into ASS
#
# Settings (font size / opacity / speed / density / area / chConvert) are
# read from $DANMAKU_SETTINGS or ~/.config/mpv/danmaku-settings.json so
# the Lua side can pass them through one canonical place.

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Any

# Force stdout/stderr to UTF-8. On Windows, Python's PIPE encoding
# defaults to the system codepage (cp936 on Chinese Windows), which
# would mangle CJK content like "SERIES:尖帽子的魔法工房" into the
# subprocess pipe — mpv's Lua side reads bytes with no idea what
# encoding they're in, and any non-ASCII match anchors break.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# ============================================================================
# Constants & defaults — match upstream Izumiko/Jellyfin-Danmaku
# ============================================================================
DDP_API = "https://api.dandanplay.net"
USER_AGENT = "mpv-dandanplay-danmaku/1.0"

# Default CORS-proxying CF Worker. This proxy bundles dandanplay v2 auth
# server-side so unauthenticated clients can still query the API. We use
# this only as a FALLBACK when the user hasn't registered their own AppId
# yet — once they do, we go direct to dandanplay (out of courtesy: not
# burdening a third party's free worker, and resilient to its eventual
# shutdown / URL change).
DEFAULT_CORS_PROXY = "https://ddplay-api.930524.xyz/cors/"

# Cross-platform path resolution:
#   - mpv config dir (settings, proxy/credentials JSON):
#       Linux/macOS: $MPV_HOME or ~/.config/mpv
#       Windows:     %MPV_HOME% or %APPDATA%\mpv
#   - cache dir (match cache, per-episode offsets):
#       Linux:   $XDG_CACHE_HOME/mpv-danmaku or ~/.cache/mpv-danmaku
#       macOS:   ~/Library/Caches/mpv-danmaku
#       Windows: %LOCALAPPDATA%\mpv-danmaku
# All paths are individually overridable via DANMAKU_* env vars.

def _mpv_config_dir() -> pathlib.Path:
    if os.environ.get("MPV_HOME"):
        return pathlib.Path(os.environ["MPV_HOME"])
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return pathlib.Path(appdata) / "mpv"
    return pathlib.Path.home() / ".config" / "mpv"


def _cache_dir() -> pathlib.Path:
    if sys.platform == "win32":
        localapp = os.environ.get("LOCALAPPDATA")
        if localapp:
            return pathlib.Path(localapp) / "mpv-danmaku"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Caches" / "mpv-danmaku"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return pathlib.Path(xdg) / "mpv-danmaku"
    return pathlib.Path.home() / ".cache" / "mpv-danmaku"


# Two config files, looked up in this order; later overrides earlier.
#
#   1) <mpv-config>/danmaku-config.json  (general config — proxy URL etc.,
#      written by the installer with sensible defaults)
#         {"cors_proxy": "https://...your-proxy.../cors/"}
#
#   2) <mpv-config>/danmaku-credentials.json  (user-supplied AppId pair —
#      NEVER written by the installer)
#         {"app_id": "<from kaedei@dandanplay.net>",
#          "app_secret": "<from kaedei@dandanplay.net>"}
#
# Resolution at request time:
#   - If app_id + app_secret are present → go direct to api.dandanplay.net
#     with HMAC signing. CORS proxy is ignored.
#   - Else if cors_proxy is set → proxy the request.
#   - Else error.
_MPV_CFG = _mpv_config_dir()
CONFIG_FILE = pathlib.Path(os.environ.get(
    "DANMAKU_CONFIG_FILE", str(_MPV_CFG / "danmaku-config.json")))
CREDENTIALS_FILE = pathlib.Path(os.environ.get(
    "DANMAKU_CREDENTIALS_FILE", str(_MPV_CFG / "danmaku-credentials.json")))
CACHE_DIR = pathlib.Path(os.environ.get(
    "DANMAKU_CACHE_DIR", str(_cache_dir())))
SETTINGS_FILE = pathlib.Path(os.environ.get(
    "DANMAKU_SETTINGS_FILE", str(_MPV_CFG / "danmaku-settings.json")))

DEFAULT_SETTINGS = {
    "opacity": 0.75,            # 0.0 - 1.0
    "speed": 144,               # px / second @ 1920p logical width
    "font_size": 36,            # pt @ 1080p logical height (scaled at runtime)
    "density": "medium",        # low|medium|high — translated to bucket-limit
    "area": 0.8,                # vertical fraction of screen used (0.5..1.0)
    "chConvert": 1,             # 0=off, 1=trad→simp, 2=simp→trad
    "stroke_width": 2.0,        # px (border around text for readability)
    # Comma-separated font fallback chain for libass. First name wins if
    # fontconfig has it. "Microsoft YaHei" is checked first because the
    # user installed it under ~/.local/share/fonts; if fontconfig hasn't
    # picked it up, the next two pure-CJK fonts are usually present.
    "font": "Microsoft YaHei,Noto Sans CJK SC,sans-serif",
    # render_mode: how to map each comment's source mode tag to display.
    #   "original" — honor each comment's normalized mode (1=rtl scroll,
    #                4=bottom-fixed, 5=top-fixed, 6=ltr scroll); show_modes
    #                filters individual modes. Raw Bilibili modes 2 and 3
    #                are pre-normalized to 1 ("普通弹幕"); modes 7/8/9
    #                (高级/代码/BAS) aren't renderable in ASS and are
    #                dropped at parse time. (DEFAULT — matches upstream)
    #   "rtl"      — force everyone right→left (classic Bilibili look)
    #   "ltr"      — force everyone left→right
    "render_mode": "original",
    "show_modes": [1, 4, 5, 6], # only consulted when render_mode="original"
    # Source filter: drop comments by their dandanplay source platform.
    # The /comment/?withRelated=true API aggregates from multiple sites and
    # tags each comment's user field with its origin: [BiliBili]…, [Gamer]…,
    # bare-id (native dandanplay), or [Other]… (iqiyi/acfun/tucao/etc.).
    # Listed names are *disabled*; empty list = show everything.
    # Valid values: "bilibili", "gamer", "dandanplay", "other".
    # Mirrors Izumiko/Jellyfin-Danmaku's 弹幕过滤 panel (B站/巴哈/弹弹/其他).
    "disabled_sources": [],
    # Keyword filter: any comment whose text matches ANY pattern is dropped.
    # Patterns use shell-style wildcards (fnmatch): "*" any chars, "?" one
    # char, "[abc]" character class. Examples:
    #   ["*广告*", "*spoiler*"] — block anything containing 广告 or spoiler
    #   ["哈哈哈*"]              — block anything starting with 哈哈哈
    "filter_keywords": [],
    # Duplicate filter: when many users post the same text in a short
    # window (typical at meme moments), collapse to a single line. The
    # first occurrence is kept; if the chain has count >= dedup_min_count,
    # "[+N]" is appended to the kept line so the user knows it spiked.
    "dedup": True,
    "dedup_window": 1.0,        # seconds — chain breaks if next dup is later than this
    "dedup_min_count": 5,       # only show [+N] when the group has at least this many
    # Visual emphasis for collapsed spike lines: when on, the [+N]-annotated
    # comment renders bolder and ~aggregate_font_bonus px larger than baseline
    # so spikes stand out. Vertical position still uses the normal lane y, so
    # the taller text may slightly overlap neighbors above/below — acceptable
    # because aggregated lines are rare. Horizontal width is recomputed at the
    # enlarged size so lane-collision timing stays correct.
    "aggregate_emphasis": True,
    "aggregate_font_bonus": 4,  # extra px of font size for [+N] lines
    # Anti-overlap: when on, comments that can't find a free lane are
    # *dropped* instead of stacked on top of an existing comment. Applies
    # independently to scroll/top/bottom lane pools — does NOT prevent
    # cross-mode collisions (a top-fixed comment and a scroll comment
    # passing through the same row are different pools and can clash).
    # Default ON: cleaner reading experience, sacrifices density.
    "anti_overlap": True,
    "screen_w": 1920,           # ASS PlayResX (script writes pixel coords here)
    "screen_h": 1080,           # ASS PlayResY
}


def log(msg: str) -> None:
    """Human-readable progress to stderr; stdout is reserved for results."""
    print(f"[danmaku] {msg}", file=sys.stderr, flush=True)


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"settings file unreadable, using defaults ({e})")
    return s


# ============================================================================
# Match cache: keyed on (title, season, episode) or filepath
# ============================================================================
def _cache_path() -> pathlib.Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "matches.json"


def cache_get(key: str) -> int | None:
    p = _cache_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get(key)
        return int(v) if v else None
    except Exception:
        return None


def cache_put(key: str, episode_id: int) -> None:
    p = _cache_path()
    data = {}
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = int(episode_id)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ============================================================================
# Series-level alias map.
#
# When auto-match parses a video title to "Series A" but dandanplay knows it
# as "番剧A", the user manually searches and picks "番剧A". We remember that
# mapping so the NEXT episode of "Series A" — even though auto-match still
# parses to the same wrong name — falls back to "番剧A" without manual
# intervention.
#
#   {"Series A": "番剧A",
#    "尖帽子的魔法工坊": "尖帽子的魔法工房"}
#
# Aliases are deliberately series-level (no season/episode). dandanplay
# itself stores different seasons as separate anime entries, so the user
# normally picks per-season anyway and the alias gets overwritten with the
# correct season's title on the next pick if needed.
# ============================================================================
def _alias_path() -> pathlib.Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "aliases.json"


def alias_get(series: str) -> str | None:
    """Return the cached alias for a series name, or None."""
    p = _alias_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get(series)
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def alias_put(series: str, alias: str) -> None:
    """Record that `series` should fall back to searching `alias`. A
    no-op if the names are identical or empty."""
    series = (series or "").strip()
    alias = (alias or "").strip()
    if not series or not alias or series == alias:
        return
    p = _alias_path()
    data: dict[str, str] = {}
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                data = {k: v for k, v in d.items() if isinstance(v, str)}
        except Exception:
            data = {}
    data[series] = alias
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ============================================================================
# dandanplay API client — through a CORS-proxy CF Worker (default), or
# direct with HMAC-SHA256 signature if AppId+AppSecret configured.
# ============================================================================
def _load_config() -> dict:
    """Resolve config from env vars, danmaku-config.json, and the
    separate danmaku-credentials.json (latter overrides earlier)."""
    cfg = {
        "cors_proxy": os.environ.get("DANMAKU_CORS_PROXY", ""),
        "app_id":     os.environ.get("DANMAKU_APP_ID", ""),
        "app_secret": os.environ.get("DANMAKU_APP_SECRET", ""),
    }
    if CONFIG_FILE.is_file():
        try:
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in ("cors_proxy", "app_id", "app_secret"):
                if d.get(k):
                    cfg[k] = d[k]
        except Exception as e:
            log(f"config file unreadable ({e})")
    if CREDENTIALS_FILE.is_file():
        try:
            d = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            for k in ("app_id", "app_secret"):
                if d.get(k):
                    cfg[k] = d[k]
        except Exception as e:
            log(f"credentials file unreadable ({e})")
    # If user has neither creds nor proxy configured, fall back to the
    # default proxy so out-of-the-box install works.
    if not cfg["cors_proxy"] and not (cfg["app_id"] and cfg["app_secret"]):
        cfg["cors_proxy"] = DEFAULT_CORS_PROXY
    return cfg


def _sign(path: str, app_id: str, app_secret: str) -> dict[str, str]:
    """X-AppId / X-Timestamp / X-Signature for direct (proxy-less) requests.
        signature = base64(sha256(AppId + Timestamp + Path + AppSecret))"""
    ts = str(int(time.time()))
    payload = f"{app_id}{ts}{path}{app_secret}".encode("utf-8")
    sig = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return {"X-AppId": app_id, "X-Timestamp": ts, "X-Signature": sig}


def _build_url(path_with_query: str) -> tuple[str, dict[str, str]]:
    """Returns (final_url, extra_headers). PRIORITY: when AppId+AppSecret
    are configured, go direct to api.dandanplay.net with HMAC signing
    (don't burden the CORS proxy). Fall back to proxy when no creds."""
    cfg = _load_config()
    base = DDP_API + path_with_query
    if cfg["app_id"] and cfg["app_secret"]:
        # The path used for signing is the URL path without query string
        sig_path = urllib.parse.urlsplit(base).path
        return base, _sign(sig_path, cfg["app_id"], cfg["app_secret"])
    if cfg["cors_proxy"]:
        return cfg["cors_proxy"].rstrip("/") + "/" + base, {}
    raise RuntimeError(
        "no AppId/AppSecret and no CORS proxy configured. "
        f"Either create {CREDENTIALS_FILE} with your dandanplay creds, "
        f"or set cors_proxy in {CONFIG_FILE}.")


def _http_get_json(path_with_query: str, *, timeout: float = 15.0) -> Any:
    url, extra = _build_url(path_with_query)
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **extra,
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ddp_search_episodes(anime: str, episode: str | int | None = None) -> list[dict]:
    """Search by anime title (and optional episode number).
    Returns list of {animeId, animeTitle, episodes: [...]}."""
    params = {"anime": anime, "withRelated": "true"}
    if episode is not None and episode != "":
        params["episode"] = str(episode)
    path_q = "/api/v2/search/episodes?" + urllib.parse.urlencode(params)
    data = _http_get_json(path_q)
    if not data.get("success", True) and "errorMessage" in data:
        log(f"search error: {data['errorMessage']}")
        return []
    return data.get("animes", [])


def ddp_get_comments(episode_id: int, ch_convert: int = 1) -> list[dict]:
    """Fetch all comments for an episode. Returns list of {p, m} dicts."""
    path_q = (f"/api/v2/comment/{episode_id}"
              f"?withRelated=true&chConvert={ch_convert}")
    data = _http_get_json(path_q, timeout=25.0)
    return data.get("comments", [])


# ============================================================================
# Filename parsing — pull title + season + episode out of common patterns
# ============================================================================
_SE_PATS = [
    # S01E03 / s01e03  (season + episode)
    re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE),
    # 1x03  (season x episode)
    re.compile(r"\b(\d{1,2})x(\d{1,3})\b"),
    # E03 / Ep03 / EP.03 / 第03话 / [03] / - 03 (episode-only; season=1)
    re.compile(r"\bE[Pp]?\.?\s?(\d{1,3})\b"),
    re.compile(r"第\s*(\d{1,3})\s*[话集]"),
    re.compile(r"[\[\(]\s*(\d{1,3})\s*[\]\)]"),
    re.compile(r"-\s*(\d{1,3})\s*(?:[\[\(\.]|$)"),
]


def parse_filename(name: str) -> tuple[str, int | None, int | None]:
    """Best-effort parse: returns (title, season, episode).
    Title is the part of the filename stripped of season/episode/tags."""
    base = pathlib.Path(name).stem
    # Strip release-group / quality brackets first so leftover ']' chars
    # don't end up in the title.
    base = re.sub(r"\[[^\]]*\]", " ", base)          # any [tag]
    base = re.sub(r"\([^\)]*\)", " ", base)          # any (tag)
    # Strip common quality/codec tags
    base = re.sub(r"\b(?:1080p|2160p|720p|480p|x26[45]|h\.?26[45]|hevc|"
                  r"AV1|10bit|8bit|HDR|DV|REMUX|BluRay|Blu-Ray|WEB(?:-?DL|RIP)?|"
                  r"HDTV|DTS(?:-?HD)?|TrueHD|Atmos|AC3|FLAC|AAC[\d.]*|EAC3)\b",
                  "", base, flags=re.IGNORECASE)
    # Pull S/E, then strip from title
    season, episode = None, None
    for pat in _SE_PATS:
        m = pat.search(base)
        if m:
            if len(m.groups()) == 2:
                season = int(m.group(1))
                episode = int(m.group(2))
            else:
                season = season or 1
                episode = int(m.group(1))
            base = base[:m.start()] + base[m.end():]
            break
    # Final cleanup: dots/underscores → spaces, collapse, trim trailing dashes
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"\s{2,}", " ", base)
    base = re.sub(r"\s*-\s*$", "", base.strip())
    return base.strip(" -"), season, episode


# ============================================================================
# Match resolution — given metadata, find a dandanplay episodeId
# ============================================================================
# Query normalisation before hitting dandanplay's search. Two media-server
# (Jellyfin/Plex/Emby) artifacts break matching:
#
#  1. Bracketed release year, e.g. "罗小黑战记 2 (2025)". dandanplay doesn't
#     expect it. We strip only *bracketed* years — a bare trailing year is
#     kept because some titles legitimately end in one ("2046",
#     "Blade Runner 2049").
#
#  2. A space between a CJK title and a sequel/season number, e.g.
#     "罗小黑战记 2". dandanplay tokenises the query on spaces, so
#     "罗小黑战记 2" matches the base title "罗小黑战记" (the WRONG season —
#     or zero results, as with "命运石之门 0") while the space-free
#     "罗小黑战记2" / "刀剑神域2" / "命运石之门0" precisely matches the sequel.
#     We join a space only when it sits between a CJK ideograph and a digit;
#     spaces inside Latin titles ("Re Zero", "Blade Runner 2049") are left
#     intact so they aren't mangled.
#
# Applied to the FINAL query — after the season number is appended — so the
# appended "刀剑神域 2" gets joined to "刀剑神域2" too. The filename path
# already drops "(...)" tags in parse_filename(); this also covers the
# Jellyfin path, which feeds the title in verbatim.
_YEAR_BRACKET_RE = re.compile(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*")
_CJK_DIGIT_SPACE_RE = re.compile(
    r"(?<=[一-鿿])\s+(?=\d)|(?<=\d)\s+(?=[一-鿿])"
)


def _normalize_search_query(query: str) -> str:
    """Strip a bracketed release year and join CJK↔digit spaces so the
    dandanplay search lands on the right title. Returns the input
    unchanged if normalisation would leave it empty (defensive)."""
    q = _YEAR_BRACKET_RE.sub(" ", query)
    q = re.sub(r"\s{2,}", " ", q).strip()
    q = _CJK_DIGIT_SPACE_RE.sub("", q)
    return q or query.strip()


def _search_with_season(title: str, season: int | None,
                        episode: int | None) -> list[dict]:
    """Run a single ddp search, appending the season number to non-S1 titles."""
    query = title.strip()
    if season and season > 1:
        query = f"{query} {season}"
    query = _normalize_search_query(query)
    log(f"searching: anime={query!r} episode={episode}")
    return ddp_search_episodes(query, episode)


def resolve_match(title: str, season: int | None, episode: int | None,
                  cache_key: str | None = None) -> int | None:
    """Search dandanplay and pick the best episode match. Caches result.

    Smart-match: if the primary search returns no animes AND we have a
    series-level alias on file (recorded by the user via manual search),
    retry the search using the aliased name. This lets future episodes
    of the same series auto-match without manual intervention even when
    the file's title doesn't match dandanplay's anime title."""
    if cache_key:
        hit = cache_get(cache_key)
        if hit:
            log(f"cache hit: {cache_key} → {hit}")
            return hit

    animes = _search_with_season(title, season, episode)
    if not animes:
        # Fallback: did the user previously map this series to a
        # different dandanplay anime title via manual search?
        alias = alias_get(title)
        if alias and alias.strip() != title.strip():
            log(f"no match for {title!r}; trying alias {alias!r}")
            animes = _search_with_season(alias, season, episode)
        if not animes:
            log("no matches")
            return None

    # Take the first anime, then within it pick the episode whose number
    # matches our episode (look at episodeTitle prefix or position).
    anime = animes[0]
    log(f"  → anime: {anime.get('animeTitle')} ({len(anime.get('episodes',[]))} eps)")

    eps = anime.get("episodes", [])
    if not eps:
        return None

    chosen = None
    if episode is not None:
        # Exact-number match in title (covers most series)
        for e in eps:
            t = str(e.get("episodeTitle", ""))
            m = re.match(r"^第(\d+)[话集]|^E[Pp]?\.?\s*(\d+)\b|^(\d+)\b", t)
            if m:
                num = int(next(g for g in m.groups() if g))
                if num == int(episode):
                    chosen = e
                    break
        # Fall back to positional (1-based)
        if chosen is None and 1 <= int(episode) <= len(eps):
            chosen = eps[int(episode) - 1]
    if chosen is None:
        chosen = eps[0]   # movie or single-episode

    log(f"  → episode: {chosen.get('episodeTitle')} (id={chosen.get('episodeId')})")
    eid = int(chosen["episodeId"])
    if cache_key:
        cache_put(cache_key, eid)
    return eid


# ============================================================================
# Comments → ASS conversion (lane assignment + animation)
# ============================================================================
@dataclass
class Comment:
    time: float          # seconds
    # Normalized mode: 1=rtl 4=bottom 5=top 6=ltr.
    # Per Bilibili spec, raw modes {1,2,3} are all "普通弹幕" (RTL scroll);
    # 7 (高级/positioned) and 8 (代码弹幕) are not renderable here. We
    # normalize at parse time so the renderer only has to handle 1/4/5/6.
    mode: int
    color: int           # 0xRRGGBB
    text: str
    source: str = "dandanplay"  # bilibili|gamer|dandanplay|other
    duration: float = 8.0       # default scroll duration (s)

    @classmethod
    def from_ddp(cls, raw: dict) -> "Comment | None":
        try:
            p = str(raw.get("p", ""))
            m = str(raw.get("m", "")).strip()
            if not p or not m:
                return None
            parts = p.split(",")
            t = float(parts[0])
            mode = _normalize_mode(int(parts[1]))
            if mode is None:
                return None
            color = int(parts[2])
            user = parts[3] if len(parts) > 3 else ""
            return cls(time=t, mode=mode, color=color, text=m,
                       source=_classify_source(user))
        except Exception:
            return None


def _normalize_mode(m: int) -> int | None:
    """Bilibili/dandanplay raw mode → renderable mode.
        1, 2, 3  → 1   (普通弹幕, RTL scroll — spec groups them together;
                        modern Bilibili web/app only emits mode 1, but
                        legacy dumps and some forwarders preserve 2/3)
        4        → 4   (底部, bottom-fixed)
        5        → 5   (顶部, top-fixed)
        6        → 6   (逆向, LTR scroll)
        7, 8, 9  → None (高级/代码/BAS — script-driven, can't render in ASS)
    Returns None for unrenderable modes so the caller drops them."""
    if m in (1, 2, 3):
        return 1
    if m in (4, 5, 6):
        return m
    return None


def _classify_source(user: str) -> str:
    """Classify a dandanplay comment's user-field tag into a source bucket.
    Mirrors Izumiko/Jellyfin-Danmaku's preProcessDanmaku:
        [BiliBili]…  → bilibili
        [Gamer]…     → gamer
        [Other…]…    → other  (any [Source] prefix that isn't BiliBili/Gamer)
        otherwise    → dandanplay  (native user-uploaded comment)"""
    if not user:
        return "dandanplay"
    if user.startswith("[BiliBili]"):
        return "bilibili"
    if user.startswith("[Gamer]"):
        return "gamer"
    if user.startswith("[") and "]" in user[1:]:
        return "other"
    return "dandanplay"


def _color_to_ass(rgb: int) -> str:
    """0xRRGGBB → ASS &HBBGGRR& (note: ASS is BGR not RGB)."""
    r = (rgb >> 16) & 0xff
    g = (rgb >> 8) & 0xff
    b = rgb & 0xff
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _ass_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - (h * 3600 + m * 60)
    return f"{h}:{m:02d}:{s:05.2f}"


def _measure_text_px(text: str, font_size: float, stroke_w: float) -> float:
    """Approximate the rendered pixel width of a comment for libass.

    libass uses fontconfig + freetype for actual measurement, which we
    can't replicate without a font lib. Empirical em-widths for the
    fonts we ship (Microsoft YaHei / Noto Sans CJK / DejaVu Sans):
        CJK ideographs / kana / hangul / fullwidth: ~1.00 em (square)
        Latin (printable ASCII):                    ~0.55 em (avg sans)
        Other (combining marks, control, etc.):     ~0.65 em (compromise)

    Stroke (\\bord) extends the visible bbox by stroke_w on every side,
    so it widens the effective occupied span by 2×stroke_w. We include
    that here so callers using the result for collision timing don't
    underestimate the real on-screen footprint."""
    if not text:
        return font_size  # at least one em wide so the lane isn't free instantly
    cjk = 0
    latin = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF        # CJK Unified Ideographs
            or 0x3400 <= cp <= 0x4DBF      # CJK Ext A
            or 0x3040 <= cp <= 0x30FF      # Hiragana + Katakana
            or 0xAC00 <= cp <= 0xD7AF      # Hangul Syllables
            or 0xFF00 <= cp <= 0xFFEF      # Halfwidth/Fullwidth Forms
            or 0x3000 <= cp <= 0x303F):    # CJK Symbols & Punctuation
            cjk += 1
        elif 0x20 <= cp <= 0x7E:
            latin += 1
        else:
            other += 1
    text_w = (cjk * 1.00 + latin * 0.55 + other * 0.65) * font_size
    return text_w + 2.0 * stroke_w


# Safety margins for scroll-lane collision timing. Real glyph metrics
# vary slightly from our em-based estimate (kerning, spacing tables,
# fallback fonts substituting wider glyphs for missing chars). These
# absorb that drift so the trailing edge of a previous comment never
# overlaps the leading edge of the next one on the same lane.
_LANE_SAFETY_PX = 16.0   # pixels of extra slack beyond measured width
_LANE_SAFETY_S = 0.15    # seconds of additional gap between same-lane reuses


def _escape_ass(text: str) -> str:
    return (text
            .replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("\n", " "))


def _filter_sources(comments: list["Comment"], disabled: list[str]) -> tuple[list["Comment"], int]:
    if not disabled:
        return comments, 0
    drop = {str(s).lower().strip() for s in disabled}
    out, dropped = [], 0
    for c in comments:
        if c.source in drop:
            dropped += 1
        else:
            out.append(c)
    return out, dropped


def _filter_keywords(comments: list["Comment"], patterns: list[str]) -> tuple[list["Comment"], int]:
    if not patterns:
        return comments, 0
    out, dropped = [], 0
    for c in comments:
        if any(fnmatch.fnmatch(c.text, p) for p in patterns):
            dropped += 1
        else:
            out.append(c)
    return out, dropped


def _dedup_comments(comments: list["Comment"], window: float, min_count: int
                    ) -> tuple[list["Comment"], int, dict[int, int]]:
    """Collapse same-text-near-time chains. Returns:
        (kept_list, num_dropped, count_by_kept_index)
    A chain extends as long as the next matching duplicate arrives within
    `window` seconds of the previous one. The first occurrence is kept;
    chains of size >= min_count get a "[+N]" annotation later."""
    sorted_c = sorted(comments, key=lambda c: c.time)
    kept: list[Comment] = []
    counts: dict[int, int] = {}            # kept-index → total in chain
    last_for_key: dict[tuple, tuple[float, int]] = {}  # key → (last_time, kept_idx)
    dropped = 0
    for c in sorted_c:
        key = (c.text, c.color, c.mode)
        if key in last_for_key:
            last_time, kept_idx = last_for_key[key]
            if c.time - last_time <= window:
                counts[kept_idx] += 1
                last_for_key[key] = (c.time, kept_idx)
                dropped += 1
                continue
        kept.append(c)
        last_for_key[key] = (c.time, len(kept) - 1)
        counts[len(kept) - 1] = 1
    return kept, dropped, counts


def comments_to_ass(comments: list[Comment], settings: dict) -> tuple[str, int]:
    """Render comments into an ASS subtitle string. Returns (ass_text, count)."""
    W = int(settings["screen_w"])
    H = int(settings["screen_h"])
    base_font = float(settings["font_size"])
    speed = float(settings["speed"])           # px/s @ W
    opacity = float(settings["opacity"])
    stroke = float(settings["stroke_width"])
    area = float(settings["area"])              # 0..1 of screen height
    # ASS Style is comma-separated, so we can't put a "Font1,Font2,Font3"
    # fallback chain in the Fontname field — libass would treat each comma
    # as a field separator and the entire Style row would become corrupt
    # (Fontsize parses as the second font name, etc., and \move tags fail
    # to render → all comments collapse to top-fixed). Take only the first
    # font name; fontconfig handles missing-glyph substitution at runtime,
    # so a single name is functionally equivalent to a chain on Linux.
    font = str(settings.get("font", "sans-serif")).split(",")[0].strip() or "sans-serif"
    render_mode = str(settings.get("render_mode", "original")).lower()
    show_modes = set(int(x) for x in settings.get("show_modes", [1, 4, 5, 6]))
    if not show_modes:
        show_modes = {1, 4, 5, 6}
        log("show_modes was empty; treating as all-on")

    # Source filter (drops by platform of origin: bilibili/gamer/dandanplay/other).
    comments, src_dropped = _filter_sources(
        comments, list(settings.get("disabled_sources") or []))
    if src_dropped:
        log(f"source filter dropped {src_dropped} comments")

    # Keyword filter (drops comments whose text matches any wildcard).
    comments, kw_dropped = _filter_keywords(
        comments, list(settings.get("filter_keywords") or []))
    if kw_dropped:
        log(f"keyword filter dropped {kw_dropped} comments")

    # Duplicate filter (collapse spam chains; mark big chains with [+N]).
    chain_counts: dict[int, int] = {}
    if settings.get("dedup", True):
        comments, dup_dropped, chain_counts = _dedup_comments(
            comments,
            window=float(settings.get("dedup_window", 1.0)),
            min_count=int(settings.get("dedup_min_count", 5)),
        )
        if dup_dropped:
            log(f"dedup collapsed {dup_dropped} duplicate comments")
    else:
        # No dedup: each comment is its own chain of size 1
        chain_counts = {i: 1 for i in range(len(comments))}

    # Density bucket limits (mirror upstream's `9 - level*2`)
    density = settings.get("density", "medium")
    bucket_limit = {"low": 5, "medium": 14, "high": 30}.get(density, 14)

    line_h = base_font * 1.15
    n_lanes = max(4, int((H * area) // line_h))
    # Three sets of lanes (scrolling, top-fixed, bottom-fixed) plus a
    # round-robin pointer per set so consecutive comments at similar
    # timestamps don't all stack at the top of the screen — they get
    # spread across the available lanes naturally.
    scroll_lane_free = [0.0] * n_lanes
    top_lane_free = [0.0] * n_lanes
    bot_lane_free = [0.0] * n_lanes
    rr_scroll = 0  # rolling pointer for scroll-band scattering
    bucket_count: dict[int, int] = {}        # second → count of placed danmaku

    _RR_STRIDE = 7   # coprime with 4/8/12/16/20/24 → spread within a band
    anti_overlap = bool(settings.get("anti_overlap", True))

    def _alloc_lane_fixed(lanes: list[float], t: float) -> int | None:
        """Sequential picker for top/bottom-fixed comments: scan lanes
        0..n-1 in order and take the first free one. Top-fixed thus
        stack tightly from the top edge downward; bottom-fixed from the
        bottom edge upward. Falls back to LRU if all are busy and
        anti_overlap is off."""
        n = len(lanes)
        for i in range(n):
            if lanes[i] <= t:
                return i
        if anti_overlap:
            return None
        return min(range(n), key=lambda i: lanes[i])

    def _alloc_lane_scroll(t: float) -> int | None:
        """Banded picker for RTL/LTR scroll: try the top 25% of the
        screen first, expand to 50% if all those lanes are still busy,
        then 75%, then the full 100% (capped by `area`). Within a band,
        a stride-7 round-robin scatters consecutive arrivals so they
        don't queue up on the same row.

        This produces tight clustering near the top when the comment
        rate is light, and graceful spreading downward as density grows.
        Lanes "release" once the trailing edge of the previous comment
        has fully entered the screen — see lane_clear_time below."""
        nonlocal rr_scroll
        n = n_lanes
        # Band cutoffs: ⌈n/4⌉, ⌈n/2⌉, ⌈3n/4⌉, n. Use ceil so very small
        # lane counts (e.g. n=4 → bands 1,2,3,4) still produce 4 distinct
        # bands instead of collapsing.
        bands = [max(1, (n + 3) // 4),
                 max(1, (n + 1) // 2),
                 max(1, (3 * n + 3) // 4),
                 n]
        for band_size in bands:
            start = rr_scroll % band_size
            for off in range(band_size):
                i = (start + off * _RR_STRIDE) % band_size
                if scroll_lane_free[i] <= t:
                    rr_scroll = (i + _RR_STRIDE) % n  # cycle across all lanes
                    return i
        if anti_overlap:
            return None
        return min(range(n), key=lambda i: scroll_lane_free[i])

    # Header — PlayResX/Y are the ASS coordinate system; mpv scales to display
    alpha_hex = f"{int(round((1 - opacity) * 255)):02X}"
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Single style; per-event overrides apply alpha/colour/font.
        # Font is a fallback chain; libass picks first available via fontconfig.
        f"Style: DM,{font},{int(base_font)},&H{alpha_hex}FFFFFF,"
        f"&H{alpha_hex}FFFFFF,&H{alpha_hex}000000,&H{alpha_hex}000000,"
        f"0,0,0,0,100,100,0,0,1,{stroke:.1f},0,7,0,0,0,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    out_lines: list[str] = []
    placed = 0
    skipped_density = 0
    skipped_mode = 0
    skipped_overlap = 0

    # If dedup found chains of size >= dedup_min_count, append "[+N]" to
    # the kept comment so the user can see the spike. We also record the
    # *identity* of each annotated Comment so the render loop below can
    # apply visual emphasis without re-parsing the text (and without
    # depending on the post-sort index).
    emphasis_on = bool(settings.get("aggregate_emphasis", True))
    font_bonus = float(settings.get("aggregate_font_bonus", 4))
    aggregated_ids: set[int] = set()
    min_count = int(settings.get("dedup_min_count", 5))
    for idx, count in chain_counts.items():
        if count >= min_count and 0 <= idx < len(comments):
            old = comments[idx]
            comments[idx] = Comment(time=old.time, mode=old.mode,
                                    color=old.color,
                                    text=f"{old.text}[+{count - 1}]")
            aggregated_ids.add(id(comments[idx]))

    # _dedup_comments already returns sorted-by-time, but be explicit
    # in case dedup is off.
    comments = sorted(comments, key=lambda c: c.time)

    for c in comments:
        # Capture the [+N] emphasis flag *before* render_mode rebinds c to
        # a fresh Comment (which would invalidate the id() lookup).
        is_aggregated = emphasis_on and id(c) in aggregated_ids
        # Apply render_mode override: convert all comments to one display
        # style (RTL by default — the universal "弹幕" expectation).
        # In "original" mode, honor the source tag and apply show_modes.
        if render_mode == "rtl":
            c = Comment(time=c.time, mode=1, color=c.color, text=c.text)
        elif render_mode == "ltr":
            c = Comment(time=c.time, mode=6, color=c.color, text=c.text)
        elif c.mode not in show_modes:
            skipped_mode += 1
            continue
        sec = int(c.time)
        if bucket_count.get(sec, 0) >= bucket_limit:
            skipped_density += 1
            continue

        text = _escape_ass(c.text)
        # Effective font size and emphasis override for this comment.
        # Aggregated [+N] lines render larger + bold so spikes stand out.
        # `ScaledBorderAndShadow: yes` (in the style header) means the
        # stroke scales with \fs automatically, so we don't override \bord;
        # we *do* pass the scaled stroke into the width measurement so
        # lane-collision timing matches the on-screen footprint.
        if is_aggregated:
            eff_font = base_font + font_bonus
            stroke_eff = stroke * (eff_font / base_font)
            # libass synthesizes bold by drawing slightly wider strokes when
            # no native bold face exists; either way the glyph advance grows
            # by a few percent. 1.08 is a conservative inflation.
            bold_w_factor = 1.08
            emphasis_tag = f"\\fs{int(round(eff_font))}\\b1"
        else:
            eff_font = base_font
            stroke_eff = stroke
            bold_w_factor = 1.0
            emphasis_tag = ""
        # Two width values:
        #   tw       — raw measured width, used for the \move start/end x
        #              coords (where the comment is *drawn*).
        #   tw_safe  — tw + safety pixels, used for lane-clear timing
        #              (so two same-lane comments never visually overlap
        #              even when our em-based measurement is a few px off).
        tw_measured = _measure_text_px(c.text, eff_font, stroke_eff) * bold_w_factor
        tw = max(50, int(round(tw_measured)))
        tw_safe = tw_measured + _LANE_SAFETY_PX
        col = _color_to_ass(c.color)

        if c.mode in (1, 6):  # scrolling
            duration = (W + tw) / speed
            end_t = c.time + duration
            # Lane reusable once the trailing edge of THIS comment has
            # crossed the screen edge that the NEXT comment will enter
            # from, plus a small safety gap.
            #   c.time + tw_safe / speed = trailing edge crosses the entry edge
            #   + _LANE_SAFETY_S         = breathing room for measurement drift
            lane_clear_time = c.time + (tw_safe / speed) + _LANE_SAFETY_S
            lane = _alloc_lane_scroll(c.time)
            if lane is None:
                skipped_overlap += 1
                continue
            scroll_lane_free[lane] = max(scroll_lane_free[lane], lane_clear_time)
            y = int(lane * line_h + line_h * 0.5)

            if c.mode == 1:   # rtl: starts at right, ends at left
                x_start, x_end = W + tw // 2, -tw // 2
            else:              # ltr
                x_start, x_end = -tw // 2, W + tw // 2

            tag = (f"{{\\an5\\move({x_start},{y},{x_end},{y})"
                   f"\\1c{col}\\3a&H{alpha_hex}{emphasis_tag}}}")
            out_lines.append(
                f"Dialogue: 0,{_ass_time(c.time)},{_ass_time(end_t)},"
                f"DM,,0,0,0,,{tag}{text}")

        elif c.mode == 5:  # top-fixed
            duration = 5.0
            end_t = c.time + duration
            lane = _alloc_lane_fixed(top_lane_free, c.time)
            if lane is None:
                skipped_overlap += 1
                continue
            top_lane_free[lane] = end_t
            y = int(lane * line_h + line_h * 0.5)
            tag = (f"{{\\an8\\pos({W//2},{y})"
                   f"\\1c{col}\\3a&H{alpha_hex}{emphasis_tag}}}")
            out_lines.append(
                f"Dialogue: 0,{_ass_time(c.time)},{_ass_time(end_t)},"
                f"DM,,0,0,0,,{tag}{text}")

        elif c.mode == 4:  # bottom-fixed
            duration = 5.0
            end_t = c.time + duration
            lane = _alloc_lane_fixed(bot_lane_free, c.time)
            if lane is None:
                skipped_overlap += 1
                continue
            bot_lane_free[lane] = end_t
            y = int(H - (lane * line_h + line_h * 0.5))
            tag = (f"{{\\an2\\pos({W//2},{y})"
                   f"\\1c{col}\\3a&H{alpha_hex}{emphasis_tag}}}")
            out_lines.append(
                f"Dialogue: 0,{_ass_time(c.time)},{_ass_time(end_t)},"
                f"DM,,0,0,0,,{tag}{text}")

        else:
            continue

        bucket_count[sec] = bucket_count.get(sec, 0) + 1
        placed += 1

    log(f"placed {placed}/{len(comments)} comments "
        f"(density-skipped {skipped_density}, mode-filtered {skipped_mode}, "
        f"overlap-dropped {skipped_overlap}, lanes={n_lanes})")
    return header + "\n".join(out_lines) + "\n", placed


# ============================================================================
# XML loader (Bilibili / standard danmaku XML format) — for local files
# ============================================================================
def load_xml_comments(path: str) -> list[Comment]:
    tree = ET.parse(path)
    root = tree.getroot()
    out: list[Comment] = []
    for d in root.findall(".//d"):
        p = d.get("p") or ""
        text = (d.text or "").strip()
        if not p or not text:
            continue
        parts = p.split(",")
        try:
            t = float(parts[0])
            mode = _normalize_mode(int(parts[1]))
            if mode is None:
                continue
            color = int(parts[3])
            out.append(Comment(time=t, mode=mode, color=color, text=text,
                               source="bilibili"))
        except Exception:
            continue
    return out


# ============================================================================
# CLI commands
# ============================================================================
def _emit_match(eid: int | None, series: str,
                season: int | None, episode: int | None) -> None:
    """Print the match result as a multi-line key:value record on stdout.
    Lua reads each line and captures the parsed series for alias recording.
    First line is always EPID:<id> on success or NONE on failure (kept for
    backwards compat with old Lua callers that match against the first line)."""
    print(f"EPID:{eid}" if eid else "NONE")
    if series:
        print(f"SERIES:{series}")
    if season is not None:
        print(f"SEASON:{season}")
    if episode is not None:
        print(f"EPISODE:{episode}")


def cmd_match_jellyfin(args) -> int:
    series = (args.title or "").strip()
    cache_key = f"jf::{series}::{args.season}::{args.episode}"
    eid = resolve_match(series, args.season, args.episode, cache_key)
    _emit_match(eid, series, args.season, args.episode)
    return 0 if eid else 1


def cmd_match_file(args) -> int:
    title, season, episode = parse_filename(args.path)
    if not title:
        log("could not parse filename")
        _emit_match(None, "", None, None)
        return 1
    log(f"parsed: title={title!r} season={season} episode={episode}")
    cache_key = f"file::{os.path.basename(args.path)}"
    eid = resolve_match(title, season, episode, cache_key)
    _emit_match(eid, title, season, episode)
    return 0 if eid else 1


def cmd_record_alias(args) -> int:
    """Persist a series → dandanplay-anime-title alias. Called by Lua after
    the user manually picks an anime via the search panel for a series
    whose auto-match was wrong or empty."""
    series = (args.series or "").strip()
    alias = (args.alias or "").strip()
    if not series or not alias:
        log("record-alias: empty series or alias")
        print("NOOP")
        return 1
    if series == alias:
        log(f"record-alias: series and alias identical ({series!r}) — no-op")
        print("NOOP")
        return 0
    alias_put(series, alias)
    log(f"recorded alias: {series!r} → {alias!r}")
    print("OK")
    return 0


def cmd_alias_list(args) -> int:
    """Dump current alias map as JSON for diagnostics."""
    p = _alias_path()
    if not p.is_file():
        print("{}")
        return 0
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_search(args) -> int:
    """Print one JSON line per candidate {animeId, animeTitle, episodes:[...]}."""
    # Normalise the query (strip bracketed year, join CJK↔digit spaces)
    # the same way the auto-match path does — e.g. when the search panel
    # pre-fills the auto-parsed title "罗小黑战记 2 (2025)".
    animes = ddp_search_episodes(_normalize_search_query(args.query))
    for a in animes:
        # Trim to essentials so the Lua menu doesn't get massive
        slim = {
            "animeTitle": a.get("animeTitle"),
            "type": a.get("type"),
            "episodes": [
                {"episodeId": e["episodeId"], "episodeTitle": e["episodeTitle"]}
                for e in a.get("episodes", [])
            ],
        }
        print(json.dumps(slim, ensure_ascii=False))
    return 0


def _shift_times(comments: list[Comment], offset: float) -> list[Comment]:
    if not offset:
        return comments
    log(f"applying time offset {offset:+.2f}s to {len(comments)} comments")
    return [Comment(time=c.time + offset, mode=c.mode, color=c.color,
                    text=c.text, source=c.source, duration=c.duration)
            for c in comments]


def cmd_fetch(args) -> int:
    settings = load_settings()
    log(f"fetching ddp episode {args.episode_id}")
    raw = ddp_get_comments(args.episode_id, ch_convert=int(settings["chConvert"]))
    comments = [c for c in (Comment.from_ddp(r) for r in raw) if c]
    log(f"got {len(comments)} parseable comments")
    comments = _shift_times(comments, float(args.offset or 0.0))
    ass, n = comments_to_ass(comments, settings)
    pathlib.Path(args.output).write_text(ass, encoding="utf-8")
    print(f"OK:{n}")
    return 0


def cmd_load_xml(args) -> int:
    settings = load_settings()
    comments = load_xml_comments(args.input)
    log(f"loaded {len(comments)} from XML")
    comments = _shift_times(comments, float(args.offset or 0.0))
    ass, n = comments_to_ass(comments, settings)
    pathlib.Path(args.output).write_text(ass, encoding="utf-8")
    print(f"OK:{n}")
    return 0


def cmd_check(args) -> int:
    """Quick health check: can we reach the API, and via which path?"""
    cfg = _load_config()
    if cfg["app_id"] and cfg["app_secret"]:
        log(f"auth: direct (HMAC signed) — AppId={cfg['app_id'][:6]}...")
    elif cfg["cors_proxy"]:
        log(f"auth: via CORS proxy → {cfg['cors_proxy']}")
        log("  (configure $CREDENTIALS_FILE with your own dandanplay AppId "
            "to switch to direct API)".replace("$CREDENTIALS_FILE", str(CREDENTIALS_FILE)))
    else:
        log("no AppId and no CORS proxy — cannot make API calls")
        print("NO_CONFIG")
        return 1
    try:
        animes = ddp_search_episodes("test")
        log(f"  → search returned {len(animes)} animes")
        print("OK")
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP_{e.code}")
        log(f"http error: {e.code} {e.reason}")
        return 2
    except Exception as e:
        print(f"ERROR:{e!r}")
        log(f"unexpected: {e!r}")
        return 3


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("match-jellyfin")
    s.add_argument("title")
    s.add_argument("season", nargs="?", type=int, default=None)
    s.add_argument("episode", nargs="?", type=int, default=None)
    s.set_defaults(func=cmd_match_jellyfin)

    s = sub.add_parser("match-file")
    s.add_argument("path")
    s.set_defaults(func=cmd_match_file)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("fetch")
    s.add_argument("episode_id", type=int)
    s.add_argument("output")
    s.add_argument("--offset", type=float, default=0.0,
                   help="seconds to add to every comment time (per-episode)")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("load-xml")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--offset", type=float, default=0.0,
                   help="seconds to add to every comment time")
    s.set_defaults(func=cmd_load_xml)

    s = sub.add_parser("check")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("record-alias",
        help="map a parsed-series-name to a dandanplay anime title for "
             "future auto-match fallback")
    s.add_argument("series", help="series name as parsed by auto-match")
    s.add_argument("alias",  help="dandanplay anime title chosen via manual search")
    s.set_defaults(func=cmd_record_alias)

    s = sub.add_parser("alias-list",
        help="dump the alias map as JSON")
    s.set_defaults(func=cmd_alias_list)

    args = p.parse_args()
    try:
        return args.func(args)
    except urllib.error.HTTPError as e:
        log(f"http error: {e.code} {e.reason}")
        print(f"ERROR:http_{e.code}")
        return 2
    except urllib.error.URLError as e:
        log(f"network error: {e}")
        print("ERROR:network")
        return 2
    except Exception as e:
        log(f"unexpected error: {e!r}")
        print(f"ERROR:{e!r}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
