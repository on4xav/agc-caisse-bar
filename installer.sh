#!/bin/bash
# Installation de la caisse bar sur le POS (Ubuntu).
# À lancer une seule fois depuis le dossier caisse_bar :   bash installer.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Dossier de la caisse bar : $DIR"

echo "== Paquets système"
sudo apt update
sudo apt install -y python3-venv python3-pip

echo "== Environnement Python"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install flask python-escpos pyusb pyserial

echo "== Imprimante tickets USB (0416:5011) : droits d'accès"
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0416", ATTR{idProduct}=="5011", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-imprimante-tickets.rules > /dev/null

echo "== Module USB tiroir-caisse (Prolific 067b:23a3) : nom fixe /dev/tiroir + droits"
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="23a3", SYMLINK+="tiroir", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-tiroir.rules > /dev/null

sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG dialout "$USER"

echo "== Reprise des données de l'ancienne installation (~/caisse), si elle existe"
for f in caisse_concours_tickets.db password.txt; do
    if [ -f "$HOME/caisse/$f" ] && [ ! -f "$DIR/$f" ]; then
        cp -v "$HOME/caisse/$f" "$DIR/$f"
    fi
done

echo "== Raccourci « Caisse bar » et démarrage automatique"
chmod +x "$DIR/lancer_caisse_bar.sh"
# supprime les raccourcis de l'ancienne installation pour éviter un double démarrage
rm -f "$HOME/.config/autostart/caisse.desktop" "$HOME/.local/share/applications/caisse.desktop"
RACCOURCI="[Desktop Entry]
Type=Application
Name=Caisse bar
Comment=Caisse du bar - concours de tir
Exec=$DIR/lancer_caisse_bar.sh
Icon=accessories-calculator
Terminal=false
Categories=Office;"
mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"
echo "$RACCOURCI" > "$HOME/.local/share/applications/caisse_bar.desktop"
echo "$RACCOURCI" > "$HOME/.config/autostart/caisse_bar.desktop"

echo
echo "Installation terminée."
echo ">> Redémarrez le POS : la caisse bar s'ouvrira toute seule en plein écran."
echo ">> Pour régler le tiroir :  cd $DIR && venv/bin/python trouver_tiroir.py"
