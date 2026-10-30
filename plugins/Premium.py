import re
import pytz
import datetime
import asyncio
import logging
import aiohttp
from Script import script
from info import *
from utils import get_seconds, temp
from database.users_chats_db import db
from pyrogram import Client, filters, enums
from pyrogram.errors.exceptions.bad_request_400 import MessageTooLong
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButtonBuy,
    LabeledPrice,
    CallbackQuery,
    PreCheckoutQuery,
    InputMediaPhoto,
)

logger = logging.getLogger(__name__)

# In-memory idempotency set for processed Razorpay payment links / payment IDs
_PROCESSED_PAYMENTS = set()


def format_plan_label(duration_str: str) -> str:
    """Convert raw plan duration string like '7day' or '1month' into readable label."""
    s = str(duration_str).strip()
    m = re.match(r"^(\d+)\s*([a-zA-Z]+)$", s)
    if not m:
        return s.title()
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("d"):
        unit_label = "Day" if val == 1 else "Days"
    elif unit.startswith("m") and not unit.startswith("mi"):
        unit_label = "Month" if val == 1 else "Months"
    elif unit.startswith("y"):
        unit_label = "Year" if val == 1 else "Years"
    elif unit.startswith("h"):
        unit_label = "Hour" if val == 1 else "Hours"
    else:
        unit_label = unit.title()
    return f"{val} {unit_label}"


