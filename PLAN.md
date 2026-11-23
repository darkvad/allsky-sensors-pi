# Plan de travail — forçage du chauffage et interface web

Document de suivi : le plan de référence (validé le 22/11/2026), l'avancement étape par étape, et
pour chaque étape **ce qu'il faut tester pour la valider**. Le plan d'origine de la session
précédente a été écarté ; celui-ci le remplace.

> Règle du projet rappelée dans `agents.md` : aucun `git push` sans demande expresse de l'utilisateur.

## 1. Objectif

Deux fonctionnalités demandées :

1. **Forcer le chauffage** en imposant une température à atteindre, sans tenir compte du point de rosée.
2. **Une interface web** pour forcer le chauffage et modifier les paramètres de
   `/etc/allsky-sensors/allsky-sensors.env`, ces modifications étant prises en compte à la sauvegarde.

## 2. Décisions d'architecture

| Sujet | Décision | Raison |
|---|---|---|
| Processus | Deux services systemd : `allsky-sensors` (capteurs, chauffage, GPIO) et `allsky-sensors-web` (HTTP) | la boucle de sécurité ne dépend ni du web ni de ses plantages ; l'interface reste disponible pour diagnostiquer une panne de capteur |
| Couplage | Fichiers d'état dans `<dossier du programme>/state`, soit `/opt/allsky-sensors/state` sur la Pi (variable `STATE_DIR` pour le déplacer) | tout reste dans le dossier d'installation, comme le reste du projet ; aucun `/var/lib` à créer |
| Consigne de forçage | `override.json`, écrit par le web, lu par le service à chaque tick (10 s) | application immédiate, sans signal ni redémarrage |
| Rechargement des réglages | comparaison du mtime de `ENV_FILE` à chaque tick | « pris en compte à la sauvegarde » sans privilèges ni IPC |
| État en direct | `status.json`, écrit par le service à chaque publication | permet de vérifier qu'un forçage agit, et d'afficher les erreurs de configuration |
| Dépendances | aucune nouvelle (stdlib : `http.server`, `json`, `base64`, `hmac`) | cohérent avec le projet (4 dépendances matérielles) |
| Validation | `validate_env()` et `HeaterConfig` partagés entre démon et web | une seule vérité, messages d'erreur identiques |

## 3. Contrats

### `override.json` (étape 1 ✅)

```json
{"version": 1, "mode": "force", "target_c": 18.0, "minutes": 60,
 "created_at": 1763990000.0, "expires_at": 1763993600.0}
```

- consigne bornée entre −20 °C et `HEATER_MAX_DOME_TEMP` ; expiration plafonnée à 24 h ;
- consigne expirée → fichier supprimé, retour en automatique ; fichier inexploitable → conservé, motif journalisé ;
- supprimé au démarrage du service et si `HEATER_ENABLED=0` (un redémarrage repart toujours en automatique).

### `status.json` (étape 3 ⏳)

```json
{"updated_at": 0.0, "mode": "auto", "target_c": null, "override_expires_at": null,
 "dome_c": 9.5, "dew_c": -2.3, "duty_pct": 0.0, "readings": {}, "sensors_unavailable": [],
 "config_error": null, "settings_mtime": 0.0, "pid": 0}
```

### Commandes de forçage (étape 1 ✅)

Par MQTT, sur le service **en marche** (rien d'autre à lancer, aucun second processus) :

```bash
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m '18'       # 18 °C, 60 min
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m '18 30'    # 18 °C, 30 min
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m 'cancel'   # retour automatique
```

Par ligne de commande (processus éphémère : écrit la consigne et rend la main, sans GPIO ni MQTT) :

```bash
python3 allsky_sensors_mqtt.py --heater-force 18               # 18 °C, pendant 60 min
python3 allsky_sensors_mqtt.py --heater-force 18 --heater-force-minutes 15
python3 allsky_sensors_mqtt.py --heater-cancel                 # retour au point de rosée + marge
```

Dans les deux cas, seule la consigne (`override.json`) est écrite ; c'est le service qui applique, au
tick suivant (10 s au plus) : une seule main sur le GPIO. Le fichier d'environnement (`ENV_FILE`, par
défaut `/etc/allsky-sensors/allsky-sensors.env`) est lu par la commande, donc elle fonctionne par SSH sans exporter
`HEATER_*` à la main.

### Topics MQTT

| Topic | Contenu | Dans `--print-topics` |
|---|---|---|
| `allsky/dome_heater_pwm` | rapport cyclique (%) | oui (existant) |
| `allsky/heater_mode` | `auto` ou `force` | **non** (état, pas une mesure) |
| `allsky/heater_target` | cible effective (°C) | **non** |
| `allsky/heater_command` | **entrant** : `18`, `18 30` ou `cancel` ; QoS 1, **jamais retenu** | **non** |

### Variables d'environnement ajoutées

| Variable | Défaut | Étape | Rôle |
|---|---|---|---|
| `STATE_DIR` | `<dossier du programme>/state` (soit `/opt/allsky-sensors/state`) | 1 ✅ | dossier d'état (consigne, état publié) |
| `ENV_FILE` | (vide : pas de rechargement) | 3 | fichier `.env` surveillé ; lu par `--heater-force` |
| `WEB_BIND` / `WEB_PORT` | `0.0.0.0` / `8080` | 4 | écoute du serveur web |
| `WEB_USER` / `WEB_PASSWORD` | `admin` / (obligatoire, ≥ 12 caractères) | 4 | authentification |

## 4. Avancement

