from flask import Flask, request, redirect, url_for, session, render_template_string
import sqlite3
import os
import subprocess
import threading
import time
import shutil
import unicodedata
import sys
from datetime import datetime

try:
    from escpos.printer import Usb
except Exception:
    Usb = None

# =========================================================
# CONFIG
# =========================================================

VERSION = "1.3.0"          # à augmenter à chaque mise à jour publiée sur GitHub

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "caisse_concours_tickets.db")
PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")

def charger_cle_secrete():
    """Clé secrète propre à chaque caisse, créée au premier démarrage (jamais dans le dépôt Git)."""
    import secrets
    chemin = os.path.join(BASE_DIR, "secret_key.txt")
    try:
        with open(chemin, encoding="utf-8") as f:
            cle = f.read().strip()
        if len(cle) >= 32:
            return cle
    except OSError:
        pass
    cle = secrets.token_hex(32)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(cle)
    try:
        os.chmod(chemin, 0o600)
    except OSError:
        pass
    return cle


app.secret_key = charger_cle_secrete()

POS_VENDOR_ID = 0x0416
POS_PRODUCT_ID = 0x5011
POS_INTERFACE = 0
POS_IN_EP = 0x82
POS_OUT_EP = 0x01

# Billets proposés sur l'écran de paiement en mode ARGENT
BILLETS = [5, 10, 20, 50]

# =========================================================
# SQLITE
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(table, column):
    conn = db()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return any(r["name"] == column for r in rows)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            prix INTEGER NOT NULL DEFAULT 0,
            categorie TEXT DEFAULT 'Divers',
            couleur TEXT DEFAULT '#2d89ef',
            jetons_rouges INTEGER DEFAULT 0,
            jetons_bleus INTEGER DEFAULT 0,
            prix_euro INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ventes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            total INTEGER,
            paiement TEXT DEFAULT 'JETONS',
            recu INTEGER DEFAULT 0,
            rendu INTEGER DEFAULT 0,
            operateur TEXT DEFAULT '',
            annulee INTEGER DEFAULT 0,
            mode TEXT DEFAULT 'JETONS'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS lignes_vente (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vente_id INTEGER,
            article TEXT,
            prix INTEGER,
            quantite INTEGER DEFAULT 1,
            jetons_rouges INTEGER DEFAULT 0,
            jetons_bleus INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS parametres (
            cle TEXT PRIMARY KEY,
            valeur TEXT
        )
    """)

    migrations = [
        ("articles", "categorie", "ALTER TABLE articles ADD COLUMN categorie TEXT DEFAULT 'Divers'"),
        ("articles", "couleur", "ALTER TABLE articles ADD COLUMN couleur TEXT DEFAULT '#2d89ef'"),
        ("articles", "jetons_rouges", "ALTER TABLE articles ADD COLUMN jetons_rouges INTEGER DEFAULT 0"),
        ("articles", "jetons_bleus", "ALTER TABLE articles ADD COLUMN jetons_bleus INTEGER DEFAULT 0"),
        ("articles", "prix_euro", "ALTER TABLE articles ADD COLUMN prix_euro INTEGER DEFAULT 0"),
        ("ventes", "paiement", "ALTER TABLE ventes ADD COLUMN paiement TEXT DEFAULT 'JETONS'"),
        ("ventes", "recu", "ALTER TABLE ventes ADD COLUMN recu INTEGER DEFAULT 0"),
        ("ventes", "rendu", "ALTER TABLE ventes ADD COLUMN rendu INTEGER DEFAULT 0"),
        ("ventes", "operateur", "ALTER TABLE ventes ADD COLUMN operateur TEXT DEFAULT ''"),
        ("ventes", "annulee", "ALTER TABLE ventes ADD COLUMN annulee INTEGER DEFAULT 0"),
        ("ventes", "mode", "ALTER TABLE ventes ADD COLUMN mode TEXT DEFAULT 'JETONS'"),
        ("lignes_vente", "quantite", "ALTER TABLE lignes_vente ADD COLUMN quantite INTEGER DEFAULT 1"),
        ("lignes_vente", "jetons_rouges", "ALTER TABLE lignes_vente ADD COLUMN jetons_rouges INTEGER DEFAULT 0"),
        ("lignes_vente", "jetons_bleus", "ALTER TABLE lignes_vente ADD COLUMN jetons_bleus INTEGER DEFAULT 0"),
    ]

    for table, col, sql in migrations:
        try:
            if not column_exists(table, col):
                cur.execute(sql)
        except Exception as e:
            print("Erreur migration", table, col, ":", e)

    # Ancien mode TICKETS -> mode JETONS (le prix en jetons = rouges + bleus)
    cur.execute("UPDATE ventes SET mode = 'JETONS' WHERE mode = 'TICKETS'")
    cur.execute("UPDATE ventes SET paiement = 'JETONS' WHERE paiement = 'TICKETS'")
    cur.execute("UPDATE parametres SET valeur = 'JETONS' WHERE cle = 'mode' AND valeur = 'TICKETS'")
    cur.execute("UPDATE articles SET prix = COALESCE(jetons_rouges, 0) + COALESCE(jetons_bleus, 0)")

    cur.execute("SELECT COUNT(*) FROM articles")
    if cur.fetchone()[0] == 0:
        articles = [
            # nom, jetons (rouges + bleus), catégorie, couleur, rouges, bleus, prix en centimes
            ("Café", 1, "Boissons", "#2d89ef", 1, 0, 150),
            ("Eau", 1, "Boissons", "#3498db", 0, 1, 150),
            ("Soft", 2, "Boissons", "#2980b9", 1, 1, 200),
            ("Pain saucisse", 4, "Nourriture", "#e67e22", 2, 2, 400),
            ("Sandwich", 3, "Nourriture", "#d35400", 2, 1, 350),
            ("Repas", 10, "Repas", "#27ae60", 5, 5, 1500),
        ]
        cur.executemany("""
            INSERT INTO articles (nom, prix, categorie, couleur, jetons_rouges, jetons_bleus, prix_euro)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, articles)

    conn.commit()
    conn.close()

# =========================================================
# PARAMETRES / MODE
# =========================================================

def get_param(cle, defaut=""):
    conn = db()
    row = conn.execute("SELECT valeur FROM parametres WHERE cle = ?", (cle,)).fetchone()
    conn.close()
    return row["valeur"] if row else defaut


def set_param(cle, valeur):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
    conn.commit()
    conn.close()


def get_mode():
    mode = get_param("mode", "JETONS")
    return mode if mode in ("JETONS", "ARGENT") else "JETONS"


# Moyens de paiement électroniques activables (les espèces sont toujours actives)
OPTIONS_PAIEMENT = ("pay_carte", "pay_payconiq", "print_journal", "tiroir_actif")


OPTIONS_PAR_DEFAUT_NON = ("tiroir_actif",)   # désactivées tant qu'on ne les allume pas


def option_active(cle):
    defaut = "0" if cle in OPTIONS_PAR_DEFAUT_NON else "1"
    return get_param(cle, defaut) == "1"


def get_fond_caisse():
    try:
        return int(get_param("fond_caisse", "0"))
    except ValueError:
        return 0

# =========================================================
# FORMATAGE MONTANTS
# =========================================================
# En mode JETONS, le client paie en jetons rouges et bleus ; les montants
# stockés sont le nombre total de jetons (rouges + bleus).
# En mode ARGENT, les montants sont stockés en CENTIMES (150 = 1,50 €).

def euros(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",") + " €"


@app.template_filter("montant")
def fmt_montant(valeur, mode="JETONS", rouges=None, bleus=None):
    v = int(valeur or 0)
    if mode == "ARGENT":
        return euros(v)
    if rouges is not None and bleus is not None:
        return f"🔴 {int(rouges or 0)}  🔵 {int(bleus or 0)}"
    return f"{v} jeton{'s' if v > 1 else ''}"


@app.template_filter("euros_input")
def fmt_euros_input(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",")


def parse_euros(texte):
    try:
        t = str(texte).strip().replace("€", "").replace(",", ".")
        return int(round(float(t) * 100))
    except ValueError:
        return 0


def montant_txt(valeur, mode, rouges=None, bleus=None):
    """Version imprimante thermique (pas de symbole €)."""
    v = int(valeur or 0)
    if mode == "ARGENT":
        return f"{v / 100:.2f} EUR".replace(".", ",")
    if rouges is not None and bleus is not None:
        return f"{int(rouges or 0)}R {int(bleus or 0)}B"
    return f"{v} J"


def sans_accents(texte):
    return unicodedata.normalize("NFKD", str(texte)).encode("ascii", "ignore").decode("ascii")

# =========================================================
# PASSWORD / BACKUP / NAVIGATEUR
# =========================================================

def read_password():
    if not os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
            f.write("1234")
        return "1234"
    with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def write_password(new_password):
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(new_password.strip())


def backup_db():
    try:
        if not os.path.exists(DB_FILE):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, f"caisse_backup_{stamp}.db")
        shutil.copy2(DB_FILE, dst)
    except Exception as e:
        print("Erreur backup :", e)

# =========================================================
# RESUME DES VENTES (stats / fin de service / impression)
# =========================================================

def resume_ventes():
    conn = db()

    jetons_vente = """
        LEFT JOIN (SELECT vente_id,
                          SUM(jetons_rouges * quantite) AS r,
                          SUM(jetons_bleus * quantite) AS b
                   FROM lignes_vente GROUP BY vente_id) j ON j.vente_id = v.id
    """

    par_mode = conn.execute(f"""
        SELECT v.mode, COUNT(*) AS nb, COALESCE(SUM(v.total), 0) AS total,
               COALESCE(SUM(j.r), 0) AS rouges, COALESCE(SUM(j.b), 0) AS bleus
        FROM ventes v {jetons_vente} WHERE v.annulee = 0
        GROUP BY v.mode ORDER BY v.mode DESC
    """).fetchall()

    par_paiement = conn.execute(f"""
        SELECT v.mode, v.paiement, COUNT(*) AS nb, COALESCE(SUM(v.total), 0) AS total,
               COALESCE(SUM(j.r), 0) AS rouges, COALESCE(SUM(j.b), 0) AS bleus
        FROM ventes v {jetons_vente} WHERE v.annulee = 0
        GROUP BY v.mode, v.paiement ORDER BY v.mode DESC, v.paiement
    """).fetchall()

    jetons = conn.execute("""
        SELECT COALESCE(SUM(l.jetons_rouges * l.quantite), 0) AS rouges,
               COALESCE(SUM(l.jetons_bleus * l.quantite), 0) AS bleus
        FROM lignes_vente l JOIN ventes v ON v.id = l.vente_id
        WHERE v.annulee = 0
    """).fetchone()

    articles = conn.execute("""
        SELECT v.mode, l.article,
               SUM(l.quantite) AS quantite,
               SUM(l.prix * l.quantite) AS total,
               SUM(l.jetons_rouges * l.quantite) AS rouges,
               SUM(l.jetons_bleus * l.quantite) AS bleus
        FROM lignes_vente l JOIN ventes v ON v.id = l.vente_id
        WHERE v.annulee = 0
        GROUP BY v.mode, l.article ORDER BY v.mode DESC, l.article
    """).fetchall()

    top = conn.execute("""
        SELECT l.article, SUM(l.quantite) AS qte
        FROM lignes_vente l JOIN ventes v ON v.id = l.vente_id
        WHERE v.annulee = 0 GROUP BY l.article ORDER BY qte DESC LIMIT 1
    """).fetchone()

    especes = conn.execute("""
        SELECT COALESCE(SUM(total), 0) AS total FROM ventes
        WHERE annulee = 0 AND mode = 'ARGENT' AND paiement = 'ESPECES'
    """).fetchone()["total"]

    conn.close()

    fond = get_fond_caisse()

    return {
        "fond": fond,
        "especes": especes,
        "attendu": fond + especes,
        "argent_utilise": fond > 0 or any(m["mode"] == "ARGENT" for m in par_mode),
        "par_mode": par_mode,
        "par_paiement": par_paiement,
        "jetons": jetons,
        "articles": articles,
        "top": top,
        "nb_ventes": sum(int(m["nb"]) for m in par_mode),
    }