def get_ist_expiry_and_remaining(expiry: datetime.datetime):
    """Return (expiry_str_in_ist, time_left_str, total_seconds_left) safely for naive or aware datetimes."""
    ist_tz = pytz.timezone("Asia/Kolkata")
    now_naive = datetime.datetime.now()
    if expiry.tzinfo is not None:
        expiry_ist = expiry.astimezone(ist_tz)
        current_ist = datetime.datetime.now(ist_tz)
        time_left = expiry_ist - current_ist
    else:
        time_left = expiry - now_naive
        expiry_ist = ist_tz.localize(expiry) if hasattr(ist_tz, "localize") else expiry.replace(tzinfo=ist_tz)

    total_seconds = time_left.total_seconds()
    if total_seconds <= 0:
        return expiry_ist.strftime("%d-%m-%Y\n⏱️ ᴇxᴘɪʀʏ ᴛɪᴍᴇ : %I:%M:%S %p"), "Expired", 0

    days = int(total_seconds // 86400)
    remainder = int(total_seconds % 86400)
    hours, rem2 = divmod(remainder, 3600)
    minutes, _ = divmod(rem2, 60)
    time_left_str = f"{days} ᴅᴀʏꜱ, {hours} ʜᴏᴜʀꜱ, {minutes} ᴍɪɴᴜᴛᴇꜱ"
    expiry_str_in_ist = expiry_ist.strftime("%d-%m-%Y\n⏱️ ᴇxᴘɪʀʏ ᴛɪᴍᴇ : %I:%M:%S %p")
    return expiry_str_in_ist, time_left_str, total_seconds


def build_razorpay_plan_buttons(include_extras: bool = True, back_callback: str = None) -> InlineKeyboardMarkup:
    """Build one-tap automatic Razorpay purchase buttons for all plans."""
    buttons = []
    for amount_inr in sorted(RAZORPAY_PLANS.keys()):
        raw_dur = RAZORPAY_PLANS[amount_inr]
        label = format_plan_label(raw_dur)
        buttons.append([
            InlineKeyboardButton(
                f"💳 {label} Pʀᴇᴍɪᴜᴍ — ₹{amount_inr}",
                callback_data=f"buy_{amount_inr}",
            )
        ])

    if include_extras:
        buttons.append([
            InlineKeyboardButton("👤 ᴍʏ ᴘʟᴀɴ", callback_data="myplan_cb"),
            InlineKeyboardButton("🎁 ꜰʀᴇᴇ ᴛʀɪᴀʟ", callback_data="give_trial"),
        ])
        buttons.append([
            InlineKeyboardButton("🔗 ʀᴇꜰᴇʀ ꜰʀɪᴇɴᴅꜱ", callback_data="reffff"),
            InlineKeyboardButton("💳 ᴜᴘɪ / Qʀ", callback_data="upi_info"),
        ])

    if back_callback:
        buttons.append([InlineKeyboardButton("⇋ ʙᴀᴄᴋ ⇋", callback_data=back_callback)])
    else:
        buttons.append([InlineKeyboardButton("🚫 ᴄʟᴏꜱᴇ 🚫", callback_data="close_data")])

    return InlineKeyboardMarkup(buttons)


# Alias for compatibility with other modules
build_star_plan_buttons = build_razorpay_plan_buttons


async def send_paywall_alert(client, chat_id: int, user_mention: str):
    """Send paywall requirement prompt with automatic Razorpay buttons."""
    caption = script.PAYWALL_ALERT_TEXT.format(user_mention)
    markup = build_razorpay_plan_buttons(include_extras=True)
    try:
        await client.send_photo(
            chat_id=chat_id,
            photo=SUBSCRIPTION,
            caption=caption,
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await client.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )


async def create_razorpay_payment_link(user_id: int, amount_inr: int, readable_duration: str, raw_duration: str):
    """
    Create a Razorpay Payment Link via Razorpay REST API (POST /v1/payment_links).
    Returns dict with 'id', 'short_url', 'status' or None on failure.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return None

    ts = int(datetime.datetime.now().timestamp())
    ref_id = f"rzp_{user_id}_{amount_inr}_{ts}"
    payload = {
        "amount": int(amount_inr) * 100,  # Amount in paise
        "currency": "INR",
        "accept_partial": False,
        "description": f"VIP Premium Access ({readable_duration})",
        "reference_id": ref_id,
        "notes": {
            "user_id": str(user_id),
            "amount": str(amount_inr),
            "duration": str(raw_duration),
        },
        "reminder_enable": False,
    }

    auth = aiohttp.BasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    try:
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(
                "https://api.razorpay.com/v1/payment_links",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                err_txt = await resp.text()
                logger.error(f"Razorpay create link failed ({resp.status}): {err_txt}")
                return None
    except Exception as e:
        logger.error(f"Error creating Razorpay payment link: {e}")
        return None


async def check_razorpay_payment_link(plink_id: str):
    """
    Fetch status of a Razorpay Payment Link via GET /v1/payment_links/{plink_id}.
    Returns the JSON dict or None on failure.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET or not plink_id:
        return None

    auth = aiohttp.BasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    try:
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(
                f"https://api.razorpay.com/v1/payment_links/{plink_id}",
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception as e:
        logger.error(f"Error checking Razorpay payment link {plink_id}: {e}")
        return None


async def fulfill_razorpay_payment(
    client,
    plink_id: str,
    user_id: int,
    amount_inr: int,
    payment_id: str = "",
    chat_id: int = None,
    message_id: int = None,
) -> bool:
    """
    Idempotently activate or extend premium subscription for a verified Razorpay payment.
    Returns True if newly fulfilled, False if already processed or invalid.
    """
    user_id = int(user_id)
    amount_inr = int(amount_inr)
    unique_key = plink_id or payment_id
    if not unique_key:
        return False

    if unique_key in _PROCESSED_PAYMENTS:
        return False

    # Check persistent DB record for idempotency (allow retries if status is pending/failed)
    existing = await db.codes.find_one({"_id": f"rzp_{unique_key}"})
    if existing and existing.get("status") == "completed":
        _PROCESSED_PAYMENTS.add(unique_key)
        return False

    # Mark as durable pending before attempting subscription update
    try:
        await db.codes.update_one(
            {"_id": f"rzp_{unique_key}"},
            {"$set": {
                "user_id": user_id,
                "amount_inr": amount_inr,
                "payment_id": payment_id,
                "status": "pending",
                "updated_at": datetime.datetime.now(),
            }},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error recording pending payment {unique_key}: {e}")

    if amount_inr in RAZORPAY_PLANS:
        raw_time = RAZORPAY_PLANS[amount_inr]
        plan_label = format_plan_label(raw_time)
        seconds = await get_seconds(raw_time)
    else:
        days = max(1, int(amount_inr // 5))
        plan_label = f"{days} Days"
        seconds = days * 86400

    if seconds <= 0:
        return False

    try:
        data = await db.add_or_extend_premium(
            user_id=user_id,
            seconds=seconds,
            amount_paid=amount_inr,
            plan_name=plan_label,
        )
    except Exception as e:
        logger.error(f"Database error during premium activation for {user_id}: {e}")
        # Keep status as pending/failed to permit retries
        try:
            await db.codes.update_one(
                {"_id": f"rzp_{unique_key}"},
                {"$set": {"status": "failed", "error": str(e), "failed_at": datetime.datetime.now()}}
            )
        except Exception:
            pass
        return False

    if not data:
        return False

    # Mark as completed only after successful activation
    try:
        await db.codes.update_one(
            {"_id": f"rzp_{unique_key}"},
            {"$set": {
                "status": "completed",
                "completed_at": datetime.datetime.now(),
            }}
        )
    except Exception:
        pass
    _PROCESSED_PAYMENTS.add(unique_key)
    expiry = data.get("expiry_time")
    expiry_str_in_ist, time_left_str, _ = get_ist_expiry_and_remaining(expiry)

    try:
        user_obj = await client.get_users(user_id)
        user_mention = user_obj.mention
    except Exception:
        user_mention = f"<code>{user_id}</code>"

    time_zone = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
    current_time = time_zone.strftime("%d-%m-%Y | %I:%M:%S %p")
    txn_display = payment_id or plink_id

    receipt_text = (
        f"🎉 <b>ʀᴀᴢᴏʀᴘᴀʏ ᴘᴀʏᴍᴇɴᴛ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ! ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ ✅</b>\n\n"
        f"👋 ᴛʜᴀɴᴋ ʏᴏᴜ {user_mention}, ʏᴏᴜʀ ʀᴀᴢᴏʀᴘᴀʏ ᴘᴀʏᴍᴇɴᴛ ʜᴀꜱ ʙᴇᴇɴ ᴠᴇʀɪꜰɪᴇᴅ!\n\n"
        f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_label}</code>\n"
        f"💳 <b>ᴀᴍᴏᴜɴᴛ ᴘᴀɪᴅ :</b> <code>₹{amount_inr}</code>\n"
        f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> <code>{time_left_str}</code>\n"
        f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}\n"
        f"🔖 <b>ᴘᴀʏᴍᴇɴᴛ ɪᴅ :</b> <code>{txn_display}</code>\n\n"
        f"🚀 <i>ᴇɴᴊᴏʏ ʏᴏᴜʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴅ-ꜰʀᴇᴇ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ!</i>"
    )
    receipt_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 ᴠɪᴇᴡ ᴍʏ ᴘʟᴀɴ", callback_data="myplan_cb")]
    ])

    target_chat = chat_id or user_id
    sent_receipt = False
    if chat_id and message_id:
        try:
            await client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=receipt_text,
                reply_markup=receipt_markup,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_receipt = True
        except Exception:
            pass

    if not sent_receipt:
        try:
            await client.send_message(
                chat_id=target_chat,
                text=receipt_text,
                reply_markup=receipt_markup,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Failed to send Razorpay receipt to {target_chat}: {e}")

    try:
        await client.send_message(
            PREMIUM_LOGS,
            text=(
                f"#Purchase_Premium_With_Razorpay\n\n"
                f"👤 ᴜꜱᴇʀ - {user_mention}\n"
                f"⚡ ᴜꜱᴇʀ ɪᴅ - <code>{user_id}</code>\n"
                f"💳 ᴀᴍᴏᴜɴᴛ ᴘᴀɪᴅ - <code>₹{amount_inr}</code>\n"
                f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ - <code>{plan_label}</code>\n"
                f"⏳ ᴊᴏɪɴɪɴɢ ᴅᴀᴛᴇ - {current_time}\n"
                f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ - {expiry_str_in_ist}\n"
                f"🔖 ᴘᴀʏᴍᴇɴᴛ ɪᴅ - <code>{txn_display}</code>"
            ),
            disable_web_page_preview=True,
        )
    except Exception as log_err:
        logger.warning(f"Failed to send Razorpay log to PREMIUM_LOGS: {log_err}")

    return True


async def _auto_poll_razorpay_link(client, plink_id: str, user_id: int, amount_inr: int, chat_id: int, message_id: int):
    """
    Background task that polls Razorpay payment link status every 5 seconds for up to 10 minutes
    and automatically activates the user's subscription as soon as status == 'paid'.
    """
    for _ in range(120):
        await asyncio.sleep(5)
        if plink_id in _PROCESSED_PAYMENTS:
            return
        info = await check_razorpay_payment_link(plink_id)
        if not info:
            continue
        status = str(info.get("status", "")).lower()
        if status == "paid":
            payments = info.get("payments") or []
            payment_id = ""
            if payments and isinstance(payments, list) and isinstance(payments[0], dict):
                payment_id = payments[0].get("payment_id", "") or ""
            await fulfill_razorpay_payment(
                client=client,
                plink_id=plink_id,
                user_id=user_id,
                amount_inr=amount_inr,
                payment_id=payment_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            return
        if status in ("cancelled", "expired"):
            return


async def send_razorpay_payment(client, chat_id: int, user_id: int, amount: int) -> bool:
    """
    Generate and send a Razorpay payment checkout to the specified chat.
    1. If RAZORPAY_KEY_ID & RAZORPAY_KEY_SECRET are set: Creates a Razorpay Payment Link
       with automatic background verification + 1-tap verify button.
    2. Else if RAZORPAY_PROVIDER_TOKEN is set: Sends a native Telegram invoice in INR via Razorpay provider.
    """
    if amount not in RAZORPAY_PLANS:
        return False

    raw_duration = RAZORPAY_PLANS[amount]
    readable_duration = format_plan_label(raw_duration)

    # Option 1: Razorpay Payment Links API (RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET)
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        link_data = await create_razorpay_payment_link(
            user_id=user_id,
            amount_inr=amount,
            readable_duration=readable_duration,
            raw_duration=raw_duration,
        )
        if not link_data or not link_data.get("short_url"):
            return False

        plink_id = link_data["id"]
        short_url = link_data["short_url"]

        buttons = [
            [InlineKeyboardButton(f"💳 ᴘᴀʏ ₹{amount} ᴠɪᴀ ʀᴀᴢᴏʀᴘᴀʏ", url=short_url)],
            [InlineKeyboardButton("🔄 ᴠᴇʀɪꜰʏ ᴘᴀʏᴍᴇɴᴛ ꜱᴛᴀᴛᴜꜱ", callback_data=f"rzp_verify#{plink_id}#{amount}#{user_id}")],
            [InlineKeyboardButton("🚫 ᴄᴀɴᴄᴇʟ", callback_data="close_data")],
        ]
        msg = await client.send_message(
            chat_id=chat_id,
            text=(
                f"💳 <b><u>ʀᴀᴢᴏʀᴘᴀʏ ɪɴꜱᴛᴀɴᴛ ᴄʜᴇᴄᴋᴏᴜᴛ</u></b>\n\n"
                f"📦 <b>ᴘʟᴀɴ :</b> <code>{readable_duration} Pʀᴇᴍɪᴜᴍ</code>\n"
                f"💰 <b>ᴀᴍᴏᴜɴᴛ :</b> <code>₹{amount}</code>\n"
                f"🆔 <b>ᴏʀᴅᴇʀ ɪᴅ :</b> <code>{plink_id}</code>\n\n"
                f"1️⃣ ᴛᴀᴘ <b>💳 ᴘᴀʏ ₹{amount} ᴠɪᴀ ʀᴀᴢᴏʀᴘᴀʏ</b> ʙᴇʟᴏᴡ ᴛᴏ ᴘᴀʏ ᴜꜱɪɴɢ ᴜᴘɪ, ɢᴘᴀʏ, ᴘʜᴏɴᴇᴘᴇ, ᴘᴀʏᴛᴍ, ᴏʀ ᴄᴀʀᴅ.\n"
                f"2️⃣ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴡɪʟʟ ᴀᴄᴛɪᴠᴀᴛᴇ <b>ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ</b> within seconds of payment (or tap <b>🔄 ᴠᴇʀɪꜰʏ ᴘᴀʏᴍᴇɴᴛ ꜱᴛᴀᴛᴜꜱ</b>)."
            ),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        asyncio.create_task(
            _auto_poll_razorpay_link(client, plink_id, user_id, amount, chat_id, msg.id)
        )
        return True

    # Option 2: Native Telegram Invoice via BotFather RAZORPAY_PROVIDER_TOKEN
    if RAZORPAY_PROVIDER_TOKEN:
        payload = f"dreamxrazorpay_{amount}_{user_id}_{int(datetime.datetime.now().timestamp())}"
        buttons = [
            [InlineKeyboardButtonBuy(text=f"💳 Pay ₹{amount} via Razorpay")],
            [InlineKeyboardButton("ᴄᴀɴᴄᴇʟ 🚫", callback_data="close_data")],
        ]
        try:
            await client.send_invoice(
                chat_id=chat_id,
                title=f"💳 VIP Premium ({readable_duration})",
                description=(
                    f"Instant automatic activation of {readable_duration} Premium Access "
                    f"via Razorpay (Direct files, No verification, Ad-free)."
                ),
                payload=payload,
                provider_token=RAZORPAY_PROVIDER_TOKEN,
                currency="INR",
                prices=[
                    LabeledPrice(
                        label=f"Premium ({readable_duration})",
                        amount=int(amount) * 100,
                    )
                ],
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return True
        except Exception as e:
            logger.error(f"Error sending Razorpay provider invoice to {chat_id}: {e}")
            return False

    return False


# Alias for compatibility with commands.py
send_star_invoice = send_razorpay_payment


# =============================================================================
# Admin Commands: /remove_premium, /get_premium, /add_premium, /premium_users, /paywall
# =============================================================================

@Client.on_message(filters.command("remove_premium") & filters.user(ADMINS))
async def remove_premium(client, message):
    if len(message.command) == 2:
        try:
            user_id = int(message.command[1])
        except ValueError:
            return await message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ !")
        try:
            user = await client.get_users(user_id)
            mention = user.mention
        except Exception:
            mention = f"<code>{user_id}</code>"

        if await db.remove_premium_access(user_id):
            await message.reply_text("ᴜꜱᴇʀ ʀᴇᴍᴏᴠᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ !")
            try:
                await client.send_message(
                    chat_id=user_id,
                    text=script.PREMIUM_END_TEXT.format(mention),
                )
            except Exception:
                pass
        else:
            await message.reply_text("ᴜɴᴀʙʟᴇ ᴛᴏ ʀᴇᴍᴏᴠᴇ ᴜꜱᴇʀ !\nᴀʀᴇ ʏᴏᴜ ꜱᴜʀᴇ, ɪᴛ ᴡᴀꜱ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ɪᴅ ?")
    else:
        await message.reply_text("ᴜꜱᴀɢᴇ : /remove_premium user_id")


@Client.on_message(filters.command(["myplan", "subscription"]))
async def myplan(client, message):
    try:
        user = message.from_user.mention
        user_id = message.from_user.id
        data = await db.get_user(user_id)

        if data and data.get("expiry_time"):
            expiry = data.get("expiry_time")
            expiry_str_in_ist, time_left_str, total_secs = get_ist_expiry_and_remaining(expiry)
            if total_secs > 0:
                plan_name = data.get("plan_name", "VIP Premium")
                amount_paid = data.get("amount_paid", 0)
                caption = (
                    f"⚜️ <b>ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :</b>\n\n"
                    f"👤 <b>ᴜꜱᴇʀ :</b> {user}\n"
                    f"⚡ <b>ᴜꜱᴇʀ ɪᴅ :</b> <code>{user_id}</code>\n"
                    f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_name}</code>\n"
                    f"💳 <b>ᴀᴍᴏᴜɴᴛ ᴘᴀɪᴅ :</b> <code>₹{amount_paid}</code>\n"
                    f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> {time_left_str}\n"
                    f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}"
                )

                await message.reply_photo(
                    photo=SUBSCRIPTION,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔥 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ (ʀᴀᴢᴏʀᴘᴀʏ)", callback_data="star_info")]]
                    ),
                )
                return

        await message.reply_photo(
            photo="https://i.ibb.co/gMrpRQWP/photo-2025-07-09-05-21-32-7524948058832896004.jpg",
            caption=(
                f"<b>ʜᴇʏ {user},\n\n"
                f"ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ.\n"
                f"ꜱᴇʟᴇᴄᴛ ᴀ ᴘʟᴀɴ ʙᴇʟᴏᴡ ꜰᴏʀ ɪɴꜱᴛᴀɴᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ ᴠɪᴀ ʀᴀᴢᴏʀᴘᴀʏ 💳!</b>"
            ),
            reply_markup=build_razorpay_plan_buttons(include_extras=False),
        )
    except Exception as e:
        logger.error(f"Error in myplan: {e}")


@Client.on_callback_query(filters.regex(r"^myplan_cb$"), group=-1)
async def myplan_callback(client, query: CallbackQuery):
    try:
        user = query.from_user.mention
        user_id = query.from_user.id
        data = await db.get_user(user_id)

        if data and data.get("expiry_time"):
            expiry = data.get("expiry_time")
            expiry_str_in_ist, time_left_str, total_secs = get_ist_expiry_and_remaining(expiry)
            if total_secs > 0:
                plan_name = data.get("plan_name", "VIP Premium")
                amount_paid = data.get("amount_paid", 0)
                caption = (
                    f"⚜️ <b>ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :</b>\n\n"
                    f"👤 <b>ᴜꜱᴇʀ :</b> {user}\n"
                    f"⚡ <b>ᴜꜱᴇʀ ɪᴅ :</b> <code>{user_id}</code>\n"
                    f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_name}</code>\n"
                    f"💳 <b>ᴀᴍᴏᴜɴᴛ ᴘᴀɪᴅ :</b> <code>₹{amount_paid}</code>\n"
                    f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> {time_left_str}\n"
                    f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}"
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔥 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ (ʀᴀᴢᴏʀᴘᴀʏ)", callback_data="star_info")],
                    [InlineKeyboardButton("⇋ ʙᴀᴄᴋ ⇋", callback_data="premium_info")],
                ])
                try:
                    await client.edit_message_media(
                        chat_id=query.message.chat.id,
                        message_id=query.message.id,
                        media=InputMediaPhoto(media=SUBSCRIPTION, caption=caption, parse_mode=enums.ParseMode.HTML),
                        reply_markup=markup,
                    )
                except Exception:
                    await query.message.edit_text(caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
                await query.answer()
                return

        caption = (
            f"<b>ʜᴇʏ {user},\n\n"
            f"ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ.\n"
            f"ꜱᴇʟᴇᴄᴛ ᴀ ᴘʟᴀɴ ʙᴇʟᴏᴡ ꜰᴏʀ ɪɴꜱᴛᴀɴᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ ᴠɪᴀ ʀᴀᴢᴏʀᴘᴀʏ 💳!</b>"
        )
        markup = build_razorpay_plan_buttons(include_extras=False, back_callback="premium_info")
        try:
            await client.edit_message_media(
                chat_id=query.message.chat.id,
                message_id=query.message.id,
                media=InputMediaPhoto(media=SUBSCRIPTION, caption=caption, parse_mode=enums.ParseMode.HTML),
                reply_markup=markup,
            )
        except Exception:
            await query.message.edit_text(caption, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        await query.answer()
    except Exception as e:
        logger.error(f"Error in myplan_callback: {e}")


@Client.on_message(filters.command("get_premium") & filters.user(ADMINS))
async def get_premium(client, message):
    if len(message.command) == 2:
        try:
            user_id = int(message.command[1])
        except ValueError:
            return await message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ !")
        try:
            user = await client.get_users(user_id)
            mention = user.mention
        except Exception:
            mention = f"<code>{user_id}</code>"

        data = await db.get_user(user_id)
        if data and data.get("expiry_time"):
            expiry = data.get("expiry_time")
            expiry_str_in_ist, time_left_str, _ = get_ist_expiry_and_remaining(expiry)
            plan_name = data.get("plan_name", "VIP")
            amount_paid = data.get("amount_paid", 0)
            await message.reply_text(
                f"⚜️ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :\n\n"
                f"👤 ᴜꜱᴇʀ : {mention}\n"
                f"⚡ ᴜꜱᴇʀ ɪᴅ : <code>{user_id}</code>\n"
                f"📦 ᴘʟᴀɴ : <code>{plan_name}</code>\n"
                f"💳 ᴀᴍᴏᴜɴᴛ ᴘᴀɪᴅ : <code>₹{amount_paid}</code>\n"
                f"⏰ ᴛɪᴍᴇ ʟᴇꜰᴛ : {time_left_str}\n"
                f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {expiry_str_in_ist}"
            )
        else:
            await message.reply_text("ɴᴏ ᴀɴʏ ᴘʀᴇᴍɪᴜᴍ ᴅᴀᴛᴀ ᴏꜰ ᴛʜᴇ ᴜꜱᴇʀ ᴡᴀꜱ ꜰᴏᴜɴᴅ ɪɴ ᴅᴀᴛᴀʙᴀꜱᴇ !")
    else:
        await message.reply_text("ᴜꜱᴀɢᴇ : /get_premium user_id")


@Client.on_message(filters.command("add_premium") & filters.user(ADMINS))
async def give_premium_cmd_handler(client, message):
    if len(message.command) >= 3:
        time_zone = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
        current_time = time_zone.strftime("%d-%m-%Y\n⏱️ ᴊᴏɪɴɪɴɢ ᴛɪᴍᴇ : %I:%M:%S %p")
        try:
            user_id = int(message.command[1])
        except ValueError:
            return await message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ !")

        try:
            user = await client.get_users(user_id)
            mention = user.mention
        except Exception:
            mention = f"<code>{user_id}</code>"

        time_str = " ".join(message.command[2:])
        seconds = await get_seconds(time_str)
        if seconds > 0:
            data = await db.add_or_extend_premium(
                user_id=user_id,
                seconds=seconds,
                amount_paid=0,
                plan_name=f"Admin Grant ({time_str})",
            )
            expiry = data.get("expiry_time")
            expiry_str_in_ist, _, _ = get_ist_expiry_and_remaining(expiry)
            await message.reply_text(
                f"ᴘʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ✅\n\n"
                f"👤 ᴜꜱᴇʀ : {mention}\n"
                f"⚡ ᴜꜱᴇʀ ɪᴅ : <code>{user_id}</code>\n"
                f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ : <code>{time_str}</code>\n\n"
                f"⏳ ᴊᴏɪɴɪɴɢ ᴅᴀᴛᴇ : {current_time}\n\n"
                f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {expiry_str_in_ist}",
                disable_web_page_preview=True,
            )
            try:
                await client.send_message(
                    chat_id=user_id,
                    text=(
                        f"👋 ʜᴇʏ {mention},\n"
                        f"ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ᴘᴜʀᴄʜᴀꜱɪɴɢ ᴘʀᴇᴍɪᴜᴍ.\n"
                        f"ᴇɴᴊᴏʏ !! ✨🎉\n\n"
                        f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ : <code>{time_str}</code>\n"
                        f"⏳ ᴊᴏɪɴɪɴɢ ᴅᴀᴛᴇ : {current_time}\n\n"
                        f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {expiry_str_in_ist}"
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            try:
                await client.send_message(
                    PREMIUM_LOGS,
                    text=(
                        f"#Added_Premium\n\n"
                        f"👤 ᴜꜱᴇʀ : {mention}\n"
                        f"⚡ ᴜꜱᴇʀ ɪᴅ : <code>{user_id}</code>\n"
                        f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ : <code>{time_str}</code>\n\n"
                        f"⏳ ᴊᴏɪɴɪɴɢ ᴅᴀᴛᴇ : {current_time}\n\n"
                        f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {expiry_str_in_ist}"
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        else:
            await message.reply_text(
                "❌ ɪɴᴠᴀʟɪᴅ ᴛɪᴍᴇ ꜰᴏʀᴍᴀᴛ ❗\n"
                "🕒 ᴘʟᴇᴀsᴇ ᴜsᴇ: <code>1 day</code>, <code>7 days</code>, <code>1 hour</code>, <code>1 min</code>, <code>1 month</code>, or <code>1 year</code>"
            )
    else:
        await message.reply_text(
            "📌 ᴜsᴀɢᴇ: <code>/add_premium user_id time</code>\n"
            "📅 ᴇxᴀᴍᴘʟᴇ: <code>/add_premium 123456 1 month</code>\n"
            "🧭 ᴀᴄᴄᴇᴘᴛᴇᴅ ꜰᴏʀᴍᴀᴛs: <code>1 day</code>, <code>7 days</code>, <code>1 hour</code>, <code>1 min</code>, <code>1 month</code>, <code>1 year</code>"
        )


@Client.on_message(filters.command("premium_users") & filters.user(ADMINS))
async def premium_user(client, message):
    aa = await message.reply_text("<i>ꜰᴇᴛᴄʜɪɴɢ...</i>")
    new = "⚜️ <b>ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ʟɪꜱᴛ :</b>\n\n"
    user_count = 1
    users = await db.get_all_users()
    async for user in users:
        data = await db.get_user(user["id"])
        if data and data.get("expiry_time"):
            expiry = data.get("expiry_time")
            expiry_str_in_ist, time_left_str, total_secs = get_ist_expiry_and_remaining(expiry)
            if total_secs > 0:
                try:
                    u_obj = await client.get_users(user["id"])
                    mention = u_obj.mention
                except Exception:
                    mention = f"<code>{user['id']}</code>"
                new += (
                    f"{user_count}. {mention}\n"
                    f"👤 ᴜꜱᴇʀ ɪᴅ : <code>{user['id']}</code>\n"
                    f"⏳ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {expiry_str_in_ist}\n"
                    f"⏰ ᴛɪᴍᴇ ʟᴇꜰᴛ : {time_left_str}\n\n"
                )
                user_count += 1
    if user_count == 1:
        new += "<i>ɴᴏ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ꜰᴏᴜɴᴅ.</i>"
    try:
        await aa.edit_text(new)
    except MessageTooLong:
        with open("usersplan.txt", "w+", encoding="utf-8") as outfile:
            outfile.write(new)
        await message.reply_document("usersplan.txt", caption="Paid Users:")


@Client.on_message(filters.command("paywall") & filters.user(ADMINS))
async def paywall_command(client, message):
    """
    Admin command to toggle or check Paywall (Paid-Members-Only mode):
    /paywall on | off | status
    """
    bot_id = client.me.id if client.me else 0
    if len(message.command) < 2:
        current = await db.is_paywall_enabled(bot_id)
        status_str = "🔒 <b>ᴏɴ (ᴘᴀɪᴅ ᴍᴇᴍʙᴇʀꜱ ᴏɴʟʏ)</b>" if current else "🟢 <b>ᴏꜰꜰ (ꜰʀᴇᴇ ᴀᴄᴄᴇꜱꜱ ꜰᴏʀ ᴀʟʟ)</b>"
        buttons = [
            [
                InlineKeyboardButton("🔒 ᴛᴜʀɴ ᴏɴ", callback_data="paywall_on"),
                InlineKeyboardButton("🟢 ᴛᴜʀɴ ᴏꜰꜰ", callback_data="paywall_off"),
            ],
            [InlineKeyboardButton("🚫 ᴄʟᴏꜱᴇ", callback_data="close_data")],
        ]
        await message.reply_text(
            f"🛡️ <b>ᴘᴀʏᴡᴀʟʟ ᴀᴄᴄᴇꜱꜱ ᴄᴏɴᴛʀᴏʟ</b>\n\n"
            f"📊 <b>ᴄᴜʀʀᴇɴᴛ ꜱᴛᴀᴛᴜꜱ :</b> {status_str}\n\n"
            f"• ᴡʜᴇɴ <b>ᴏɴ</b>, ᴏɴʟʏ ᴘʀᴇᴍɪᴜᴍ ꜱᴜʙꜱᴄʀɪʙᴇʀꜱ (ᴀɴᴅ ᴀᴅᴍɪɴꜱ) ᴄᴀɴ ᴀᴄᴄᴇꜱꜱ ꜰɪʟᴇꜱ & ꜱᴇᴀʀᴄʜ.\n"
            f"• ᴡʜᴇɴ <b>ᴏꜰꜰ</b>, ᴀʟʟ ᴜꜱᴇʀꜱ ᴄᴀɴ ᴜꜱᴇ ᴛʜᴇ ʙᴏᴛ ꜰʀᴇᴇʟʏ.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    arg = message.command[1].strip().lower()
    if arg in ("on", "enable", "true", "1"):
        await db.set_paywall_enabled(True, bot_id)
        await message.reply_text(
            "🔒 <b>ᴘᴀʏᴡᴀʟʟ ᴇɴᴀʙʟᴇᴅ !</b>\n\n"
            "ᴛʜᴇ ʙᴏᴛ ɪꜱ ɴᴏᴡ ɪɴ <b>ᴘᴀɪᴅ ᴍᴇᴍʙᴇʀꜱ ᴏɴʟʏ</b> ᴍᴏᴅᴇ. "
            "ɴᴏɴ-ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ᴡɪʟʟ ʙᴇ ᴘʀᴏᴍᴘᴛᴇᴅ ᴛᴏ ꜱᴜʙꜱᴄʀɪʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴡɪᴛʜ ʀᴀᴢᴏʀᴘᴀʏ 💳.",
            parse_mode=enums.ParseMode.HTML,
        )
    elif arg in ("off", "disable", "false", "0"):
        await db.set_paywall_enabled(False, bot_id)
        await message.reply_text(
            "🟢 <b>ᴘᴀʏᴡᴀʟʟ ᴅɪꜱᴀʙʟᴇᴅ !</b>\n\n"
            "ᴛʜᴇ ʙᴏᴛ ɪꜱ ɴᴏᴡ ᴏᴘᴇɴ ꜰᴏʀ <b>ꜰʀᴇᴇ ᴘᴜʙʟɪᴄ ᴀᴄᴄᴇꜱꜱ</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        current = await db.is_paywall_enabled(bot_id)
        premium_count = await db.all_premium_users()
        status_str = "🔒 ᴏɴ (ᴘᴀɪᴅ ᴏɴʟʏ)" if current else "🟢 ᴏꜰꜰ (ꜰʀᴇᴇ ᴀᴄᴄᴇꜱꜱ)"
        await message.reply_text(
            f"📊 <b>ᴘᴀʏᴡᴀʟʟ ꜱᴛᴀᴛᴜꜱ :</b> <code>{status_str}</code>\n"
            f"👥 <b>ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ :</b> <code>{premium_count}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_callback_query(filters.regex(r"^paywall_(on|off)$"), group=-1)
async def paywall_callback_handler(client, query: CallbackQuery):
    if query.from_user.id not in ADMINS:
        return await query.answer("⛔ ᴀᴅᴍɪɴ ᴏɴʟʏ !", show_alert=True)
    bot_id = client.me.id if client.me else 0
    enable = query.data == "paywall_on"
    await db.set_paywall_enabled(enable, bot_id)
    status_str = "🔒 <b>ᴏɴ (ᴘᴀɪᴅ ᴍᴇᴍʙᴇʀꜱ ᴏɴʟʏ)</b>" if enable else "🟢 <b>ᴏꜰꜰ (ꜰʀᴇᴇ ᴀᴄᴄᴇꜱꜱ ꜰᴏʀ ᴀʟʟ)</b>"
    buttons = [
        [
            InlineKeyboardButton("🔒 ᴛᴜʀɴ ᴏɴ", callback_data="paywall_on"),
            InlineKeyboardButton("🟢 ᴛᴜʀɴ ᴏꜰꜰ", callback_data="paywall_off"),
        ],
        [InlineKeyboardButton("🚫 ᴄʟᴏꜱᴇ", callback_data="close_data")],
    ]
    try:
        await query.message.edit_text(
            f"🛡️ <b>ᴘᴀʏᴡᴀʟʟ ᴀᴄᴄᴇꜱꜱ ᴄᴏɴᴛʀᴏʟ</b>\n\n"
            f"📊 <b>ᴄᴜʀʀᴇɴᴛ ꜱᴛᴀᴛᴜꜱ :</b> {status_str}\n\n"
            f"• ᴡʜᴇɴ <b>ᴏɴ</b>, ᴏɴʟʏ ᴘʀᴇᴍɪᴜᴍ ꜱᴜʙꜱᴄʀɪʙᴇʀꜱ (ᴀɴᴅ ᴀᴅᴍɪɴꜱ) ᴄᴀɴ ᴀᴄᴄᴇꜱꜱ ꜰɪʟᴇꜱ & ꜱᴇᴀʀᴄʜ.\n"
            f"• ᴡʜᴇɴ <b>ᴏꜰꜰ</b>, ᴀʟʟ ᴜꜱᴇʀꜱ ᴄᴀɴ ᴜꜱᴇ ᴛʜᴇ ʙᴏᴛ ꜰʀᴇᴇʟʏ.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass
    await query.answer(f"Paywall is now {'ON (Paid Only)' if enable else 'OFF (Free Access)'}", show_alert=True)


# =============================================================================
# User Plan Command & Automatic Razorpay Payment Handlers
# =============================================================================

@Client.on_message(filters.command(["plan", "plans", "buy", "vip"]))
async def plan(client, message):
    user_id = message.from_user.id
    users = message.from_user.mention
    log_message = (
        f"<b><u>💎 ᴛʜɪs ᴜsᴇʀ ᴄʜᴇᴄᴋᴇᴅ /plan</u> {temp.B_LINK}\n\n"
        f"- ɪᴅ - `{user_id}`\n- ɴᴀᴍᴇ - {users}</b>"
    )
    reply_markup = build_razorpay_plan_buttons(include_extras=True)
    msg = await message.reply_photo(
        photo="https://graph.org/file/86da2027469565b5873d6.jpg",
        caption=script.BPREMIUM_TXT,
        reply_markup=reply_markup,
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        await client.send_message(PREMIUM_LOGS, log_message)
    except Exception:
        pass
    await asyncio.sleep(300)
    try:
        await msg.delete()
        await message.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^buy_\d+$"), group=-1)
async def premium_button(client, callback_query: CallbackQuery):
    try:
        amount = int(callback_query.data.split("_")[1])
        if amount not in RAZORPAY_PLANS:
            await callback_query.answer("⚠️ Invalid Premium Package.", show_alert=True)
            return

        user_id = callback_query.from_user.id
        chat_type = callback_query.message.chat.type if callback_query.message else enums.ChatType.PRIVATE

        if not RAZORPAY_KEY_ID and not RAZORPAY_PROVIDER_TOKEN:
            await callback_query.answer(
                "⚠️ Razorpay credentials (RAZORPAY_KEY_ID & RAZORPAY_KEY_SECRET) are not configured yet.",
                show_alert=True,
            )
            return

        # If triggered inside a group, send the Razorpay checkout to user's PM or redirect to PM
        if chat_type != enums.ChatType.PRIVATE:
            sent = await send_razorpay_payment(client, user_id, user_id, amount)
            if sent:
                await callback_query.answer("📩 Razorpay payment link sent to your Private Messages (PM)!", show_alert=True)
            else:
                bot_username = temp.U_NAME or (await client.get_me()).username
                pm_url = f"https://t.me/{bot_username}?start=buy_{amount}"
                await callback_query.answer()
                await callback_query.message.reply_text(
                    "<b>💳 <u>ᴄᴏᴍᴘʟᴇᴛᴇ ʀᴀᴢᴏʀᴘᴀʏ ᴘᴀʏᴍᴇɴᴛ ɪɴ ᴘᴍ</u>\n\n"
                    "ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴏᴘᴇɴ ʏᴏᴜʀ ɪɴꜱᴛᴀɴᴛ ʀᴀᴢᴏʀᴘᴀʏ ᴄʜᴇᴄᴋᴏᴜᴛ ɪɴ ᴘʀɪᴠᴀᴛᴇ ᴄʜᴀᴛ:</b>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"💳 Pay ₹{amount} via Razorpay in PM", url=pm_url)]
                    ]),
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        target_chat_id = callback_query.message.chat.id if callback_query.message else user_id
        sent = await send_razorpay_payment(client, target_chat_id, user_id, amount)
        if sent:
            await callback_query.answer()
        else:
            await callback_query.answer("🚫 Error creating Razorpay payment link. Please check Razorpay API keys.", show_alert=True)
    except Exception as e:
        logger.error(f"Error In buy_ callback: {e}")
        await callback_query.answer("🚫 Error Processing Your Payment. Try again.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^rzp_verify#"), group=-1)
async def razorpay_verify_callback(client, query: CallbackQuery):
    """Handle manual 1-tap verification of a Razorpay Payment Link."""
    try:
        parts = query.data.split("#")
        plink_id = parts[1]
        amount_inr = int(parts[2])
        target_user_id = int(parts[3]) if len(parts) > 3 else query.from_user.id

        if query.from_user.id != target_user_id and query.from_user.id not in ADMINS:
            return await query.answer("⚠️ This payment link belongs to another user.", show_alert=True)

        if plink_id in _PROCESSED_PAYMENTS:
            return await query.answer("✅ Payment already verified and Premium activated!", show_alert=True)

        info = await check_razorpay_payment_link(plink_id)
        if not info:
            return await query.answer("⚠️ Could not reach Razorpay right now. Please try again in a moment.", show_alert=True)

        status = str(info.get("status", "")).lower()
        if status == "paid":
            payments = info.get("payments") or []
            payment_id = ""
            if payments and isinstance(payments, list) and isinstance(payments[0], dict):
                payment_id = payments[0].get("payment_id", "") or ""
            await query.answer("✅ Payment verified! Activating Premium...", show_alert=True)
            await fulfill_razorpay_payment(
                client=client,
                plink_id=plink_id,
                user_id=target_user_id,
                amount_inr=amount_inr,
                payment_id=payment_id,
                chat_id=query.message.chat.id if query.message else target_user_id,
                message_id=query.message.id if query.message else None,
            )
        elif status in ("cancelled", "expired"):
            await query.answer(f"❌ This payment link has {status}. Please generate a new one via /plan.", show_alert=True)
        else:
            await query.answer(
                "⏳ Payment not completed yet. Complete the payment via the Razorpay link first, then tap Verify!",
                show_alert=True,
            )
    except Exception as e:
        logger.error(f"Error in razorpay_verify_callback: {e}")
        await query.answer("⚠️ Error checking payment status.", show_alert=True)


@Client.on_pre_checkout_query()
async def pre_checkout_handler(client, query: PreCheckoutQuery):
    try:
        if query.payload and query.payload.startswith("dreamxrazorpay_"):
            await query.answer(success=True)
        else:
            await query.answer(success=False, error="⚠️ Invalid Purchase Type.")
    except Exception as e:
        logger.error(f"Pre-checkout error: {e}")
        try:
            await query.answer(success=False, error="🚫 Unexpected Error Occurred.")
        except Exception:
            pass


@Client.on_message(filters.successful_payment)
async def successful_premium_payment(client, message):
    """Handle native Telegram INR invoice payments via Razorpay provider token."""
    try:
        payment = message.successful_payment
        # Telegram INR invoices report total_amount in paise (100 paise = ₹1)
        raw_total = int(payment.total_amount)
        amount_inr = raw_total // 100 if payment.currency == "INR" else raw_total
        charge_id = (
            getattr(payment, "provider_payment_charge_id", "")
            or getattr(payment, "telegram_payment_charge_id", "N/A")
            or "N/A"
        )
        user_id = message.from_user.id
        await fulfill_razorpay_payment(
            client=client,
            plink_id=charge_id,
            user_id=user_id,
            amount_inr=amount_inr,
            payment_id=charge_id,
            chat_id=message.chat.id,
        )
    except Exception as e:
        logger.error(f"Error Processing Razorpay Provider Payment: {e}")
        await message.reply_text("✅ Thank You For Your Payment! Your subscription is being processed.")
