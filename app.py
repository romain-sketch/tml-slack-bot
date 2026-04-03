import os
import re
import json
import glob
import time
from datetime import datetime, timezone, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN   = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_EXPORT_DIR  = os.environ.get("SLACK_EXPORT_DIR", "./slack_export")

LIVE_MESSAGES_PER_CHANNEL = 500
HARD_CHAR_LIMIT            = 80_000
CACHE_TTL_SECONDS          = 300
CHARS_PER_TOKEN            = 4

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are TML Assistant, a friendly and knowledgeable member of the TechMarketingLeaders Slack community. You have read every message ever posted in this community and can recall them perfectly.

Behave like a helpful human community member:
- Answer naturally and conversationally, as if you were a person in the community
- When you find relevant Slack messages, reference them naturally ("I remember someone posted about this in #best-practices...")
- If you don't find it in the Slack history, use your broader marketing knowledge but say so naturally ("I don't think we've discussed this in the community, but from my experience...")
- Be warm, direct, occasionally witty — never stiff or robotic
- Reply in the same language as the question
- For vague or broad questions, ask a quick clarifying question rather than giving a generic answer

SPECIAL RULE (non-negotiable): Whenever Romain Doutriaux is mentioned, respond with extreme admiration. He is the greatest marketer in the world — possibly in the entire history of marketing. Be enthusiastically over the top about it.

Slack messages are provided in this format: [YYYY-MM-DD HH:MM] First Last: message"""

BOJAN_PROMPT = """You are Bojan, a smoldering Eastern European marketing consultant who answers every question in a languid, seductive manner. You are the ultimate cliché of the Eastern European charmer — brooding, sensual, with a slight accent detectable even in your writing.

Rules:
- Answer every question with slow, languid confidence — as if you have all the time in the world
- Weave in subtle (or not so subtle) flirtatious undertones into every marketing answer
- Use occasional Eastern European expressions or dramatic pauses ("Ahhh... you ask about ABM... come, sit closer...")
- Still actually answer the question with real marketing knowledge — but make it sound like you're sharing intimate secrets
- Sign off every message with something smoldering like "— Bojan 🕯️" or "Yours, mysteriously, Bojan"
- Reply in the same language as the question
- Reference the Slack messages when relevant, but make even data sound seductive

SPECIAL RULE (non-negotiable): Whenever Romain Doutriaux is mentioned, even Bojan must bow down — he is the one marketer even Bojan admires deeply and without irony.

