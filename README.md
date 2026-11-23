# Capteurs et chauffage anti-buée de l'allsky, via MQTT

Programme pour la Raspberry Pi de l'allsky (testé sans matériel : simulation, vrai broker Mosquitto, 57 tests). Il :
1. lit un BME280 (extérieur), un DS18B20 (dôme) et un MLX90614 (température du ciel), calcule le point de rosée ;
2. régule un chauffage anti-buée : température du dôme = point de rosée + marge ;
3. publie tout sur MQTT, y compris le rapport cyclique PWM appliqué.

```
capteurs + chauffage → Raspberry Pi (ce programme) → broker Mosquitto → indi-allsky → latest_metadata.json → site web
```

| Topic | Source | Contenu |
|---|---|---|
| `allsky/outdoor_temperature` | BME280 | température extérieure (°C) |
| `allsky/outdoor_humidity` | BME280 | humidité extérieure (%) |
| `allsky/outdoor_pressure` | BME280 | pression (hPa), ramenée au niveau de la mer si `ALTITUDE_M` est défini |
| `allsky/outdoor_pressure_station` | BME280 | pression mesurée (hPa), publiée seulement si `ALTITUDE_M` est défini |
| `allsky/outdoor_dew_point` | calculé | point de rosée (°C), formule de Magnus |
| `allsky/dome_temperature` | DS18B20 | température dans le dôme (°C) |
| `allsky/sky_temperature` | MLX90614 | température du ciel (°C), si `mlx90614` est dans `ENABLED` |
| `allsky/dome_heater_pwm` | chauffage | rapport cyclique appliqué (%), seulement si le chauffage est activé |

`--print-topics` ne liste que les capteurs activés dans `ENABLED` (plus la pression de station si `ALTITUDE_M` est défini, plus `dome_heater_pwm` si le chauffage est activé) : c'est cette liste, et rien d'autre, qu'il faut coller dans indi-allsky.

## Pression : niveau de la mer ou pression de station

Un baromètre mesure la pression **à l'altitude où il se trouve** (la « pression de station »). Les services météo, eux, publient toujours la pression **ramenée au niveau de la mer**, pour que les valeurs soient comparables d'un lieu à l'autre : c'est l'usage, et ce que tes visiteurs attendent. La pression de station sert surtout aux calculs de réfraction atmosphérique.

Renseigne `ALTITUDE_M` (altitude du capteur) : `outdoor_pressure` devient alors la pression au niveau de la mer, et la pression mesurée est publiée en plus dans `outdoor_pressure_station`. Sans altitude, seule la pression mesurée est publiée.
Quelques repères : 1 m d'altitude vaut environ 0,12 hPa, et un BME280 est précis à environ 1 hPa. Pour caler l'altitude, compare avec la pression au niveau de la mer d'une station météo voisine et ajuste `ALTITUDE_M` jusqu'à obtenir la même valeur.

## Câblage des capteurs

Le détail broche par broche, le brochage en image, les schémas, la checklist avant mise sous tension et les erreurs fréquentes sont dans **[CABLAGE.md](CABLAGE.md)**. Voici l'essentiel :

- **BME280 (I2C)** : 3,3 V, GND, SDA broche 3 (GPIO 2), SCL broche 5 (GPIO 3). Beaucoup de modules vendus comme BME280 sont des BMP280, sans humidité. Au-delà d'un à deux mètres de câble, l'I2C devient capricieux. Abri ventilé, à l'ombre.
- **DS18B20 en boîtier TO-92** : face plate vers toi, pattes vers le bas, de gauche à droite : **GND, DQ (données), VDD**. DQ va sur la broche 7 (GPIO 4), VDD sur 3,3 V, avec **4,7 kΩ entre DQ et 3,3 V**. **Une inversion d'alimentation le fait chauffer et le détruit** : vérifie deux fois. Pour mesurer ce qui compte (la surface qui condense), fixe-le contre la paroi intérieure du dôme (ruban adhésif ou colle thermique) plutôt que dans l'air.
- **MLX90614 (I2C)** : 3,3 V, GND, SDA broche 3 (GPIO 2), SCL broche 5 (GPIO 3) — le même bus que le BME280 — adresse **0x5A**. Ne monte pas le bus à 400 kHz : ce capteur ne fonctionne qu'à 100 kHz, la fréquence par défaut. Deux pièges à connaître : **plexiglas et verre sont opaques à l'infrarouge lointain** (8-14 µm), donc un MLX90614 placé sous le dôme mesure la paroi du dôme, pas le ciel — il doit voir le ciel directement, par une ouverture ou derrière une fenêtre transparente à l'IR (film polyéthylène ou polypropylène très fin) ; et **tout ce qui le réchauffe fausse la mesure** (soleil, boîtier fermé, électronique voisine), puisqu'il se sert de sa propre température comme référence. À l'abri de la pluie : une visée mouillée ne mesure plus rien.