IMPRIMANTE_LOCK = threading.Lock()
ERREUR_JOURNAL = {"texte": ""}


def ouvrir_imprimante():
    if Usb is None:
        raise RuntimeError("Module python-escpos manquant. Installer avec : pip install python-escpos pyusb")
    try:
        return Usb(POS_VENDOR_ID, POS_PRODUCT_ID, interface=POS_INTERFACE, in_ep=POS_IN_EP, out_ep=POS_OUT_EP)
    except TypeError:
        return Usb(POS_VENDOR_ID, POS_PRODUCT_ID)


def fermer_imprimante(printer):
    try:
        printer.close()
    except Exception:
        pass


def montant_court(centimes):
    return f"{int(centimes or 0) / 100:.2f}".replace(".", ",")


def lignes_journal_vente(vente_id):
    """Bloc compact (32 colonnes) d'une vente, pour le journal papier."""
    conn = db()
    v = conn.execute("SELECT * FROM ventes WHERE id = ?", (vente_id,)).fetchone()
    lignes = conn.execute("SELECT * FROM lignes_vente WHERE vente_id = ?", (vente_id,)).fetchall()
    conn.close()
    if not v:
        return []

    heure = str(v["date"])[11:16]
    out = [f"#{v['id']:<5} {heure}  {v['paiement']:>18}"[:32]]
    for l in lignes:
        nom = sans_accents(l["article"])[:17]
        q = int(l["quantite"] or 1)
        out.append(f"{nom:<17} x{q:<3}{montant_court(l['prix'] * q):>9}"[:32])
    out.append(f"{'TOTAL':<20}{montant_court(v['total']):>8} EUR"[:32])
    if v["paiement"] == "ESPECES" and int(v["rendu"] or 0) > 0:
        out.append(f"Recu {montant_court(v['recu'])}  Rendu {montant_court(v['rendu'])}"[:32])
    out.append("-" * 32)
    return out


def imprimer_journal(lignes, gras_premiere=True):
    """Imprime des lignes sans couper ni avancer le papier."""
    if not lignes:
        return
    with IMPRIMANTE_LOCK:
        try:
            printer = ouvrir_imprimante()
            try:
                for i, ligne in enumerate(lignes):
                    printer.set(align="left", bold=(gras_premiere and i == 0), width=1, height=1)
                    printer.text(sans_accents(ligne) + "\n")
            finally:
                fermer_imprimante(printer)
            ERREUR_JOURNAL["texte"] = ""
        except Exception as e:
            ERREUR_JOURNAL["texte"] = str(e)
            print("Erreur impression journal :", e)


def journal_en_arriere_plan(lignes):
    """La caisse n'attend pas l'imprimante."""
    threading.Thread(target=imprimer_journal, args=(lignes,), daemon=True).start()


def journal_actif():
    return option_active("print_journal")


def imprimer_fin_service():
    r = resume_ventes()

    with IMPRIMANTE_LOCK:
        printer = ouvrir_imprimante()
        try:
            _imprimer_bilan(printer, r)
        finally:
            fermer_imprimante(printer)


def _imprimer_bilan(printer, r):
    def p(texte):
        printer.text(sans_accents(texte))

    sep = "--------------------------------\n"

    printer.set(align="center", bold=True, width=2, height=2)
    p("FIN SERVICE\n")

    printer.set(align="center", bold=False, width=1, height=1)
    p("CONCOURS TIR\n")
    p(f"Caisse bar v{VERSION}\n")
    p(datetime.now().strftime("%d/%m/%Y %H:%M:%S") + "\n")
    p(sep)

    if not r["par_mode"]:
        p("Aucune vente\n")

    for m in r["par_mode"]:
        mode = m["mode"]
        printer.set(align="left", bold=True, width=1, height=1)
        p(f"MODE {mode}\n")
        printer.set(bold=False)
        p(f"Nombre ventes : {m['nb']}\n")
        p(f"Total : {montant_txt(m['total'], mode, m['rouges'], m['bleus'])}\n")

        if mode == "ARGENT":
            for pp in r["par_paiement"]:
                if pp["mode"] == mode:
                    p(f"  {pp['paiement']:<10}{pp['nb']:>4} {montant_txt(pp['total'], mode):>14}\n")

        p(sep)
        p("DETAIL ARTICLES\n")
        for a in r["articles"]:
            if a["mode"] == mode:
                nom = sans_accents(a["article"])[:14]
                quantite = int(a["quantite"] or 0)
                p(f"{nom:<14} x{quantite:<3} {montant_txt(a['total'], mode, a['rouges'], a['bleus']):>11}\n")
        p(sep)

    if r["argent_utilise"]:
        printer.set(align="left", bold=True, width=1, height=1)
        p("CAISSE ESPECES\n")
        printer.set(bold=False)
        p(f"{'Fond de caisse':<18}{montant_txt(r['fond'], 'ARGENT'):>14}\n")
        p(f"{'+ Ventes especes':<18}{montant_txt(r['especes'], 'ARGENT'):>14}\n")
        printer.set(bold=True)
        p(f"{'= ATTENDU':<18}{montant_txt(r['attendu'], 'ARGENT'):>14}\n")
        printer.set(bold=False)
        p(f"{'Compte :':<18}{'..........':>14}\n")
        p(f"{'Ecart  :':<18}{'..........':>14}\n")
        p(sep)

    printer.set(align="left", bold=True)
    p(f"Jetons Rouges : {r['jetons']['rouges']}\n")
    p(f"Jetons Bleus  : {r['jetons']['bleus']}\n")
    printer.set(bold=False)
    p(sep)

    for m in r["par_mode"]:
        printer.set(align="center", bold=True, width=1, height=2)
        p(f"TOTAL {m['mode']} : {montant_txt(m['total'], m['mode'], m['rouges'], m['bleus'])}\n")

    printer.set(align="center", bold=False, width=1, height=1)
    p("\n\n\n")

    try:
        printer.cut()
    except Exception:
        pass


NAVIGATEUR = {"proc": None}
URL_CAISSE = "http://127.0.0.1:5003"

# Profil Firefox dédié à la caisse (dans le dossier personnel, accessible au Firefox "snap" d'Ubuntu).
# Il autorise le bouton "Fermer la fenêtre" à fermer Firefox, comme ALT + F4.
PROFIL_FIREFOX = os.path.join(os.path.expanduser("~"), "caisse_bar_firefox_profil")

PREFS_FIREFOX = """
user_pref("dom.allow_scripts_to_close_windows", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
"""


def preparer_profil_firefox():
    os.makedirs(PROFIL_FIREFOX, exist_ok=True)
    with open(os.path.join(PROFIL_FIREFOX, "user.js"), "w", encoding="utf-8") as f:
        f.write(PREFS_FIREFOX)


def fermer_navigateur():
    """Ferme la fenêtre de la caisse (et seulement elle). Le serveur continue de tourner."""
    proc = NAVIGATEUR.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if os.name == "posix":
        # Firefox de la caisse (profil dédié) puis toute fenêtre lancée sur l'adresse de la caisse
        for motif in (PROFIL_FIREFOX, "127.0.0.1:5003"):
            subprocess.run(["pkill", "-f", motif], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def envoyer_alt_f4_windows():
    try:
        import ctypes
        VK_MENU, VK_F4, KEYUP = 0x12, 0x73, 0x0002
        u = ctypes.windll.user32
        u.keybd_event(VK_MENU, 0, 0, 0)
        u.keybd_event(VK_F4, 0, 0, 0)
        u.keybd_event(VK_F4, 0, KEYUP, 0)
        u.keybd_event(VK_MENU, 0, KEYUP, 0)
        return True
    except Exception as e:
        print("ALT+F4 Windows impossible :", e)
        return False


def fermer_fenetre_plus_tard():
    # Le navigateur essaie d'abord de se fermer lui-même (window.close).
    # Si la fenêtre est toujours là après 1,5 s, le programme la ferme.
    time.sleep(1.5)
    if os.name == "nt":
        if not envoyer_alt_f4_windows():
            fermer_navigateur()
    else:
        fermer_navigateur()


def ouvrir_navigateur():
    time.sleep(2)
    url = URL_CAISSE
    try:
        preparer_profil_firefox()
    except Exception as e:
        print("Profil Firefox :", e)
    commandes = [
        ["firefox", "--kiosk", "--new-instance", "--profile", PROFIL_FIREFOX, url],
        ["chromium", "--kiosk", "--noerrdialogs", "--disable-infobars", url],
        ["chromium-browser", "--kiosk", url],
        ["google-chrome", "--kiosk", url],
        ["chrome", "--kiosk", url],
        ["msedge", "--kiosk", url],
    ]
    for commande in commandes:
        try:
            NAVIGATEUR["proc"] = subprocess.Popen(commande)
            print("Navigateur lancé :", " ".join(commande))
            return
        except FileNotFoundError:
            continue
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        print("Aucun navigateur trouvé")

# =========================================================
# TIROIR-CAISSE
# =========================================================
# Le port "CR" du POS (RJ11/RJ12) commande le tiroir. Selon le modèle de PC,
# il est piloté par une ligne d'un port série (DTR ou RTS), par l'envoi d'octets
# sur un port série, ou bien le tiroir est branché sur l'imprimante (port DK).
# La page Configuration > Tiroir-caisse permet de tester chaque méthode.

METHODES_TIROIR = {
    "aucun": "Pas de tiroir",
    "imprimante": "Via l'imprimante tickets (port DK)",
    "serie_dtr": "Port COM — impulsion DTR",
    "serie_rts": "Port COM — impulsion RTS",
    "serie_octet": "Port COM — envoi d'octets",
    "parallele_data": "Port LPT — lignes de données",
    "parallele_ctrl": "Port LPT — lignes de contrôle",
}
CODE_ESCPOS_TIROIR = [b"\x1bp\x00\x19\xfa", b"\x1bp\x01\x19\xfa"]   # broches 2 et 5


def ports_series():
    """Ports série réellement présents sur le POS (COM1 = /dev/ttyS0, COM5 = /dev/ttyS4, ...).

    Linux crée beaucoup de /dev/ttyS* même sans matériel : on ne garde que ceux
    dont le type d'UART est connu (fichier /sys/class/tty/ttySx/type différent de 0).
    """
    import glob
    import re
    trouves = []
    for chemin in glob.glob("/sys/class/tty/ttyS*"):
        nom = os.path.basename(chemin)
        try:
            with open(os.path.join(chemin, "type")) as f:
                if f.read().strip() == "0":
                    continue
        except OSError:
            continue
        trouves.append("/dev/" + nom)
    trouves.sort(key=lambda p: int(re.sub(r"\D", "", p) or 0))
    # adaptateurs USB-série éventuels
    usb = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    # nom fixe du module USB tiroir (règle udev /dev/tiroir), en premier
    fixe = ["/dev/tiroir"] if os.path.exists("/dev/tiroir") else []
    trouves = fixe + usb + trouves
    return trouves or [f"/dev/ttyS{i}" for i in range(0, 4)]


def impulsion_parallele(port, mode):
    """Impulsion sur un port parallèle (LPT) via /dev/parportN (module ppdev).
    mode "parallele_data" : lignes de données D0-D7 ; "parallele_ctrl" : lignes de contrôle."""
    import fcntl
    import struct
    PPCLAIM, PPRELEASE = 0x708B, 0x708C
    PPWDATA, PPWCONTROL = 0x40017086, 0x40017084
    fd = os.open(port, os.O_RDWR)
    try:
        fcntl.ioctl(fd, PPCLAIM)
        try:
            if mode == "parallele_data":
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0x00)); time.sleep(0.05)
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0xFF)); time.sleep(0.2)
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0x00))
            else:
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04)); time.sleep(0.05)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x0B)); time.sleep(0.2)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04))
        finally:
            fcntl.ioctl(fd, PPRELEASE)
    finally:
        os.close(fd)


