import aiosqlite
import json
import logging
from datetime import datetime, timedelta
from config import DB_PATH

logger = logging.getLogger(__name__)

async def init_db():
    """Initializes the SQLite database tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Users Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Accounts (Userbot Sessions) Table - Max 5 per user
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                session_string TEXT,
                first_name TEXT,
                last_name TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        
        # Forwarding Tasks Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                source_chat_id INTEGER,
                source_msg_id INTEGER,
                source_text TEXT,
                target_types TEXT, -- comma-separated e.g. "dm,channel,group,topic"
                interval_minutes INTEGER,
                next_run_at TIMESTAMP,
                last_run_at TIMESTAMP,
                status TEXT DEFAULT 'active', -- 'active', 'paused'
                accounts_to_use TEXT, -- comma-separated list of account_ids or 'all'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        
        # System Settings Table (e.g. force-join channels)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
        logger.info("Database initialized successfully.")

# --- User Functions ---

async def add_user(user_id: int, username: str, is_admin: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, is_admin) VALUES (?, ?, ?)",
            (user_id, username, 1 if is_admin else 0)
        )
        # Update username if it changed
        await db.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username, user_id)
        )
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cursor:
            return await cursor.fetchall()

async def is_admin(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user['is_admin'])

async def set_admin(user_id: int, status: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (1 if status else 0, user_id))
        await db.commit()

async def is_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user['is_banned'])

async def set_banned(user_id: int, status: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if status else 0, user_id))
        await db.commit()

# --- Userbot Accounts Functions ---

async def add_account(user_id: int, phone: str, session_string: str, first_name: str, last_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if account with same phone exists for user
        async with db.execute(
            "SELECT account_id FROM accounts WHERE user_id = ? AND phone = ?", 
            (user_id, phone)
        ) as cursor:
            existing = await cursor.fetchone()
            
        if existing:
            await db.execute(
                "UPDATE accounts SET session_string = ?, first_name = ?, last_name = ?, is_active = 1 WHERE account_id = ?",
                (session_string, first_name, last_name, existing[0])
            )
        else:
            await db.execute(
                "INSERT INTO accounts (user_id, phone, session_string, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
                (user_id, phone, session_string, first_name, last_name)
            )
        await db.commit()

async def get_user_accounts(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchall()

async def count_user_accounts(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def get_account(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)) as cursor:
            return await cursor.fetchone()

async def delete_account(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
        await db.commit()

async def set_account_active(account_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE accounts SET is_active = ? WHERE account_id = ?", (1 if active else 0, account_id))
        await db.commit()

# --- Forwarding Tasks Functions ---

async def add_task(user_id: int, source_chat_id: int, source_msg_id: int, source_text: str, 
                   target_types: list, interval_minutes: int, accounts_to_use: list):
    target_types_str = ",".join(target_types)
    accounts_str = ",".join(map(str, accounts_to_use))
    
    now = datetime.now()
    next_run = now + timedelta(minutes=interval_minutes)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tasks 
               (user_id, source_chat_id, source_msg_id, source_text, target_types, interval_minutes, next_run_at, accounts_to_use) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, source_chat_id, source_msg_id, source_text, target_types_str, interval_minutes, 
             next_run.strftime('%Y-%m-%d %H:%M:%S'), accounts_str)
        )
        await db.commit()

async def get_user_tasks(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC", (user_id,)) as cursor:
            return await cursor.fetchall()

async def get_task(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            return await cursor.fetchone()

async def delete_task(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await db.commit()

async def update_task_status(task_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
        await db.commit()

async def get_due_tasks():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE status = 'active' AND next_run_at <= ?", 
            (now_str,)
        ) as cursor:
            return await cursor.fetchall()

async def update_task_next_run(task_id: int, last_run_at: datetime, next_run_at: datetime):
    last_run_str = last_run_at.strftime('%Y-%m-%d %H:%M:%S')
    next_run_str = next_run_at.strftime('%Y-%m-%d %H:%M:%S')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET last_run_at = ?, next_run_at = ? WHERE task_id = ?",
            (last_run_str, next_run_str, task_id)
        )
        await db.commit()

async def get_total_active_tasks_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

# --- Settings Functions ---

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return row[0]
            return default

async def set_setting(key: str, value):
    val_str = json.dumps(value)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, val_str)
        )
        await db.commit()
