"""Atomic, idempotent publication of a completed market event in Redis."""

WATERMARK_KEY = "market:continuity_watermarks:v2"

_APPEND = """
local previous = redis.call('HGET', KEYS[2], 'time')
if not previous then
    local records = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
    local data = (#records > 0) and records[1][2][2] or false
    if ARGV[6] == '1' then
        local latest = redis.call('HGET', KEYS[4], 'latest')
        if latest and (not data or cjson.decode(latest).open_time > cjson.decode(data).open_time) then data = latest end
    end
    if data then
        local event = cjson.decode(data)
        previous = event.open_time or event.timestamp
        redis.call('HSET', KEYS[2], 'time', tostring(previous), 'data', data)
    end
end
if previous and tonumber(ARGV[2]) <= tonumber(previous) then
    if tonumber(ARGV[2]) == tonumber(previous)
       and redis.call('HGET', KEYS[2], 'data') ~= ARGV[1] then
        return redis.error_reply('conflicting completed market event')
    end
    return 0
end
redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[5], '*', 'data', ARGV[1])
redis.call('HSET', KEYS[2], 'time', ARGV[2], 'data', ARGV[1])
if ARGV[4] ~= '' then redis.call('HSET', KEYS[3], ARGV[3], ARGV[4]) end
if ARGV[6] == '1' then redis.call('HSET', KEYS[4], 'latest', ARGV[1]) end
return 1
"""


async def append_event(client, stream: str, message: str, *, timestamp: int,
                       watermark_field: str, watermark: int | None,
                       maxlen: int, latest_key: str | None = None) -> None:
    state = f"market:published:{stream}"
    await client.eval(
        _APPEND, 4, stream, state, WATERMARK_KEY, latest_key or state,
        message, str(timestamp), watermark_field,
        "" if watermark is None else str(watermark), str(maxlen),
        "1" if latest_key else "0",
    )