def ports_paralleles():
    import glob
    return sorted(glob.glob("/dev/parport*"))


def nom_com(port):
    """/dev/ttyS0 -> COM1 (nom imprimé à l'arrière du POS)."""
    if port.startswith("/dev/ttyS"):
        try:
            return f"COM{int(port[9:]) + 1}"
        except ValueError:
            pass
    if port.startswith("/dev/parport"):
        try:
            return f"LPT{int(port[12:]) + 1}"
        except ValueError:
            pass
    return port


def ouvrir_tiroir(methode=None, port=None):
    """Envoie l'impulsion d'ouverture. Retourne (ok, message)."""
    methode = methode or get_param("tiroir_methode", "aucun")
    port = port or get_param("tiroir_port", ports_series()[0])
    try:
        if methode == "aucun":
            return False, "Aucun tiroir configuré"

        if methode == "imprimante":
            with IMPRIMANTE_LOCK:
                printer = ouvrir_imprimante()
                try:
                    for code in CODE_ESCPOS_TIROIR:
                        printer._raw(code)
                finally:
                    fermer_imprimante(printer)
            return True, "Commande envoyée à l'imprimante"

        if methode in ("parallele_data", "parallele_ctrl"):
            impulsion_parallele(port, methode)
            return True, f"Impulsion envoyée sur {nom_com(port)}"
        if methode not in ("serie_dtr", "serie_rts", "serie_octet"):
            return False, f"Méthode inconnue : {methode}"
        try:
            import serial
        except ImportError:
            return False, "Module pyserial manquant (relancer installer.sh)"
        s = serial.Serial(port, 9600, timeout=1)
        try:
            if methode == "serie_dtr":
                s.dtr = False; time.sleep(0.05)
                s.dtr = True;  time.sleep(0.2)
                s.dtr = False
            elif methode == "serie_rts":
                s.rts = False; time.sleep(0.05)
                s.rts = True;  time.sleep(0.2)
                s.rts = False
            else:
                for code in CODE_ESCPOS_TIROIR:
                    s.write(code)
                s.write(b"\x07")
                s.flush()
        finally:
            s.close()
        return True, f"Impulsion envoyée sur {nom_com(port)}"
    except Exception as e:
        texte = f"{type(e).__name__} : {e}"
        if "Permission denied" in texte:
            texte += " — ajouter l'utilisateur aux groupes dialout et lp puis se reconnecter"
        return False, texte


def ouvrir_tiroir_arriere_plan(raison=""):
    def tache():
        try:
            ouvrir_tiroir()          # tiroir absent ou débranché : on ignore, la vente continue
        except Exception:
            pass
    threading.Thread(target=tache, daemon=True).start()


def tiroir_configure():
    """Vrai seulement si le tiroir est activé ET qu'une méthode a été choisie."""
    return option_active("tiroir_actif") and get_param("tiroir_methode", "aucun") != "aucun"

# =========================================================
# STYLE
# =========================================================

BLOC_CAISSE_ESPECES = """
{% if r.argent_utilise %}
<div class="card">
    <h2>💶 Caisse espèces</h2>
    <table style="max-width:700px;margin:auto;font-size:28px;">
        <tr><td style="text-align:left;">Fond de caisse</td><td style="text-align:right;">{{ r.fond|montant('ARGENT') }}</td></tr>
        <tr><td style="text-align:left;">+ Ventes en espèces</td><td style="text-align:right;">{{ r.especes|montant('ARGENT') }}</td></tr>
        <tr><td style="text-align:left;"><b>= Attendu dans la caisse</b></td><td style="text-align:right;color:#00ff66;"><b>{{ r.attendu|montant('ARGENT') }}</b></td></tr>
        <tr><td style="text-align:left;">Montant compté</td>
            <td style="text-align:right;"><input id="compte" inputmode="decimal" placeholder="0,00" style="width:170px;text-align:right;font-size:28px;" oninput="calculEcart()"> €</td></tr>
        <tr><td style="text-align:left;">Écart</td><td id="ecart" style="text-align:right;font-weight:bold;">—</td></tr>
    </table>
    <p style="color:#aaa;font-size:18px;">Carte et Payconiq ne sont pas dans le tiroir : ils ne comptent pas dans l'attendu.</p>
</div>
<script>
function calculEcart() {
    const v = document.getElementById("compte").value.replace(",", ".").replace("€", "").trim();
    const td = document.getElementById("ecart");
    if (v === "" || isNaN(parseFloat(v))) { td.textContent = "—"; td.style.color = ""; return; }
    const ecart = Math.round(parseFloat(v) * 100) - {{ r.attendu }};
    const txt = (Math.abs(ecart) / 100).toFixed(2).replace(".", ",") + " €";
    if (ecart === 0) { td.textContent = "0,00 € ✔"; td.style.color = "#00ff66"; }
    else if (ecart > 0) { td.textContent = "+" + txt + " (en trop)"; td.style.color = "#f1c40f"; }
    else { td.textContent = "−" + txt + " (manquant)"; td.style.color = "#ff5555"; }
}
</script>
{% endif %}
"""

STYLE = """
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #111; color: white; font-family: Arial, sans-serif; }
header { position: relative; background: #222; padding: 15px; text-align: center; font-size: 34px; font-weight: bold; }
header.argent { background: #8a6d00; }
.config-link { position: absolute; top: 50%; right: 12px; transform: translateY(-50%); display: flex; align-items: center; gap: 8px;
               height: 58px; padding: 0 24px; border-radius: 14px; background: #555; color: white; font-size: 24px; font-weight: bold;
               text-decoration: none; box-shadow: 0 3px 8px rgba(0,0,0,.4); }
.config-link:active { background: #777; }
.container { display: flex; height: calc(100vh - 80px); }
.articles { flex: 3; display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 12px; overflow-y: auto; align-content: start; }
.article-btn { width: 100%; height: 140px; border: none; border-radius: 16px; color: white; font-size: 24px; font-weight: bold; box-shadow: 0 4px 10px rgba(0,0,0,0.35); cursor: pointer; }
.article-sub { font-size: 16px; font-weight: normal; opacity: 0.9; margin-top: 4px; }
.panier { flex: 2; background: #1d1d1d; padding: 12px; display: flex; flex-direction: column; }
.panier-list { flex: 1; overflow-y: auto; background: black; padding: 10px; font-size: 22px; border-radius: 12px; }
.panier-row { display: grid; grid-template-columns: 1fr 60px 50px 60px 120px; gap: 4px; align-items: center; padding: 8px; border-bottom: 1px solid #333; }
.panier-jetons { grid-column: 1 / -1; font-size: 16px; color: #aaa; padding-left: 5px; margin-top: -4px; padding-bottom: 4px; }
.qty-btn { border: none; border-radius: 10px; padding: 14px; color: white; background: #555; font-size: 26px; font-weight: bold; text-decoration: none; text-align: center; }
.total { text-align: center; font-size: 36px; font-weight: bold; color: #00ff66; margin-top: 5px; }
.total-jetons-box { display: flex; justify-content: space-around; background: #222; border-radius: 10px; padding: 8px; margin: 5px 0; font-size: 20px; }
.jeton-r { color: #ff4d4d; font-weight: bold; }
.jeton-b { color: #3399ff; font-weight: bold; }
.btn { display: inline-block; text-decoration: none; text-align: center; border: none; border-radius: 14px; padding: 18px; margin: 5px; font-size: 24px; font-weight: bold; color: white; cursor: pointer; }
.btn:disabled { opacity: .3; cursor: default; }
.green { background: green; } .orange { background: orange; color: black; } .red { background: #c0392b; } .blue { background: #2d89ef; } .gray { background: #555; } .purple { background: #8e44ad; }
.actions { display: flex; flex-direction: column; gap: 22px; margin-top: 12px; }
.btn-valider, .btn-vider { display: block; width: 100%; margin: 0; padding: 0; border-radius: 20px; }
.btn-valider { height: 120px; font-size: 42px; background: #1e9e3e; }
.btn-vider { height: 80px; font-size: 28px; background: #c0392b; }
.btn-vider.armed { background: #ff2a2a; animation: clignote .5s infinite alternate; }
@keyframes clignote { from { opacity: 1; } to { opacity: .6; } }
.flash { position: fixed; top: 90px; left: 50%; transform: translateX(-50%); z-index: 100; background: #1e9e3e; color: white; font-size: 34px; font-weight: bold; text-align: center; padding: 20px 40px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0,0,0,.6); pointer-events: none; animation: flashout 2.5s forwards; }
.flash.flash-long { top: 50%; transform: translate(-50%, -50%); background: #d68910; padding: 40px 60px; font-size: 38px; border: 6px solid white; box-shadow: 0 0 0 100vmax rgba(0,0,0,.6); animation-duration: 6s; }
.flash .rendu { font-size: 64px; display: inline-block; margin: 10px 0; white-space: nowrap; }
@keyframes flashout { 0%, 70% { opacity: 1; } 100% { opacity: 0; } }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.88); z-index: 50; align-items: center; justify-content: center; }
.overlay-box { background: #1d1d1d; border-radius: 24px; padding: 30px; width: min(900px, 95vw); text-align: center; }
.overlay-total { font-size: 52px; font-weight: bold; color: #00ff66; margin-bottom: 25px; }
.pay-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 20px; margin-bottom: 25px; }
.pay-btn { grid-column: span 2; height: 120px; border: none; border-radius: 20px; font-size: 36px; font-weight: bold; background: #f39c12; color: black; cursor: pointer; }
.pay-btn.exact { background: #1e9e3e; color: white; }
.pay-btn.elec { grid-column: span 3; color: white; }
.pay-btn.elec.seul { grid-column: span 6; }
.pay-btn.carte { background: #2d89ef; }
.pay-btn.payconiq { background: #ff4785; }
table { width: 100%; border-collapse: collapse; font-size: 22px; } td, th { border: 1px solid #555; padding: 12px; }
input, select { font-size: 24px; padding: 10px; max-width: 100%; }
.card { background: #1d1d1d; border-radius: 18px; padding: 20px; margin: 12px; }
.big-number { font-size: 58px; color: #00ff66; font-weight: bold; }
@media print { body { background: white; color: black; } .no-print { display: none; } header { background: white; color: black; } .config-link { display: none; } }
</style>
"""

# =========================================================
# PANIER
# =========================================================

def get_panier():
    return session.get("panier", [])


def save_panier(panier):
    session["panier"] = panier
    session.modified = True


