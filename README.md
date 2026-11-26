# AGC — Caisse bar

Caisse tactile (Flask) pour le bar des concours de tir, prévue pour un POS Posiflex sous Ubuntu.

## Fonctions

- **Mode tickets** ou **mode argent**, au choix dans la configuration
- Jetons rouges / bleus à remettre, affichés pour chaque article et chaque vente
- Gros boutons **Valider** / **Vider** (double appui pour vider), validation directe sans écran intermédiaire
- Mode argent : compte juste, billets 5/10/20/50 €, carte et Payconiq (activables), montant à rendre affiché au centre de l'écran
- **Fond de caisse** et montant d'espèces attendu, avec calcul de l'écart en fin de journée
- Imprimante tickets ESC/POS : journal des ventes au fil de l'eau (sans coupe) et bilan de fin de service
- **Tiroir-caisse** : ouverture automatique aux paiements en espèces (module USB, port COM ou imprimante), activable OUI / NON
- Accès maintenance par **pavé numérique**, entrées / sorties imprimées sur le journal
- Le RESET de l'historique remet la numérotation des ventes à 1
- Sauvegarde automatique de la base dans `backup/`

## Fichiers

| Fichier | Rôle |
|---|---|
| `caisse_bar.py` | L'application |
| `installer.sh` | Installation complète sur le POS |
| `lancer_caisse_bar.sh` | Démarre la caisse et ouvre Firefox en plein écran |
| `trouver_tiroir.py` | Trouve le réglage qui ouvre le tiroir-caisse et l'enregistre |
| `requirements.txt` | Dépendances Python |

## Installation sur le POS (Ubuntu)

```bash
git clone https://github.com/on4xav/agc-caisse-bar.git ~/caisse_bar
cd ~/caisse_bar
bash installer.sh
```

L'installateur :

- crée l'environnement Python (`venv/`) et installe les dépendances ;
- donne les droits sur l'imprimante tickets USB (`0416:5011`) ;
- crée le nom fixe `/dev/tiroir` pour le module USB du tiroir (Prolific `067b:23a3`) ;
- ajoute l'utilisateur au groupe `dialout` (ports série) ;
- reprend la base et le code de l'ancienne installation `~/caisse` s'ils existent ;
- crée l'icône **Caisse bar** et le démarrage automatique.

Redémarrer ensuite le POS : la caisse s'ouvre toute seule.

### Régler le tiroir-caisse

```bash
cd ~/caisse_bar
venv/bin/python trouver_tiroir.py
```

Le script essaie chaque port et chaque méthode et demande si le tiroir s'est ouvert.
La bonne réponse est enregistrée directement dans la caisse.

## Mise à jour

```bash
cd ~/caisse_bar
git pull
pkill -f caisse_bar.py
```

Puis relancer avec l'icône **Caisse bar**.
La version installée s'affiche en haut à droite de la page **Config** et sur le ticket de fin de service. Les ventes et le code ne sont pas touchés
(ils ne sont pas dans le dépôt).

## Premier démarrage

- Code de maintenance par défaut : **1234** (à changer dans Config → Changer mot de passe, chiffres uniquement).
- Les prix en euros des articles sont à saisir dans Config → Articles avant d'utiliser le mode argent.
