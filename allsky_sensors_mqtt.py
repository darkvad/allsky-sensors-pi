#!/usr/bin/env python3
"""Capteurs et chauffage anti-buée de l'allsky, publiés sur MQTT, pour la Raspberry Pi.

Capteurs :
  - BME280 (I2C)     : température, humidité et pression extérieures ; point de rosée calculé
  - DS18B20 (1-Wire) : température dans le dôme
  - MLX90614 (I2C)   : température du ciel, lue par un thermomètre infrarouge sans contact
    (ou lue par MQTT sur un SQMeter : SKY_TEMP_TOPIC, quand le capteur n'est pas sur la Pi)
Chauffage anti-buée (facultatif, HEATER_ENABLED=1) :
  - commande PWM d'un module MOSFET alimentant des résistances ; régulation de la température du dôme
    à (point de rosée + marge) ; le rapport cyclique appliqué est publié (dome_heater_pwm, en %).
  - forçage ponctuel : une consigne (override.json, dossier STATE_DIR) vise une température donnée sans
    tenir compte du point de rosée ; elle peut être déposée par l'interface web, par la commande
    --heater-force, ou reçue par MQTT (<préfixe>/heater_command : « 18 », « 18 30 » ou « cancel »).
    La consigne expire et ne contourne aucune sécurité (plafond de puissance, coupure en surchauffe,
    mesure périmée). Le mode employé est publié (heater_mode : auto ou force) ainsi que la cible
    effective (heater_target, en °C).

Chaque valeur est publiée en texte brut (un nombre par topic) pour le capteur « MQTT Broker »
d'indi-allsky. Configuration par variables d'environnement (voir allsky-sensors.env.example).

Essais sans matériel ni broker :  python3 allsky_sensors_mqtt.py --simulate --dry-run --once
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("allsky-sensors")

PREFIX = os.getenv("MQTT_PREFIX", "allsky")
DEFAULT_SENSORS = "bme280,ds18b20"  # capteurs lus si ENABLED n'est pas défini
# Capteurs (variable ENABLED) et topics qu'ils publient, dans l'ordre de publication.
# Le dernier élément du topic sert de nom dans indi-allsky : il doit être distinct d'un topic à l'autre.
SENSOR_TOPICS = {
    "bme280": ("outdoor_temperature", "outdoor_humidity", "outdoor_pressure", "outdoor_dew_point"),
    "ds18b20": ("dome_temperature",),
    "mlx90614": ("sky_temperature",),
}
HEATER_TOPIC = "dome_heater_pwm"
HEATER_MODE_TOPIC = "heater_mode"  # « auto » ou « force »
HEATER_TARGET_TOPIC = "heater_target"  # cible effective en °C (jamais listée dans --print-topics)
HEATER_COMMAND_TOPIC = "heater_command"  # « 18 », « 18 30 » ou « cancel » : consigne pour le service en marche
# Température du ciel lue sur le broker (SQMeter…) : message JSON, chemin de la valeur, fraîcheur admise.
SKY_KEY_DEFAULT = "infrared.skyTemperature"
SKY_STALE_KEY_DEFAULT = "dataStale"  # booléen vrai = donnée périmée : le message est ignoré
SKY_STATUS_KEY_DEFAULT = "infrared.status"  # doit valoir « ok » pour être retenu
SKY_MAX_AGE_DEFAULT = 600.0  # secondes : au-delà, la dernière valeur reçue n'est plus utilisée
SKY_MIN_C, SKY_MAX_C = -80.0, 150.0  # même plage de vraisemblance que le MLX90614
SKY_MAX_PAYLOAD_BYTES = 64 * 1024  # un message d'état tient très largement en deçà
SKY_ERROR_REPEAT_SECONDS = 600  # un message ignoré ne remplit pas le journal
W1_DEVICES = Path("/sys/bus/w1/devices")

# État du programme (consigne de forçage, état publié pour l'interface web) : sous-dossier du programme,
# donc /opt/allsky-sensors/state sur la Pi ; la variable STATE_DIR permet de le déplacer.
STATE_DIR_DEFAULT = Path(__file__).resolve().parent / "state"
OVERRIDE_FILENAME = "override.json"
STATUS_FILENAME = "status.json"
STATUS_VERSION = 1
DEFAULT_ENV_FILE = Path("/etc/allsky-sensors/allsky-sensors.env")  # fichier d'environnement systemd, lu par --heater-force
OVERRIDE_VERSION = 1
OVERRIDE_MIN_MINUTES = 5
OVERRIDE_MAX_MINUTES = 24 * 60
OVERRIDE_DEFAULT_MINUTES = 60
FORCE_MIN_C = -20.0  # plancher de vraisemblance d'une consigne forcée (°C)

HEATER_TICK_SECONDS = 10  # période de régulation quand le chauffage est actif
MAX_TICK_SECONDS = 30  # le chien de garde systemd attend un signe de vie plus souvent que cela
MAX_AGE_SECONDS = 600  # une mesure plus ancienne n'est plus publiée
STALE_SECONDS = 120  # sans mesure valide depuis ce délai, le chauffage s'arrête
DERATING = 0.5  # on n'utilise que la moitié de la puissance nominale des résistances (enfermées dans le dôme)
RESERVED_GPIOS = frozenset({2, 3, 4})  # I2C (2, 3) et 1-Wire (4) ; GPIO_RESERVED en ajoute d'autres (focuser...)


def dew_point(temp_c: float, humidity: float) -> float | None:
    """Point de rosée en °C (formule de Magnus), ou None si l'humidité est invalide."""
    if not 0 < humidity <= 100:
        return None
    b, c = 17.62, 243.12
    gamma = math.log(humidity / 100) + b * temp_c / (c + temp_c)
    return c * gamma / (b - gamma)


def sea_level_pressure(station_hpa: float, temp_c: float, altitude_m: float) -> float:
    """Pression ramenée au niveau de la mer (hPa), comme la publient les services météo.

    Formule barométrique tenant compte de la température de l'air ; 1 m d'altitude vaut environ 0,12 hPa.
    """
    return station_hpa * (1 - 0.0065 * altitude_m / (temp_c + 0.0065 * altitude_m + 273.15)) ** -5.257


def parse_w1_slave(text: str) -> float | None:
    """Température (°C) lue dans le fichier ``w1_slave`` d'un DS18B20, ou None si la mesure est invalide."""
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].strip().endswith("YES") or "t=" not in lines[1]:
        return None  # erreur de CRC ou fichier incomplet
    try:
        raw = int(lines[1].split("t=")[1])
    except ValueError:
        return None
    if raw == 85000:  # valeur de mise sous tension du DS18B20, pas une mesure
        return None
    value = raw / 1000
    return value if -55 <= value <= 125 else None