def panier_du_mode(mode):
    """Vide automatiquement les articles ajoutés dans l'autre mode."""
    panier = get_panier()
    filtre = [i for i in panier if i.get("mode") == mode]
    if len(filtre) != len(panier):
        save_panier(filtre)
    return filtre


def panier_total(panier):
    return sum(int(item["prix"]) * int(item.get("quantite", 1)) for item in panier)


def panier_total_jetons(panier):
    rouges = sum(int(item.get("jetons_rouges", 0)) * int(item.get("quantite", 1)) for item in panier)
    bleus = sum(int(item.get("jetons_bleus", 0)) * int(item.get("quantite", 1)) for item in panier)
    return {"rouges": rouges, "bleus": bleus}


def add_to_panier(nom, prix, jetons_rouges, jetons_bleus, mode):
    panier = get_panier()
    for item in panier:
        if item["nom"] == nom and int(item["prix"]) == int(prix) and item.get("mode") == mode:
            item["quantite"] = int(item.get("quantite", 1)) + 1
            save_panier(panier)
            return
    panier.append({
        "nom": nom,
        "prix": int(prix),
        "quantite": 1,
        "jetons_rouges": int(jetons_rouges),
        "jetons_bleus": int(jetons_bleus),
        "mode": mode,
    })
    save_panier(panier)

# =========================================================
# ACCUEIL
# =========================================================

@app.route("/")
def index():
    mode = get_mode()
    conn = db()
    articles = conn.execute("SELECT * FROM articles ORDER BY categorie, id").fetchall()
    conn.close()
    panier = panier_du_mode(mode)
    total = panier_total(panier)
    totaux_jetons = panier_total_jetons(panier)
    derniere = session.pop("derniere_vente", None)

    return render_template_string("""
    {{ style|safe }}
    {% if derniere %}
    <div class="flash {% if derniere.rendu %}flash-long{% endif %}">
        ✔ Vente n° {{ derniere.id }} — {{ derniere.total|montant(derniere.mode, derniere.rouges, derniere.bleus) }}{% if derniere.paiement in ('CARTE', 'PAYCONIQ') %} ({{ derniere.paiement|lower }}){% endif %}<br>
        {% if derniere.rendu %}<span class="rendu">À RENDRE : {{ derniere.rendu|montant('ARGENT') }}</span><br>{% endif %}
        {% if derniere.mode == 'ARGENT' %}<span style="font-size:26px;">🔴 {{ derniere.rouges }} &nbsp; 🔵 {{ derniere.bleus }}</span>{% endif %}
    </div>
    {% endif %}
    {% if alerte_imprimante %}
    <div style="position:fixed;bottom:12px;left:12px;z-index:40;background:#c0392b;color:white;font-size:20px;font-weight:bold;padding:10px 18px;border-radius:12px;">⚠ Imprimante journal : erreur</div>
    {% endif %}
    <header class="{{ 'argent' if mode == 'ARGENT' else '' }}"><span style="position:absolute;left:20px;top:50%;transform:translateY(-50%);font-size:18px;font-weight:normal;color:#aaa;">v{{ version }}</span>CAISSE CONCOURS TIR - MODE {{ mode }}<a class="config-link" href="/login">⚙ Config</a></header>
    <div class="container">
        <div class="articles">
            {% for a in articles %}
            <form method="post" action="/add">
                <input type="hidden" name="id" value="{{ a.id }}">
                <button class="article-btn" type="submit" style="background:{{ a.couleur }};">
                    {{ a.nom }}<br>
                    {% if mode == 'ARGENT' %}💶 {{ a.prix_euro|montant('ARGENT') }}
                    <div class="article-sub">
                        🔴 x{{ a.jetons_rouges }} | 🔵 x{{ a.jetons_bleus }}
                    </div>
                    {% else %}{{ a.prix|montant('JETONS', a.jetons_rouges, a.jetons_bleus) }}{% endif %}
                </button>
            </form>
            {% endfor %}
        </div>
        <div class="panier">
            <h1 style="text-align:center; margin:5px 0;">PANIER</h1>
            <div class="panier-list">
                {% if not panier %}<div style="text-align:center;color:#aaa;margin-top:40px;">Aucun article</div>{% endif %}
                {% for item in panier %}
                <div class="panier-row">
                    <div>{{ item.nom }}</div>
                    <a class="qty-btn" href="/moins/{{ loop.index0 }}">−</a>
                    <div style="text-align:center;">x{{ item.quantite }}</div>
                    <a class="qty-btn" href="/plus/{{ loop.index0 }}">+</a>
                    <div style="text-align:right;">{{ (item.prix * item.quantite)|montant(mode, item.jetons_rouges * item.quantite, item.jetons_bleus * item.quantite) }}</div>
                </div>
                {% if mode == 'ARGENT' %}
                <div class="panier-jetons">
                    Jetons unitaires : <span class="jeton-r">🔴 x{{ item.jetons_rouges }}</span> | <span class="jeton-b">🔵 x{{ item.jetons_bleus }}</span>
                </div>
                {% endif %}
                {% endfor %}
            </div>

            {% if mode == 'ARGENT' %}
            <div class="total-jetons-box">
                <span>Total Rouges : <span class="jeton-r">{{ totaux_jetons.rouges }}</span></span>
                <span>Total Bleus : <span class="jeton-b">{{ totaux_jetons.bleus }}</span></span>
            </div>
            <div class="total">Total : {{ total|montant(mode) }}</div>
            {% else %}
            <div class="total">Total : <span class="jeton-r">🔴 {{ totaux_jetons.rouges }}</span> &nbsp; <span class="jeton-b">🔵 {{ totaux_jetons.bleus }}</span></div>
            {% endif %}

            <div class="actions">
                <button type="button" class="btn btn-valider" {% if not panier %}disabled{% endif %} onclick="valider(this)">✔ VALIDER</button>
                <button type="button" class="btn btn-vider" {% if not panier %}disabled{% endif %} onclick="vider(this)">✖ Vider</button>
            </div>
        </div>
    </div>

    {% if mode == 'ARGENT' and panier %}
    <div id="paiement" class="overlay" style="display:none;">
        <div class="overlay-box">
            <div class="overlay-total">À payer : {{ total|montant('ARGENT') }}</div>
            <form method="post" action="/payer" class="pay-grid" onsubmit="if (this.dataset.envoye) return false; this.dataset.envoye = 1;">
                <button class="pay-btn exact" name="choix" value="EXACT">Compte juste</button>
                {% for b in billets if b * 100 > total %}
                <button class="pay-btn" name="choix" value="{{ b * 100 }}">{{ b }} €</button>
                {% endfor %}
                {% if carte %}
                <button class="pay-btn elec carte {{ '' if payconiq else 'seul' }}" name="choix" value="CARTE">💳 Carte</button>
                {% endif %}
                {% if payconiq %}
                <button class="pay-btn elec payconiq {{ '' if carte else 'seul' }}" name="choix" value="PAYCONIQ">📱 Payconiq</button>
                {% endif %}
            </form>
            <button type="button" class="btn gray" style="width:100%;height:80px;margin:0;" onclick="fermerPaiement()">Annuler</button>
        </div>
    </div>
    {% endif %}

    <script>
    const MODE = "{{ mode }}";
    function valider(b) {
        if (MODE === "ARGENT") {
            document.getElementById("paiement").style.display = "flex";
            return;
        }
        b.disabled = true;
        b.textContent = "Enregistrement…";
        location.href = "/valider_jetons";
    }
    function fermerPaiement() {
        document.getElementById("paiement").style.display = "none";
    }
    let videArme = null;
    function vider(b) {
        if (videArme) { clearTimeout(videArme); location.href = "/clear"; return; }
        b.classList.add("armed");
        b.textContent = "Toucher encore pour vider";
        videArme = setTimeout(() => {
            b.classList.remove("armed");
            b.textContent = "✖ Vider";
            videArme = null;
        }, 3000);
    }
    </script>
    """, style=STYLE, articles=articles, panier=panier, total=total, totaux_jetons=totaux_jetons,
        derniere=derniere, mode=mode, billets=BILLETS, version=VERSION,
        carte=option_active("pay_carte"), payconiq=option_active("pay_payconiq"),
        alerte_imprimante=(mode == "ARGENT" and journal_actif() and bool(ERREUR_JOURNAL["texte"])))

@app.route("/add", methods=["POST"])
def add():
    mode = get_mode()
    conn = db()
    a = conn.execute("SELECT * FROM articles WHERE id = ?", (request.form.get("id"),)).fetchone()
    conn.close()
    if a:
        if mode == "ARGENT":
            prix = a["prix_euro"]
        else:
            prix = (a["jetons_rouges"] or 0) + (a["jetons_bleus"] or 0)
        add_to_panier(a["nom"], prix or 0, a["jetons_rouges"] or 0, a["jetons_bleus"] or 0, mode)
    return redirect(url_for("index"))

@app.route("/plus/<int:index>")
def plus(index):
    panier = get_panier()
    if 0 <= index < len(panier):
        panier[index]["quantite"] = int(panier[index].get("quantite", 1)) + 1
    save_panier(panier)
    return redirect(url_for("index"))

@app.route("/moins/<int:index>")
def moins(index):
    panier = get_panier()
    if 0 <= index < len(panier):
        panier[index]["quantite"] = int(panier[index].get("quantite", 1)) - 1
        if panier[index]["quantite"] <= 0:
            panier.pop(index)
    save_panier(panier)
    return redirect(url_for("index"))

@app.route("/delete_last")
def delete_last():
    panier = get_panier()
    if panier:
        panier[-1]["quantite"] = int(panier[-1].get("quantite", 1)) - 1
        if panier[-1]["quantite"] <= 0:
            panier.pop()
    save_panier(panier)
    return redirect(url_for("index"))

@app.route("/clear")
def clear():
    save_panier([])
    return redirect(url_for("index"))

# =========================================================
# VENTES / PAIEMENT
# =========================================================

