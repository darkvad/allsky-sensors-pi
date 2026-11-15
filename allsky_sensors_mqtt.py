#!/usr/bin/env python3
"""Capteurs et chauffage anti-buée de l'allsky, publiés sur MQTT, pour la Raspberry Pi.

Capteurs :
  - BME280 (I2C)     : température, humidité et pression extérieures ; point de rosée calculé
  - DS18B20 (1-Wire) : température dans le dôme
  - MLX90614 (I2C)   : température du ciel, lue par un thermomètre infrarouge sans contact
Chauffage anti-buée (facultatif, HEATER_ENABLED=1) :
  - commande PWM d'un module MOSFET alimentant des résistances ; régulation de la température du dôme
    à (point de rosée + marge) ; le rapport cyclique appliqué est publié (dome_heater_pwm, en %).

Chaque valeur est publiée en texte brut (un nombre par topic) pour le capteur « MQTT Broker »
d'indi-allsky. Configuration par variables d'environnement (voir allsky-sensors.env.example).

Essais sans matériel ni broker :  python3 allsky_sensors_mqtt.py --simulate --dry-run --once
"""

from __future__ import annotations

import argparse
import logging
import math
import os
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
W1_DEVICES = Path("/sys/bus/w1/devices")

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
        if env.get("HEATER_ENABLED", "0") != "1":
            return None
        if "HEATER_SUPPLY_VOLTS" not in env:
            raise ValueError("HEATER_SUPPLY_VOLTS doit être défini : tension d'alimentation du chauffage, en volts")
        config = cls(
            volts=float(env["HEATER_SUPPLY_VOLTS"]),
            gpio=int(env.get("HEATER_GPIO", "23")),
            reserved=tuple(int(n) for n in env.get("GPIO_RESERVED", "").split(",") if n.strip()),
            pwm_hz=int(env.get("HEATER_PWM_HZ", "100")),
            resistors=int(env.get("HEATER_RESISTORS", "6")),
            ohms=float(env.get("HEATER_RESISTOR_OHMS", "47")),
            rating_watts=float(env.get("HEATER_RESISTOR_RATING_WATTS", "1")),
            margin=float(env.get("HEATER_MARGIN", "4")),
            max_dome_temp=float(env.get("HEATER_MAX_DOME_TEMP", "35")),
            kp=float(env.get("HEATER_KP", "0.3")),
            ki=float(env.get("HEATER_KI", "0.0002")),
        )
        config.validate()
        return config


class HeaterController:
    """Régulateur PI : amène la température du dôme à (point de rosée + marge).

    La sortie est un rapport cyclique entre 0 et ``config.max_duty``. Toute mesure indispensable
    manquante depuis plus de STALE_SECONDS, ou un dôme trop chaud, coupe le chauffage.
    """

    def __init__(self, config: HeaterConfig) -> None:
        self.config = config
        self._integral = 0.0
        self._last_time: float | None = None
        self._dome: float | None = None
        self._dew: float | None = None
        self._dome_at: float | None = None
        self._dew_at: float | None = None

    @staticmethod
    def _stale(at: float | None, now: float) -> bool:
        return at is None or now - at > STALE_SECONDS

    def update(self, dome: float | None, dew: float | None, now: float) -> float:
        if dome is not None:
            self._dome, self._dome_at = dome, now
        if dew is not None:
            self._dew, self._dew_at = dew, now
        dt = 0.0 if self._last_time is None else min(max(now - self._last_time, 0.0), 60.0)
        self._last_time = now

        if self._stale(self._dome_at, now) or self._stale(self._dew_at, now):
            self._integral = 0.0
            return 0.0  # pas de mesure fiable : pas de chauffage
        assert self._dome is not None and self._dew is not None
        if self._dome >= self.config.max_dome_temp:
            self._integral = 0.0
            return 0.0

        error = self._dew + self.config.margin - self._dome
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


def enabled_sensors() -> list[str]:
    """Capteurs à lire (variable ENABLED, « bme280,ds18b20 » par défaut)."""
    return [name.strip() for name in os.getenv("ENABLED", DEFAULT_SENSORS).split(",") if name.strip()]


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


# --------------------------------------------------------------------------- MQTT


class Publisher:
    def __init__(self, host: str, port: int, user: str, password: str, dry_run: bool) -> None:
        self._client = None
        self._pending: list = []
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
        self._client.on_disconnect = self._on_disconnect
        self._client.max_queued_messages_set(200)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        # connect_async : le programme démarre même si le broker n'est pas encore joignable.
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    @staticmethod
    def _on_connect(client, userdata, flags, reason_code, *rest) -> None:
        # Compatible paho-mqtt 1.x (code entier) et 2.x (objet ReasonCode).
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.warning("connexion MQTT refusée : %s (identifiants ? adresse ? droits du broker ?)", reason_code)
        else:
            log.info("connecté au broker MQTT")

    @staticmethod
    def _on_disconnect(client, userdata, *args) -> None:
        log.warning("connexion MQTT perdue, nouvelle tentative automatique")

    def publish(self, name: str, value: float) -> None:
        topic, payload = f"{PREFIX}/{name}", f"{value:.2f}"
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


