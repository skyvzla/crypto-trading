CREATE TABLE IF NOT EXISTS chart_settings (
    setting_key VARCHAR(64) PRIMARY KEY
        CHECK (setting_key = 'global'),
    settings JSONB NOT NULL
        CHECK (jsonb_typeof(settings) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO chart_settings (setting_key, settings)
VALUES (
    'global',
    '{
        "main": {
            "ema": {
                "enabled": false,
                "lines": [
                    {"period": 9, "color": "#f5c451"},
                    {"period": 21, "color": "#66b3ff"}
                ]
            },
            "ma": {
                "enabled": false,
                "lines": [
                    {"period": 5, "color": "#f59e0b"},
                    {"period": 10, "color": "#22c55e"},
                    {"period": 20, "color": "#3b82f6"}
                ]
            },
            "boll": {
                "enabled": false,
                "period": 20,
                "deviation": 2.0,
                "colors": {
                    "upper": "#ef4444",
                    "middle": "#eab308",
                    "lower": "#22c55e"
                }
            }
        },
        "sub": {
            "volume": {
                "enabled": true,
                "ma_lines": [
                    {"period": 5, "color": "#f5c451"},
                    {"period": 20, "color": "#4da3ff"}
                ]
            },
            "macd": {
                "enabled": false,
                "fast_period": 12,
                "slow_period": 26,
                "signal_period": 9,
                "colors": {
                    "dif": "#4da3ff",
                    "dea": "#f5c451",
                    "histogram_up": "#2ebd85",
                    "histogram_down": "#f05252"
                }
            },
            "kdj": {
                "enabled": false,
                "period": 9,
                "colors": {"k": "#4da3ff", "d": "#f5c451", "j": "#d98bff"}
            },
            "rsi": {
                "enabled": false,
                "lines": [
                    {"period": 6, "color": "#f5c451"},
                    {"period": 12, "color": "#4da3ff"},
                    {"period": 24, "color": "#d98bff"}
                ]
            },
            "atr": {"enabled": false, "period": 14, "color": "#14b8a6"}
        }
    }'::JSONB
)
ON CONFLICT (setting_key) DO NOTHING;
