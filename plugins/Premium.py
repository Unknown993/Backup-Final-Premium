import re
import pytz
import datetime
import asyncio
import logging
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


def build_star_plan_buttons(include_extras: bool = True, back_callback: str = None) -> InlineKeyboardMarkup:
    """Build one-tap automatic Telegram Stars purchase buttons for all plans."""
    buttons = []
    for stars in sorted(STAR_PREMIUM_PLANS.keys()):
        raw_dur = STAR_PREMIUM_PLANS[stars]
        label = format_plan_label(raw_dur)
        buttons.append([
            InlineKeyboardButton(
                f"⭐ {label} Pʀᴇᴍɪᴜᴍ — {stars} Sᴛᴀʀꜱ",
                callback_data=f"buy_{stars}",
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


async def send_paywall_alert(client, chat_id: int, user_mention: str):
    """Send paywall requirement prompt with automatic Telegram Stars buttons."""
    caption = script.PAYWALL_ALERT_TEXT.format(user_mention)
    markup = build_star_plan_buttons(include_extras=True)
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


async def send_star_invoice(client, chat_id: int, user_id: int, amount: int) -> bool:
    """
    Generate and send an automatic Telegram Stars (XTR) invoice to the specified chat.
    Ensures the first button is an InlineKeyboardButtonBuy (pay=True).
    """
    if amount not in STAR_PREMIUM_PLANS:
        return False

    raw_duration = STAR_PREMIUM_PLANS[amount]
    readable_duration = format_plan_label(raw_duration)
    payload = f"dreamxpremium_{amount}_{user_id}_{int(datetime.datetime.now().timestamp())}"

    buttons = [
        [InlineKeyboardButtonBuy(text=f"⭐ Pay {amount} Stars")],
        [InlineKeyboardButton("ᴄᴀɴᴄᴇʟ 🚫", callback_data="close_data")],
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    try:
        await client.send_invoice(
            chat_id=chat_id,
            title=f"⭐ VIP Premium ({readable_duration})",
            description=(
                f"Instant automatic activation of {readable_duration} Premium Access "
                f"(Direct files, No verification, Ad-free, High-speed streaming)."
            ),
            payload=payload,
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f"Premium ({readable_duration})",
                    amount=amount,
                )
            ],
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        logger.error(f"Error sending Star invoice to {chat_id}: {e}")
        return False


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
                stars_paid = data.get("stars_paid", 0)
                caption = (
                    f"⚜️ <b>ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :</b>\n\n"
                    f"👤 <b>ᴜꜱᴇʀ :</b> {user}\n"
                    f"⚡ <b>ᴜꜱᴇʀ ɪᴅ :</b> <code>{user_id}</code>\n"
                    f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_name}</code>\n"
                    f"⭐ <b>ꜱᴛᴀʀꜱ ᴘᴀɪᴅ :</b> <code>{stars_paid}</code>\n"
                    f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> {time_left_str}\n"
                    f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}"
                )

                await message.reply_photo(
                    photo=SUBSCRIPTION,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔥 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ (ᴀᴜᴛᴏ-ᴘᴀʏ)", callback_data="star_info")]]
                    ),
                )
                return

        await message.reply_photo(
            photo="https://i.ibb.co/gMrpRQWP/photo-2025-07-09-05-21-32-7524948058832896004.jpg",
            caption=(
                f"<b>ʜᴇʏ {user},\n\n"
                f"ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ.\n"
                f"ꜱᴇʟᴇᴄᴛ ᴀ ᴘʟᴀɴ ʙᴇʟᴏᴡ ꜰᴏʀ ɪɴꜱᴛᴀɴᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ ᴠɪᴀ ᴛᴇʟᴇɢʀᴀᴍ ꜱᴛᴀʀꜱ ⭐!</b>"
            ),
            reply_markup=build_star_plan_buttons(include_extras=False),
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
                stars_paid = data.get("stars_paid", 0)
                caption = (
                    f"⚜️ <b>ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :</b>\n\n"
                    f"👤 <b>ᴜꜱᴇʀ :</b> {user}\n"
                    f"⚡ <b>ᴜꜱᴇʀ ɪᴅ :</b> <code>{user_id}</code>\n"
                    f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_name}</code>\n"
                    f"⭐ <b>ꜱᴛᴀʀꜱ ᴘᴀɪᴅ :</b> <code>{stars_paid}</code>\n"
                    f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> {time_left_str}\n"
                    f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}"
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔥 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ", callback_data="star_info")],
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
            f"ꜱᴇʟᴇᴄᴛ ᴀ ᴘʟᴀɴ ʙᴇʟᴏᴡ ꜰᴏʀ ɪɴꜱᴛᴀɴᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ ᴠɪᴀ ᴛᴇʟᴇɢʀᴀᴍ ꜱᴛᴀʀꜱ ⭐!</b>"
        )
        markup = build_star_plan_buttons(include_extras=False, back_callback="premium_info")
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
            stars_paid = data.get("stars_paid", 0)
            await message.reply_text(
                f"⚜️ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :\n\n"
                f"👤 ᴜꜱᴇʀ : {mention}\n"
                f"⚡ ᴜꜱᴇʀ ɪᴅ : <code>{user_id}</code>\n"
                f"📦 ᴘʟᴀɴ : <code>{plan_name}</code>\n"
                f"⭐ ꜱᴛᴀʀꜱ ᴘᴀɪᴅ : <code>{stars_paid}</code>\n"
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
                stars_paid=0,
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
            "ɴᴏɴ-ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ᴡɪʟʟ ʙᴇ ᴘʀᴏᴍᴘᴛᴇᴅ ᴛᴏ ꜱᴜʙꜱᴄʀɪʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴡɪᴛʜ ᴛᴇʟᴇɢʀᴀᴍ ꜱᴛᴀʀꜱ ⭐.",
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
# User Plan Command & Automatic Telegram Star Payment Handlers
# =============================================================================

@Client.on_message(filters.command(["plan", "plans", "buy", "vip"]))
async def plan(client, message):
    user_id = message.from_user.id
    users = message.from_user.mention
    log_message = (
        f"<b><u>💎 ᴛʜɪs ᴜsᴇʀ ᴄʜᴇᴄᴋᴇᴅ /plan</u> {temp.B_LINK}\n\n"
        f"- ɪᴅ - `{user_id}`\n- ɴᴀᴍᴇ - {users}</b>"
    )
    reply_markup = build_star_plan_buttons(include_extras=True)
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
        if amount not in STAR_PREMIUM_PLANS:
            await callback_query.answer("⚠️ Invalid Premium Package.", show_alert=True)
            return

        user_id = callback_query.from_user.id
        chat_type = callback_query.message.chat.type if callback_query.message else enums.ChatType.PRIVATE

        # If triggered inside a group, send the invoice to user's private chat or redirect to PM
        if chat_type != enums.ChatType.PRIVATE:
            sent = await send_star_invoice(client, user_id, user_id, amount)
            if sent:
                await callback_query.answer("📩 Invoice sent to your Private Messages (PM)!", show_alert=True)
            else:
                bot_username = temp.U_NAME or (await client.get_me()).username
                pm_url = f"https://t.me/{bot_username}?start=buy_{amount}"
                await callback_query.answer()
                await callback_query.message.reply_text(
                    "<b>💳 <u>ᴄᴏᴍᴘʟᴇᴛᴇ ᴘᴀʏᴍᴇɴᴛ ɪɴ ᴘᴍ</u>\n\n"
                    "ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴏᴘᴇɴ ʏᴏᴜʀ ɪɴꜱᴛᴀɴᴛ ᴛᴇʟᴇɢʀᴀᴍ ꜱᴛᴀʀꜱ ɪɴᴠᴏɪᴄᴇ ɪɴ ᴘʀɪᴠᴀᴛᴇ ᴄʜᴀᴛ:</b>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"⭐ Pay {amount} Stars in PM", url=pm_url)]
                    ]),
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        target_chat_id = callback_query.message.chat.id if callback_query.message else user_id
        sent = await send_star_invoice(client, target_chat_id, user_id, amount)
        if sent:
            await callback_query.answer()
        else:
            await callback_query.answer("🚫 Error Processing Your Payment. Try again.", show_alert=True)
    except Exception as e:
        logger.error(f"Error In buy_ callback: {e}")
        await callback_query.answer("🚫 Error Processing Your Payment. Try again.", show_alert=True)


