"""
Bot de Cross Telegram : fichier unique.
Ce fichier contient tout le bot. Il se lance avec : python main.py
"""
import sys
import types

_SOURCES = {}
_SOURCES['config'] = r'''import os
import re

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

_PG = re.compile(r"postgres(?:ql)?://[^\s'\"]+")


def _find_database_url():
    """Cherche le lien Neon : d'abord DATABASE_URL, puis n'importe quelle variable contenant un lien postgresql://
    (nom mal écrit, guillemets, texte autour du lien...)."""
    candidates = [os.getenv("DATABASE_URL", "")] + [v for k, v in os.environ.items() if k != "DATABASE_URL"]
    for value in candidates:
        m = _PG.search(value or "")
        if m:
            return m.group(0)
    return None


DATABASE_URL = _find_database_url() or "sqlite:///crossbot.db"
# noms de variables qui ressemblent à une base de données (pour t'aider à repérer une faute de frappe)
DB_HINT_NAMES = sorted(k for k in os.environ if re.search(r"DATA|BASE|NEON|POSTG|^PG|DB", k, re.I))
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

MIN_MEMBERS = int(os.getenv("MIN_MEMBERS", "1000"))  # 1K minimum (les admins sont exemptés)
MAX_CHANNELS = int(os.getenv("MAX_CHANNELS", "10000"))
GROUP_SIZE = min(max(int(os.getenv("GROUP_SIZE", "80")), 10), 88)  # canaux par message (Telegram : 100 boutons max)
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "1000"))
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "false").lower() == "true"
BUTTONS_PER_ROW = max(1, min(int(os.getenv("BUTTONS_PER_ROW", "2")), 4))
TIMEZONE = os.getenv("TIMEZONE", "Africa/Douala")
DEFAULT_TIMES = os.getenv("POST_TIMES", "10:00,18:00")          # modifiable ensuite dans le bot
DEFAULT_DURATION = float(os.getenv("DELETE_AFTER_HOURS", "1.5"))  # modifiable ensuite dans le bot
DEFAULT_PAUSE = float(os.getenv("PAUSE_HOURS", "2"))              # pause entre deux diffusions (mode répétition)
DEFAULT_MODE = os.getenv("SCHEDULE_MODE", "cycle")                # "cycle" (répétition) ou "daily" (horaires fixes)
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
_SOURCES['db'] = r'''import os
import time

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
        grp INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS extras (id BIGINT PRIMARY KEY, label TEXT NOT NULL, link TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        name TEXT,
        username TEXT,
        last_seen TIMESTAMP,
        blocked INTEGER NOT NULL DEFAULT 0,
        optout INTEGER NOT NULL DEFAULT 0
    )""",
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
    if "grp" not in cols:
        _x("ALTER TABLE channels ADD COLUMN grp INTEGER")
    # anciennes bases : colonnes ajoutées à la table des utilisateurs
    ucols = {c["name"] for c in inspect(engine).get_columns("users")}
    for col, ddl in (("name", "TEXT"), ("username", "TEXT"), ("last_seen", "TIMESTAMP"), ("blocked", "INTEGER NOT NULL DEFAULT 0"), ("optout", "INTEGER NOT NULL DEFAULT 0")):
        if col not in ucols:
            _x(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


# ---- utilisateurs / réglages
def add_user(uid, name=None, username=None):
    """Enregistre l'identifiant s'il manque (nom et @ sont mis à jour par touch_user)."""
    _x("INSERT INTO users (id, last_seen) VALUES (:i, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING", i=uid)


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
           members=:m, status=:s, publish=:p, cross_msg_id=NULL, grp=NULL""",
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


# ---- groupes (rotation) et pagination
def set_group(cid, grp):
    _x("UPDATE channels SET grp=:g WHERE id=:i", g=grp, i=cid)


def set_groups(pairs):
    if not pairs:
        return
    with engine.begin() as c:
        c.execute(text("UPDATE channels SET grp=:g WHERE id=:i"), [{"g": g, "i": i} for i, g in pairs])


def clear_all_msgs():
    _x("UPDATE channels SET cross_msg_id=NULL")


def list_channels_page(offset, limit):
    return _q("SELECT * FROM channels ORDER BY added_at, id LIMIT :l OFFSET :o", l=limit, o=offset)


def count_total():
    return _q("SELECT COUNT(*) AS n FROM channels")[0]["n"]


# ---- type de base (alerte si les données risquent d'être perdues)
def is_postgres():
    return config.DATABASE_URL.startswith("postgresql")


def is_temporary():
    """SQLite sur Render : le fichier est effacé à chaque redémarrage."""
    return (not is_postgres()) and bool(os.getenv("RENDER"))


def kind_label():
    if is_postgres():
        return "✅ Neon / PostgreSQL (permanente)"
    return "⚠️ SQLite TEMPORAIRE (canaux effacés au redémarrage)" if is_temporary() else "💾 SQLite locale"


# ---- sauvegarde / restauration
_BACKUP_KEYS = ("header", "photo", "times", "duration", "pause", "mode", "sched_on")


def export_backup():
    settings = {k: get_setting(k) for k in _BACKUP_KEYS if get_setting(k) is not None}
    channels = _q("SELECT id, owner_id, title, link, members, status, publish FROM channels ORDER BY added_at, id")
    extras = [{"label": e["label"], "link": e["link"]} for e in list_extras()]
    users = _q("SELECT id, name, username FROM users ORDER BY first_seen, id")
    return {"version": 1, "channels": channels, "extras": extras, "settings": settings, "users": users}


def restore_backup(data):
    """Remet en place canaux, sponsors et réglages. Retourne (nb_canaux, nb_sponsors)."""
    n_ch = n_ex = 0
    for it in data.get("channels", []):
        try:
            status = it.get("status") if it.get("status") in ("active", "pending", "lost") else "active"
            upsert_channel(int(it["id"]), int(it["owner_id"]), str(it["title"]), str(it["link"]),
                           int(it.get("members") or 0), status, 0 if it.get("publish") == 0 else 1)
            n_ch += 1
        except (KeyError, ValueError, TypeError):
            continue
    if not list_extras():
        for e in data.get("extras", []):
            if e.get("label") and e.get("link"):
                add_extra(str(e["label"])[:40], str(e["link"]))
                n_ex += 1
                time.sleep(0.002)  # identifiants uniques
    for u in data.get("users", []):
        try:
            touch_user(int(u["id"]), u.get("name"), u.get("username"))
        except (KeyError, ValueError, TypeError):
            continue
    for k, v in (data.get("settings") or {}).items():
        if k in _BACKUP_KEYS and v is not None:
            set_setting(k, str(v))
    return n_ch, n_ex


# ---- utilisateurs : qui utilise le bot
def touch_user(uid, name=None, username=None):
    _x(
        """INSERT INTO users (id, name, username, last_seen, blocked)
           VALUES (:i, :n, :u, CURRENT_TIMESTAMP, 0)
           ON CONFLICT (id) DO UPDATE SET name=COALESCE(:n, users.name), username=:u,
           last_seen=CURRENT_TIMESTAMP, blocked=0""",
        i=uid, n=name, u=username,
    )


def list_users_page(offset, limit):
    return _q("SELECT * FROM users ORDER BY first_seen DESC, id DESC LIMIT :l OFFSET :o", l=limit, o=offset)


def users_stats():
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    return {
        "total": _q("SELECT COUNT(*) AS n FROM users")[0]["n"],
        "new": _q("SELECT COUNT(*) AS n FROM users WHERE first_seen >= :t", t=since)[0]["n"],
        "blocked": _q("SELECT COUNT(*) AS n FROM users WHERE blocked=1")[0]["n"],
        "optout": _q("SELECT COUNT(*) AS n FROM users WHERE optout=1")[0]["n"],
        "unnamed": _q("SELECT COUNT(*) AS n FROM users WHERE name IS NULL")[0]["n"],
    }


