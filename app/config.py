"""All tunables come from environment variables so the image never needs rebuilding to tweak them."""

import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


# Bedrock
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are Silknet Assistant, a friendly and concise helper. Answer clearly and briefly. "
    "Use Markdown (lists, **bold**, `code`, fenced code blocks) when it helps readability.",
)
MAX_TOKENS = _int("MAX_TOKENS", 1024)

# Storage
DB_PATH = os.getenv("DB_PATH", "/data/app.db")

# Auth
SESSION_DAYS = _int("SESSION_DAYS", 7)
COOKIE_SECURE = _bool("COOKIE_SECURE", False)  # set true once served over HTTPS
ALLOW_SIGNUP = _bool("ALLOW_SIGNUP", True)

# Compaction: once the un-compacted part of a chat exceeds either trigger, everything except
# the last COMPACT_KEEP_RECENT messages is folded into a running summary.
COMPACT_TRIGGER_MESSAGES = _int("COMPACT_TRIGGER_MESSAGES", 16)
COMPACT_TRIGGER_CHARS = _int("COMPACT_TRIGGER_CHARS", 24000)
COMPACT_KEEP_RECENT = _int("COMPACT_KEEP_RECENT", 6)

# Rate limits (sliding windows)
RATE_IP_PER_MIN = _int("RATE_IP_PER_MIN", 120)        # every request, per IP
RATE_CHAT_PER_MIN = _int("RATE_CHAT_PER_MIN", 12)     # chat messages, per user
RATE_CHAT_PER_DAY = _int("RATE_CHAT_PER_DAY", 300)    # chat messages, per user
RATE_LOGIN_PER_MIN = _int("RATE_LOGIN_PER_MIN", 10)   # login attempts, per IP
RATE_LOGIN_PER_EMAIL = _int("RATE_LOGIN_PER_EMAIL", 10)  # login attempts per account / 15 min
RATE_SIGNUP_PER_HOUR = _int("RATE_SIGNUP_PER_HOUR", 5)   # signups, per IP

# IP blocking: abusive events add strikes; STRIKE_LIMIT strikes within STRIKE_WINDOW seconds
# blocks the IP for BLOCK_SECONDS.
STRIKE_LIMIT = _int("STRIKE_LIMIT", 20)
STRIKE_WINDOW = _int("STRIKE_WINDOW", 600)
BLOCK_SECONDS = _int("BLOCK_SECONDS", 3600)
IP_ALLOWLIST = {ip.strip() for ip in os.getenv("IP_ALLOWLIST", "").split(",") if ip.strip()}
TRUST_PROXY = _bool("TRUST_PROXY", False)  # only true behind a load balancer that sets X-Forwarded-For
