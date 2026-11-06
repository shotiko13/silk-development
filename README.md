# Silknet Chatbot

FastAPI chatbot on Claude Haiku 4.5 (Amazon Bedrock) with email/password auth, saved chats,
automatic chat compaction, rate limiting and abusive-IP blocking.

## Layout
| File | Purpose |
|---|---|
| `app/main.py` | Routes, security middleware (IP block check, global rate limit, 404 strikes) |
| `app/security.py` | scrypt password hashing, sessions, `RateLimiter`, `IpGuard` |
| `app/chat.py` | Conversation storage, Bedrock Converse calls, compaction |
| `app/db.py` | SQLite schema (`/data/app.db`) |
| `app/admin.py` | CLI to list / block / unblock IPs |
| `app/static/` | UI (`index.html`, `app.css`, `app.js`) |

## How the features work
- **Auth**: sign up / sign in with email + password (min 8 chars). Passwords are hashed with scrypt; sessions are random tokens (only their SHA-256 is stored) in an HttpOnly, SameSite=Lax cookie valid 7 days.
- **Compaction**: when the un-compacted part of a chat exceeds 16 messages or ~24k characters, everything except the last ~6 messages is summarized by Haiku into a running summary. The model then gets *summary + recent messages*; the full history is still stored and shown, with a "compacted" divider you can click to read the summary.
- **Rate limits**: 120 req/min per IP (all routes), 12 chat messages/min and 300/day per user, 10 logins/min per IP, 10 logins / 15 min per account, 5 signups/hour per IP. Responses are `429` with `Retry-After`.
- **IP blocking**: abusive events add strikes: failed login (2), hitting any rate limit (1), 404s / path scanning (1), invalid payloads (1). 20 strikes within 10 min blocks the IP for 1 hour (`403`). Blocks are stored in SQLite, so they survive restarts.

All thresholds are env vars; see `app/config.py`.

## Admin
```bash
sudo docker exec silknet-chatbot python -m app.admin list
sudo docker exec silknet-chatbot python -m app.admin unblock 1.2.3.4
sudo docker exec silknet-chatbot python -m app.admin block 1.2.3.4 120   # minutes
```
Or pass `-e IP_ALLOWLIST=1.2.3.4` to `docker run` so your own IP can never be blocked.

## Build & push (from Mac; image must be amd64 for t3 instances)
```bash
REPO=881490130721.dkr.ecr.us-east-1.amazonaws.com/silknet-deployment-shota
aws ecr get-login-password | docker login --username AWS --password-stdin 881490130721.dkr.ecr.us-east-1.amazonaws.com
docker build --platform linux/amd64 -t "${REPO}:latest" .
docker push "${REPO}:latest"
```
(In zsh, write `"${REPO}:latest"`, not `$REPO:latest`, because zsh treats `:l` as a modifier.)

## Deploy on EC2 (`silknet-deployment-shota`, i-07bb7eddf0c9e4ef0)
```bash
ssh -i ~/silknet6-key.pem ec2-user@100.55.19.118
REG=881490130721.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | sudo docker login --username AWS --password-stdin $REG
sudo docker pull $REG/silknet-deployment-shota:latest
sudo docker rm -f silknet-chatbot
sudo docker run -d --name silknet-chatbot --restart unless-stopped -p 8000:8000 \
  -v silknet-data:/data $REG/silknet-deployment-shota:latest
```
The `silknet-data` volume holds users, chats and IP blocks, so keep it across redeploys.
The instance uses IAM role `silknet-deployment-shota-role` (ECR read-only + Bedrock invoke for Haiku 4.5), so it needs no access keys.

## Caveats
- Served over plain HTTP, so the session cookie isn't `Secure`. Put it behind HTTPS (ALB + ACM, or Caddy) and set `COOKIE_SECURE=true`.
- Rate-limit and strike counters live in process memory (single worker). Use Redis if you run several replicas.