def enregistrer_vente(paiement, recu=0, rendu=0, operateur="", mode="JETONS"):
    panier = get_panier()
    if not panier:
        return None
    total = panier_total(panier)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ventes (date, total, paiement, recu, rendu, operateur, annulee, mode)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total, paiement, recu, rendu, operateur, mode))
    vente_id = cur.lastrowid
    for item in panier:
        cur.execute("""
            INSERT INTO lignes_vente (vente_id, article, prix, quantite, jetons_rouges, jetons_bleus)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (vente_id, item["nom"], int(item["prix"]), int(item.get("quantite", 1)), int(item.get("jetons_rouges", 0)), int(item.get("jetons_bleus", 0))))
    conn.commit()
    conn.close()
    save_panier([])
    backup_db()
    return vente_id

@app.route("/valider_jetons")
def valider_jetons():
    mode = get_mode()
    panier = panier_du_mode(mode)
    if not panier or mode != "JETONS":
        return redirect(url_for("index"))

    total = panier_total(panier)
    jetons = panier_total_jetons(panier)
    vente_id = enregistrer_vente("JETONS", recu=total, rendu=0, operateur="", mode="JETONS")
    session["derniere_vente"] = {
        "id": vente_id,
        "mode": "JETONS",
        "paiement": "JETONS",
        "total": total,
        "rendu": 0,
        "rouges": jetons["rouges"],
        "bleus": jetons["bleus"],
    }
    return redirect(url_for("index"))


@app.route("/payer", methods=["POST"])
def payer():
    mode = get_mode()
    panier = panier_du_mode(mode)
    if not panier or mode != "ARGENT":
        return redirect(url_for("index"))

    total = panier_total(panier)
    jetons = panier_total_jetons(panier)
    choix = request.form.get("choix", "")

    if choix == "CARTE":
        if not option_active("pay_carte"):
            return redirect(url_for("index"))
        paiement, recu = "CARTE", total
    elif choix == "PAYCONIQ":
        if not option_active("pay_payconiq"):
            return redirect(url_for("index"))
        paiement, recu = "PAYCONIQ", total
    elif choix == "EXACT":
        paiement, recu = "ESPECES", total
    else:
        try:
            recu = int(choix)
        except ValueError:
            return redirect(url_for("index"))
        paiement = "ESPECES"
        if recu < total:
            recu = total

    rendu = recu - total
    vente_id = enregistrer_vente(paiement, recu=recu, rendu=rendu, operateur="", mode="ARGENT")
    if tiroir_configure() and (paiement == "ESPECES" or get_param("tiroir_quand", "especes") == "tous"):
        ouvrir_tiroir_arriere_plan()
    if journal_actif():
        journal_en_arriere_plan(lignes_journal_vente(vente_id))
    session["derniere_vente"] = {
        "id": vente_id,
        "mode": "ARGENT",
        "paiement": paiement,
        "total": total,
        "rendu": rendu,
        "rouges": jetons["rouges"],
        "bleus": jetons["bleus"],
    }
    return redirect(url_for("index"))


@app.route("/ticket/<int:vente_id>")
def ticket(vente_id):
    conn = db()
    vente = conn.execute("SELECT * FROM ventes WHERE id = ?", (vente_id,)).fetchone()
    lignes = conn.execute("SELECT * FROM lignes_vente WHERE vente_id = ?", (vente_id,)).fetchall()
    conn.close()

    if not vente:
        return redirect(url_for("index"))

    total_r = sum(int(l["jetons_rouges"]) * int(l["quantite"]) for l in lignes)
    total_b = sum(int(l["jetons_bleus"]) * int(l["quantite"]) for l in lignes)

    return render_template_string("""
    {{ style|safe }}
    <header>Ticket de caisse</header>
    <div style="max-width:520px;margin:auto;padding:20px;">
        <div style="text-align:center;"><h2>CONCOURS TIR</h2><p>{{ vente.date }}</p><p>Vente n° {{ vente.id }}</p></div><hr>
        {% for l in lignes %}
        <div style="display:flex;justify-content:space-between;font-size:22px;margin-bottom:2px;">
            <span>{{ l.article }} x{{ l.quantite }}</span>
            <span>{{ (l.prix * l.quantite)|montant(vente.mode, l.jetons_rouges * l.quantite, l.jetons_bleus * l.quantite) }}</span>
        </div>
        {% if vente.mode == 'ARGENT' %}
        <div style="font-size:16px;color:#aaa;margin-bottom:8px;">
            Détail : 🔴 x{{ l.jetons_rouges * l.quantite }} | 🔵 x{{ l.jetons_bleus * l.quantite }}
        </div>
        {% endif %}
        {% endfor %}
        <hr>
        {% if vente.mode == 'ARGENT' %}
        <div style="font-size: 22px; text-align:right; margin-bottom: 10px;">
            Total Rouges : <span class="jeton-r">{{ total_r }}</span><br>
            Total Bleus : <span class="jeton-b">{{ total_b }}</span>
        </div>
        {% endif %}
        <h2 style="text-align:right;">Total : {{ vente.total|montant(vente.mode, total_r, total_b) }}</h2>
        <p>Paiement : {{ vente.paiement }}</p>
        {% if vente.paiement == 'ESPECES' %}
        <p>Reçu : {{ vente.recu|montant('ARGENT') }} — Rendu : {{ vente.rendu|montant('ARGENT') }}</p>
        {% endif %}

        {% if vente.annulee %}<h2 style="color:red;text-align:center;">VENTE ANNULÉE</h2>{% endif %}
        <div class="no-print" style="text-align:center;margin-top:25px;">
            <button class="btn green" onclick="window.print();">Imprimer</button>
            <a class="btn gray" href="/ventes">Retour historique</a>
        </div>
    </div>
    """, style=STYLE, vente=vente, lignes=lignes, total_r=total_r, total_b=total_b)

# =========================================================
# ECRAN CLIENT
# =========================================================

@app.route("/client")
def client():
    mode = get_mode()
    panier = panier_du_mode(mode)
    total = panier_total(panier)
    totaux_jetons = panier_total_jetons(panier)
    return render_template_string("""
    {{ style|safe }}
    <meta http-equiv="refresh" content="2">
    <header class="{{ 'argent' if mode == 'ARGENT' else '' }}">ÉCRAN CLIENT</header>
    <div style="padding:40px;text-align:center;">
        <h1>Votre panier</h1>
        <div style="max-width:800px;margin:auto;font-size:34px;">
            {% for item in panier %}
            <div style="display:flex;justify-content:space-between;border-bottom:1px solid #444;padding:12px;">
                <span>{{ item.nom }} x{{ item.quantite }}</span>
                <span>{{ (item.prix * item.quantite)|montant(mode, item.jetons_rouges * item.quantite, item.jetons_bleus * item.quantite) }}</span>
            </div>
            {% endfor %}
        </div>

        {% if mode == 'ARGENT' %}
        <div style="margin-top:30px; display:flex; justify-content:center; gap:50px; font-size:28px;">
            <div>Jetons Rouges : <span class="jeton-r">{{ totaux_jetons.rouges }}</span></div>
            <div>Jetons Bleus : <span class="jeton-b">{{ totaux_jetons.bleus }}</span></div>
        </div>
        {% endif %}

        <div style="margin-top:40px;"><div>Total à payer</div><div class="big-number">{{ total|montant(mode, totaux_jetons.rouges, totaux_jetons.bleus) }}</div></div>
    </div>
    """, style=STYLE, panier=panier, total=total, totaux_jetons=totaux_jetons, mode=mode)

# =========================================================
# ADMIN
# =========================================================

PAVE_HTML = """
<style>
.pave-wrap { display: flex; flex-direction: column; align-items: center; padding: 20px; }
.pave-code { width: 420px; height: 90px; margin-bottom: 18px; background: black; border: 3px solid #444; border-radius: 16px;
             font-size: 56px; letter-spacing: 18px; text-align: center; line-height: 84px; color: white; }
.pave-code.erreur { border-color: #ff3333; animation: secoue .4s; }
@keyframes secoue { 0%,100% { transform: translateX(0); } 25% { transform: translateX(-12px); } 75% { transform: translateX(12px); } }
.pave { display: grid; grid-template-columns: repeat(3, 130px); gap: 16px; }
.pave button { height: 100px; border: none; border-radius: 18px; font-size: 44px; font-weight: bold; color: white; background: #444; cursor: pointer; }
.pave button:active { background: #777; }
.pave .effacer { background: #b9770e; font-size: 34px; }
.pave .ok { background: {{ couleur }}; font-size: 30px; }
.pave-msg { color: #ff5555; font-size: 26px; font-weight: bold; height: 34px; margin-bottom: 6px; }
</style>
<div class="pave-wrap">
    <div class="pave-msg">{% if erreur %}Code incorrect{% endif %}</div>
    <div id="pave-code" class="pave-code {{ 'erreur' if erreur else '' }}">&nbsp;</div>
    <form method="post" id="pave-form">
        <input type="hidden" name="password" id="pave-valeur">
        <div class="pave">
            {% for n in [1,2,3,4,5,6,7,8,9] %}
            <button type="button" onclick="paveTouche('{{ n }}')">{{ n }}</button>
            {% endfor %}
            <button type="button" class="effacer" onclick="paveEfface()">⌫</button>
            <button type="button" onclick="paveTouche('0')">0</button>
            <button type="submit" class="ok">{{ bouton }}</button>
        </div>
    </form>
    <a class="btn gray" href="{{ retour }}" style="width:422px;margin-top:22px;">Retour</a>
</div>
<script>
let paveSaisie = "";
function paveAffiche() {
    document.getElementById("pave-code").innerHTML = paveSaisie ? "●".repeat(paveSaisie.length) : "&nbsp;";
    document.getElementById("pave-valeur").value = paveSaisie;
}
function paveTouche(c) { if (paveSaisie.length < 12) { paveSaisie += c; paveAffiche(); } }
function paveEfface() { paveSaisie = paveSaisie.slice(0, -1); paveAffiche(); }
document.addEventListener("keydown", e => {
    if (/^[0-9]$/.test(e.key)) paveTouche(e.key);
    else if (e.key === "Backspace") paveEfface();
    else if (e.key === "Enter") document.getElementById("pave-form").submit();
});
</script>
"""


def pave_numerique(bouton="Entrer", couleur="#1e9e3e", retour="/", erreur=False):
    return render_template_string(PAVE_HTML, bouton=bouton, couleur=couleur, retour=retour, erreur=erreur)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password", "") == read_password():
            session["admin"] = True
            trace_maintenance("ENTREE MAINTENANCE")
            return redirect(url_for("config"))
        trace_maintenance("CODE MAINTENANCE FAUX")
        return redirect(url_for("login", err=1))
    return render_template_string("""
    {{ style|safe }}
    <header>CODE MAINTENANCE</header>
    {{ pave|safe }}
    """, style=STYLE, pave=pave_numerique(
        bouton="Entrer", couleur="#1e9e3e", retour="/",
        erreur=bool(request.args.get("err"))))

@app.route("/logout")
def logout():
    if session.get("admin"):
        trace_maintenance("SORTIE MAINTENANCE")
    session["admin"] = False
    return redirect(url_for("index"))


def trace_maintenance(evenement):
    """Note sur le journal papier les entrées / sorties de maintenance."""
    if not journal_actif():
        return
    lignes = [f"*** {evenement} ***"[:32], datetime.now().strftime("%d/%m/%Y %H:%M:%S")]
    if evenement == "ENTREE MAINTENANCE" and get_mode() == "ARGENT":
        r = resume_ventes()
        lignes.append(f"{'Especes attendues':<18}{montant_txt(r['attendu'], 'ARGENT'):>14}"[:32])
    lignes.append("-" * 32)
    journal_en_arriere_plan(lignes)


def admin_required():
    return session.get("admin") is True

@app.route("/config")
def config():
    if not admin_required():
        return redirect(url_for("login"))
    mode = get_mode()
    autre = "ARGENT" if mode == "JETONS" else "JETONS"
    return render_template_string("""
    {{ style|safe }}
    <header>CONFIGURATION<span style="position:absolute;right:20px;top:50%;transform:translateY(-50%);font-size:18px;font-weight:normal;color:#aaa;">version {{ version }}</span></header>
    <div style="padding:20px;text-align:center;">
        <div class="card">
            <h2>Mode de la caisse : <span style="color:{{ '#f1c40f' if mode == 'ARGENT' else '#00ff66' }};">{{ mode }}</span></h2>
            <form method="post" action="/mode" onsubmit="return confirm('Passer la caisse en mode {{ autre }} ? Le panier en cours sera vidé.');">
                <input type="hidden" name="mode" value="{{ autre }}">
                <button class="btn {{ 'orange' if autre == 'ARGENT' else 'green' }}">Passer en mode {{ autre }}</button>
            </form>
        </div>
        <div class="card">
            <h2>🗄️ Tiroir-caisse</h2>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="tiroir_actif">
                <button class="btn {{ 'green' if tiroir_actif else 'gray' }}" style="min-width:300px;">🗄️ Tiroir : {{ 'OUI' if tiroir_actif else 'NON' }}</button>
            </form>
            {% if tiroir_actif %}<p style="font-size:22px;">{{ tiroir_libelle }}</p>{% endif %}
            {% if tiroir_ok %}
            <form method="post" action="/tiroir/ouvrir" style="display:inline;">
                <button class="btn orange">Ouvrir le tiroir</button>
            </form>
            {% endif %}
            {% if tiroir_actif %}<a class="btn blue" href="/tiroir">Réglage / test du tiroir</a>{% endif %}
        </div>
        <div class="card">
            <h2>💶 Fond de caisse (mode ARGENT)</h2>
            <p style="color:#aaa;">Montant en espèces dans le tiroir au début de la journée</p>
            <form method="post" action="/fond_caisse">
                <input name="fond" inputmode="decimal" value="{{ fond|euros_input }}" style="width:180px;text-align:right;font-size:32px;"> €
                <button class="btn green">Enregistrer</button>
            </form>
            <div style="font-size:24px;margin-top:10px;">
                Fond {{ r.fond|montant('ARGENT') }} + ventes espèces {{ r.especes|montant('ARGENT') }}
                = <b style="color:#00ff66;">attendu {{ r.attendu|montant('ARGENT') }}</b>
            </div>
        </div>
        <div class="card">
            <h2>Moyens de paiement (mode ARGENT)</h2>
            <p style="color:#aaa;">💶 Espèces : toujours actif — toucher un bouton pour activer / désactiver</p>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="pay_carte">
                <button class="btn {{ 'blue' if carte else 'gray' }}">💳 Carte : {{ 'OUI' if carte else 'NON' }}</button>
            </form>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="pay_payconiq">
                <button class="btn {{ 'purple' if payconiq else 'gray' }}">📱 Payconiq : {{ 'OUI' if payconiq else 'NON' }}</button>
            </form>
        </div>
        <div class="card">
            <h2>Impression au fil de l'eau</h2>
            <p style="color:#aaa;">Ventes en mode ARGENT, fond de caisse, annulations et accès à la maintenance (tous modes) sont imprimés à la suite, sans couper le papier</p>
            <form method="post" action="/option" style="display:inline;">
                <input type="hidden" name="cle" value="print_journal">
                <button class="btn {{ 'green' if journal else 'gray' }}">🧾 Journal : {{ 'OUI' if journal else 'NON' }}</button>
            </form>
            <form method="post" action="/avance_papier" style="display:inline;">
                <button class="btn gray">⇡ Avancer le papier</button>
            </form>
            {% if erreur_journal %}<p style="color:#ff5555;">Dernière erreur imprimante : {{ erreur_journal }}</p>{% endif %}
        </div>
        <a class="btn blue" href="/articles">Articles configurables</a><br>
        <a class="btn blue" href="/ventes">Historique ventes</a><br>
        <a class="btn blue" href="/recap">Récapitulatif articles</a><br>
        <a class="btn green" href="/stats">Statistiques</a><br>
        <a class="btn green" href="/fin_service">Fin de service + impression</a><br>
        <a class="btn purple" href="/change_password">Changer mot de passe</a><br>
        <a class="btn orange" href="/backup">Sauvegarde DB</a><br>
        <a class="btn red" href="/reset">RESET HISTORIQUE</a><br>
        <div class="card">
            <h2>🔄 Mise à jour</h2>
            <p style="font-size:22px;">Version installée : <b>{{ version }}</b></p>
            <a class="btn blue" href="/maj">Vérifier les mises à jour</a>
            <form method="post" action="/redemarrer_pos" style="display:inline;"
                  onsubmit="return confirm('Redémarrer complètement le POS ?');">
                <button class="btn gray">⟳ Redémarrer le POS</button>
            </form>
        </div>
        <a class="btn gray" href="/logout">Retour caisse (quitter la maintenance)</a>
        <div class="card" style="margin-top:40px;">
            <button type="button" class="btn red" style="width:100%;max-width:600px;height:90px;font-size:30px;"
                    onclick="fermerFenetre()">✖ Fermer la fenêtre (ALT + F4)</button>
            <script>
            function fermerFenetre() {
                if (!confirm("Fermer la fenêtre de la caisse ?")) return;
                // Prévient le serveur (trace + fermeture de secours), puis ferme la fenêtre comme ALT + F4
                fetch("/fermer_fenetre", {method: "POST"}).finally(() => {
                    window.close();
                    setTimeout(() => { document.body.innerHTML =
                        '<h1 style="text-align:center;margin-top:120px;">Fermeture de la fenêtre…</h1>'; }, 300);
                });
            }
            </script>
        </div>
    </div>
    """, style=STYLE, version=VERSION, mode=mode, autre=autre,
        carte=option_active("pay_carte"), payconiq=option_active("pay_payconiq"),
        journal=journal_actif(), erreur_journal=ERREUR_JOURNAL["texte"],
        fond=get_fond_caisse(), r=resume_ventes(),
        tiroir_ok=tiroir_configure(), tiroir_libelle=libelle_tiroir(),
        tiroir_actif=option_active("tiroir_actif"))

def libelle_tiroir():
    methode = get_param("tiroir_methode", "aucun")
    if methode == "aucun":
        return "Méthode pas encore choisie : faire « Réglage / test du tiroir »"
    txt = METHODES_TIROIR.get(methode, methode)
    if methode.startswith("serie") or methode.startswith("parallele"):
        port = get_param("tiroir_port", ports_series()[0])
        txt += f" sur {nom_com(port)}"
    quand = get_param("tiroir_quand", "especes")
    txt += " — s'ouvre " + ("à chaque paiement" if quand == "tous" else "pour les paiements en espèces")
    return txt


@app.route("/tiroir/ouvrir", methods=["POST"])
def tiroir_ouvrir_manuel():
    if not admin_required():
        return redirect(url_for("login"))
    ok, msg = ouvrir_tiroir()
    if ok and journal_actif():
        journal_en_arriere_plan([
            "*** OUVERTURE TIROIR ***",
            datetime.now().strftime("%d/%m/%Y %H:%M:%S") + "  (manuel)",
            "-" * 32,
        ])
    return redirect(url_for("tiroir", msg=("✔ " if ok else "✖ ") + msg))


@app.route("/tiroir", methods=["GET", "POST"])
def tiroir():
    if not admin_required():
        return redirect(url_for("login"))
    msg = request.args.get("msg", "")

    if request.method == "POST":
        action = request.form.get("action")
        methode = request.form.get("methode", "aucun")
        port = request.form.get("port", "")
        if action == "tester":
            ok, m = ouvrir_tiroir(methode, port or None)
            msg = ("✔ " if ok else "✖ ") + m + (" — le tiroir s'est-il ouvert ?" if ok else "")
            return render_page_tiroir(msg, methode, port)
        if action == "choisir":
            set_param("tiroir_methode", methode)
            if port:
                set_param("tiroir_port", port)
            return redirect(url_for("tiroir", msg="Réglage enregistré : " + libelle_tiroir()))
        if action == "quand":
            set_param("tiroir_quand", "tous" if request.form.get("quand") == "tous" else "especes")
            return redirect(url_for("tiroir", msg="Réglage enregistré : " + libelle_tiroir()))

    return render_page_tiroir(msg, None, None)


def render_page_tiroir(msg, dernier_methode, dernier_port):
    essais = [("imprimante", "")]
    for p in ports_paralleles():
        essais += [("parallele_data", p), ("parallele_ctrl", p)]
    for m in ("serie_dtr", "serie_rts", "serie_octet"):
        for p in ports_series():
            essais.append((m, p))
    return render_template_string("""
    {{ style|safe }}
    <header>TIROIR-CAISSE</header>
    <div style="padding:20px;text-align:center;">
        <div class="card">
            <h2>Réglage actuel</h2>
            <p style="font-size:24px;">{{ libelle }}</p>
            {% if msg %}<p style="font-size:26px;font-weight:bold;color:{{ '#00ff66' if msg.startswith('✔') or msg.startswith('Réglage') else '#ff5555' }};">{{ msg }}</p>{% endif %}
            <form method="post" style="margin-top:10px;">
                <input type="hidden" name="action" value="quand">
                <button class="btn {{ 'green' if quand == 'especes' else 'gray' }}" name="quand" value="especes">Ouvrir pour les espèces seulement</button>
                <button class="btn {{ 'green' if quand == 'tous' else 'gray' }}" name="quand" value="tous">Ouvrir à chaque paiement</button>
            </form>
        </div>

        <div class="card">
            <h2>Trouver la bonne méthode</h2>
            <p style="color:#aaa;font-size:20px;">Touchez « Tester » ligne par ligne jusqu'à ce que le tiroir s'ouvre, puis « Utiliser ».</p>
            <table>
                <tr><th>Méthode</th><th>Port</th><th></th></tr>
                {% for m, p in essais %}
                <tr {% if m == dernier_methode and p == (dernier_port or '') %}style="background:#2c3e50;"{% endif %}>
                    <td style="text-align:left;">{{ methodes[m] }}</td>
                    <td>{{ nom_com(p) if p else '—' }}</td>
                    <td style="white-space:nowrap;">
                        <form method="post" style="display:inline;">
                            <input type="hidden" name="methode" value="{{ m }}"><input type="hidden" name="port" value="{{ p }}">
                            <button class="btn blue" name="action" value="tester">Tester</button>
                            <button class="btn green" name="action" value="choisir">Utiliser</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
                <tr>
                    <td style="text-align:left;">{{ methodes['aucun'] }}</td><td>—</td>
                    <td><form method="post"><input type="hidden" name="methode" value="aucun">
                        <button class="btn gray" name="action" value="choisir">Utiliser</button></form></td>
                </tr>
            </table>
        </div>
        <a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, msg=msg, libelle=libelle_tiroir(), quand=get_param("tiroir_quand", "especes"),
        essais=essais, methodes=METHODES_TIROIR, nom_com=nom_com,
        dernier_methode=dernier_methode, dernier_port=dernier_port)


# =========================================================
# MISE À JOUR DEPUIS GITHUB
# =========================================================

def git(*args, timeout=30):
    """Lance une commande git dans le dossier de la caisse. Retourne (ok, sortie)."""
    try:
        r = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return False, "git n'est pas installé (sudo apt install git)"
    except subprocess.TimeoutExpired:
        return False, "Délai dépassé (pas d'Internet ?)"


def version_dans(texte):
    import re
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', texte or "", re.M)
    return m.group(1) if m else "?"


def etat_mise_a_jour():
    """Interroge GitHub et compare avec la version installée."""
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return {"erreur": "Cette caisse n'a pas été installée depuis GitHub (pas de dossier .git)."}
    ok, sortie = git("fetch", "--quiet", "origin")
    if not ok:
        return {"erreur": "Impossible de joindre GitHub : " + sortie}
    ok, branche = git("rev-parse", "--abbrev-ref", "HEAD")
    branche = branche if ok and branche != "HEAD" else "main"
    distant = f"origin/{branche}"
    ok, nb = git("rev-list", "--count", f"HEAD..{distant}")
    if not ok:
        return {"erreur": nb}
    nb = int(nb or 0)
    _, code_distant = git("show", f"{distant}:caisse_bar.py")
    _, journal = git("log", "--pretty=format:%s", f"HEAD..{distant}")
    _, modifies = git("status", "--porcelain", "--untracked-files=no")
    return {
        "erreur": "",
        "branche": branche,
        "nb": nb,
        "version_distante": version_dans(code_distant),
        "changements": [l for l in journal.splitlines() if l.strip()],
        "fichiers_modifies": [l for l in modifies.splitlines() if l.strip()],
    }


def redemarrer_caisse_plus_tard():
    """Relance le programme de la caisse. La fenêtre Firefox reste ouverte et se recharge toute seule.

    Un nouveau processus démarre 2 s après l'arrêt de celui-ci (le port 5003 doit être libéré).
    """
    time.sleep(1.5)
    with IMPRIMANTE_LOCK:              # attend la fin d'une impression en cours
        pass
    script = os.path.join(BASE_DIR, "caisse_bar.py")
    log = os.path.join(BASE_DIR, "caisse_bar.log")
    if os.name == "nt":
        subprocess.Popen([sys.executable, script, "--sans-navigateur"], cwd=BASE_DIR,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        commande = f'sleep 2; exec "{sys.executable}" "{script}" --sans-navigateur >> "{log}" 2>&1'
        subprocess.Popen(["/bin/sh", "-c", commande], cwd=BASE_DIR,
                         start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os._exit(0)


@app.route("/version")
def route_version():
    return {"version": VERSION}


@app.route("/maj")
def maj():
    if not admin_required():
        return redirect(url_for("login"))
    e = etat_mise_a_jour()
    return render_template_string("""
    {{ style|safe }}
    <header>MISE À JOUR</header>
    <div style="padding:20px;text-align:center;">
        <div class="card">
            <p style="font-size:24px;">Version installée : <b>{{ version }}</b></p>
            {% if e.erreur %}
                <h2 style="color:#ff5555;">✖ {{ e.erreur }}</h2>
            {% elif e.nb == 0 %}
                <h2 style="color:#00ff66;">✔ La caisse est à jour</h2>
            {% else %}
                <h2 style="color:#f1c40f;">Nouvelle version disponible : {{ e.version_distante }}</h2>
                <div style="text-align:left;max-width:800px;margin:auto;font-size:20px;">
                    <b>Changements :</b>
                    <ul>{% for c in e.changements %}<li>{{ c }}</li>{% endfor %}</ul>
                </div>
                {% if e.fichiers_modifies %}
                <p style="color:#ff5555;font-size:20px;">Des fichiers du programme ont été modifiés à la main sur ce POS :
                {{ e.fichiers_modifies|join(', ') }}. La mise à jour risque d'échouer.</p>
                {% endif %}
                <form method="post" action="/maj/installer"
                      onsubmit="return confirm('Installer la version {{ e.version_distante }} et redémarrer la caisse ?');">
                    <button class="btn green" style="font-size:30px;padding:24px 40px;">Installer et redémarrer la caisse</button>
                </form>
            {% endif %}
        </div>
        <a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, version=VERSION, e=e)


@app.route("/maj/installer", methods=["POST"])
def maj_installer():
    if not admin_required():
        return redirect(url_for("login"))
    e = etat_mise_a_jour()
    if e.get("erreur") or e.get("nb", 0) == 0:
        return redirect(url_for("maj"))

    _, fichiers = git("diff", "--name-only", f"HEAD..origin/{e['branche']}")
    ok, sortie = git("merge", "--ff-only", f"origin/{e['branche']}")
    if not ok:
        return render_template_string("""
        {{ style|safe }}<header>MISE À JOUR</header>
        <div style="padding:20px;text-align:center;"><div class="card">
            <h2 style="color:#ff5555;">✖ La mise à jour a échoué</h2>
            <pre style="text-align:left;white-space:pre-wrap;font-size:18px;">{{ sortie }}</pre>
        </div><a class="btn gray" href="/config">Retour</a></div>
        """, style=STYLE, sortie=sortie)

    # nouvelles dépendances Python éventuelles
    if "requirements.txt" in fichiers.split():
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                            os.path.join(BASE_DIR, "requirements.txt")], timeout=600)
        except Exception as ex:
            print("pip :", ex)

    if journal_actif():
        imprimer_journal([
            "*** MISE A JOUR ***",
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            f"v{VERSION} -> v{e['version_distante']}",
            "-" * 32,
        ])
    backup_db()
    session["admin"] = False
    threading.Thread(target=redemarrer_caisse_plus_tard, daemon=True).start()
    return render_template_string("""
    {{ style|safe }}<header>MISE À JOUR</header>
    <div style="padding:60px;text-align:center;">
        <h1 style="color:#00ff66;">✔ Version {{ nouvelle }} installée</h1>
        <p id="etat" style="font-size:28px;">Redémarrage de la caisse…</p>
    </div>
    <script>
    setTimeout(function attendre() {
        fetch("/version", {cache: "no-store"})
            .then(r => r.json())
            .then(d => { if (d.version === "{{ nouvelle }}") location.href = "/"; else setTimeout(attendre, 1000); })
            .catch(() => setTimeout(attendre, 1000));
    }, 2500);
    </script>
    """, style=STYLE, nouvelle=e["version_distante"])


@app.route("/redemarrer_pos", methods=["POST"])
def redemarrer_pos():
    if not admin_required():
        return redirect(url_for("login"))
    if journal_actif():
        imprimer_journal(["*** REDEMARRAGE DU POS ***",
                          datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "-" * 32])
    backup_db()

    def plus_tard():
        time.sleep(2)
        for cmd in (["systemctl", "reboot"], ["sudo", "-n", "reboot"]):
            try:
                if subprocess.run(cmd, timeout=10).returncode == 0:
                    return
            except Exception:
                continue
        print("Redémarrage du POS refusé par le système")

    threading.Thread(target=plus_tard, daemon=True).start()
    return render_template_string("""
    {{ style|safe }}<header>REDÉMARRAGE</header>
    <div style="padding:60px;text-align:center;font-size:30px;">Redémarrage du POS…<br>
    <span style="font-size:20px;color:#aaa;">Si rien ne se passe dans 30 secondes, le système a refusé : redémarrez avec le bouton marche/arrêt.</span></div>
    """, style=STYLE)


@app.route("/fermer_fenetre", methods=["POST"])
def fermer_fenetre():
    """Ferme seulement la fenêtre du navigateur. Le serveur de caisse continue de tourner."""
    if not admin_required():
        return redirect(url_for("login"))
    if session.get("admin"):
        trace_maintenance("SORTIE MAINTENANCE")
    session["admin"] = False
    threading.Thread(target=fermer_fenetre_plus_tard, daemon=True).start()
    return render_template_string("""
    {{ style|safe }}
    <header>FERMETURE…</header>
    <div style="padding:60px;text-align:center;font-size:30px;">Fermeture de la fenêtre…</div>
    """, style=STYLE)


@app.route("/fond_caisse", methods=["POST"])
def fond_caisse():
    if not admin_required():
        return redirect(url_for("login"))
    fond = max(0, parse_euros(request.form.get("fond", "0")))
    set_param("fond_caisse", str(fond))
    if journal_actif():
        journal_en_arriere_plan([
            f"FOND DE CAISSE  {datetime.now().strftime('%d/%m %H:%M')}",
            f"{'Montant':<20}{montant_court(fond):>8} EUR",
            "-" * 32,
        ])
    return redirect(url_for("config"))

@app.route("/option", methods=["POST"])
def basculer_option():
    if not admin_required():
        return redirect(url_for("login"))
    cle = request.form.get("cle", "")
    if cle in OPTIONS_PAIEMENT:
        set_param(cle, "0" if option_active(cle) else "1")
    return redirect(url_for("config"))

@app.route("/mode", methods=["POST"])
def changer_mode():
    if not admin_required():
        return redirect(url_for("login"))
    mode = request.form.get("mode", "JETONS")
    if mode in ("JETONS", "ARGENT"):
        set_param("mode", mode)
        save_panier([])
    return redirect(url_for("config"))

@app.route("/articles", methods=["GET", "POST"])
def articles():
    if not admin_required():
        return redirect(url_for("login"))
    conn = db()
    if request.method == "POST":
        conn.execute("DELETE FROM articles")
        noms = request.form.getlist("nom")
        prix_euros = request.form.getlist("prix_euro")
        categories = request.form.getlist("categorie")
        couleurs = request.form.getlist("couleur")
        rouges = request.form.getlist("jetons_rouges")
        bleus = request.form.getlist("jetons_bleus")
        for nom, prix_e, categorie, couleur, r, b in zip(noms, prix_euros, categories, couleurs, rouges, bleus):
            nom = nom.strip()
            if nom:
                try:
                    r_int = int(r)
                except ValueError:
                    r_int = 0
                try:
                    b_int = int(b)
                except ValueError:
                    b_int = 0
                conn.execute("""
                    INSERT INTO articles (nom, prix, categorie, couleur, jetons_rouges, jetons_bleus, prix_euro)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (nom, r_int + b_int, categorie.strip() or "Divers", couleur.strip() or "#2d89ef", r_int, b_int, parse_euros(prix_e)))
        conn.commit()
        conn.close()
        return redirect(url_for("articles"))
    rows = conn.execute("SELECT * FROM articles ORDER BY categorie, id").fetchall()
    conn.close()
    return render_template_string("""
    {{ style|safe }}
    <header>ARTICLES</header>
    <form method="post" style="padding:20px;">
        <p style="color:#aaa;">Pour supprimer un article : effacer sa description puis Sauver.<br>
        En mode JETONS, le prix d'un article est le nombre de jetons 🔴 rouges et 🔵 bleus.</p>
        <table id="table">
            <tr><th>Description</th><th>Prix €</th><th>🔴 Rouges</th><th>🔵 Bleus</th><th>Catégorie</th><th>Couleur</th></tr>
            {% for a in rows %}
            <tr>
                <td><input name="nom" value="{{ a.nom }}"></td>
                <td><input style="width:110px;" name="prix_euro" inputmode="decimal" value="{{ a.prix_euro|euros_input }}"></td>
                <td><input style="width:80px; color:#ff4d4d;" type="number" name="jetons_rouges" value="{{ a.jetons_rouges }}"></td>
                <td><input style="width:80px; color:#3399ff;" type="number" name="jetons_bleus" value="{{ a.jetons_bleus }}"></td>
                <td><input name="categorie" value="{{ a.categorie }}"></td>
                <td><input name="couleur" value="{{ a.couleur }}"></td>
            </tr>
            {% endfor %}
        </table><br>
        <button type="button" class="btn blue" onclick="addRow()">Ajouter</button>
        <button class="btn green" type="submit">Sauver</button>
        <a class="btn gray" href="/config">Retour</a>
    </form>
    <script>
    function addRow() {
        let table = document.getElementById("table");
        let row = table.insertRow(-1);
        row.innerHTML = '<td><input name="nom" value="Nouvel article"></td>' +
                        '<td><input style="width:110px;" name="prix_euro" inputmode="decimal" value="0,00"></td>' +
                        '<td><input style="width:80px; color:#ff4d4d;" type="number" name="jetons_rouges" value="0"></td>' +
                        '<td><input style="width:80px; color:#3399ff;" type="number" name="jetons_bleus" value="0"></td>' +
                        '<td><input name="categorie" value="Divers"></td>' +
                        '<td><input name="couleur" value="#2d89ef"></td>';
    }
    </script>
    """, style=STYLE, rows=rows)

@app.route("/ventes")
def ventes():
    if not admin_required():
        return redirect(url_for("login"))
    conn = db()
    rows = conn.execute("""
        SELECT v.*,
               (SELECT COALESCE(SUM(jetons_rouges * quantite), 0) FROM lignes_vente WHERE vente_id = v.id) AS rouges,
               (SELECT COALESCE(SUM(jetons_bleus * quantite), 0) FROM lignes_vente WHERE vente_id = v.id) AS bleus
        FROM ventes v ORDER BY v.id DESC
    """).fetchall()
    conn.close()
    return render_template_string("""
    {{ style|safe }}
    <header>HISTORIQUE VENTES</header>
    <div style="padding:20px;">
        <table>
            <tr><th>ID</th><th>Date</th><th>Total</th><th>Paiement</th><th>Rendu</th><th>Statut</th><th>Actions</th></tr>
            {% for v in rows %}
            <tr>
                <td>{{ v.id }}</td><td>{{ v.date }}</td><td>{{ v.total|montant(v.mode, v.rouges, v.bleus) }}</td><td>{{ v.paiement }}</td>
                <td>{% if v.paiement == 'ESPECES' %}{{ v.rendu|montant('ARGENT') }}{% endif %}</td>
                <td>{% if v.annulee %}<span style="color:red;">Annulée</span>{% else %}OK{% endif %}</td>
                <td><a class="btn blue" href="/ticket/{{ v.id }}">Ticket</a>{% if not v.annulee %}<a class="btn red" href="/annuler_vente/{{ v.id }}" onclick="return confirm('Annuler cette vente ?');">Annuler</a>{% endif %}</td>
            </tr>
            {% endfor %}
        </table><br><a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, rows=rows)

@app.route("/annuler_vente/<int:vente_id>")
def annuler_vente(vente_id):
    if not admin_required():
        return redirect(url_for("login"))
    conn = db()
    v = conn.execute("SELECT * FROM ventes WHERE id = ? AND annulee = 0", (vente_id,)).fetchone()
    conn.execute("UPDATE ventes SET annulee = 1 WHERE id = ?", (vente_id,))
    conn.commit()
    conn.close()
    backup_db()
    if v and v["mode"] == "ARGENT" and journal_actif():
        heure = datetime.now().strftime("%H:%M")
        journal_en_arriere_plan([
            f"ANNULATION #{v['id']}  {heure}",
            f"{'Montant':<20}{'-' + montant_court(v['total']):>8} EUR"[:32],
            "-" * 32,
        ])
    return redirect(url_for("ventes"))


@app.route("/avance_papier", methods=["POST"])
def avance_papier():
    if not admin_required():
        return redirect(url_for("login"))
    imprimer_journal([""] * 6, gras_premiere=False)
    return redirect(url_for("config"))

@app.route("/recap")
def recap():
    if not admin_required():
        return redirect(url_for("login"))
    r = resume_ventes()
    return render_template_string("""
    {{ style|safe }}
    <header>RÉCAPITULATIF ARTICLES</header>
    <div style="padding:20px;">
        <table>
            <tr><th>Mode</th><th>Article</th><th>Quantité</th><th>Total</th><th>🔴 Rouges</th><th>🔵 Bleus</th></tr>
            {% for a in r.articles %}
            <tr>
                <td>{{ a.mode }}</td><td>{{ a.article }}</td><td>{{ a.quantite }}</td><td>{{ a.total|montant(a.mode, a.rouges, a.bleus) }}</td>
                <td class="jeton-r">{{ a.rouges }}</td><td class="jeton-b">{{ a.bleus }}</td>
            </tr>
            {% endfor %}
        </table><br><a class="btn gray" href="/config">Retour</a></div>
    """, style=STYLE, r=r)

@app.route("/stats")
def stats():
    if not admin_required():
        return redirect(url_for("login"))
    r = resume_ventes()
    return render_template_string("""
    {{ style|safe }}
    <header>STATISTIQUES</header>
    <div style="padding:20px;">
        <div style="display:flex; flex-wrap:wrap;">
            {% for m in r.par_mode %}
            <div class="card" style="flex:1;"><h2>Total {{ m.mode|lower }}</h2><div class="big-number">{{ m.total|montant(m.mode, m.rouges, m.bleus) }}</div><div>{{ m.nb }} vente{% if m.nb > 1 %}s{% endif %}</div></div>
            {% else %}
            <div class="card" style="flex:1;"><h2>Aucune vente</h2></div>
            {% endfor %}
            <div class="card" style="flex:1;"><h2>🔴 Rouges</h2><div class="big-number" style="color:#ff4d4d;">{{ r.jetons.rouges }}</div></div>
            <div class="card" style="flex:1;"><h2>🔵 Bleus</h2><div class="big-number" style="color:#3399ff;">{{ r.jetons.bleus }}</div></div>
        </div>
        {{ bloc_especes|safe }}
        <div class="card"><h2>Article le plus vendu</h2>{% if r.top %}<div class="big-number">{{ r.top.article }} x{{ r.top.qte }}</div>{% else %}<div>Aucune vente</div>{% endif %}</div>
        <div class="card"><h2>Par type de paiement</h2>
            <table><tr><th>Mode</th><th>Paiement</th><th>Nombre</th><th>Total</th></tr>
            {% for p in r.par_paiement %}<tr><td>{{ p.mode }}</td><td>{{ p.paiement }}</td><td>{{ p.nb }}</td><td>{{ p.total|montant(p.mode, p.rouges, p.bleus) }}</td></tr>{% endfor %}
            </table>
        </div>
        <a class="btn gray" href="/config">Retour</a>
    </div>
    """, style=STYLE, r=r, bloc_especes=render_template_string(BLOC_CAISSE_ESPECES, r=r))


@app.route("/fin_service")
def fin_service():
    if not admin_required():
        return redirect(url_for("login"))

    r = resume_ventes()
    message = ""
    erreur = ""

    try:
        imprimer_fin_service()
        message = "Fin de service imprimée correctement"
    except Exception as e:
        erreur = str(e)

    return render_template_string("""
    {{ style|safe }}
    <header>FIN DE SERVICE</header>
    <div style="padding:20px;text-align:center;">
        {% if message %}
            <h1 style="color:#00ff66;">{{ message }}</h1>
        {% else %}
            <h1 style="color:#ff5555;">Erreur impression</h1>
            <div class="card"><pre style="white-space:pre-wrap;font-size:20px;text-align:left;">{{ erreur }}</pre></div>
        {% endif %}

        <div class="card">
            <h2>Résumé financier & Jetons</h2>
            {% for m in r.par_mode %}
            <div class="big-number">{{ m.total|montant(m.mode, m.rouges, m.bleus) }}</div>
            <div style="font-size:22px;margin-bottom:10px;">Mode {{ m.mode }} — {{ m.nb }} vente{% if m.nb > 1 %}s{% endif %}</div>
            {% else %}
            <h2>Aucune vente</h2>
            {% endfor %}
            {% for p in r.par_paiement if p.mode == 'ARGENT' %}
            <div style="font-size:22px;">{{ p.paiement }} : {{ p.nb }} vente{% if p.nb > 1 %}s{% endif %} — {{ p.total|montant('ARGENT') }}</div>
            {% endfor %}
            <div style="font-size:24px; margin-top:15px;">
                🔴 Rouges : <b>{{ r.jetons.rouges }}</b> | 🔵 Bleus : <b>{{ r.jetons.bleus }}</b>
            </div>
        </div>

        {{ bloc_especes|safe }}

        <div class="card">
            <h2>Détail articles</h2>
            <table>
                <tr><th>Mode</th><th>Article</th><th>Quantité</th><th>Total</th><th>🔴 R</th><th>🔵 B</th></tr>
                {% for a in r.articles %}
                <tr>
                    <td>{{ a.mode }}</td><td>{{ a.article }}</td><td>{{ a.quantite }}</td><td>{{ a.total|montant(a.mode, a.rouges, a.bleus) }}</td>
                    <td class="jeton-r">{{ a.rouges }}</td><td class="jeton-b">{{ a.bleus }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <a class="btn green" href="/fin_service">Réimprimer</a>
        <a class="btn gray" href="/config">Retour configuration</a>
    </div>
    """, style=STYLE, message=message, erreur=erreur, r=r,
        bloc_especes=render_template_string(BLOC_CAISSE_ESPECES, r=r))

@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if not admin_required():
        return redirect(url_for("login"))
    message = ""
    if request.method == "POST":
        old = request.form.get("old", "")
        new1 = request.form.get("new1", "")
        new2 = request.form.get("new2", "")
        if old != read_password():
            message = "Ancien mot de passe incorrect"
        elif not new1:
            message = "Nouveau mot de passe vide"
        elif not new1.isdigit() or len(new1) > 12:
            message = "Le code doit contenir uniquement des chiffres (12 max)"
        elif new1 != new2:
            message = "Les deux mots de passe ne correspondent pas"
        else:
            write_password(new1)
            message = "Mot de passe modifié"
    return render_template_string("""
    {{ style|safe }}
    <header>CHANGER MOT DE PASSE</header>
    <div style="padding:30px;text-align:center;">
        {% if message %}<h2>{{ message }}</h2>{% endif %}
        <form method="post">
            <input type="password" name="old" inputmode="numeric" placeholder="Ancien code"><br><br>
            <input type="password" name="new1" inputmode="numeric" placeholder="Nouveau code (chiffres)"><br><br>
            <input type="password" name="new2" inputmode="numeric" placeholder="Confirmer"><br><br>
            <button class="btn green">Sauver</button><a class="btn gray" href="/config">Retour</a>
        </form>
    </div>
    """, style=STYLE, message=message)

@app.route("/backup")
def backup():
    if not admin_required():
        return redirect(url_for("login"))
    backup_db()
    return render_template_string("""
    {{ style|safe }}
    <header>SAUVEGARDE</header>
    <div style="padding:30px;text-align:center;"><h1>Sauvegarde effectuée</h1><p>Dossier : <b>{{ backup_dir }}</b></p><a class="btn gray" href="/config">Retour</a></div>
    """, style=STYLE, backup_dir=BACKUP_DIR)

@app.route("/reset", methods=["GET", "POST"])
def reset():
    if not admin_required():
        return redirect(url_for("login"))
    erreur = False
    if request.method == "POST":
        if request.form.get("password", "") == read_password():
            backup_db()
            conn = db()
            conn.execute("DELETE FROM lignes_vente")
            conn.execute("DELETE FROM ventes")
            # Remet les compteurs AUTOINCREMENT à zéro : la prochaine vente sera n° 1
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('ventes', 'lignes_vente')")
            conn.commit()
            conn.close()
            if journal_actif():
                journal_en_arriere_plan([
                    "*** RESET HISTORIQUE ***",
                    datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "Prochaine vente : n 1",
                    "-" * 32,
                ])
            return redirect(url_for("config"))
        erreur = True
    return render_template_string("""
    {{ style|safe }}
    <header>RESET HISTORIQUE</header>
    <h2 style="text-align:center;color:#ff5555;margin:15px 0 0;">Attention : cela efface les ventes.</h2>
    {{ pave|safe }}
    """, style=STYLE, pave=pave_numerique(
        bouton="Confirmer reset", couleur="#c0392b", retour="/config", erreur=erreur))

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    print(f"Caisse bar version {VERSION}")
    init_db()
    try:
        preparer_profil_firefox()
    except Exception as e:
        print("Profil Firefox :", e)
    # lancer_caisse.sh ouvre lui-même Firefox : il passe --sans-navigateur
    if "--sans-navigateur" not in sys.argv:
        threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    app.run(host="0.0.0.0", port=5003, debug=False)
