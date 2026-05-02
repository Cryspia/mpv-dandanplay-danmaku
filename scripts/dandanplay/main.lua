-- main.lua — mpv-side integration for the dandanplay danmaku helper.
--
-- Responsibilities:
--   1) On file load, try to auto-match the playing video to a dandanplay
--      episode (using Jellyfin metadata if shim spawned us, filename
--      otherwise) and load the rendered ASS as a secondary subtitle.
--   2) Provide F10 / Shift+F10 / Ctrl+F10 keybindings + a small clickable
--      "弹" icon overlay in the top-right corner that mirrors the
--      visibility of mpv's OSC.
--   3) Persist user-tunable settings (density / opacity / font size) to
--      a JSON file the helper reads.
--
-- Talks to danmaku_helper.py via mp.command_native({name="subprocess",…}).

local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"

-- mp.input is a separate module (added in mpv 0.39) that must be
-- explicitly required — it is NOT auto-attached to the `mp` global.
-- pcall guards against older mpv where the module file is absent.
local _ok_input, input_mod = pcall(require, "mp.input")
local input = _ok_input and input_mod or nil

-- ============================================================================
-- Cross-platform path helpers
-- ============================================================================
local IS_WINDOWS = (package.config:sub(1, 1) == "\\")
local PSEP = IS_WINDOWS and "\\" or "/"

local function path_join(...)
    return table.concat({...}, PSEP)
end

-- Locate this script bundle's own directory (where main.lua + helper live).
-- mpv 0.40+ exposes mp.get_script_directory(); fall back to introspecting
-- debug.getinfo when running on older mpv.
local function script_dir()
    if mp.get_script_directory then
        local d = mp.get_script_directory()
        if d and d ~= "" then return d end
    end
    local src = debug.getinfo(1, "S").source
    if src and src:sub(1, 1) == "@" then src = src:sub(2) end
    return (src or "."):match("^(.*)[/\\][^/\\]+$") or "."
end

-- mpv config dir: lets `mp.find_config_file("")` resolve to the right place
-- on Linux (~/.config/mpv) / macOS (~/.config/mpv) / Windows (%APPDATA%\mpv),
-- honoring $MPV_HOME / --config-dir if the user set them.
local function mpv_config_dir()
    -- Resolve a known file: any sibling under the config dir works.
    -- We try a couple of names and parse the parent out.
    for _, name in ipairs({"mpv.conf", "input.conf", "scripts"}) do
        local p = mp.find_config_file(name)
        if p then
            local parent = p:match("^(.*)[/\\][^/\\]+$")
            if parent and parent ~= "" then return parent end
        end
    end
    -- Last-resort fallback (Linux/macOS layout).
    local home = os.getenv("HOME") or os.getenv("USERPROFILE") or "."
    return path_join(home, ".config", "mpv")
end

-- Cache dir for non-config state (match cache, per-episode offsets,
-- debug log). Honors $XDG_CACHE_HOME on Linux, $LOCALAPPDATA on Windows,
-- ~/Library/Caches on macOS.
local function cache_dir()
    local override = os.getenv("DANMAKU_CACHE_DIR")
    if override and override ~= "" then return override end
    if IS_WINDOWS then
        local base = os.getenv("LOCALAPPDATA")
        if base then return path_join(base, "mpv-danmaku") end
    end
    local home = os.getenv("HOME") or os.getenv("USERPROFILE") or "."
    -- Treat macOS as Linux-ish — most users follow XDG conventions.
    local xdg = os.getenv("XDG_CACHE_HOME")
    if xdg and xdg ~= "" then return path_join(xdg, "mpv-danmaku") end
    return path_join(home, ".cache", "mpv-danmaku")
end

-- Platform-aware temp dir for ASS output and the no-match debug log.
local function temp_dir()
    local d = os.getenv("TMPDIR") or os.getenv("TEMP") or os.getenv("TMP")
    if d and d ~= "" then return d end
    return IS_WINDOWS and "C:\\Windows\\Temp" or "/tmp"
end

-- ============================================================================
-- Constants
-- ============================================================================
local SCRIPT_DIR = script_dir()
local CFG_DIR = mpv_config_dir()
local CACHE_DIR = cache_dir()
local TMP_DIR = temp_dir()
local SETTINGS_FILE = path_join(CFG_DIR, "danmaku-settings.json")
-- Per-episode time offsets (seconds). Indexed by episodeId.
local OFFSETS_FILE = path_join(CACHE_DIR, "offsets.json")
-- Debug log written when auto-match fails (paste contents into bug report).
local DEBUG_LOG = path_join(TMP_DIR, "danmaku-debug.log")

-- Helper script lives next to main.lua in the bundle.
local HELPER = path_join(SCRIPT_DIR, "danmaku_helper.py")

-- Python interpreter. Default to "python3" (Linux/macOS) and fall back
-- to "python" (Windows). Override with $DANMAKU_PYTHON if needed.
local PYTHON = (function()
    local override = os.getenv("DANMAKU_PYTHON")
    if override and override ~= "" then return override end
    if IS_WINDOWS then return "python" end
    return "python3"
end)()

