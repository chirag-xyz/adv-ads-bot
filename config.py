import os

# Telegram API Configuration
# Get these from https://my.telegram.org/apps
API_ID = int(os.getenv("TELEGRAM_API_ID", 31830373))
API_HASH = os.getenv("TELEGRAM_API_HASH", "3b6c59b722a1d48197328d95905552d5")

# Telegram Bot Token
# Get this from https://t.me/BotFather
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7948478362:AAEQTTqHB7bROfPGlfGHLNOqEb4qm5gRdcM")

# Bot Admins
# List of Telegram user IDs of the bot administrators
ADMINS = [
    255448871
    # Replace with admin user IDs (integers), e.g., 123456789
]

# Database Path
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adbot.db")

# Force Join Settings
# These can be updated via Admin Panel as well, but initialized here
DEFAULT_FORCE_JOINS = [
    {"channel_id": -1002881282115,"invite_link": "https://t.me/xyzfound"}
    # Example structure:
    # {"channel_id": -100123456789, "invite_link": "https://t.me/your_channel"},
]

# Client connection options
# Delay between messages during forwarding (seconds) to prevent flood limits
FORWARD_DELAY = 4.0
