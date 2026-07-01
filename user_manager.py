import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError, 
    PhoneCodeInvalidError, 
    PhoneCodeExpiredError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError
)
import database
import config

logger = logging.getLogger(__name__)

# Temporary login storage: user_id -> dict with client, phone, phone_code_hash, etc.
_login_sessions = {}

def get_login_session(user_id: int):
    return _login_sessions.get(user_id)

async def clear_login_session(user_id: int):
    if user_id in _login_sessions:
        # Disconnect client if connected
        client = _login_sessions[user_id].get('client')
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        del _login_sessions[user_id]

def describe_code_delivery(sent_code) -> str:
    """Build a short user-facing hint about where Telegram sent the login code."""
    code_type = getattr(sent_code, 'type', None)
    type_name = type(code_type).__name__

    if 'SentCodeTypeApp' in type_name:
        return "Telegram sent the code inside your Telegram app/session, not as an SMS."
    if 'SentCodeTypeSms' in type_name:
        return "Telegram sent the code by SMS."
    if 'SentCodeTypeCall' in type_name:
        return "Telegram will provide the code by phone call."
    if 'SentCodeTypeFlashCall' in type_name:
        return "Telegram will provide the code by flash call."
    if 'SentCodeTypeFragmentSms' in type_name:
        return "Telegram sent the code through Fragment SMS."

    return "Check your Telegram app first; Telegram often sends login codes there instead of SMS."

async def start_login(user_id: int, phone: str):
    """
    Starts the login process by sending a code request.
    Returns "code_sent" on success, or raises exception.
    """
    # Clean up any existing attempt
    await clear_login_session(user_id)
    
    # Create Telethon client with StringSession
    session = StringSession()
    client = TelegramClient(session, config.API_ID, config.API_HASH)
    
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(phone)
        _login_sessions[user_id] = {
            'client': client,
            'phone': phone,
            'phone_code_hash': sent_code.phone_code_hash,
            'step': 'wait_code',
            'delivery_hint': describe_code_delivery(sent_code)
        }
        logger.info(
            f"OTP code request sent to {phone} for user {user_id}; "
            f"type={type(getattr(sent_code, 'type', None)).__name__}"
        )
        return {"status": "code_sent", "delivery_hint": describe_code_delivery(sent_code)}
    except PhoneNumberInvalidError:
        await client.disconnect()
        logger.error(f"Invalid phone number for login: {phone}")
        raise ValueError("Telegram says this phone number is invalid. Use international format, for example +1234567890.")
    except PhoneNumberFloodError:
        await client.disconnect()
        logger.error(f"Phone number flood while sending code to {phone}")
        raise RuntimeError("Telegram is temporarily blocking new code requests for this phone number. Please wait before trying again.")
    except FloodWaitError as e:
        await client.disconnect()
        logger.error(f"Flood wait while sending code to {phone}: {e.seconds}s")
        raise RuntimeError(f"Telegram rate-limited this login request. Try again after {e.seconds} seconds.")
    except Exception as e:
        await client.disconnect()
        logger.error(f"Failed to send code to {phone}: {e}")
        raise e

async def submit_code(user_id: int, code: str):
    """
    Submits OTP code.
    Returns:
      - "success" if logged in
      - "need_password" if 2FA password is required
    Raises exceptions for invalid code or other failures.
    """
    session = get_login_session(user_id)
    if not session:
        raise ValueError("No active login session. Please start over.")
        
    client = session['client']
    phone = session['phone']
    phone_code_hash = session['phone_code_hash']
    
    try:
        # Code might contain spaces or formatting depending on user input
        clean_code = code.strip().replace(" ", "")
        await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
        
        # Successful login without 2FA
        me = await client.get_me()
        session_str = client.session.save()
        
        await database.add_account(
            user_id=user_id,
            phone=phone,
            session_string=session_str,
            first_name=me.first_name or "",
            last_name=me.last_name or ""
        )
        
        await client.disconnect()
        del _login_sessions[user_id]
        logger.info(f"User {user_id} successfully logged in account {phone}")
        return "success"
        
    except SessionPasswordNeededError:
        session['step'] = 'wait_password'
        logger.info(f"2FA password required for account {phone} (user {user_id})")
        return "need_password"
    except PhoneCodeInvalidError:
        raise PhoneCodeInvalidError("The code you entered is invalid. Please check and try again.")
    except PhoneCodeExpiredError:
        await client.disconnect()
        del _login_sessions[user_id]
        raise PhoneCodeExpiredError("The code has expired. Please restart the registration process.")
    except Exception as e:
        await client.disconnect()
        del _login_sessions[user_id]
        logger.error(f"Error during sign-in for user {user_id}: {e}")
        raise e

async def submit_password(user_id: int, password: str):
    """
    Submits 2FA password.
    Returns "success" if login completes.
    """
    session = get_login_session(user_id)
    if not session:
        raise ValueError("No active login session. Please start over.")
        
    client = session['client']
    phone = session['phone']
    
    try:
        await client.sign_in(password=password)
        
        me = await client.get_me()
        session_str = client.session.save()
        
        await database.add_account(
            user_id=user_id,
            phone=phone,
            session_string=session_str,
            first_name=me.first_name or "",
            last_name=me.last_name or ""
        )
        
        await client.disconnect()
        del _login_sessions[user_id]
        logger.info(f"User {user_id} logged in account {phone} with 2FA password")
        return "success"
    except PasswordHashInvalidError:
        raise PasswordHashInvalidError("Incorrect 2FA password. Please try again.")
    except Exception as e:
        await client.disconnect()
        del _login_sessions[user_id]
        logger.error(f"Error during 2FA sign-in for user {user_id}: {e}")
        raise e
