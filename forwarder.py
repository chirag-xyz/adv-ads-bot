import asyncio
import logging
from datetime import datetime, timedelta
from telethon import TelegramClient, helpers
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerIdInvalidError,
    UserDeactivatedError,
)
from telethon.tl.functions.messages import ForwardMessagesRequest, GetForumTopicsRequest
import database
import config

logger = logging.getLogger(__name__)

# In-memory flag to control the background loop
_running = True
_account_locks = {}

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

async def deliver_message(client: TelegramClient, entity, msg_to_send, source_chat=None, source_msg_id=None, topic_id=None):
    """Forward source messages when possible; send plain text only as a fallback."""
    if isinstance(msg_to_send, str):
        await client.send_message(entity, msg_to_send, reply_to=topic_id)
        return

    if source_chat and source_msg_id:
        if topic_id:
            await client(ForwardMessagesRequest(
                from_peer=source_chat,
                id=[source_msg_id],
                random_id=[helpers.generate_random_long()],
                to_peer=entity,
                top_msg_id=topic_id
            ))
        else:
            await client.forward_messages(entity, source_msg_id, from_peer=source_chat)
        return

    await client.forward_messages(entity, msg_to_send)

async def process_task_for_account(task, account, bot_client):
    """Executes forwarding for a single account and a single task."""
    user_id = task['user_id']
    task_id = task['task_id']
    target_types = task['target_types'].split(',')
    
    phone = account['phone']
    session_str = account['session_string']
    account_id = account['account_id']
    account_lock = _account_locks.setdefault(account_id, asyncio.Lock())

    async with account_lock:
        await _process_task_for_account_locked(task, account, bot_client)

async def _process_task_for_account_locked(task, account, bot_client):
    """Executes forwarding for a single account while its session is exclusively held."""
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
        can_forward_from_source = bool(source_chat and source_msg_id and int(source_chat) != int(user_id))
        
        msg_to_send = None
        if source_chat and source_msg_id:
            try:
                msg_to_send = await client.get_messages(source_chat, ids=source_msg_id)
            except Exception as e:
                logger.warning(f"Account {phone} could not fetch source message {source_msg_id} from {source_chat}: {e}")
        
        # Fallback to text if message not found
        if not msg_to_send and source_text and not can_forward_from_source:
            msg_to_send = source_text
            
        if not msg_to_send and not can_forward_from_source:
            logger.error(f"Task {task_id} source message is not available.")
            await bot_client.send_message(
                user_id,
                f"❌ **Task Failed!**\nCampaign #{task_id} could not find the source message to forward/copy."
            )
            return

        # Fetch dialogs
        dialogs = await client.get_dialogs()
        logger.info(f"Account {phone} has {len(dialogs)} dialogs.")
        
        # Filter target chats
        targets = []
        for dialog in dialogs:
            entity = dialog.entity
            is_dm = dialog.is_user and not entity.bot
            is_channel = dialog.is_channel and entity.broadcast
            # For Telethon, normal groups and supergroups which are not channels
            is_group = dialog.is_group
            
            # Check if forum/topic group
            is_topic_group = False
            if hasattr(entity, 'forum') and entity.forum:
                is_topic_group = True
                is_group = False # Mutually exclusive for target filtering
                
            if 'dm' in target_types and is_dm:
                targets.append(('dm', dialog))
            elif 'channel' in target_types and is_channel:
                targets.append(('channel', dialog))
            elif 'group' in target_types and is_group:
                targets.append(('group', dialog))
            elif 'topic' in target_types and is_topic_group:
                targets.append(('topic', dialog))

        if not targets:
            logger.info(f"No matching target chats found for account {phone}.")
            return
            
        logger.info(f"Account {phone} forwarding to {len(targets)} matching targets.")
        
        # Notify user that forwarding started
        start_msg = await bot_client.send_message(
            user_id,
            f"🚀 **Forwarding Started!**\n"
            f"Account: **{phone}** ({account['first_name']})\n"
            f"Campaign: #{task_id}\n"
            f"Targets: {len(targets)} chats."
        )
        
        # Forward to targets
        for chat_type, dialog in targets:
            if not _running:
                break
                
            entity = dialog.entity
            chat_name = dialog.name or "Unknown Chat"
            
            try:
                if chat_type == 'topic':
                    # Fetch active topics in this forum group
                    topics = await get_forum_topics(client, entity, limit=3)
                    if topics:
                        for topic in topics:
                            try:
                                await deliver_message(client, entity, msg_to_send, source_chat, source_msg_id, topic['id'])
                                success_count += 1
                                logger.debug(f"Forwarded message to topic '{topic['title']}' in '{chat_name}'")
                                await asyncio.sleep(config.FORWARD_DELAY)
                            except FloodWaitError as e:
                                logger.warning(f"Flood wait inside topic group: {e}")
                                flood_waits += 1
                                # If short, sleep
                                if e.seconds < 30:
                                    await asyncio.sleep(e.seconds)
                                    # retry once
                                    try:
                                        await deliver_message(client, entity, msg_to_send, source_chat, source_msg_id, topic['id'])
                                        success_count += 1
                                    except Exception:
                                        fail_count += 1
                                else:
                                    fail_count += 1
                            except Exception as e:
                                fail_count += 1
                                logger.debug(f"Failed topic send: {e}")
                    else:
                        # Fallback: send to General (no topic ID)
                        await deliver_message(client, entity, msg_to_send, source_chat, source_msg_id)
                        success_count += 1
                        await asyncio.sleep(config.FORWARD_DELAY)
                else:
                    # Normal DM, Group, or Channel
                    await deliver_message(client, entity, msg_to_send, source_chat, source_msg_id)
                    success_count += 1
                    logger.debug(f"Forwarded message to {chat_type} '{chat_name}'")
                    await asyncio.sleep(config.FORWARD_DELAY)
                    
            except FloodWaitError as e:
                logger.warning(f"FloodWaitError on account {phone}: must wait {e.seconds}s")
                flood_waits += 1
                if e.seconds < 45:
                    await asyncio.sleep(e.seconds)
                    # retry
                    try:
                        await deliver_message(client, entity, msg_to_send, source_chat, source_msg_id)
                        success_count += 1
                    except Exception:
                        fail_count += 1
                else:
                    logger.warning(f"Flood wait too long ({e.seconds}s), skipping chat '{chat_name}'")
                    fail_count += 1
            except (PeerIdInvalidError, UserDeactivatedError) as e:
                logger.warning(f"Failed sending to {chat_name}: {e}")
                fail_count += 1
            except Exception as e:
                logger.error(f"Error sending to {chat_name}: {e}")
                fail_count += 1
                
        # Send execution summary
        summary_text = (
            f"✅ **Forwarding Complete!**\n"
            f"Account: **{phone}** ({account['first_name']})\n"
            f"Campaign: #{task_id}\n"
            f"📈 Success: `{success_count}`\n"
            f"📉 Failed: `{fail_count}`\n"
            f"⏳ Flood Waits: `{flood_waits}`"
        )
        await bot_client.send_message(user_id, summary_text)
        
    except AuthKeyDuplicatedError:
        logger.error(f"Auth key duplicated for account {phone}; marking inactive")
        await database.set_account_active(account_id, False)
        await bot_client.send_message(
            user_id,
            f"⚠️ **Session Invalidated!**\nAccount **{phone}** was used from two IP addresses/processes at the same time, so Telegram revoked this session. Please stop any other copies of the bot and re-add the account."
        )
    except AuthKeyUnregisteredError:
        logger.error(f"Auth key unregistered for account {phone}")
        await database.set_account_active(account_id, False)
        await bot_client.send_message(
            user_id, 
            f"⚠️ **Re-Authentication Required!**\nAccount **{phone}** was disconnected by Telegram. Please log in again."
        )
    except Exception as e:
        logger.error(f"Failed to process task {task_id} for account {phone}: {e}")
        await bot_client.send_message(
            user_id,
            f"❌ **Task Error!**\nAccount **{phone}** hit an error while forwarding: {str(e)[:200]}"
        )
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