-- ============================================================================
-- Default settings (mirror upstream Izumiko/Jellyfin-Danmaku)
-- ============================================================================
local DEFAULT_SETTINGS = {
    enabled = true,         -- danmaku on/off (this script-side; persists)
    opacity = 0.75,
    speed = 144,
    font_size = 36,
    density = "medium",     -- low | medium | high
    area = 0.8,
    chConvert = 1,
    stroke_width = 2.0,
    font = "Microsoft YaHei,Noto Sans CJK SC,sans-serif",
    -- render_mode: "original" (default, honor each comment's source mode tag),
    -- "rtl" (force everyone right→left), or "ltr".
    render_mode = "original",
    show_modes = {1, 4, 5, 6},
    -- Source filter (Izumiko/Jellyfin-Danmaku parity): names listed here are
    -- *disabled*. Valid: "bilibili", "gamer", "dandanplay", "other".
    disabled_sources = {},
    -- Wildcard keyword filter (fnmatch syntax). Persisted; survives uninstall.
    filter_keywords = {},
    -- Duplicate filter: collapse same-text-near-time chains; suffix [+N].
    dedup = true,
    dedup_window = 1.0,
    dedup_min_count = 5,
    -- Anti-overlap: when true, comments that can't fit in any free lane
    -- are dropped instead of stacking. Within-pool only (a top-fixed and
    -- a scroll on the same row are different pools and can still clash).
    -- Default on: cleaner reading at the cost of fewer comments.
    anti_overlap = true,
    screen_w = 1920,
    screen_h = 1080,
}

local settings = {}
for k, v in pairs(DEFAULT_SETTINGS) do settings[k] = v end

-- ============================================================================
-- State
-- ============================================================================
local state = {
    sub_id = nil,                  -- mpv sub-track index where ours lives
    ass_path = nil,                -- on-disk path of the loaded ASS
    last_match_episode = nil,      -- ddp episodeId for current file
    last_count = 0,                -- comments rendered for current file
    last_status = "idle",          -- idle | loading | ok | none | error | no_config
    overlay_visible = false,       -- icon shown right now?
    osc_visible = false,           -- approximation tracked via cursor activity
    cursor_last = 0,               -- mp.get_time() of last mouse activity
    overlay_obj = nil,             -- mp osd-overlay object
    file_id = 0,                   -- bumped on each file-loaded; cancels stale searches
    parsed_series = nil,           -- helper's SERIES: line from last match attempt
    parsed_season = nil,
    parsed_episode = nil,
}

-- Coordinates for the icon (top-right corner, slightly inset). Click area
-- is computed from this on every mouse-btn0 press.
local ICON_W, ICON_H = 70, 50
local ICON_PAD = 24

-- ============================================================================
-- Settings persistence
-- ============================================================================
local function settings_save()
    local f = io.open(SETTINGS_FILE, "w")
    if not f then return end
    -- Tiny JSON encode (mp.utils.format_json exists in mpv 0.40+)
    f:write(utils.format_json(settings))
    f:close()
end

local function settings_load()
    local f = io.open(SETTINGS_FILE, "r")
    if not f then return end
    local data = f:read("*a"); f:close()
    if data == "" then return end
    local parsed, err = utils.parse_json(data)
    if not parsed then
        msg.warn("settings parse error: " .. tostring(err))
        return
    end
    for k, v in pairs(parsed) do settings[k] = v end
end

settings_load()

-- ============================================================================
-- Per-episode time offsets (seconds added to every comment's time).
-- Persisted across sessions in OFFSETS_FILE; survives uninstall.
-- ============================================================================
local offsets = {}    -- map: tostring(episodeId) → number(seconds)
local function offsets_load()
    local f = io.open(OFFSETS_FILE, "r")
    if not f then return end
    local data = f:read("*a"); f:close()
    if data == "" then return end
    local parsed = utils.parse_json(data)
    if type(parsed) == "table" then offsets = parsed end
end
local function ensure_dir(path)
    -- mp.utils.subprocess is the cross-platform way to spawn a command;
    -- cmd.exe and POSIX sh both happen to support `mkdir` so we use it.
    local cmd
    if IS_WINDOWS then
        -- `mkdir` on Windows fails if the dir exists, so suppress error.
        cmd = string.format('cmd /c if not exist "%s" mkdir "%s"', path, path)
    else
        cmd = string.format("mkdir -p '%s' 2>/dev/null", path)
    end
    os.execute(cmd)
end

local function offsets_save()
    ensure_dir(CACHE_DIR)
    local f = io.open(OFFSETS_FILE, "w")
    if not f then return end
    f:write(utils.format_json(offsets))
    f:close()
end
local function offset_get(epid)
    if not epid then return 0 end
    return tonumber(offsets[tostring(epid)] or 0) or 0
end
local function offset_set(epid, seconds)
    if not epid then return end
    if seconds == 0 then
        offsets[tostring(epid)] = nil  -- don't bloat the file with zeros
    else
        offsets[tostring(epid)] = seconds
    end
    offsets_save()
end
offsets_load()

-- ASS \fn override expects a single font name. settings.font may be a
-- comma-separated chain ("Microsoft YaHei,Noto Sans CJK SC,sans-serif")
-- which libass treats as one literal name → glyphs go missing. Strip
-- to the first name; fontconfig handles missing-glyph substitution.
local function ass_font()
    local s = settings.font
    if not s or s == "" then return "sans-serif" end
    local first = s:match("^([^,]+)")
    if not first then return "sans-serif" end
    return (first:gsub("^%s+", ""):gsub("%s+$", ""))
end

-- ============================================================================
-- ASS overlay drawing — the clickable "弹" icon
-- ============================================================================
local function update_icon_overlay()
    if not state.overlay_obj then
        state.overlay_obj = mp.create_osd_overlay("ass-events")
    end
    local ov = state.overlay_obj

    local W = mp.get_property_number("osd-width", 1920)
    local H = mp.get_property_number("osd-height", 1080)
    ov.res_x = W
    ov.res_y = H

    -- Show only when "OSC-likely-visible": cursor moved within last 2s.
    -- (mpv exposes cursor-autohide-fs-only and similar but not a clean
    -- "is OSC visible" property. The 2s heuristic mirrors the OSC's own
    -- reveal/hide window.)
    local now = mp.get_time()
    local active = (now - state.cursor_last) < 2.0
    if not active and not state.overlay_visible then
        ov.data = ""
        ov:update()
        return
    end
    if not active then
        state.overlay_visible = false
        ov.data = ""
        ov:update()
        return
    end
    state.overlay_visible = true

    local x = W - ICON_W - ICON_PAD
    local y = ICON_PAD
    local fg, bg, bg_alpha
    if not settings.enabled then
        fg = "&H888888&"; bg = "&H222222&"; bg_alpha = "&H80&"
    elseif state.last_status == "ok" then
        fg = "&HFFFFFF&"; bg = "&H553388&"; bg_alpha = "&H40&"  -- purple-ish
    elseif state.last_status == "loading" then
        fg = "&HFFFFFF&"; bg = "&H666666&"; bg_alpha = "&H40&"
    elseif state.last_status == "none" or state.last_status == "no_config" then
        fg = "&HBBBBBB&"; bg = "&H222222&"; bg_alpha = "&H80&"
    else
        fg = "&HFFFFFF&"; bg = "&H333333&"; bg_alpha = "&H80&"
    end

    -- Rounded box drawing using ASS \p1 path commands (corners as bezier).
    local r = 8  -- corner radius
    local rect = string.format(
        "{\\an7\\pos(%d,%d)\\bord0\\1c%s\\1a%s\\p1}m %d 0 l %d 0 b %d 0 %d %d %d %d "
        .. "l %d %d b %d %d %d %d %d %d "
        .. "l %d %d b 0 %d 0 %d %d %d "
        .. "l 0 %d b 0 0 0 0 %d 0{\\p0}",
        x, y, bg, bg_alpha,
        r, ICON_W - r, ICON_W, ICON_W, 0, ICON_W, r,
        ICON_W, ICON_H - r, ICON_W, ICON_H, ICON_W, ICON_H, ICON_W - r, ICON_H,
        r, ICON_H, ICON_H, ICON_H - r, 0, ICON_H,
        r, r
    )

    -- Center the text "弹" inside the box. ass \an5 = center anchor.
    local cx = x + ICON_W / 2
    local cy = y + ICON_H / 2
    local label = string.format(
        "{\\an5\\pos(%d,%d)\\fn%s\\fs28\\b1\\bord1\\3c&H000000&\\1c%s}弹",
        cx,
        cy,
        ass_font(),
        fg)

    -- Tiny status hint underneath
    local hint = ""
    if state.last_status == "ok" and state.last_count > 0 then
        hint = string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs14\\1c&HCCCCCC&\\bord1\\3c&H000000&}%d",
            x + 2,
            y + ICON_H + 2,
            ass_font(),
            state.last_count)
    elseif state.last_status == "none" then
        hint = string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs12\\1c&HFF8888&\\bord1\\3c&H000000&}未匹配",
            x + 2,
            y + ICON_H + 2,
            ass_font())
    elseif state.last_status == "no_config" then
        hint = string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs12\\1c&HFFAA00&\\bord1\\3c&H000000&}无 token",
            x + 2,
            y + ICON_H + 2,
            ass_font())
    elseif state.last_status == "loading" then
        hint = string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs12\\1c&HCCCCCC&\\bord1\\3c&H000000&}搜索中...",
            x + 2,
            y + ICON_H + 2,
            ass_font())
    end

    ov.data = rect .. "\n" .. label .. (hint ~= "" and ("\n" .. hint) or "")
    ov:update()
