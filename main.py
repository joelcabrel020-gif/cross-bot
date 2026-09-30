"""
Bot de Cross Telegram : fichier unique.
Ce fichier contient tout le bot. Il se lance avec : python main.py
"""
import sys
import types

_SOURCES = {}
_SOURCES['config'] = r'''import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///crossbot.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

MIN_MEMBERS = int(os.getenv("MIN_MEMBERS", "1000"))  # 1K minimum (les admins sont exemptés)
MAX_CHANNELS = min(int(os.getenv("MAX_CHANNELS", "60")), 85)  # limite Telegram : 100 boutons
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "3"))
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "false").lower() == "true"
BUTTONS_PER_ROW = max(1, min(int(os.getenv("BUTTONS_PER_ROW", "2")), 4))
TIMEZONE = os.getenv("TIMEZONE", "Africa/Douala")
DEFAULT_TIMES = os.getenv("POST_TIMES", "10:00,18:00")          # modifiable ensuite dans le bot
DEFAULT_DURATION = float(os.getenv("DELETE_AFTER_HOURS", "6"))  # modifiable ensuite dans le bot
SPONSOR_EVERY = max(1, int(os.getenv("SPONSOR_EVERY", "10")))    # 1 sponsor tous les X canaux
BUTTON_NAME_MAX = max(8, int(os.getenv("BUTTON_NAME_MAX", "16")))  # nom coupé au-delà, pour que la pastille de fin reste visible
BUTTON_ICONS = [i.strip() for i in os.getenv("BUTTON_ICONS", "⭐,✨,🌟,💫").split(",") if i.strip()]
PORT = int(os.getenv("PORT", "10000"))

DEFAULT_HEADER = (
    "🔥 <b>CROSS PREMIUM</b> 🔥\n"
    "━━━━━━━━━━━━━━━\n\n"
    "<blockquote>Les meilleurs canaux Telegram réunis au même endroit.\n"
    "Abonne-toi à tous les canaux ci-dessous 👇</blockquote>\n\n"
    "📊 <b>{count}</b> canaux partenaires\n"
    "➕ <i>Ton canal peut rejoindre la cross gratuitement avec le bouton en bas.</i>"
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
'''
_SOURCES['db'] = r'''import time

from sqlalchemy import create_engine, inspect, text

import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS channels (
        id BIGINT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        title TEXT NOT NULL,
        link TEXT NOT NULL,
        members INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        cross_msg_id BIGINT,
        publish INTEGER NOT NULL DEFAULT 1,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS extras (id BIGINT PRIMARY KEY, label TEXT NOT NULL, link TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS users (id BIGINT PRIMARY KEY, first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
]


def _q(sql, **p):
    with engine.begin() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]


def _x(sql, **p):
    with engine.begin() as c:
        c.execute(text(sql), p)


def init():
    for s in SCHEMA:
        _x(s)
    # anciennes bases : on ajoute la colonne « publish » si elle manque
    cols = {c["name"] for c in inspect(engine).get_columns("channels")}
    if "publish" not in cols:
        _x("ALTER TABLE channels ADD COLUMN publish INTEGER NOT NULL DEFAULT 1")


# ---- utilisateurs / réglages
def add_user(uid):
    _x("INSERT INTO users (id) VALUES (:i) ON CONFLICT (id) DO NOTHING", i=uid)


def count_users():
    return _q("SELECT COUNT(*) AS n FROM users")[0]["n"]


def get_setting(key, default=None):
    r = _q("SELECT v FROM settings WHERE k=:k", k=key)
    return r[0]["v"] if r else default


def set_setting(key, value):
    _x("INSERT INTO settings (k, v) VALUES (:k, :v) ON CONFLICT (k) DO UPDATE SET v=:v", k=key, v=value)


# ---- canaux
def list_channels(status=None):
    if status:
        return _q("SELECT * FROM channels WHERE status=:s ORDER BY added_at, id", s=status)
    return _q("SELECT * FROM channels ORDER BY added_at, id")


def owner_channels(owner_id):
    return _q("SELECT * FROM channels WHERE owner_id=:o ORDER BY added_at, id", o=owner_id)


def get_channel(cid):
    r = _q("SELECT * FROM channels WHERE id=:i", i=cid)
    return r[0] if r else None


def count(status):
    return _q("SELECT COUNT(*) AS n FROM channels WHERE status=:s", s=status)[0]["n"]


def count_by_owner(owner_id):
    return _q("SELECT COUNT(*) AS n FROM channels WHERE owner_id=:o AND status<>'lost'", o=owner_id)[0]["n"]


def total_members():
    return _q("SELECT COALESCE(SUM(members),0) AS n FROM channels WHERE status='active'")[0]["n"]


def upsert_channel(cid, owner_id, title, link, members, status, publish=1):
    _x(
        """INSERT INTO channels (id, owner_id, title, link, members, status, publish)
           VALUES (:i, :o, :t, :l, :m, :s, :p)
           ON CONFLICT (id) DO UPDATE SET owner_id=:o, title=:t, link=:l,
           members=:m, status=:s, publish=:p, cross_msg_id=NULL""",
        i=cid, o=owner_id, t=title, l=link, m=members, s=status, p=publish,
    )


def set_publish(cid, value):
    _x("UPDATE channels SET publish=:p WHERE id=:i", p=value, i=cid)


def set_status(cid, status):
    _x("UPDATE channels SET status=:s WHERE id=:i", s=status, i=cid)


def set_msg(cid, msg_id):
    _x("UPDATE channels SET cross_msg_id=:m WHERE id=:i", m=msg_id, i=cid)


def delete_channel(cid):
    _x("DELETE FROM channels WHERE id=:i", i=cid)


# ---- sponsors (boutons pleine largeur)
def list_extras():
    return _q("SELECT * FROM extras ORDER BY id")


def add_extra(label, link):
    _x("INSERT INTO extras (id, label, link) VALUES (:i, :l, :k)", i=int(time.time() * 1000), l=label, k=link)


def delete_extra(eid):
    _x("DELETE FROM extras WHERE id=:i", i=eid)
'''
_SOURCES['keyboards'] = r'''from telegram import InlineKeyboardButton as _B
from telegram import InlineKeyboardMarkup as M

_ICON = {"active": "🟢", "pending": "⏳", "lost": "⚠️"}


def B(text, data=None, style=None, url=None):
    """Bouton inline (action ou lien). style : 'primary' (bleu), 'success' (vert), 'danger' (rouge)."""
    kw = {"api_kwargs": {"style": style}} if style else {}
    if url:
        return _B(text, url=url, **kw)
    return _B(text, callback_data=data, **kw)


def icon(status):
    return _ICON.get(status, "•")


def menu(is_admin=False):
    rows = [
        [B("➕  Ajouter mon canal", "add", "primary")],
        [B("📂  Mes canaux", "mine"), B("ℹ️  Aide", "help")],
    ]
    if is_admin:
        rows.append([B("🛠  Panneau admin", "admin")])
    return M(rows)


def back(target="menu"):
    return M([[B("⬅️  Retour", target)]])


def cancel():
    return M([[B("✖️  Annuler", "menu", "danger")]])


def confirm(is_admin=False):
    if is_admin:
        return M([
            [B("✅  Oui, publier la cross ici", "confirm_add", "success")],
            [B("🚫  Non, juste dans la liste", "confirm_nopub")],
            [B("✖️  Annuler", "menu", "danger")],
        ])
    return M([[B("✅  Confirmer", "confirm_add", "success"), B("✖️  Annuler", "menu", "danger")]])


def my_channels(channels):
    rows = [[B(f"{icon(c['status'])}  {c['title'][:35]}", f"ch:{c['id']}")] for c in channels]
    rows.append([B("⬅️  Retour", "menu")])
    return M(rows)


def channel_detail(cid, is_admin=False, publish=True):
    rows = []
    if is_admin:
        rows.append([B("📢  Publication : activée" if publish else "🚫  Publication : désactivée", f"pub:{cid}")])
    rows.append([B("🗑  Retirer de la cross", f"del:{cid}", "danger")])
    rows.append([B("⬅️  Retour", "mine")])
    return M(rows)


def delete_confirm(cid):
    return M([[B("🗑  Oui, retirer", f"delok:{cid}", "danger"), B("↩️  Non", f"ch:{cid}")]])


def admin(pending):
    return M([
        [B("✏️  Texte", "adm_text"), B("🖼  Photo", "adm_photo")],
        [B("⏰  Planning", "adm_plan", "primary"), B("⭐  Sponsors", "adm_sp")],
        [B("🔄  Synchroniser", "adm_sync"), B(f"⏳  En attente ({pending})", "adm_pending")],
        [B("📋  Tous les canaux", "adm_all")],
        [B("⬅️  Menu", "menu")],
    ])


def pending_list(channels):
    rows = [[B(f"✅  {c['title'][:25]}", f"appr:{c['id']}", "success"), B("❌", f"rej:{c['id']}", "danger")] for c in channels]
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)


def all_list(channels):
    rows = [[B(f"🗑  {c['title'][:35]}", f"admdel:{c['id']}")] for c in channels]
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)


def approve_reject(cid):
    return M([[B("✅  Accepter", f"appr:{cid}", "success"), B("❌  Refuser", f"rej:{cid}", "danger")]])


def photo_menu():
    return M([[B("🗑  Retirer la photo", "adm_photo_del", "danger")], [B("⬅️  Retour", "admin")]])


def planning(on):
    return M([
        [B("🔴  Désactiver le planning", "adm_plan_toggle", "danger") if on else B("🟢  Activer le planning", "adm_plan_toggle", "success")],
        [B("🕒  Horaires", "adm_plan_times"), B("⏳  Durée", "adm_plan_duration")],
        [B("🚀  Publier maintenant", "adm_plan_now", "success"), B("🛑  Retirer maintenant", "adm_plan_stop", "danger")],
        [B("⬅️  Retour", "admin")],
    ])


def sponsors(extras):
    rows = [[B(f"🗑  {e['label'][:35]}", f"spdel:{e['id']}")] for e in extras]
    rows.append([B("➕  Ajouter un sponsor", "adm_sp_add", "success")])
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)
'''
_SOURCES['texts'] = r'''LINE = "━━━━━━━━━━━━━━━"


def num(n):
    return f"{int(n):,}".replace(",", " ")


# ------------------------------------------------------------------ membres
MENU = (
    "✨ <b>CROSS PREMIUM</b> ✨\n" + LINE + "\n\n"
    "Fais découvrir ton canal gratuitement grâce à la cross automatique.\n\n"
    "📊 <b>{n}</b> canaux partenaires\n\n"
    "👇 <i>Choisis une action</i>"
)

HELP = (
    "ℹ️ <b>Comment ça marche ?</b>\n" + LINE + "\n\n"
    "1️⃣ Ajoute le bot comme <b>administrateur</b> de ton canal\n"
    "2️⃣ Inscris ton canal ici en 2 étapes\n"
    "3️⃣ Le bot publie la cross dans ton canal, avec tous les canaux partenaires en boutons\n"
    "4️⃣ Elle se supprime toute seule après quelques heures, puis revient chaque jour\n\n"
    "✅ <b>Condition :</b> au moins <b>{min}</b> abonnés\n"
    "⚠️ Ne retire pas le bot de ton canal, sinon il sera retiré de la liste."
)

ADD_STEP1 = (
    "➕ <b>Ajouter ton canal</b>\n" + LINE + "\n"
    "<b>Étape 1 sur 2</b>\n\n"
    "1️⃣ Ajoute @{bot} comme <b>administrateur</b> de ton canal\n"
    "2️⃣ Donne-lui les droits : <b>publier</b>, <b>modifier</b> et <b>supprimer</b> des messages "
    "(et <b>inviter des utilisateurs</b> si ton canal est privé)\n"
    "3️⃣ Envoie-moi ici le <b>@username</b> de ton canal, ou <b>transfère-moi un message</b> de ton canal\n\n"
    "🔒 <i>Minimum requis : {min} abonnés</i>"
)

CONFIRM = (
    "✅ <b>Vérification réussie</b>\n" + LINE + "\n\n"
    "📣 <b>{title}</b>\n"
    "👥 {members} abonnés\n"
    "🔗 {link}\n\n"
    "<b>Étape 2 sur 2</b> — Confirmer l'inscription à la cross ?"
)

ADDED = "🎉 <b>Bienvenue dans la cross !</b>\n" + LINE + "\n\nTon canal est inscrit. Le message est en cours de publication dans tous les canaux partenaires."
ADDED_WAIT = "🎉 <b>Bienvenue dans la cross !</b>\n" + LINE + "\n\nTon canal est inscrit. Il sera publié à la prochaine diffusion : <b>{next}</b>."
PENDING = "⏳ <b>Demande envoyée</b>\n" + LINE + "\n\nUn admin doit valider ton canal. Tu seras prévenu ici."
APPROVED = "✅ Ton canal <b>{title}</b> a été accepté dans la cross !"
REJECTED = "❌ Ton canal <b>{title}</b> a été refusé."
LOST = "⚠️ Ton canal <b>{title}</b> a été retiré de la cross : le bot n'a plus accès (admin retiré ou droits manquants). Tu peux le réinscrire depuis le menu."

FULL = "😕 La cross est complète pour le moment (<b>{n}</b> canaux max)."
LIMIT = "😕 Tu as déjà atteint la limite de <b>{n}</b> canaux par personne."

ERR_FORMAT = "⚠️ Je n'ai pas compris. Envoie le <b>@username</b> du canal, ou transfère-moi un message de ce canal."
ERR_NOTFOUND = "⚠️ Canal introuvable. Vérifie le @username (canal privé : transfère-moi un message de celui-ci)."
ERR_TYPE = "⚠️ Ce n'est pas un canal. Seuls les <b>canaux</b> sont acceptés."
ERR_EXISTS = "⚠️ Ce canal est déjà inscrit dans la cross."
ERR_BOT = "⚠️ Je ne suis pas administrateur de ce canal. Ajoute-moi puis réessaie."
ERR_RIGHTS = "⚠️ Il me manque des droits : <b>publier</b> et <b>modifier</b> des messages. Corrige puis renvoie ton canal."
ERR_OWNER = "⚠️ Tu dois être administrateur de ce canal pour l'inscrire."
ERR_MIN = "⚠️ <b>Canal trop petit</b>\n\nIl faut au moins <b>{n}</b> abonnés pour rejoindre la cross.\nTon canal en compte <b>{have}</b>."
ERR_LINK = "⚠️ Canal privé : donne-moi le droit <b>inviter des utilisateurs</b> pour que je crée le lien, puis réessaie."

NO_CHANNELS = "📂 Tu n'as encore aucun canal dans la cross.\n\nAppuie sur « Ajouter mon canal » pour commencer."
MINE = "📂 <b>Mes canaux</b>\n" + LINE + "\n\n🟢 actif  ·  ⏳ en attente  ·  ⚠️ bot retiré"
CHANNEL = "📣 <b>{title}</b>\n" + LINE + "\n\n👥 {members} abonnés\n📌 Statut : {status}\n🔗 {link}"
DELETE_ASK = "🗑 Retirer <b>{title}</b> de la cross ?\n\nLe message sera supprimé de ton canal."
DELETED = "✅ Canal retiré de la cross."

# ------------------------------------------------------------------ admin
ADMIN = (
    "🛠 <b>Panneau admin</b>\n" + LINE + "\n\n"
    "👤 Utilisateurs : <b>{users}</b>\n"
    "🟢 Canaux actifs : <b>{active}</b>\n"
    "⏳ En attente : <b>{pending}</b>\n"
    "👥 Audience totale : <b>{members}</b>"
)
ADMIN_HEADER = (
    "✏️ <b>Texte de la cross</b>\n" + LINE + "\n\n"
    "Texte actuel :\n\n{current}\n\n" + LINE + "\n"
    "Envoie le <b>nouveau texte</b> (la mise en forme Telegram est conservée).\n"
    "💡 Écris <code>{{count}}</code> pour afficher automatiquement le nombre de canaux."
)
NEW_PENDING = "🔔 <b>Nouveau canal en attente</b>\n" + LINE + "\n\n📣 {title}\n👥 {members} abonnés\n🔗 {link}"
ADMIN_PHOTO = (
    "🖼 <b>Photo de la cross</b>\n" + LINE + "\n\n{state}\n\n"
    "Envoie-moi la <b>photo</b> à afficher en haut du message. Elle sera publiée dans tous les canaux."
)
ERR_CAPTION = "⚠️ Avec une photo, le texte est limité à <b>1024 caractères</b>. Raccourcis-le puis réessaie."

PLANNING = (
    "⏰ <b>Planning</b>\n" + LINE + "\n\n"
    "Statut : {status}\n"
    "🕒 Horaires ({tz}) : <b>{times}</b>\n"
    "⏳ Durée d'affichage : <b>{duration}</b>\n"
    "📡 En ce moment : {live}\n\n"
    "<i>Chaque jour, la cross est publiée toute seule à ces horaires, puis supprimée après la durée choisie.</i>"
)
TIMES_ASK = "🕒 <b>Horaires de publication</b>\n" + LINE + "\n\nEnvoie les heures (fuseau {tz}) séparées par des espaces.\nExemple : <code>10:00 18:00 22:30</code>"
DURATION_ASK = "⏳ <b>Durée d'affichage</b>\n" + LINE + "\n\nEnvoie le nombre d'heures avant la suppression automatique.\nExemples : <code>6</code> ou <code>1.5</code>"
ERR_TIMES = "⚠️ Aucune heure valide. Exemple : <code>10:00 18:00</code>"
ERR_DURATION = "⚠️ Envoie un nombre d'heures entre 0.25 et 72. Exemple : <code>6</code>"

SPONSORS = (
    "⭐ <b>Sponsors</b>\n" + LINE + "\n\n"
    "Boutons pleine largeur insérés entre les groupes de canaux (tous les {every} canaux).\n\n"
    "{items}\n\n<i>Touche un sponsor pour le supprimer.</i>"
)
SP_LABEL = "⭐ <b>Nouveau sponsor</b>\n" + LINE + "\n\nÉtape 1 sur 2 — envoie le <b>texte du bouton</b>.\nExemple : <code>⭐ Sponsor ⭐</code>"
SP_LINK = "Étape 2 sur 2 — envoie le <b>lien</b> du bouton (<code>https://…</code> ou <code>@username</code>)."
ERR_SP_LINK = "⚠️ Lien invalide. Envoie un lien <code>https://…</code> ou un <code>@username</code>."

CONFIRM_ADMIN = (
    "✅ <b>Vérification réussie</b>\n" + LINE + "\n\n"
    "📣 <b>{title}</b>\n"
    "👥 {members} abonnés\n"
    "🔗 {link}\n\n"
    "📢 <b>Publier la cross dans ce canal ?</b>\n"
    "<i>Si tu refuses, le canal apparaît quand même en bouton dans les autres canaux, mais la cross n'y est pas publiée.</i>"
)
ADDED_NOPUB = (
    "🎉 <b>Canal ajouté à la liste</b>\n" + LINE + "\n\n"
    "🚫 La cross ne sera <b>pas publiée</b> dans ce canal.\n"
    "Il apparaît en bouton dans tous les autres canaux."
)
'''
_SOURCES['ui'] = r'''"""Panneau unique : chaque étape édite le même message, les messages de l'utilisateur sont supprimés."""
from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def clean(update):
    if update.message:
        try:
            await update.message.delete()
        except TelegramError:
            pass


async def show(update, context, text, kb=None):
    kw = dict(parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW)
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, **kw)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        context.user_data["panel"] = (q.message.chat_id, q.message.message_id)
        return

    chat_id = update.effective_chat.id
    panel = context.user_data.get("panel")
    is_cmd = bool(update.message and (update.message.text or "").startswith("/"))
    if panel and not is_cmd:
        try:
            await context.bot.edit_message_text(text, chat_id=panel[0], message_id=panel[1], **kw)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except TelegramError:
            pass
    if panel:
        try:
            await context.bot.delete_message(panel[0], panel[1])
        except TelegramError:
            pass
    msg = await context.bot.send_message(chat_id, text, **kw)
    context.user_data["panel"] = (msg.chat_id, msg.message_id)


async def ack(update, text=None, show_alert=False):
    """Répond au clic d'un bouton sans jamais planter (même si déjà répondu)."""
    q = update.callback_query
    if q:
        try:
            await q.answer(text, show_alert=show_alert)
        except TelegramError:
            pass
'''
_SOURCES['cross'] = r'''"""Coeur du bot : publication, mise à jour et suppression planifiée de la cross."""
import asyncio
import logging
import re
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

import config, db, texts
from keyboards import B
from ui import NO_PREVIEW

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)
_lock = asyncio.Lock()
_LOST_HINTS = ("chat not found", "not enough rights", "kicked", "administrator", "no rights", "deactivated")


# ------------------------------------------------------------------ boutons
def _label(title, index):
    """Pastille + nom du canal + la même pastille (la couleur change à chaque bouton)."""
    icons = config.BUTTON_ICONS
    if not icons:
        return title[:34]
    icon = icons[index % len(icons)]
    n = config.BUTTON_NAME_MAX
    name = title if len(title) <= n else title[: n - 1].rstrip() + "…"
    return f"{icon} {name} {icon}"


def build_markup(bot_username, channels, extras=()):
    """Canaux en grille (X par ligne) + un sponsor pleine largeur entre chaque groupe."""
    per = config.BUTTONS_PER_ROW
    group = max(per, config.SPONSOR_EVERY - config.SPONSOR_EVERY % per)
    extras = list(extras)
    rows = []
    for i in range(0, len(channels), group):
        chunk = channels[i:i + group]
        for j in range(0, len(chunk), per):
            rows.append([B(_label(c["title"], i + j + k), url=c["link"]) for k, c in enumerate(chunk[j:j + per])])
        if extras and i + group < len(channels):
            e = extras.pop(0)
            rows.append([B(e["label"][:40], url=e["link"], style="success")])
    for e in extras:
        rows.append([B(e["label"][:40], url=e["link"], style="success")])
    rows.append([B("➕  Rejoindre la cross", url=f"https://t.me/{bot_username}", style="primary")])
    return M(rows)


# ------------------------------------------------------------------ planning
def schedule_on():
    return db.get_setting("sched_on", "1") == "1"


def is_live():
    """Vrai si la cross doit être affichée maintenant (toujours vrai si le planning est désactivé)."""
    return (not schedule_on()) or db.get_setting("live", "0") == "1"


def parse_times(raw):
    out = set()
    for m in re.finditer(r"(\d{1,2})\s*[:hH]\s*(\d{2})", raw or ""):
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 24 and mi < 60:
            out.add(f"{h:02d}:{mi:02d}")
    return sorted(out)


def get_times():
    return parse_times(db.get_setting("times", config.DEFAULT_TIMES))


def get_duration():
    try:
        return float(db.get_setting("duration", str(config.DEFAULT_DURATION)))
    except ValueError:
        return config.DEFAULT_DURATION


def _slot(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=TZ)


def next_slot_str():
    now = datetime.now(TZ)
    times = get_times()
    if not times:
        return "bientôt"
    for offset in (0, 1):
        day = (now + timedelta(days=offset)).date()
        for t in times:
            if _slot(day, t) > now:
                return ("aujourd'hui" if offset == 0 else "demain") + f" à {t}"
    return "bientôt"


def until_str():
    raw = db.get_setting("delete_at", "")
    try:
        return datetime.fromisoformat(raw).astimezone(TZ).strftime("%H:%M")
    except ValueError:
        return "?"


def set_live_state():
    db.set_setting("live", "1")
    db.set_setting("delete_at", (datetime.now(TZ) + timedelta(hours=get_duration())).isoformat())


async def start_cycle(bot):
    set_live_state()
    for ch in db.list_channels("active"):
        db.set_msg(ch["id"], None)
    await sync_all(bot)


async def end_cycle(bot):
    db.set_setting("live", "0")
    db.set_setting("delete_at", "")
    async with _lock:
        for ch in db.list_channels():
            if ch.get("cross_msg_id"):
                await remove_message(bot, ch)
                db.set_msg(ch["id"], None)
                await asyncio.sleep(0.3)


async def tick(context):
    """Appelé chaque minute : supprime quand c'est l'heure, publie aux horaires prévus."""
    try:
        if not schedule_on():
            return
        bot = context.bot
        now = datetime.now(TZ)
        if db.get_setting("live", "0") == "1":
            raw = db.get_setting("delete_at", "")
            if not raw or now >= datetime.fromisoformat(raw):
                await end_cycle(bot)
            return
        slots = [_slot(now.date(), t) for t in get_times()]
        due = max((s for s in slots if s <= now), default=None)
        if not due:
            return
        raw_last = db.get_setting("last_slot", "")
        last = datetime.fromisoformat(raw_last) if raw_last else None
        if last is None or due > last:
            db.set_setting("last_slot", due.isoformat())
            if now - due <= timedelta(minutes=30):  # trop en retard (bot endormi) : on saute
                await start_cycle(bot)
    except Exception:
        log.exception("Erreur dans le planning")


# ------------------------------------------------------------------ envoi
async def _mark_lost(bot, ch):
    db.set_status(ch["id"], "lost")
    db.set_msg(ch["id"], None)
    try:
        await bot.send_message(ch["owner_id"], texts.LOST.format(title=ch["title"]), parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def _push(bot, ch, header, markup, photo=None):
    """Retourne True si le canal vient d'être marqué comme perdu."""
    kw = dict(parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NO_PREVIEW)
    for _ in range(2):
        try:
            if ch.get("cross_msg_id"):
                try:
                    if photo:
                        await bot.edit_message_caption(
                            chat_id=ch["id"], message_id=ch["cross_msg_id"], caption=header,
                            parse_mode=ParseMode.HTML, reply_markup=markup,
                        )
                    else:
                        await bot.edit_message_text(header, chat_id=ch["id"], message_id=ch["cross_msg_id"], **kw)
                    return False
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        return False
                    ch["cross_msg_id"] = None  # message supprimé -> on republie
            if photo:
                msg = await bot.send_photo(
                    ch["id"], photo, caption=header, parse_mode=ParseMode.HTML, reply_markup=markup
                )
            else:
                msg = await bot.send_message(ch["id"], header, **kw)
            db.set_msg(ch["id"], msg.message_id)
            return False
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            await _mark_lost(bot, ch)
            return True
        except BadRequest as e:
            if any(h in str(e).lower() for h in _LOST_HINTS):
                await _mark_lost(bot, ch)
                return True
            log.warning("Envoi impossible vers %s : %s", ch["id"], e)
            return False
        except TelegramError as e:
            log.warning("Erreur Telegram vers %s : %s", ch["id"], e)
            return False
    return False


async def sync_all(bot):
    if not is_live():
        return
    async with _lock:
        channels = db.list_channels("active")
        header = db.get_setting("header", config.DEFAULT_HEADER).replace("{count}", str(len(channels)))
        markup = build_markup(bot.username, channels, db.list_extras())
        photo = db.get_setting("photo") or None
        any_lost = False
        for ch in channels:
            if ch.get("publish") == 0:  # canal en « liste seulement » : présent en bouton, pas de publication
                continue
            any_lost |= await _push(bot, ch, header, markup, photo)
            await asyncio.sleep(0.4)
    if any_lost:
        asyncio.create_task(sync_all(bot))


async def remove_message(bot, ch):
    if not ch.get("cross_msg_id"):
        return
    for _ in range(2):
        try:
            await bot.delete_message(ch["id"], ch["cross_msg_id"])
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError:
            return


async def remove_channel(bot, ch):
    db.delete_channel(ch["id"])
    await remove_message(bot, ch)
    await sync_all(bot)


async def repost_all(bot):
    if not is_live():
        return
    for ch in db.list_channels("active"):
        await remove_message(bot, ch)
        db.set_msg(ch["id"], None)
        await asyncio.sleep(0.3)
    await sync_all(bot)


async def drop_message(bot, ch):
    """Supprime le message de cross d'un canal (sans le retirer de la liste)."""
    await remove_message(bot, ch)
    db.set_msg(ch["id"], None)
'''
_SOURCES['add_channel'] = r'''import html
import re

from telegram import MessageOriginChannel
from telegram.constants import ParseMode
from telegram.error import TelegramError

import config, cross, db, keyboards as kb, texts, ui


async def on_add(update, context):
    await ui.ack(update)
    await begin(update, context)


async def begin(update, context):
    uid = update.effective_user.id
    if db.count("active") + db.count("pending") >= config.MAX_CHANNELS:
        return await ui.show(update, context, texts.FULL.format(n=config.MAX_CHANNELS), kb.back())
    if not config.is_admin(uid) and db.count_by_owner(uid) >= config.MAX_PER_USER:
        return await ui.show(update, context, texts.LIMIT.format(n=config.MAX_PER_USER), kb.back())
    context.user_data["state"] = "await_channel"
    await ui.show(update, context, texts.ADD_STEP1.format(bot=context.bot.username, min=texts.num(config.MIN_MEMBERS)), kb.cancel())


async def handle_input(update, context):
    msg = update.message
    await ui.clean(update)

    ref = None
    origin = msg.forward_origin
    if isinstance(origin, MessageOriginChannel):
        ref = origin.chat.id
    elif msg.text:
        t = msg.text.strip()
        m = re.search(r"(?:@|t\.me/)([A-Za-z0-9_]{4,})", t)
        if m:
            ref = "@" + m.group(1)
        elif re.fullmatch(r"-100\d+", t):
            ref = int(t)
    if ref is None:
        return await ui.show(update, context, texts.ERR_FORMAT, kb.cancel())

    async def fail(text):
        await ui.show(update, context, text, kb.cancel())

    bot = context.bot
    try:
        chat = await bot.get_chat(ref)
    except TelegramError:
        return await fail(texts.ERR_NOTFOUND)
    if chat.type != "channel":
        return await fail(texts.ERR_TYPE)

    existing = db.get_channel(chat.id)
    if existing and existing["status"] != "lost":
        return await fail(texts.ERR_EXISTS)

    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        user = await bot.get_chat_member(chat.id, update.effective_user.id)
        members = await bot.get_chat_member_count(chat.id)
    except TelegramError:
        return await fail(texts.ERR_BOT)

    if me.status != "administrator":
        return await fail(texts.ERR_BOT)
    if not (getattr(me, "can_post_messages", False) and getattr(me, "can_edit_messages", False)):
        return await fail(texts.ERR_RIGHTS)
    if user.status not in ("creator", "administrator"):
        return await fail(texts.ERR_OWNER)
    if members < config.MIN_MEMBERS and not config.is_admin(update.effective_user.id):
        return await fail(texts.ERR_MIN.format(n=texts.num(config.MIN_MEMBERS), have=texts.num(members)))

    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except TelegramError:
            return await fail(texts.ERR_LINK)

    context.user_data["state"] = None
    context.user_data["draft"] = dict(id=chat.id, title=chat.title, link=link, members=members)
    await ui.show(
        update, context,
        (texts.CONFIRM_ADMIN if config.is_admin(update.effective_user.id) else texts.CONFIRM).format(title=html.escape(chat.title), members=texts.num(members), link=html.escape(link)),
        kb.confirm(config.is_admin(update.effective_user.id)),
    )


async def on_confirm(update, context):
    q = update.callback_query
    await ui.ack(update)
    d = context.user_data.pop("draft", None)
    uid = q.from_user.id
    if not d:
        return await ui.show(update, context, texts.MENU.format(n=db.count("active")), kb.menu(config.is_admin(uid)))

    pending = config.REQUIRE_APPROVAL and not config.is_admin(uid)
    publish = 0 if (q.data == "confirm_nopub" and config.is_admin(uid)) else 1
    db.upsert_channel(d["id"], uid, d["title"], d["link"], d["members"], "pending" if pending else "active", publish)

    if not pending:
        context.application.create_task(cross.sync_all(context.bot))
        if not publish:
            msg = texts.ADDED_NOPUB
        else:
            msg = texts.ADDED if cross.is_live() else texts.ADDED_WAIT.format(next=cross.next_slot_str())
        return await ui.show(update, context, msg, kb.back())

    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                texts.NEW_PENDING.format(title=html.escape(d["title"]), members=d["members"], link=html.escape(d["link"])),
                parse_mode=ParseMode.HTML,
                reply_markup=kb.approve_reject(d["id"]),
            )
        except TelegramError:
            pass
    await ui.show(update, context, texts.PENDING, kb.back())
'''
_SOURCES['admin'] = r'''import functools
import html
import re

from telegram.constants import ParseMode
from telegram.error import TelegramError

import config, cross, db, keyboards as kb, texts, ui


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update, context):
        if not config.is_admin(update.effective_user.id):
            if update.callback_query:
                await ui.ack(update, "Accès refusé", show_alert=True)
            return
        return await fn(update, context)
    return wrapper


async def _panel(update, context):
    context.user_data.pop("state", None)
    pending = db.count("pending")
    text = texts.ADMIN.format(
        users=db.count_users(), active=db.count("active"), pending=pending, members=db.total_members()
    )
    await ui.show(update, context, text, kb.admin(pending))


async def _notify(bot, uid, text):
    try:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


@admin_only
async def on_admin(update, context):
    await ui.ack(update)
    await _panel(update, context)


@admin_only
async def on_header_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_header"
    current = db.get_setting("header", config.DEFAULT_HEADER)
    await ui.show(update, context, texts.ADMIN_HEADER.format(current=current), kb.back("admin"))


async def handle_header(update, context):
    new = update.message.text_html
    plain = update.message.text or ""
    await ui.clean(update)
    if not new:
        return
    if db.get_setting("photo") and len(plain) > 1024:
        return await ui.show(update, context, texts.ERR_CAPTION, kb.back("admin"))
    db.set_setting("header", new)
    context.application.create_task(cross.sync_all(context.bot))
    await _panel(update, context)


@admin_only
async def on_sync(update, context):
    await ui.ack(update, "Synchronisation lancée ✅")
    context.application.create_task(cross.sync_all(context.bot))


@admin_only
async def on_pending(update, context):
    await ui.ack(update)
    channels = db.list_channels("pending")
    if not channels:
        return await _panel(update, context)
    lines = "\n".join(f"• {html.escape(c['title'])} ({c['members']} 👥)" for c in channels)
    await ui.show(update, context, f"⏳ <b>CANAUX EN ATTENTE</b>\n━━━━━━━━━━━━━━━\n\n{lines}", kb.pending_list(channels))


@admin_only
async def on_decision(update, context):
    q = update.callback_query
    await ui.ack(update)
    action, cid = q.data.split(":")
    ch = db.get_channel(int(cid))
    if ch and ch["status"] == "pending":
        title = html.escape(ch["title"])
        if action == "appr":
            db.set_status(ch["id"], "active")
            await _notify(context.bot, ch["owner_id"], texts.APPROVED.format(title=title))
            context.application.create_task(cross.sync_all(context.bot))
        else:
            db.delete_channel(ch["id"])
            await _notify(context.bot, ch["owner_id"], texts.REJECTED.format(title=title))
    await on_pending(update, context)


@admin_only
async def on_all(update, context):
    await ui.ack(update)
    channels = db.list_channels()
    if not channels:
        return await _panel(update, context)
    await ui.show(update, context, "📋 <b>TOUS LES CANAUX</b>\n━━━━━━━━━━━━━━━\n\nTouche un canal pour le supprimer.", kb.all_list(channels))


@admin_only
async def on_admin_delete(update, context):
    q = update.callback_query
    await ui.ack(update, "Canal supprimé")
    ch = db.get_channel(int(q.data.split(":")[1]))
    if ch:
        context.application.create_task(cross.remove_channel(context.bot, ch))
        await _notify(context.bot, ch["owner_id"], f"ℹ️ Ton canal <b>{html.escape(ch['title'])}</b> a été retiré de la cross par un admin.")
    await on_all(update, context)


@admin_only
async def on_photo_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_photo"
    state = "✅ Une photo est définie." if db.get_setting("photo") else "Aucune photo définie."
    await ui.show(update, context, texts.ADMIN_PHOTO.format(state=state), kb.photo_menu())


async def handle_photo(update, context):
    msg = update.message
    photo = msg.photo[-1].file_id if msg.photo else None
    await ui.clean(update)
    if not photo:
        return
    header_plain = re.sub(r"<[^>]+>", "", db.get_setting("header", config.DEFAULT_HEADER))
    if len(header_plain) > 1024:
        return await ui.show(update, context, texts.ERR_CAPTION, kb.back("admin"))
    db.set_setting("photo", photo)
    context.application.create_task(cross.repost_all(context.bot))
    await _panel(update, context)


@admin_only
async def on_photo_del(update, context):
    await ui.ack(update, "Photo retirée ✅")
    db.set_setting("photo", "")
    context.application.create_task(cross.repost_all(context.bot))
    await _panel(update, context)
'''
_SOURCES['admin_tools'] = r'''"""Outils admin : planning (publication / suppression automatiques) et sponsors."""
import html

import config, cross, db, keyboards as kb, texts, ui
from admin import _panel, admin_only

STATES = ("await_times", "await_duration", "await_sp_label", "await_sp_link")
MAX_SPONSORS = 10


# ------------------------------------------------------------------ planning
async def _planning(update, context):
    context.user_data.pop("state", None)
    on = cross.schedule_on()
    if not on:
        live = "🟢 Cross affichée en permanence"
    elif db.get_setting("live", "0") == "1":
        live = f"🟢 Publiée jusqu'à <b>{cross.until_str()}</b>"
    else:
        live = f"⚪ Non publiée — prochaine diffusion : <b>{cross.next_slot_str()}</b>"
    text = texts.PLANNING.format(
        status="🟢 Activé" if on else "🔴 Désactivé",
        tz=config.TIMEZONE,
        times=", ".join(cross.get_times()) or "aucun",
        duration=f"{cross.get_duration():g} h",
        live=live,
    )
    await ui.show(update, context, text, kb.planning(on))


@admin_only
async def on_planning(update, context):
    await ui.ack(update)
    await _planning(update, context)


@admin_only
async def on_toggle(update, context):
    await ui.ack(update)
    if cross.schedule_on():
        db.set_setting("sched_on", "0")
        db.set_setting("live", "0")
        db.set_setting("delete_at", "")
        context.application.create_task(cross.sync_all(context.bot))
    else:
        db.set_setting("sched_on", "1")
        context.application.create_task(cross.end_cycle(context.bot))
    await _planning(update, context)


@admin_only
async def on_times_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_times"
    await ui.show(update, context, texts.TIMES_ASK.format(tz=config.TIMEZONE), kb.back("adm_plan"))


@admin_only
async def on_duration_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_duration"
    await ui.show(update, context, texts.DURATION_ASK, kb.back("adm_plan"))


@admin_only
async def on_now(update, context):
    await ui.ack(update, "Publication lancée ✅")
    if cross.schedule_on():
        cross.set_live_state()
    context.application.create_task(cross.repost_all(context.bot))
    await _planning(update, context)


@admin_only
async def on_stop(update, context):
    if cross.schedule_on():
        await ui.ack(update, "Suppression lancée ✅")
        context.application.create_task(cross.end_cycle(context.bot))
        await _planning(update, context)
    else:
        await ui.ack(update, "Planning désactivé : la cross est permanente.", show_alert=True)


# ------------------------------------------------------------------ sponsors
async def _sponsors(update, context):
    context.user_data.pop("state", None)
    extras = db.list_extras()
    items = "\n".join(f"• {html.escape(e['label'])}" for e in extras) or "Aucun sponsor pour l'instant."
    await ui.show(update, context, texts.SPONSORS.format(every=config.SPONSOR_EVERY, items=items), kb.sponsors(extras))


@admin_only
async def on_sponsors(update, context):
    await ui.ack(update)
    await _sponsors(update, context)


@admin_only
async def on_sp_add(update, context):
    if len(db.list_extras()) >= MAX_SPONSORS:
        return await ui.ack(update, f"Maximum {MAX_SPONSORS} sponsors.", show_alert=True)
    await ui.ack(update)
    context.user_data["state"] = "await_sp_label"
    await ui.show(update, context, texts.SP_LABEL, kb.back("adm_sp"))


@admin_only
async def on_sp_del(update, context):
    await ui.ack(update, "Sponsor supprimé")
    db.delete_extra(int(update.callback_query.data.split(":")[1]))
    context.application.create_task(cross.sync_all(context.bot))
    await _sponsors(update, context)


def _normalize_link(text):
    t = text.strip()
    if t.startswith("@") and len(t) > 4 and " " not in t:
        return "https://t.me/" + t[1:]
    if t.lower().startswith("t.me/"):
        return "https://" + t
    if t.lower().startswith(("http://", "https://")) and " " not in t:
        return t
    return None


# ------------------------------------------------------------------ saisies texte
async def handle_input(update, context):
    state = context.user_data.get("state")
    text = (update.message.text or "").strip()
    await ui.clean(update)
    if not text:
        return

    if state == "await_times":
        times = cross.parse_times(text)
        if not times:
            return await ui.show(update, context, texts.ERR_TIMES, kb.back("adm_plan"))
        db.set_setting("times", ",".join(times))
        db.set_setting("last_slot", "")
        return await _planning(update, context)

    if state == "await_duration":
        try:
            hours = float(text.replace(",", ".").lower().replace("h", "").strip())
        except ValueError:
            hours = 0
        if not 0.25 <= hours <= 72:
            return await ui.show(update, context, texts.ERR_DURATION, kb.back("adm_plan"))
        db.set_setting("duration", str(hours))
        return await _planning(update, context)

    if state == "await_sp_label":
        context.user_data["sp_label"] = text[:40]
        context.user_data["state"] = "await_sp_link"
        return await ui.show(update, context, texts.SP_LINK, kb.back("adm_sp"))

    if state == "await_sp_link":
        link = _normalize_link(text)
        if not link:
            return await ui.show(update, context, texts.ERR_SP_LINK, kb.back("adm_sp"))
        db.add_extra(context.user_data.pop("sp_label", "⭐ Sponsor ⭐"), link)
        context.application.create_task(cross.sync_all(context.bot))
        return await _sponsors(update, context)
'''
_SOURCES['botcommands'] = r'''"""Menu des commandes (bouton « Menu » à côté de la zone de saisie). Les commandes admin ne sont visibles que par les admins."""
import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import TelegramError

import config

log = logging.getLogger(__name__)

USER = [
    BotCommand("start", "🏠 Menu principal"),
    BotCommand("ajouter", "➕ Ajouter mon canal"),
    BotCommand("canaux", "📂 Mes canaux"),
    BotCommand("aide", "ℹ️ Comment ça marche"),
]
ADMIN = USER + [
    BotCommand("admin", "🛠 Panneau admin"),
    BotCommand("planning", "⏰ Planning de diffusion"),
    BotCommand("sponsors", "⭐ Gérer les sponsors"),
    BotCommand("publier", "🚀 Publier maintenant"),
    BotCommand("retirer", "🛑 Retirer maintenant"),
]


async def for_admin(bot, chat_id):
    try:
        await bot.set_my_commands(ADMIN, scope=BotCommandScopeChat(chat_id))
    except TelegramError as e:
        log.info("Commandes admin non définies pour %s (il doit d'abord faire /start) : %s", chat_id, e)


async def setup(bot):
    try:
        await bot.set_my_commands(USER, scope=BotCommandScopeDefault())
    except TelegramError as e:
        log.warning("Impossible de définir le menu des commandes : %s", e)
    for admin_id in config.ADMIN_IDS:
        await for_admin(bot, admin_id)
'''
_SOURCES['my_channels'] = r'''import html

import config, cross, db, keyboards as kb, texts, ui

_STATUS = {"active": "🟢 Actif", "pending": "⏳ En attente", "lost": "⚠️ Bot retiré"}


def _owned(ch, uid):
    return ch and (ch["owner_id"] == uid or config.is_admin(uid))


async def on_mine(update, context):
    await ui.ack(update)
    await show_mine(update, context)


async def show_mine(update, context):
    channels = db.owner_channels(update.effective_user.id)
    if not channels:
        return await ui.show(update, context, texts.NO_CHANNELS, kb.back())
    await ui.show(update, context, texts.MINE, kb.my_channels(channels))


async def _detail(update, context, ch):
    uid = update.effective_user.id
    publish = ch.get("publish") != 0
    status = _STATUS.get(ch["status"], ch["status"]) + ("" if publish else "  ·  🚫 sans publication")
    await ui.show(
        update, context,
        texts.CHANNEL.format(
            title=html.escape(ch["title"]), members=ch["members"],
            status=status, link=html.escape(ch["link"]),
        ),
        kb.channel_detail(ch["id"], config.is_admin(uid), publish),
    )


async def on_channel(update, context):
    q = update.callback_query
    await ui.ack(update)
    ch = db.get_channel(int(q.data.split(":")[1]))
    if not _owned(ch, q.from_user.id):
        return await show_mine(update, context)
    await _detail(update, context, ch)


async def on_toggle_publish(update, context):
    q = update.callback_query
    if not config.is_admin(q.from_user.id):
        return await ui.ack(update, "Accès refusé", show_alert=True)
    ch = db.get_channel(int(q.data.split(":")[1]))
    if not ch:
        await ui.ack(update)
        return await show_mine(update, context)
    new = 0 if ch.get("publish") != 0 else 1
    db.set_publish(ch["id"], new)
    if new:
        context.application.create_task(cross.sync_all(context.bot))
    else:
        context.application.create_task(cross.drop_message(context.bot, ch))
    await ui.ack(update, "Publication activée ✅" if new else "Publication désactivée 🚫")
    await _detail(update, context, db.get_channel(ch["id"]))


async def on_delete_ask(update, context):
    q = update.callback_query
    await ui.ack(update)
    ch = db.get_channel(int(q.data.split(":")[1]))
    if not _owned(ch, q.from_user.id):
        return await show_mine(update, context)
    await ui.show(update, context, texts.DELETE_ASK.format(title=html.escape(ch["title"])), kb.delete_confirm(ch["id"]))


async def on_delete_ok(update, context):
    q = update.callback_query
    await ui.ack(update)
    ch = db.get_channel(int(q.data.split(":")[1]))
    if _owned(ch, q.from_user.id):
        context.application.create_task(cross.remove_channel(context.bot, ch))
    await ui.show(update, context, texts.DELETED, kb.back("mine"))
'''
_SOURCES['commands'] = r'''"""Commandes du menu : ouvrent directement le bon écran."""
import functools

import add_channel, admin, admin_tools, config, cross, db, keyboards as kb, my_channels, texts, ui


def _admin_cmd(fn):
    @functools.wraps(fn)
    async def wrapper(update, context):
        await ui.clean(update)
        context.user_data.pop("state", None)
        if not config.is_admin(update.effective_user.id):
            return  # commande réservée : on ne répond rien
        return await fn(update, context)
    return wrapper


async def cmd_add(update, context):
    db.add_user(update.effective_user.id)
    context.user_data.pop("state", None)
    await ui.clean(update)
    await add_channel.begin(update, context)


async def cmd_mine(update, context):
    context.user_data.pop("state", None)
    await ui.clean(update)
    await my_channels.show_mine(update, context)


async def cmd_help(update, context):
    context.user_data.pop("state", None)
    await ui.clean(update)
    await ui.show(update, context, texts.HELP.format(min=texts.num(config.MIN_MEMBERS)), kb.back())


@_admin_cmd
async def cmd_admin(update, context):
    await admin._panel(update, context)


@_admin_cmd
async def cmd_planning(update, context):
    await admin_tools._planning(update, context)


@_admin_cmd
async def cmd_sponsors(update, context):
    await admin_tools._sponsors(update, context)


@_admin_cmd
async def cmd_publish(update, context):
    if cross.schedule_on():
        cross.set_live_state()
    context.application.create_task(cross.repost_all(context.bot))
    await admin_tools._planning(update, context)


@_admin_cmd
async def cmd_stop(update, context):
    if cross.schedule_on():
        context.application.create_task(cross.end_cycle(context.bot))
    await admin_tools._planning(update, context)
'''
_SOURCES['health'] = r'''"""Mini serveur HTTP : Render exige un port ouvert, et UptimeRobot peut le pinger."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def start(port: int):
    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
'''
_SOURCES['router'] = r'''import config, ui
import add_channel, admin, admin_tools


async def on_message(update, context):
    state = context.user_data.get("state")
    if state == "await_channel":
        await add_channel.handle_input(update, context)
    elif state == "await_header" and config.is_admin(update.effective_user.id):
        await admin.handle_header(update, context)
    elif state == "await_photo" and config.is_admin(update.effective_user.id):
        await admin.handle_photo(update, context)
    elif state in admin_tools.STATES and config.is_admin(update.effective_user.id):
        await admin_tools.handle_input(update, context)
    else:
        await ui.clean(update)  # message hors parcours : on garde le chat propre
'''
_SOURCES['start'] = r'''import botcommands, config, db, keyboards as kb, texts, ui


def _menu(update):
    return texts.MENU.format(n=db.count("active")), kb.menu(config.is_admin(update.effective_user.id))


async def cmd_start(update, context):
    db.add_user(update.effective_user.id)
    context.user_data.pop("state", None)
    context.user_data.pop("draft", None)
    await ui.clean(update)
    if config.is_admin(update.effective_user.id):
        await botcommands.for_admin(context.bot, update.effective_chat.id)
    text, markup = _menu(update)
    await ui.show(update, context, text, markup)


async def on_menu(update, context):
    q = update.callback_query
    await ui.ack(update)
    context.user_data.pop("state", None)
    context.user_data.pop("draft", None)
    if q.data == "help":
        await ui.show(update, context, texts.HELP.format(min=texts.num(config.MIN_MEMBERS)), kb.back())
    else:
        text, markup = _menu(update)
        await ui.show(update, context, text, markup)
'''
_SOURCES['botmain'] = r'''import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import botcommands, config, cross, db, health
import add_channel, admin, admin_tools, commands, my_channels, router, start

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("crossbot")


async def post_init(app: Application):
    db.init()
    await botcommands.setup(app.bot)
    log.info("Bot démarré : @%s", app.bot.username)


async def on_error(update, context):
    log.error("Erreur non gérée", exc_info=context.error)


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN manquant")

    health.start(config.PORT)
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start.cmd_start, filters=private))

    for name, fn in [
        ("ajouter", commands.cmd_add), ("canaux", commands.cmd_mine), ("aide", commands.cmd_help),
        ("admin", commands.cmd_admin), ("planning", commands.cmd_planning), ("sponsors", commands.cmd_sponsors),
        ("publier", commands.cmd_publish), ("retirer", commands.cmd_stop),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=private))

    cb = CallbackQueryHandler
    app.add_handler(cb(start.on_menu, pattern=r"^(menu|help)$"))
    app.add_handler(cb(add_channel.on_add, pattern=r"^add$"))
    app.add_handler(cb(add_channel.on_confirm, pattern=r"^confirm_(add|nopub)$"))
    app.add_handler(cb(my_channels.on_mine, pattern=r"^mine$"))
    app.add_handler(cb(my_channels.on_channel, pattern=r"^ch:-?\d+$"))
    app.add_handler(cb(my_channels.on_toggle_publish, pattern=r"^pub:-?\d+$"))
    app.add_handler(cb(my_channels.on_delete_ask, pattern=r"^del:-?\d+$"))
    app.add_handler(cb(my_channels.on_delete_ok, pattern=r"^delok:-?\d+$"))

    app.add_handler(cb(admin.on_admin, pattern=r"^admin$"))
    app.add_handler(cb(admin.on_header_ask, pattern=r"^adm_text$"))
    app.add_handler(cb(admin.on_photo_ask, pattern=r"^adm_photo$"))
    app.add_handler(cb(admin.on_photo_del, pattern=r"^adm_photo_del$"))
    app.add_handler(cb(admin.on_sync, pattern=r"^adm_sync$"))
    app.add_handler(cb(admin_tools.on_planning, pattern=r"^adm_plan$"))
    app.add_handler(cb(admin_tools.on_toggle, pattern=r"^adm_plan_toggle$"))
    app.add_handler(cb(admin_tools.on_times_ask, pattern=r"^adm_plan_times$"))
    app.add_handler(cb(admin_tools.on_duration_ask, pattern=r"^adm_plan_duration$"))
    app.add_handler(cb(admin_tools.on_now, pattern=r"^adm_plan_now$"))
    app.add_handler(cb(admin_tools.on_stop, pattern=r"^adm_plan_stop$"))
    app.add_handler(cb(admin_tools.on_sponsors, pattern=r"^adm_sp$"))
    app.add_handler(cb(admin_tools.on_sp_add, pattern=r"^adm_sp_add$"))
    app.add_handler(cb(admin_tools.on_sp_del, pattern=r"^spdel:\d+$"))
    app.add_handler(cb(admin.on_pending, pattern=r"^adm_pending$"))
    app.add_handler(cb(admin.on_decision, pattern=r"^(appr|rej):-?\d+$"))
    app.add_handler(cb(admin.on_all, pattern=r"^adm_all$"))
    app.add_handler(cb(admin.on_admin_delete, pattern=r"^admdel:-?\d+$"))

    app.add_handler(MessageHandler(private & ~filters.COMMAND, router.on_message))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(cross.tick, interval=60, first=15)

    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
'''

for _name in ['config', 'db', 'keyboards', 'texts', 'ui', 'cross', 'add_channel', 'admin', 'admin_tools', 'botcommands', 'my_channels', 'commands', 'health', 'router', 'start', 'botmain']:
    _mod = types.ModuleType(_name)
    sys.modules[_name] = _mod
    exec(compile(_SOURCES[_name], _name + ".py", "exec"), _mod.__dict__)

if __name__ == "__main__":
    sys.modules["botmain"].main()
