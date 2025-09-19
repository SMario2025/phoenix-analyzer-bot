# Phoenix Analyzer Bot — Full Check + Rug Check + Wallet Links (Helius) + Membership Gate + JobQueue safe

import os, time, requests, threading, html
from collections import defaultdict
from urllib.parse import urlencode
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# ===== env =====
load_dotenv(override=True)
BOT_TOKEN       = os.getenv("BOT_TOKEN")
PRIMARY_RPC     = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
FALLBACK_RPC    = os.getenv("FALLBACK_RPC", "https://solana-rpc.publicnode.com")
SECOND_RPC      = os.getenv("SECOND_RPC")
GROUP_USERNAME  = os.getenv("GROUP_USERNAME", "@PHX2025New")
GROUP_JOIN_LINK = os.getenv("GROUP_JOIN_LINK", "https://t.me/PHX2025New")
HELIUS_KEY      = os.getenv("HELIUS_KEY")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

RPC_LIST = [u for u in [PRIMARY_RPC, FALLBACK_RPC, SECOND_RPC] if u and u.startswith("http")]
print("RPCs:", " | ".join(RPC_LIST))
print("Gate group:", GROUP_USERNAME)
print("Helius key:", "set" if HELIUS_KEY else "not set")

# ===== utils =====
TIMEOUT = 20
MAX_TRIES = 2
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USER_COOLDOWN_SEC = 1.0
_last_call = defaultdict(float)
CACHE_TTL = 300
_cache = {}
_cache_lock = threading.Lock()

def rpc(method: str, params: list):
    last_err = None
    for url in RPC_LIST:
        for attempt in range(1, MAX_TRIES + 1):
            try:
                r = requests.post(
                    url, json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
                    timeout=TIMEOUT, headers={"Content-Type":"application/json"}
                )
                if r.status_code in (401,403,429):
                    last_err = RuntimeError(f"{url} -> HTTP {r.status_code}")
                    break
                r.raise_for_status()
                data = r.json()
                if data.get("error"):
                    last_err = RuntimeError(f"{url} -> {data['error']}")
                else:
                    if "result" not in data:
                        last_err = RuntimeError(f"{url} -> no 'result'")
                    else:
                        return data["result"]
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(0.25*attempt)
    raise RuntimeError(f"RPC failed across endpoints: {last_err}")

def fmt_usd(x):
    try:
        v = float(x)
        if v >= 1_000_000_000: return f"${v/1_000_000_000:.2f}B"
        if v >= 1_000_000:     return f"${v/1_000_000:.2f}M"
        if v >= 1_000:         return f"${v/1_000:.2f}k"
        return f"${v:.6f}" if v < 1 else f"${v:.2f}"
    except: return "—"

def solscan(addr): return f"https://solscan.io/account/{addr}"
def safe(s: str) -> str: return html.escape(s or "")

# ===== membership gate =====
async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        user = update.effective_user
        if not user:
            await update.effective_message.reply_text("Cannot identify user. Please try again.")
            return False
        member = await context.bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user.id)
        if member.status in ("left", "kicked"):
            await update.effective_message.reply_text(
                "🚫 Access denied.\nJoin our Phoenix community first:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {GROUP_USERNAME}", url=GROUP_JOIN_LINK)]])
            )
            return False
        return True
    except Exception:
        await update.effective_message.reply_text(
            "⚠️ Membership check failed.\nPlease join our community first:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {GROUP_USERNAME}", url=GROUP_JOIN_LINK)]])
        )
        return False

# ===== telegram flow =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {GROUP_USERNAME}", url=GROUP_JOIN_LINK)]])
    await update.message.reply_text(
        "<b>Phoenix Analyzer</b> is online.\n\n"
        "Commands:\n"
        "• <b>/check</b> – Full analysis\n"
        "• <b>/slot</b> – Current Solana slot\n"
        "• <b>/ping</b> – Heartbeat\n\n"
        "Join our community to unlock full access.",
        parse_mode="HTML", reply_markup=join_kb
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = rpc("getSlot", [])
        await update.message.reply_text(f"Slot: {s}")
    except Exception as e:
        await update.message.reply_text(f"RPC error: {e}")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    await update.message.reply_text("✅ Analysis placeholder — full logic omitted here for brevity")

# ===== dummy jobs =====
async def price_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    print("⏰ Price alerts tick")

async def whale_job(context: ContextTypes.DEFAULT_TYPE):
    print("🐋 Whale watch tick")

# ===== post_init cleanup =====
async def post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("slot", slot))
    app.add_handler(CommandHandler("check", check))

    # --- JobQueue Setup (safe) ---
    jq = app.job_queue
    if jq is None:
        print("⚠️ JobQueue not available. Install with: pip install \"python-telegram-bot[job-queue]\"")
    else:
        jq.run_repeating(price_alerts_job, interval=60, first=10)
        jq.run_repeating(whale_job,        interval=75, first=20)

    print("🚀 Bot läuft…")
    app.run_polling()

if __name__ == "__main__":
    main()