`sudo raspi-config` → Interface Options : active **I2C** et **1-Wire**, puis redémarre. Vérifications : `i2cdetect -y 1` (76 ou 77 pour le BME280, 5a pour le MLX90614) et `ls /sys/bus/w1/devices/` (`28-…`).

## Chauffage : dimensionnement et câblage (alimentation 5 V)

Résistances de 47 Ω et 1 W, alimentées en 5 V :

| En parallèle | Résistance totale | Courant à 100 % | Puissance à 100 % | Rapport cyclique max retenu | Puissance max retenue |
|---|---|---|---|---|---|
| 6 | 7,83 Ω | 0,64 A | 3,2 W | 94 % | 3,0 W |
| 5 | 9,4 Ω | 0,53 A | 2,7 W | 94 % | 2,5 W |

Chaque résistance dissipe 0,53 W à pleine puissance, un peu plus de la moitié de son nominal : le programme la limite à 0,5 W (94 %). Même un MOSFET bloqué passant ne détruirait pas les résistances. Pour plus de puissance, ajoute des résistances (+0,5 W chacune) ; le journal montre si le chauffage tourne à fond sans atteindre la cible.

**Câblage**
- GPIO 23 (broche 16) → entrée de commande du module MOSFET ; GND de la Pi relié au GND de l'alimentation du chauffage ; résistances en parallèle sur la sortie du module. Vérifie sur sa fiche qu'il accepte un signal de 3,3 V et une PWM de 100 Hz, et dans quel sens se branche la charge.
- **Alimentation par la prise USB 5 V / 4 A du HAT PoE** (choix retenu) : c'est une sortie prévue pour alimenter des périphériques, donc une bonne source, à condition de ne pas passer par les broches 5 V du GPIO ni par l'entrée micro-USB de la Pi. La masse est commune avec la Pi d'office. Raccorde le chauffage avec un connecteur USB-A à bornes, ou un câble USB sacrifié : fil rouge = 5 V, noir = GND, et les deux fils de données coupés et isolés.
- **Fusible de 1 A tout près de la prise USB** : la source peut fournir jusqu'à 4 A, et les fils fins d'un câble USB chaufferaient bien avant que la protection du HAT n'intervienne en cas de court-circuit.
- **Budget PoE** (HAT de type Waveshare PoE HAT (C), 802.3af/at) : sortie USB-A 5 V / 4 A au maximum, header 12 V / 2 A, **25 W au total** pour le 5 V et le 12 V, et un convertisseur MP8676 de 4,5 A au plus pour la Pi **et** la prise USB-A ensemble. Ordre de grandeur de ta consommation : Pi 3 B+ de 3 à 5 W, caméra USB jusqu'à 2,5 W, chauffage 3,2 W, soit 9 à 11 W en 5 V (environ 2 à 3 A, donc sous les 4,5 A).
- **Switch TP-Link TL-SG1005P** : 4 ports PoE+ (802.3af/at), jusqu'à 30 W par port, budget total annoncé de 65 W pour les 4 ports, ce que TP-Link dit non garanti (certains revendeurs indiquent 56 W). C'est largement suffisant pour la Pi, qui demandera environ 12 à 14 W au port, pertes du HAT comprises ; compte le reste du budget pour tes autres appareils PoE. **Branche la Pi sur l'un des ports 1 à 4 : le 5e n'a pas de PoE.** Avec un simple port 802.3af (environ 13 W reçus), ce serait en revanche juste dans le pire cas.
- **GPIO** : le HAT reprend le connecteur de 40 broches (stackable) : tu peux donc y brancher tes câbles (I2C, 1-Wire, GPIO 23) par le dessus. Son ventilateur est présenté comme intégré, sans mention d'un GPIO de commande dans les fiches que j'ai trouvées ; vérifie quand même le manuel du fabricant. Ton focuser occupe GPIO 17, 18, 22 et 27 : le chauffage utilise donc **GPIO 23** (pas de PWM matériel sur cette broche, sans importance : la PWM est logicielle, à 100 Hz). Si tu changes `HEATER_GPIO`, évite aussi 2, 3 et 4 (I2C, 1-Wire) ; `GPIO_RESERVED` permet au programme de refuser les broches déjà prises. Le header 12 V du HAT n'est pas utilisé ici : le programme est réglé pour du 5 V.
- Après un essai de chauffage, `vcgencmd get_throttled` doit répondre `throttled=0x0` : sinon, la Pi a vu une sous-tension (ajoute alors un condensateur de 470 à 1000 µF près du module).
- Une résistance de **10 kΩ entre le signal et GND**, si le module n'en a pas : le MOSFET reste bloqué quand la Pi démarre ou plante.
- Un MOSFET qui claque en court-circuit laisserait environ 3 W en continu : résistances intactes, dôme réchauffé de quelques degrés (à vérifier chez toi). Un thermostat ou fusible thermique à 45-50 °C en série reste une bonne précaution, mais il est facultatif à 5 V.
- Résistances solidement fixées, loin des câbles et du plastique fragile.