end

-- ============================================================================
-- Helper subprocess wrapper
-- ============================================================================
local function helper_run_sync(args)
    local full = {PYTHON, HELPER}
    for _, a in ipairs(args) do table.insert(full, a) end
    msg.debug("helper: " .. table.concat(full, " "))
    local r = mp.command_native({
        name = "subprocess",
        args = full,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    })
    return r  -- {status, stdout, stderr, error_string, killed_by_us}
end

-- Async variant for matching (so we don't block file-load)
local function helper_run_async(args, callback)
    local full = {PYTHON, HELPER}
    for _, a in ipairs(args) do table.insert(full, a) end
    mp.command_native_async({
        name = "subprocess",
        args = full,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    }, callback)
end

-- ============================================================================
-- Subtitle track management
-- ============================================================================
-- Remove the current danmaku sub track from mpv. Does NOT delete the
-- on-disk ASS file — when reloading after a settings change, the helper
-- writes the new ASS to the SAME path ($TMP/dmk-<id>.ass), and deleting
-- it here would erase the freshly-written content right before sub-add
-- tries to load it. The temp file gets cleaned up by the OS at boot.
local function unload_current_sub()
    if state.sub_id ~= nil then
        local n = mp.get_property_number("track-list/count", 0)
        for i = 0, n - 1 do
            local k = string.format("track-list/%d/", i)
            local typ = mp.get_property(k .. "type")
            local fname = mp.get_property(k .. "external-filename")
            if typ == "sub" and fname == state.ass_path then
                local id = mp.get_property_number(k .. "id")
                if id then
                    mp.commandv("sub-remove", tostring(id))
                end
                break
            end
        end
        state.sub_id = nil
    end
    state.ass_path = nil
end

local function load_ass(path, count)
    unload_current_sub()
    state.ass_path = path
    state.last_count = count
    -- CRITICAL: mpv's secondary-sub-ass-override defaults to "strip", which
    -- removes all \move \pos \an \1c overrides from secondary-sub events.
    -- Our entire ASS is built from those overrides, so without this our
    -- danmaku rendering collapses (every event becomes plain text at the
    -- Style-default position; libass auto-stacks them top-down). Setting
    -- "no" tells mpv to render exactly what the script says.
    mp.set_property("secondary-sub-ass-override", "no")
    -- Pin secondary sub to top-left at script's PlayRes, NOT shifted by
    -- secondary-sub-pos / sub-margin-y, so our pixel-precise \move and
    -- \pos coords land where we computed them.
    mp.set_property_number("secondary-sub-pos", 0)
    -- Use sub-add with selection mode "auto" (load but don't auto-select)
    -- so it doesn't replace user's primary subtitle. Then enable visibility
    -- via secondary sub.
    local title = string.format("弹幕 (%d)", count)
    mp.commandv("sub-add", path, "auto", title)
    -- Find the index of the sub we just added
    local n = mp.get_property_number("track-list/count", 0)
    for i = 0, n - 1 do
        local k = string.format("track-list/%d/", i)
        if mp.get_property(k .. "external-filename") == path then
            state.sub_id = mp.get_property_number(k .. "id")
            break
        end
    end
    -- mpv only shows one sub track at a time by default. Enable our track
    -- as the SECONDARY subtitle so it stacks on top of any normal subtitles.
    if state.sub_id then
        mp.set_property("secondary-sid", tostring(state.sub_id))
    end
end

local function set_visible(on)
    settings.enabled = on
    settings_save()
    if state.sub_id then
        mp.set_property("secondary-sub-visibility", on and "yes" or "no")
    end
    update_icon_overlay()
end

-- ============================================================================
-- Match flow
-- ============================================================================
local function detect_source()
    -- Returns "jellyfin" | "local" | "stream", plus a metadata dict
    local path = mp.get_property("path", "")
    local title = mp.get_property("media-title", "") or ""
    if path:match("^https?://") then
        if path:lower():find("/items/") or path:lower():find("/videos/")
           or path:lower():find("jellyfin") then
            return "jellyfin", { title = title, path = path }
        end
        return "stream", { title = title, path = path }
    end
    return "local", { path = path, basename = mp.get_property("filename", "") }
end

-- Try to extract season/episode from a Jellyfin-format media title like
-- "Series Name S02E03 - Episode Title" or "Series Name - 03 - Title".
local function parse_jellyfin_title(t)
    local s, e = t:match("[Ss](%d+)[Ee](%d+)")
    if s and e then
        local series = t:sub(1, t:find("[Ss]%d+[Ee]%d+") - 1)
                       :gsub("%s*[%-:]%s*$", "")
                       :gsub("%s+$", "")
        return series, tonumber(s), tonumber(e)
    end
    -- Match "<title> - 03 - <ep title>"
    local series2, ep = t:match("(.-)%s*%-%s*(%d+)%s*%-")
    if series2 and ep then
        return series2:gsub("%s+$", ""), 1, tonumber(ep)
    end
    return t, nil, nil
end

local function trigger_match()
    state.file_id = state.file_id + 1
    local mine = state.file_id
    state.last_status = "loading"
    state.last_count = 0
    update_icon_overlay()

    local source, meta = detect_source()
    msg.info(string.format("source=%s  path=%q  media-title=%q",
                           source, mp.get_property("path",""),
                           mp.get_property("media-title","")))

    local args
    if source == "jellyfin" then
        local series, season, episode = parse_jellyfin_title(meta.title)
        msg.info(string.format("jellyfin parse: raw=%q → series=%q S=%s E=%s",
                               meta.title or "",
                               series or "", tostring(season), tostring(episode)))
        args = {"match-jellyfin", series}
        if season then table.insert(args, tostring(season)) end
        if episode then table.insert(args, tostring(episode)) end
    elseif source == "local" then
        msg.info(string.format("local parse will use path=%q", meta.path or ""))
        args = {"match-file", meta.path}
    else
        msg.info("non-jellyfin stream — skipping danmaku auto-match")
        state.last_status = "none"
        update_icon_overlay()
        return
    end

    helper_run_async(args, function(success, result, _err)
        if mine ~= state.file_id then return end  -- file changed underneath us
        local stdout = (result and result.stdout or ""):gsub("\n$", "")
        local stderr = (result and result.stderr or "")
        for line in stderr:gmatch("[^\n]+") do msg.info(line) end

        -- Helper output is multi-line key:value:
        --   EPID:<id> | NONE       (first line, success / failure)
        --   SERIES:<title>          (always when known)
        --   SEASON:<n>              (when known)
        --   EPISODE:<n>             (when known)
        -- Capture all of them so we can record an alias on manual pick.
        local epid, parsed_series, parsed_season, parsed_episode
        for line in stdout:gmatch("[^\n]+") do
            local v = line:match("^EPID:(%d+)$")
            if v then epid = v end
            v = line:match("^SERIES:(.+)$")
            if v then parsed_series = v end
            v = line:match("^SEASON:(%d+)$")
            if v then parsed_season = tonumber(v) end
            v = line:match("^EPISODE:(%d+)$")
            if v then parsed_episode = tonumber(v) end
        end
        state.parsed_series = parsed_series
        state.parsed_season = parsed_season
        state.parsed_episode = parsed_episode
        if not epid then
            -- Append diagnostic info to a known log file so the user can
            -- paste it back when "no match" is unexpected. mpv's own log
            -- isn't easily readable when launched via .desktop entry.
            local logf = io.open(DEBUG_LOG, "a")
            if logf then
                logf:write(string.format(
                    "[%s] no-match  source=%s  path=%q  media-title=%q\n  args=%s\n  stdout=%q\n  stderr=%q\n",
                    os.date("%Y-%m-%d %H:%M:%S"),
                    source,
                    mp.get_property("path",""),
                    mp.get_property("media-title",""),
                    table.concat(args, " "),
                    stdout, stderr))
                logf:close()
            end
            state.last_status = "none"
            mp.osd_message(string.format(
                "弹幕: 未匹配到 (Ctrl+F10 手动搜索 / 日志: %s)", DEBUG_LOG), 4)
            update_icon_overlay()
            return
        end

        state.last_match_episode = tonumber(epid)
        local out_path = path_join(TMP_DIR, string.format("dmk-%d.ass", state.last_match_episode))
        local off = offset_get(state.last_match_episode)
        helper_run_async({"fetch", epid, out_path,
                          "--offset", string.format("%.3f", off)},
        function(_s, r2, _e)
            if mine ~= state.file_id then return end
            local so = (r2 and r2.stdout or ""):gsub("\n$", "")
            for line in (r2 and r2.stderr or ""):gmatch("[^\n]+") do msg.info(line) end
            local n = tonumber(so:match("^OK:(%d+)$") or "")
            if not n then
                state.last_status = "error"
                mp.osd_message("弹幕: 抓取失败", 3)
                update_icon_overlay()
                return
            end
            load_ass(out_path, n)
            state.last_status = "ok"
            mp.set_property("secondary-sub-visibility",
                            settings.enabled and "yes" or "no")
            mp.osd_message(string.format("弹幕: %d 条已加载", n), 2)
            update_icon_overlay()
        end)
    end)
end

-- ============================================================================
-- Reload current match with the latest settings (called when user changes
-- a setting in the panel — settings get persisted, helper re-runs).
-- ============================================================================
local function reload_current()
    -- If no match yet, "重新加载" means "try matching again". Useful when
    -- the first auto-match returned no result (transient API hiccup or a
    -- title-parse miss); user picks the row to retry without restarting mpv.
    if not state.last_match_episode then
        msg.info("reload: no previous match, retrying auto-match")
        trigger_match()
        return
    end
    state.last_status = "loading"
    update_icon_overlay()
    local out_path = path_join(TMP_DIR, string.format("dmk-%d.ass", state.last_match_episode))
    local off = offset_get(state.last_match_episode)
    helper_run_async({"fetch", tostring(state.last_match_episode), out_path,
                      "--offset", string.format("%.3f", off)},
        function(_s, r, _e)
            local so = (r and r.stdout or ""):gsub("\n$", "")
            local n = tonumber(so:match("^OK:(%d+)$") or "")
            if not n then
                state.last_status = "error"
                update_icon_overlay()
                return
            end
            load_ass(out_path, n)
            state.last_status = "ok"
            mp.set_property("secondary-sub-visibility",
                            settings.enabled and "yes" or "no")
            update_icon_overlay()
        end)
end

-- ============================================================================
-- Common modal-overlay infrastructure (panel UI, search results UI)
-- ============================================================================
local modal = {
    overlay = nil,
    active = nil,        -- "panel" | "results" | nil
    bindings = {},
}

local function modal_clear_bindings()
    for _, name in ipairs(modal.bindings) do
        mp.remove_key_binding(name)
    end
    modal.bindings = {}
end

local function modal_bind(key, name, fn)
    mp.add_forced_key_binding(key, name, fn)
    table.insert(modal.bindings, name)
end

local function modal_close()
    modal_clear_bindings()
    if modal.overlay then
        modal.overlay.data = ""
        modal.overlay:update()
    end
    modal.active = nil
end

-- ============================================================================
-- Settings panel  (Shift+F10)
-- ============================================================================
local PANEL_ROWS = {
    -- Section divider rows (`section=true`) are rendered as labels-only
    -- and skipped during ↑↓ navigation. They group adjacent rows
    -- visually so the dense panel stays scannable.
    {section=true, label="── 显示 ──"},
    {key="opacity",     label="透明度",     values={0.30, 0.50, 0.65, 0.75, 0.85, 1.00},
                        fmt=function(v) return string.format("%d%%", math.floor(v*100+0.5)) end},
    -- font_size: ←→ cycles presets; Enter opens a text input for any
    -- value (1..200 pt). Cycle list spans small (12) up to large (72).
    {key="font_size",   label="字号",
                        values={12, 14, 16, 18, 20, 24, 28, 32, 36, 42, 48, 56, 64, 72},
                        custom_min=1, custom_max=200,
                        fmt=function(v) return string.format("%dpt (Enter 自定义)", v) end},
    {key="stroke_width",label="描边粗细",   values={0.0, 1.0, 2.0, 3.0, 4.0},
                        fmt=function(v) return string.format("%.1f px", v) end},
    {key="speed",       label="速度",       values={72, 108, 144, 180, 216},
                        fmt=function(v) return string.format("%d px/s", v) end},
    {key="density",     label="密度",       values={"low","medium","high"},
                        fmt=function(v) return ({low="低",medium="中",high="高"})[v] or v end},
    {key="area",        label="显示区域",   values={0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00},
                        fmt=function(v) return string.format("%d%%", math.floor(v*100+0.5)) end},

    {section=true, label="── 模式 ──"},
    {key="render_mode", label="渲染模式",   values={"original","rtl","ltr"},
                        fmt=function(v) return ({
                            original="保留原始模式",
                            rtl="全部右→左滚动",
                            ltr="全部左→右滚动",
                        })[v] or v end},
    {key="anti_overlap",label="弹幕防重叠", values={false, true},
                        fmt=function(v) return v and "开 (拥挤时丢弃)" or "关 (允许重叠)" end},
    {key="chConvert",   label="繁简转换",   values={0, 1, 2},
                        fmt=function(v) return ({[0]="不转换",[1]="繁→简",[2]="简→繁"})[v] or v end},
    -- Per-episode time offset. Stored in offsets cache (per episodeId)
    -- rather than settings; a custom getter/setter is wired in panel_adjust.
    -- ←→ steps the preset cycle (range ±30 s, fine-grained near 0);
    -- Enter opens a text prompt for any float in [-3600..+3600] s.
    {key="time_offset", label="时间偏移",
                        is_offset=true,
                        custom_min=-3600, custom_max=3600,
                        values={-30, -20, -10, -5, -3, -2, -1, -0.5,
                                0,
                                0.5, 1, 2, 3, 5, 10, 20, 30},
                        fmt=function(v)
                            if v == 0 then return "无 (0.0 s, Enter 自定义)" end
                            return string.format("%+.1f s (Enter 自定义)", v)
                        end},

    {section=true, label="── 过滤 ──"},
    -- Multi-chip rows. `set_key` names the settings list; `semantic` is
    -- "include" (chip lit ⇔ id present in list ⇔ visible) or "exclude"
    -- (chip lit ⇔ id NOT in list ⇔ visible). Both render the same way:
    -- a lit chip means "currently shown on screen".
    {multi=true, label="弹幕来源",
                 set_key="disabled_sources", semantic="exclude",
                 items={ {"bilibili","B站"}, {"gamer","巴哈"},
                         {"dandanplay","弹弹"}, {"other","其他"} }},
    {multi=true, label="显示模式",
                 set_key="show_modes", semantic="include",
                 items={ {1,"滚动"}, {6,"逆向"}, {5,"顶部"}, {4,"底部"} }},
    {key="dedup",       label="弹幕去重",   values={false, true},
                        fmt=function(v) return v and "开" or "关" end},
    {key="dedup_window",label="去重窗口",   values={0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0},
                        fmt=function(v) return string.format("%.1f s", v) end},
    {key="dedup_min_count", label="标注阈值", values={3, 5, 10, 20},
                        fmt=function(v) return string.format("≥ %d 条标 [+N]", v) end},

    {section=true, label="── 操作 ──"},
    -- Action rows (label_fn for dynamic state).
    {action="search",   label="› 手动搜索 / 切换匹配"},
    {action="reload",   label="↻ 重新加载当前匹配"},
    {action="toggle",   label_fn=function()
                            return settings.enabled
                                and "■ 关闭弹幕显示"
                                or  "▶ 开启弹幕显示"
                        end},
}
-- Panel sized to fit ~22 rows comfortably at row_h=30 + section spacing.
local PANEL_W, PANEL_H = 740, 760

-- panel_state: row cursor + sub-cursor for multi-toggle rows (chip index)
local panel_cursor = 1
local panel_sub_cursor = 1   -- which chip is highlighted on a multi row

local function panel_render()
    if not modal.overlay then
        modal.overlay = mp.create_osd_overlay("ass-events")
    end
    local ov = modal.overlay
    local W = mp.get_property_number("osd-width", 1920)
    local H = mp.get_property_number("osd-height", 1080)
    ov.res_x = W; ov.res_y = H

    -- Center the panel
    local x = math.floor((W - PANEL_W) / 2)
    local y = math.floor((H - PANEL_H) / 2)

    -- Background panel (dark, semi-transparent rounded box)
    local bg = string.format(
        "{\\an7\\pos(%d,%d)\\bord0\\1c&H1A1A1A&\\1a&H30&\\p1}"
        .. "m 0 0 l %d 0 l %d %d l 0 %d{\\p0}",
        x, y, PANEL_W, PANEL_W, PANEL_H, PANEL_H)

    -- Title bar
    local title = string.format(
        "{\\an7\\pos(%d,%d)\\fn%s\\fs26\\b1\\1c&HCC9966&}弹幕设置",
        x + 24,
        y + 18,
        ass_font())
    local hint = string.format(
        "{\\an7\\pos(%d,%d)\\fn%s\\fs14\\1c&H888888&}↑↓ 选行 ←→ 调值 Enter 触发 Esc 关闭",
        x + 24,
        y + PANEL_H - 28,
        ass_font())

    local rows = {bg, title, hint}
    local row_y = y + 56
    local row_h = 30

    for i, r in ipairs(PANEL_ROWS) do
        local is_cursor = (i == panel_cursor)
        local lcol = is_cursor and "&H66CCFF&" or "&HCCCCCC&"
        local marker = is_cursor and "▶ " or "  "

        if r.section then
            -- Non-interactive divider: muted color, smaller font, no marker.
            table.insert(rows, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs15\\1c&H7AAAB0&\\b1}%s",
                x + 24,
                row_y + (i-1)*row_h + 4,
                ass_font(),
                r.label))
        elseif r.action then
            local label_text = r.label_fn and r.label_fn() or r.label
            local label_str = string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs20\\1c%s}%s%s",
                x + 24,
                row_y + (i-1)*row_h,
                ass_font(),
                lcol,
                marker,
                label_text)
            table.insert(rows, label_str)
        elseif r.multi then
            -- Multi-chip row. Membership is read from settings[r.set_key];
            -- chip "lit" means "currently shown on screen" — for include
            -- semantics the id must be in the set, for exclude semantics
            -- the id must NOT be in the set.
            local in_set = {}
            for _, m in ipairs(settings[r.set_key] or {}) do in_set[m] = true end
            local exclude_semantic = (r.semantic == "exclude")
            local label_str = string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs20\\1c%s}%s%-10s",
                x + 24,
                row_y + (i-1)*row_h,
                ass_font(),
                lcol,
                marker,
                r.label)
            table.insert(rows, label_str)
            -- Chips at right side. When this row is the active row, the
            -- chip at panel_sub_cursor gets a brighter outline so user
            -- knows which one Enter will toggle.
            local chip_x = x + 220
            for j, item in ipairs(r.items) do
                local m_id, m_lbl = item[1], item[2]
                local present = in_set[m_id] and true or false
                local on = exclude_semantic and (not present) or present
                local sub_active = is_cursor and (j == panel_sub_cursor)
                local chip_col = on and "&HFFFFFF&" or "&H777777&"
                local chip_bg = on and "&H553388&" or "&H222222&"
                local chip_w = 90
                -- Chip background
                table.insert(rows, string.format(
                    "{\\an7\\pos(%d,%d)\\1c%s\\1a&H80&\\bord0\\p1}m 0 0 l %d 0 l %d 26 l 0 26{\\p0}",
                    chip_x, row_y + (i-1)*row_h - 1, chip_bg, chip_w, chip_w))
                -- Sub-cursor outline (drawn as a slightly bigger frame behind)
                if sub_active then
                    table.insert(rows, string.format(
                        "{\\an7\\pos(%d,%d)\\1c&H66CCFF&\\1a&H30&\\bord0\\p1}"
                        .. "m 0 0 l %d 0 l %d 30 l 0 30{\\p0}",
                        chip_x - 2, row_y + (i-1)*row_h - 3, chip_w + 4, chip_w + 4))
                end
                table.insert(rows, string.format(
                    "{\\an7\\pos(%d,%d)\\fn%s\\fs16\\1c%s\\bord0}%s",
                    chip_x + 16,
                    row_y + (i-1)*row_h + 2,
                    ass_font(),
                    chip_col,
                    m_lbl))
                chip_x = chip_x + chip_w + 8
            end
        else
            -- Value row: label on left, current value highlighted on right.
            -- Special-case time_offset (per-episode, lives in offsets cache).
            local cur
            if r.is_offset then
                cur = offset_get(state.last_match_episode)
            else
                cur = settings[r.key]
            end
            local val_str = r.fmt and r.fmt(cur) or tostring(cur)
            local idx = 0
            for j, v in ipairs(r.values) do if v == cur then idx = j; break end end
            local total = #r.values
            -- Draw a simple indicator: current/total dots
            local dots = ""
            for j = 1, total do
                dots = dots .. (j == idx and "●" or "○")
            end
            table.insert(rows, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs20\\1c%s}%s%-10s",
                x + 24,
                row_y + (i-1)*row_h,
                ass_font(),
                lcol,
                marker,
                r.label))
            table.insert(rows, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs18\\1c&HFFFFFF&}%s",
                x + 220,
                row_y + (i-1)*row_h,
                ass_font(),
                val_str))
            table.insert(rows, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs16\\1c&H888888&}%s",
                x + 380,
                row_y + (i-1)*row_h + 2,
                ass_font(),
                dots))
        end
    end

    ov.data = table.concat(rows, "\n")
    ov:update()