def list_reachable_ids():
    return [r["id"] for r in _q("SELECT id FROM users WHERE blocked=0 AND optout=0 ORDER BY id")]


def count_reachable():
    return _q("SELECT COUNT(*) AS n FROM users WHERE blocked=0 AND optout=0")[0]["n"]


def mark_blocked(uid):
    _x("UPDATE users SET blocked=1 WHERE id=:i", i=uid)


def owners_missing():
    """Propriétaires de canaux que le bot ne connaît pas encore comme utilisateurs (ou sans nom)."""
    rows = _q(
        """SELECT DISTINCT c.owner_id AS id FROM channels c
           LEFT JOIN users u ON u.id = c.owner_id
           WHERE u.id IS NULL OR u.name IS NULL"""
    )
    return [r["id"] for r in rows]


def set_optout(uid, value):
    _x("UPDATE users SET optout=:v WHERE id=:i", v=value, i=uid)


def users_without_name():
    return [r["id"] for r in _q("SELECT id FROM users WHERE name IS NULL ORDER BY id")]


def set_profile(uid, name, username):
    """Met à jour nom et @ sans toucher aux autres réglages (bloqué, désabonné...)."""
    _x("UPDATE users SET name=COALESCE(:n, name), username=:u WHERE id=:i", n=name, u=username, i=uid)
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
    "👥 Audience totale : <b>{members}</b>\n"
    "🔐 Validation des canaux : <b>{approval}</b>\n"
    "💾 Base : {db}"
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
    "🔁 Mode : <b>{mode}</b>\n"
    "{schedule}\n"
    "⏳ Durée d'affichage : <b>{duration}</b>\n"
    "📡 En ce moment : {live}\n\n"
    "<i>{explain}</i>"
)
TIMES_ASK = "🕒 <b>Horaires de publication</b>\n" + LINE + "\n\nEnvoie les heures (fuseau {tz}) séparées par des espaces.\nExemple : <code>10:00 18:00 22:30</code>"
DURATION_ASK = "⏳ <b>Durée d'affichage</b>\n" + LINE + "\n\nEnvoie combien de temps la cross reste affichée avant d'être supprimée.\nExemples : <code>1h30</code>, <code>90min</code>, <code>2</code>"
ERR_TIMES = "⚠️ Aucune heure valide. Exemple : <code>10:00 18:00</code>"
ERR_DURATION = "⚠️ Envoie une durée entre 15 minutes et 72 heures. Exemples : <code>1h30</code>, <code>90min</code>, <code>2</code>"

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
PAUSE_ASK = "⏸ <b>Pause entre deux diffusions</b>\n" + LINE + "\n\nEnvoie combien de temps le bot attend avant de republier la cross.\nExemples : <code>2h</code>, <code>2h30</code>, <code>45min</code>"

WARN_TEMP_DB = (
    "⚠️ <b>Base de données TEMPORAIRE</b>\n" + LINE + "\n\n"
    "Le bot n'est pas relié à Neon : tes canaux seront <b>effacés</b> au prochain redémarrage.\n\n"
    "Sur Render, ajoute la variable <code>DATABASE_URL</code> avec ton lien Neon (<code>postgresql://…</code>).\n\n"
    "🔎 Variables vues par le bot qui ressemblent à une base : <b>{names}</b>"
)
BACKUP_CAPTION = "💾 <b>Sauvegarde de la cross</b>\n{n} canaux.\nGarde ce fichier. Pour restaurer : /restaurer puis envoie-le ici."
RESTORE_ASK = (
    "♻️ <b>Restaurer une sauvegarde</b>\n" + LINE + "\n\n"
    "Envoie-moi le fichier <b>sauvegarde_cross….json</b> que le bot t'a envoyé.\n"
    "Les canaux, sponsors et réglages seront remis en place."
)
RESTORED = "✅ <b>Sauvegarde restaurée</b>\n" + LINE + "\n\n📣 {n} canaux\n⭐ {s} sponsors\n\nLa cross se met à jour."
ERR_BACKUP = "⚠️ Fichier illisible. Envoie le fichier <b>.json</b> de sauvegarde."
NO_BACKUP = "⚠️ Aucun canal à sauvegarder pour le moment."

USERS = (
    "👥 <b>UTILISATEURS</b>\n" + LINE + "\n\n"
    "👤 Total : <b>{total}</b>\n"
    "🆕 Nouveaux (24 h) : <b>{new}</b>\n"
    "🚫 Ont bloqué le bot : <b>{blocked}</b>\n"
    "🔕 Désabonnés des annonces : <b>{optout}</b>\n\n"
    "{items}\n\n"
    "📄 Page {page}/{pages}"
)
USERS_EMPTY = "👥 <b>UTILISATEURS</b>\n" + LINE + "\n\nAucun utilisateur enregistré pour le moment."
USER_ITEM = "👤 <b>{name}</b> {username}\n🆔 <code>{id}</code>  ·  📅 {date}  ·  {chans}"
BC_ASK = (
    "📢 <b>Message à tous</b>\n" + LINE + "\n\n"
    "Envoie-moi le message à diffuser : un texte (mise en forme conservée), ou une photo avec légende.\n\n"
    "👥 Il sera envoyé à <b>{n}</b> personnes."
)
BC_CONFIRM = "👆 <b>Voici le message qui sera envoyé.</b>\n\n👥 Destinataires : <b>{n}</b>\n\nConfirmer l'envoi ?"
BC_SENDING = "📤 <b>Envoi en cours…</b>\n\nTu recevras un bilan à la fin."
BC_DONE = "✅ <b>Envoi terminé</b>\n" + LINE + "\n\n📬 Reçu : <b>{ok}</b>\n🚫 Ont bloqué le bot : <b>{blocked}</b>\n⚠️ Échecs : <b>{failed}</b>"
BC_NONE = "⚠️ Aucun utilisateur à qui envoyer pour le moment."

UNSUB = (
    "🔕 <b>Annonces désactivées</b>\n" + LINE + "\n\n"
    "Tu ne recevras plus les messages d'information du bot.\n"
    "Tes canaux et la cross ne changent pas.\n\n"
    "Tu peux les réactiver quand tu veux."
)
RESUB = "🔔 <b>Annonces réactivées</b>\n" + LINE + "\n\nTu recevras de nouveau les messages d'information du bot."

PREMIUM = (
    "💎 <b>Emojis premium</b>\n" + LINE + "\n\n"
    "📌 Statut : {status}\n"
    "📦 Pack : <b>{pack}</b>\n"
    "🎯 Couverture : <b>{cov}</b>\n\n"
    "<i>Dès que ton compte Premium utilise le bot, ses emojis deviennent premium tout seuls. "
    "Les canaux gardent les emojis normaux : Telegram ne l'autorise pas dans les canaux.</i>"
)
PREMIUM_PACK_ASK = (
    "📦 <b>Choisir un pack d'emojis premium</b>\n" + LINE + "\n\n"
    "Dans Telegram, ouvre un pack d'emojis premium qui te plaît, copie son lien "
    "(il ressemble à <code>t.me/addemoji/NomDuPack</code>) et envoie-le ici."
)
PREMIUM_ERR = "⚠️ Pack introuvable, ou ce n'est pas un pack d'<b>emojis premium</b>. Envoie un lien <code>t.me/addemoji/…</code>."

