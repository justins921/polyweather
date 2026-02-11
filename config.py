"""
Configuration for the Kalshi Weather Trading Bot.

Fill in KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, and CLAUDE_API_KEY before running.
"""

# ── Authentication ────────────────────────────────────────────────────────────
# Kalshi API key ID (from kalshi.com/account/profile → API Keys)
KALSHI_API_KEY_ID = ""

# Path to your Kalshi RSA private key PEM file
KALSHI_PRIVATE_KEY_PATH = ""

# Anthropic API key
CLAUDE_API_KEY = ""

# ── Kalshi endpoints ─────────────────────────────────────────────────────────
KALSHI_API_BASE = "https://api.elections.kalshi.com"
KALSHI_API_PATH = "/trade-api/v2"

# Set to True to use the demo/paper trading environment
KALSHI_DEMO_MODE = False
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co"

# ── Weather market series on Kalshi ──────────────────────────────────────────
# Series tickers for weather markets to scan
KALSHI_WEATHER_SERIES = [
    "KXHIGHNY",      # NYC high temperature
    "KXHIGHCHI",     # Chicago high temperature
    "KXHIGHLAX",     # LA high temperature
    "KXHIGHMIAMI",   # Miami high temperature
    "KXRAINNYC",     # NYC rain
]

# ── Claude model ──────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# ── Risk management ───────────────────────────────────────────────────────────
STARTING_BANKROLL = 100.0       # USD
HARD_STOP_BANKROLL = 70.0       # Pause trading if bankroll drops below this
MAX_BET_FRACTION = 0.03         # Max 3% of current bankroll per trade
MAX_OPEN_POSITIONS = 5          # Max simultaneous open positions
MIN_EDGE_THRESHOLD = 0.10       # Only trade when |fair_value - market_price| > 10%
MIN_CONFIDENCE = 0.3            # Skip trades where Claude confidence < this
KELLY_FRACTION = 0.25           # Quarter-Kelly

# ── Scanning parameters ──────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 1800    # 30 minutes
MAX_DAYS_TO_RESOLUTION = 30     # Skip markets resolving more than 30 days out

# ── API cost management ───────────────────────────────────────────────────────
# Sonnet pricing per million tokens (as of 2025)
CLAUDE_INPUT_COST_PER_MTOK = 3.00    # $/MTok input
CLAUDE_OUTPUT_COST_PER_MTOK = 15.00  # $/MTok output
DAILY_API_BUDGET = 2.00              # Halt Claude calls if daily spend exceeds this
ANALYSIS_CACHE_TTL_SECONDS = 1800    # Re-use cached analysis if market price moved < threshold
CACHE_PRICE_MOVE_THRESHOLD = 0.03    # Only re-analyze if price moved more than 3%
# Pre-filter: skip markets where YES price is this close to 0 or 1 (no edge possible)
SKIP_EXTREME_PRICE_THRESHOLD = 0.04  # Skip if price < 0.04 or > 0.96

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = "logs"
TRADE_LOG_FILE = "logs/trades.jsonl"
BOT_LOG_FILE = "logs/bot.log"
LEDGER_FILE = "logs/ledger.jsonl"

# ── City coordinates for weather lookups ──────────────────────────────────────
# (lat, lon, country_code)  — country_code "US" triggers NOAA, others use Open-Meteo
# Mapped to Kalshi series tickers where applicable
CITY_COORDS = {
    # US cities (matched to Kalshi series)
    "new york":     (40.7128, -74.0060, "US"),
    "nyc":          (40.7128, -74.0060, "US"),
    "chicago":      (41.8781, -87.6298, "US"),
    "miami":        (25.7617, -80.1918, "US"),
    "los angeles":  (34.0522, -118.2437, "US"),
    "la":           (34.0522, -118.2437, "US"),
    "houston":      (29.7604, -95.3698, "US"),
    "phoenix":      (33.4484, -112.0740, "US"),
    "philadelphia": (39.9526, -75.1652, "US"),
    "san antonio":  (29.4241, -98.4936, "US"),
    "san diego":    (32.7157, -117.1611, "US"),
    "dallas":       (32.7767, -96.7970, "US"),
    "denver":       (39.7392, -104.9903, "US"),
    "seattle":      (47.6062, -122.3321, "US"),
    "washington":   (38.9072, -77.0369, "US"),
    "dc":           (38.9072, -77.0369, "US"),
    "boston":        (42.3601, -71.0589, "US"),
    "atlanta":      (33.7490, -84.3880, "US"),
    "san francisco": (37.7749, -122.4194, "US"),
    "sf":           (37.7749, -122.4194, "US"),
    # International cities
    "london":       (51.5074, -0.1278, "GB"),
    "paris":        (48.8566,  2.3522, "FR"),
    "tokyo":        (35.6762, 139.6503, "JP"),
    "seoul":        (37.5665, 126.9780, "KR"),
    "ankara":       (39.9334,  32.8597, "TR"),
    "wellington":   (-41.2866, 174.7756, "NZ"),
    "buenos aires": (-34.6037, -58.3816, "AR"),
    "sydney":       (-33.8688, 151.2093, "AU"),
    "mumbai":       (19.0760,  72.8777, "IN"),
    "cairo":        (30.0444,  31.2357, "EG"),
    "berlin":       (52.5200,  13.4050, "DE"),
    "toronto":      (43.6532, -79.3832, "CA"),
    "mexico city":  (19.4326, -99.1332, "MX"),
    "são paulo":    (-23.5505, -46.6333, "BR"),
    "sao paulo":    (-23.5505, -46.6333, "BR"),
}