end

local panel_open  -- forward declaration
local panel_close -- forward declaration

local function panel_navigate(dir)
    -- Step in `dir` direction, skipping any section divider rows.
    -- Bounded loop so a malformed PANEL_ROWS (all sections) won't hang.
    for _ = 1, #PANEL_ROWS do
        panel_cursor = panel_cursor + dir
        if panel_cursor < 1 then panel_cursor = #PANEL_ROWS end
        if panel_cursor > #PANEL_ROWS then panel_cursor = 1 end
        if not PANEL_ROWS[panel_cursor].section then break end
    end
    -- Reset sub-cursor when leaving / entering a multi row
    panel_sub_cursor = 1
    panel_render()
end

local function panel_adjust(dir)
    local r = PANEL_ROWS[panel_cursor]
    if not r then return end
    if r.action then return end
    if r.multi then
        -- LEFT/RIGHT moves the sub-cursor between chips
        panel_sub_cursor = panel_sub_cursor + dir
        if panel_sub_cursor < 1 then panel_sub_cursor = #r.items end
        if panel_sub_cursor > #r.items then panel_sub_cursor = 1 end
        panel_render()
        return
    end
    -- Value row: cycle through r.values.
    -- time_offset is a special row (per-episode, persisted to offsets file).
    if r.is_offset then
        if not state.last_match_episode then
            mp.osd_message("还没有匹配到弹幕，无法设置偏移", 1.5)
            return
        end
        local cur = offset_get(state.last_match_episode)
        local idx = 1
        for i, v in ipairs(r.values) do if v == cur then idx = i; break end end
        idx = idx + dir
        if idx < 1 then idx = #r.values end
        if idx > #r.values then idx = 1 end
        offset_set(state.last_match_episode, r.values[idx])
        panel_render()
        reload_current()  -- re-fetch with new --offset
        return
    end
    local cur = settings[r.key]
    local idx = 1
    for i, v in ipairs(r.values) do if v == cur then idx = i; break end end
    idx = idx + dir
    if idx < 1 then idx = #r.values end
    if idx > #r.values then idx = 1 end
    settings[r.key] = r.values[idx]
    settings_save()
    panel_render()
    -- Re-render the loaded ASS so the change takes effect immediately
    reload_current()
