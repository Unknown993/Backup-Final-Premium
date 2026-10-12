import os
import sys
import glob
import importlib
import asyncio
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Install uvloop before creating the event loop.
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

# Initialize the event loop BEFORE importing Pyrogram
# and the other bot modules.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pyrogram import Client, idle, __version__
from pyrogram.raw.all import layer
from pyrogram.errors import FloodWait

import time
from datetime import date, datetime

import pytz
from aiohttp import web

from database.ia_filterdb import Media, Media2
from database.users_chats_db import db
from info import *
from utils import temp
from Script import script
from plugins import web_server, check_expired_premium, keep_alive
from dreamxbotz.Bot import dreamxbotz
from dreamxbotz.util.keepalive import ping_server
from dreamxbotz.Bot.clients import initialize_clients

from PIL import Image

Image.MAX_IMAGE_PIXELS = 500_000_000

import logging
import logging.config

logging.config.fileConfig("logging.conf")

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)
logging.getLogger("pymongo").setLevel(logging.WARNING)

botStartTime = time.time()

ppath = "plugins/*.py"
files = glob.glob(ppath)


async def dreamxbotz_start():
    print("\n\nInitializing DreamxBotz")

    await dreamxbotz.start()

    bot_info = await dreamxbotz.get_me()
    dreamxbotz.username = bot_info.username

    await initialize_clients()

    # Import plugins.
    for name in files:
        with open(name, encoding="utf-8") as a:
            patt = Path(a.name)
            plugin_name = patt.stem
            plugins_dir = Path(f"plugins/{plugin_name}.py")
            import_path = f"plugins.{plugin_name}"

            spec = importlib.util.spec_from_file_location(
                import_path, plugins_dir
            )

            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)

            sys.modules["plugins." + plugin_name] = load

            print("DreamxBotz Imported => " + plugin_name)

    # Start Heroku keep-alive task.
    if ON_HEROKU:
        asyncio.create_task(ping_server())

    # Load banned users and chats.
    b_users, b_chats = await db.get_banned()

    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats

    # Create database indexes.
    await Media.ensure_indexes()

    if MULTIPLE_DB:
        await Media2.ensure_indexes()

        print(
            "Multiple Database Mode On. "
            "Files will be saved in the second DB if the first DB is full."
        )
    else:
        print("Single DB Mode On! Files will be saved in the first DB.")

    # Store bot information.
    me = await dreamxbotz.get_me()

    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    temp.B_LINK = me.mention

    dreamxbotz.username = "@" + me.username

    # Background tasks.
    dreamxbotz.loop.create_task(
        check_expired_premium(dreamxbotz)
    )

    logging.info(
        f"{me.first_name} with Pyrogram v{__version__} "
        f"(Layer {layer}) started on {me.username}."
    )

    logging.info(LOG_STR)
    logging.info(script.LOGO)

    # Current Indian date and time.
    tz = pytz.timezone("Asia/Kolkata")

    today = date.today()
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M:%S %p")

    # Send restart notification.
    try:
        await dreamxbotz.send_message(
            chat_id=LOG_CHANNEL,
            text=script.RESTART_TXT.format(
                temp.B_LINK,
                today,
                current_time
            ),
        )
    except Exception as e:
        logging.warning(
            f"Could not send restart notification to "
            f"LOG_CHANNEL ({LOG_CHANNEL}): {e}"
        )

    # Start web server.
    app = web.AppRunner(await web_server())
    await app.setup()

    bind_address = "0.0.0.0"

    await web.TCPSite(
        app,
        bind_address,
        PORT
    ).start()

    # Start keep-alive task.
    dreamxbotz.loop.create_task(keep_alive())

    # Keep the bot running.
    await idle()


if __name__ == "__main__":
    try:
        loop.run_until_complete(dreamxbotz_start())

    except FloodWait as e:
        logging.warning(
            f"FloodWait! Sleeping for {e.value} seconds."
        )
        time.sleep(e.value)

    except KeyboardInterrupt:
        logging.info("Service stopped. Bye!")

    except Exception:
        logging.exception("Bot startup failed")
        raise

    finally:
        if not loop.is_closed():
            # Cancel pending tasks before closing the loop.
            pending = asyncio.all_tasks(loop)

            for task in pending:
                task.cancel()

            if pending:
                loop.run_until_complete(
                    asyncio.gather(
                        *pending,
                        return_exceptions=True
                    )
                )

            loop.close()
