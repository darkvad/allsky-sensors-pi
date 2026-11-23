# Installation et exploitation

`install.sh` installe les deux services, écrit la configuration, pose les permissions et propose
d'activer I2C / 1-Wire. **Il valide le fichier avec le programme avant de l'installer** : une valeur
refusée (tension manquante, GPIO réservé, mot de passe trop court…) est signalée avant toute
modification. Le script est idempotent : relancé, il remet les fichiers à jour sans toucher à la
configuration (sauf `--keep-config` ou réponse contraire).

## Depuis la release GitHub

Chaque release publie `install.sh`, `allsky-sensors-pi-<version>.tar.gz` et `SHA256SUMS`.

```bash
VERSION=1.0.0
BASE=https://github.com/darkvad/allsky-sensors-pi/releases/download/v$VERSION
curl -fsSLO "$BASE/install.sh"
curl -fsSLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing     # doit répondre « OK »
sudo bash install.sh
```

- l'installateur détecte qu'il n'est pas dans le dépôt, télécharge l'archive de sa version, vérifie
  `SHA256SUMS` et l'extrait dans un dossier temporaire ;
- `--release X.Y.Z` installe une autre version ; `--no-verify` désactive la vérification (essais) ;
- `--local` refuse le téléchargement et exige les fichiers à côté du script ;
- `--version` affiche la version de l'installateur.