end

-- Toggle a single chip on the multi row. `idx` is 1-based chip index.
local function panel_toggle_chip(idx)
    local r = PANEL_ROWS[panel_cursor]
    if not r or not r.multi then return end
    local item = r.items[idx]
    if not item then return end
    local m_id = item[1]
    local in_set = {}
    for _, m in ipairs(settings[r.set_key] or {}) do in_set[m] = true end
    in_set[m_id] = not in_set[m_id]
    local new = {}
    for k, v in pairs(in_set) do if v then table.insert(new, k) end end
    table.sort(new, function(a, b)
        -- numeric or string list — table.sort default works for both as
        -- long as elements are uniform type, which they are per row.
        return tostring(a) < tostring(b)
    end)
    -- Defensive: for include-semantic rows, never let the user end up
    -- with zero entries (would yield an empty filter chain and no danmaku
    -- at all). Exclude-semantic rows have no such constraint — an empty
    -- "disabled" list is the natural default ("show everything").
    if r.semantic ~= "exclude" and #new == 0 then
        mp.osd_message("至少保留一项", 1.5)
        return
    end
    settings[r.set_key] = new
    settings_save()
    panel_render()
    reload_current()
end

local function panel_activate()
    local r = PANEL_ROWS[panel_cursor]
    if not r then return end
    if r.action == "search" then
        panel_close()
        mp.add_timeout(0.05, function() open_search_input() end)
        return
    elseif r.action == "reload" then
        panel_close()
        reload_current()
        mp.osd_message("弹幕: 重载中...", 1.5)
        return
    elseif r.action == "toggle" then
        panel_close()
        set_visible(not settings.enabled)
        mp.osd_message("弹幕: " .. (settings.enabled and "开" or "关"), 1.5)
        return
    elseif r.multi then
        -- Enter / Space toggles the chip currently under the sub-cursor.
        panel_toggle_chip(panel_sub_cursor)
        return
    elseif r.custom_min and r.custom_max then
        -- Numeric value row with custom-input support: prompt for any
        -- value in [custom_min..custom_max]. Routing depends on the row:
        --   is_offset=true → write through offset_set(epid, ...)
        --   otherwise      → write to settings[r.key]
        if not input then
            mp.osd_message("此 mpv 版本不支持文本输入 (需 ≥0.39)", 2)
            return
        end
        if r.is_offset and not state.last_match_episode then
            mp.osd_message("还没有匹配到弹幕，无法设置偏移", 1.5)
            return
        end
        -- Read current value from the right place.
        local cur
        if r.is_offset then
            cur = string.format("%.2f", offset_get(state.last_match_episode))
        else
            cur = tostring(settings[r.key] or "")
        end
        local prompt = string.format("%s (%s..%s): ",
            r.label,
            tostring(r.custom_min), tostring(r.custom_max))
        -- Critical: the Enter key event that triggered panel_activate
        -- can collide with input.get's own Enter handler (the prompt
        -- can immediately accept the default value or the prompt window
        -- can fail to open). Defer the prompt by one tick so the Enter
        -- finishes propagating, exactly like open_search_input does.
        modal_close()
        mp.add_timeout(0.05, function()
            input.get({
                prompt = prompt,
                default_text = cur,
                submit = function(text)
                    input.terminate()
                    local n = tonumber(text)
                    if not n or n < r.custom_min or n > r.custom_max then
                        mp.osd_message(string.format(
                            "无效输入 (需 %s..%s)",
                            tostring(r.custom_min),
                            tostring(r.custom_max)), 2)
                    else
                        if r.is_offset then
                            offset_set(state.last_match_episode, n)
                            mp.osd_message(string.format(
                                "%s = %+.2f s", r.label, n), 1.2)
                        else
                            settings[r.key] = n
                            settings_save()
                            mp.osd_message(string.format(
                                "%s = %s", r.label, tostring(n)), 1.2)
                        end
                        reload_current()
                    end
                    -- Reopen the panel so user keeps editing other settings.
                    mp.add_timeout(0.05, function() panel_open() end)
                end,
            })
        end)
        return
    end