async def execute_task(task, bot_client):
    """Runs a task across all specified userbot accounts."""
    task_id = task['task_id']
    user_id = task['user_id']
    accounts_to_use_raw = task['accounts_to_use']
    
    # Get user's accounts
    user_accounts = await database.get_user_accounts(user_id)
    if not user_accounts:
        logger.warning(f"No accounts found for user {user_id}. Pausing task {task_id}")
        await database.update_task_status(task_id, 'paused')
        await bot_client.send_message(
            user_id, 
            f"⚠️ **Task Paused!**\nCampaign #{task_id} was paused because you don't have any logged-in accounts. Please log in an account first."
        )
        return

    # Filter accounts to use
    selected_accounts = []
    if accounts_to_use_raw == 'all':
        selected_accounts = [acc for acc in user_accounts if acc['is_active']]
    else:
        acc_ids = [int(x) for x in accounts_to_use_raw.split(',') if x.strip().isdigit()]
        selected_accounts = [acc for acc in user_accounts if acc['account_id'] in acc_ids and acc['is_active']]
        
    if not selected_accounts:
        logger.warning(f"No active accounts matches selection for task {task_id}.")
        await bot_client.send_message(
            user_id,
            f"⚠️ **Task Alert!**\nCampaign #{task_id} did not run because your selected accounts are currently logged out or disabled."
        )
        return

    # Process task for each selected account sequentially to keep rate limits sane and prevent cross-account bans
    for account in selected_accounts:
        if not _running:
            break
        await process_task_for_account(task, account, bot_client)

async def forwarder_worker_loop(bot_client):
    """Main background loop checking for due campaigns."""
    logger.info("Background forwarder loop started.")
    global _running
    
    # Let database initialize
    await asyncio.sleep(5)
    
    while _running:
        try:
            due_tasks = await database.get_due_tasks()
            if due_tasks:
                logger.info(f"Found {len(due_tasks)} tasks due for execution.")
                for task in due_tasks:
                    if not _running:
                        break
                        
                    task_id = task['task_id']
                    interval = task['interval_minutes']
                    
                    # Lock task by updating next run immediately (to avoid double runs)
                    now = datetime.now()
                    next_run = now + timedelta(minutes=interval)
                    await database.update_task_next_run(task_id, now, next_run)
                    
                    # Run task execution asynchronously
                    # We spawn it in the background so one slow task doesn't delay others
                    asyncio.create_task(execute_task(task, bot_client))
                    
            # Check every 30 seconds
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            logger.info("Forwarder loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in forwarder loop: {e}")
            await asyncio.sleep(30)
            
    logger.info("Background forwarder loop stopped.")