| # | Étape | État | Preuve |
|---|---|---|---|
| 1 | Cœur : forçage du chauffage | ✅ **terminé** | 160 tests (157 verts + 2 ignorés + 1 échec pré-existant sous Windows), `ruff` OK |
| 1b | Commande MQTT du chauffage en marche | ✅ **terminé** | **reçue de bout en bout le 22/11** (test paho : écho du broker, `override.json`, `heater_mode force`, `cancel` → `auto`) |
| 2 | Cœur : configuration (`.env`, schéma, `status.json`) | ✅ **terminé** | 30 variables, `validate_env`, `Settings`, `update_env_file`, `status.json` ; branchement boucle à l'étape 3 |
| 3 | Cœur : rechargement à chaud | ✅ **terminé** | mtime + `Settings` reconstruits ; version **déployée sur la Pi** (état chauffage publié toutes les 10 s, constaté le 22/11) ; rechargement lui-même à déclencher à la main |
| 4 | Web : socle (serveur, authentification, page d'état) | ✅ **terminé** | `web_api.py` + unité durcie ; 26 tests dans `tests/test_web_api.py` (serveur réel sur le port 0), 183 verts au total |
| 5 | Web : formulaire de réglages | ✅ **terminé** | formulaire depuis `ENV_SCHEMA`, `POST /reglages` (303) + jeton anti-CSRF, 46 tests web, 203 verts au total |
| 6 | Web : commandes de forçage | ⏳ à faire | |
| 7 | Déploiement, doc, README | ⏳ à faire | |

## 5. Détail des étapes

### Étape 1 — Cœur : forçage du chauffage ✅

Livré dans `allsky_sensors_mqtt.py` :

- `HeaterOverride`, `state_dir()`, `override_path()`, `write_json_atomic()`, `clear_override()`,
  `load_override()` (bornage, plafond 24 h, purge des consignes expirées, motifs d'ignorance) et
  `write_override()` (validation 5 min à 24 h, écriture atomique) ;
- `HeaterController.update(..., target=)` : mode forcé qui ignore le point de rosée, sécurités
  inchangées (dôme périmé > 120 s, dôme ≥ `HEATER_MAX_DOME_TEMP`, plafond `max_duty`),
  remise à zéro de l'intégrale au changement de mode, propriété `setpoint` ;
- publication de `heater_mode` et `heater_target` ; journal avec mention ` (forcé)` ;
- purge de la consigne au démarrage et si `HEATER_ENABLED=0` ;
- commandes `--heater-force` / `--heater-force-minutes` / `--heater-cancel`, qui lisent le fichier
  d'environnement (`read_env_file`, tolérant au BOM) : utilisables par SSH sans exporter les variables ;
  `--heater-test`, `--heater-cancel` et `--print-topics` le lisent aussi (corrigé : ils ne voyaient que
  l'environnement du shell, que systemd ne fournit qu'au service) — le préfixe MQTT affiché vient du fichier ;
- dossier d'état `<dossier du programme>/state` (donc `/opt/allsky-sensors/state`), surchargeable par
  `STATE_DIR` ; absent de git (`.gitignore`) ;
- **commande MQTT** : le service s'abonne à `<préfixe>/heater_command` (QoS 1, non retenu) quand le
  chauffage est activé ; `apply_heater_command()` accepte « 18 », « 18 30 », « 17,5 30 », « cancel »
  (et refuser proprement le reste). Une commande refusée ne laisse aucune consigne derrière elle.

**Tester l'étape 1 (sans matériel, sous Windows ou Linux) :**

```powershell
$env:HEATER_ENABLED='1'; $env:HEATER_SUPPLY_VOLTS='12'; $env:STATE_DIR="$env:TEMP\allsky-demo"
Remove-Item -Recurse -Force "$env:TEMP\allsky-demo" -ErrorAction SilentlyContinue
python allsky_sensors_mqtt.py --simulate --dry-run --once
```

Attendu : `allsky/dome_heater_pwm`, `allsky/heater_mode auto`, `allsky/heater_target` égal à
(point de rosée + 4 °C), et la ligne de journal `dôme … °C, rosée … °C, cible … °C, chauffage … %`.

```powershell
python allsky_sensors_mqtt.py --heater-force 18 --heater-force-minutes 30   # consigne enregistrée
Get-Content "$env:TEMP\allsky-demo\override.json"
python allsky_sensors_mqtt.py --heater-cancel                              # consigne supprimée
```

Attendu : `consigne de forçage enregistrée : 18 °C pendant 30 min (jusqu'à HH:MM)`, le fichier JSON
correspondant, puis `consigne de forçage supprimée`.

Sur la Pi (en production : le service n'est pas redémarré), après avoir créé le dossier d'état une fois :

```bash
sudo install -d -m 700 -o TON_UTILISATEUR -g TON_UTILISATEUR /opt/allsky-sensors/state
journalctl -u allsky-sensors -f     # suivre l'effet
```

**Test 1 — commander le service en marche par MQTT** (aucun processus à lancer à côté) :

```bash
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m '18'
```

Attendu, dans le journal, immédiatement : `commande de chauffage reçue ('18') : 18 °C pendant 60 min
(jusqu'à HH:MM)`, puis au tick suivant (≤ 10 s) : `dôme … °C, cible 18.0 °C, chauffage X % (forcé)`.
`mosquitto_sub -t 'allsky/heater#' -v` montre `heater_mode force`, `heater_target 18.00`.

```bash
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m '18 30'   # 30 min
mosquitto_pub -h localhost -u allsky-pi -P MOT_DE_PASSE -t allsky/heater_command -m 'cancel'  # retour auto
```

Les commandes invalides (`40`, `chaud`, `18 1`) sont refusées avec un message clair et ne changent
rien ; la consigne expire seule ; un redémarrage du service la supprime.

**Test 2 — même chose depuis la ligne de commande** (processus éphémère, sans GPIO ni MQTT) :

```bash
sudo chown TON_UTILISATEUR /etc/allsky-sensors/allsky-sensors.env && sudo chmod 600 /etc/allsky-sensors/allsky-sensors.env  # une fois
sudo -u TON_UTILISATEUR /opt/allsky-sensors/venv/bin/python /opt/allsky-sensors/allsky_sensors_mqtt.py --heater-force 18
```

Attendu : `consigne de forçage enregistrée : 18 °C pendant 60 min (jusqu'à HH:MM)` puis le même
comportement que ci-dessus ; `--heater-cancel` pour revenir en automatique.

**Diagnostic MQTT sans `mosquitto-clients`** (le venv contient déjà `paho-mqtt`) : ce script publie
`20` sur le topic de commande *et* écoute `allsky/#` quelques secondes. Il prouve du même coup que le
broker est le bon, que le préfixe est le bon et que le message part.

> ⚠️ Version d'origine, conservée pour l'historique : elle n'attendait que 4 s la réaction (or
> `heater_mode` n'est publié qu'à chaque `INTERVAL`, 60 s) et ne vérifiait pas l'écho du broker. Le
> script qui fait foi, validé le 22/11, est celui du §9 (« Outil de diagnostic MQTT »).

```bash
sudo /opt/allsky-sensors/venv/bin/python - <<'PY'
import time
import paho.mqtt.client as mqtt

env = {}
with open("/etc/allsky-sensors/allsky-sensors.env", encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()

prefix = env.get("MQTT_PREFIX", "allsky")
topic = f"{prefix}/heater_command"
host = env.get("MQTT_HOST", "localhost")
port = int(env.get("MQTT_PORT", "1883"))
print("broker :", host, port, "| topic de commande :", topic)

try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="allsky-diagnostic")
except AttributeError:
    client = mqtt.Client(client_id="allsky-diagnostic")
client.username_pw_set(env.get("MQTT_USER", ""), env.get("MQTT_PASSWORD", ""))


def on_connect(client, userdata, flags, reason_code, *rest):
    client.subscribe(f"{prefix}/#", qos=1)
    client.publish(topic, "20", qos=1)
    print("abonné à", f"{prefix}/#", "et publié 20 sur", topic)


def on_message(client, userdata, message):
    print("reçu", message.topic, "=", message.payload.decode("utf-8", "replace"))


client.on_connect = on_connect
client.on_message = on_message
client.connect(host, port, 60)
client.loop_start()
time.sleep(4)
client.loop_stop()
client.disconnect()
PY
```

Lecture du résultat :

- `reçu allsky/heater_mode = force` et `reçu allsky/heater_target = 20.00` → le broker, le préfixe et
  la commande sont bons : si MQTT Explorer ne fonctionne pas, c'est *lui* qui n'est pas branché sur ce
  broker (ou mauvais topic/format) ;
- `reçu allsky/dome_temperature …` mais pas de `heater_mode force` → la commande est arrivée, mais le
  chauffage n'est pas dans le bon mode : regarder le journal (`commande de chauffage reçue`,
  `commande de chauffage refusée`) ;
- rien du tout → mauvais broker, utilisateur sans droit de lecture, ou broker arrêté.

Contrôles automatiques : `python -m pytest -q` (148 verts) et `python -m ruff check .`.

### Étape 2 — Cœur : configuration ✅

Livré dans `allsky_sensors_mqtt.py` :

- `read_env_file()` (tolérant au BOM) et `update_env_file()` : réécriture **en place** des seules clés
  concernées, commentaires / ordre / variables inconnues préservés, liste blanche de clés (aucune
  variable arbitraire injectable), refus des sauts de ligne, écriture atomique en 600, copie `.bak` ;
- `ENV_SCHEMA` : 30 variables décrites (section, libellé, type, bornes, unité, aide, caractère secret,
  « redémarrage nécessaire »), source unique pour la validation, le formulaire web et la doc ;
- `Param.validate_value()` et `validate_env(env)` : messages d'erreur en français, « 4,5 » accepté,
  adresses I²C en hexadécimal, GPIO vérifiés, puis validation d'ensemble du chauffage ;
- `ConfigError` (porte la liste des messages) et `Settings.from_env()` : intervalle, altitude,
  capteurs activés, offsets, adresses, configuration MQTT et chauffage, validés en une passe ;
- `status_path()` / `write_status()` / `read_status()` : état publié pour l'interface web
  (écriture atomique, fichier lisible sans secret, versionné, tolérant à un fichier absent ou abîmé) ;
- `HeaterConfig` lit désormais les nombres avec tolérance de la virgule, et **refuse** un
  `HEATER_ENABLED` qui ne vaut ni `0` ni `1` (un `true` ou un `oui` désactivait le chauffage en silence).

Le branchement de la boucle sur `Settings` se fait à l'étape 3, qui doit de toute façon reconstruire
la configuration à chaud.

**Tester l'étape 2 (sans matériel) :**

```powershell
python -m pytest -q -k "parameter or validation or settings or env_update or status or schema"
python -c "import allsky_sensors_mqtt as s; print(len(s.ENV_SCHEMA)); print(s.validate_env({'INTERVAL': '5'}))"
```

Attendu : tests verts, `30`, et `['INTERVAL doit être au moins 10 s']`.

Essai d'écriture du fichier d'environnement sur une copie (commentaires et ordre préservés) :

```powershell
python -c "import shutil, pathlib, os, allsky_sensors_mqtt as s; d = pathlib.Path(os.environ['TEMP']) / 'allsky-test.env'; shutil.copy('allsky-sensors.env.example', d); s.update_env_file(d, {'HEATER_MARGIN': '6'}); print([l for l in d.read_text(encoding='utf-8').splitlines() if 'MARGIN' in l]); print('backup:', d.with_name('allsky-test.env.bak').exists())"
```

Attendu : `['HEATER_MARGIN=6']` et `backup: True`.

Contrôles automatiques : `python -m pytest -q` (157 verts) et `python -m ruff check .`.

### Étape 3 — Cœur : rechargement à chaud ✅

Livré dans `allsky_sensors_mqtt.py` et `allsky-sensors.service` :

- `Settings.state_dir` et `Runtime` / `build_runtime()` / `close_runtime()` : capteurs et chauffage
  construits depuis une configuration validée, et libérés proprement (GPIO) avant reconstruction ;
- `ENV_FILE` surveillé par `_file_mtime()` : au tick suivant la modification, le service relit le
  fichier, reconstruit capteurs, chauffage et `PWMOutput` (si GPIO/fréquence changent), et le
  publisher MQTT (si les réglages MQTT changent) ; `unavailable` est remis à zéro ;
- configuration invalide → **rien n'est appliqué** : chaque erreur est journalisée et `config_error`
  apparaît dans `status.json` ; le service continue avec l'ancienne configuration ;
- `status.json` écrit à chaque tick : mode, cible, consigne forcée et son expiration, dôme, rosée,
  rapport cyclique, mesures retenues, capteurs indisponibles, `config_error`, `settings_mtime`, PID ;
- `dome_heater_pwm`, `heater_mode` et `heater_target` publiés **toutes les 10 s** (les mesures restent
  à `INTERVAL`) : la PWM vue sur MQTT suit les changements en moins de 10 s ;
- `allsky-sensors.service` : `Environment=ENV_FILE=/etc/allsky-sensors/allsky-sensors.env` ;
- `configured_env_file()` / `service_env()` : le service lit **lui-même** son fichier (le fichier gagne
  sur l'instantané fourni par systemd), et retombe sur `/etc/allsky-sensors/allsky-sensors.env` si `ENV_FILE` n'est pas
  déclaré — une unité systemd mal écrite ne peut donc plus laisser le service sans configuration.

**Tester l'étape 3 (sur la Pi) :**

```bash
sudo systemctl restart allsky-sensors
sudoedit /etc/allsky-sensors/allsky-sensors.env     # par exemple : HEATER_MARGIN=4 devient 6
journalctl -u allsky-sensors -f
```

Attendu, au tick suivant (≤ 10 s avec chauffage) : `configuration rechargée depuis
/etc/allsky-sensors/allsky-sensors.env`, puis la ligne habituelle avec une cible égale à **rosée + 6 °C** — sans
redémarrage, PWM maintenue.

```bash
cat /opt/allsky-sensors/state/status.json
```

Attendu : `version`, `updated_at` récent, `settings_mtime`, `mode`, `target_c`, `duty_pct`,
`readings`, `config_error: null`.

Puis une valeur fausse (`HEATER_MARGIN=50`) : le service **continue**, journalise
`configuration non appliquée : HEATER_MARGIN doit être au plus 10 °C`, et `config_error` devient non
nul dans `status.json` ; l'ancienne valeur reste active. Corrige ensuite la valeur : `config_error`
revient à `null`.

Sans matériel : `python -m pytest -q -k "reload or status or tick"`.

Contrôles automatiques : `python -m pytest -q` (157 verts) et `python -m ruff check .`.

### Étape 4 — Web : socle ✅

Livré dans `web_api.py` (stdlib : `ThreadingHTTPServer`, `base64`, `hmac`) et `allsky-sensors-web.service` :

- Basic auth obligatoire, comparaison en temps constant (`hmac.compare_digest`) et `WWW-Authenticate`
  en UTF-8 ; `WEB_PASSWORD` de 12 caractères au moins, sinon **refus de démarrer** (code de retour 2) ;
  utilisateur et port validés avec des messages en français ;
- `GET /` : page d'état en UTF-8, sans JavaScript (`meta refresh` de 10 s) — mode et cible, consigne
  forcée avec le temps restant, dôme, rosée, PWM, âge des mesures et des réglages, PID, mesures avec
  unités (virgule décimale), `config_error` et capteurs indisponibles en encadré ; tout est échappé ;
- `GET /sante` : JSON (`etat`, `mise_a_jour_s`, `mode`, `config_error`, `pid`) — **200** si `status.json`
  a moins de 300 s, **503** sinon (supervision) ;
- POST : `Origin`/`Referer` obligatoire et identique à l'en-tête `Host` (403 sinon), corps ≤ 64 Kio (413),
  `Content-Length` obligatoire (411) ; aucune action branchée avant les étapes 5 et 6 ;
- en-têtes `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: no-referrer`, CSP
  `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'` ; en-tête `Server` sans version de
  Python ; journal dans syslog via `logging` ;
- unité durcie : `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
  `ProtectKernel*`, `RestrictAddressFamilies=AF_INET AF_INET6`, `NoNewPrivileges`,
  `ReadWritePaths=/opt/allsky-sensors/state /etc/allsky-sensors` (le **dossier**, pas le fichier : l'écriture
  atomique crée `.tmp` puis renomme, ce qui exige un droit d'écriture sur le répertoire) ;
- **décision** : pas de `StateDirectory` (l'état vit dans `/opt/allsky-sensors/state`, conformément au
  §2) ; `ReadWritePaths` couvre l'état et le fichier de réglages ;
- `allsky-sensors.env.example` : section « Interface web », `WEB_PASSWORD=` vide par défaut — tant qu'il
  est vide, le service web refuse de démarrer plutôt que d'écouter sans mot de passe.

**Tester (sans matériel, sous Windows) :**

```powershell
python -m pytest -q tests/test_web_api.py          # 26 tests, serveur réel sur le port 0
$env:WEB_BIND='127.0.0.1'; $env:WEB_PORT='8080'; $env:WEB_PASSWORD='mot-de-passe-de-test'
python web_api.py                                   # puis http://127.0.0.1:8080/ (admin)
```

Attendu : 401 sans identifiants (invite du navigateur), page en français, `/sante` en JSON. Le fichier
d'état est celui de `STATE_DIR` (ou `--status-file`).

**Sur la Pi :**

```bash
sudo install -d -m 750 -o pi -g pi /etc/allsky-sensors   # une fois : dossier inscriptible par le web
sudoedit /etc/allsky-sensors/allsky-sensors.env          # WEB_PASSWORD (12 caractères au moins), WEB_USER, WEB_PORT
sudo install -m 644 web_api.py /opt/allsky-sensors/web_api.py
sudo install -m 644 allsky-sensors-web.service /etc/systemd/system/allsky-sensors-web.service
sudo systemctl daemon-reload && sudo systemctl enable --now allsky-sensors-web
journalctl -u allsky-sensors-web -f
curl -u admin:LE_MOT_DE_PASSE http://indicam-pi:8080/sante
```

Critères tenus : sans identifiants → 401 ; page en UTF-8/français ; POST d'une autre origine → 403 ;
aucun secret dans le HTML ; tests sur le port 0.

### Étape 5 — Web : formulaire de réglages ✅

Livré dans `web_api.py` :

- `GET /reglages` : formulaire **généré depuis `ENV_SCHEMA`** — un `fieldset` par section, ordre du
  schéma, libellés et unités, aide, badge « au redémarrage » pour `restart=True`, cases à cocher pour
  `ENABLED`, liste 0/1 pour les `flag`, `inputmode="decimal"` (la virgule française est acceptée) ;
- secrets (`MQTT_PASSWORD`, `WEB_PASSWORD`) rendus **vides** avec « vide : inchangé » : leur valeur
  n'est jamais renvoyée au navigateur ;
- `POST /reglages` : `Content-Type: application/x-www-form-urlencoded` exigé (415 sinon), `Origin`
  vérifié (403), corps ≤ 64 Kio (413) ; seules les clés du schéma sont lues (une variable arbitraire
  est ignorée) ; comparaison avec les valeurs effectives pour ne pas réécrire sans changement ;
- validation `validate_env({**env, **changes})` : en cas d'erreur, **rien n'est écrit** et le formulaire
  est réaffiché avec la saisie et les messages ; sinon `update_env_file` (atomique, `.bak`, 0600), puis
  **redirection 303** vers `/reglages?applique=HH:MM:SS` (+ `&redemarrage=1` si une variable marquée le
  demande) — pas de renvoi de formulaire au rafraîchissement ;
- **anti-CSRF** : jeton caché `_jeton` (HMAC-SHA256 du mot de passe web) exigé sur chaque POST, comparé
  en temps constant ; le contrôle `Origin`/`Referer` reste, mais **ne refuse plus une requête qui n'en
  annonce aucun, ni un `Origin: null`** (voir l'incident du 22/11 au §9) ; la comparaison porte sur la
  machine (et sur le port seulement s'il est annoncé des deux côtés) ; `Referrer-Policy: same-origin`
  (et non `no-referrer`, qui faisait sérialiser `Origin` à `null`) ;
- journal : noms des variables enregistrées, **jamais les valeurs** ; `409` si `ENV_FILE` est absent ou
  introuvable ; la page d'état renvoie vers `/reglages`.

**Tester (sans matériel) :**

```powershell
python -m pytest -q tests/test_web_api.py     # 46 tests, serveur réel sur le port 0
```

Chaîne complète vérifiée le 22/11 (script jetable, supprimé depuis) : formulaire de 8,9 Ko, `POST`
`HEATER_MARGIN=6` → 303 puis « Réglages enregistrés à 23:35:05 » ; fichier réécrit (commentaire d'en-tête,
ordre et secrets conservés, `.bak` identique à l'ancien) ; service relancé avec le même `ENV_FILE` :
`dôme 12,7 °C, rosée 8,7 °C, cible 14,7 °C` — la marge de 6 est appliquée.

Critères tenus : hors bornes → fichier intact ; enregistrement valide → commentaires et ordre conservés ;
aucun changement → fichier non réécrit (donc pas de rechargement inutile).

### Étape 6 — Web : commandes de forçage ⏳

À livrer : `POST /forcer` (`target_c`, `minutes`, bornés par `HEATER_MAX_DOME_TEMP`, 5 min à 24 h),
`POST /forcer/annuler`, refus explicite si `HEATER_ENABLED=0`, affichage du mode, de la consigne et
du temps restant ; écriture via `write_override`. Les deux formulaires portent le jeton `_jeton` de
l'étape 5 (même contrôle anti-CSRF, même redirection 303).

Critères : forçage visible dans `status.json`/MQTT au tick suivant ; consigne > `HEATER_MAX_DOME_TEMP`
refusée avec message clair ; annulation → retour `auto` immédiat.

**Tester (sera précisé en fin d'étape).**

### Étape 7 — Déploiement et documentation ⏳

À livrer : `allsky-sensors.service` (`Environment=ENV_FILE=`, `StateDirectory=allsky-sensors`),
`allsky-sensors.env.example` (section « Interface web »), README (interface web et sécurité :
réseau local uniquement, mot de passe fort, tunnel SSH ; chapitre forçage ; sécurités ; installation
des deux unités ; `--print-topics` et topics d'état ; nombre de tests à jour), `agents.md` inchangé.

Critères : installation décrite de bout en bout sur une Pi vierge ; `pytest` et `ruff` verts ;
aucun secret dans le dépôt.

**Tester (sera précisé en fin d'étape).**

## 6. Sécurités à ne jamais contourner

- plafond de puissance `max_duty` (dérating 0,5 W par résistance) ;
- coupure si le dôme atteint `HEATER_MAX_DOME_TEMP` ;
- coupure si la mesure du dôme manque depuis plus de 120 s ;
- coupure à l'arrêt du programme (PWM logicielle) et chien de garde systemd ;
- une consigne forcée ne dispense **que** de la mesure du point de rosée ;
- expiration systématique d'un forçage (60 min par défaut, 24 h au maximum) ;
- GPIO 2, 3, 4 et `GPIO_RESERVED` refusés.

## 7. Hors périmètre

Pas de HTTPS intégré (tunnel SSH ou reverse proxy), pas de comptes multiples, pas d'historique ni de
graphiques, pas de pilotage depuis Internet (MQTT et web restent sur le réseau local), aucune
dépendance nouvelle.

## 8. Points ouverts

- port 8080 et écoute `0.0.0.0` avec Basic auth obligatoire (à confirmer à l'étape 4) ;
- conservation d'un seul `.bak` du fichier d'environnement (à confirmer à l'étape 5).

## 9. État de reprise (22/11/2026, après le test MQTT de bout en bout)

### Dépôt local

| Fichier | État |
|---|---|
| `allsky_sensors_mqtt.py` | **modifié** : forçage (fichier + MQTT), schéma/validation/`Settings`, `update_env_file`, rechargement à chaud, `status.json`, état chauffage publié chaque 10 s |
| `web_api.py` | **nouveau** : interface web — socle (Basic auth, `/`, `/sante`), **formulaire de réglages** (`GET/POST /reglages`, jeton anti-CSRF) |
| `allsky-sensors-web.service` | **nouveau** : unité durcie de l'interface |
| `allsky-sensors.service` | **modifié** : `Environment=ENV_FILE=/etc/allsky-sensors/allsky-sensors.env` |
| `allsky-sensors.env.example` | **modifié** : section « Interface web » (`WEB_*`, `WEB_PASSWORD` vide par défaut) |
| `tests/test_allsky_sensors.py` | **modifié** : 160 tests (157 verts, 2 ignorés faute de gpiozero sous Windows, 1 échec `AF_UNIX` pré-existant sous Windows) ; un test découplé du contenu de l'exemple (`ALTITUDE_M` au lieu de `WEB_PORT`) |
| `tests/test_web_api.py` | **nouveau** : 46 tests, serveur réel sur le port 0 |
| `.gitignore` | **modifié** : `state/`, `*.env.bak` |
| `PLAN.md` | **nouveau** (ce document) |

Dernier commit : `83a850c` (« Règles du projet : pas de push sans demande expresse »).
**Rien n'est commité ni poussé depuis** : les fichiers ci-dessus sont en attente.

### Sur la Pi (indicam-pi)

- la version de l'**étape 3 est déployée** : le test paho du 22/11 reçoit `heater_mode` et
  `heater_target` toutes les 10 s (seule cette version publie l'état chauffage hors cycle de mesure) ;
- `/etc/allsky-sensors/allsky-sensors.env` : `HEATER_ENABLED=1`, `MQTT_HOST=192.168.155.248`, utilisateur `allsky-pi` ;
- **pas encore déployé** : l'interface web (étapes 4 et 5 : `web_api.py` + `allsky-sensors-web.service`) ;
  il faut d'abord renseigner `WEB_PASSWORD` (12 caractères au moins), sinon l'unité refuse de démarrer, et
  donner le fichier `.env` au compte du service pour que le formulaire puisse l'écrire ;
- commandes de déploiement (si réinstallation nécessaire) :

```bash
sudo install -m 644 allsky_sensors_mqtt.py /opt/allsky-sensors/allsky_sensors_mqtt.py
sudo install -m 644 allsky-sensors.service /etc/systemd/system/allsky-sensors.service
sudo systemctl daemon-reload && sudo systemctl restart allsky-sensors
```

- `/opt/allsky-sensors/state` existe et appartient au compte du service (constaté le 22/11) ;
- le redémarrage annule toute consigne forcée (sécurité) ;
- constat matériel : `RPiGPIOFactory` (RPi.GPIO) est le backend gpiozero actif, la sortie PWM fonctionne
  (`--heater-test 20` sans erreur) ; l'avertissement `lgpio` est cosmétique.

### Incident du 22/11 22:25 et correctif ✅ résolu le 22/11 22:32

Symptôme : au redémarrage, `systemd[1]: allsky-sensors.service:9: EnvironmentFile= path is not absolute,
ignoring: ENV_FILE=/etc/allsky-sensors.env`, puis le service tourne **sans aucune configuration**
(`broker MQTT visé : localhost:1883 (anonyme)`, `chauffage désactivé`).

Cause : dans la copie installée de l'unité, la ligne `Environment=ENV_FILE=…` était **indentée** (ou
collée sur la ligne précédente). Pour systemd, une ligne commençant par un espace est la **suite** de la
précédente : il a lu `EnvironmentFile=/etc/allsky-sensors.env ENV_FILE=/etc/allsky-sensors.env` et rejeté
la directive entière. Le fichier du dépôt, lui, est correct (aucun espace ni tabulation en tête).

Correctif côté code (livré) : `service_env()` lit désormais directement `/etc/allsky-sensors/allsky-sensors.env` quand
`ENV_FILE` n'est pas fourni, et le fichier a priorité sur l'environnement du processus.

Résolution : unité réécrite sur la Pi (`sudo tee … <<'EOF'`, aucune indentation, `User=pi`) ;
au redémarrage du 22/11 22:32 le journal affiche de nouveau `chauffage : 6 x 47 ohm …`,
`broker MQTT visé : 192.168.155.248:1883 (utilisateur allsky-pi)`, `commandes acceptées sur
allsky/heater_command` et `abonnement au topic de commande accepté`.

Vérification (à refaire en cas de doute) :

```bash
sudo sed -n '6,14p' /etc/systemd/system/allsky-sensors.service | cat -A   # un espace/tab en tête se voit
systemctl show allsky-sensors -p EnvironmentFiles -p Environment
```

### Incident du 22/11 : « Requête refusée » à l'enregistrement des réglages ✅ corrigé

Symptôme : dans le navigateur, « Enregistrer » sur `/reglages` répondait **403 « Requête refusée —
En-tête Origin (ou Referer) absent ou d'une autre origine »**, alors que la page venait bien du serveur.

Cause (trouvée au journal du 22/11 23:49 : `Origin='null' Referer=None`) : le socle exigeait la présence
d'un en-tête `Origin` ou `Referer` **utilisable**. Or nous envoyions `Referrer-Policy: no-referrer` : la
spec Fetch demande alors au navigateur de sérialiser l'origine de la requête à `null` (`Origin: null`), en
plus de supprimer `Referer`. Notre propre politique de confidentialité neutralisait donc l'en-tête sur
lequel reposait le contrôle. Faux positif, entièrement de notre fait.

Correctif, en deux volets :

1. `Referrer-Policy: same-origin` : le navigateur annonce de nouveau `Origin` (et un `Referer` pour nos
   propres pages), et n'envoie toujours rien vers l'extérieur ;
2. protection réelle par **jeton de formulaire** (`_jeton`, HMAC-SHA256 du mot de passe web), en champ
   caché, vérifié en temps constant ; `Origin`/`Referer` restent contrôlés **quand ils sont annoncés**,
   mais leur absence **et** le cas `Origin: null` (origine masquée : politique de confidentialité, iframe
   en bac à sable) ne bloquent plus. La comparaison porte sur la machine (hostname) et sur le port
   seulement s'il est annoncé des deux côtés, ce qui tolère un reverse proxy qui retire le port du `Host`.

Un refus journalise `Origin`/`Referer`/`Host` (sans secret) : c'est ce message qui a permis de trancher.

Conséquence pratique : après mise à jour de `web_api.py`, **recharger la page** du formulaire (la page
ouverte avant le correctif n'a pas de jeton et serait refusée avec « Formulaire expiré ou d'une autre
page »). Tests : `test_post_sans_origine_avec_jeton_est_accepte`,
`test_post_sans_origine_sans_jeton_est_refuse`, `test_post_avec_un_jeton_invalide_refuse`,
`test_formulaire_contient_un_jeton`, `test_origine_compare_la_machine_pas_l_url_brute`,
`test_origine_masquee_avec_jeton_est_accepte`, `test_politique_de_referrer_compatible_avec_l_origine`.

### Incident du 22/11 23:53 : écriture des réglages impossible (`Read-only file system`) ✅ corrigé

Symptôme : `ERROR réglages non enregistrés dans /etc/allsky-sensors.env : [Errno 30] Read-only file
system: '/etc/allsky-sensors.env.bak'`.

Cause : deux obstacles cumulés, tous deux liés à l'écriture **atomique** d'un fichier isolé dans `/etc` :

1. `ReadWritePaths=` ne rend inscriptible que le **fichier** listé ; `.bak` et `.tmp` sont créés **à côté**
   et restaient en lecture seule (`ProtectSystem=strict`) ;
2. même sans sandbox, un service non root ne peut pas écrire dans `/etc` : créer `.bak`/`.tmp` **et
   renommer** le fichier exigent un droit d'écriture sur le **répertoire**, que `pi` n'a pas (et ne doit
   pas avoir).

Correctif : la configuration vit dans un dossier dédié, propriété du compte du service :

- `DEFAULT_ENV_FILE = /etc/allsky-sensors/allsky-sensors.env` (constante du programme) ;
- les deux unités lisent ce chemin (`EnvironmentFile=`, `Environment=ENV_FILE=`) ;
- l'unité web monte `/etc/allsky-sensors` en écriture (`ReadWritePaths=/opt/allsky-sensors/state
  /etc/allsky-sensors`) : le dossier, et non le fichier, sinon `.tmp` + renommage échouent ;
- `service_env()` journalise désormais « … introuvable : configuration de l'environnement uniquement »
  quand `ENV_FILE` désigne un fichier absent : le piège de l'incident systemd du 22/11 ne peut plus
  passer inaperçu.

Migration sur une installation existante (l'ancien fichier reste lisible par `--heater-force` tant qu'il
est là, mais les unités doivent pointer le nouveau chemin) :

```bash
sudo install -d -m 750 -o pi -g pi /etc/allsky-sensors
sudo install -m 600 -o pi -g pi /etc/allsky-sensors.env /etc/allsky-sensors/allsky-sensors.env
sudo rm /etc/allsky-sensors.env            # facultatif, une fois les deux unités à jour
sudo install -m 644 allsky-sensors.service /etc/systemd/system/allsky-sensors.service
sudo install -m 644 allsky-sensors-web.service /etc/systemd/system/allsky-sensors-web.service
sudo systemctl daemon-reload && sudo systemctl restart allsky-sensors allsky-sensors-web
systemctl show allsky-sensors allsky-sensors-web -p EnvironmentFiles -p Environment
journalctl -u allsky-sensors -n 5          # aucune ligne « introuvable »
```

Tests : aucun chemin absolu n'est codé en dur dans les tests (fichiers temporaires) ; suite verte.

**Suite du 23/11** : après la migration (`/etc/allsky-sensors/`, 750 `pi:pi`, unités à jour), l'erreur
`EROFS` **persistait** sur `.bak` — donc le bac à sable du service web n'avait toujours pas l'exception
d'écriture. Diagnostic, dans cet ordre :

```bash
systemctl show allsky-sensors-web -p ProtectSystem -p ReadWritePaths -p ReadOnlyPaths -p FragmentPath
sudo systemctl cat allsky-sensors-web --no-pager | cat -A        # une ligne indentée = avalée par systemd
sudo grep allsky /proc/$(systemctl show -p MainPID --value allsky-sensors-web)/mountinfo
```

- `ReadWritePaths=` vide ou limité à `/opt/allsky-sensors/state` : la ligne n'est pas chargée (unité
  ancienne, ligne collée à la précédente) → réinstaller l'unité, `daemon-reload`, `restart` ;
- `ReadWritePaths` correct mais aucune ligne `rw` pour `/etc/allsky-sensors` dans `mountinfo` : système
  de fichiers non remonté dans le bac à sable → repli `ProtectSystem=true` (le dossier appartient au
  compte du service, donc les droits Unix suffisent) ;
- ligne `rw` présente : l'erreur vient d'ailleurs, vérifier que le `MainPID` est bien le processus qui a
  servi la requête.

Le message affiché par la page distingue désormais les deux causes (`EROFS` → « ReadWritePaths »,
`EACCES` → « compte du service »), et `_write_error()` le journalise tel quel.

**Décision (23/11)** : sur la Pi, l'unité **s'édite sur place** ; le fichier du dépôt n'est que la
référence (et le modèle pour une installation neuve, où `User=` est substitué — voir README). Conséquence
assumée : quand une étape ajoute une permission (comme `ReadWritePaths` ici), c'est à l'unité **éditée sur
la Pi** qu'il faut l'ajouter. Deux garde-fous : le message d'erreur de `/reglages` nomme désormais la
directive à corriger, et cette liste dit ce que l'unité doit contenir :

```bash
systemctl show allsky-sensors-web -p Environment -p ProtectSystem -p ReadWritePaths -p FragmentPath
# Environment=ENV_FILE=/etc/allsky-sensors/allsky-sensors.env
# ProtectSystem=strict
# ReadWritePaths=/opt/allsky-sensors/state /etc/allsky-sensors
```

C'est la double maintenance (copie du dépôt + copie éditée sur la Pi) qui a réinstallé une révision en
retard et fait revenir le `EROFS` : d'où la règle, maintenant explicite, qu'aucune installation d'unité
ne doit être faite depuis une copie locale non relue.

Les commandes **datées** des étapes 1 à 5 et les journaux cités plus haut montrent l'ancien chemin
`/etc/allsky-sensors.env` : c'est voulu (ils racontent l'historique). Les blocs opérationnels, eux, sont à
jour du nouveau chemin.

### Validations

1. `--heater-force 20` → `chauffage 94 % (forcé)` : **acquis** (journal du 22/11 22:03).
2. **Commande MQTT reçue de bout en bout : acquise le 22/11** (test paho, outil ci-dessous) — écho du
   broker, `override.json` à `20 °C / 5 min`, `heater_mode force` + `heater_target 20.00` toutes les
   10 s, `dome_heater_pwm 94 %`, puis `cancel` → `auto`, cible revenue au point de rosée + marge et
   fichier supprimé. Service, topic, préfixe et droits d'écriture du compte `allsky-pi` hors de cause.
3. Le dôme **monte-t-il** à 94 % en extérieur ? Observation du 22/11 : `11,12 → 11,19 °C` en 65 s
   (offset `DS18B20_OFFSET_C=3` inclus). Trop court pour conclure : à mesurer sur 60 min, chauffage
   coupé puis à 94 %, et comparer. Voir le dimensionnement ci-dessous.
4. **Rechargement à chaud (étape 3) : acquise le 22/11 23:07.** `status.json` était présent et frais,
   puis `HEATER_MARGIN=4` → `6` a produit `configuration rechargée depuis /etc/allsky-sensors/allsky-sensors.env` et,
   au tick suivant, une cible de 9,3 °C (rosée 3,3 + 6) sans redémarrage. Marge remise à `4`.
5. **MQTT Explorer n'a jamais fait arriver sa commande** : seul point encore ouvert, côté client
   (le service, lui, est prouvé bon). Voir la section suivante.

### MQTT Explorer : résolu le 22/11 (espace final dans le topic)

Cause : le champ topic de MQTT Explorer contenait `allsky/heater_command ` — **un espace à la fin**. Le
topic publié différait donc de celui qu'écoute le service, et le message partait dans le vide sans la
moindre erreur visible. Compte, broker, préfixe et droits étaient bons depuis le début : c'est le test
paho (écho du broker + `override.json`) qui a permis de le prouver, puis de chercher ailleurs.

Leçon : un topic MQTT est une chaîne exacte (espaces, `/` initial ou final, majuscules et préfixe
comptent). Les deux commandes qui ferment le sujet : `mosquitto_sub -h localhost -u allsky-pi -P '…' -t
'allsky/#' -v` sur le broker, et le script paho du §9 qui montre ce que le service reçoit.

Si cela recommence, vérifications utiles :

Vérifications à faire sur le **broker** (192.168.155.248), pas sur la Pi :

```bash
# 1) voir ce qui arrive sur le préfixe quand on publie depuis MQTT Explorer
mosquitto_sub -h localhost -u allsky-pi -P 'MOT_DE_PASSE' -t 'allsky/#' -v
# 2) si rien n'apparaît : trouver le topic réellement employé (tous les topics, 20 s)
timeout 20 mosquitto_sub -h localhost -u allsky-pi -P 'MOT_DE_PASSE' -t '#' -v
# 3) droits d'écriture refusés ?
sudo journalctl -u mosquitto --since "-5 min" --no-pager | grep -iE 'denied|not authori'
sudo grep -nE '^(listener|allow_anonymous|password_file|acl_file)' /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/*.conf
```

Avec un `acl_file`, il faut `topic read allsky/#` **et** `topic write allsky/heater_command` pour
l'utilisateur employé par MQTT Explorer (souvent un autre compte que `allsky-pi`).

### Outil de diagnostic MQTT (validé le 22/11)

À relancer sur la Pi dès qu'un doute revient sur la chaîne MQTT. Contrairement à la version d'origine de
l'étape 1, il lit lui-même `/etc/allsky-sensors/allsky-sensors.env` (broker, identifiants, préfixe du service), s'abonne
à `<préfixe>/#`, publie `20 5`, **vérifie l'écho de sa propre publication** (preuve que le broker
accepte l'écriture), lit `override.json` (preuve que le service a traité la commande, sans attendre un
cycle), attend une publication complète (`INTERVAL + 5 s`) puis publie `cancel` — même en cas d'erreur.
Durée ≈ 80 s ; chauffage forcé 5 min au plus.

```bash
sudo /opt/allsky-sensors/venv/bin/python - <<'PY'
"""Diagnostic paho : le service recoit-il les commandes sur <prefixe>/heater_command ?

Lit /etc/allsky-sensors/allsky-sensors.env (meme broker, memes identifiants et meme prefixe que le service),
s'abonne a <prefixe>/#, publie « 20 5 » sur le topic de commande, verifie l'echo du broker,
le fichier de consigne, puis publie « cancel » pour ne rien laisser derriere.
"""
import pathlib
import sys
import time

import paho.mqtt.client as mqtt

ENV_FILE = pathlib.Path("/etc/allsky-sensors/allsky-sensors.env")
DEFAULT_STATE = pathlib.Path("/opt/allsky-sensors/state")

env = {}
for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()

prefix = env.get("MQTT_PREFIX", "allsky") or "allsky"
host = env.get("MQTT_HOST", "localhost")
port = int(env.get("MQTT_PORT", "1883"))
interval = int(env.get("INTERVAL", "60") or 60)
state = pathlib.Path(env.get("STATE_DIR") or DEFAULT_STATE)
override = state / "override.json"
topic = f"{prefix}/heater_command"

if len(sys.argv) >= 3:  # variante : identifiants d'un autre client (celui de MQTT Explorer)
    user, password = sys.argv[1], sys.argv[2]
else:
    user, password = env.get("MQTT_USER", ""), env.get("MQTT_PASSWORD", "")

print(f"broker {host}:{port} | utilisateur {user or 'anonyme'} | prefixe {prefix}")
print(f"commande {topic} | consigne {override} | intervalle {interval} s")
print(f"HEATER_ENABLED (lu dans le fichier) : {env.get('HEATER_ENABLED', '0')}")

received: dict[str, str] = {}


def on_connect(client, userdata, flags, reason_code, properties=None):
    failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
    if failed:
        print("CONNEXION REFUSEE :", reason_code)
        return
    result, _ = client.subscribe(f"{prefix}/#", qos=1)
    print("connecte, abonnement a", f"{prefix}/#", "->", result)


def on_message(client, userdata, message):
    text = message.payload.decode("utf-8", "replace")
    received[message.topic] = text
    print("  recu", message.topic, "=", text)


def show_override(label):
    if override.exists():
        print(label, override.read_text(encoding="utf-8").strip())
    else:
        print(label, "ABSENT")


try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="allsky-diagnostic")
except AttributeError:  # paho-mqtt 1.x
    client = mqtt.Client(client_id="allsky-diagnostic")
if user:
    client.username_pw_set(user, password)
client.on_connect = on_connect
client.on_message = on_message

try:
    client.connect(host, port, 60)
except OSError as error:
    print("CONNEXION IMPOSSIBLE :", error)
    raise SystemExit(1) from None
client.loop_start()
time.sleep(3)  # laisse l'abonnement s'etablir

try:
    info = client.publish(topic, "20 5", qos=1, retain=False)
    time.sleep(2)
    print("publie « 20 5 » sur", topic, "| rc =", info.rc)
    time.sleep(3)
    show_override("override.json apres la commande :")
    print(f"attente de {interval + 5} s : une publication complete de heater_mode doit arriver...")
    time.sleep(interval + 5)
finally:
    try:
        client.publish(topic, "cancel", qos=1, retain=False)
        time.sleep(4)
        show_override("override.json apres cancel :")
    except Exception as error:  # noqa: BLE001 - le menage ne doit jamais masquer le diagnostic
        print("cancel non publie :", error)
    try:
        client.loop_stop()
        client.disconnect()
    except Exception as error:  # noqa: BLE001
        print("arret du client incomplet :", error)

print()
print("--- bilan ---")
print("echo de ma commande par le broker :", "OUI" if topic in received else "NON")
print("heater_mode :", received.get(f"{prefix}/heater_mode", "non recu"))
print("heater_target :", received.get(f"{prefix}/heater_target", "non recu"))
mesures = sorted(t for t in received if t not in (topic, f"{prefix}/heater_mode", f"{prefix}/heater_target"))
print("mesures recues :", ", ".join(mesures) or "aucune")
PY
```

Lecture du bilan :

| Bilan | Conclusion |
|---|---|
| `echo … : OUI` + `override.json` + `heater_mode force` | chaîne paho bonne : le problème est dans MQTT Explorer (topic, payload, retain, compte) |
| `echo … : OUI` mais `override.json ABSENT` | le broker accepte, le service n'agit pas : journal (`chauffage désactivé`, `commande refusée`) |
| `echo … : NON` | droits d'**écriture** refusés, ou broker qui ne rediffuse pas : ACL côté broker |
| aucune mesure non plus | mauvais broker/port (exclu ici : les mesures arrivent) |

### Dimensionnement du chauffage (à trancher)

Le plafond de 3 W vient du **dérating** (0,5 W par résistance) et du nombre de résistances, **pas** de
la tension : `max_duty = 0,5 × 1 W / (V²/R)`, donc `3 W` au total avec 6 résistances de 1 W, que
l'alimentation soit en 5 V ou en 12 V. Pour chauffer davantage, il faut des résistances de puissance
nominale plus élevée (2 W → 6 W, 3 W → 9 W) ou davantage de résistances, pas une tension plus haute.

Ordre de grandeur pour un dôme d'allsky : pertes radiatives vers un ciel clair ≈ 3 à 6 W pour ~0,08 m²,
plus la convection (≈ 0,4 W par °C d'écart au vent calme, davantage avec du vent). 3 W permettent donc
au mieux **+1 à +3 °C** en air calme, et souvent moins : à mesurer (94 % pendant 60 min, comparer
`dome_temperature` à chauffage coupé). Si la montée est insuffisante pour tenir le dôme au-dessus du
point de rosée, passer à des résistances de 2 W (6 W utiles) avant de toucher au régulateur.

Première mesure (22/11, test paho à 94 % pendant 65 s) : `11,12 → 11,19 °C`, soit **+0,07 °C en une
minute** (offset DS18B20 inclus). Trop court pour conclure, mais cohérent avec une montée lente : refaire
l'essai sur 60 min, puis comparer à chauffage coupé.

Contrainte matérielle (23/11) : les résistances de **2 W ne tiennent pas sous le dôme** (essayé, abandonné).
Piste réaliste : **8 × 1 W** → 4,0 W au plafond, 0,80 A, même fusible de 1 A (marge juste) ; les deux
unités supplémentaires se dispersent mieux autour du dôme que des 2 W serrées. Au-delà de 8, le courant
dépasse le fusible (10 → 1,0 A) : à remplacer avant d'y penser. En attendant, le levier gratuit reste
l'anticipation (voir « Température du ciel par MQTT »).

### Prochaine étape

1. **Déployer l'interface web sur la Pi** (étapes 4 et 5 livrées) : créer `/etc/allsky-sensors` (mode 750,
   propriétaire du compte du service) et y placer le fichier d'environnement (600), renseigner
   `WEB_PASSWORD`, puis les `install`/`systemctl` de l'étape 4. Essayer `/` puis `/reglages` : modifier
   `HEATER_MARGIN`, enregistrer, et vérifier au tick suivant que la cible suit, sans redémarrage du
   service.
2. **Étape 6 — Web : commandes de forçage** : `POST /forcer` (`target_c`, `minutes`, bornés par
   `HEATER_MAX_DOME_TEMP`, 5 min à 24 h), `POST /forcer/annuler`, refus explicite si `HEATER_ENABLED=0`,
   affichage du mode, de la consigne et du temps restant ; écriture via `write_override`, application au
   tick suivant (10 s).
3. Facultatif : rien à faire côté MQTT Explorer, la cause est connue (espace final dans le topic).

Pour reprendre : lire les sections 4 (avancement) et 5 (détail des étapes), puis cette section 9.

### Température du ciel par MQTT (idée retenue, après l'étape 6)

Le MLX90614 **n'est pas câblé sur la Pi** : il sert à un projet SQMeter, ailleurs. Plutôt que d'en acheter
un second, le service peut lire sa mesure sur le broker — c'est le même Mosquitto (192.168.155.248), et
l'abonnement existe déjà pour `heater_command`.

Contrat proposé (rien n'est codé à ce jour) :

| Variable | Défaut | Rôle |
|---|---|---|
| `SKY_TEMP_TOPIC` | (vide : désactivé) | topic d'où lire la température du ciel (ex. `sqmeter/sky_temperature`), QoS 1 |
| `SKY_TEMP_MAX_AGE` | `600` | au-delà de cet âge (s), la valeur reçue est ignorée |

- abonnement refait à **chaque** connexion (comme `heater_command`), valeur bornée (−80 à +150 °C), horodatée ;
- usage 1 (simple) : publier `sky_temperature` quand `mlx90614` n'est pas dans `ENABLED` — un topic de
  mesure de plus, sans matériel ;
- usage 2 (le vrai gain) : **anticipation**. Quand `ciel − air` décroche (ciel clair = le dôme rayonne),
  relever la marge ou avancer le chauffage, au lieu d'attendre que le dôme atteigne la cible. À 3 W,
  anticiper vaut mieux que chauffer plus fort ;
- garde-fous : une valeur externe ne doit **jamais** contourner `max_duty`, la coupure à
  `HEATER_MAX_DOME_TEMP`, ni la coupure sur mesure périmée ; topic vide = fonctionnalité absente, zéro
  surprise ; si le topic est retenu (retain), la valeur est disponible dès la connexion.

À vérifier avant de coder : ce que le SQMeter publie réellement. S'il publie une **magnitude**
(mag/arcsec²) et pas une température, on peut quand même s'en servir comme indicateur « ciel clair » —
c'est même plus direct — avec un seuil plutôt qu'un écart de températures.

### Clôture de la session du 23/11

**Acquis en production (Pi `indicam-pi`)** : étapes 1 à 5 déployées et vérifiées — forçage par fichier,
commande MQTT reçue de bout en bout, rechargement à chaud, `status.json`, interface web `/` et `/sante`,
formulaire `/reglages` qui écrit `/etc/allsky-sensors/allsky-sensors.env` (jeton anti-CSRF, `303`,
`.bak` en 600). Configuration dans `/etc/allsky-sensors/` (750 `pi:pi`), l'unité web y écrit.

**Décisions à conserver** :

- dérating 50 % par résistance, plafond logiciel appliqué à *toutes* les commandes (`DERATING`, pas de
  variable d'environnement) : ne pas le contourner ; pour plus de chaleur, plus de résistances (8 × 1 W),
  jamais un dérating relevé ;
- `HEATER_SUPPLY_VOLTS` déclarée doit être la tension **réelle** : c'est la seule sécurité que le logiciel
  ne peut pas vérifier ;
- unité systemd **éditée sur place** sur la Pi ; le fichier du dépôt est la référence ; toute nouvelle
  permission d'écriture va dans l'unité de la Pi (le message de `/reglages` la nomme) ;
- `Referrer-Policy: same-origin` et jeton `_jeton` : les deux sont liés (voir incidents du 22/11).

**État du dépôt** : modifications non commitées (`.gitignore`, `allsky-sensors.env.example`,
`allsky-sensors.service`, `allsky_sensors_mqtt.py`, `README.md`, `tests/test_allsky_sensors.py`) et
fichiers nouveaux (`web_api.py`, `allsky-sensors-web.service`, `tests/test_web_api.py`, `PLAN.md`).
Aucun commit, aucun push. `pytest` : 203 passés, 2 ignorés, 1 échec `AF_UNIX` pré-existant sous Windows ;
`ruff` : OK.

**Prochaine session** : étape 6 — forçage du chauffage depuis le web (`POST /forcer`,
`POST /forcer/annuler`, jeton `_jeton`, écriture `override.json`, refus si `HEATER_ENABLED=0`, compte à
rebours affiché). Puis l'étape 7 (déploiement/documentation) et, si le besoin se confirme, la température
du ciel par MQTT ci-dessus.