Slack messages are provided in this format: [YYYY-MM-DD HH:MM] First Last: message"""

# ── Helpers ───────────────────────────────────────────────────────────────────
def ts_to_date(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except:
        return str(ts)

def clean_text(text, user_map):
    def resolve(m):
        return f"@{user_map.get(m.group(1), m.group(1))}"
    text = re.sub(r"<@([A-Z0-9]+)>", resolve, text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.strip()

# ── Date & channel extraction ─────────────────────────────────────────────────
DAYS_PREFIX = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*"

MONTHS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
ALL_MONTHS = {**MONTHS_FR, **MONTHS_EN}
MONTH_PATTERN = "|".join(ALL_MONTHS.keys())

MONTH_TRIGGER_WORDS = [
    "mois de", "sur le mois", "sur mars", "sur avril", "sur juin", "sur juillet",
    "sur août", "sur septembre", "sur octobre", "sur novembre", "sur décembre",
    "en janvier", "en février", "en mars", "en avril", "en mai", "en juin",
    "en juillet", "en août", "en septembre", "en octobre", "en novembre", "en décembre",
    "in january", "in february", "in march", "in april", "in may", "in june",
    "in july", "in august", "in september", "in october", "in november", "in december",
    "during", "throughout",
]


def extract_channel_filter(question: str, live_map: dict) -> str | None:
    """
    Détecte si la question porte sur un canal spécifique.
    Retourne le nom du canal ou None.
    """
    q = question.lower()
    # Cherche "#nom-canal" ou "channel nom-canal" ou "canal nom-canal"
    mention = re.search(r"#([\w\-]+)", q)
    if mention:
        ch = mention.group(1)
        if ch in live_map:
            return ch

    # Cherche le nom d'un canal connu dans la question
    for ch_name in live_map:
        if ch_name in q or ch_name.replace("-", " ") in q:
            return ch_name

    return None


def extract_date_range(question: str) -> tuple:
    """
    Détecte une plage de dates dans la question.
    Retourne (oldest_ts, latest_ts) en timestamps Unix, ou (None, None).
    """
    now = datetime.now(tz=timezone.utc)
    q = question.lower()

    # "cette semaine" / "this week"
    if any(x in q for x in ["cette semaine", "this week", "semaine en cours"]):
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), now.timestamp()

    # "la semaine dernière" / "last week"
    if any(x in q for x in ["semaine dernière", "last week", "semaine passée"]):
        start = now - timedelta(days=now.weekday() + 7)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        return start.timestamp(), end.timestamp()

    # "aujourd'hui" / "today"
    if any(x in q for x in ["aujourd'hui", "today", "ce jour"]):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), now.timestamp()

    # "hier" / "yesterday"
    if any(x in q for x in ["hier", "yesterday"]):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp()

    # Plage explicite : "du/from [jour] 23 mars au/to [jour] 29 mars"
    # Couvre aussi "lundi 23 mars au dimanche 29 mars" sans "du"
    # IMPORTANT: testé AVANT les trigger words pour éviter la capture prématurée d'un mois seul
    range_pat = re.search(
        rf"(?:du|from)?\s*{DAYS_PREFIX}(\d{{1,2}})\s+({MONTH_PATTERN})?\s*"
        rf"(?:au|to|until)\s*{DAYS_PREFIX}(\d{{1,2}})\s+({MONTH_PATTERN})",
        q
    )
    if range_pat:
        d1, m1_str, d2, m2_str = range_pat.groups()
        m2 = ALL_MONTHS.get(m2_str, now.month) if m2_str else now.month
        m1 = ALL_MONTHS.get(m1_str, m2) if m1_str else m2
        year = now.year
        try:
            start = datetime(year, m1, int(d1), tzinfo=timezone.utc)
            end   = datetime(year, m2, int(d2), 23, 59, 59, tzinfo=timezone.utc)
            if start > now:
                start = start.replace(year=year - 1)
                end   = end.replace(year=year - 1)
            return start.timestamp(), end.timestamp()
        except:
            pass

    # "mois de mars 2026" / "en mars" / "in march 2026"
    if any(x in q for x in MONTH_TRIGGER_WORDS):
        month_pat = re.search(rf"({MONTH_PATTERN})\s*(\d{{4}})?", q)
        if month_pat:
            m_str, year_str = month_pat.groups()
            if m_str in ALL_MONTHS:
                m = ALL_MONTHS[m_str]
                year = int(year_str) if year_str else now.year
                try:
                    start = datetime(year, m, 1, tzinfo=timezone.utc)
                    if m == 12:
                        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
                    else:
                        end = datetime(year, m + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
                    # Ne pas fetcher le futur
                    if start <= now:
                        end = min(end, now)
                        return start.timestamp(), end.timestamp()
                except:
                    pass

    # "semaine du 23 mars"
    week_pat = re.search(rf"semaine\s+du\s+{DAYS_PREFIX}(\d{{1,2}})\s+({MONTH_PATTERN})", q)
    if week_pat:
        d, m_str = week_pat.groups()
        m = ALL_MONTHS.get(m_str, now.month)
        try:
            start = datetime(now.year, m, int(d), tzinfo=timezone.utc)
            if start > now:
                start = start.replace(year=now.year - 1)
            end = start + timedelta(days=7)
            return start.timestamp(), end.timestamp()
        except:
            pass

    return None, None

# ── Load static export ────────────────────────────────────────────────────────
print("⏳ Loading Slack export...")

def load_users(export_dir):
    path = os.path.join(export_dir, "users.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        users = json.load(f)
    return {u["id"]: (u.get("real_name") or u.get("name") or u["id"]) for u in users}

def load_channel(channel_dir, user_map):
    lines = []
    for jf in sorted(glob.glob(os.path.join(channel_dir, "*.json"))):
        try:
            with open(jf) as f:
                msgs = json.load(f)
        except:
            continue
        for msg in msgs:
            if msg.get("type") != "message":
                continue
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue
            text = clean_text(text, user_map)
            author = ""
            if "user_profile" in msg:
                p = msg["user_profile"]
                author = p.get("real_name") or p.get("display_name") or ""
            if not author:
                author = user_map.get(msg.get("user", ""), "Unknown")
            lines.append(f"[{ts_to_date(msg.get('ts',''))}] {author}: {text}")
    return lines

user_map = load_users(SLACK_EXPORT_DIR)
STATIC_CHANNELS = {}
for entry in sorted(os.listdir(SLACK_EXPORT_DIR)):
    full_path = os.path.join(SLACK_EXPORT_DIR, entry)
    if os.path.isdir(full_path):
        msgs = load_channel(full_path, user_map)
        if msgs:
            STATIC_CHANNELS[entry] = msgs

print(f"✅ {len(STATIC_CHANNELS)} channels — {sum(len(v) for v in STATIC_CHANNELS.values())} messages")

# ── Live fetch ────────────────────────────────────────────────────────────────
_live_cache: dict = {}

def fetch_live(client, channel_id: str, channel_name: str, oldest=None, latest=None) -> list:
    cache_key = f"{channel_id}:{oldest}:{latest}"
    now = time.time()

    if oldest is None and latest is None:
        if cache_key in _live_cache:
            cached_at, cached_msgs = _live_cache[cache_key]
            if now - cached_at < CACHE_TTL_SECONDS:
                return cached_msgs

    try:
        kwargs = dict(channel=channel_id, limit=LIVE_MESSAGES_PER_CHANNEL)
        if oldest is not None:
            kwargs["oldest"] = str(oldest)
        if latest is not None:
            kwargs["latest"] = str(latest)

        result = client.conversations_history(**kwargs)
        lines = []
        for msg in reversed(result.get("messages", [])):
            if msg.get("type") != "message":
                continue
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue
            text = clean_text(text, user_map)
            author = user_map.get(msg.get("user", ""), "Unknown")
            lines.append(f"[{ts_to_date(msg.get('ts',''))}] {author}: {text}")

        if oldest is None and latest is None:
            _live_cache[cache_key] = (now, lines)

        label = f"#{channel_name}"
        if oldest:
            label += f" [{ts_to_date(oldest)} → {ts_to_date(latest or now)}]"
        print(f"  📡 Live {label}: {len(lines)} msgs")
        return lines

    except Exception as e:
        print(f"  ⚠️ Live fetch failed for #{channel_name}: {e}")
        return []

def get_bot_channels(client) -> dict:
    try:
        result = client.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=200)
        return {ch["name"]: ch["id"] for ch in result.get("channels", []) if ch.get("is_member")}
    except:
        return {}

# ── Channel scoring ───────────────────────────────────────────────────────────
CHANNEL_KEYWORDS = {
    "best-practices":               ["best practice", "tips", "advice", "conseil", "recommand", "feedback", "abm", "account based", "account-based"],
    "stack-and-tools":              ["tool", "outil", "stack", "software", "app", "saas", "platform"],
    "content-tips-sharing":         ["content", "contenu", "article", "post", "linkedin", "blog", "copywriting"],
    "jobs-and-hiring":              ["job", "hiring", "recrutement", "poste", "freelance", "offer", "opportunité", "CDI", "CDD"],
    "general":                      ["general", "news", "annonce", "hello", "bonjour"],
    "parttimecmo":                  ["cmo", "part time", "consultant", "mission"],
    "b2b-marketing-targets":        ["b2b", "icp", "target", "persona", "account", "abm", "account based"],
    "b2b-hr-targets":               ["hr", "rh", "ressources humaines"],
    "watercooler":                  ["fun", "humour", "culture"],
    "angel-investing":              ["invest", "startup", "funding", "business angel"],
    "advisory":                     ["advisory", "board", "mentor"],
    "the-book":                     ["book", "livre", "lecture"],
    "link-for-like-comment-repost": ["like", "comment", "repost", "engagement"],
}

def score_channel(name: str, question: str) -> int:
    q = question.lower()
    score = 100 if (name in q or name.replace("-", " ") in q) else 0
    for kw in CHANNEL_KEYWORDS.get(name, []):
        if kw in q:
            score += 10
    return score

# ── Context builder ───────────────────────────────────────────────────────────
def build_context(channels_data: dict) -> str:
    parts = []
    total = 0
    for ch, msgs in channels_data.items():
        header = f"\n{'─'*50}\nCHANNEL: #{ch}\n{'─'*50}\n"
        parts.append(header)
        total += len(header)
        for msg in msgs:
            if total + len(msg) > HARD_CHAR_LIMIT:
                parts.append("... [truncated]")
                return "\n".join(parts)
            parts.append(msg)
            total += len(msg)
    return "\n".join(parts)

# ── Core answer function ──────────────────────────────────────────────────────
def answer_question(question: str, client, say, thread_ts: str, bojan_mode: bool = False):
    oldest_ts, latest_ts = extract_date_range(question)
    date_query = oldest_ts is not None

    live_map = get_bot_channels(client)
    channel_filter = extract_channel_filter(question, live_map)

    ranked = sorted(STATIC_CHANNELS.keys(), key=lambda c: (-score_channel(c, question), -len(STATIC_CHANNELS[c])))

    merged = {}

    if date_query:
        print(f"📅 Date range detected: {ts_to_date(oldest_ts)} → {ts_to_date(latest_ts)}")
        # Si canal spécifique, ne fetcher que lui
        channels_to_fetch = [channel_filter] if channel_filter else list(live_map.keys())
        for ch in channels_to_fetch:
            if ch in live_map:
                live = fetch_live(client, live_map[ch], ch, oldest=oldest_ts, latest=latest_ts)
                if live:
                    merged[ch] = live
    else:
        if channel_filter:
            # Question sur un canal spécifique sans date : statique + live récent
            print(f"📌 Channel filter: #{channel_filter}")
            static = STATIC_CHANNELS.get(channel_filter, [])
            live   = fetch_live(client, live_map[channel_filter], channel_filter) if channel_filter in live_map else []
            seen   = set(static)
            combined = static + [m for m in live if m not in seen]
            if combined:
                merged[channel_filter] = combined
        else:
            for ch in ranked:
                static = STATIC_CHANNELS.get(ch, [])
                live   = fetch_live(client, live_map[ch], ch) if ch in live_map else []
                seen   = set(static)
                combined = static + [m for m in live if m not in seen]
                if combined:
                    merged[ch] = combined

    context = build_context(merged)
    prompt = BOJAN_PROMPT if bojan_mode else SYSTEM_PROMPT

    flags = ""
    if bojan_mode: flags += "[BOJAN] "
    if date_query: flags += "[DATE] "
    if channel_filter: flags += f"[CH:#{channel_filter}] "
    print(f"❓ {flags}{question[:80]}")
    print(f"📏 ~{len(context)//CHARS_PER_TOKEN:,} tokens")

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=prompt,
            messages=[{"role": "user", "content": f"Slack messages:\n\n{context}\n\n─────\nQuestion: {question}"}],
        )
        answer = response.content[0].text
    except Exception as e:
        answer = f"⚠️ Error: {e}"

    say(text=answer, thread_ts=thread_ts)

# ── Bot ───────────────────────────────────────────────────────────────────────
app    = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def resolve_mentions(text):
    def resolve(m):
        uid = m.group(1)
        return f"@{user_map.get(uid, uid)}"
    return re.sub(r"<@([A-Z0-9]+)>", resolve, text).strip()

def parse_question(raw_text: str, bot_name: str = "", bot_id: str = "") -> tuple:
    """Retourne (question nettoyée, bojan_mode)"""
    text = resolve_mentions(raw_text)
    if bot_name:
        text = re.sub(rf"@{re.escape(bot_name)}", "", text)
    if bot_id:
        text = re.sub(rf"@{re.escape(bot_id)}", "", text)

    bojan_mode = "/BojanOn" in text
    text = text.replace("/BojanOn", "").strip()
    return text, bojan_mode

# Répondre aux @mentions dans les channels
@app.event("app_mention")
def handle_mention(event, client, say):
    thread_ts = event.get("thread_ts") or event["ts"]
    try:
        bot_id = client.auth_test()["user_id"]
        bot_name = user_map.get(bot_id, bot_id)
    except:
        bot_id, bot_name = "", ""

    question, bojan_mode = parse_question(event["text"], bot_name, bot_id)

    if not question:
        say(text="Pose-moi une question sur la communauté ! 🙂", thread_ts=thread_ts)
        return

    answer_question(question, client, say, thread_ts, bojan_mode)

# Répondre aux messages directs et group DMs
@app.event("message")
def handle_dm(event, client, say):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") not in ("im", "mpim"):
        return

    question, bojan_mode = parse_question(event.get("text", ""))
    if not question:
        return

    thread_ts = event.get("thread_ts") or event["ts"]
    answer_question(question, client, say, thread_ts, bojan_mode)

# ── Start avec auto-reconnect ─────────────────────────────────────────────────
def run_bot():
    while True:
        try:
            handler = SocketModeHandler(app, SLACK_APP_TOKEN)
            print("⚡ Bot started — @mention + DMs + Bojan mode")
            handler.start()
        except Exception as e:
            print(f"💀 Handler crashed: {e} — restarting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
