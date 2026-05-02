# mpv-dandanplay-danmaku

[简体中文](./README.zh-CN.md)

Bullet-chat (danmaku / 弹幕) overlay for **mpv**, fed by
[dandanplay](https://www.dandanplay.com/) — the same comment aggregator
used by Bilibili, Acfun, Bahamut, Tucao, iQIYI etc. Auto-matches the
playing video to a dandanplay episode, downloads the comments, and
renders them as a moving ASS subtitle track.

![preview](./docs/preview.jpg)

## Acknowledgements

This project is a **port and re-implementation of large parts of
[Izumiko/Jellyfin-Danmaku](https://github.com/Izumiko/Jellyfin-Danmaku)**
— the popular browser-side userscript that adds danmaku to Jellyfin's
web UI. The match algorithm, the source-platform filter
(B站 / 巴哈 / 弹弹 / 其他), the source-tag classification, the comment
de-duplication heuristic, the anti-overlap lane filter, and the CORS
proxy fallback all derive directly from that project.

If you use this script, **please also star and support that upstream
project** — they did the hard product-design work, we just translated
it from JavaScript-in-the-browser to Lua-in-mpv.

## Features

- **Auto-match** by filename or, if launched via `jellyfin-mpv-shim`,
  by Jellyfin metadata.
- **Manual fuzzy search** (Ctrl+F10) with two-stage picker:
  search → choose anime → choose episode.
- **Smart-match alias fallback**: when your library's title for a show
  differs from dandanplay's anime title, the first time you manually
  pick the right one we remember the mapping. Future episodes of the
  same series whose primary search returns nothing automatically retry
  using the picked title — no manual search needed. Mapping is stored
  in `aliases.json` (cache dir) and persists across sessions.
- **In-player settings panel** (Shift+F10) for opacity / font size /
  speed / density / display area / render mode / anti-overlap /
  source filter / dedup / per-episode time offset, etc.
- **Renders inside mpv as a secondary subtitle** so it stacks on top
  of any normal subtitles instead of replacing them.
- **Anti-overlap** (default on): comments that can't fit a free lane
  are dropped instead of stacking visually.
- **CJK-aware lane allocation**: width is measured per-glyph (CJK = 1.0
  em, ASCII = 0.55 em) so two consecutive Chinese comments on the same
  lane never overlap.
- **Banded scroll lanes**: comments cluster in the top quarter of the
  screen and only spread downward when traffic is heavy.
- **Smart dedup**: collapses spam chains; chains of ≥ N hits get a
  `[+N]` annotation so meme spikes are still visible.
- **Per-episode time offset**, persisted across sessions.
- **Source filter**: hide comments by origin platform (B站/巴哈/弹弹/其他).
- **Keyword filter** with shell-style wildcards.

## Requirements

- mpv ≥ 0.40 (0.39 mostly works, but `mp.input.get` for free-form
  text input only landed in 0.39).
- Python 3.8+ on PATH (the helper uses only the stdlib — no `pip
  install` needed).
- Internet access to dandanplay (or a CORS proxy you control).

## Installation

### One-liner (Linux / macOS / Windows)

```bash
git clone https://github.com/Cryspia/mpv-dandanplay-danmaku.git
cd mpv-dandanplay-danmaku
python3 install.py
```

The installer copies the script bundle into mpv's config dir
(`scripts/dandanplay/`) and seeds the JSON config files. It detects:

| Platform | mpv config dir |
|---|---|
| Linux | `~/.config/mpv` (honors `$MPV_HOME`) |
| macOS | `~/.config/mpv` (honors `$MPV_HOME`) |
| Windows | `%APPDATA%\mpv` (honors `%MPV_HOME%`) |

Restart mpv (or `jellyfin-mpv-shim`) and you're done.

### Manual install

Copy two files into your mpv config dir and you're ready:

```
<mpv-config>/
└── scripts/
    └── dandanplay/
        ├── main.lua            # from this repo's scripts/dandanplay/
        └── danmaku_helper.py
```

mpv treats `scripts/dandanplay/` as a script bundle — the directory IS
the script. No init / require / autoload steps required.

Optionally drop `examples/danmaku-config.json` and
`examples/danmaku-settings.json` into `<mpv-config>/` to seed defaults.

### Other installer commands

```bash
python3 install.py --status     # show what's installed
python3 install.py --uninstall  # remove bundle (preserves credentials!)
```

## Usage

### Auto-match

When mpv starts playing a file, the script tries to match it:

1. **Jellyfin** (when launched via `jellyfin-mpv-shim`): parses
   `media-title` like `Series Name s01e03 - Episode Title` for series,
   season, episode → searches dandanplay → fetches matching comments.
2. **Local file**: parses the filename (e.g. `[Group] Series.S02E05.[1080p].mkv`)
   for the same. Common patterns recognized: `S01E03`, `1x03`, `EP03`,
   `第03话`, `[03]`, `- 03 -`.

Successful matches are cached, so the same series/episode resolves
instantly next time.

### Manual search (Ctrl+F10)

If auto-match misses, hit `Ctrl+F10`:

1. A text input appears: type any fuzzy query (`尖帽子`, `frieren`, …).
2. **Stage 1 — anime picker**: every anime that matched, with episode
   count and type. Use `↑↓` / `PgUp/PgDn` / `1-9` to pick.
3. **Stage 2 — episode picker**: full episode list of the chosen anime.
   `Enter` loads the danmaku.

The match is cached so the same series + episode auto-loads next time.

### Smart-match alias

After you manually pick "尖帽子的魔法工房" for a file whose library
title is "Magic Workshop", the script records that mapping. The next
time you play another episode of the same series (e.g.
`Magic Workshop S1E6.mkv`):

1. Auto-match parses "Magic Workshop" and queries dandanplay.
2. Zero hits → it consults `aliases.json` and finds the prior mapping
   "Magic Workshop → 尖帽子的魔法工房".
3. Re-queries with "尖帽子的魔法工房" + episode 6 → match → loads.

No manual intervention. The alias map lives at `<cache>/aliases.json`,
inspectable via `python3 danmaku_helper.py alias-list`. Edit the file
directly to remove or adjust entries.

### Keybindings

| Key | Action |
|---|---|
| **F10** | toggle danmaku visibility |
| **Shift+F10** | settings panel |
| **Ctrl+F10** | manual search |
| **Esc** (in panels) | close |

The settings panel covers everything: `透明度` opacity, `字号` font
size (Enter for free-form input), `速度` speed, `密度` density,
`显示区域` display area, `渲染模式` render mode (original / forced RTL
/ forced LTR), `弹幕防重叠` anti-overlap, `繁简转换` traditional ↔
simplified, `时间偏移` time offset (Enter for free-form input),
`弹幕来源` source filter, `显示模式` mode visibility, `弹幕去重` dedup
on/off + window + threshold.

A clickable **`弹`** icon also appears in the top-right when mpv's OSC
is visible — click it to toggle.

### Configuration

The script reads three JSON files from your mpv config directory:

| File | Purpose |
|---|---|
| `danmaku-settings.json` | per-installation defaults (panel writes to it) |
| `danmaku-config.json` | CORS proxy URL |
| `danmaku-credentials.json` | dandanplay AppId/AppSecret (if you have them) |

`filter_keywords` is editable here only — fnmatch wildcards (`*`, `?`,
`[abc]`); whole-text match. See `examples/danmaku-settings.json` for
the full reference.

## Strongly recommended: register your own dandanplay AppId

**By default this script uses the CORS-proxy CloudFlare Worker
generously kept online by upstream Izumiko/Jellyfin-Danmaku
(`ddplay-api.930524.xyz`).** That proxy bundles dandanplay's HMAC v2
auth on the server side, so unauthenticated clients can still query.
This is convenient out-of-the-box but has two downsides:

1. **It's a free service kindly hosted by someone else.** If the script
   becomes popular enough that traffic burdens that endpoint, the
   proxy may be rate-limited or shut down.
2. **You depend on its continued availability.** Anything that breaks
   the proxy (DNS, certs, the maintainer moving on) breaks danmaku for
   you, and you can't fix it.

**The right thing to do is register your own dandanplay AppId**:

1. Email `kaedei@dandanplay.net` requesting an AppId / AppSecret per
   the [open platform docs](https://doc.dandanplay.com/open/) (1–3 day
   approval).
2. Once approved, in your mpv config dir:
   ```bash
   cp danmaku-credentials.json.example danmaku-credentials.json
   $EDITOR danmaku-credentials.json   # fill in app_id and app_secret
   ```
3. Restart mpv. The helper now signs requests itself with HMAC-SHA256
   and goes direct to `api.dandanplay.net`. The CORS proxy is bypassed
   entirely.

Direct API mode is also faster (no extra hop) and more private (your
queries only hit dandanplay, not a third-party worker).

If you'd rather self-host the CORS proxy, deploy
[Izumiko's `cf_worker.js`](https://github.com/Izumiko/Jellyfin-Danmaku/blob/master/cf_worker.js)
to your own CloudFlare Workers account and put its URL in
`danmaku-config.json`'s `cors_proxy` field.

## Troubleshooting

- **No match on Jellyfin URLs**: a 1.5 s grace period after `file-loaded`
  is built in (mpv's `media-title` doesn't contain the proper title at
  the exact moment of file-loaded for network streams). If it still
  fails, the script writes a diagnostic line to
  `$TMPDIR/danmaku-debug.log` (or the equivalent on your platform).
  Paste it into a bug report.
- **Comments stack at the top, no scrolling**: this means
  `secondary-sub-ass-override` is set to `strip` (mpv's default). The
  script sets it to `no` automatically when it loads the ASS, but if
  you load the ASS some other way, add this to your `mpv.conf`:
  ```
  secondary-sub-ass-override=no
  secondary-sub-pos=0
  ```
- **Wrong Python**: by default the script invokes `python3` (Linux/macOS)
  or `python` (Windows). Override with the env var
  `DANMAKU_PYTHON=/path/to/python`.
- **Custom mpv config dir**: honored via `$MPV_HOME` like mpv itself.

## Project layout

```
mpv-dandanplay-danmaku/
├── README.md                       # this file
├── README.zh-CN.md                 # Chinese version
├── LICENSE                         # MIT, with attribution
├── install.py                      # cross-platform installer
├── scripts/
│   └── dandanplay/                 # mpv script bundle (the deliverable)
│       ├── main.lua
│       └── danmaku_helper.py
├── examples/
│   ├── danmaku-config.json
│   ├── danmaku-credentials.json
│   └── danmaku-settings.json
└── docs/
    └── preview.jpg
```

## License

MIT — see [LICENSE](./LICENSE). Significant portions of the algorithmic
design derive from `Izumiko/Jellyfin-Danmaku`; please credit and support
that project.