def systemd_notify(message: bytes) -> None:
    """Signe de vie pour le chien de garde systemd (sans effet hors systemd)."""
    path = os.getenv("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(message)
    except OSError as error:
        log.debug("notification systemd impossible : %s", error)


# --------------------------------------------------------------------------- chauffage


@dataclass(frozen=True)
class HeaterConfig:
    volts: float
    gpio: int = 23
    reserved: tuple[int, ...] = ()  # GPIO déjà utilisés par d'autres matériels (variable GPIO_RESERVED)
    pwm_hz: int = 100
    resistors: int = 6
    ohms: float = 47.0
    rating_watts: float = 1.0
    margin: float = 4.0  # °C au-dessus du point de rosée
    max_dome_temp: float = 35.0  # °C : au-delà, le chauffage est coupé
    kp: float = 0.3  # par °C d'écart
    ki: float = 0.0002  # par °C d'écart et par seconde

    @property
    def watts_per_resistor_full(self) -> float:
        """Puissance dans UNE résistance à 100 % de rapport cyclique."""
        return self.volts**2 / self.ohms

    @property
    def watts_full(self) -> float:
        return self.watts_per_resistor_full * self.resistors

    @property
    def total_ohms(self) -> float:
        return self.ohms / self.resistors

    @property
    def max_duty(self) -> float:
        """Rapport cyclique maximal : chaque résistance reste à DERATING x sa puissance nominale."""
        return min(1.0, DERATING * self.rating_watts / self.watts_per_resistor_full)

    def validate(self) -> None:
        if not 0 < self.volts <= 30:
            raise ValueError("HEATER_SUPPLY_VOLTS doit être compris entre 0 et 30 V")
        if self.resistors < 1 or self.ohms <= 0 or self.rating_watts <= 0:
            raise ValueError("résistances : nombre, valeur (ohms) et puissance nominale doivent être positifs")
        if not 0 <= self.gpio <= 27:
            raise ValueError("HEATER_GPIO doit être compris entre 0 et 27")
        if self.gpio in RESERVED_GPIOS or self.gpio in self.reserved:
            raise ValueError(f"GPIO {self.gpio} déjà utilisé (I2C, 1-Wire ou GPIO_RESERVED) : choisis un GPIO libre")
        if not 1 <= self.pwm_hz <= 1000:
            raise ValueError("HEATER_PWM_HZ doit être compris entre 1 et 1000")
        if not 0 <= self.margin <= 10:
            raise ValueError("HEATER_MARGIN doit être compris entre 0 et 10 °C")
        if not 20 <= self.max_dome_temp <= 45:
            raise ValueError("HEATER_MAX_DOME_TEMP doit être compris entre 20 et 45 °C")
        if self.kp < 0 or self.ki < 0:
            raise ValueError("HEATER_KP et HEATER_KI ne peuvent pas être négatifs")

    def describe(self) -> str:
        return (
            f"chauffage : {self.resistors} x {self.ohms:g} ohm en parallèle = {self.total_ohms:.2f} ohm ; "
            f"sous {self.volts:g} V : {self.watts_full:.1f} W à 100 % ({self.watts_per_resistor_full:.2f} W par résistance) ; "
            f"rapport cyclique plafonné à {self.max_duty * 100:.0f} % "
            f"({DERATING * self.rating_watts:g} W par résistance au plus, soit {self.max_duty * self.watts_full:.1f} W au total)"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> HeaterConfig | None:
        """Configuration du chauffage, ou None s'il est désactivé. Lève ValueError si elle est invalide."""
        enabled = env.get("HEATER_ENABLED", "0").strip()
        if enabled not in ("0", "1"):  # « true », « oui »… sont des pièges : on refuse plutôt que d'ignorer
            raise ValueError(f"HEATER_ENABLED doit valoir 0 ou 1 (reçu : {enabled!r})")
        if enabled != "1":
            return None
        if "HEATER_SUPPLY_VOLTS" not in env:
            raise ValueError("HEATER_SUPPLY_VOLTS doit être défini : tension d'alimentation du chauffage, en volts")
        config = cls(
            volts=_as_float(env["HEATER_SUPPLY_VOLTS"]),
            gpio=int(_as_float(env.get("HEATER_GPIO"), 23)),
            reserved=tuple(int(n) for n in env.get("GPIO_RESERVED", "").split(",") if n.strip()),
            pwm_hz=int(_as_float(env.get("HEATER_PWM_HZ"), 100)),
            resistors=int(_as_float(env.get("HEATER_RESISTORS"), 6)),
            ohms=_as_float(env.get("HEATER_RESISTOR_OHMS"), 47),
            rating_watts=_as_float(env.get("HEATER_RESISTOR_RATING_WATTS"), 1),
            margin=_as_float(env.get("HEATER_MARGIN"), 4),
            max_dome_temp=_as_float(env.get("HEATER_MAX_DOME_TEMP"), 35),
            kp=_as_float(env.get("HEATER_KP"), 0.3),
            ki=_as_float(env.get("HEATER_KI"), 0.0002),
        )
        config.validate()
        return config


# --------------------------------------------------------------------------- fichier d'environnement


def read_env_file(path: Path) -> dict[str, str]:
    """Variables d'un fichier d'environnement systemd : lignes ``CLE=valeur``, ``#`` commentaire.

    Les lignes qui ne sont pas de simples affectations sont ignorées : le fichier reste la propriété
    de l'utilisateur (commentaires, ordre, variables inconnues), on n'en lit que ce qui est compris.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # utf-8-sig : tolère un BOM (éditeur Windows)
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _write_private(path: Path, content: str) -> None:
    """Écrit un fichier en une seule opération : temporaire, permissions 600, sauvegarde ``.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        os.chmod(backup, 0o600)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def update_env_file(path: Path, changes: Mapping[str, str]) -> None:
    """Met à jour des variables dans un fichier d'environnement systemd, sans le réécrire.

    Seules les clés du schéma sont acceptées (impossible d'injecter une variable arbitraire), les
    valeurs ne peuvent pas contenir de saut de ligne. Commentaires, ordre et variables inconnues sont
    préservés ; une clé absente est ajoutée en fin de fichier. Écriture atomique, mode 600 et copie
    ``.bak`` : soit l'ancien fichier, soit le nouveau, jamais un fichier tronqué.
    """
    unknown = sorted(set(changes) - ENV_KEYS)
    if unknown:
        raise ValueError(f"clé inconnue : {', '.join(unknown)}")
    for key, value in changes.items():
        if any(character in value for character in "\r\n\0"):
            raise ValueError(f"{key} : une valeur ne peut pas contenir de saut de ligne")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        text = ""
    lines = text.splitlines()
    for key, value in changes.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    while lines and not lines[-1].strip():
        lines.pop()
    _write_private(path, "\n".join(lines) + "\n")


def cli_env() -> dict[str, str]:
    """Environnement effectif d'une commande lancée à la main (par SSH, par exemple).

    Le service reçoit son environnement de systemd ; ici, on lit d'abord le fichier d'environnement
    (ENV_FILE, par défaut /etc/allsky-sensors/allsky-sensors.env), puis os.environ, qui garde la priorité. Un fichier
    illisible (droits root, par exemple) donne un avertissement clair plutôt qu'un plantage.
    """
    path = Path(os.getenv("ENV_FILE", str(DEFAULT_ENV_FILE)))
    values: dict[str, str] = {}
    if path.is_file():
        try:
            values = read_env_file(path)
        except OSError as error:
            log.warning("%s illisible (%s) : seules les variables du shell sont prises en compte", path, error)
    return {**values, **os.environ}


# --------------------------------------------------------------------------- configuration


def _split_list(text: str) -> list[str]:
    """Éléments d'une liste séparée par des virgules, vides ignorés."""
    return [word.strip() for word in text.split(",") if word.strip()]


def _number_from_text(text: str, kind: str) -> float | None:
    """Nombre lu dans une valeur de fichier (« 4,5 » accepté), ou None si ce n'est pas un nombre."""
    if kind == "hex":
        try:
            return float(int(text, 16))
        except ValueError:
            return None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    if kind == "int" and not value.is_integer():
        return None
    return value


def _as_float(text: str | None, default: float = 0.0) -> float:
    """Nombre d'une variable d'environnement, ``default`` si elle est absente ou vide."""
    cleaned = "" if text is None else text.strip().replace(",", ".")
    return float(cleaned) if cleaned else default


@dataclass(frozen=True)
class Param:
    """Descripteur d'une variable du fichier d'environnement (validation, formulaire web, doc)."""

    name: str
    section: str
    label: str
    kind: str  # text, secret, int, float, flag, ints, sensors, hex
    default: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int = 0
    unit: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    allow_empty: bool = False  # une valeur vide a un sens (variable facultative)
    restart: bool = False  # pris en compte seulement au redémarrage du service

    @property
    def suffix(self) -> str:
        return f" {self.unit}" if self.unit else ""

    def _bounds_error(self, number: float) -> str | None:
        if self.minimum is not None and number < self.minimum:
            return f"{self.name} doit être au moins {self.minimum:g}{self.suffix}"
        if self.maximum is not None and number > self.maximum:
            return f"{self.name} doit être au plus {self.maximum:g}{self.suffix}"
        return None

    def validate_value(self, value: str) -> str | None:
        """Message d'erreur pour une valeur présente dans le fichier, ou None si elle convient."""
        text = value.strip()
        if not text:
            return None if self.allow_empty else f"{self.name} ne peut pas être vide"
        if self.kind in ("text", "secret"):
            if len(text) < self.min_length:
                return f"{self.name} doit compter au moins {self.min_length} caractères"
            return None
        if self.kind == "flag":
            return None if text in ("0", "1") else f"{self.name} doit valoir 0 ou 1"
        if self.kind == "sensors":
            unknown = [name for name in _split_list(text) if name not in SENSOR_TOPICS]
            if unknown:
                return f"{self.name} : capteur inconnu ({', '.join(unknown)}) ; attendu : {', '.join(SENSOR_TOPICS)}"
            if not _split_list(text):
                return f"{self.name} doit nommer au moins un capteur"
            return None
        if self.kind == "ints":
            for word in _split_list(text):
                if not word.isdigit() or not 0 <= int(word) <= 27:
                    return f"{self.name} : « {word} » n'est pas un numéro de GPIO (0 à 27)"
            return None
        number = _number_from_text(text, self.kind)
        if number is None:
            return f"{self.name} : « {text} » n'est pas un nombre"
        return self._bounds_error(number)


ENV_SCHEMA: tuple[Param, ...] = (
    Param("MQTT_HOST", "Broker MQTT", "Adresse du broker", "text", default="localhost", help="nom ou IP du serveur Mosquitto"),
    Param("MQTT_PORT", "Broker MQTT", "Port du broker", "int", default="1883", minimum=1, maximum=65535),
    Param("MQTT_USER", "Broker MQTT", "Utilisateur MQTT", "text", default="", allow_empty=True),
    Param("MQTT_PASSWORD", "Broker MQTT", "Mot de passe MQTT", "secret", default="", allow_empty=True),
    Param("MQTT_PREFIX", "Broker MQTT", "Préfixe des topics", "text", default="allsky", restart=True),
    Param(
        "ENABLED",
        "Capteurs",
        "Capteurs lus",
        "sensors",
        default=DEFAULT_SENSORS,
        help="bme280 (extérieur), ds18b20 (dôme), mlx90614 (ciel)",
    ),
    Param("BME280_ADDRESS", "Capteurs", "Adresse I2C du BME280", "hex", default="0x76", minimum=0x08, maximum=0x77),
    Param(
        "ALTITUDE_M",
        "Capteurs",
        "Altitude du capteur",
        "float",
        minimum=-500,
        maximum=9000,
        unit="m",
        allow_empty=True,
        help="si renseignée : pression ramenée au niveau de la mer",
    ),
    Param("DS18B20_ID", "Capteurs", "Identifiant du DS18B20 du dôme", "text", default="", allow_empty=True),
    Param(
        "DS18B20_OFFSET_C",
        "Capteurs",
        "Correction du DS18B20",
        "float",
        default="0",
        minimum=-10,
        maximum=10,
        unit="°C",
        allow_empty=True,
    ),
    Param("MLX90614_ADDRESS", "Capteurs", "Adresse I2C du MLX90614", "hex", default="0x5A", minimum=0x08, maximum=0x77),
    Param(
        "MLX90614_OFFSET_C",
        "Capteurs",
        "Correction du MLX90614",
        "float",
        default="0",
        minimum=-20,
        maximum=20,
        unit="°C",
        allow_empty=True,
    ),
    Param("INTERVAL", "Capteurs", "Période de publication", "int", default="60", minimum=10, maximum=3600, unit="s"),
    Param(
        "SKY_TEMP_TOPIC",
        "Température du ciel (MQTT)",
        "Topic du SQMeter",
        "text",
        default="",
        allow_empty=True,
        help="message JSON publié sur le broker, ex. sqmeter/state ; vide : pas de source externe",
    ),
    Param(
        "SKY_TEMP_KEY",
        "Température du ciel (MQTT)",
        "Chemin de la température",
        "text",
        default=SKY_KEY_DEFAULT,
        allow_empty=True,
        help="clé du JSON, points pour descendre ; vide : le message entier doit être un nombre",
    ),
    Param(
        "SKY_STALE_KEY",
        "Température du ciel (MQTT)",
        "Chemin du drapeau « périmé »",
        "text",
        default=SKY_STALE_KEY_DEFAULT,
        allow_empty=True,
        help="booléen vrai = donnée périmée, message ignoré ; vide : pas de contrôle",
    ),
    Param(
        "SKY_STATUS_KEY",
        "Température du ciel (MQTT)",
        "Chemin du statut",
        "text",
        default=SKY_STATUS_KEY_DEFAULT,
        allow_empty=True,
        help="doit valoir « ok » ; vide : pas de contrôle",
    ),
    Param(
        "SKY_MAX_AGE",
        "Température du ciel (MQTT)",
        "Fraîcheur admise",
        "int",
        default="600",
        minimum=10,
        maximum=86400,
        unit="s",
        help="au-delà, la dernière valeur reçue est ignorée",
    ),
    Param("HEATER_ENABLED", "Chauffage", "Chauffage activé", "flag", default="0", help="1 pour activer"),
    Param(
        "HEATER_SUPPLY_VOLTS",
        "Chauffage",
        "Tension d'alimentation",
        "float",
        minimum=0.1,
        maximum=30,
        unit="V",
        help="obligatoire si le chauffage est activé",
    ),
    Param("HEATER_GPIO", "Chauffage", "GPIO de commande", "int", default="23", minimum=0, maximum=27),
    Param("GPIO_RESERVED", "Chauffage", "GPIO déjà utilisés", "ints", default="", allow_empty=True),
    Param("HEATER_PWM_HZ", "Chauffage", "Fréquence PWM", "int", default="100", minimum=1, maximum=1000, unit="Hz"),
    Param("HEATER_RESISTORS", "Chauffage", "Nombre de résistances", "int", default="6", minimum=1, maximum=20),
    Param(
        "HEATER_RESISTOR_OHMS",
        "Chauffage",
        "Valeur d'une résistance",
        "float",
        default="47",
        minimum=0.1,
        maximum=1000,
        unit="Ω",
    ),
    Param(
        "HEATER_RESISTOR_RATING_WATTS",
        "Chauffage",
        "Puissance nominale d'une résistance",
        "float",
        default="1",
        minimum=0.01,
        maximum=100,
        unit="W",
    ),
    Param(
        "HEATER_MARGIN",
        "Chauffage",
        "Marge au-dessus du point de rosée",
        "float",
        default="4",
        minimum=0,
        maximum=10,
        unit="°C",
    ),
    Param(
        "HEATER_MAX_DOME_TEMP",
        "Chauffage",
        "Coupure en surchauffe",
        "float",
        default="35",
        minimum=20,
        maximum=45,
        unit="°C",
    ),
    Param("HEATER_KP", "Chauffage", "Gain proportionnel", "float", default="0.3", minimum=0, maximum=100),
    Param("HEATER_KI", "Chauffage", "Gain intégral", "float", default="0.0002", minimum=0, maximum=10),
    Param(
        "WEB_BIND",
        "Interface web",
        "Adresse d'écoute",
        "text",
        default="0.0.0.0",
        restart=True,
        help="0.0.0.0 : tout le réseau local",
    ),
    Param("WEB_PORT", "Interface web", "Port d'écoute", "int", default="8080", minimum=1, maximum=65535, restart=True),
    Param("WEB_USER", "Interface web", "Utilisateur", "text", default="admin", min_length=1, restart=True),
    Param(
        "WEB_PASSWORD",
        "Interface web",
        "Mot de passe",
        "secret",
        min_length=12,
        allow_empty=True,
        restart=True,
        help="obligatoire pour lancer l'interface : 12 caractères au moins",
    ),
    Param(
        "STATE_DIR",
        "Avancé",
        "Dossier d'état",
        "text",
        allow_empty=True,
        restart=True,
        help="consigne de forçage et état publié (défaut : state/ à côté du programme)",
    ),
)

ENV_KEYS = frozenset(param.name for param in ENV_SCHEMA)


@dataclass(frozen=True)
class SkyConfig:
    """Source externe de la température du ciel : un message JSON publié sur le broker MQTT.

    Les valeurs par défaut décrivent le SQMeter (``sqmeter/state``) ; un topic vide désactive la
    fonctionnalité. ``stale_key`` et ``status_key`` sont facultatifs (chemin vide = pas de contrôle).
    """

    topic: str = ""
    key: str = SKY_KEY_DEFAULT
    stale_key: str = SKY_STALE_KEY_DEFAULT
    status_key: str = SKY_STATUS_KEY_DEFAULT
    max_age: float = SKY_MAX_AGE_DEFAULT

    @property
    def enabled(self) -> bool:
        """Vrai si une source MQTT est configurée."""
        return bool(self.topic)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SkyConfig:
        """Source lue dans la configuration ; lève ValueError si le topic est inutilisable."""
        topic = env.get("SKY_TEMP_TOPIC", "").strip()
        # Un topic MQTT est une chaîne exacte : un espace final l'a déjà fait échouer une fois (22/11).
        if any(character in topic for character in " \t\r\n+#"):
            raise ValueError(
                f"SKY_TEMP_TOPIC doit être un topic littéral, sans espace, « + » ni « # » (reçu : {topic!r})"
            )
        return cls(
            topic=topic,
            key=env.get("SKY_TEMP_KEY", SKY_KEY_DEFAULT).strip(),  # vide = le message entier doit être un nombre
            stale_key=env.get("SKY_STALE_KEY", SKY_STALE_KEY_DEFAULT).strip(),
            status_key=env.get("SKY_STATUS_KEY", SKY_STATUS_KEY_DEFAULT).strip(),
            max_age=_as_float(env.get("SKY_MAX_AGE"), SKY_MAX_AGE_DEFAULT),
        )


def configured_env_file(env: Mapping[str, str] | None = None) -> Path | None:
    """Fichier d'environnement à relire, ou None s'il n'y en a pas.

    ``ENV_FILE`` vide désactive la surveillance (essais, tests) ; ``ENV_FILE`` absent retombe sur
    ``/etc/allsky-sensors/allsky-sensors.env`` s'il existe — ainsi le service reste correct même si l'unité systemd
    oublie de le déclarer.
    """
    source = os.environ if env is None else env
    if "ENV_FILE" in source:
        text = source["ENV_FILE"].strip()
        return Path(text) if text else None
    return DEFAULT_ENV_FILE if DEFAULT_ENV_FILE.is_file() else None


def service_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Configuration effective du service : environnement du processus, puis le fichier par-dessus.

    Le fichier gagne : c'est lui que l'on modifie (éditeur ou interface web), l'environnement n'en est
    qu'un instantané pris par systemd au démarrage. Sert au démarrage comme à chaque rechargement.
    """
    source = dict(os.environ if env is None else env)
    path = configured_env_file(source)
    if path is None:
        return source
    if not path.is_file():
        # Unité systemd mal écrite ou fichier déplacé : sans ce message, le service tournerait muet avec
        # l'environnement seul (voir les incidents du 22/11).
        log.warning("%s introuvable : configuration de l'environnement uniquement", path)
        return source
    try:
        source.update(read_env_file(path))
    except OSError as error:
        log.warning("%s illisible (%s) : configuration de l'environnement uniquement", path, error)
    return source


class ConfigError(ValueError):
    """Configuration refusée : un message lisible par problème (les tests et le web les affichent)."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__(" ; ".join(messages))
        self.messages = tuple(messages)


def validate_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Erreurs de configuration, dans l'ordre du schéma (liste vide : tout va bien).

    Chaque variable présente est vérifiée selon son type et ses bornes, puis la configuration du
    chauffage est validée en bloc (GPIO réservés, tension obligatoire si le chauffage est activé…).
    """
    source = os.environ if env is None else env
    errors: list[str] = []
    for param in ENV_SCHEMA:
        if param.name in source:
            message = param.validate_value(source[param.name])
            if message:
                errors.append(message)
    try:
        HeaterConfig.from_env(source)
    except ValueError as error:
        errors.append(f"chauffage : {error}")
    try:
        SkyConfig.from_env(source)
    except ValueError as error:
        errors.append(f"température du ciel : {error}")
    return errors


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    user: str = ""
    password: str = ""


@dataclass(frozen=True)
class Settings:
    """Configuration validée, prête à l'emploi : c'est elle que la boucle recharge à chaud."""

    enabled: tuple[str, ...]
    interval: int
    altitude: float | None
    ds18b20_id: str | None
    ds18b20_offset: float
    bme280_address: int
    mlx90614_address: int
    mlx90614_offset: float
    heater: HeaterConfig | None
    mqtt: MqttConfig
    state_dir: Path
    sky: SkyConfig = SkyConfig()  # source MQTT de la température du ciel (topic vide = désactivée)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Construit la configuration ; lève ConfigError si le fichier contient des erreurs."""
        source = os.environ if env is None else env
        errors = validate_env(source)
        if errors:
            raise ConfigError(errors)
        enabled = tuple(_split_list(source.get("ENABLED", DEFAULT_SENSORS)))
        altitude_text = source.get("ALTITUDE_M", "").strip()
        return cls(
            enabled=enabled,
            interval=int(_as_float(source.get("INTERVAL"), 60)),
            altitude=_as_float(altitude_text) if altitude_text else None,
            ds18b20_id=source.get("DS18B20_ID", "").strip() or None,
            ds18b20_offset=_as_float(source.get("DS18B20_OFFSET_C")),
            bme280_address=int(source.get("BME280_ADDRESS", "0x76"), 16),
            mlx90614_address=int(source.get("MLX90614_ADDRESS", "0x5a"), 16),
            mlx90614_offset=_as_float(source.get("MLX90614_OFFSET_C")),
            heater=HeaterConfig.from_env(source),
            mqtt=MqttConfig(
                host=source.get("MQTT_HOST", "localhost").strip() or "localhost",
                port=int(_as_float(source.get("MQTT_PORT"), 1883)),
                user=source.get("MQTT_USER", "").strip(),
                password=source.get("MQTT_PASSWORD", ""),
            ),
            state_dir=Path(source.get("STATE_DIR", "").strip() or str(STATE_DIR_DEFAULT)),
            sky=SkyConfig.from_env(source),
        )


# --------------------------------------------------------------------------- forçage du chauffage


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Dossier d'état (variable STATE_DIR) : ``state`` à côté du programme par défaut."""
    return Path((os.environ if env is None else env).get("STATE_DIR", str(STATE_DIR_DEFAULT)))


def override_path(env: Mapping[str, str] | None = None) -> Path:
    """Fichier où une consigne de forçage est déposée."""
    return state_dir(env) / OVERRIDE_FILENAME


def write_json_atomic(path: Path, data: Mapping[str, object], mode: int = 0o600) -> None:
    """Écrit un objet JSON en une seule opération : fichier temporaire, puis renommage.

    Le service ne lit ainsi jamais un fichier à moitié écrit, même si l'interface web est interrompue.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def clear_override(path: Path) -> bool:
    """Supprime le fichier de forçage ; True s'il existait."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        log.warning("consigne de forçage impossible à supprimer (%s) : %s", path, error)
        return False
    return True


@dataclass(frozen=True)
class HeaterOverride:
    """Consigne de forçage : température à atteindre, sans tenir compte du point de rosée."""

    target_c: float
    expires_at: float  # epoch (time.time()) : au-delà, retour en régulation automatique
    created_at: float = 0.0

    def remaining(self, now: float) -> float:
        """Secondes restantes avant expiration (0 si la consigne est expirée)."""
        return max(self.expires_at - now, 0.0)


def _number(value: object) -> float | None:
    """Nombre fini tiré d'un JSON, ou None (les booléens ne sont pas des nombres ici)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def load_override(path: Path, now: float, max_target_c: float, purge_expired: bool = True) -> tuple[HeaterOverride | None, str | None]:
    """Consigne de forçage lue sur disque : ``(consigne, None)`` ou ``(None, motif d'ignorance)``.

    Les valeurs lues sont bornées : consigne ramenée entre FORCE_MIN_C et max_target_c, expiration
    plafonnée à OVERRIDE_MAX_MINUTES. Une consigne expirée est effacée (``purge_expired``) ; un fichier
    inexploitable est conservé, il explique ce qui s'est passé. Un fichier absent (``(None, None)``)
    signifie simplement « régulation automatique ».
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"fichier de forçage illisible ({error})"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "fichier de forçage illisible (JSON invalide)"
    if not isinstance(data, dict):
        return None, "fichier de forçage invalide (objet JSON attendu)"
    version = data.get("version", OVERRIDE_VERSION)
    if version != OVERRIDE_VERSION:
        return None, f"fichier de forçage en version {version!r}, version {OVERRIDE_VERSION} attendue"
    mode = data.get("mode", "force")
    if mode != "force":
        return None, f"mode de forçage inconnu : {mode!r}"
    target = _number(data.get("target_c"))
    if target is None:
        return None, "consigne de forçage illisible (target_c)"
    expires = _number(data.get("expires_at"))
    if expires is None:
        return None, "date d'expiration du forçage illisible (expires_at)"
    if expires <= now:
        if purge_expired:
            clear_override(path)
        return None, "forçage expiré"
    return (
        HeaterOverride(
            target_c=min(max(target, FORCE_MIN_C), max_target_c),  # jamais au-delà de la coupure en surchauffe
            expires_at=min(expires, now + OVERRIDE_MAX_MINUTES * 60),  # une consigne ne dure jamais plus d'un jour
            created_at=_number(data.get("created_at")) or 0.0,
        ),
        None,
    )


def write_override(path: Path, target_c: float, minutes: float, now: float, max_target_c: float) -> HeaterOverride:
    """Enregistre une consigne de forçage (interface web). Lève ValueError si elle est incohérente."""
    target = _number(target_c)
    if target is None:
        raise ValueError("consigne de forçage : température invalide")
    if not FORCE_MIN_C <= target <= max_target_c:
        raise ValueError(f"consigne de forçage : température attendue entre {FORCE_MIN_C:g} et {max_target_c:g} °C")
    duration = _number(minutes)
    if duration is None:
        raise ValueError("durée de forçage invalide")
    if not OVERRIDE_MIN_MINUTES <= duration <= OVERRIDE_MAX_MINUTES:
        raise ValueError(f"durée de forçage attendue entre {OVERRIDE_MIN_MINUTES} et {OVERRIDE_MAX_MINUTES} minutes")
    override = HeaterOverride(target_c=target, expires_at=now + duration * 60, created_at=now)
    write_json_atomic(
        path,
        {
            "version": OVERRIDE_VERSION,
            "mode": "force",
            "target_c": override.target_c,
            "minutes": int(duration),
            "created_at": override.created_at,
            "expires_at": override.expires_at,
        },
    )
    return override


def apply_heater_command(text: str, config: HeaterConfig, path: Path, now: float) -> str:
    """Applique une commande de chauffage reçue par MQTT et renvoie le compte rendu.

    Formats : « 18 » (18 °C pendant OVERRIDE_DEFAULT_MINUTES), « 18 30 » (18 °C pendant 30 min),
    « cancel » (retour à la régulation automatique). Lève ValueError si la commande est inutilisable.
    """
    words = text.split()
    if not words:
        raise ValueError("commande vide")
    if words[0].lower() in ("cancel", "annuler", "stop", "off"):
        if clear_override(path):
            return "consigne annulée : retour à la régulation automatique"
        return "aucune consigne à annuler : déjà en régulation automatique"
    if len(words) > 2:
        raise ValueError("format attendu : « 18 », « 18 30 » ou « cancel »")
    try:
        target = float(words[0].replace(",", "."))  # une virgule décimale française est tolérée
    except ValueError:
        raise ValueError(f"température illisible : {words[0]!r}") from None
    try:
        minutes = float(words[1]) if len(words) == 2 else OVERRIDE_DEFAULT_MINUTES
    except ValueError:
        raise ValueError(f"durée illisible : {words[1]!r}") from None
    override = write_override(path, target, minutes, now, config.max_dome_temp)
    until = time.strftime("%H:%M", time.localtime(override.expires_at))
    return f"{override.target_c:g} °C pendant {minutes:g} min (jusqu'à {until})"


# --------------------------------------------------------------------------- état publié


def status_path(env: Mapping[str, str] | None = None) -> Path:
    """Fichier d'état publié par le service pour l'interface web."""
    return state_dir(env) / STATUS_FILENAME


def write_status(path: Path, status: Mapping[str, object]) -> None:
    """Publie l'état courant du service (écriture atomique, lisible par l'interface web).

    Aucun secret n'y figure : l'interface peut donc le lire sans privilège particulier.
    """
    write_json_atomic(path, {**status, "version": STATUS_VERSION}, mode=0o644)


def read_status(path: Path) -> dict[str, object] | None:
    """État publié par le service, ou None s'il est absent, illisible ou d'une autre version."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATUS_VERSION:
        return None
    return data


class HeaterController:
    """Régulateur PI : amène la température du dôme à (point de rosée + marge), ou à une consigne forcée.

    La sortie est un rapport cyclique entre 0 et ``config.max_duty``. Toute mesure indispensable
    manquante depuis plus de STALE_SECONDS, ou un dôme trop chaud, coupe le chauffage ; une consigne
    forcée ne dispense que de la mesure du point de rosée, jamais de celle du dôme.
    """

    def __init__(self, config: HeaterConfig) -> None:
        self.config = config
        self._integral = 0.0
        self._last_time: float | None = None
        self._dome: float | None = None
        self._dew: float | None = None
        self._dome_at: float | None = None
        self._dew_at: float | None = None
        self._mode: str | None = None
        self._setpoint: float | None = None

    @staticmethod
    def _stale(at: float | None, now: float) -> bool:
        return at is None or now - at > STALE_SECONDS

    @property
    def setpoint(self) -> float | None:
        """Cible (°C) du dernier calcul effectif : point de rosée + marge, ou consigne forcée."""
        return self._setpoint

    def update(self, dome: float | None, dew: float | None, now: float, target: float | None = None) -> float:
        """Rapport cyclique à appliquer (entre 0 et ``config.max_duty``).

        ``target`` est la consigne d'un forçage : elle remplace (point de rosée + marge) et dispense
        d'une mesure de point de rosée. Les sécurités restent identiques : sans mesure récente du dôme,
        ou dôme déjà à ``config.max_dome_temp``, le chauffage est coupé, forçage ou non.
        """
        if dome is not None:
            self._dome, self._dome_at = dome, now
        if dew is not None:
            self._dew, self._dew_at = dew, now
        dt = 0.0 if self._last_time is None else min(max(now - self._last_time, 0.0), 60.0)
        self._last_time = now

        mode = "force" if target is not None else "auto"
        if mode != self._mode:  # l'intégrale accumulée dans l'autre mode n'a plus de sens
            self._mode, self._integral = mode, 0.0

        if self._stale(self._dome_at, now) or (target is None and self._stale(self._dew_at, now)):
            self._integral = 0.0
            self._setpoint = None
            return 0.0  # pas de mesure fiable : pas de chauffage
        assert self._dome is not None
        if self._dome >= self.config.max_dome_temp:
            self._integral = 0.0
            self._setpoint = None
            return 0.0

        if target is None:
            assert self._dew is not None
            self._setpoint = self._dew + self.config.margin
        else:
            self._setpoint = min(target, self.config.max_dome_temp)  # jamais au-delà de la coupure en surchauffe
        error = self._setpoint - self._dome
        raw = self.config.kp * error + self._integral
        # Anti-emballement : on n'intègre pas quand la sortie est saturée dans le sens de l'écart.
        if (raw < 1.0 or error < 0) and (raw > 0.0 or error > 0):
            self._integral = min(max(self._integral + self.config.ki * error * dt, 0.0), 1.0)
        level = min(max(self.config.kp * error + self._integral, 0.0), 1.0)
        return level * self.config.max_duty


class PwmOutput:
    """Sortie PWM logicielle (gpiozero) : si le programme s'arrête, le signal s'arrête avec lui."""

    def __init__(self, gpio: int, hz: int) -> None:
        from gpiozero import PWMOutputDevice

        self._device = PWMOutputDevice(gpio, frequency=hz, initial_value=0)

    def set(self, duty: float) -> None:
        self._device.value = min(max(duty, 0.0), 1.0)

    def close(self) -> None:
        try:
            self._device.value = 0
        finally:
            self._device.close()


class NullOutput:
    """Sortie factice (essais) : ne touche à aucun GPIO."""

    def __init__(self) -> None:
        self.duty = 0.0

    def set(self, duty: float) -> None:
        self.duty = duty

    def close(self) -> None:
        self.duty = 0.0


# --------------------------------------------------------------------------- capteurs


_I2C_BUS = None  # le bus I2C de la carte, ouvert une seule fois : le BME280 et le MLX90614 le partagent


def i2c_bus():
    """Bus I2C de la carte, ouvert au premier besoin."""
    global _I2C_BUS
    if _I2C_BUS is None:
        import board

        _I2C_BUS = board.I2C()
    return _I2C_BUS


class Bme280:
    def __init__(self, address: int, altitude: float | None = None) -> None:
        self._altitude = altitude
        from adafruit_bme280 import basic as adafruit_bme280

        self._device = adafruit_bme280.Adafruit_BME280_I2C(i2c_bus(), address=address)

    def read(self) -> dict[str, float]:
        temp = float(self._device.temperature)
        humidity = min(float(self._device.relative_humidity), 100.0)
        station = float(self._device.pressure)
        values = {
            "outdoor_temperature": temp,
            "outdoor_humidity": humidity,
            "outdoor_pressure": station,
        }
        if self._altitude is not None:
            values["outdoor_pressure"] = sea_level_pressure(station, temp, self._altitude)
            values["outdoor_pressure_station"] = station
        point = dew_point(temp, humidity)
        if point is not None:
            values["outdoor_dew_point"] = point
        return values


class Ds18b20:
    def __init__(self, device_id: str | None, offset: float = 0.0) -> None:
        self._offset = offset  # correction (°C) ajoutée à la mesure brute, pour une sonde qui lit faux
        if device_id:
            self._file = W1_DEVICES / device_id / "w1_slave"
        else:
            if not W1_DEVICES.is_dir():
                raise RuntimeError("1-Wire non activé : sudo raspi-config, Interface Options, 1-Wire, puis redémarrer")
            found = sorted(W1_DEVICES.glob("28-*"))
            if not found:
                raise RuntimeError(
                    "1-Wire actif mais aucun DS18B20 détecté : vérifie le câblage (DQ sur GPIO4, 4,7 kΩ entre DQ et 3,3 V, sens du TO-92)"
                )
            self._file = found[0] / "w1_slave"

    def read(self) -> dict[str, float]:
        value = parse_w1_slave(self._file.read_text())
        return {} if value is None else {"dome_temperature": value + self._offset}


class Mlx90614:
    """Thermomètre infrarouge sans contact : mesure la température de ce qu'il vise (le ciel, la nuit).

    La valeur ne veut rien dire si le capteur vise autre chose que le ciel (dôme, mur, sol, sa propre
    monture). Verre et plexiglas sont opaques à l'infrarouge lointain (8-14 µm) : placé sous le dôme,
    il mesure la température du dôme, pas celle du ciel. Le bus I2C doit rester à 100 kHz (défaut).
    """

    def __init__(self, address: int, offset: float = 0.0) -> None:
        self._offset = offset  # correction (°C) ajoutée à la mesure brute, pour un capteur qui lit faux
        import adafruit_mlx90614

        self._device = adafruit_mlx90614.MLX90614(i2c_bus(), address=address)

    def read(self) -> dict[str, float]:
        value = float(self._device.object_temperature) + self._offset
        if not -80 <= value <= 150:  # -273,15 °C : capteur non alimenté ; au-delà : erreur de bus
            return {}
        return {"sky_temperature": value}


class Simulated:
    """Valeurs plausibles pour tester sans matériel."""

    def __init__(self, altitude: float | None = None) -> None:
        self._altitude = altitude

    def read(self) -> dict[str, float]:
        phase = math.sin(time.time() / 600)
        temp, humidity = 8 + 4 * phase, 70 + 15 * phase
        station = 955 + 3 * phase
        values = {
            "outdoor_temperature": temp,
            "outdoor_humidity": humidity,
            "outdoor_pressure": station,
            "dome_temperature": 11 + 2 * phase,
            "sky_temperature": temp - 18 + 6 * phase,  # ciel clair : bien plus froid que l'air
        }
        if self._altitude is not None:
            values["outdoor_pressure"] = sea_level_pressure(station, temp, self._altitude)
            values["outdoor_pressure_station"] = station
        point = dew_point(temp, humidity)
        if point is not None:
            values["outdoor_dew_point"] = point
        return values


class WarnOnce:
    """Évite de répéter le même avertissement à chaque cycle (le journal serait inondé)."""

    def __init__(self, every: float = 600.0) -> None:
        self._every = every
        self._last: dict[str, float] = {}

    def __call__(self, key: str, message: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last.get(key, -self._every) >= self._every:
            log.warning(message, *args)
            self._last[key] = now


def enabled_sensors(env: Mapping[str, str] | None = None) -> list[str]:
    """Capteurs à lire (variable ENABLED, « bme280,ds18b20 » par défaut)."""
    source = os.environ if env is None else env
    return [name.strip() for name in source.get("ENABLED", DEFAULT_SENSORS).split(",") if name.strip()]


def temp_offset(variable: str, limit: float) -> float:
    """Correction (°C) lue dans une variable d'environnement, 0 si elle n'est pas définie.

    Lève ValueError si la valeur n'est pas un nombre ou si elle sort de la plage ± limit.
    """
    text = os.getenv(variable, "").strip()
    if not text:
        return 0.0
    value = float(text)
    if not -limit <= value <= limit:
        raise ValueError(f"{variable} doit être compris entre {-limit:g} et {limit:g} °C")
    return value


def ds18b20_offset() -> float:
    """Correction du DS18B20 en °C (variable DS18B20_OFFSET_C, 0 par défaut). Lève ValueError si elle est invalide."""
    return temp_offset("DS18B20_OFFSET_C", 10)


def mlx90614_offset() -> float:
    """Correction du MLX90614 en °C (variable MLX90614_OFFSET_C, 0 par défaut). Lève ValueError si elle est invalide."""
    return temp_offset("MLX90614_OFFSET_C", 20)


def build_sensor(name: str, altitude: float | None = None):
    if name == "bme280":
        return Bme280(int(os.getenv("BME280_ADDRESS", "0x76"), 16), altitude)
    if name == "ds18b20":
        return Ds18b20(os.getenv("DS18B20_ID") or None, ds18b20_offset())
    if name == "mlx90614":
        return Mlx90614(int(os.getenv("MLX90614_ADDRESS", "0x5a"), 16), mlx90614_offset())
    raise ValueError(f"capteur inconnu : {name}")


# --------------------------------------------------------------------------- température du ciel par MQTT


def _json_node(data: object, key: str) -> tuple[object | None, str | None]:
    """Nœud d'un objet JSON au chemin ``a.b.c`` (``key`` vide : le message entier)."""
    node = data
    for part in key.split(".") if key else ():
        if not isinstance(node, Mapping) or part not in node:
            return None, f"clé {key!r} absente du message"
        node = node[part]
    return node, None


def read_sky_payload(payload: bytes | str, config: SkyConfig) -> tuple[float | None, str | None]:
    """Température du ciel tirée d'un message JSON : ``(valeur, motif)``, un seul des deux est None.

    ``config.key`` est un chemin (« infrared.skyTemperature ») ; vide, le message entier doit être un
    nombre. Le drapeau ``stale_key`` et le statut ``status_key`` sont facultatifs (chemin vide) et
    écartent une donnée que la source annonce elle-même comme périmée ou absente.
    """
    if len(payload) > SKY_MAX_PAYLOAD_BYTES:
        return None, f"message trop volumineux ({len(payload)} octets)"
    try:
        data = json.loads(payload)
    except ValueError:
        return None, "JSON illisible"
    if config.stale_key:
        stale, missing = _json_node(data, config.stale_key)
        if missing is None and stale is True:
            return None, f"{config.stale_key} signale une donnée périmée"
    if config.status_key:
        status, missing = _json_node(data, config.status_key)
        if missing is None and str(status).strip().lower() != "ok":
            return None, f"{config.status_key} vaut {status!r} (« ok » attendu)"
    node, missing = _json_node(data, config.key)
    if missing is not None:
        return None, missing
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return None, f"{config.key or 'la valeur'} n'est pas un nombre"
    value = float(node)
    if not math.isfinite(value) or not SKY_MIN_C <= value <= SKY_MAX_C:
        return None, f"température hors bornes ({value:g} °C)"
    return value, None


class SkyFeed:
    """Dernière température du ciel reçue par MQTT : le client écrit, la boucle de mesure lit.

    La valeur est horodatée à la réception (``time.monotonic``, la même horloge que la boucle) : une
    source qui se tait est donc oubliée après ``SKY_MAX_AGE``, comme un capteur périmé.
    """

    def __init__(self, config: SkyConfig) -> None:
        self._lock = threading.Lock()
        self._config = config
        self._value: float | None = None
        self._at = 0.0
        self._valid = False
        self._problem: str | None = None
        self._problem_at = 0.0

    @property
    def config(self) -> SkyConfig:
        with self._lock:
            return self._config

    def configure(self, config: SkyConfig) -> None:
        """Applique un topic ou un chemin modifié à chaud (la dernière valeur reste utilisable)."""
        with self._lock:
            self._config = config

    def handle(self, payload: bytes) -> float | None:
        """Traite un message reçu (thread du client MQTT) ; renvoie la valeur retenue, ou None."""
        config = self.config
        value, problem = read_sky_payload(payload, config)
        now = time.monotonic()
        with self._lock:
            if value is None:
                if problem != self._problem or now - self._problem_at > SKY_ERROR_REPEAT_SECONDS:
                    log.warning("température du ciel ignorée sur %s : %s", config.topic, problem)
                    self._problem, self._problem_at = problem, now
                self._valid = False
                return None
            if not self._valid:
                log.info("température du ciel reçue sur %s : %.1f °C", config.topic, value)
            self._value, self._at, self._valid = value, now, True
            self._problem, self._problem_at = None, 0.0
            return value

    def reading(self, now: float, max_age: float) -> tuple[float, float] | None:
        """Valeur et instant de réception, si elle n'a pas dépassé ``max_age`` secondes."""
        with self._lock:
            if self._value is None or now - self._at > max_age:
                return None
            return self._value, self._at


def sky_measurement(
    feed: SkyFeed, config: SkyConfig, current: Mapping[str, float], now: float
) -> tuple[float, float] | None:
    """Mesure du ciel à ajouter : la source MQTT, sauf si un capteur local fournit déjà la valeur."""
    if "sky_temperature" in current or not config.enabled:
        return None
    return feed.reading(now, config.max_age)


# --------------------------------------------------------------------------- MQTT


class Publisher:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        dry_run: bool,
        on_command=None,
        sky_topic: str = "",
        on_sky=None,
    ) -> None:
        self._client = None
        self._pending: list = []
        self._command_topic = f"{PREFIX}/{HEATER_COMMAND_TOPIC}"
        self._on_command = on_command  # appelé avec le texte d'une commande reçue sur le topic de commande
        self._sky_topic = sky_topic  # topic JSON de la température du ciel (vide : pas d'abonnement)
        self._on_sky = on_sky  # appelé avec la charge utile brute reçue sur ce topic
        if dry_run:
            return
        import paho.mqtt.client as mqtt

        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="allsky-sensors")
        except AttributeError:  # paho-mqtt 1.x
            self._client = mqtt.Client(client_id="allsky-sensors")
        if user:
            self._client.username_pw_set(user, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
        self._client.max_queued_messages_set(200)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        # connect_async : le programme démarre même si le broker n'est pas encore joignable.
        log.info("broker MQTT visé : %s:%s%s", host, port, f" (utilisateur {user})" if user else " (anonyme)")
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, *rest) -> None:
        # Compatible paho-mqtt 1.x (code entier) et 2.x (objet ReasonCode).
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.warning("connexion MQTT refusée : %s (identifiants ? adresse ? droits du broker ?)", reason_code)
            return
        log.info("connecté au broker MQTT")
        if self._on_command is not None:  # abonnement refait à chaque connexion, y compris après coupure
            client.subscribe(self._command_topic, qos=1)
            log.info("commandes acceptées sur %s : « 18 », « 18 30 » ou « cancel »", self._command_topic)
        if self._sky_topic:
            client.subscribe(self._sky_topic, qos=1)
            log.info("température du ciel lue sur %s (message JSON)", self._sky_topic)

    @staticmethod
    def _on_subscribe(client, userdata, mid, reason_codes, *rest) -> None:
        """Accusé d'abonnement : un refus de droits du broker devient ainsi visible dans le journal."""
        codes = reason_codes if isinstance(reason_codes, (list, tuple)) else [reason_codes]
        refused = [code for code in codes if getattr(code, "is_failure", False) or getattr(code, "value", code) == 128]
        if refused:
            log.error("abonnement MQTT refusé (%s) : droits de l'utilisateur à vérifier", refused[0])
        else:
            log.info("abonnement MQTT accepté")

    def listen_sky(self, topic: str) -> None:
        """Suit un topic du ciel modifié à chaud, sans reconstruire le client MQTT."""
        previous, self._sky_topic = self._sky_topic, topic
        if self._client is None:
            return
        if previous and previous != topic:
            self._client.unsubscribe(previous)
        if topic:
            self._client.subscribe(topic, qos=1)
            log.info("température du ciel désormais lue sur %s", topic)

    def _on_message(self, client, userdata, message) -> None:
        """Message reçu : état du ciel (JSON) ou commande de chauffage (texte), selon le topic."""
        topic = getattr(message, "topic", None)
        if self._on_sky is not None and self._sky_topic and topic == self._sky_topic:
            try:
                self._on_sky(message.payload)
            except Exception as error:  # noqa: BLE001 - un message en échec ne doit jamais tuer le client MQTT
                log.warning("message du ciel %r non traité : %s", self._sky_topic, error)
            return
        if self._on_command is None or topic != self._command_topic:
            return
        text = message.payload.decode("utf-8", "replace")
        try:
            self._on_command(text)
        except Exception as error:  # noqa: BLE001 - une commande en échec ne doit jamais tuer le client MQTT
            log.warning("commande de chauffage %r non traitée : %s", text, error)

    @staticmethod
    def _on_disconnect(client, userdata, *args) -> None:
        log.warning("connexion MQTT perdue, nouvelle tentative automatique")

    def publish(self, name: str, value: float | str) -> None:
        """Publie une mesure (deux décimales) ou un texte, tel que le mode du chauffage."""
        topic = f"{PREFIX}/{name}"
        payload = value if isinstance(value, str) else f"{value:.2f}"
        if self._client is None:
            print(f"{topic} {payload}")
        else:
            self._pending.append(self._client.publish(topic, payload, qos=1, retain=True))
            del self._pending[:-20]  # on ne garde que les derniers messages en attente d'accusé de réception

    def close(self) -> None:
        if self._client is not None:
            deadline = time.monotonic() + 5  # laisse partir les derniers messages (5 s au plus)
            for message in self._pending:
                try:
                    message.wait_for_publish(timeout=max(deadline - time.monotonic(), 0.1))
                except Exception:  # noqa: BLE001
                    break
            self._client.on_disconnect = None  # arrêt volontaire : pas d'alerte « connexion perdue »
            self._client.loop_stop()
            self._client.disconnect()


# --------------------------------------------------------------------------- programme


def topic_names(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Topics à coller dans le champ « Pin » d'indi-allsky : ceux des capteurs activés, et ceux qui dépendent des réglages."""
    source = os.environ if env is None else env
    enabled = enabled_sensors(source)
    names: list[str] = []
    for sensor, topics in SENSOR_TOPICS.items():
        if sensor not in enabled:
            continue  # capteur non activé : son topic ne serait jamais publié
        names += topics
        if sensor == "bme280" and source.get("ALTITUDE_M", "").strip():  # avec l'altitude, la pression de station est publiée en plus
            names.insert(names.index("outdoor_pressure") + 1, "outdoor_pressure_station")
    if source.get("SKY_TEMP_TOPIC", "").strip() and "sky_temperature" not in names:
        names.append("sky_temperature")  # source MQTT : le topic est publié même sans MLX90614 local
    if source.get("HEATER_ENABLED", "0").strip() == "1":
        names.append(HEATER_TOPIC)
    return tuple(names)


def heater_test(config: HeaterConfig, percent: float, seconds: float) -> int:
    """Commande le chauffage à un rapport cyclique fixe (plafonné) pour vérifier le câblage."""
    duty = min(max(percent / 100, 0.0), config.max_duty)
    log.info(config.describe())
    log.info("essai : %.0f %% pendant %g s (demandé : %.0f %%)", duty * 100, seconds, percent)
    output = PwmOutput(config.gpio, config.pwm_hz)
    try:
        output.set(duty)
        time.sleep(seconds)
    finally:
        output.close()
    log.info("essai terminé, chauffage coupé")
    return 0


def heater_test_command(percent: float, seconds: float) -> int:
    """Essai de câblage lancé à la main : lit le fichier d'environnement, comme --heater-force.

    Sans cela, la commande lancée par SSH ne voyait ni HEATER_ENABLED ni HEATER_SUPPLY_VOLTS, que
    systemd ne fournit qu'au service.
    """
    try:
        config = HeaterConfig.from_env(cli_env())
    except ValueError as error:
        log.error("configuration du chauffage invalide : %s", error)
        return 2
    if config is None:
        log.error("HEATER_ENABLED=1 et HEATER_SUPPLY_VOLTS sont nécessaires pour l'essai du chauffage")
        return 2
    return heater_test(config, percent, seconds)


def heater_command(target: float | None, minutes: float, cancel: bool) -> int:
    """Enregistre (ou annule) une consigne de forçage pour le service en marche.

    La sortie PWM reste pilotée par le service : la consigne est un fichier qu'il relit à chaque tick
    (10 s au plus). Sans service en marche, rien ne chauffe ; et au démarrage suivant, la consigne
    est annulée (retour en régulation automatique).
    """
    env = cli_env()
    path = override_path(env)
    if cancel:
        if clear_override(path):
            log.info("consigne de forçage supprimée : retour à la régulation automatique (point de rosée + marge)")
        else:
            log.info("aucune consigne de forçage à supprimer : le chauffage est déjà en régulation automatique")
        return 0
    try:
        config = HeaterConfig.from_env(env)
    except ValueError as error:
        log.error("configuration du chauffage invalide : %s", error)
        return 2
    if config is None:
        log.error("HEATER_ENABLED=1 et HEATER_SUPPLY_VOLTS sont nécessaires pour forcer le chauffage")
        return 2
    assert target is not None  # argparse garantit une température hors --heater-cancel
    try:
        override = write_override(path, target, minutes, time.time(), config.max_dome_temp)
    except (ValueError, OSError) as error:
        log.error("consigne de forçage refusée : %s", error)
        return 2
    log.info(
        "consigne de forçage enregistrée : %g °C pendant %g min (jusqu'à %s), appliquée par le service au tick suivant",
        override.target_c,
        minutes,
        time.strftime("%H:%M", time.localtime(override.expires_at)),
    )
    log.info("suivre l'effet : journalctl -u allsky-sensors -f (ligne « … cible %g °C … (forcé) »)", override.target_c)
    return 0


def _file_mtime(path: Path) -> float | None:
    """Date de dernière modification d'un fichier (None s'il est absent ou illisible)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@dataclass
class Runtime:
    """Tout ce qui dépend de la configuration et doit être reconstruit lors d'un rechargement."""

    settings: Settings
    names: list[str]
    sensors: dict[str, object | None]
    controller: HeaterController | None
    output: object | None
    error: str | None = None  # raison pour laquelle le chauffage n'a pas de sortie (GPIO occupé…)


def build_runtime(settings: Settings, simulate: bool = False, dry_run: bool = False) -> Runtime:
    """Construit capteurs et chauffage pour une configuration donnée (rechargeable à chaud)."""
    names = ["simulation"] if simulate else list(settings.enabled)
    sensors: dict[str, object | None] = {name: Simulated(settings.altitude) if simulate else None for name in names}
    controller = output = None
    error = None
    if settings.heater is not None:
        log.info(settings.heater.describe())
        controller = HeaterController(settings.heater)
        try:
            output = NullOutput() if (dry_run or simulate) else PwmOutput(settings.heater.gpio, settings.heater.pwm_hz)
        except Exception as failure:  # noqa: BLE001 - le service continue, chauffage coupé
            controller = None
            error = f"sortie PWM impossible (gpiozero installé ? GPIO {settings.heater.gpio} libre ?) : {failure}"
    return Runtime(settings, names, sensors, controller, output, error)


def close_runtime(runtime: Runtime) -> None:
    """Coupe le chauffage et libère le GPIO (appelé avant toute reconstruction et à l'arrêt)."""
    if runtime.output is not None:
        runtime.output.close()
        runtime.output = None
    runtime.controller = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--once", action="store_true", help="une seule série de mesures, puis quitter")
    parser.add_argument("--dry-run", action="store_true", help="afficher les valeurs au lieu de les publier (GPIO inactif)")
    parser.add_argument("--simulate", action="store_true", help="valeurs simulées, sans matériel (GPIO inactif)")
    parser.add_argument("--print-topics", action="store_true", help="afficher la liste à coller dans le champ « Pin »")
    parser.add_argument(
        "--heater-test", type=float, metavar="POURCENT", help="chauffer à ce rapport cyclique (plafonné) pendant quelques secondes"
    )
    parser.add_argument("--heater-test-seconds", type=float, default=10, help="durée de l'essai (défaut : 10 s)")
    parser.add_argument(
        "--heater-force",
        type=float,
        metavar="TEMP_C",
        help="enregistre une consigne de forçage : chauffer jusqu'à cette température (°C), sans point de rosée",
    )
    parser.add_argument(
        "--heater-force-minutes",
        type=float,
        default=OVERRIDE_DEFAULT_MINUTES,
        help=f"durée de la consigne forcée ({OVERRIDE_MIN_MINUTES} à {OVERRIDE_MAX_MINUTES} min, défaut : {OVERRIDE_DEFAULT_MINUTES})",
    )
    parser.add_argument("--heater-cancel", action="store_true", help="annule la consigne forcée (retour au point de rosée + marge)")
    args = parser.parse_args()

    if args.print_topics:
        environment = cli_env()  # lancé à la main : la vérité est dans le fichier d'environnement
        prefix = environment.get("MQTT_PREFIX", PREFIX)
        print(",".join(f"{prefix}/{name}" for name in topic_names(environment)))
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.heater_test is not None:
        return heater_test_command(args.heater_test, args.heater_test_seconds)

    if args.heater_force is not None or args.heater_cancel:
        return heater_command(args.heater_force, args.heater_force_minutes, args.heater_cancel)

    env_file = configured_env_file()
    try:
        settings = Settings.from_env(service_env())
    except ConfigError as error:
        for message in error.messages:
            log.error("%s", message)
        return 2
    if settings.ds18b20_offset:
        log.info("DS18B20 : correction de %+g °C appliquée à la température du dôme", settings.ds18b20_offset)
    if settings.mlx90614_offset:
        log.info("MLX90614 : correction de %+g °C appliquée à la température du ciel", settings.mlx90614_offset)
    if settings.sky.enabled:
        log.info(
            "température du ciel lue sur %s (clé %s, fraîcheur %g s)%s",
            settings.sky.topic,
            settings.sky.key or "(message entier)",
            settings.sky.max_age,
            " ; le MLX90614 local reste prioritaire" if "mlx90614" in settings.enabled else "",
        )
    if settings.altitude is None:
        log.info("ALTITUDE_M non défini : la pression publiée est la pression de station, non ramenée au niveau de la mer")

    runtime = build_runtime(settings, simulate=args.simulate, dry_run=args.dry_run)
    if runtime.error is not None:
        log.error("%s", runtime.error)
        return 2
    forced_file = settings.state_dir / OVERRIDE_FILENAME
    if clear_override(forced_file):
        log.info("consigne de forçage trouvée au démarrage : annulée, retour en régulation automatique")
    if runtime.controller is None:
        log.info("chauffage désactivé : aucune commande de forçage ne sera acceptée (HEATER_ENABLED=1 pour l'activer)")

    def heater_on_command(text: str) -> None:
        """Commande reçue par MQTT : une consigne que la boucle applique au tick suivant."""
        heater = runtime.settings.heater  # lu à chaque appel : le chauffage a pu être activé à chaud
        if heater is None:
            log.warning("commande de chauffage ignorée (%r) : chauffage désactivé", text)
            return
        try:
            path = runtime.settings.state_dir / OVERRIDE_FILENAME
            report = apply_heater_command(text, heater, path, time.time())
        except (ValueError, OSError) as error:
            log.warning("commande de chauffage refusée (%r) : %s", text, error)
            return
        log.info("commande de chauffage reçue (%r) : %s", text, report)

    sky_feed = SkyFeed(settings.sky)

    def make_publisher(current: Runtime) -> Publisher:
        return Publisher(
            current.settings.mqtt.host,
            current.settings.mqtt.port,
            current.settings.mqtt.user,
            current.settings.mqtt.password,
            args.dry_run,
            on_command=heater_on_command if current.controller else None,
            sky_topic=current.settings.sky.topic,
            on_sky=sky_feed.handle,
        )

    publisher = make_publisher(runtime)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    systemd_notify(b"READY=1")

    readings: dict[str, tuple[float, float]] = {}  # topic -> (valeur, instant de la mesure)
    last_publish: float | None = None
    duty = 0.0
    override: HeaterOverride | None = None
    warn = WarnOnce()
    unavailable: set[str] = set()
    settings_mtime = _file_mtime(env_file) if env_file is not None else None
    config_error: str | None = None
    try:
        while not stop.is_set():
            now = time.monotonic()

            if env_file is not None and _file_mtime(env_file) != settings_mtime:
                settings_mtime = _file_mtime(env_file)  # mémorisé même si le fichier est refusé
                try:
                    reloaded = Settings.from_env(service_env())
                except (ConfigError, OSError) as error:
                    messages = list(error.messages) if isinstance(error, ConfigError) else [str(error)]
                    for message in messages:
                        log.error("configuration non appliquée : %s", message)
                    config_error = " ; ".join(messages)  # visible dans status.json et donc sur le web
                else:
                    config_error = None
                    previous_sky = runtime.settings.sky
                    restart_mqtt = reloaded.mqtt != runtime.settings.mqtt
                    close_runtime(runtime)  # coupe le chauffage et libère le GPIO avant de le redemander
                    runtime = build_runtime(reloaded, simulate=args.simulate, dry_run=args.dry_run)
                    unavailable.clear()
                    sky_feed.configure(runtime.settings.sky)  # la clé du JSON peut avoir changé
                    if runtime.error is not None:
                        log.error("chauffage indisponible : %s", runtime.error)
                    if restart_mqtt:
                        publisher.close()
                        publisher = make_publisher(runtime)
                    elif runtime.settings.sky.topic != previous_sky.topic:
                        publisher.listen_sky(runtime.settings.sky.topic)
                    log.info("configuration rechargée depuis %s", env_file)

            current: dict[str, float] = {}
            for name in runtime.names:
                if runtime.sensors[name] is None:  # première fois, ou échec précédent : on réessaie à chaque cycle
                    try:
                        runtime.sensors[name] = build_sensor(name, runtime.settings.altitude)
                    except Exception as error:  # noqa: BLE001 - un capteur absent ne doit pas arrêter les autres
                        warn(f"build-{name}", "capteur %s indisponible : %s", name, error)
                        unavailable.add(name)
                        continue
                if name in unavailable:
                    unavailable.discard(name)
                    log.info("capteur %s disponible", name)
                try:
                    current.update(runtime.sensors[name].read())
                except Exception as error:  # noqa: BLE001
                    warn(f"read-{name}", "lecture de %s impossible : %s", name, error)
                    runtime.sensors[name] = None
            for topic, value in current.items():
                readings[topic] = (value, now)
            sky = sky_measurement(sky_feed, runtime.settings.sky, current, now)
            if sky is not None:
                readings["sky_temperature"] = sky  # publié comme les capteurs locaux
            elif "sky_temperature" not in current:
                readings.pop("sky_temperature", None)  # source périmée : ne pas republier une vieille valeur

            override = None
            if runtime.controller and runtime.output:
                heater = runtime.settings.heater
                assert heater is not None
                override, ignored = load_override(runtime.settings.state_dir / OVERRIDE_FILENAME, time.time(), heater.max_dome_temp)
                if ignored:
                    warn(f"override-{ignored}", "forçage ignoré : %s", ignored)
                duty = runtime.controller.update(
                    current.get("dome_temperature"),
                    current.get("outdoor_dew_point"),
                    now,
                    target=override.target_c if override else None,
                )
                runtime.output.set(duty)
                # état du chauffage publié à chaque tick (10 s) : une interface de pilotage doit être réactive
                publisher.publish(HEATER_TOPIC, duty * 100)
                publisher.publish(HEATER_MODE_TOPIC, "force" if override else "auto")
                if runtime.controller.setpoint is not None:
                    publisher.publish(HEATER_TARGET_TOPIC, runtime.controller.setpoint)
            else:
                duty = 0.0

            if last_publish is None or now - last_publish >= runtime.settings.interval:
                for topic, (value, measured_at) in readings.items():
                    if now - measured_at <= MAX_AGE_SECONDS:
                        publisher.publish(topic, value)
                if runtime.controller:
                    setpoint = runtime.controller.setpoint
                    dome, dew = current.get("dome_temperature"), current.get("outdoor_dew_point")
                    suffix = " (forcé)" if override else ""
                    if dome is not None and setpoint is not None and dew is not None:
                        log.info(
                            "dôme %.1f °C, rosée %.1f °C, cible %.1f °C, chauffage %.0f %%%s",
                            dome,
                            dew,
                            setpoint,
                            duty * 100,
                            suffix,
                        )
                    elif dome is not None and setpoint is not None:
                        log.info("dôme %.1f °C, cible %.1f °C, chauffage %.0f %%%s", dome, setpoint, duty * 100, suffix)
                last_publish = now

            setpoint = runtime.controller.setpoint if runtime.controller else None
            status_file = runtime.settings.state_dir / STATUS_FILENAME
            try:
                write_status(
                    status_file,
                    {
                        "updated_at": time.time(),
                        "mode": "force" if override else "auto",
                        "target_c": setpoint,
                        "override_target_c": override.target_c if override else None,
                        "override_expires_at": override.expires_at if override else None,
                        "dome_c": current.get("dome_temperature"),
                        "dew_c": current.get("outdoor_dew_point"),
                        "duty_pct": duty * 100,
                        "readings": {topic: value for topic, (value, _at) in readings.items()},
                        "sensors_unavailable": sorted(unavailable),
                        "config_error": config_error,
                        "settings_mtime": settings_mtime,
                        "pid": os.getpid(),
                    },
                )
            except OSError as error:
                warn(f"status-{error}", "état non publié (%s) : %s", status_file, error)

            systemd_notify(b"WATCHDOG=1")
            if args.once:
                break
            stop.wait(HEATER_TICK_SECONDS if runtime.controller else min(runtime.settings.interval, MAX_TICK_SECONDS))
    finally:
        close_runtime(runtime)  # le chauffage est toujours coupé en quittant
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