Avec une autre tension, le programme recalcule le plafond (`HEATER_SUPPLY_VOLTS`) : sous 12 V, chaque résistance dissiperait 3,06 W à 100 %, trois fois son nominal, et le rapport cyclique serait plafonné à 16 %. Il **refuse de démarrer le chauffage si la tension n'est pas renseignée**.

## Installation sur la Pi 3 B+

**Avant tout, active l'I2C et le 1-Wire** : `sudo raspi-config`, Interface Options, I2C puis 1-Wire, et redémarre. Sans cela, le BME280 et le DS18B20 restent invisibles.

```
sudo apt install i2c-tools python3-venv
sudo mkdir -p /opt/allsky-sensors && sudo chown $USER /opt/allsky-sensors
cp allsky_sensors_mqtt.py requirements.txt /opt/allsky-sensors/
python3 -m venv /opt/allsky-sensors/venv
/opt/allsky-sensors/venv/bin/pip install -r /opt/allsky-sensors/requirements.txt
sudo usermod -aG i2c,gpio $USER      # puis se reconnecter
```

gpiozero peut afficher un avertissement de repli de bibliothèque GPIO : sans importance.

## Essais, dans l'ordre

```
cd /opt/allsky-sensors
./venv/bin/python allsky_sensors_mqtt.py --simulate --dry-run --once      # sans matériel ni broker
./venv/bin/python allsky_sensors_mqtt.py --dry-run --once                 # vrais capteurs, sans broker
./venv/bin/python allsky_sensors_mqtt.py --print-topics                   # liste à coller dans indi-allsky
```

**Si le DS18B20 n'apparaît pas** (« capteur ds18b20 indisponible ») :
- le message dit « 1-Wire non activé » : active-le dans `raspi-config` (ou ajoute `dtoverlay=w1-gpio` dans `/boot/firmware/config.txt`, ou `/boot/config.txt` sur un ancien système), puis redémarre ;
- il dit « aucun DS18B20 détecté » : `ls /sys/bus/w1/devices/` ne doit pas contenir que `w1_bus_master1`. Vérifie DQ sur la broche 7 (GPIO 4), la résistance de 4,7 kΩ entre DQ et 3,3 V, l'alimentation, et le sens du TO-92 (face plate vers toi : GND, DQ, VDD). **Si le capteur chauffe, débranche-le tout de suite** : il est à l'envers ;
- `dmesg | grep -i w1` et `lsmod | grep w1` montrent si le noyau a chargé `w1_gpio` et `w1_therm`.

**Si le MLX90614 n'apparaît pas** (« capteur mlx90614 indisponible ») :
- `i2cdetect -y 1` doit afficher `5a` : sinon, vérifie l'alimentation (3,3 V, pas 5 V sur un module « 3 V » sans régulateur), les soudures et la longueur des câbles (au-delà d'un mètre, l'I2C devient capricieux) ;
- il est détecté mais aucune valeur n'arrive : les mesures aberrantes sont ignorées (hors de -80 à +150 °C, dont le -273,15 °C d'un capteur non alimenté) — regarde le journal, et vérifie que `MLX90614_OFFSET_C` est bien entre -20 et +20 °C.