end

panel_close = function()
    modal_close()
end

panel_open = function()
    modal_close()  -- close any other modal
    modal.active = "panel"
    -- Start on the first non-section row.
    panel_cursor = 1
    while panel_cursor <= #PANEL_ROWS and PANEL_ROWS[panel_cursor].section do
        panel_cursor = panel_cursor + 1
    end
    panel_sub_cursor = 1
    panel_render()
    modal_bind("UP",       "dmk-panel-up",     function() panel_navigate(-1) end)
    modal_bind("DOWN",     "dmk-panel-down",   function() panel_navigate( 1) end)
    modal_bind("LEFT",     "dmk-panel-left",   function() panel_adjust(-1) end)
    modal_bind("RIGHT",    "dmk-panel-right",  function() panel_adjust( 1) end)
    modal_bind("ENTER",    "dmk-panel-enter",  panel_activate)
    modal_bind("SPACE",    "dmk-panel-space",  panel_activate)
    modal_bind("ESC",      "dmk-panel-esc",    panel_close)
    modal_bind("Shift+F10","dmk-panel-toggle", panel_close)
    -- Numeric shortcuts: only meaningful when on the mode-chips row.
    -- We still bind them globally inside the panel modal, but the
    -- handler no-ops when the cursor isn't on a multi row, so they
    -- don't accidentally toggle modes from other rows.
    for i = 1, 4 do
        modal_bind(tostring(i), "dmk-panel-num-" .. i, function()
            local r = PANEL_ROWS[panel_cursor]
            if r and r.multi then
                panel_toggle_chip(i)
            end
        end)
    end
