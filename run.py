import logging
import asyncio
import sys
from telethon import TelegramClient
import config
import database
import bot
import forwarder

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('adbot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

async def main():
    # 1. Verify Configuration
    if not config.API_ID or not config.API_HASH:
        logger.error("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured in config.py or set as environment variables.")
        return
        
    if not config.BOT_TOKEN:
        logger.error("Error: TELEGRAM_BOT_TOKEN must be configured in config.py or set as environment variable.")
        return

    logger.info("Starting Telegram Ad Bot backend...")

    # 2. Initialize Database
    try:
        await database.init_db()
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        return

    # 3. Create Bot Client
    bot_client = TelegramClient('adbot_bot', config.API_ID, config.API_HASH)
    
    # 4. Register Event Handlers
    bot.register_bot_handlers(bot_client)

    # 5. Start Bot Client
    try:
        await bot_client.start(bot_token=config.BOT_TOKEN)
        logger.info("Bot client logged in successfully.")
    except Exception as e:
        logger.critical(f"Failed to start bot client: {e}")
        return

    # 6. Start Background Forwarder
    forwarder_task = asyncio.create_task(forwarder.forwarder_worker_loop(bot_client))

    # 7. Run Bot until disconnected
    logger.info("System is fully active. Press Ctrl+C to terminate.")
    try:
        await bot_client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info("Received termination signal.")
    finally:
        logger.info("Shutting down services...")
        # Stop forwarder loop
        forwarder.stop_forwarder()
        # Cancel forwarder task
        forwarder_task.cancel()
        try:
            await forwarder_task
        except asyncio.CancelledError:
            pass
        # Disconnect bot client
        await bot_client.disconnect()
        logger.info("Services stopped. Goodbye!")

if __name__ == '__main__':
    # Use standard loop run
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
