import logging
import asyncio
import re
from datetime import datetime
from telethon import TelegramClient, events, Button, utils
from telethon.errors import UserNotParticipantError, ChannelPrivateError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.types import ChannelParticipant

import config
import database
import user_manager

logger = logging.getLogger(__name__)

# User states dictionary: user_id -> {'state': str, 'data': dict}
_user_states = {}

def get_user_state(user_id: int):
    return _user_states.get(user_id, {'state': 'idle', 'data': {}})

def set_user_state(user_id: int, state: str, data: dict = None):
    _user_states[user_id] = {'state': state, 'data': data or {}}

def clear_user_state(user_id: int):
    if user_id in _user_states:
        del _user_states[user_id]

# --- Force Join Helper ---

async def verify_force_join(bot_client: TelegramClient, user_id: int) -> tuple[bool, list]:
    """
    Checks if the user is in all force-join channels.
    Returns (is_joined, list_of_missing_channel_links)
    """
    # Admins bypass force join
    if await database.is_admin(user_id) or user_id in config.ADMINS:
        return True, []
        
    force_joins = await database.get_setting("force_join_channels", config.DEFAULT_FORCE_JOINS)
    if not force_joins:
        return True, []
        
    missing_channels = []
    for fj in force_joins:
        chat_id = fj.get("channel_id")
        link = fj.get("invite_link")
        name = fj.get("name", "Sponsor Channel")
        
        if not chat_id or not link:
            continue
            
        try:
            participant = await bot_client(GetParticipantRequest(
                channel=chat_id,
                participant=user_id
            ))
            # Check if user has left or is banned
            if not participant or isinstance(participant.participant, (ChannelParticipant)):
                # Normal participant, admin, or creator is fine
                pass
        except UserNotParticipantError:
            missing_channels.append({"name": name, "link": link})
        except Exception as e:
            # If the bot is not in the channel or has other permission issue, log it
            logger.warning(f"Error checking force join for channel {chat_id}: {e}")
            # Do not block the user if the check itself fails due to bot configuration issues
            
    if missing_channels:
        return False, missing_channels
    return True, []

async def send_force_join_block(event, missing_channels):
    """Sends the force join keyboard block."""
    text = (
        "⚠️ **Access Denied!**\n\n"
        "To use this bot, you must first join our sponsor channels. "
        "This helps keep this service free and high quality.\n\n"
        "Please join the channels listed below and click **Verify**:"
    )
    
    buttons = []
    for index, ch in enumerate(missing_channels, 1):
        buttons.append([Button.url(f"📢 Join {ch['name']}", ch['link'])])
    
    buttons.append([Button.inline("🔄 Verify Membership", "verify_fj")])
    
    await event.respond(text, buttons=buttons)

# --- Navigation / Keyboards ---

def get_main_menu_keyboard(is_admin_user: bool = False):
    buttons = [
        [Button.inline("📱 Manage Accounts", "menu_accounts"), Button.inline("📢 Manage Campaigns", "menu_campaigns")],
        [Button.inline("ℹ️ Help & Info", "menu_help")]
    ]
    if is_admin_user:
        buttons.append([Button.inline("⚙️ Admin Control Panel", "menu_admin")])
    return buttons

async def send_main_menu(event, user_id: int, edit=False):
    is_admin_user = await database.is_admin(user_id) or user_id in config.ADMINS
    text = (
        "👋 **Welcome to the Professional Ad Forwarder Bot!**\n\n"
        "Maximize your reach with our automated scheduled forwarder. "
        "Connect up to **5 Telegram accounts** and distribute messages to all "
        "DMs, Channels, Groups, and Topics in minutes.\n\n"
        "⚡ **Bot Features:**\n"
        "• Asynchronous multi-account forwarding\n"
        "• Target specific chat types (DMs, GCs, Channels, Topics)\n"
        "• Custom scheduled intervals\n"
        "• Anti-ban protection (delay settings & flood wait handling)\n\n"
        "👉 Use the menu buttons below to manage your settings."
    )
    
    if edit:
        try:
            await event.edit(text, buttons=get_main_menu_keyboard(is_admin_user))
            return
        except Exception:
            pass
    await event.respond(text, buttons=get_main_menu_keyboard(is_admin_user))

# --- Bot Handler Definitions ---