`--dry-run` et `--simulate` ne touchent **jamais** aux GPIO. Pour le chauffage, avec `HEATER_ENABLED=1` et `HEATER_SUPPLY_VOLTS` dans l'environnement :

1. **À vide**, charge débranchée : `--heater-test 50` pendant 10 s ; vérifie au multimètre ou avec une LED que la sortie du module commute.
2. **Charge branchée**, `--heater-test 20` : les résistances doivent tiédir, pas chauffer. Mesure le courant si tu peux. L'essai est toujours plafonné au rapport cyclique maximal, quoi que tu demandes.
3. Puis le service, et surveille `journalctl -u allsky-sensors -f` : chaque minute, une ligne « dôme … rosée … cible … chauffage … % ».

## Sécurités logicielles

- plafond de puissance calculé selon la tension (tableau ci-dessus), refus de démarrer sans tension ;
- coupure immédiate si une mesure indispensable (dôme ou point de rosée) manque depuis plus de 2 minutes ;
- coupure si le dôme atteint `HEATER_MAX_DOME_TEMP` (35 °C par défaut) ;
- coupure à l'arrêt du programme ; la PWM est logicielle : si le programme est tué, le signal s'arrête avec lui ;
- chien de garde systemd : si le programme se fige, il est tué (PWM coupée) puis relancé ;
- mesures du MLX90614 manifestement fausses ignorées (hors de -80 à +150 °C) ;
- GPIO 2, 3 et 4 refusés (I2C et 1-Wire).

## Réglages du chauffage (`/etc/allsky-sensors/allsky-sensors.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `HEATER_ENABLED` | 0 | 1 pour activer |
| `HEATER_SUPPLY_VOLTS` | (obligatoire) | tension du chauffage |
| `HEATER_RESISTORS` / `_OHMS` / `_RATING_WATTS` | 6 / 47 / 1 | tes résistances |
| `HEATER_MARGIN` | 4 | °C au-dessus du point de rosée (3 à 5 selon ton goût) |
| `HEATER_MAX_DOME_TEMP` | 35 | coupure en température |
| `HEATER_GPIO` / `HEATER_PWM_HZ` | 23 / 100 | sortie et fréquence |
| `GPIO_RESERVED` | (vide ; exemple : `17,18,22,27`) | GPIO déjà pris par d'autres matériels (focuser) : le programme refuse de les utiliser |
| `HEATER_KP` / `HEATER_KI` | 0,3 / 0,0002 | régulateur PI |

Les gains par défaut sont stables sur un modèle de dôme (deux inerties, retard de 60 s, chauffages faibles ou forts), sans dépassement ni oscillation. **C'est un modèle, pas ton dôme** : si la température oscille, baisse `HEATER_KP` ; si elle est trop lente à rattraper, monte `HEATER_KI` un peu.

## Le broker (sur le Linux d'indi-allsky)

Le dépôt d'indi-allsky contient un script de déploiement de Mosquitto (`./misc/setup_mosquitto.sh`). Sinon : `sudo apt install mosquitto mosquitto-clients`.
Mosquitto 2 n'écoute par défaut que sur la machine locale : pour que la Pi s'y connecte, ajoute dans `/etc/mosquitto/conf.d/allsky.conf` :

```
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
```

```
sudo mosquitto_passwd -c /etc/mosquitto/passwd allsky-pi    # sans -c si le fichier existe déjà
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd        # sans cela, Mosquitto refuse de démarrer
sudo systemctl restart mosquitto
```

Réseau local uniquement : n'expose jamais ce port à Internet. Pour voir les mesures : `mosquitto_sub -h localhost -u allsky-pi -P MOT_DE_PASSE -t 'allsky/#' -v`

## Lancement automatique sur la Pi

```
sudo install -d -m 750 -o $USER -g $USER /etc/allsky-sensors      # dossier inscriptible par le service web
sudo install -m 600 -o $USER -g $USER allsky-sensors.env.example /etc/allsky-sensors/allsky-sensors.env
sudoedit /etc/allsky-sensors/allsky-sensors.env                   # broker, identifiants, chauffage, mot de passe web

# unités : le compte du service est substitué automatiquement, rien à éditer à la main
sudo install -m 644 allsky-sensors.service allsky-sensors-web.service /etc/systemd/system/
sudo sed -i "s/^User=.*/User=$(id -un)/" /etc/systemd/system/allsky-sensors.service /etc/systemd/system/allsky-sensors-web.service
sudo systemctl daemon-reload && sudo systemctl enable --now allsky-sensors allsky-sensors-web
```