NAMES_DONE = (
    "✅ <b>Noms actualisés</b>\n" + LINE + "\n\n"
    "👤 Récupérés : <b>{ok}</b> sur <b>{n}</b>\n\n"
    "<i>Ceux qui n'ont jamais écrit au bot restent « Sans nom » jusqu'à leur première action.</i>"
)
'''
_SOURCES['premium'] = r'''"""Emojis premium : remplacement automatique dans les messages privés du bot quand le compte Premium l'utilise.
Telegram n'autorise pas les emojis premium d'un bot dans les canaux : la cross publiée garde les emojis normaux."""
import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import db, texts

log = logging.getLogger(__name__)

MAP = {}            # emoji -> identifiant de l'emoji premium
REVERSE = {}        # identifiant -> emoji
PACK = ""
MODE = "auto"       # "auto" ou "off"
OWNER_PREMIUM = False
VERIFIED = False
_probed = False
_probing = False
_RE = None
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2B05-\u2B07\u231A\u231B\u23E9-\u23FA"
    "\u2194-\u21AA\u25AA-\u25FE\u2139\u203C\u2049]\ufe0f?"
)


def _norm(e):
    return e.replace("\ufe0f", "")


def set_map(mapping):
    global MAP, REVERSE, _RE
    MAP = {_norm(k): str(v) for k, v in (mapping or {}).items() if _norm(k)}
    REVERSE = {v: k for k, v in MAP.items()}
    _RE = re.compile("(" + "|".join(re.escape(k) for k in sorted(MAP, key=len, reverse=True)) + ")\ufe0f?") if MAP else None


def load():
    """Au démarrage : relit le pack choisi et le mode."""
    global MODE, PACK
    try:
        MODE = "off" if db.get_setting("premium_mode") == "off" else "auto"
        PACK = db.get_setting("emoji_pack", "") or ""
        raw = db.get_setting("emoji_map", "") or ""
        set_map(json.loads(raw) if raw else {})
    except Exception:
        log.exception("Emojis premium : chargement impossible")


def active():
    return MODE != "off" and OWNER_PREMIUM and VERIFIED and bool(MAP)


def disable(reason=""):
    global VERIFIED
    VERIFIED = False
    log.warning("Emojis premium désactivés : %s", reason)


def apply(text):
    """Remplace les emojis du pack par leur version premium."""
    if not active() or not _RE:
        return text
    return _RE.sub(lambda m: f'<tg-emoji emoji-id="{MAP[m.group(1)]}">{m.group(0)}</tg-emoji>', text)


def icon_and_text(text):
    """Pour un bouton : (identifiant d'emoji premium, texte sans l'emoji de début)."""
    if not active() or not _RE:
        return None, text
    m = _RE.match(text)
    if not m:
        return None, text
    rest = text[m.end():].lstrip()
    if not rest:
        return None, text
    return MAP[m.group(1)], rest


def plain_markup(kb):
    """Clavier sans emojis premium (secours si Telegram les refuse)."""
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return kb
    rows, changed = [], False
    for row in kb.inline_keyboard:
        new = []
        for b in row:
            extra = dict(getattr(b, "api_kwargs", None) or {})
            icon = extra.pop("icon_custom_emoji_id", None)
            text = b.text
            if icon:
                changed = True
                text = f"{REVERSE.get(icon, '')} {text}".strip()
            new.append(InlineKeyboardButton(text, callback_data=b.callback_data, url=b.url, api_kwargs=extra or None))
        rows.append(new)
    return InlineKeyboardMarkup(rows) if changed else kb


async def probe(bot, chat_id):
    """Test réel : un message avec un emoji premium et un bouton à icône, envoyé puis supprimé aussitôt."""
    if not MAP:
        return False
    emoji, cid = next(iter(MAP.items()))
    try:
        m = await bot.send_message(
            chat_id, f'<tg-emoji emoji-id="{cid}">{emoji}</tg-emoji>', parse_mode="HTML", disable_notification=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("·", callback_data="noop", api_kwargs={"icon_custom_emoji_id": cid})]]),
        )
        await bot.delete_message(chat_id, m.message_id)
        return True
    except TelegramError as e:
        log.info("Emojis premium refusés par Telegram : %s", e)
        return False


async def reprobe(bot, chat_id):
    global VERIFIED, _probed
    VERIFIED = await probe(bot, chat_id)
    _probed = True
    return VERIFIED


async def _probe_task(bot, chat_id):
    global _probing
    try:
        await reprobe(bot, chat_id)
    finally:
        _probing = False


def on_owner_seen(is_premium, app, bot, chat_id):
    """Appelé quand un admin parle au bot : détecte son Premium et vérifie (une fois) que Telegram accepte les emojis."""
    global OWNER_PREMIUM, _probing
    OWNER_PREMIUM = bool(is_premium)
    if OWNER_PREMIUM and MAP and not _probed and not _probing:
        _probing = True
        app.create_task(_probe_task(bot, chat_id))


def coverage():
    """(emojis du bot couverts par le pack, emojis différents utilisés par le bot)."""
    used = set()
    for name in dir(texts):
        v = getattr(texts, name)
        if name.isupper() and isinstance(v, str):
            used |= {_norm(m.group(0)) for m in _EMOJI.finditer(v)}
    return len(used & set(MAP)), len(used)


def status():
    if not MAP:
        return "⚪ Inactif — aucun pack choisi"
    if MODE == "off":
        return "⚪ Désactivé"
    if not OWNER_PREMIUM:
        return "⚪ En attente — ouvre le bot avec ton compte Premium"
    if not VERIFIED:
        return "⚪ Refusé ou en cours de vérification — réessaie avec le bouton Activer"
    return "🟢 Actif"
'''
_SOURCES['keyboards'] = r'''from telegram import InlineKeyboardButton as _B
from telegram import InlineKeyboardMarkup as M

import premium

_ICON = {"active": "🟢", "pending": "⏳", "lost": "⚠️"}


def B(text, data=None, style=None, url=None):
    """Bouton inline (action ou lien). style : 'primary' (bleu), 'success' (vert), 'danger' (rouge)."""
    extra = {"style": style} if style else {}
    if url:
        return _B(text, url=url, **({"api_kwargs": extra} if extra else {}))
    icon_id, text = premium.icon_and_text(text)  # emoji premium en début de bouton (si disponible)
    if icon_id:
        extra["icon_custom_emoji_id"] = icon_id
    return _B(text, callback_data=data, **({"api_kwargs": extra} if extra else {}))


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


def _nav(prefix, page, pages):
    nav = []
    if page > 0:
        nav.append(B("◀️  Précédent", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("Suivant  ▶️", f"{prefix}:{page + 1}"))
    return [nav] if nav else []


def my_channels(channels, page=0, pages=1):
    rows = [[B(f"{icon(c['status'])}  {c['title'][:35]}", f"ch:{c['id']}")] for c in channels]
    rows += _nav("mine", page, pages)
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


def admin(pending, approval=False):
    return M([
        [B("✏️  Texte", "adm_text"), B("🖼  Photo", "adm_photo")],
        [B("⏰  Planning", "adm_plan", "primary"), B("⭐  Sponsors", "adm_sp")],
        [B("🔄  Synchroniser", "adm_sync"), B(f"⏳  En attente ({pending})", "adm_pending")],
        [B("📋  Tous les canaux", "adm_all"), B("🔐  Validation : ON" if approval else "🔓  Validation : OFF", "adm_approval")],
        [B("👥  Utilisateurs", "adm_users"), B("📢  Message à tous", "adm_bc")],
        [B("💎  Emojis premium", "adm_premium")],
        [B("💾  Sauvegarde", "adm_backup"), B("♻️  Restaurer", "adm_restore")],
        [B("⬅️  Menu", "menu")],
    ])


def pending_list(channels):
    rows = [[B(f"✅  {c['title'][:25]}", f"appr:{c['id']}", "success"), B("❌", f"rej:{c['id']}", "danger")] for c in channels]
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)