def topic_names() -> tuple[str, ...]:
    """Topics à coller dans le champ « Pin » d'indi-allsky : ceux des capteurs activés, et ceux qui dépendent des réglages."""
    enabled = enabled_sensors()
    names: list[str] = []
    for sensor, topics in SENSOR_TOPICS.items():
        if sensor not in enabled:
            continue  # capteur non activé : son topic ne serait jamais publié
        names += topics
        if sensor == "bme280" and os.getenv("ALTITUDE_M", "").strip():  # avec l'altitude, la pression de station est publiée en plus
            names.insert(names.index("outdoor_pressure") + 1, "outdoor_pressure_station")
    if os.getenv("HEATER_ENABLED", "0") == "1":
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
    args = parser.parse_args()

    if args.print_topics:
        print(",".join(f"{PREFIX}/{name}" for name in topic_names()))
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        heater_config = HeaterConfig.from_env()
    except ValueError as error:
        log.error("configuration du chauffage invalide : %s", error)
        return 2

    if args.heater_test is not None:
        if heater_config is None:
            log.error("HEATER_ENABLED=1 et HEATER_SUPPLY_VOLTS sont nécessaires pour l'essai du chauffage")
            return 2
        return heater_test(heater_config, args.heater_test, args.heater_test_seconds)

    try:
        offset = ds18b20_offset()
    except ValueError as error:
        log.error("DS18B20_OFFSET_C invalide : %s", error)
        return 2
    if offset:
        log.info("DS18B20 : correction de %+g °C appliquée à la température du dôme", offset)

    try:
        sky_offset = mlx90614_offset()
    except ValueError as error:
        log.error("MLX90614_OFFSET_C invalide : %s", error)
        return 2
    if sky_offset:
        log.info("MLX90614 : correction de %+g °C appliquée à la température du ciel", sky_offset)

    interval = int(os.getenv("INTERVAL", "60"))
    names = ["simulation"] if args.simulate else enabled_sensors()
    altitude_text = os.getenv("ALTITUDE_M", "").strip()
    try:
        altitude = float(altitude_text) if altitude_text else None
        if altitude is not None and not -500 <= altitude <= 9000:
            raise ValueError
    except ValueError:
        log.error("ALTITUDE_M invalide : %r (altitude du capteur en mètres, entre -500 et 9000)", altitude_text)
        return 2
    if altitude is None:
        log.info("ALTITUDE_M non défini : la pression publiée est la pression de station, non ramenée au niveau de la mer")
    sensors: dict[str, object | None] = {name: Simulated(altitude) if args.simulate else None for name in names}

    controller = output = None
    if heater_config is not None:
        log.info(heater_config.describe())
        controller = HeaterController(heater_config)
        try:
            output = NullOutput() if (args.dry_run or args.simulate) else PwmOutput(heater_config.gpio, heater_config.pwm_hz)
        except Exception as error:  # noqa: BLE001
            log.error("sortie PWM impossible (gpiozero installé ? GPIO %s libre ?) : %s", heater_config.gpio, error)
            return 2
    tick = HEATER_TICK_SECONDS if controller else min(interval, MAX_TICK_SECONDS)

    publisher = Publisher(
        os.getenv("MQTT_HOST", "localhost"),
        int(os.getenv("MQTT_PORT", "1883")),
        os.getenv("MQTT_USER", ""),
        os.getenv("MQTT_PASSWORD", ""),
        args.dry_run,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    systemd_notify(b"READY=1")

    readings: dict[str, tuple[float, float]] = {}  # topic -> (valeur, instant de la mesure)
    last_publish: float | None = None
    duty = 0.0
    warn = WarnOnce()
    unavailable: set[str] = set()
    try:
        while not stop.is_set():
            now = time.monotonic()
            current: dict[str, float] = {}
            for name in names:
                if sensors[name] is None:  # première fois, ou échec précédent : on réessaie à chaque cycle
                    try:
                        sensors[name] = build_sensor(name, altitude)
                    except Exception as error:  # noqa: BLE001 - un capteur absent ne doit pas arrêter les autres
                        warn(f"build-{name}", "capteur %s indisponible : %s", name, error)
                        unavailable.add(name)
                        continue
                if name in unavailable:
                    unavailable.discard(name)
                    log.info("capteur %s disponible", name)
                try:
                    current.update(sensors[name].read())
                except Exception as error:  # noqa: BLE001
                    warn(f"read-{name}", "lecture de %s impossible : %s", name, error)
                    sensors[name] = None
            for topic, value in current.items():
                readings[topic] = (value, now)

            if controller and output:
                duty = controller.update(current.get("dome_temperature"), current.get("outdoor_dew_point"), now)
                output.set(duty)

            if last_publish is None or now - last_publish >= interval:
                for topic, (value, measured_at) in readings.items():
                    if now - measured_at <= MAX_AGE_SECONDS:
                        publisher.publish(topic, value)
                if controller:
                    publisher.publish(HEATER_TOPIC, duty * 100)
                    dome, dew = current.get("dome_temperature"), current.get("outdoor_dew_point")
                    if dome is not None and dew is not None:
                        log.info(
                            "dôme %.1f °C, rosée %.1f °C, cible %.1f °C, chauffage %.0f %%",
                            dome,
                            dew,
                            dew + heater_config.margin,
                            duty * 100,
                        )
                last_publish = now

            systemd_notify(b"WATCHDOG=1")
            if args.once:
                break
            stop.wait(tick)
    finally:
        if output:
            output.close()  # le chauffage est toujours coupé en quittant
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