Si tu préfères éditer l'unité sur place (`/etc/systemd/system/allsky-sensors-web.service`), garde-la
identique à celle du dépôt : seul `User=` doit différer. Les directives attendues sont rappelées dans
`PLAN.md` (« Décision du 23/11 »), et un refus d'écriture de `/reglages` nomme désormais celle qui manque.

## Réglage d'indi-allsky

Ajoute un capteur de type **MQTT Broker**. Dans le champ « Pin », colle la liste donnée par `--print-topics` (10 topics au plus, séparés par des virgules, chaque valeur étant un nombre). Cette liste suit `ENABLED` : recolle-la après chaque changement de capteur. Les noms viennent du dernier élément du topic, d'où les noms distincts.
Je n'ai pas pu vérifier dans la documentation où se règlent l'adresse et les identifiants du broker pour ce capteur : ce sont en principe les réglages MQTT d'indi-allsky.
Regarde ensuite dans `latest_metadata.json` quels `sensor_user_N` ont reçu les valeurs (le numéro d'emplacement n'est pas précisé par la doc).

## Côté site web

Dans `app/metadata.py` du site, une ligne par mesure à afficher, avec les numéros trouvés ci-dessus :

```python
SENSORS = {
    "sensor_user_10": ("Température extérieure", "°C"),
    "sensor_user_11": ("Humidité extérieure", "%"),
    "sensor_user_12": ("Pression (niveau de la mer)", "hPa"),
    "sensor_user_13": ("Point de rosée", "°C"),
    "sensor_user_14": ("Température du dôme", "°C"),
    "sensor_user_15": ("Chauffage du dôme", "%"),
    "sensor_user_16": ("Température du ciel", "°C"),
}
```

## Température du ciel (MLX90614)

Un ciel dégagé rayonne très peu en infrarouge : sa « température » est souvent 20 à 40 °C sous celle de l'air, alors qu'un ciel couvert se rapproche de la température de l'air (quelques degrés d'écart). C'est sur quoi reposent les détecteurs de nuages, et c'est aussi ce que subit le dôme : tourné vers le ciel, il se refroidit par rayonnement, descend sous la température de l'air et se couvre de buée (puis de rosée) avant tout le reste du matériel.

Le MLX90614 mesure cette température de rayonnement, sans contact. Pour l'activer, ajoute-le à `ENABLED` :

```
ENABLED=bme280,ds18b20,mlx90614
```

| Variable | Défaut | Rôle |
|---|---|---|
| `MLX90614_ADDRESS` | 0x5A | adresse I2C du capteur |
| `MLX90614_OFFSET_C` | 0 | correction (°C, de -20 à +20) si le capteur lit faux |

Repères pour vérifier le montage :

- vise un mur à l'ombre (ou pose une plaque devant l'ouverture) : la mesure doit tomber à 1 ou 2 °C de `outdoor_temperature` ; sinon, corrige avec `MLX90614_OFFSET_C` ;
- la nuit, sous un ciel dégagé, compte 15 à 30 °C de moins que l'air ; sous un ciel bouché, presque autant que l'air ;
- une valeur hors de -80 à +150 °C est traitée comme une erreur de lecture et n'est pas publiée : le topic garde sa dernière valeur, puis n'est plus mis à jour au bout de 10 minutes ;
- le programme ne touche pas au facteur d'émission du capteur (réglé dans son EEPROM) et ne publie pas sa mesure « ambiante » interne, qui ne vaut pas un vrai thermomètre d'air.

Cette mesure ne commande encore rien : la régulation du chauffage continue de comparer la température du dôme au point de rosée de l'air. Elle sert pour l'instant à l'affichage, et à repérer un ciel clair ou couvert.

## Développement

`pip install pytest ruff` puis `pytest` et `ruff check .` : 57 tests (point de rosée, DS18B20, MLX90614, plafond de puissance, sécurités, régulation en boucle fermée, sortie PWM simulée, chien de garde). Deux sont ignorés si gpiozero n'est pas installé, et celui du chien de garde systemd a besoin d'un socket Unix : il ne passe que sur Linux.