def all_list(channels, page=0, pages=1):
    rows = [[B(f"🗑  {c['title'][:35]}", f"admdel:{c['id']}")] for c in channels]
    rows += _nav("adm_all", page, pages)
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)


def approve_reject(cid):
    return M([[B("✅  Accepter", f"appr:{cid}", "success"), B("❌  Refuser", f"rej:{cid}", "danger")]])


def photo_menu():
    return M([[B("🗑  Retirer la photo", "adm_photo_del", "danger")], [B("⬅️  Retour", "admin")]])


def planning(on, mode="cycle"):
    cycle = mode == "cycle"
    return M([
        [B("🔴  Désactiver le planning", "adm_plan_toggle", "danger") if on else B("🟢  Activer le planning", "adm_plan_toggle", "success")],
        [B("🔁  Mode : répétition" if cycle else "📅  Mode : horaires fixes", "adm_plan_mode")],
        [B("⏳  Durée", "adm_plan_duration"), B("⏸  Pause", "adm_plan_pause")] if cycle
        else [B("🕒  Horaires", "adm_plan_times"), B("⏳  Durée", "adm_plan_duration")],
        [B("🚀  Publier maintenant", "adm_plan_now", "success"), B("🛑  Retirer maintenant", "adm_plan_stop", "danger")],
        [B("⬅️  Retour", "admin")],
    ])


def sponsors(extras):
    rows = [[B(f"🗑  {e['label'][:35]}", f"spdel:{e['id']}")] for e in extras]
    rows.append([B("➕  Ajouter un sponsor", "adm_sp_add", "success")])
    rows.append([B("⬅️  Retour", "admin")])
    return M(rows)


def users_page(page=0, pages=1, unnamed=0):
    rows = _nav("adm_users", page, pages)
    if unnamed:
        rows.append([B(f"🔄  Actualiser les noms ({unnamed})", "adm_users_refresh", "primary")])
    return M(rows + [[B("⬅️  Retour", "admin")]])


def bc_confirm():
    return M([[B("✅  Envoyer à tous", "bc_go", "success")], [B("✖️  Annuler", "admin", "danger")]])


def unsub_menu():
    return M([[B("🔔  Réactiver les annonces", "ann_on", "success")], [B("⬅️  Menu", "menu")]])


def premium_menu(on=True):
    return M([
        [B("📦  Choisir un pack d'emojis", "adm_premium_pack", "primary")],
        [B("🔴  Désactiver", "adm_premium_mode", "danger") if on else B("🟢  Activer", "adm_premium_mode", "success")],
        [B("⬅️  Retour", "admin")],
    ])
'''
_SOURCES['ui'] = r'''"""Panneau unique : chaque étape édite le même message, les messages de l'utilisateur sont supprimés."""
from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

import premium

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def clean(update):
    if update.message:
        try:
            await update.message.delete()
        except TelegramError:
            pass


async def _show(update, context, text, kb=None):
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


async def show(update, context, text, kb=None):
    """Affiche le panneau. Avec un compte Premium détecté, les emojis deviennent premium ; si Telegram refuse, retour automatique aux emojis normaux."""
    if premium.active():
        try:
            return await _show(update, context, premium.apply(text), kb)
        except BadRequest:
            premium.disable("refusé par Telegram")
            kb = premium.plain_markup(kb)
    return await _show(update, context, text, kb)


async def ack(update, text=None, show_alert=False):
    """Répond au clic d'un bouton sans jamais planter (même si déjà répondu)."""
    q = update.callback_query
    if q:
        try:
            await q.answer(text, show_alert=show_alert)
        except TelegramError:
            pass


PAGE_SIZE = 8


def page_of(update, prefix):
    """Numéro de page demandé par un bouton « prefix:N » (0 sinon)."""
    q = update.callback_query
    data = q.data if q else ""
    if data.startswith(prefix + ":"):
        try:
            return max(0, int(data.split(":")[1]))
        except ValueError:
            return 0
    return 0


async def _fresh(update, context, text, kb=None):
    """Supprime l'ancien panneau et en envoie un nouveau tout en bas (sous le message de l'utilisateur)."""
    panel = context.user_data.pop("panel", None)
    if panel:
        try:
            await context.bot.delete_message(panel[0], panel[1])
        except TelegramError:
            pass
    msg = await context.bot.send_message(
        update.effective_chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW
    )
    context.user_data["panel"] = (msg.chat_id, msg.message_id)


async def fresh(update, context, text, kb=None):
    if premium.active():
        try:
            return await _fresh(update, context, premium.apply(text), kb)
        except BadRequest:
            premium.disable("refusé par Telegram")
            kb = premium.plain_markup(kb)
    return await _fresh(update, context, text, kb)
'''
_SOURCES['cross'] = r'''"""Coeur du bot : publication, mise à jour et suppression planifiée de la cross."""
import asyncio
import json
import logging
import math
import random
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
SEND_DELAY = 0.12  # pause entre deux envois (Telegram tolère environ 30 envois par seconde)
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


# ------------------------------------------------------------------ validation des canaux
def approval_required():
    """Validation manuelle des nouveaux canaux : réglage du bot, sinon variable REQUIRE_APPROVAL."""
    v = db.get_setting("approval")
    return config.REQUIRE_APPROVAL if v is None else v == "1"


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


def get_mode():
    return "daily" if db.get_setting("mode", config.DEFAULT_MODE) == "daily" else "cycle"


def get_pause():
    try:
        return float(db.get_setting("pause", str(config.DEFAULT_PAUSE)))
    except ValueError:
        return config.DEFAULT_PAUSE


def parse_hours(raw):
    """« 1h30 », « 90min », « 1.5 », « 2h » -> nombre d'heures (0 si illisible)."""
    t = (raw or "").lower().replace(",", ".").replace(" ", "")
    m = re.fullmatch(r"(\d+)h(\d{1,2})(?:min|m)?", t)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:min|m)", t)
    if m:
        return float(m.group(1)) / 60
    m = re.fullmatch(r"(\d+(?:\.\d+)?)h?", t)
    if m:
        return float(m.group(1))
    return 0


def fmt_hours(h):
    total = int(round(h * 60))
    hh, mm = divmod(total, 60)
    if hh and mm:
        return f"{hh} h {mm:02d} min"
    if hh:
        return f"{hh} h"
    return f"{mm} min"


def get_duration():
    try:
        return float(db.get_setting("duration", str(config.DEFAULT_DURATION)))
    except ValueError:
        return config.DEFAULT_DURATION


def _slot(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=TZ)


def next_start_str():
    raw = db.get_setting("next_start", "")
    now = datetime.now(TZ)
    try:
        dt = datetime.fromisoformat(raw).astimezone(TZ)
    except ValueError:
        return "dans un instant"
    if dt <= now:
        return "dans un instant"
    days = (dt.date() - now.date()).days
    day = "aujourd'hui" if days == 0 else "demain" if days == 1 else dt.strftime("%d/%m")
    return f"{day} à {dt:%H:%M}"


def next_slot_str():
    if get_mode() == "cycle":
        return next_start_str()
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
    db.set_setting("next_start", "")
    assign_groups()  # rotation : les groupes changent à chaque diffusion
    db.set_setting("delete_at", (datetime.now(TZ) + timedelta(hours=get_duration())).isoformat())


async def start_cycle(bot):
    set_live_state()
    db.clear_all_msgs()
    await sync_all(bot)


