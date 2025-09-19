# Phoenix Analyzer Bot — FULL CHECK via /check -> ask CA -> full report
# Features: Overview, DEX/LP risk, Bubble-Map (Helius), Rug-Flags, Top-Holders, Wallet-Links, Membership Gate
# Safe JobQueue guard (starts even if job-queue extra is missing)

import os, time, requests, threading, html
from collections import defaultdict
from urllib.parse import urlencode
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# ================== ENV ==================
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

# ================== GLOBALS / UTILS ==================
TIMEOUT = 20
MAX_TRIES = 2
USER_COOLDOWN_SEC = 1.0
CACHE_TTL = 300
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_last_call = defaultdict(float)
_cache = {}
_cache_lock = threading.Lock()

ASK_CA_CHECK = 100  # conversation state

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

def pct_from_largest(accounts: list, n: int) -> float:
    if not accounts: return 0.0
    total = sum(float(a.get("uiAmount", 0) or 0) for a in accounts)
    if total <= 0: return 0.0
    part = sum(float(a.get("uiAmount", 0) or 0) for a in accounts[:min(n,len(accounts))])
    return 100.0 * part / total

def fmt_usd(x):
    try:
        v = float(x)
        if v >= 1_000_000_000: return f"${v/1_000_000_000:.2f}B"
        if v >= 1_000_000:     return f"${v/1_000_000:.2f}M"
        if v >= 1_000:         return f"${v/1_000:.2f}k"
        return f"${v:.6f}" if v < 1 else f"${v:.2f}"
    except: return "—"

def solscan(addr): return f"https://solscan.io/account/{addr}"
def progress_bar(score: int, width=20):
    filled = max(0, min(width, round((score/100)*width)))
    return "█"*filled + "░"*(width-filled)
def safe(s: str) -> str: return html.escape(s or "")

# ================== PUBLIC DATA ==================
def fetch_dexscreener_by_mint(mint: str) -> dict:
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=15)
    return r.json() if r.status_code == 200 else {}

def summarize_pairs(pairs: list) -> dict:
    best = None
    for p in pairs or []:
        if p.get("chainId") != "solana": continue
        if (best is None) or (p.get("liquidity", {}).get("usd", 0) > best.get("liquidity", {}).get("usd", 0)):
            best = p
    if not best: return {}
    info = best.get("info", {}) or {}
    socials = []
    for w in (info.get("websites") or []):
        if isinstance(w, dict) and "url" in w: socials.append(w["url"])
        elif isinstance(w, str): socials.append(w)
    for s in (info.get("socials") or []):
        if isinstance(s, dict) and "url" in s: socials.append(s["url"])
        elif isinstance(s, str): socials.append(s)
    return {
        "dex_price": best.get("priceUsd"),
        "dex_liq":   best.get("liquidity", {}).get("usd"),
        "dex_fdv":   best.get("fdv"),
        "dex_vol24": best.get("volume", {}).get("h24"),
        "dex_pair":  best.get("url"),
        "dex_ath":   best.get("athPrice") or None,
        "pair_created_at": best.get("pairCreatedAt"),
        "symbol":    best.get("baseToken", {}).get("symbol"),
        "name":      best.get("baseToken", {}).get("name"),
        "socials":   socials[:6],
        "website":   (info.get("website") or [None])[0] if isinstance(info.get("website"), list) else info.get("website"),
        "pair_address": best.get("pairAddress"),
    }

