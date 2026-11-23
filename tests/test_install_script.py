"""Tests du script d'installation `install.sh` : structure, syntaxe et rendu du `.env`."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import allsky_sensors_mqtt as sensors

RACINE = Path(__file__).resolve().parent.parent
SCRIPT = RACINE / "install.sh"


def _bash():
    """bash disponible (PATH, ou Git for Windows sur Windows)."""
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        if candidate.is_file():
            return str(candidate)
    return None


BASH = _bash()


def test_script_existe_et_est_en_lf():
    """Un shebang ou une ligne CRLF empêcherait le script de tourner sur la Pi."""
    data = SCRIPT.read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r\n" not in data


def test_invariants_de_securite():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "umask 077" in text
    assert "install -m 600" in text
    assert "validate_env" in text  # la configuration est validée avant d'être installée
    assert "change-me" not in text  # aucun mot de passe d'exemple en dur
    for variable in ("MQTT_HOST", "HEATER_SUPPLY_VOLTS", "WEB_PASSWORD", "SKY_TEMP_TOPIC"):
        assert variable in text


def test_installateur_et_programme_partagent_la_version():
    """Une release publie le même numéro dans le script, le programme et l'interface."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert f'SCRIPT_VERSION="{sensors.VERSION}"' in text


@pytest.mark.skipif(BASH is None, reason="bash absent")
def test_syntaxe_bash():
    result = subprocess.run([BASH, "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


RENDU = """\
set -euo pipefail
source "$1"
MQTT_HOST=broker.local
MQTT_PORT=1883
MQTT_USER=allsky-pi
MQTT_PASSWORD=mqtt-secret
MQTT_PREFIX=allsky
ENABLED=bme280,ds18b20
BME280_ADDRESS=0x76
ALTITUDE_M=500
DS18B20_ID=
DS18B20_OFFSET_C=3
MLX90614_ADDRESS=0x5A
MLX90614_OFFSET_C=0
INTERVAL=60
SKY_TEMP_TOPIC=sqmeter/state
SKY_TEMP_KEY=infrared.skyTemperature
SKY_STALE_KEY=dataStale
SKY_STATUS_KEY=infrared.status
SKY_MAX_AGE=600
SKY_ANTICIPATION_C=2
SKY_CLEAR_DELTA_C=-15
SKY_CLEAR_HYSTERESIS_C=2
HEATER_ENABLED="$2"
HEATER_SUPPLY_VOLTS=12
HEATER_GPIO=23
GPIO_RESERVED=17,18,22,27
HEATER_PWM_HZ=100
HEATER_RESISTORS=6
HEATER_RESISTOR_OHMS=47
HEATER_RESISTOR_RATING_WATTS=1
HEATER_MARGIN=4
HEATER_MAX_DOME_TEMP=35
WEB_BIND=0.0.0.0
WEB_PORT=8080
WEB_USER=admin
WEB_PASSWORD=mot-de-passe-de-test
render_env "$3"
"""


@pytest.mark.skipif(BASH is None, reason="bash absent")
@pytest.mark.parametrize(("heater", "supply_present"), [("1", True), ("0", False)])
def test_rendu_du_fichier_env(tmp_path, heater, supply_present):
    runner = tmp_path / "rendu.sh"
    runner.write_text(RENDU, encoding="utf-8", newline="\n")
    env_file = tmp_path / "allsky-sensors.env"
    result = subprocess.run(
        [BASH, str(runner), SCRIPT.as_posix(), heater, env_file.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "MQTT_HOST=broker.local" in text
    assert "SKY_TEMP_TOPIC=sqmeter/state" in text
    assert "SKY_ANTICIPATION_C=2" in text
    assert "WEB_PASSWORD=mot-de-passe-de-test" in text
    assert ("HEATER_SUPPLY_VOLTS=12" in text) is supply_present
    env = sensors.read_env_file(env_file)
    assert sensors.validate_env(env) == []
