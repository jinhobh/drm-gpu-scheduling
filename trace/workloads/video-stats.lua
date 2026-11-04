-- mp.get_time() uses mpv's time base; bracket it with host CLOCK_MONOTONIC events.
-- Counters are mpv diagnostics, not per-frame Wayland presentation feedback.
local output = assert(io.open(os.getenv('MPV_LOG') or '/dev/null', 'w'))
local utils = require 'mp.utils'
local properties = {'time-pos', 'frame-drop-count', 'decoder-frame-drop-count',
                    'mistimed-frame-count', 'vo-delayed-frame-count',
                    'estimated-display-fps', 'hwdec-current', 'video-codec'}
local function sample()
    local row = {mpv_time = mp.get_time()}
    for _, key in ipairs(properties) do row[key] = mp.get_property_native(key) end
    output:write(utils.format_json(row), '\n')
    output:flush()
end
mp.add_periodic_timer(0.1, sample)
mp.register_event('shutdown', function() sample(); output:close() end)