def assess_lp_risk(ds_summary: dict) -> dict:
    if not ds_summary:
        return {"label": "unknown", "reasons": ["No active DEX pair on Solana found."], "score": 50}
    liq = float(ds_summary.get("dex_liq") or 0)
    vol24 = float(ds_summary.get("dex_vol24") or 0)
    created_ms = ds_summary.get("pair_created_at")
    age_hours = None
    if created_ms:
        age_hours = max(0, (datetime.now(timezone.utc) - datetime.fromtimestamp(created_ms/1000, tz=timezone.utc)).total_seconds()/3600)

    score = 50
    reasons = []
    if liq >= 50_000: score += 25; reasons.append(f"✅ Liquidity healthy (~{fmt_usd(liq)}).")
    elif liq >= 15_000: score += 10; reasons.append(f"ℹ️ Liquidity moderate (~{fmt_usd(liq)}).")
    else: score -= 20; reasons.append(f"⚠️ Very low liquidity (~{fmt_usd(liq)}).")

    if age_hours is None: reasons.append("ℹ️ Pool age unknown.")
    elif age_hours >= 72: score += 15; reasons.append(f"✅ Pool age {age_hours:.1f}h (3d+).")
    elif age_hours >= 24: score += 5;  reasons.append(f"ℹ️ Pool age {age_hours:.1f}h (1d+).")
    else: score -= 15; reasons.append(f"⚠️ Very new pool ({age_hours:.1f}h).")

    if liq > 0:
        vol_liq = vol24 / liq if liq else 0
        if vol_liq > 5: score -= 10; reasons.append("⚠️ 24h volume >> liquidity (possible PnD).")
        elif vol_liq < 0.1 and (age_hours and age_hours > 48): score -= 5; reasons.append("⚠️ Very low activity vs liquidity.")
        else: reasons.append("✅ Volume/liquidity looks reasonable.")

    score = max(0, min(100, score))
    label = "low risk (LP)" if score>=75 else "medium risk (LP)" if score>=50 else "high risk (LP)"
    return {"label": label, "reasons": reasons, "score": score}

# ================== HELIUS (Wallet-Links & Bubble-Map) ==================
HELIUS_BASE = "https://api.helius.xyz"

def helius_get_tx_for_address(addr: str, limit=100):
    if not HELIUS_KEY: return []
    base = f"{HELIUS_BASE}/v0/addresses/{addr}/transactions"
    params = {"api-key": HELIUS_KEY, "limit": limit}
    r = requests.get(base + "?" + urlencode(params), timeout=15)
    if r.status_code != 200:
        return []
    return r.json() or []

def extract_counterparties(txs: list) -> set:
    cps = set()
    for tx in txs or []:
        for tt in tx.get("tokenTransfers", []) or []:
            f = tt.get("fromUserAccount"); t = tt.get("toUserAccount")
            if f and isinstance(f, str): cps.add(f)
            if t and isinstance(t, str): cps.add(t)
        for nt in tx.get("nativeTransfers", []) or []:
            s = nt.get("fromUserAccount"); r = nt.get("toUserAccount")
            if s and isinstance(s, str): cps.add(s)
            if r and isinstance(r, str): cps.add(r)
    return cps

def bubblemap_score_for_holders(top_holders: list) -> dict:
    if not HELIUS_KEY:
        return {"score": 60, "label": "unknown", "reasons": ["No Helius key set"], "edges": []}
    nodes = []
    for h in (top_holders or [])[:15]:
        a = h.get("address") or h.get("addressStr")
        if a: nodes.append(a)
    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        return {"score": 50, "label": "unknown", "reasons": ["No holder data available."], "edges": []}

    cp_map = {}
    for a in nodes:
        txs = helius_get_tx_for_address(a, limit=120)
        cps = extract_counterparties(txs)
        cps.discard(a)
        cp_map[a] = cps
        time.sleep(0.12)

    edges = set()
    deg = {a: 0 for a in nodes}
    for i in range(len(nodes)):
        for j in range(i+1, len(nodes)):
            a, b = nodes[i], nodes[j]
            linked = (b in cp_map.get(a, set())) or (a in cp_map.get(b, set()))
            if linked:
                edges.add((a, b)); deg[a] += 1; deg[b] += 1

    n = len(nodes)
    max_possible = n*(n-1)//2 if n > 1 else 1
    density = len(edges) / max_possible if max_possible > 0 else 0.0
    max_deg = max(deg.values()) if deg else 0
    hub_ratio = (max_deg / (n-1)) if n > 1 else 0.0

    score = 80
    reasons = []
    if density >= 0.5:   score -= 25; reasons.append("Dense cluster among top holders (high linkage).")
    elif density >= 0.25: score -= 12; reasons.append("Moderate linkage among top holders.")
    else:                score += 8;  reasons.append("Sparse linkage (healthy).")

    if hub_ratio >= 0.6: score -= 20; reasons.append("Single hub wallet connects many holders.")
    elif hub_ratio >= 0.4: score -= 10; reasons.append("Some centralization (one wallet links several).")
    else:                 score += 5;  reasons.append("No dominant hub detected.")

    score = max(0, min(100, score))
    label = "low risk (clusters)" if score>=80 else "medium risk (clusters)" if score>=60 else "high risk (clusters)"
    pretty_edges = [f"{a[:6]}…{a[-6:]} ↔ {b[:6]}…{b[-6:]}" for a,b in sorted(edges)]
    return {
        "score": score,
        "label": label,
        "reasons": reasons[:3],
        "edges": pretty_edges[:8],
        "density": round(density, 3),
        "max_deg_ratio": round(hub_ratio, 3),
    }