def register_bot_handlers(bot: TelegramClient):
    
    # Check force join for all incoming events unless they are callbacks verifying membership
    async def global_block_check(event):
        if not event.is_private:
            return True # Only private chat interactions
            
        user_id = event.sender_id
        if not user_id:
            return True
            
        # Register user in DB if they don't exist
        username = event.sender.username or ""
        await database.add_user(user_id, username, is_admin=(user_id in config.ADMINS))
        
        # Check if banned
        if await database.is_banned(user_id):
            await event.respond("❌ **Access Denied!**\nYou have been banned from using this bot.")
            return True # Stop propagation
            
        # If they are clicking 'verify_fj' or are admins, allow it
        if isinstance(event, events.CallbackQuery.Event) and event.data == b"verify_fj":
            return False # Let handler process
            
        # Verify force join
        is_joined, missing = await verify_force_join(bot, user_id)
        if not is_joined:
            await send_force_join_block(event, missing)
            raise events.StopPropagation # Block further handlers
            
        return False

    # Hook the check into all events
    @bot.on(events.NewMessage(incoming=True))
    async def on_new_message_check(event):
        await global_block_check(event)
        
    @bot.on(events.CallbackQuery())
    async def on_callback_query_check(event):
        await global_block_check(event)

    # --- Start Command ---
    @bot.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        user_id = event.sender_id
        clear_user_state(user_id)
        await send_main_menu(event, user_id)

    # --- Verification Callback ---
    @bot.on(events.CallbackQuery(pattern="verify_fj"))
    async def verify_fj_handler(event):
        user_id = event.sender_id
        is_joined, missing = await verify_force_join(bot, user_id)
        if is_joined:
            await event.answer("✅ Verification successful! Welcome back.", alert=True)
            await send_main_menu(event, user_id, edit=True)
        else:
            await event.answer("❌ You still haven't joined all required channels!", alert=True)

    # --- Main Menu Navigation ---
    @bot.on(events.CallbackQuery(pattern="menu_main"))
    async def menu_main_handler(event):
        user_id = event.sender_id
        clear_user_state(user_id)
        await send_main_menu(event, user_id, edit=True)

    # --- Help & Info ---
    @bot.on(events.CallbackQuery(pattern="menu_help"))
    async def menu_help_handler(event):
        text = (
            "ℹ️ **How to use the Ad Forwarder Bot:**\n\n"
            "1️⃣ **Link Accounts**:\n"
            "   Go to **Manage Accounts** and click **Add Account**. Enter your phone number "
            "and the OTP code Telegram sends you. You can link up to 5 accounts.\n\n"
            "2️⃣ **Create a Campaign**:\n"
            "   Go to **Manage Campaigns** and click **Create Campaign**. Send the message you "
            "wish to forward, select targets (DMs, Channels, Groups, or Forum Topics), set the interval, "
            "and select which accounts to send from.\n\n"
            "3️⃣ **Monitor Progress**:\n"
            "   The bot will run your campaigns in the background at the specified interval. You will "
            "receive a message showing the delivery statistics (Success/Failed/Flood Wait) after each run.\n\n"
            "⚠️ **Anti-Ban Safety Tip:** We enforce a safe delay (4 seconds) between messages. To prevent "
            "bans, avoid scheduling short intervals (e.g. less than 15 minutes) for a large number of chats."
        )
        buttons = [[Button.inline("⬅️ Back to Menu", "menu_main")]]
        await event.edit(text, buttons=buttons)

    # --- Manage Accounts Section ---
    @bot.on(events.CallbackQuery(pattern="menu_accounts"))
    async def menu_accounts_handler(event):
        user_id = event.sender_id
        accounts = await database.get_user_accounts(user_id)
        
        text = (
            "📱 **Account Management**\n\n"
            f"Active linked accounts: `{len(accounts)} / 5`\n\n"
        )
        
        if accounts:
            text += "Here are your linked accounts:\n"
            for index, acc in enumerate(accounts, 1):
                status_emoji = "✅ Active" if acc['is_active'] else "⚠️ Disconnected"
                text += f" {index}. **{acc['phone']}** - {acc['first_name']} ({status_emoji})\n"
        else:
            text += "⚡ _No Telegram accounts linked yet._"
            
        buttons = []
        # List accounts with delete buttons
        if accounts:
            for acc in accounts:
                buttons.append([
                    Button.inline(f"🗑️ Delete {acc['phone']}", f"del_acc_{acc['account_id']}")
                ])
                
        # Only show add button if user has < 5 accounts
        if len(accounts) < 5:
            buttons.append([Button.inline("➕ Add Account", "add_account")])
            
        buttons.append([Button.inline("⬅️ Back to Menu", "menu_main")])
        await event.edit(text, buttons=buttons)

    # --- Add Account Flow ---
    @bot.on(events.CallbackQuery(pattern="add_account"))
    async def add_account_handler(event):
        user_id = event.sender_id
        accounts = await database.get_user_accounts(user_id)
        if len(accounts) >= 5:
            await event.answer("⚠️ You can only link a maximum of 5 accounts.", alert=True)
            return
            
        set_user_state(user_id, "wait_phone")
        text = (
            "📱 **Add Telegram Account (1/3)**\n\n"
            "Please send the phone number of the Telegram account you want to link. "
            "The phone number must be in the international format (with country code).\n\n"
            "Example: `+1234567890`"
        )
        buttons = [[Button.inline("❌ Cancel", "menu_accounts")]]
        await event.edit(text, buttons=buttons)

    # --- Delete Account ---
    @bot.on(events.CallbackQuery(pattern=r"del_acc_(\d+)"))
    async def del_account_handler(event):
        account_id = int(event.pattern_match.group(1))
        account = await database.get_account(account_id)
        if account and account['user_id'] == event.sender_id:
            await database.delete_account(account_id)
            await event.answer(f"🗑️ Account {account['phone']} deleted.", alert=True)
        await menu_accounts_handler(event)

    # --- Manage Campaigns Section ---
    @bot.on(events.CallbackQuery(pattern="menu_campaigns"))
    async def menu_campaigns_handler(event):
        user_id = event.sender_id
        tasks = await database.get_user_tasks(user_id)
        
        text = (
            "📢 **Campaign Management**\n\n"
            f"Your active forwarding campaigns: `{len(tasks)}`\n\n"
        )
        
        buttons = []
        if tasks:
            text += "Here are your active campaigns:\n\n"
            for index, task in enumerate(tasks, 1):
                status_emoji = "▶️ Running" if task['status'] == 'active' else "⏸️ Paused"
                # Shorten details
                targets = task['target_types'].upper()
                text += (
                    f"**Campaign #{task['task_id']}** ({status_emoji})\n"
                    f"• Interval: `{task['interval_minutes']}m` | Targets: `{targets}`\n"
                    f"• Next Run: `{task['next_run_at']}`\n\n"
                )
                
                # Dynamic action buttons
                toggle_text = "⏸️ Pause" if task['status'] == 'active' else "▶️ Resume"
                buttons.append([
                    Button.inline(toggle_text, f"toggle_task_{task['task_id']}"),
                    Button.inline("🗑️ Delete", f"del_task_{task['task_id']}")
                ])
        else:
            text += "⚡ _No forwarding campaigns created yet._"
            
        buttons.append([Button.inline("➕ Create Campaign", "create_campaign")])
        buttons.append([Button.inline("⬅️ Back to Menu", "menu_main")])
        await event.edit(text, buttons=buttons)

    # --- Toggle/Delete Campaign ---
    @bot.on(events.CallbackQuery(pattern=r"toggle_task_(\d+)"))
    async def toggle_task_handler(event):
        task_id = int(event.pattern_match.group(1))
        task = await database.get_task(task_id)
        if task and task['user_id'] == event.sender_id:
            new_status = 'paused' if task['status'] == 'active' else 'active'
            await database.update_task_status(task_id, new_status)
            await event.answer(f"Campaign #{task_id} status updated to {new_status}.", alert=True)
        await menu_campaigns_handler(event)

    @bot.on(events.CallbackQuery(pattern=r"del_task_(\d+)"))
    async def del_task_handler(event):
        task_id = int(event.pattern_match.group(1))
        task = await database.get_task(task_id)
        if task and task['user_id'] == event.sender_id:
            await database.delete_task(task_id)
            await event.answer(f"🗑️ Campaign #{task_id} deleted successfully.", alert=True)
        await menu_campaigns_handler(event)

    # --- Create Campaign Flow ---
    @bot.on(events.CallbackQuery(pattern="create_campaign"))
    async def create_campaign_handler(event):
        user_id = event.sender_id
        accounts = await database.get_user_accounts(user_id)
        if not accounts:
            await event.answer("⚠️ You must link at least one Telegram account before creating a campaign.", alert=True)
            return
            
        set_user_state(user_id, "camp_msg")
        text = (
            "📢 **Create Forwarding Campaign (1/4)**\n\n"
            "Please **forward** the post you want to distribute, or **send a text message** directly. "
            "All media types (images, videos, files) are supported if forwarded."
        )
        buttons = [[Button.inline("❌ Cancel", "menu_campaigns")]]
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"camp_tgt_(toggle|next)"))
    async def camp_targets_handler(event):
        user_id = event.sender_id
        state_data = get_user_state(user_id)
        
        if state_data['state'] != 'camp_targets':
            return
            
        data = state_data['data']
        selected = data.setdefault('targets', [])
        
        action = event.pattern_match.group(1).decode('utf-8')
        if action == 'toggle':
            # This is handled separately via specific pattern, but just in case
            pass
        elif action == 'next':
            if not selected:
                await event.answer("⚠️ Please select at least one target type!", alert=True)
                return
            # Move to next step (interval)
            set_user_state(user_id, "camp_interval", data)
            text = (
                "📢 **Create Forwarding Campaign (3/4)**\n\n"
                "Please send the forwarding interval in minutes.\n"
                "Minimum: `10` minutes.\n\n"
                "Example: Send `60` for every hour."
            )
            buttons = [[Button.inline("❌ Cancel", "menu_campaigns")]]
            await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"camp_tgt_toggle_(.+)"))
    async def camp_tgt_toggle_handler(event):
        user_id = event.sender_id
        state_data = get_user_state(user_id)
        if state_data['state'] != 'camp_targets':
            return
            
        target_type = event.pattern_match.group(1).decode('utf-8')
        data = state_data['data']
        selected = data.setdefault('targets', [])
        
        if target_type in selected:
            selected.remove(target_type)
        else:
            selected.append(target_type)
            
        # Re-render selection screen
        text = (
            "📢 **Create Forwarding Campaign (2/4)**\n\n"
            "Select target chat types for this campaign.\n"
            "You can toggle multiple options:"
        )
        
        def btn_lbl(lbl, key):
            return f"✅ {lbl}" if key in selected else f"⬜ {lbl}"
            
        buttons = [
            [Button.inline(btn_lbl("Direct Messages (DMs)", "dm"), "camp_tgt_toggle_dm")],
            [Button.inline(btn_lbl("Broadcast Channels", "channel"), "camp_tgt_toggle_channel")],
            [Button.inline(btn_lbl("Groups (GCs)", "group"), "camp_tgt_toggle_group")],
            [Button.inline(btn_lbl("Forum Topics (Topic GCs)", "topic"), "camp_tgt_toggle_topic")],
            [Button.inline("Next Step ➡️", "camp_tgt_next")],
            [Button.inline("❌ Cancel", "menu_campaigns")]
        ]
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"camp_acc_(toggle|confirm)"))
    async def camp_accounts_handler(event):
        user_id = event.sender_id
        state_data = get_user_state(user_id)
        if state_data['state'] != 'camp_accounts':
            return
            
        data = state_data['data']
        action = event.pattern_match.group(1).decode('utf-8')
        
        if action == 'confirm':
            selected_accs = data.get('accounts', [])
            if not selected_accs:
                await event.answer("⚠️ Please select at least one account or choose All!", alert=True)
                return
                
            # Create Campaign in Database!
            try:
                # Target types
                targets = data['targets']
                interval = data['interval']
                
                # Source message info
                src_chat = data.get('source_chat_id')
                src_msg = data.get('source_msg_id')
                src_text = data.get('source_text')
                
                # Accounts
                accounts_to_use = selected_accs
                if 'all' in selected_accs:
                    accounts_to_use = ['all']
                    
                await database.add_task(
                    user_id=user_id,
                    source_chat_id=src_chat,
                    source_msg_id=src_msg,
                    source_text=src_text,
                    target_types=targets,
                    interval_minutes=interval,
                    accounts_to_use=accounts_to_use
                )
                
                await event.answer("🚀 Campaign created successfully!", alert=True)
            except Exception as e:
                logger.error(f"Failed to create campaign: {e}")
                await event.answer("❌ Error creating campaign. Please try again.", alert=True)
                
            clear_user_state(user_id)
            await menu_campaigns_handler(event)

    @bot.on(events.CallbackQuery(pattern=r"camp_acc_toggle_(.+)"))
    async def camp_acc_toggle_handler(event):
        user_id = event.sender_id
        state_data = get_user_state(user_id)
        if state_data['state'] != 'camp_accounts':
            return
            
        acc_val = event.pattern_match.group(1).decode('utf-8')
        data = state_data['data']
        selected = data.setdefault('accounts', [])
        
        if acc_val == 'all':
            if 'all' in selected:
                selected.clear()
            else:
                selected.clear()
                selected.append('all')
        else:
            if 'all' in selected:
                selected.remove('all')
            acc_id = int(acc_val)
            if acc_id in selected:
                selected.remove(acc_id)
            else:
                selected.append(acc_id)
                
        # Re-render selection screen
        accounts = await database.get_user_accounts(user_id)
        text = (
            "📢 **Create Forwarding Campaign (4/4)**\n\n"
            "Select which accounts should execute this campaign:\n"
            "You can choose specific accounts or all:"
        )
        
        buttons = []
        all_lbl = "✅ All Accounts" if 'all' in selected else "⬜ All Accounts"
        buttons.append([Button.inline(all_lbl, "camp_acc_toggle_all")])
        
        for acc in accounts:
            is_sel = acc['account_id'] in selected or 'all' in selected
            lbl = f"✅ {acc['phone']}" if is_sel else f"⬜ {acc['phone']}"
            buttons.append([Button.inline(lbl, f"camp_acc_toggle_{acc['account_id']}")])
            
        buttons.append([Button.inline("🚀 Confirm & Schedule", "camp_acc_confirm")])
        buttons.append([Button.inline("❌ Cancel", "menu_campaigns")])
        await event.edit(text, buttons=buttons)

    # --- Admin Panel Section ---
    @bot.on(events.CallbackQuery(pattern="menu_admin"))
    async def menu_admin_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS):
            await event.answer("🚫 Unauthorized access.", alert=True)
            return
            
        clear_user_state(user_id)
        
        # Gather Stats
        users = await database.get_all_users()
        total_users = len(users)
        banned_users = sum(1 for u in users if u['is_banned'])
        
        # Count all accounts
        async with database.aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM accounts") as cursor:
                row = await cursor.fetchone()
                total_accounts = row[0] if row else 0
                
        active_tasks = await database.get_total_active_tasks_count()
        
        text = (
            "⚙️ **Admin Control Panel**\n\n"
            f"👥 **Total Users**: `{total_users}`\n"
            f"🚫 **Banned Users**: `{banned_users}`\n"
            f"📱 **Total Connected Accounts**: `{total_accounts}`\n"
            f"📢 **Active Campaigns**: `{active_tasks}`\n\n"
            "Choose an option below to configure settings:"
        )
        
        buttons = [
            [Button.inline("📢 Broadcast Message", "admin_broadcast")],
            [Button.inline("⚙️ Sponsor Channels (Force Join)", "admin_force_join")],
            [Button.inline("🚫 Manage Users", "admin_users_list")],
            [Button.inline("⬅️ Back to Menu", "menu_main")]
        ]
        await event.edit(text, buttons=buttons)

    # --- Admin Sponsor Channels (Force Join) ---
    @bot.on(events.CallbackQuery(pattern="admin_force_join"))
    async def admin_force_join_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS):
            return
            
        force_joins = await database.get_setting("force_join_channels", config.DEFAULT_FORCE_JOINS)
        
        text = (
            "📢 **Force Join Configuration**\n\n"
            "Users must join these channels to use the bot. "
            "You can specify up to 3 channels.\n\n"
            "**Current Configuration:**\n"
        )
        
        if force_joins:
            for idx, ch in enumerate(force_joins, 1):
                text += f"**{idx}. {ch.get('name')}**\n• ID: `{ch.get('channel_id')}`\n• Invite: {ch.get('invite_link')}\n\n"
        else:
            text += "⚡ _No sponsor channels configured. Force-join is disabled._\n\n"
            
        text += "⚠️ **Important:** The bot must be an Admin in these channels to check membership."
        
        buttons = [
            [Button.inline("➕ Add/Update Channel", "admin_add_fj")],
            [Button.inline("🧹 Reset All", "admin_reset_fj")],
            [Button.inline("⬅️ Back to Admin Menu", "menu_admin")]
        ]
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern="admin_reset_fj"))
    async def admin_reset_fj_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS): return
        
        await database.set_setting("force_join_channels", [])
        await event.answer("🧹 Force Join settings cleared.", alert=True)
        await admin_force_join_handler(event)

    @bot.on(events.CallbackQuery(pattern="admin_add_fj"))
    async def admin_add_fj_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS): return
        
        # Check current count
        fjs = await database.get_setting("force_join_channels", [])
        if len(fjs) >= 3:
            await event.answer("⚠️ You can configure at most 3 sponsor channels. Please reset first.", alert=True)
            return
            
        set_user_state(user_id, "admin_fj_id")
        text = (
            "⚙️ **Sponsor Channel Setup (1/3)**\n\n"
            "Please send the **Channel ID** (or username starting with @, e.g. `@my_channel`).\n"
            "For private channels, send the numeric ID (usually starts with -100)."
        )
        buttons = [[Button.inline("❌ Cancel", "admin_force_join")]]
        await event.edit(text, buttons=buttons)

    # --- Admin Broadcast ---
    @bot.on(events.CallbackQuery(pattern="admin_broadcast"))
    async def admin_broadcast_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS): return
        
        set_user_state(user_id, "admin_broadcast_msg")
        text = (
            "📢 **System Broadcast**\n\n"
            "Send the message you want to broadcast to ALL bot users. "
            "You can use formatting, links, and media."
        )
        buttons = [[Button.inline("❌ Cancel", "menu_admin")]]
        await event.edit(text, buttons=buttons)

    # --- Admin Manage Users List ---
    @bot.on(events.CallbackQuery(pattern="admin_users_list"))
    async def admin_users_list_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS): return
        
        users = await database.get_all_users()
        text = "👤 **User List Management**\n\n"
        
        buttons = []
        count = 0
        for u in users[:15]: # Show top 15 for UI size limits
            # Don't show control buttons for self
            if u['user_id'] == user_id:
                continue
                
            status_text = "🚫 Unban" if u['is_banned'] else "🛑 Ban"
            lbl = f"{u['username'] or u['user_id']} ({'Banned' if u['is_banned'] else 'Active'})"
            
            buttons.append([
                Button.inline(lbl, f"view_u_{u['user_id']}"),
                Button.inline(status_text, f"ban_u_{u['user_id']}")
            ])
            count += 1
            
        if not count:
            text += "_No other users registered in the database._"
            
        buttons.append([Button.inline("⬅️ Back to Admin Menu", "menu_admin")])
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"ban_u_(\d+)"))
    async def ban_u_handler(event):
        user_id = event.sender_id
        if not (await database.is_admin(user_id) or user_id in config.ADMINS): return
        
        target_uid = int(event.pattern_match.group(1))
        target_user = await database.get_user(target_uid)
        
        if target_user:
            new_ban_status = not bool(target_user['is_banned'])
            await database.set_banned(target_uid, new_ban_status)
            act = "banned" if new_ban_status else "unbanned"
            await event.answer(f"User {target_uid} {act}.", alert=True)
            
        await admin_users_list_handler(event)

    # --- Text Message & Inputs Handler (State Machine) ---
    @bot.on(events.NewMessage(incoming=True))
    async def input_handler(event):
        # Exclude commands
        if event.text.startswith('/'):
            return
            
        user_id = event.sender_id
        if not user_id:
            return
            
        state_data = get_user_state(user_id)
        state = state_data['state']
        data = state_data['data']
        
        if state == 'idle':
            return
            
        # 1. State: Wait Phone
        if state == 'wait_phone':
            phone = event.text.strip().replace(" ", "")
            if not re.match(r"^\+\d{7,15}$", phone):
                await event.respond("❌ **Invalid Phone Number!**\nPlease enter a valid phone number including '+' and country code (e.g. `+1234567890`).")
                return
                
            msg = await event.respond("⏳ **Connecting to Telegram and requesting verification code...**")
            try:
                res = await user_manager.start_login(user_id, phone)
                if res == "code_sent":
                    set_user_state(user_id, "wait_otp", {"phone": phone})
                    await msg.edit(
                        f"✉️ **OTP Code Sent to {phone}!**\n\n"
                        "Please enter the verification code you received from Telegram.\n"
                        "Note: If the code is `12345`, just send `12345` as a message."
                    )
            except Exception as e:
                logger.error(f"Error starting login: {e}")
                clear_user_state(user_id)
                await msg.edit(f"❌ **Login Initiation Failed!**\nError: `{str(e)[:150]}`\n\nPlease try again.")
                
        # 2. State: Wait OTP
        elif state == 'wait_otp':
            code = event.text.strip()
            phone = data['phone']
            msg = await event.respond("⏳ **Verifying code...**")
            try:
                res = await user_manager.submit_code(user_id, code)
                if res == "success":
                    clear_user_state(user_id)
                    await msg.edit(f"✅ **Account Link Successful!**\nYour account **{phone}** has been connected.")
                    # Show account menu
                    # Create custom callback event trigger
                    await bot.send_message(user_id, "Choose action below:", buttons=get_main_menu_keyboard(await database.is_admin(user_id)))
                elif res == "need_password":
                    set_user_state(user_id, "wait_2fa", {"phone": phone})
                    await msg.edit(
                        "🔑 **Two-Step Verification Enabled!**\n\n"
                        "Your account requires a 2-FA cloud password. Please send your password."
                    )
            except Exception as e:
                logger.error(f"Error submitting OTP: {e}")
                # Don't reset state so they can try entering OTP again if they mistyped
                await msg.edit(f"❌ **Code Verification Failed!**\n`{str(e)}`\n\nPlease enter the correct code again, or type `/start` to abort.")

        # 3. State: Wait 2FA Password
        elif state == 'wait_2fa':
            password = event.text.strip()
            phone = data['phone']
            msg = await event.respond("⏳ **Verifying 2-FA Password...**")
            try:
                res = await user_manager.submit_password(user_id, password)
                if res == "success":
                    clear_user_state(user_id)
                    await msg.edit(f"✅ **Account Link Successful!**\nYour account **{phone}** (with 2-FA) has been connected.")
                    await bot.send_message(user_id, "Choose action below:", buttons=get_main_menu_keyboard(await database.is_admin(user_id)))
            except Exception as e:
                logger.error(f"Error submitting password: {e}")
                await msg.edit(f"❌ **Authentication Failed!**\n`{str(e)}`\n\nPlease try your password again.")

        # 4. State: Campaign Setup - Message Input
        elif state == 'camp_msg':
            # Save the message source properties
            # If the user forwarded a message
            if event.fwd_from:
                data['source_chat_id'] = event.chat_id
                data['source_msg_id'] = event.id
                data['source_text'] = event.text or ""
            else:
                data['source_chat_id'] = None
                data['source_msg_id'] = None
                # We copy raw properties if text is sent
                data['source_text'] = event.text
                # Also save the message ID of this specific message they sent to the bot, 
                # so we can fetch it later to preserve media/formatting if sent directly
                data['source_chat_id'] = event.chat_id
                data['source_msg_id'] = event.id

            set_user_state(user_id, "camp_targets", data)
            
            # Show targets checklist
            text = (
                "📢 **Create Forwarding Campaign (2/4)**\n\n"
                "Select target chat types for this campaign.\n"
                "You can toggle multiple options:"
            )
            buttons = [
                [Button.inline("⬜ Direct Messages (DMs)", "camp_tgt_toggle_dm")],
                [Button.inline("⬜ Broadcast Channels", "camp_tgt_toggle_channel")],
                [Button.inline("⬜ Groups (GCs)", "camp_tgt_toggle_group")],
                [Button.inline("⬜ Forum Topics (Topic GCs)", "camp_tgt_toggle_topic")],
                [Button.inline("Next Step ➡️", "camp_tgt_next")],
                [Button.inline("❌ Cancel", "menu_campaigns")]
            ]
            await event.respond(text, buttons=buttons)

        # 5. State: Campaign Setup - Interval Input
        elif state == 'camp_interval':
            try:
                interval = int(event.text.strip())
                if interval < 10:
                    await event.respond("⚠️ **Minimum interval is 10 minutes** to protect your accounts from bans. Please send a larger number.")
                    return
            except ValueError:
                await event.respond("❌ **Invalid Input!** Please send a valid number (integer).")
                return
                
            data['interval'] = interval
            
            # Show account selection
            accounts = await database.get_user_accounts(user_id)
            if not accounts:
                await event.respond("❌ You don't have any linked accounts. Setting campaign failed.")
                clear_user_state(user_id)
                return
                
            # Default: select all
            data['accounts'] = ['all']
            set_user_state(user_id, "camp_accounts", data)
            
            text = (
                "📢 **Create Forwarding Campaign (4/4)**\n\n"
                "Select which accounts should execute this campaign:\n"
                "You can choose specific accounts or all:"
            )
            
            buttons = [
                [Button.inline("✅ All Accounts", "camp_acc_toggle_all")]
            ]
            for acc in accounts:
                # By default, since "all" is checked, individual ones are checked
                buttons.append([Button.inline(f"✅ {acc['phone']}", f"camp_acc_toggle_{acc['account_id']}")])
                
            buttons.append([Button.inline("🚀 Confirm & Schedule", "camp_acc_confirm")])
            buttons.append([Button.inline("❌ Cancel", "menu_campaigns")])
            
            await event.respond(text, buttons=buttons)

        # 6. State: Admin Broadcast Message
        elif state == 'admin_broadcast_msg':
            if not (await database.is_admin(user_id) or user_id in config.ADMINS):
                clear_user_state(user_id)
                return
                
            # Start background task to broadcast
            users = await database.get_all_users()
            await event.respond(f"📢 **Starting broadcast to {len(users)} users...**")
            
            success = 0
            failed = 0
            for u in users:
                try:
                    # Send copy of the message
                    await bot.send_message(u['user_id'], event.message)
                    success += 1
                    await asyncio.sleep(0.05) # Tiny sleep to avoid flood waits
                except Exception as e:
                    logger.debug(f"Broadcast failed for user {u['user_id']}: {e}")
                    failed += 1
                    
            await event.respond(
                f"✅ **Broadcast Finished!**\n\n"
                f"📈 Successful: `{success}`\n"
                f"📉 Failed: `{failed}`",
                buttons=[[Button.inline("⬅️ Back to Admin Panel", "menu_admin")]]
            )
            clear_user_state(user_id)

        # 7. State: Admin Force Join ID
        elif state == 'admin_fj_id':
            if not (await database.is_admin(user_id) or user_id in config.ADMINS):
                clear_user_state(user_id)
                return
                
            val = event.text.strip()
            # If numeric ID
            if val.startswith('-100') or val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
                try:
                    chat_id = int(val)
                except ValueError:
                    await event.respond("❌ Invalid Channel ID format. Please send digits.")
                    return
            elif val.startswith('@'):
                # We can try to resolve it using the bot client
                try:
                    entity = await bot.get_entity(val)
                    chat_id = entity.id
                    # Format standard channel ID
                    if hasattr(entity, 'broadcast') and entity.broadcast:
                        # For telethon, get_entity already resolves correct ID, but check type
                        pass
                except Exception as e:
                    await event.respond(f"❌ Failed to resolve username {val}: {e}\nPlease verify the bot is in this channel/group.")
                    return
            else:
                await event.respond("❌ Format invalid. Send numeric ID (starts with -100) or @username.")
                return
                
            data['channel_id'] = chat_id
            set_user_state(user_id, "admin_fj_link", data)
            
            await event.respond(
                "⚙️ **Sponsor Channel Setup (2/3)**\n\n"
                "Please send the **Invite Link** or public link for this channel (e.g. `https://t.me/my_channel`)."
            )

        # 8. State: Admin Force Join Link
        elif state == 'admin_fj_link':
            if not (await database.is_admin(user_id) or user_id in config.ADMINS):
                clear_user_state(user_id)
                return
                
            link = event.text.strip()
            if not link.startswith("https://t.me/"):
                await event.respond("❌ Link must start with `https://t.me/`. Please send a valid link.")
                return
                
            data['invite_link'] = link
            set_user_state(user_id, "admin_fj_name", data)
            
            await event.respond(
                "⚙️ **Sponsor Channel Setup (3/3)**\n\n"
                "Please send a short **Display Name** for this channel (e.g. `Sponsor Chat`)."
            )

        # 9. State: Admin Force Join Name
        elif state == 'admin_fj_name':
            if not (await database.is_admin(user_id) or user_id in config.ADMINS):
                clear_user_state(user_id)
                return
                
            name = event.text.strip()
            data['name'] = name
            
            # Save to Database settings
            fjs = await database.get_setting("force_join_channels", [])
            fjs.append({
                "channel_id": data['channel_id'],
                "invite_link": data['invite_link'],
                "name": data['name']
            })
            await database.set_setting("force_join_channels", fjs)
            
            clear_user_state(user_id)
            await event.respond(
                "✅ **Sponsor Channel Added Successfully!**",
                buttons=[[Button.inline("⬅️ Back to Settings", "admin_force_join")]]
            )
