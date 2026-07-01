import asyncio
import logging
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PeerIdInvalidError, UserDeactivatedError, AuthKeyUnregisteredError
from telethon.tl.functions.messages import GetForumTopicsRequest
import database
import config

logger = logging.getLogger(__name__)

# In-memory flag to control the background loop
_running = True

def stop_forwarder():
    global _running
    _running = False

def normalize_chat_id(chat_id):
    """Convert numeric chat IDs stored as strings into ints for Telethon."""
    if chat_id is None:
        return None

    if isinstance(chat_id, int):
        return chat_id

    chat_id = str(chat_id).strip()
    if not chat_id:
        return None

    try:
        return int(chat_id)
    except ValueError:
        return chat_id

def task_value(task, key, default=None):
    """Read task values from either dicts or sqlite-style row objects."""
    if hasattr(task, 'get'):
        return task.get(key, default)

    try:
        return task[key]
    except (KeyError, IndexError):
        return default

async def resolve_source_peer(client: TelegramClient, task):
    """Resolve a channel/group source from any field the campaign may have stored."""
    candidates = [
        task_value(task, 'source_chat_username'),
        task_value(task, 'source_username'),
        task_value(task, 'source_chat'),
        normalize_chat_id(task_value(task, 'source_chat_id')),
    ]

    last_error = None
    for candidate in candidates:
        if not candidate:
            continue

        try:
            return await client.get_input_entity(candidate)
        except Exception as e:
            last_error = e
            logger.debug(f"Could not resolve source peer {candidate!r}: {e}")

    if last_error:
        raise last_error

    return None

async def get_client_for_session(session_str: str) -> TelegramClient:
    """Creates and starts a Telethon client for a session string."""
    client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
    await client.connect()
    return client

async def get_forum_topics(client: TelegramClient, channel_entity, limit=5):
    """Fetches active topics in a forum/topic group."""
    try:
        result = await client(GetForumTopicsRequest(
            channel=channel_entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=limit
        ))
        topics = []
        if result and result.topics:
            for topic in result.topics:
                # topic.id is the message ID representing the topic
                topics.append({
                    'id': topic.id,
                    'title': topic.title
                })
        return topics
    except Exception as e:
        logger.debug(f"Failed to fetch forum topics for {channel_entity.id}: {e}")
        return []