@Client.on_pre_checkout_query()
async def pre_checkout_handler(client, query: PreCheckoutQuery):
    try:
        if query.payload and query.payload.startswith("dreamxpremium_"):
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
    try:
        payment = message.successful_payment
        amount = int(payment.total_amount)
        charge_id = getattr(payment, "telegram_payment_charge_id", "N/A") or "N/A"
        user_id = message.from_user.id
        user_mention = message.from_user.mention if message.from_user else f"<code>{user_id}</code>"
        time_zone = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
        current_time = time_zone.strftime("%d-%m-%Y | %I:%M:%S %p")

        if amount in STAR_PREMIUM_PLANS:
            raw_time = STAR_PREMIUM_PLANS[amount]
            plan_label = format_plan_label(raw_time)
            seconds = await get_seconds(raw_time)
        else:
            # Fallback automatic calculation if custom star amount
            days = max(1, int(amount * 0.7))
            raw_time = f"{days}day"
            plan_label = f"{days} Days"
            seconds = days * 86400

        if seconds > 0:
            data = await db.add_or_extend_premium(
                user_id=user_id,
                seconds=seconds,
                stars_paid=amount,
                plan_name=plan_label,
            )
            expiry = data.get("expiry_time")
            expiry_str_in_ist, time_left_str, _ = get_ist_expiry_and_remaining(expiry)

            await message.reply_text(
                text=(
                    f"🎉 <b>ᴘᴀʏᴍᴇɴᴛ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ! ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ ✅</b>\n\n"
                    f"👋 ᴛʜᴀɴᴋ ʏᴏᴜ {user_mention}, ʏᴏᴜʀ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴘᴀʏᴍᴇɴᴛ ʜᴀꜱ ʙᴇᴇɴ ᴠᴇʀɪꜰɪᴇᴅ!\n\n"
                    f"📦 <b>ᴘʟᴀɴ :</b> <code>{plan_label}</code>\n"
                    f"⭐ <b>ꜱᴛᴀʀꜱ ᴘᴀɪᴅ :</b> <code>{amount} ⭐</code>\n"
                    f"⏰ <b>ᴛɪᴍᴇ ʟᴇꜰᴛ :</b> <code>{time_left_str}</code>\n"
                    f"⌛️ <b>ᴇxᴘɪʀʏ ᴅᴀᴛᴇ :</b> {expiry_str_in_ist}\n"
                    f"🔖 <b>ᴛʀᴀɴꜱᴀᴄᴛɪᴏɴ ɪᴅ :</b> <code>{charge_id}</code>\n\n"
                    f"🚀 <i>ᴇɴᴊᴏʏ ʏᴏᴜʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴅ-ꜰʀᴇᴇ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ!</i>"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 ᴠɪᴇᴡ ᴍʏ ᴘʟᴀɴ", callback_data="myplan_cb")]
                ]),
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )

            try:
                await client.send_message(
                    PREMIUM_LOGS,
                    text=(
                        f"#Purchase_Premium_With_Star\n\n"
                        f"👤 ᴜꜱᴇʀ - {user_mention}\n"
                        f"⚡ ᴜꜱᴇʀ ɪᴅ - <code>{user_id}</code>\n"
                        f"⭐ ꜱᴛᴀʀ ᴘᴀʏ - <code>{amount}⭐</code>\n"
                        f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ - <code>{plan_label}</code>\n"
                        f"⏳ ᴊᴏɪɴɪɴɢ ᴅᴀᴛᴇ - {current_time}\n"
                        f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ - {expiry_str_in_ist}\n"
                        f"🔖 ᴄʜᴀʀɢᴇ ɪᴅ - <code>{charge_id}</code>"
                    ),
                    disable_web_page_preview=True,
                )
            except Exception as log_err:
                logger.warning(f"Failed to send payment log to PREMIUM_LOGS: {log_err}")
        else:
            await message.reply_text("⚠️ Invalid Premium Time.")
    except Exception as e:
        logger.error(f"Error Processing Premium Payment: {e}")
        await message.reply_text("✅ Thank You For Your Payment! Your subscription is being processed.")