end

-- ============================================================================
-- Search UI: input box + results list
-- ============================================================================
function open_search_input()
    -- mp.input is a separately-required module (added mpv 0.39).
    -- The fallback path below uses mpv's command console as a degraded
    -- experience for ancient mpv builds.
    if input then
        input.get({
            prompt = "弹幕搜索: ",
            submit = function(text)
                input.terminate()
                if not text or text == "" then return end
                run_search_query(text)
            end,
        })
    else
        mp.osd_message("Ctrl+F10: 在 console 里输入: script-message dmk-search <关键词>", 5)
        mp.commandv("script-message", "type", "script-message dmk-search ")
    end
end

function run_search_query(query)
    state.last_status = "loading"; update_icon_overlay()
    helper_run_async({"search", query}, function(_s, r, _e)
        local results = {}
        for line in (r and r.stdout or ""):gmatch("[^\n]+") do
            local ok, parsed = pcall(utils.parse_json, line)
            if ok and parsed then table.insert(results, parsed) end
        end
        if #results == 0 then
            state.last_status = "none"
            mp.osd_message("搜索: 无结果", 2)
            update_icon_overlay()
            return
        end
        show_results_panel(results)
    end)
end

-- Generic scrollable list picker. Used for both anime selection and
-- episode selection when the user manually searches. Keys:
--   ↑↓     move cursor
--   PgUp/PgDn / Tab / Shift+Tab  page through long lists
--   Home/End  jump to first/last
--   1-9    quick-pick within the current viewport
--   Enter  confirm
--   Esc    cancel
local function _show_list_picker(opts)
    modal_close()
    modal.active = "picker"

    local items = opts.items
    if #items == 0 then
        mp.osd_message("无结果", 1.5)
        return
    end

    local cursor = 1
    local viewport_size = 18
    local viewport_top = 1

    local function render()
        if not modal.overlay then
            modal.overlay = mp.create_osd_overlay("ass-events")
        end
        local ov = modal.overlay
        local W = mp.get_property_number("osd-width", 1920)
        local H = mp.get_property_number("osd-height", 1080)
        ov.res_x = W; ov.res_y = H

        local visible = math.min(viewport_size, #items)
        local panel_w = 1000
        local panel_h = 80 + visible * 32 + 40
        local x = math.floor((W - panel_w) / 2)
        local y = math.floor((H - panel_h) / 2)

        local lines = {}
        table.insert(lines, string.format(
            "{\\an7\\pos(%d,%d)\\bord0\\1c&H1A1A1A&\\1a&H20&\\p1}"
            .. "m 0 0 l %d 0 l %d %d l 0 %d{\\p0}",
            x, y, panel_w, panel_w, panel_h, panel_h))
        table.insert(lines, string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs22\\b1\\1c&HCC9966&}%s",
            x + 24, y + 18, ass_font(), opts.title or "选择"))
        if #items > viewport_size then
            table.insert(lines, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs14\\1c&H888888&}%d/%d",
                x + panel_w - 100, y + 22, ass_font(), cursor, #items))
        end
        for vi = 1, visible do
            local idx = viewport_top + vi - 1
            if idx > #items then break end
            local item = items[idx]
            local is_cursor = (idx == cursor)
            local col = is_cursor and "&H66CCFF&" or "&HFFFFFF&"
            local marker = is_cursor and "▶ " or "  "
            -- Quick-pick number 1-9 within viewport
            local prefix = (vi <= 9) and tostring(vi) .. ". " or "   "
            table.insert(lines, string.format(
                "{\\an7\\pos(%d,%d)\\fn%s\\fs18\\1c%s}%s%s%s",
                x + 30, y + 60 + (vi-1)*32, ass_font(), col,
                marker, prefix, item.label))
        end
        table.insert(lines, string.format(
            "{\\an7\\pos(%d,%d)\\fn%s\\fs13\\1c&H888888&}"
            .. "↑↓ 选择    PgUp/PgDn 翻页    1-9 视区快选    Enter 确认    Esc 取消",
            x + 24, y + panel_h - 26, ass_font()))
        ov.data = table.concat(lines, "\n")
        ov:update()
    end

    local function clamp_viewport()
        if cursor < viewport_top then viewport_top = cursor end
        if cursor > viewport_top + viewport_size - 1 then
            viewport_top = cursor - viewport_size + 1
        end
        if viewport_top < 1 then viewport_top = 1 end
        if viewport_top > math.max(1, #items - viewport_size + 1) then
            viewport_top = math.max(1, #items - viewport_size + 1)
        end
    end
    local function move_cursor(dir)
        cursor = cursor + dir
        if cursor < 1 then cursor = #items end
        if cursor > #items then cursor = 1 end
        clamp_viewport(); render()
    end
    local function page(dir)
        cursor = math.max(1, math.min(#items, cursor + dir * viewport_size))
        clamp_viewport(); render()
    end

    render()
    modal_bind("UP",   "dmk-pick-up",   function() move_cursor(-1) end)
    modal_bind("DOWN", "dmk-pick-down", function() move_cursor( 1) end)
    modal_bind("PGUP", "dmk-pick-pgup", function() page(-1) end)
    modal_bind("PGDWN","dmk-pick-pgdn", function() page( 1) end)
    modal_bind("Tab",  "dmk-pick-tab",  function() page( 1) end)
    modal_bind("Shift+Tab", "dmk-pick-stab", function() page(-1) end)
    modal_bind("HOME", "dmk-pick-home", function()
        cursor = 1; clamp_viewport(); render()
    end)
    modal_bind("END",  "dmk-pick-end",  function()
        cursor = #items; clamp_viewport(); render()
    end)
    -- Quick-pick 1..9 within current viewport
    for k = 1, 9 do
        modal_bind(tostring(k), "dmk-pick-num-" .. k, function()
            local idx = viewport_top + k - 1
            if idx <= #items then
                opts.on_pick(items[idx])
            end
        end)
    end
    modal_bind("ENTER", "dmk-pick-enter", function()
        opts.on_pick(items[cursor])
    end)
    modal_bind("SPACE", "dmk-pick-space", function()
        opts.on_pick(items[cursor])
    end)
    modal_bind("ESC", "dmk-pick-esc", modal_close)
end

-- Forward declaration so show_results_panel and show_episode_picker can
-- reference each other.
local show_episode_picker

-- Stage 1: list of animes returned by the search query. Picker drills
-- into the chosen anime's episode list.
function show_results_panel(animes)
    local items = {}
    for _, a in ipairs(animes) do
        table.insert(items, {
            label = string.format("%s  (%d 集, %s)",
                                  a.animeTitle,
                                  #(a.episodes or {}),
                                  a.type or a.typeDescription or ""),
            anime = a,
        })
    end
    _show_list_picker({
        title = string.format("搜索结果（%d 部，选番剧）", #animes),
        items = items,
        on_pick = function(item)
            show_episode_picker(item.anime)
        end,
    })
end

-- Stage 2: list of episodes within a chosen anime. Enter loads.
show_episode_picker = function(anime)
    local items = {}
    for _, ep in ipairs(anime.episodes or {}) do
        table.insert(items, {
            label = ep.episodeTitle,
            episodeId = ep.episodeId,
        })
    end
    _show_list_picker({
        title = string.format("%s — 选剧集（共 %d）",
                              anime.animeTitle, #items),
        items = items,
        on_pick = function(item)
            local epid = item.episodeId
            modal_close()
            state.last_status = "loading"; update_icon_overlay()

            -- Smart-match: if the user is manually picking because
            -- auto-match parsed a series name that dandanplay doesn't
            -- recognize, remember that {parsed-series → picked-anime}
            -- mapping. Future episodes of the same series that fail
            -- their primary search will fall back to this alias.
            if state.parsed_series and anime.animeTitle
               and state.parsed_series ~= "" and anime.animeTitle ~= ""
               and state.parsed_series ~= anime.animeTitle then
                helper_run_async(
                    {"record-alias", state.parsed_series, anime.animeTitle},
                    function(_s, r, _e)
                        local out = (r and r.stdout or ""):gsub("\n$", "")
                        if out == "OK" then
                            msg.info(string.format(
                                "alias recorded: %q → %q",
                                state.parsed_series, anime.animeTitle))
                            mp.osd_message(string.format(
                                "已记忆：%s → %s",
                                state.parsed_series, anime.animeTitle), 2.5)
                        end
                    end)
            end

            local out_path = path_join(TMP_DIR, string.format("dmk-%d.ass", epid))
            helper_run_async({"fetch", tostring(epid), out_path,
                              "--offset", string.format("%.3f", offset_get(epid))},
                function(_s, r, _e)
                    local so = (r and r.stdout or ""):gsub("\n$", "")
                    local n = tonumber(so:match("^OK:(%d+)$") or "")
                    if not n then
                        state.last_status = "error"
                        mp.osd_message("抓取失败", 2)
                        update_icon_overlay()
                        return
                    end
                    state.last_match_episode = epid
                    load_ass(out_path, n)
                    state.last_status = "ok"
                    mp.set_property("secondary-sub-visibility",
                                    settings.enabled and "yes" or "no")
                    mp.osd_message(string.format("弹幕: %d 条已加载", n), 2)
                    update_icon_overlay()
                end)
        end,
    })
end

-- ============================================================================
-- Mouse hit-test for the overlay icon
-- ============================================================================
local function mouse_inside_icon()
    local pos = mp.get_property_native("mouse-pos")
    if not pos then return false end
    local W = mp.get_property_number("osd-width", 1920)
    local x = W - ICON_W - ICON_PAD
    local y = ICON_PAD
    return pos.x >= x and pos.x <= x + ICON_W
       and pos.y >= y and pos.y <= y + ICON_H
end

-- ============================================================================
-- Key bindings
-- ============================================================================
mp.add_key_binding("F10", "danmaku-toggle", function()
    set_visible(not settings.enabled)
    mp.osd_message("弹幕: " .. (settings.enabled and "开" or "关"), 1.5)
end)

mp.add_key_binding("Shift+F10", "danmaku-panel", panel_open)

mp.add_key_binding("Ctrl+F10", "danmaku-search", open_search_input)

-- Quick opacity cycle (lazy mode — open the panel for finer control)
mp.add_key_binding("Alt+F10", "danmaku-opacity-quick", function()
    local opts = {0.5, 0.75, 1.0}
    local cur = settings.opacity
    local idx = 1
    for i, v in ipairs(opts) do if math.abs(v - cur) < 0.01 then idx = i; break end end
    settings.opacity = opts[(idx % #opts) + 1]
    settings_save()
    mp.osd_message(string.format("弹幕透明度: %d%%", math.floor(settings.opacity*100+0.5)), 2)
    reload_current()
end)

-- Mouse handling: chain alongside any existing MOUSE_BTN0 binding (shim
-- registers one). add_key_binding's name avoids collision.
mp.add_key_binding("MBTN_LEFT", "danmaku-click", function()
    if mouse_inside_icon() then
        set_visible(not settings.enabled)
        mp.osd_message("弹幕: " .. (settings.enabled and "开" or "关"), 1.5)
    end
end)

-- Track mouse activity for the OSC visibility heuristic
mp.add_key_binding("MOUSE_MOVE", "danmaku-mousetrack", function()
    state.cursor_last = mp.get_time()
    update_icon_overlay()
end, {repeatable=true, complex=true})

-- Periodic refresh so the icon hides after the cursor stops moving
mp.add_periodic_timer(0.5, update_icon_overlay)

-- ============================================================================
-- Lifecycle hooks
-- ============================================================================
mp.register_event("file-loaded", function()
    -- Reset state for the new file
    unload_current_sub()
    state.last_match_episode = nil
    state.last_count = 0
    state.last_status = "loading"   -- show "loading" while we wait
    update_icon_overlay()

    -- For network URLs (e.g. Jellyfin), `media-title` on file-loaded is
    -- a URL-derived default ("stream?static=true&..."); the real title
    -- from the server's metadata arrives a moment later. Triggering
    -- immediately makes the parser see the URL, not the title.
    -- Solution: wait briefly, and re-check media-title after the delay.
    -- For local files, media-title is set from the filename synchronously,
    -- so no delay is needed.
    local path = mp.get_property("path", "") or ""
    local is_network = path:match("^https?://") ~= nil
    local delay = is_network and 1.5 or 0.0

    -- Race-cancel token: if a newer file-loaded fires during the delay,
    -- the older trigger is silently dropped.
    state.pending_match_token = (state.pending_match_token or 0) + 1
    local my_token = state.pending_match_token
    mp.add_timeout(delay, function()
        if my_token ~= state.pending_match_token then return end
        trigger_match()
    end)
end)

mp.register_event("end-file", function()
    unload_current_sub()
    state.last_match_episode = nil
    state.last_status = "idle"
end)

mp.register_event("shutdown", function()
    unload_current_sub()
end)

-- Boot message in the log
msg.info(string.format(
    "danmaku.lua loaded — helper=%s python=%s settings=%s",
    HELPER, PYTHON, SETTINGS_FILE))
