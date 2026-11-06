"""Conversation storage, Bedrock calls and chat compaction."""

import logging
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import config, db

log = logging.getLogger("silknet.chat")
bedrock = boto3.client("bedrock-runtime", region_name=config.REGION)

SUMMARY_PROMPT = (
    "You maintain a running summary of a conversation between a user and an AI assistant. "
    "Merge the previous summary (if any) with the new messages into a single concise summary. "
    "Preserve facts, names, numbers, decisions, user preferences, open questions and any code "
    "that may be referenced later. Write in third person, under 250 words. Output only the summary."
)


class ChatError(Exception):
    """Bedrock call failed."""


class NotFound(Exception):
    """Conversation doesn't exist or belongs to someone else."""


def _converse(system: str, messages: list[dict], max_tokens: int, temperature: float = 0.7):
    try:
        resp = bedrock.converse(
            modelId=config.MODEL_ID,
            system=[{"text": system}],
            messages=messages,
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
    except (ClientError, BotoCoreError) as e:
        log.error("Bedrock error: %s", e)
        raise ChatError(str(e)) from e
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    return text, resp.get("usage", {})


def _as_bedrock(msgs) -> list[dict]:
    return [{"role": m["role"], "content": [{"text": m["content"]}]} for m in msgs]


# ---------------------------------------------------------------- compaction


def _needs_compaction(active) -> bool:
    return (
        len(active) > config.COMPACT_TRIGGER_MESSAGES
        or sum(len(m["content"]) for m in active) > config.COMPACT_TRIGGER_CHARS
    )


def _split_for_compaction(active):
    """Keep the last COMPACT_KEEP_RECENT messages; the kept part must start with a user turn
    (Bedrock requires conversations to start with the user)."""
    cut = max(len(active) - config.COMPACT_KEEP_RECENT, 0)
    while cut < len(active) and active[cut]["role"] != "user":
        cut += 1
    return active[:cut], active[cut:]


def _summarize(previous: str | None, msgs) -> str:
    transcript = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs)
    prompt = (f"Previous summary:\n{previous}\n\n" if previous else "") + f"New messages:\n{transcript}"
    text, _ = _converse(SUMMARY_PROMPT, [{"role": "user", "content": [{"text": prompt}]}], 600, 0.2)
    return text.strip()


# ---------------------------------------------------------------- conversations


def _owned(conn, user_id: int, cid: int):
    row = conn.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (cid, user_id)).fetchone()
    if not row:
        raise NotFound
    return row


def list_conversations(user_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_conversation(user_id: int) -> dict:
    now = time.time()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, 'New chat', ?, ?)",
            (user_id, now, now),
        )
        return {"id": cur.lastrowid, "title": "New chat", "updated_at": now}


def get_conversation(user_id: int, cid: int) -> dict:
    with db.connect() as conn:
        conv = _owned(conn, user_id, cid)
        msgs = conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (cid,)
        ).fetchall()
    return {
        "id": conv["id"],
        "title": conv["title"],
        "summary": conv["summary"],
        "compacted_upto": conv["compacted_upto"],
        "messages": [dict(m) for m in msgs],
    }


def delete_conversation(user_id: int, cid: int) -> None:
    with db.connect() as conn:
        _owned(conn, user_id, cid)
        conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))


def send_message(user_id: int, cid: int, content: str) -> dict:
    now = time.time()
    with db.connect() as conn:
        conv = _owned(conn, user_id, cid)
        user_msg_id = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (cid, content, now),
        ).lastrowid
        active = conn.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id",
            (cid, conv["compacted_upto"]),
        ).fetchall()

    # Bedrock calls happen outside any open DB transaction.
    summary, compacted = conv["summary"], False
    try:
        if _needs_compaction(active):
            old, keep = _split_for_compaction(active)
            if old:
                summary = _summarize(summary, old)
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE conversations SET summary = ?, compacted_upto = ? WHERE id = ?",
                        (summary, old[-1]["id"], cid),
                    )
                log.info("Compacted conversation %s: %d messages folded into summary", cid, len(old))
                active, compacted = keep, True

        system = config.SYSTEM_PROMPT
        if summary:
            system += (
                "\n\nThe earlier part of this conversation was compacted. Summary of it:\n" + summary
            )
        reply, usage = _converse(system, _as_bedrock(active), config.MAX_TOKENS)
    except ChatError:
        with db.connect() as conn:  # don't leave an unanswered user turn behind
            conn.execute("DELETE FROM messages WHERE id = ?", (user_msg_id,))
        raise

    title = conv["title"]
    if title == "New chat":
        first_line = content.strip().splitlines()[0]
        title = first_line[:48] + ("…" if len(first_line) > 48 else "")
    with db.connect() as conn:
        reply_id = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (cid, reply, time.time()),
        ).lastrowid
        conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), cid))

    return {
        "user_message_id": user_msg_id,
        "message_id": reply_id,
        "reply": reply,
        "title": title,
        "compacted": compacted,
        "model": config.MODEL_ID,
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
    }
