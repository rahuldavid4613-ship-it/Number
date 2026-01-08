import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
from datetime import datetime, timezone
import requests


# ================== CONFIG ==================
# <-- apna bot token
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

ADMIN_IDS = [7354121862]                # <-- apna Telegram user id (int)

DEFAULT_CREDITS = 10    # first time user = 10 credits
LOOKUP_COST = 1         # har lookup pe 1 credit

# Force channel join
CHANNEL_ID = -1002163522585
CHANNEL_LINK = "https://t.me/+XZ0vuAh5fzVhYzg1"

bot = telebot.TeleBot(BOT_TOKEN)


# ================== DATABASE SETUP ==================
conn = sqlite3.connect("bott.db", check_same_thread=False)
cur = conn.cursor()


def ensure_column(table: str, col_def: str):
    col_name = col_def.split()[0]
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if col_name not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        conn.commit()


def init_db():
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()

    ensure_column("users", "username TEXT")
    ensure_column("users", "credits INTEGER DEFAULT 0")
    ensure_column("users", "referred_by INTEGER")
    ensure_column("users", "is_banned INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            result TEXT,
            created_at TEXT
        )
    """)
    conn.commit()


init_db()

# ================== HELPERS ==================

def get_or_create_user(user_id, username=None, referred_by=None):
    cur.execute("SELECT user_id, username, credits, referred_by, is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        if username and row[1] != username:
            cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
        return row

    cur.execute(
        "INSERT INTO users (user_id, username, credits, referred_by, is_banned) VALUES (?, ?, ?, ?, 0)",
        (user_id, username, DEFAULT_CREDITS, referred_by)
    )
    conn.commit()
    cur.execute("SELECT user_id, username, credits, referred_by, is_banned FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()


def set_credits(user_id, amount):
    cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def add_credits(user_id, amount):
    cur.execute("UPDATE users SET credits = COALESCE(credits, 0) + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def remove_credits(user_id, amount):
    cur.execute(
        "UPDATE users SET credits = MAX(COALESCE(credits, 0) - ?, 0) WHERE user_id = ?",
        (amount, user_id)
    )
    conn.commit()


def get_credits(user_id):
    cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def set_ban_status(user_id, status: bool):
    cur.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if status else 0, user_id))
    conn.commit()


def is_banned(user_id):
    cur.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return bool(row[0]) if row and row[0] is not None else False


def save_history(user_id, query, result):
    cur.execute(
        "INSERT INTO history (user_id, query, result, created_at) VALUES (?, ?, ?, ?)",
        (user_id, query, result[:1000], datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def get_history(user_id, limit=10):
    cur.execute(
        "SELECT query, result, created_at FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    return cur.fetchall()

# ---------------- FORCE SUB ----------------

def is_user_in_channel(user_id: int) -> bool:
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


def send_force_sub(chat_id: int):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🔔 Join Channel", url=CHANNEL_LINK)
    )
    kb.row(
        InlineKeyboardButton("✅ Joined, Check Again", callback_data="check_sub")
    )
    bot.send_message(
        chat_id,
        "📢 Bot use karne ke liye pehle hamara official channel join karein.\n\n"
        "Channel join karne ke baad ‘Joined, Check Again’ dabayein.",
        reply_markup=kb
    )


def ensure_user_record_from_obj(user_obj):
    """Force-sub pass karne ke baad user DB me ho ye ensure karta hai."""
    user_id = user_obj.id
    username = user_obj.username
    get_or_create_user(user_id, username=username, referred_by=None)

# ================== UI KEYBOARDS ==================

def main_menu(is_admin=False):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🔍 Number to Info", callback_data="number_info"),
    )
    kb.row(
        InlineKeyboardButton("🎁 Referral", callback_data="referral"),
        InlineKeyboardButton("💳 My Credits", callback_data="my_credits"),
    )
    kb.row(
        InlineKeyboardButton("📜 My History", callback_data="my_history"),
    )
    if is_admin:
        kb.row(
            InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")
        )
    return kb


def admin_menu():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Add Credit", callback_data="admin_add_credit"),
        InlineKeyboardButton("➖ Remove Credit", callback_data="admin_remove_credit"),
    )
    kb.row(
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
    )
    kb.row(
        InlineKeyboardButton("👥 All Users", callback_data="admin_all_users"),
    )
    kb.row(
        InlineKeyboardButton("🔒 Ban User", callback_data="admin_ban"),
        InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Back", callback_data="back_main")
    )
    return kb

# ================== STATE ==================
USER_STATE = {}   # {user_id: "awaiting_number" / None}
ADMIN_STATE = {}  # {user_id: {"mode": "add_credit"/...}}

# ================== REAL LOOKUP FUNCTION ==================

def _format_value(val, indent=0):
    space = "  " * indent
    if isinstance(val, dict):
        lines = []
        for k, v in val.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{space}{k}:")
                lines.append(_format_value(v, indent + 1))
            else:
                lines.append(f"{space}{k}: {v}")
        return "\n".join(lines)
    elif isinstance(val, list):
        lines = []
        for idx, item in enumerate(val, start=1):
            if isinstance(item, (dict, list)):
                lines.append(f"{space}- [{idx}]")
                lines.append(_format_value(item, indent + 1))
            else:
                lines.append(f"{space}- {item}")
        return "\n".join(lines)
    else:
        return f"{space}{val}"


def lookup_number(mobile_number: str) -> str:
    """
    API se number ka data laata hai.
    Agar API me koi bhi error aaye to user ko
    sirf generic message diya jaayega.
    """
    GENERIC_ERROR = "⚠️ API not working currently, please contact admin."

    try:
        num = mobile_number.strip().replace(" ", "").replace("-", "")

        if not num:
            return "Number khali hai."

        url = f"https://kalyug-papa.vercel.app/api/info?num={num}&key=jhat-ke-pakode"
        resp = requests.get(url, timeout=15)

        # HTTP error => generic msg
        if resp.status_code != 200:
            return GENERIC_ERROR

        # JSON parse
        try:
            data = resp.json()
        except Exception:
            return GENERIC_ERROR

        # API sahi hai lekin data empty
        if not data:
            return "❗ Data Not found ❌."

        pretty = _format_value(data, 0)

        if len(pretty) > 3900:
            pretty = pretty[:3900] + "\n\n… (trimmed)"

        return pretty

    except requests.RequestException:
        return GENERIC_ERROR
    except Exception:
        return GENERIC_ERROR

# ================== COMMAND HANDLERS ==================

@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id

    # Force join
    if not is_user_in_channel(user_id):
        send_force_sub(message.chat.id)
        return

    username = message.from_user.username

    # Referral
    args = message.text.split()
    referred_by = None
    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id:
                referred_by = ref_id
        except ValueError:
            pass

    # yahan user create hoga
    user = get_or_create_user(user_id, username=username, referred_by=referred_by)

    # referral bonus
    if referred_by:
        add_credits(referred_by, 2)

    if is_banned(user_id):
        bot.reply_to(message, "🚫 Aapko is bot se ban kiya gaya hai. Admin se contact karein.")
        return

    text = (
        f"👋 Namaste {message.from_user.first_name}!\n\n"
        "📞 Kalyug ke Bot me swagat hai.\n\n"
        "Yahan aap kar sakte hain:\n"
        "• 🔍 Number to Info lookup\n"
        "• 🎁 Referral se credits earn\n"
        "• 💳 Credits balance check\n"
        "• 📜 History dekhna\n\n"
        "⚠️ Sirf legal & ethical kaam ke liye use karein.\n\n"
        "Neeche se option choose karein 👇"
    )

    is_admin = user_id in ADMIN_IDS
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=main_menu(is_admin)
    )

# ================== CALLBACK HANDLER ==================

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data

    # check_sub for force join
    if data == "check_sub":
        bot.answer_callback_query(call.id)
        if is_user_in_channel(user_id):
            ensure_user_record_from_obj(call.from_user)
            is_admin = user_id in ADMIN_IDS
            bot.send_message(
                call.message.chat.id,
                "✅ Subscription verify ho gaya. Ab aap bot use kar sakte hain.",
                reply_markup=main_menu(is_admin)
            )
        else:
            send_force_sub(call.message.chat.id)
        return

    # Baaki sab pe force join check
    if not is_user_in_channel(user_id):
        bot.answer_callback_query(call.id)
        send_force_sub(call.message.chat.id)
        return

    # ensure user row exists for callbacks
    ensure_user_record_from_obj(call.from_user)

    if is_banned(user_id) and user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "You are banned.")
        return

    # MAIN MENU ACTIONS
    if data == "number_info":
        USER_STATE[user_id] = "awaiting_number"
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📞 Mobile number bhejein (sirf digits, jaise 6200303551).\n"
            f"Har lookup me {LOOKUP_COST} credit katega."
        )

    elif data == "referral":
        bot.answer_callback_query(call.id)
        ref_link = f"https://t.me/{bot.get_me().username}?start={user_id}"
        text = (
            "🎁 Referral Program\n\n"
            "Doston ko ye link bhejein. Jab wo bot start karenge, "
            "unko free credits milenge aur aapko +2 credits milenge.\n\n"
            f"{ref_link}"
        )
        bot.send_message(call.message.chat.id, text)

    elif data == "my_credits":
        bot.answer_callback_query(call.id)
        credits = get_credits(user_id)
        bot.send_message(call.message.chat.id, f"💳 Aapke paas abhi {credits} credits hain.")

    elif data == "my_history":
        bot.answer_callback_query(call.id)
        rows = get_history(user_id, limit=10)
        if not rows:
            bot.send_message(call.message.chat.id, "📜 Abhi tak koi history nahi mili.")
        else:
            lines = ["📜 Last 10 lookups:"]
            for q, res, dt in rows:
                lines.append(f"- {q} @ {dt}")
            bot.send_message(call.message.chat.id, "\n".join(lines))

    # ADMIN PANEL
    elif data == "admin_panel":
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Admin only.")
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "🛠 Admin Panel",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=admin_menu()
        )

    elif data == "back_main":
        is_admin = user_id in ADMIN_IDS
        bot.edit_message_text(
            "🏠 Main Menu",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=main_menu(is_admin)
        )

    # ADMIN ACTIONS
    elif data in ["admin_add_credit", "admin_remove_credit", "admin_broadcast", "admin_ban", "admin_unban", "admin_all_users"]:
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Admin only.")
            return
        bot.answer_callback_query(call.id)

        if data == "admin_add_credit":
            ADMIN_STATE[user_id] = {"mode": "add_credit"}
            bot.send_message(call.message.chat.id, "➕ User ID aur credits bhejein (format: user_id credits).")

        elif data == "admin_remove_credit":
            ADMIN_STATE[user_id] = {"mode": "remove_credit"}
            bot.send_message(call.message.chat.id, "➖ User ID aur credits bhejein (format: user_id credits).")

        elif data == "admin_ban":
            ADMIN_STATE[user_id] = {"mode": "ban"}
            bot.send_message(call.message.chat.id, "🔒 Ban karne ke liye user ID bhejein.")

        elif data == "admin_unban":
            ADMIN_STATE[user_id] = {"mode": "unban"}
            bot.send_message(call.message.chat.id, "🔓 Unban karne ke liye user ID bhejein.")

        elif data == "admin_broadcast":
            ADMIN_STATE[user_id] = {"mode": "broadcast"}
            bot.send_message(call.message.chat.id, "📢 Broadcast message bhejein (plain text).")

        elif data == "admin_all_users":
            # show list of all users + credits
            cur.execute("SELECT user_id, username, credits FROM users ORDER BY user_id")
            rows = cur.fetchall()
            if not rows:
                bot.send_message(call.message.chat.id, "👥 Abhi tak koi user register nahi hai.")
            else:
                header = "👥 All Users List:\n(user_id | username | credits)\n\n"
                chunk = header
                for uid, uname, cr in rows:
                    uname = uname if uname else "-"
                    line = f"{uid} | @{uname} | {cr} cr\n"
                    if len(chunk) + len(line) > 3900:
                        bot.send_message(call.message.chat.id, chunk)
                        chunk = ""
                    chunk += line
                if chunk:
                    bot.send_message(call.message.chat.id, chunk)

# ================== MESSAGE HANDLERS (STATE) ==================

@bot.message_handler(func=lambda m: USER_STATE.get(m.from_user.id) == "awaiting_number")
def handle_number_lookup(message):
    user_id = message.from_user.id

    if not is_user_in_channel(user_id):
        USER_STATE.pop(user_id, None)
        send_force_sub(message.chat.id)
        return

    ensure_user_record_from_obj(message.from_user)

    number = message.text.strip()

    if is_banned(user_id):
        bot.reply_to(message, "🚫 Aap banned hain.")
        return

    credits = get_credits(user_id)
    if credits < LOOKUP_COST:
        bot.reply_to(message, "❌ Aapke paas enough credits nahi hain. Pehle credits add karwayein.")
        USER_STATE[user_id] = None
        return

    bot.reply_to(message, "⌛ Lookup chalu hai, thoda wait karein…")
    result_text = lookup_number(number)

    remove_credits(user_id, LOOKUP_COST)
    save_history(user_id, number, result_text)

    # Stylish result message (without Markdown)
    header = (
        "✅ Lookup Complete\n"
        "━━━━━━━━━━━━━━\n"
        f"📱 Number: {number}\n\n"
        "📂 Extracted Data:\n"
    )

    bot.send_message(message.chat.id, header + result_text)

    remaining = get_credits(user_id)
    bot.send_message(message.chat.id, f"💳 Remaining credits: {remaining}")
    USER_STATE[user_id] = None


@bot.message_handler(func=lambda m: m.from_user.id in ADMIN_STATE)
def handle_admin_state(message):
    user_id = message.from_user.id

    if not is_user_in_channel(user_id):
        ADMIN_STATE.pop(user_id, None)
        send_force_sub(message.chat.id)
        return

    state = ADMIN_STATE.get(user_id)
    if not state:
        return

    mode = state.get("mode")

    if mode in ["add_credit", "remove_credit"]:
        try:
            uid_str, amount_str = message.text.split()
            target_id = int(uid_str)
            amount = int(amount_str)
        except Exception:
            bot.reply_to(message, "Format galat hai. Example: 123456789 10")
            return

        if mode == "add_credit":
            add_credits(target_id, amount)
            bot.reply_to(message, f"{target_id} ko +{amount} credits de diye gaye.")
        else:
            remove_credits(target_id, amount)
            bot.reply_to(message, f"{target_id} se -{amount} credits hata diye gaye.")

        ADMIN_STATE.pop(user_id, None)

    elif mode == "broadcast":
        text = message.text
        cur.execute("SELECT user_id FROM users")
        all_users = cur.fetchall()
        success = 0
        for (uid,) in all_users:
            try:
                bot.send_message(uid, f"📢 Broadcast:\n\n{text}")
                success += 1
            except Exception:
                pass
        bot.reply_to(message, f"Broadcast complete. Sent to {success} users.")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "ban":
        try:
            target_id = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "User ID number me bhejein.")
            return
        set_ban_status(target_id, True)
        bot.reply_to(message, f"User {target_id} ko ban kar diya gaya.")
        ADMIN_STATE.pop(user_id, None)

    elif mode == "unban":
        try:
            target_id = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "User ID number me bhejein.")
            return
        set_ban_status(target_id, False)
        bot.reply_to(message, f"User {target_id} ko unban kar diya gaya.")
        ADMIN_STATE.pop(user_id, None)

# fallback
@bot.message_handler(func=lambda m: True)
def fallback(message):
    user_id = message.from_user.id

    if not is_user_in_channel(user_id):
        send_force_sub(message.chat.id)
        return

    ensure_user_record_from_obj(message.from_user)

    if message.text.startswith("/"):
        bot.reply_to(message, "Command samajh nahi aaya. `/start` use karein.")
    else:
        bot.reply_to(message, "🙂 Bot use karne ke liye `/start` type karein.")

# ================== RUN ==================
if __name__ == "__main__":
    print("Bot started...")
    bot.infinity_polling()
