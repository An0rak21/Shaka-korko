# KORKO — surf anytime

Location de planches de surf en liège, en libre accès, **sans cadenas et sans app**.
Projet réalisé au hackathon **SHAKA Festival** (Biarritz, Cité de l'Océan), sur le brief Green Wave / NOTOX.

Chaque planche porte une balise BLE passive et un QR code gravé. On scanne la planche,
on finalise sa résa sur un site web, et on va à l'eau. La station (un Raspberry Pi au râtelier)
entend les balises et prévient le cloud quand une planche part ou revient.

## Comment ça marche

1. **Scanne le QR code de ta planche** (K1, K2…) : le site s'ouvre directement sur elle.
2. **Finalise ta résa** : prénom, téléphone, paiement, photo de départ.
3. **À l'eau !!** 15 min offertes, puis 0,15 € la minute. Retour détecté automatiquement, reçu par SMS.

Les choix produit sont détaillés dans [`docs/synthese-choix.md`](docs/synthese-choix.md).

## Contenu

| Fichier | Rôle |
|---|---|
| `mon_cloud.py` | Le cloud KORKO : site client, API, réservations, SMS, tableau de bord `/admin`, écran jury `/live` |
| `ma_station.py` | L'algorithme de la station : détecte départs et retours à partir du signal des balises |
| `ecoute.py` | Écoute en direct de la station (une ligne par seconde et par balise) |
| `web/` | Le site mobile (`app.html`) et ses images |
| `qr/` | Étiquettes QR des planches K1 et K2 |
| `lancer_demo.command` | Lance toute la démo (cloud + station) en un double-clic sur Mac |
| `sms.env.exemple` | Modèle de configuration SMS (Infobip, Twilio ou Brevo) |
| `korko.py`, `korko_sim.py`, `korko_test.py`, `*_exemple.py` | Kit fourni par les organisateurs (contrat, simulateur, tests) |

Aucune dépendance : Python 3.7+ et la bibliothèque standard suffisent.

## Lancer

```bash
# simulateur, sans matériel
python3 ma_station.py --sim --scenario journee

# démo réelle : Mac sur le wifi de la station
cp sms.env.exemple sms.env   # puis remplir un fournisseur SMS (facultatif)
./lancer_demo.command
```

- Site client : `http://<ip-du-mac>:9000/p/K1`
- Tableau de bord : `http://localhost:9000/admin`
- Écran jury : `http://localhost:9000/live`

Variables utiles : `KORKO_DEMO=1` (délais courts), `KORKO_ACCEL=15` (1 min réelle = 15 min de session),
`KORKO_PORT`, `KORKO_URL`, `KORKO_ETAT`.

## Sécurité

`sms.env` contient les clés SMS : il est exclu du dépôt par `.gitignore`, comme les photos
et le registre de démo (numéros de téléphone). Ne le publie jamais.