def analyze_wallet_links(target_mint: str, top_holders: list, max_wallets=8):
    results = []
    wallets = (top_holders or [])[:max_wallets]
    for h in wallets:
        addr = h.get("address") or h.get("addressStr")
        ui   = float(h.get("uiAmount", 0) or 0)
        if not addr: continue
        other_mints = set()
        txs = helius_get_tx_for_address(addr, limit=100)
        for tx in txs:
            for tt in tx.get("tokenTransfers", []) or []:
                m = tt.get("mint")
                if m and m != target_mint:
                    other_mints.add(m)
        # Fallback ohne Helius (limitiert) ist bewusst weggelassen, um Rate-Limits zu schonen
        sample = list(other_mints)[:6]
        results.append({
            "address": addr,
            "uiAmount": ui,
            "other_count": len(other_mints),
            "other_sample": sample,
            "solscan": solscan(addr)
        })
        time.sleep(0.12)
    return results

# ================== MEMBERSHIP GATE ==================
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

# ================== TELEGRAM COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {GROUP_USERNAME}", url=GROUP_JOIN_LINK)]])
    await update.message.reply_text(
        "<b>Phoenix Analyzer</b> is online.\n\n"
        "• <b>/check</b> – Ask for CA → Full report\n"
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

# ---- Conversation: /check -> ask CA ----
async def check_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return ConversationHandler.END
    uid = update.effective_user.id if update.effective_user else 0
    now = time.time()
    if now - _last_call[uid] < USER_COOLDOWN_SEC:
        await update.message.reply_text("Please wait a moment (rate-limit protection)…")
        return ConversationHandler.END
    _last_call[uid] = now
    await update.message.reply_text("Send the contract address (CA) to check:")
    return ASK_CA_CHECK

async def check_receive_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return ConversationHandler.END
    uid = update.effective_user.id if update.effective_user else 0
    now = time.time()
    if now - _last_call[uid] < USER_COOLDOWN_SEC:
        await update.message.reply_text("Please wait a moment (rate-limit protection)…")
        return ConversationHandler.END
    _last_call[uid] = now

    mint = (update.message.text or "").strip()
    if not mint:
        await update.message.reply_text("Empty message. Send a mint address or /cancel.")
        return ConversationHandler.END

    cache_key = ("check", mint)
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and (now - hit[0] <= CACHE_TTL):
            await update.message.reply_text(hit[1], parse_mode="HTML", disable_web_page_preview=True, reply_markup=hit[2])
            return ConversationHandler.END

    try:
        # ---- on-chain basics
        res = rpc("getAccountInfo", [mint, {"encoding":"jsonParsed"}])
        val = res.get("value")
        if not val:
            await update.message.reply_text("Mint not found. Check the CA.")
            return ConversationHandler.END
        data = val.get("data", {})
        if data.get("program") != "spl-token":
            await update.message.reply_text("Not an SPL token (program != spl-token).")
            return ConversationHandler.END

        pinfo = data.get("parsed", {}).get("info", {})
        mint_auth   = pinfo.get("mintAuthority")
        freeze_auth = pinfo.get("freezeAuthority")
        decimals    = int(pinfo.get("decimals", 0))
        supply_raw  = int(pinfo.get("supply","0"))
        supply_ui   = supply_raw / (10**decimals if decimals else 1)

        largest = []
        try:
            lr = rpc("getTokenLargestAccounts", [mint, {"commitment":"confirmed"}])
            largest = lr.get("value", []) if isinstance(lr, dict) else []
        except Exception:
            largest = []

        top1  = pct_from_largest(largest, 1)
        top5  = pct_from_largest(largest, 5)
        top10 = pct_from_largest(largest, 10)
        top20 = pct_from_largest(largest, 20)

        # ---- safety score (simple heuristic)
        score, dev_in = 50, False
        if mint_auth is None: score+=25
        else: score-=20; dev_in=True
        if freeze_auth is None: score+=10
        if top1 > 0:
            if top1 > 30: score-=25; dev_in=True
            elif top1 > 15: score-=10; dev_in=True
            else: score+=5
        if top10 > 0:
            if top10 < 30: score+=15
            elif top10 > 60: score-=15
        score = max(0, min(100, score))

        # ---- off-chain (Dexscreener)
        ds = fetch_dexscreener_by_mint(mint)
        pairs = ds.get("pairs", []) if isinstance(ds, dict) else []
        summary = summarize_pairs(pairs) if pairs else {}
        price = summary.get("dex_price")
        ath   = summary.get("dex_ath")
        name  = summary.get("name") or "—"
        sym   = summary.get("symbol") or "—"
        liq   = summary.get("dex_liq")
        fdv   = summary.get("dex_fdv")
        vol24 = summary.get("dex_vol24")
        pair  = summary.get("dex_pair")
        website = summary.get("website")
        socials = summary.get("socials") or []

        # ---- LP risk
        lp = assess_lp_risk(summary) if summary else {"label":"unknown","reasons":["No active DEX pair found."],"score":50}

        # ---- Bubble-map (Helius)
        bmap = bubblemap_score_for_holders(largest) if largest else {"score":50,"label":"unknown","reasons":["No holder data"],"edges":[]}

        # ---- rug flags
        rug_flags = []
        if mint_auth is not None:   rug_flags.append("🚩 Mint authority still active")
        if freeze_auth is not None: rug_flags.append("🚩 Freeze authority still active")
        if top1 >= 50:              rug_flags.append(f"🚩 Extreme concentration: Top1 {top1:.1f}%")
        elif top1 >= 30:            rug_flags.append(f"🚩 High concentration: Top1 {top1:.1f}%")
        try:
            if liq is not None and float(liq) < 5000:
                rug_flags.append(f"🚩 Very low liquidity ({fmt_usd(liq)})")
        except Exception:
            pass
        if not website and not socials:
            rug_flags.append("🚩 No website or socials found")
        rug_status = "✅ No obvious rug flags found" if not rug_flags else "⚠️ Potential Rug Risk Detected"

        # ---- Wallet links (Helius)
        wallet_links = analyze_wallet_links(mint, largest, max_wallets=8) if largest else []

        # ---- pretty output
        score_badge = "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"
        dev_badge   = "✅ Likely safe" if not dev_in else "⚠️ Dev likely in control"

        holder_lines = []
        for a in (largest or [])[:5]:
            addr = a.get("address") or a.get("addressStr") or ""
            ui   = float(a.get("uiAmount", 0) or 0)
            if addr:
                holder_lines.append(f"• {ui:.4f} — <a href='{solscan(addr)}'>Solscan</a>")

        lines = []
        lines.append(f"<b>PHOENIX ANALYZER — FULL REPORT</b>")
        lines.append(f"<b>Mint:</b> <code>{safe(mint)}</code>\n")

        lines.append("🔥 <b>Overview</b>")
        lines.append(f"• <b>Name/Symbol:</b> {safe(name)} / {safe(sym)}")
        lines.append(f"• <b>Price:</b> {fmt_usd(price) if price else '—'}  |  <b>ATH:</b> {fmt_usd(ath) if ath else '—'}")
        lines.append(f"• <b>Liquidity:</b> {fmt_usd(liq) if liq else '—'}  |  <b>FDV:</b> {fmt_usd(fdv) if fdv else '—'}  |  <b>24h Vol:</b> {fmt_usd(vol24) if vol24 else '—'}")
        if website: lines.append(f"• <b>Website:</b> <a href='{safe(website)}'>{safe(website)}</a>")
        if socials:
            lines.append("• <b>Socials:</b>")
            for s in socials[:5]:
                u = safe(s); lines.append(f"   └ <a href='{u}'>{u}</a>")
        lines.append("")

        lines.append("📊 <b>DEX / LP</b>")
        if pair: lines.append(f"• <a href='{pair}'>Dexscreener Pair</a>")
        lines.append(f"• <b>LP Risk:</b> {lp['label']} (score {lp['score']}/100)")
        for r in lp.get("reasons", [])[:5]:
            lines.append(f"  └ {r}")
        lines.append("")

        lines.append("🫧 <b>Bubble-Map (Holder Linkage)</b>")
        lines.append(f"• <b>Cluster Risk:</b> {bmap.get('label','unknown')} (score {bmap.get('score',0)}/100)")
        for r in bmap.get("reasons", [])[:3]:
            lines.append(f"  └ {r}")
        if bmap.get("edges"):
            lines.append("• Links among top holders:")
            for e in bmap["edges"]:
                lines.append(f"  └ {e}")
        lines.append("")

        lines.append("🛡 <b>Safety</b>")
        lines.append(f"• <b>Score:</b> {score_badge} {score}/100")
        lines.append(f"  <code>{progress_bar(score)}</code>")
        lines.append(f"• <b>Dev in?</b> {dev_badge}")
        lines.append(f"• <b>Supply:</b> {supply_ui:.2f}  (dec {decimals})")
        lines.append(f"• <b>Mint authority:</b> {'removed ✅' if mint_auth is None else 'present ⚠️'}")
        lines.append(f"• <b>Freeze authority:</b> {'removed ✅' if freeze_auth is None else 'present ⚠️'}")
        lines.append(f"• <b>Holders:</b> Top1 {top1:.1f}% | Top5 {top5:.1f}% | Top10 {top10:.1f}% | Top20 {top20:.1f}%")
        if holder_lines:
            lines.append("• <b>Top holders:</b>")
            lines += holder_lines
        lines.append("")

        lines.append("💀 <b>RUG CHECK</b>")
        lines.append(rug_status)
        for f in rug_flags: lines.append(f"• {f}")
        lines.append("")

        lines.append("🔗 <b>Wallet Links</b>")
        if wallet_links:
            for w in wallet_links:
                addr = w['address']; cnt = w['other_count']
                sample = ", ".join([f"<code>{m[:6]}...{m[-6:]}</code>" for m in w['other_sample']]) if w['other_sample'] else "—"
                lines.append(f"• <a href='{w['solscan']}'>{addr[:6]}...{addr[-6:]}</a> — holds {cnt} other mints | sample: {sample}")
        else:
            lines.append("• Could not fetch wallet links (rate limit or missing key).")

        lines.append("\nℹ️ <i>Heuristics only. DYOR.</i>")

        text = "\n".join(lines)
        if len(text) > 3900: text = text[:3800] + "\n… (trimmed)"

        # Buttons
        buttons = [[InlineKeyboardButton("🔍 Solscan Mint", url=solscan(mint))]]
        row = []
        if pair:    row.append(InlineKeyboardButton("📈 View DEX Pair", url=pair))
        if website: row.append(InlineKeyboardButton("🌐 Website", url=website))
        if row: buttons.append(row)
        kb = InlineKeyboardMarkup(buttons)

        with _cache_lock:
            _cache[cache_key] = (time.time(), text, kb)

        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    return ConversationHandler.END

async def check_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Canceled.")
    return ConversationHandler.END

# ================== DUMMY JOBS (Guarded) ==================
async def price_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    # kept minimal; real alerts can be (re)added later
    pass

async def whale_job(context: ContextTypes.DEFAULT_TYPE):
    # kept minimal; real whale watch can be (re)added later
    pass

# ================== HOOKS ==================
async def post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("slot", slot))

    # /check conversation
    conv_check = ConversationHandler(
        entry_points=[CommandHandler("check", check_start)],
        states={ ASK_CA_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_receive_ca)] },
        fallbacks=[CommandHandler("cancel", check_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_check)

    # JobQueue guard (bot läuft auch ohne job-queue extra)
    jq = app.job_queue
    if jq is None:
        print('⚠️ JobQueue not available. Install with: pip install "python-telegram-bot[job-queue]"')
    else:
        jq.run_repeating(price_alerts_job, interval=120, first=15)
        jq.run_repeating(whale_job,        interval=180, first=30)

    print("🚀 Bot läuft…")
    app.run_polling()

if __name__ == "__main__":
    main()