async def end_cycle(bot, restart_now=False):
    db.set_setting("live", "0")
    db.set_setting("delete_at", "")
    if get_mode() == "cycle":  # répétition : la prochaine diffusion arrive après la pause
        wait = timedelta(0) if restart_now else timedelta(hours=get_pause())
        db.set_setting("next_start", (datetime.now(TZ) + wait).isoformat())
    async with _lock:
        for ch in db.list_channels():
            if ch.get("cross_msg_id"):
                await remove_message(bot, ch)
                await asyncio.sleep(SEND_DELAY)
        db.clear_all_msgs()


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
        if get_mode() == "cycle":
            raw = db.get_setting("next_start", "")
            if not raw or now >= datetime.fromisoformat(raw):
                await start_cycle(bot)
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


def assign_groups():
    """Répartit les canaux actifs en groupes de GROUP_SIZE maximum.
    Au-delà d'un groupe, la composition est mélangée à chaque diffusion : les canaux changent de partenaires."""
    chans = db.list_channels("active")
    n = len(chans)
    if not n:
        return
    k = max(1, math.ceil(n / config.GROUP_SIZE))
    if k > 1:
        seed = int(db.get_setting("rotation", "0") or 0) + 1
        db.set_setting("rotation", str(seed))
        random.Random(seed).shuffle(chans)
    db.set_groups([(c["id"], i * k // n) for i, c in enumerate(chans)])


def _place_unassigned(channels):
    """Place les canaux sans groupe (nouveaux inscrits) dans le groupe le moins rempli. Retourne les groupes touchés."""
    sizes = {}
    for c in channels:
        if c.get("grp") is not None:
            sizes[c["grp"]] = sizes.get(c["grp"], 0) + 1
    touched = set()
    for c in channels:
        if c.get("grp") is None:
            free = [g for g in sizes if sizes[g] < config.GROUP_SIZE]
            g = min(free, key=lambda x: sizes[x]) if free else (max(sizes) + 1 if sizes else 0)
            sizes[g] = sizes.get(g, 0) + 1
            c["grp"] = g
            db.set_group(c["id"], g)
            touched.add(g)
    return touched


async def _sync(bot, only=None):
    """Publie / met à jour la cross. only = groupes à traiter (None = tous)."""
    if not is_live():
        return
    lost_groups = set()
    async with _lock:
        channels = db.list_channels("active")
        placed = _place_unassigned(channels)
        if only is not None:
            only = set(only) | placed
        raw_header = db.get_setting("header", config.DEFAULT_HEADER)
        extras = db.list_extras()
        photo = db.get_setting("photo") or None
        groups = {}
        for c in channels:
            groups.setdefault(c["grp"], []).append(c)
        for g in sorted(groups):
            if only is not None and g not in only:
                continue
            members = groups[g]
            markup = build_markup(bot.username, members, extras)
            header = raw_header.replace("{count}", str(len(members)))
            for ch in members:
                if ch.get("publish") == 0:  # « liste seulement » : présent en bouton, pas de publication
                    continue
                if await _push(bot, ch, header, markup, photo):
                    lost_groups.add(g)
                await asyncio.sleep(SEND_DELAY)
    if lost_groups:
        asyncio.create_task(_sync(bot, only=lost_groups))


async def sync_all(bot):
    """Met à jour tous les groupes (changement de texte, de photo, de sponsors...)."""
    await _sync(bot)


async def sync_touched(bot, groups=()):
    """Ne met à jour que les groupes concernés (nouveau canal, canal retiré...)."""
    await _sync(bot, only={g for g in groups if g is not None})


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
    await sync_touched(bot, {ch.get("grp")})


async def repost_all(bot):
    if not is_live():
        return
    for ch in db.list_channels("active"):
        if ch.get("cross_msg_id"):
            await remove_message(bot, ch)
            await asyncio.sleep(SEND_DELAY)
    db.clear_all_msgs()
    await sync_all(bot)


async def drop_message(bot, ch):
    """Supprime le message de cross d'un canal (sans le retirer de la liste)."""
    await remove_message(bot, ch)
    db.set_msg(ch["id"], None)


# ------------------------------------------------------------------ sauvegarde
def backup_file():
    data = db.export_backup()
    raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    return raw, len(data["channels"])


async def send_backup(bot, chat_id, silent=False):
    raw, n = backup_file()
    if not n:
        return False
    name = f"sauvegarde_cross_{datetime.now(TZ):%Y-%m-%d_%H%M}.json"
    await bot.send_document(
        chat_id, document=raw, filename=name, caption=texts.BACKUP_CAPTION.format(n=n),
        parse_mode=ParseMode.HTML, disable_notification=silent,
    )
    return True


async def backup_job(context):
    """Chaque nuit : envoie une sauvegarde à chaque admin (en silence)."""
    for admin_id in config.ADMIN_IDS:
        try:
            await send_backup(context.bot, admin_id, silent=True)
        except TelegramError as e:
            log.warning("Sauvegarde non envoyée à %s : %s", admin_id, e)
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

    pending = cross.approval_required() and not config.is_admin(uid)
    publish = 0 if (q.data == "confirm_nopub" and config.is_admin(uid)) else 1
    db.upsert_channel(d["id"], uid, d["title"], d["link"], d["members"], "pending" if pending else "active", publish)

    if not pending:
        context.application.create_task(cross.sync_touched(context.bot))
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
import math
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
        users=db.count_users(), active=db.count("active"), pending=pending, members=db.total_members(),
        db=db.kind_label(), approval="activée" if cross.approval_required() else "désactivée",
    )
    await ui.show(update, context, text, kb.admin(pending, cross.approval_required()))


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
async def on_approval(update, context):
    new = not cross.approval_required()
    db.set_setting("approval", "1" if new else "0")
    await ui.ack(update, "Validation activée 🔐" if new else "Validation désactivée 🔓")
    await _panel(update, context)


@admin_only
async def on_sync(update, context):
    await ui.ack(update, "Synchronisation lancée ✅")
    context.application.create_task(cross.sync_all(context.bot))


@admin_only
async def on_pending(update, context):
    await ui.ack(update)
    channels = db.list_channels("pending")[:20]
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
            context.application.create_task(cross.sync_touched(context.bot))
        else:
            db.delete_channel(ch["id"])
            await _notify(context.bot, ch["owner_id"], texts.REJECTED.format(title=title))
    await on_pending(update, context)


@admin_only
async def on_all(update, context):
    await ui.ack(update)
    total = db.count_total()
    if not total:
        return await _panel(update, context)
    pages = max(1, math.ceil(total / ui.PAGE_SIZE))
    page = min(ui.page_of(update, "adm_all"), pages - 1)
    channels = db.list_channels_page(page * ui.PAGE_SIZE, ui.PAGE_SIZE)
    text = "📋 <b>TOUS LES CANAUX</b>\n━━━━━━━━━━━━━━━\n\nTouche un canal pour le supprimer."
    text += f"\n\n📄 Page {page + 1}/{pages}  ·  {total} canaux"
    await ui.show(update, context, text, kb.all_list(channels, page, pages))


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
import json

import config, cross, db, keyboards as kb, texts, ui
from admin import _panel, admin_only

STATES = ("await_times", "await_duration", "await_pause", "await_sp_label", "await_sp_link")
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
    mode = cross.get_mode()
    if mode == "cycle":
        mode_label = "🔁 Répétition automatique"
        schedule = f"⏸ Pause entre deux diffusions : <b>{cross.fmt_hours(cross.get_pause())}</b>"
        explain = "La cross est publiée, supprimée après la durée choisie, puis revient après la pause, en boucle, pour toujours."
    else:
        mode_label = "📅 Horaires fixes"
        schedule = f"🕒 Horaires ({config.TIMEZONE}) : <b>{', '.join(cross.get_times()) or 'aucun'}</b>"
        explain = "Chaque jour, la cross est publiée toute seule à ces horaires, puis supprimée après la durée choisie."
    text = texts.PLANNING.format(
        status="🟢 Activé" if on else "🔴 Désactivé",
        mode=mode_label,
        schedule=schedule,
        duration=cross.fmt_hours(cross.get_duration()),
        live=live,
        explain=explain,
    )
    await ui.show(update, context, text, kb.planning(on, mode))


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
        context.application.create_task(cross.end_cycle(context.bot, restart_now=True))
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

    if state in ("await_duration", "await_pause"):
        hours = cross.parse_hours(text)
        if not 0.25 <= hours <= 72:
            msg = texts.ERR_DURATION if state == "await_duration" else texts.ERR_DURATION.replace("Envoie une durée", "Envoie une pause")
            return await ui.show(update, context, msg, kb.back("adm_plan"))
        db.set_setting("duration" if state == "await_duration" else "pause", str(hours))
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


@admin_only
async def on_mode(update, context):
    await ui.ack(update, "Mode changé ✅")
    db.set_setting("mode", "daily" if cross.get_mode() == "cycle" else "cycle")
    if cross.schedule_on():
        context.application.create_task(cross.end_cycle(context.bot, restart_now=True))
    await _planning(update, context)


@admin_only
async def on_pause_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_pause"
    await ui.show(update, context, texts.PAUSE_ASK, kb.back("adm_plan"))


# ------------------------------------------------------------------ sauvegarde / restauration
@admin_only
async def on_backup(update, context):
    sent = await cross.send_backup(context.bot, update.effective_user.id)
    await ui.ack(update, "Sauvegarde envoyée 💾" if sent else "Aucun canal à sauvegarder", show_alert=not sent)


@admin_only
async def on_restore_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_backup"
    await ui.show(update, context, texts.RESTORE_ASK, kb.back("admin"))


async def handle_backup(update, context):
    doc = update.message.document
    await ui.clean(update)
    if not doc or (doc.file_size or 0) > 5_000_000:
        return await ui.show(update, context, texts.ERR_BACKUP, kb.back("admin"))
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = json.loads(bytes(await tg_file.download_as_bytearray()).decode("utf-8"))
        n_ch, n_ex = db.restore_backup(data)
    except Exception:
        return await ui.show(update, context, texts.ERR_BACKUP, kb.back("admin"))
    context.user_data.pop("state", None)
    context.application.create_task(cross.sync_all(context.bot))
    await ui.show(update, context, texts.RESTORED.format(n=n_ch, s=n_ex), kb.back("admin"))
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
    BotCommand("stop", "🔕 Ne plus recevoir les annonces"),
]
ADMIN = USER + [
    BotCommand("admin", "🛠 Panneau admin"),
    BotCommand("planning", "⏰ Planning de diffusion"),
    BotCommand("sponsors", "⭐ Gérer les sponsors"),
    BotCommand("publier", "🚀 Publier maintenant"),
    BotCommand("retirer", "🛑 Retirer maintenant"),
    BotCommand("sauvegarde", "💾 Sauvegarder les canaux"),
    BotCommand("restaurer", "♻️ Restaurer une sauvegarde"),
    BotCommand("broadcast", "📢 Message à tous les utilisateurs"),
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
import math

import config, cross, db, keyboards as kb, texts, ui

_STATUS = {"active": "🟢 Actif", "pending": "⏳ En attente", "lost": "⚠️ Bot retiré"}


def _owned(ch, uid):
    return ch and (ch["owner_id"] == uid or config.is_admin(uid))


async def on_mine(update, context):
    await ui.ack(update)
    await show_mine(update, context, ui.page_of(update, "mine"))


async def show_mine(update, context, page=0):
    allc = db.owner_channels(update.effective_user.id)
    if not allc:
        return await ui.show(update, context, texts.NO_CHANNELS, kb.back())
    pages = max(1, math.ceil(len(allc) / ui.PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = allc[page * ui.PAGE_SIZE:(page + 1) * ui.PAGE_SIZE]
    text = texts.MINE
    if pages > 1:
        text += f"\n\n📄 Page {page + 1}/{pages}  ·  {len(allc)} canaux"
    await ui.show(update, context, text, kb.my_channels(chunk, page, pages))


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
        context.application.create_task(cross.sync_touched(context.bot, {ch.get("grp")}))
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
_SOURCES['users'] = r'''"""Utilisateurs du bot (qui l'utilise) et message à tous."""
import asyncio
import html
import logging
import math
import time

from telegram.error import Forbidden, RetryAfter, TelegramError

import config, db, keyboards as kb, premium, texts, ui
from admin import admin_only

log = logging.getLogger(__name__)
PER_PAGE = 8


# ------------------------------------------------------------------ suivi des utilisateurs
def _full_name(first, last):
    return " ".join(x for x in (first, last) if x) or None


async def track(update, context):
    """Enregistre en silence toute personne qui parle au bot (démarrage, bouton, message)."""
    u = update.effective_user
    chat = update.effective_chat
    if not u or u.is_bot or not chat or chat.type != "private":
        return
    if config.is_admin(u.id):  # compte Premium détecté -> emojis premium (si un pack est choisi)
        premium.on_owner_seen(bool(getattr(u, "is_premium", False)), context.application, context.bot, chat.id)
    key = (_full_name(u.first_name, u.last_name), u.username)
    seen = context.application.bot_data.setdefault("seen", {})
    prev = seen.get(u.id)
    now = time.time()
    if prev and prev[0] == key and now - prev[1] < 600:  # pas plus d'une écriture / 10 min par personne
        return
    try:
        db.touch_user(u.id, key[0], key[1])
        seen[u.id] = (key, now)
    except Exception:
        log.exception("Enregistrement utilisateur impossible")


async def backfill_owners(bot):
    """Au démarrage : ajoute à la liste les propriétaires de canaux déjà inscrits (nom et @ retrouvés auprès de Telegram)."""
    try:
        ids = db.owners_missing()
    except Exception:
        log.exception("Recherche des propriétaires impossible")
        return
    for uid in ids:
        try:
            chat = await bot.get_chat(uid)
            db.touch_user(uid, _full_name(chat.first_name, chat.last_name), chat.username)
        except TelegramError:
            db.touch_user(uid, None, None)  # au minimum, l'identifiant est enregistré
        except Exception:
            log.exception("Propriétaire %s non ajouté", uid)
        await asyncio.sleep(0.1)


# ------------------------------------------------------------------ section 👥 Utilisateurs
def _date(value):
    s = str(value or "")
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}" if len(s) >= 10 else "?"


@admin_only
async def on_users(update, context):
    await ui.ack(update)
    await _users_view(update, context)


async def _users_view(update, context):
    stats = db.users_stats()
    total = stats["total"]
    if not total:
        return await ui.show(update, context, texts.USERS_EMPTY, kb.users_page())
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(ui.page_of(update, "adm_users"), pages - 1)
    items = []
    for u in db.list_users_page(page * PER_PAGE, PER_PAGE):
        titles = [html.escape(c["title"]) for c in db.owner_channels(u["id"])]
        if titles:
            chans = "📣 " + ", ".join(titles[:3]) + (f" (+{len(titles) - 3})" if len(titles) > 3 else "")
        else:
            chans = "aucun canal"
        items.append(texts.USER_ITEM.format(
            name=html.escape(u.get("name") or "Sans nom"),
            username=f"@{html.escape(u['username'])}" if u.get("username") else "",
            id=u["id"], date=_date(u.get("first_seen")), chans=chans,
        ))
    text = texts.USERS.format(
        total=total, new=stats["new"], blocked=stats["blocked"], optout=stats["optout"],
        items="\n\n".join(items), page=page + 1, pages=pages,
    )
    await ui.show(update, context, text, kb.users_page(page, pages, stats["unnamed"]))


# ------------------------------------------------------------------ message à tous
async def ask_broadcast(update, context):
    n = db.count_reachable()
    if not n:
        return await ui.show(update, context, texts.BC_NONE, kb.back("admin"))
    context.user_data["state"] = "await_broadcast"
    await ui.show(update, context, texts.BC_ASK.format(n=n), kb.back("admin"))


@admin_only
async def on_bc_ask(update, context):
    await ui.ack(update)
    await ask_broadcast(update, context)


async def handle_broadcast(update, context):
    """Le message de l'admin (texte, photo...) sert d'aperçu : on le garde et on demande confirmation."""
    msg = update.message
    n = db.count_reachable()
    context.user_data.pop("state", None)
    if not n:
        return await ui.show(update, context, texts.BC_NONE, kb.back("admin"))
    context.user_data["bc"] = (msg.chat_id, msg.message_id)
    await ui.fresh(update, context, texts.BC_CONFIRM.format(n=n), kb.bc_confirm())


@admin_only
async def on_bc_go(update, context):
    draft = context.user_data.pop("bc", None)
    if not draft:
        await ui.ack(update, "Message introuvable, recommence avec /broadcast", show_alert=True)
        return
    await ui.ack(update, "Envoi lancé 📤")
    context.application.create_task(run_broadcast(context.bot, update.effective_user.id, draft))
    await ui.show(update, context, texts.BC_SENDING, kb.back("admin"))


async def run_broadcast(bot, admin_id, draft):
    """Copie le message vers chaque utilisateur (environ 20 par seconde) puis envoie un bilan."""
    ok = blocked = failed = 0
    for uid in db.list_reachable_ids():
        for _ in range(2):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=draft[0], message_id=draft[1])
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                db.mark_blocked(uid)
                blocked += 1
                break
            except TelegramError:
                failed += 1
                break
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_id, texts.BC_DONE.format(ok=ok, blocked=blocked, failed=failed), parse_mode="HTML")
    except TelegramError:
        pass
    return ok, blocked, failed


# ------------------------------------------------------------------ /stop : ne plus recevoir les annonces
async def cmd_unsubscribe(update, context):
    context.user_data.pop("state", None)
    db.add_user(update.effective_user.id)
    db.set_optout(update.effective_user.id, 1)
    await ui.clean(update)
    await ui.show(update, context, texts.UNSUB, kb.unsub_menu())


async def on_ann_on(update, context):
    await ui.ack(update, "Annonces réactivées 🔔")
    db.add_user(update.effective_user.id)
    db.set_optout(update.effective_user.id, 0)
    await ui.show(update, context, texts.RESUB, kb.back("menu"))


# ------------------------------------------------------------------ actualiser les noms des « Sans nom »
async def refresh_names(bot, ids):
    """Demande à Telegram le nom et le @ de chaque identifiant. Retourne le nombre de noms récupérés."""
    ok = 0
    for uid in ids:
        try:
            chat = await bot.get_chat(uid)
            name = _full_name(chat.first_name, chat.last_name)
            db.set_profile(uid, name, chat.username)
            ok += 1 if name else 0
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError:
            pass  # n'a jamais écrit au bot : Telegram ne donne rien
        await asyncio.sleep(0.1)
    return ok


async def _refresh_report(bot, admin_id, ids):
    ok = await refresh_names(bot, ids)
    try:
        await bot.send_message(admin_id, texts.NAMES_DONE.format(ok=ok, n=len(ids)), parse_mode="HTML")
    except TelegramError:
        pass


@admin_only
async def on_users_refresh(update, context):
    ids = db.users_without_name()
    if not ids:
        return await ui.ack(update, "Tous les noms sont déjà à jour ✅", show_alert=True)
    if len(ids) > 60:  # beaucoup de monde : en arrière-plan, avec un bilan à la fin
        await ui.ack(update, "Actualisation lancée, tu recevras un bilan 📤", show_alert=True)
        context.application.create_task(_refresh_report(context.bot, update.effective_user.id, ids))
        return
    ok = await refresh_names(context.bot, ids)
    await ui.ack(update, f"{ok} noms récupérés sur {len(ids)} ✅")
    await _users_view(update, context)
'''
_SOURCES['commands'] = r'''"""Commandes du menu : ouvrent directement le bon écran."""
import functools

import add_channel, admin, admin_tools, config, cross, db, keyboards as kb, my_channels, texts, ui, users


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


@_admin_cmd
async def cmd_backup(update, context):
    sent = await cross.send_backup(context.bot, update.effective_user.id)
    if not sent:
        await ui.show(update, context, texts.NO_BACKUP, kb.back("admin"))


@_admin_cmd
async def cmd_restore(update, context):
    context.user_data["state"] = "await_backup"
    await ui.show(update, context, texts.RESTORE_ASK, kb.back("admin"))


@_admin_cmd
async def cmd_broadcast(update, context):
    await users.ask_broadcast(update, context)
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
_SOURCES['premium_admin'] = r'''"""Écran admin « 💎 Emojis premium » : choix du pack d'emojis et activation."""
import html
import json
import re

from telegram.error import TelegramError

import config, db, keyboards as kb, premium, texts, ui
from admin import admin_only


async def _view(update, context):
    context.user_data.pop("state", None)
    covered, used = premium.coverage()
    text = texts.PREMIUM.format(
        status=premium.status(), pack=html.escape(premium.PACK) if premium.PACK else "aucun",
        cov=f"{covered} sur {used} emojis du bot",
    )
    await ui.show(update, context, text, kb.premium_menu(premium.MODE != "off"))


@admin_only
async def on_premium(update, context):
    await ui.ack(update)
    await _view(update, context)


@admin_only
async def on_premium_mode(update, context):
    new_off = premium.MODE != "off"
    premium.MODE = "off" if new_off else "auto"
    db.set_setting("premium_mode", premium.MODE)
    await ui.ack(update, "Emojis premium désactivés" if new_off else "Emojis premium activés 💎")
    if not new_off and premium.MAP:
        await premium.reprobe(context.bot, update.effective_user.id)
    await _view(update, context)


@admin_only
async def on_pack_ask(update, context):
    await ui.ack(update)
    context.user_data["state"] = "await_emojipack"
    await ui.show(update, context, texts.PREMIUM_PACK_ASK, kb.back("adm_premium"))


async def handle_pack(update, context):
    raw = (update.message.text or "").strip()
    await ui.clean(update)
    m = re.search(r"(?:addemoji|addstickers)/([A-Za-z0-9_]+)", raw) or re.fullmatch(r"@?([A-Za-z0-9_]{3,})", raw)
    if not m:
        return await ui.show(update, context, texts.PREMIUM_ERR, kb.back("adm_premium"))
    name = m.group(1)
    try:
        pack = await context.bot.get_sticker_set(name)
        if getattr(pack, "sticker_type", None) != "custom_emoji":
            raise ValueError("pas un pack d'emojis")
        mapping = {}
        for st in pack.stickers:
            if st.custom_emoji_id and st.emoji:
                mapping.setdefault(st.emoji, st.custom_emoji_id)
        if not mapping:
            raise ValueError("pack vide")
    except (TelegramError, ValueError):
        return await ui.show(update, context, texts.PREMIUM_ERR, kb.back("adm_premium"))
    db.set_setting("emoji_pack", name)
    db.set_setting("emoji_map", json.dumps(mapping, ensure_ascii=False))
    premium.PACK = name
    premium.set_map(mapping)
    if premium.OWNER_PREMIUM:
        await premium.reprobe(context.bot, update.effective_user.id)
    await _view(update, context)
'''
_SOURCES['router'] = r'''import config, ui
import add_channel, admin, admin_tools, premium_admin, users


async def on_message(update, context):
    state = context.user_data.get("state")
    if state == "await_channel":
        await add_channel.handle_input(update, context)
    elif state == "await_header" and config.is_admin(update.effective_user.id):
        await admin.handle_header(update, context)
    elif state == "await_photo" and config.is_admin(update.effective_user.id):
        await admin.handle_photo(update, context)
    elif state == "await_emojipack" and config.is_admin(update.effective_user.id):
        await premium_admin.handle_pack(update, context)
    elif state == "await_broadcast" and config.is_admin(update.effective_user.id):
        await users.handle_broadcast(update, context)
    elif state == "await_backup" and config.is_admin(update.effective_user.id):
        await admin_tools.handle_backup(update, context)
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
from datetime import time as dtime

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

import botcommands, config, cross, db, health, premium, texts
import add_channel, admin, admin_tools, commands, my_channels, premium_admin, router, start, users

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# Les journaux HTTP affichent l'adresse complète des requêtes Telegram, qui contient le token : on les masque.
for _noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("crossbot")


async def post_init(app: Application):
    db.init()
    premium.load()
    await botcommands.setup(app.bot)
    log.info("Base de données : %s", db.kind_label())
    if db.is_temporary():
        log.warning("BASE TEMPORAIRE : ajoute DATABASE_URL (Neon) sur Render, sinon les canaux seront perdus.")
        for admin_id in config.ADMIN_IDS:
            try:
                await app.bot.send_message(admin_id, texts.WARN_TEMP_DB.format(names=", ".join(config.DB_HINT_NAMES) or "aucune"), parse_mode="HTML")
            except Exception:
                pass
    app.create_task(users.backfill_owners(app.bot))  # retrouve le nom des propriétaires de canaux déjà inscrits
    log.info("Bot démarré : @%s", app.bot.username)


async def on_error(update, context):
    log.error("Erreur non gérée", exc_info=context.error)


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN manquant")

    health.start(config.PORT)
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, users.track), group=-1)  # enregistre qui utilise le bot (sans aucune notification)
    app.add_handler(CommandHandler("start", start.cmd_start, filters=private))

    for name, fn in [
        ("ajouter", commands.cmd_add), ("canaux", commands.cmd_mine), ("aide", commands.cmd_help),
        ("admin", commands.cmd_admin), ("planning", commands.cmd_planning), ("sponsors", commands.cmd_sponsors),
        ("publier", commands.cmd_publish), ("retirer", commands.cmd_stop),
        ("sauvegarde", commands.cmd_backup), ("restaurer", commands.cmd_restore),
        ("broadcast", commands.cmd_broadcast), ("baoudaucast", commands.cmd_broadcast),
        ("stop", users.cmd_unsubscribe),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=private))

    cb = CallbackQueryHandler
    app.add_handler(cb(start.on_menu, pattern=r"^(menu|help)$"))
    app.add_handler(cb(add_channel.on_add, pattern=r"^add$"))
    app.add_handler(cb(add_channel.on_confirm, pattern=r"^confirm_(add|nopub)$"))
    app.add_handler(cb(my_channels.on_mine, pattern=r"^mine(:\d+)?$"))
    app.add_handler(cb(my_channels.on_channel, pattern=r"^ch:-?\d+$"))
    app.add_handler(cb(my_channels.on_toggle_publish, pattern=r"^pub:-?\d+$"))
    app.add_handler(cb(my_channels.on_delete_ask, pattern=r"^del:-?\d+$"))
    app.add_handler(cb(my_channels.on_delete_ok, pattern=r"^delok:-?\d+$"))

    app.add_handler(cb(admin.on_admin, pattern=r"^admin$"))
    app.add_handler(cb(admin.on_header_ask, pattern=r"^adm_text$"))
    app.add_handler(cb(admin.on_photo_ask, pattern=r"^adm_photo$"))
    app.add_handler(cb(admin.on_photo_del, pattern=r"^adm_photo_del$"))
    app.add_handler(cb(admin.on_sync, pattern=r"^adm_sync$"))
    app.add_handler(cb(admin.on_approval, pattern=r"^adm_approval$"))
    app.add_handler(cb(users.on_ann_on, pattern=r"^ann_on$"))
    app.add_handler(cb(premium_admin.on_premium, pattern=r"^adm_premium$"))
    app.add_handler(cb(premium_admin.on_premium_mode, pattern=r"^adm_premium_mode$"))
    app.add_handler(cb(premium_admin.on_pack_ask, pattern=r"^adm_premium_pack$"))
    app.add_handler(cb(users.on_users_refresh, pattern=r"^adm_users_refresh$"))
    app.add_handler(cb(users.on_users, pattern=r"^adm_users(:\d+)?$"))
    app.add_handler(cb(users.on_bc_ask, pattern=r"^adm_bc$"))
    app.add_handler(cb(users.on_bc_go, pattern=r"^bc_go$"))
    app.add_handler(cb(admin_tools.on_backup, pattern=r"^adm_backup$"))
    app.add_handler(cb(admin_tools.on_restore_ask, pattern=r"^adm_restore$"))
    app.add_handler(cb(admin_tools.on_planning, pattern=r"^adm_plan$"))
    app.add_handler(cb(admin_tools.on_toggle, pattern=r"^adm_plan_toggle$"))
    app.add_handler(cb(admin_tools.on_mode, pattern=r"^adm_plan_mode$"))
    app.add_handler(cb(admin_tools.on_pause_ask, pattern=r"^adm_plan_pause$"))
    app.add_handler(cb(admin_tools.on_times_ask, pattern=r"^adm_plan_times$"))
    app.add_handler(cb(admin_tools.on_duration_ask, pattern=r"^adm_plan_duration$"))
    app.add_handler(cb(admin_tools.on_now, pattern=r"^adm_plan_now$"))
    app.add_handler(cb(admin_tools.on_stop, pattern=r"^adm_plan_stop$"))
    app.add_handler(cb(admin_tools.on_sponsors, pattern=r"^adm_sp$"))
    app.add_handler(cb(admin_tools.on_sp_add, pattern=r"^adm_sp_add$"))
    app.add_handler(cb(admin_tools.on_sp_del, pattern=r"^spdel:\d+$"))
    app.add_handler(cb(admin.on_pending, pattern=r"^adm_pending$"))
    app.add_handler(cb(admin.on_decision, pattern=r"^(appr|rej):-?\d+$"))
    app.add_handler(cb(admin.on_all, pattern=r"^adm_all(:\d+)?$"))
    app.add_handler(cb(admin.on_admin_delete, pattern=r"^admdel:-?\d+$"))

    app.add_handler(MessageHandler(private & ~filters.COMMAND, router.on_message))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(cross.tick, interval=60, first=15)
    app.job_queue.run_daily(cross.backup_job, time=dtime(3, 0, tzinfo=cross.TZ))  # sauvegarde chaque nuit à 03:00

    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
'''

for _name in ['config', 'db', 'texts', 'premium', 'keyboards', 'ui', 'cross', 'add_channel', 'admin', 'admin_tools', 'botcommands', 'my_channels', 'users', 'commands', 'health', 'premium_admin', 'router', 'start', 'botmain']:
    _mod = types.ModuleType(_name)
    sys.modules[_name] = _mod
    exec(compile(_SOURCES[_name], _name + ".py", "exec"), _mod.__dict__)

if __name__ == "__main__":
    sys.modules["botmain"].main()