> Le dépôt est public : les URLs de release se téléchargent sans authentification. Si tu forkes le
> projet en privé, crée un jeton (scope `repo`) et lance `sudo env GITHUB_TOKEN=ghp_… bash install.sh` ;
> le script l'utilise pour l'archive et `SHA256SUMS` (le jeton n'est jamais journalisé).

Pour une mise à jour : télécharger l'`install.sh` de la nouvelle release, puis
`sudo bash install.sh --upgrade` (le `.env` est conservé).

## Prérequis

- Raspberry Pi sous Raspberry Pi OS (Bookworm ou plus récent, Python 3.10+) ;
- accès réseau au broker Mosquitto (sur l'hôte d'indi-allsky) ;
- capteurs câblés (voir [CABLAGE.md](CABLAGE.md)) ;
- le dépôt cloné sur la Pi :

```bash
git clone https://github.com/darkvad/allsky-sensors-pi.git
cd allsky-sensors-pi
```

## Depuis le dépôt

```bash
sudo ./install.sh
```

Si le bit exécutable n'a pas survécu à la copie : `sudo bash install.sh`.

Le script :

1. installe les paquets manquants (`python3-venv`, `i2c-tools`) ;
2. pose les questions ci-dessous, puis vérifie la configuration avec `validate_env()` ;
3. crée `/opt/allsky-sensors` (755), `state/` (700), `doc/` et le venv Python ;
4. écrit `/etc/allsky-sensors/allsky-sensors.env` en **600**, propriétaire du compte du service
   (une ancienne configuration est sauvegardée en `.bak-AAAAMMJJ-HHMMSS`) ;
5. installe les unités systemd en remplaçant `User=`, puis `enable --now` ;
6. ajoute le compte aux groupes `i2c`, `gpio`, `spi` (s'ils existent) ;
7. propose d'activer I2C et 1-Wire (`raspi-config nonint`, ou édition de `config.txt` avec sauvegarde) ;
8. affiche l'URL de l'interface, les commandes de vérification et, si un mot de passe a été généré,
   le mot de passe **une seule fois**.

Un redémarrage est nécessaire si I2C/1-Wire viennent d'être activés ou si le compte vient d'être
ajouté aux groupes : `sudo reboot`.

## Questions posées

| Section | Question | Défaut | Conseil |
|---|---|---|---|
| Broker | adresse, port, utilisateur, mot de passe, préfixe | `localhost`, `1883`, `allsky-pi`, vide, `allsky` | l'adresse du broker d'indi-allsky ; le mot de passe peut être vide mais c'est déconseillé |
| Capteurs | liste `bme280,ds18b20,mlx90614` | `bme280,ds18b20` | `mlx90614` seulement s'il est câblé sur la Pi |
| Capteurs | adresse I2C, altitude, identifiant DS18B20, offsets | `0x76`, vide, détecté, `0` | l'identifiant DS18B20 est proposé s'il est détecté ; vide = premier détecté |
| Ciel MQTT | topic JSON du SQMeter, chemins, fraîcheur | vide, `infrared.skyTemperature`, `dataStale`, `infrared.status`, `600` | `sqmeter/state` pour le SQMeter ; vide = pas de source externe |
| Anticipation | marge par ciel dégagé, seuil, hystérésis | `0`, `-10`, `2` | laisser `0` quelques nuits, puis 1 à 3 °C |
| Chauffage | activé, tension, GPIO réservés, GPIO, résistances, marge, coupure, PWM | non, —, `17,18,22,27`, `23`, `6 × 47 Ω 1 W`, `4 °C`, `35 °C`, `100 Hz` | la tension est **obligatoire** si le chauffage est activé ; GPIO 2, 3, 4 et réservés refusés |
| Interface web | adresse, port, utilisateur, mot de passe | `0.0.0.0`, `8080`, `admin`, généré | 12 caractères minimum ; vide = mot de passe aléatoire affiché une fois |

Répondre à une question par Entrée accepte la valeur entre crochets. Un mot de passe fourni par
l'environnement (`MQTT_PASSWORD=… sudo -E ./install.sh`) n'est pas redemandé.

## Options du script

| Option | Effet |
|---|---|
| `--user COMPTE` | compte qui fait tourner les services (défaut : compte qui lance `sudo`) |
| `--prefix DOSSIER` | dossier du programme (défaut `/opt/allsky-sensors`) |
| `--config-dir DOSSIER` | dossier de configuration (défaut `/etc/allsky-sensors`) |
| `--non-interactive` | aucune question : lit les réponses dans l'environnement, utilise les défauts |
| `--keep-config` | conserve un `.env` existant (nouvelle installation) |
| `--no-services` | n'installe pas les unités systemd (essais) |
| `--no-packages` | n'installe aucun paquet système |
| `--no-boot-config` | ne touche pas à `config.txt` (I2C / 1-Wire) |
| `--recreate-venv` | recrée l'environnement Python |
| `--release VERSION` | installe cette release GitHub (téléchargée et vérifiée) |
| `--local` | exige les sources à côté du script (aucun téléchargement) |
| `--no-verify` | ne vérifie pas `SHA256SUMS` (essais seulement) |
| `--version` | affiche la version de l'installateur |
| `--upgrade` | met à jour le programme sans modifier la configuration |
| `--uninstall` | arrête et retire les services |
| `--dry-run` | affiche les actions sans rien modifier |
| `--help` | résumé des options |

Exemple d'installation automatisée (le mot de passe web est fourni, donc pas de question) :

```bash
sudo MQTT_HOST=192.168.155.248 MQTT_USER=allsky-pi MQTT_PASSWORD='…' \
     SKY_TEMP_TOPIC=sqmeter/state HEATER_ENABLED=0 \
     WEB_PASSWORD='mot-de-passe-de-12-caracteres' \
     ./install.sh --non-interactive
```

En `--non-interactive`, toutes les variables de configuration peuvent être fournies ; les autres
prennent leur défaut. Une valeur invalide arrête le script avant toute écriture.

## Après l'installation

```bash
journalctl -u allsky-sensors -f          # mesures, chauffage, rechargement
journalctl -u allsky-sensors-web -f      # requêtes de l'interface
cd /opt/allsky-sensors
./venv/bin/python allsky_sensors_mqtt.py --print-topics
curl -u admin:'<mot-de-passe>' http://127.0.0.1:8080/sante
```

- interface : `http://<adresse-de-la-pi>:8080/` (état, `/chauffage`, `/reglages`, `/sante`) ;
- si le chauffage est activé, faire d'abord les essais **à vide** du README (`--heater-test`) ;
- la configuration se modifie dans `/reglages` (appliquée à chaud) ou dans
  `/etc/allsky-sensors/allsky-sensors.env` (relu au tick suivant).

## Mise à jour

```bash
cd allsky-sensors-pi
git pull
sudo ./install.sh --upgrade
```

`--upgrade` copie les programmes, réinstalle les dépendances Python, réécrit les unités et redémarre
les services. **Le `.env` n'est pas modifié** ; pour le changer, passe par `/reglages` ou édite-le.

## Sauvegarde et restauration

Le fichier à sauvegarder est `/etc/allsky-sensors/allsky-sensors.env` (600). Il contient les mots de
passe MQTT et web : garde la copie en 600, hors du dépôt. L'état (`/opt/allsky-sensors/state`) est
recréé tout seul.

```bash
sudo install -m 600 /etc/allsky-sensors/allsky-sensors.env /root/allsky-sensors.env.$(date +%F)
# restauration
sudo install -m 600 /root/allsky-sensors.env.AAAA-MM-JJ /etc/allsky-sensors/allsky-sensors.env
sudo systemctl restart allsky-sensors allsky-sensors-web
```

## Désinstallation

```bash
sudo ./install.sh --uninstall
```

Le script arrête et retire les unités, sauvegarde le `.env` dans `/root/`, puis demande si le
programme et la configuration peuvent être supprimés. Le compte et les paquets système sont conservés.


## Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `lance ce script avec sudo` | lancé sans privilèges | `sudo ./install.sh` |
| `compte inconnu` / service qui ne démarre pas | `--user` mal orthographié | `id <compte>` puis relancer |
| `python3-venv` introuvable | paquet absent | `sudo apt install python3-venv` ou laisser le script le faire |
| `capteur bme280 indisponible` | I2C non activé, câblage, adresse | `i2cdetect -y 1` doit montrer `76`/`77` ; activer I2C puis redémarrer |
| `capteur ds18b20 indisponible` | 1-Wire non activé, sonde absente | `ls /sys/bus/w1/devices/` doit montrer `28-…` ; voir CABLAGE.md |
| service actif mais aucune mesure | mauvais broker / identifiants | `journalctl -u allsky-sensors` ; tester `mosquitto_sub` |
| `Read-only file system` sur `/reglages` | `ReadWritePaths` manquant | ajouter `/etc/allsky-sensors` à l'unité web, `daemon-reload`, `restart` |
| interface inaccessible | bind `127.0.0.1` ou port occupé | `ss -ltnp` puis filtrer sur 8080 ; changer `WEB_BIND`/`WEB_PORT` |
| mot de passe web refusé au démarrage | moins de 12 caractères | corriger `WEB_PASSWORD`, `systemctl restart allsky-sensors-web` |
| `GPIO … déjà utilisé` | GPIO 2, 3, 4 ou `GPIO_RESERVED` | choisir un autre GPIO (23 par défaut) |
| chauffage refusé au démarrage | `HEATER_SUPPLY_VOLTS` absent | renseigner la tension réelle, `systemctl restart allsky-sensors` |
| aucune valeur `sky_temperature` | topic SQMeter mal orthographié ou ACL | `mosquitto_sub -t 'sqmeter/#' -v` ; vérifier `SKY_TEMP_TOPIC` |
| `Anticipation inactive` | `SKY_ANTICIPATION_C=0` | c'est normal : mettre 1 à 3 °C pour l'activer |

## Fichiers installés

```
/opt/allsky-sensors/
├── allsky_sensors_mqtt.py     programme des capteurs et du chauffage
├── web_api.py                 interface web
├── requirements.txt
├── doc/                       README, INSTALLATION, CABLAGE
├── state/                     override.json, status.json (700)
└── venv/                      environnement Python

/etc/allsky-sensors/
└── allsky-sensors.env         600, propriétaire du compte du service

/etc/systemd/system/
├── allsky-sensors.service
└── allsky-sensors-web.service
```

## Sécurité

- l'interface web n'a **pas de HTTPS** : elle est prévue pour le réseau local. Depuis l'extérieur,
  passe par un tunnel SSH (`ssh -L 8080:localhost:8080 pi@indicam-pi`) ou un reverse proxy TLS ;
- Basic auth fait circuler le mot de passe en clair sur le LAN : mot de passe long (12 caractères
  minimum, généré par défaut), jamais réutilisé ailleurs ;
- le `.env` est en 600 et n'est **jamais** versionné : ne le colle pas dans un ticket ou un forum ;
- le programme et les unités tournent avec le compte du service, pas en root ; GPIO 2, 3, 4 refusés ;
- le chauffage reste limité par le dérating (0,5 W par résistance), la coupure à
  `HEATER_MAX_DOME_TEMP` et la coupure sur mesure périmée — l'anticipation ne contourne rien.

## Mise à jour d'une installation existante (avant ce script)

Si les services tournent déjà avec l'ancienne installation manuelle, `sudo ./install.sh` détecte le
`.env` existant, le sauvegarde en `.bak-…` et le remplace par la configuration issue des questions.
Pour **garder** l'ancien fichier tel quel, répondre « non » à la question d'écriture ou relancer avec
`--keep-config` ; les unités et le venv sont quand même mis à jour.

