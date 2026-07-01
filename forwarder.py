import asyncio
import logging
import random
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PeerIdInvalidError, UserDeactivatedError, AuthKeyUnregisteredError
from telethon.tl.functions.messages import GetForumTopicsRequest, ForwardMessagesRequest
import database
import config

logger = logging.getLogger(__name__)

# In-memory flag to control the background loop
_running = True

def stop_forwarder():
    global _running
    _running = False

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

async def forward_or_send_message(client: TelegramClient, entity, msg_to_send, source_chat=None, topic_id=None):
    """Forward the original message when possible; send text only as a fallback."""
    if isinstance(msg_to_send, str):
        return await client.send_message(entity, msg_to_send, reply_to=topic_id)

    if topic_id is not None:
        from_peer = await client.get_input_entity(source_chat or msg_to_send.peer_id)
        to_peer = await client.get_input_entity(entity)
        return await client(ForwardMessagesRequest(
            from_peer=from_peer,
            id=[msg_to_send.id],
            random_id=[random.getrandbits(63)],
            to_peer=to_peer,
            top_msg_id=topic_id
        ))

    return await client.forward_messages(entity, msg_to_send)

async def process_task_for_account(task, account, bot_client):
    """Executes forwarding for a single account and a single task."""
    user_id = task['user_id']
    task_id = task['task_id']
    target_types = task['target_types'].split(',')
    
    phone = account['phone']
    session_str = account['session_string']
    account_id = account['account_id']
    
    logger.info(f"Processing task {task_id} for account {phone}")
    
    client = None
    success_count = 0
    fail_count = 0
    flood_waits = 0
    
    try:
        client = await get_client_for_session(session_str)
        if not await client.is_user_authorized():
            logger.warning(f"Account {phone} is not authorized. Marking inactive.")
            await database.set_account_active(account_id, False)
            await bot_client.send_message(
                user_id, 
                f"⚠️ **Account Logged Out!**\nYour account **{phone}** has been logged out of Telegram. Please re-add it to continue forwarding."
            )
            return
            
        # Get source message
        source_chat = task['source_chat_id']
        source_msg_id = task['source_msg_id']
        source_text = task['source_text']
        
