#!/usr/bin/env python3
"""
Trouve sur quel port et avec quelle méthode le tiroir-caisse s'ouvre.

Lancer sur le POS :
    cd ~/caisse
    venv/bin/python trouver_tiroir.py

Le script essaie chaque port série du POS, une méthode à la fois, et demande
après chaque essai si le tiroir s'est ouvert. Quand c'est le cas, il enregistre
le réglage dans la caisse (rien d'autre à faire).
"""
import glob
import os
import re
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "caisse_concours_tickets.db")
CODE_ESCPOS = [b"\x1bp\x00\x19\xfa", b"\x1bp\x01\x19\xfa"]
DUREE = 0.2   # secondes (modifiable : --duree 2 pour voir au multimètre)
if "--duree" in sys.argv:
    try:
        DUREE = float(sys.argv[sys.argv.index("--duree") + 1])
    except (IndexError, ValueError):
        pass

METHODES = [
    ("serie_dtr", "impulsion DTR"),
    ("serie_rts", "impulsion RTS"),
    ("serie_octet", "envoi d'octets"),
]


def ports_reels():
    ports = []
    for chemin in glob.glob("/sys/class/tty/ttyS*"):
        try:
            with open(os.path.join(chemin, "type")) as f:
                if f.read().strip() == "0":
                    continue
        except OSError:
            continue
        ports.append("/dev/" + os.path.basename(chemin))
    ports.sort(key=lambda p: int(re.sub(r"\D", "", p) or 0))
    # les ports sans prise à l'arrière (COM5, COM6...) en premier : ce sont les meilleurs candidats
    ports.sort(key=lambda p: int(re.sub(r"\D", "", p) or 0) < 4)
    # module USB vers tiroir-caisse : essayé en premier
    usb = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    fixe = ["/dev/tiroir"] if os.path.exists("/dev/tiroir") else []
    return fixe + usb + ports


def nom_com(port):
    m = re.match(r"/dev/ttyS(\d+)$", port)
    if m:
        return f"COM{int(m.group(1)) + 1}"
    m = re.match(r"/dev/parport(\d+)$", port)
    return f"LPT{int(m.group(1)) + 1}" if m else port

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
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0xFF)); time.sleep(DUREE)
                fcntl.ioctl(fd, PPWDATA, struct.pack("B", 0x00))
            else:
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04)); time.sleep(0.05)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x0B)); time.sleep(DUREE)
                fcntl.ioctl(fd, PPWCONTROL, struct.pack("B", 0x04))
        finally:
            fcntl.ioctl(fd, PPRELEASE)
    finally:
        os.close(fd)



def impulsion(serial, port, methode):
    if methode.startswith("parallele"):
        return impulsion_parallele(port, methode)
    s = serial.Serial(port, 9600, timeout=1)
    try:
        if methode == "serie_dtr":
            s.dtr = False; time.sleep(0.05)
            s.dtr = True;  time.sleep(DUREE)
            s.dtr = False
        elif methode == "serie_rts":
            s.rts = False; time.sleep(0.05)
            s.rts = True;  time.sleep(DUREE)
            s.rts = False
        else:
            for code in CODE_ESCPOS:
                s.write(code)
            s.write(b"\x07")
            s.flush()
    finally:
        s.close()


def enregistrer(port, methode):
    if not os.path.exists(DB_FILE):
        print(f"\n(Base de la caisse introuvable : {DB_FILE} — réglage non enregistré.)")
        return
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS parametres (cle TEXT PRIMARY KEY, valeur TEXT)")
    for cle, valeur in (("tiroir_methode", methode), ("tiroir_port", port), ("tiroir_actif", "1")):
        conn.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
    conn.commit()
    conn.close()
    print("Réglage enregistré dans la caisse : le tiroir s'ouvrira aux paiements en espèces.")


def main():
    try:
        import serial
    except ImportError:
        print("pyserial manquant. Lancez le script avec :  venv/bin/python trouver_tiroir.py")
        sys.exit(1)

    ports = ports_reels()
    if not ports:
        print("Aucun port série trouvé sur ce PC.")
        sys.exit(1)

    print("=== Recherche du port du tiroir-caisse ===")
    print("Ports trouvés :", ", ".join(f"{nom_com(p)} ({p})" for p in ports))
    print("Vérifiez que la serrure du tiroir est en position du MILIEU (stand-by) et que le tiroir est fermé.")
    input("Appuyez sur Entrée pour commencer...")

    lpt = sorted(glob.glob("/dev/parport*"))
    if lpt:
        print("Ports parallèles (LPT) trouvés :", ", ".join(f"{nom_com(p)} ({p})" for p in lpt))
    else:
        print("Aucun port LPT visible (si le BIOS en a un : sudo modprobe ppdev puis relancer).")
    # le LPT d'abord : il n'a pas de prise à l'arrière, il est sans doute relié au port CR
    essais = [(p, m, txt) for p in lpt for m, txt in (("parallele_data", "LPT lignes de données"),
                                                      ("parallele_ctrl", "LPT lignes de contrôle"))]
    essais += [(p, m, txt) for p in ports for m, txt in METHODES]
    for n, (port, methode, txt) in enumerate(essais, 1):
        print(f"\n[{n}/{len(essais)}] {nom_com(port)} ({port}) — {txt}")
        try:
            impulsion(serial, port, methode)
        except Exception as e:
            msg = str(e)
            if "Permission denied" in msg:
                print("  ✖ Accès refusé au port.")
                print("    Faites :  sudo usermod -aG dialout,lp $USER   puis déconnectez-vous / reconnectez-vous.")
                sys.exit(1)
            print(f"  ✖ Impossible d'utiliser ce port : {msg}")
            continue

        rep = input("  Le tiroir s'est-il ouvert ? [o = oui / Entrée = non / q = quitter] ").strip().lower()
        if rep == "q":
            break
        if rep in ("o", "oui", "y"):
            print(f"\n✔ TROUVÉ : {nom_com(port)} ({port}), {txt}")
            enregistrer(port, methode)
            return

    print("\nAucun essai n'a ouvert le tiroir.")
    print("Pistes : câble (brochage Posiflex 6→8 broches), cavalier de tension du port CR, serrure du tiroir.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêté.")
