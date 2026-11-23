import json
import os
import socket
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

import allsky_sensors_mqtt as sensors
from allsky_sensors_mqtt import HeaterConfig, HeaterController

W1_OK = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


def test_dew_point():
    assert sensors.dew_point(20, 50) == pytest.approx(9.26, abs=0.01)
    assert sensors.dew_point(5, 100) == pytest.approx(5.0, abs=0.01)
    assert sensors.dew_point(20, 0) is None
    assert sensors.dew_point(20, 120) is None


def test_ds18b20_parsing_rejects_invalid_readings():
    assert sensors.parse_w1_slave(W1_OK) == 23.125
    assert sensors.parse_w1_slave(W1_OK.replace("YES", "NO")) is None
    assert sensors.parse_w1_slave(W1_OK.replace("t=23125", "t=85000")) is None  # mise sous tension
    assert sensors.parse_w1_slave(W1_OK.replace("t=23125", "t=-127000")) is None
    assert sensors.parse_w1_slave("") is None


# ----------------------------------------------------------------------------- configuration


def test_power_limit_depends_on_supply_voltage():
    twelve = HeaterConfig(volts=12)
    assert twelve.total_ohms == pytest.approx(7.83, abs=0.01)
    assert twelve.watts_per_resistor_full == pytest.approx(3.06, abs=0.01)  # 3 fois la puissance nominale de 1 W
    assert twelve.max_duty == pytest.approx(0.163, abs=0.001)
    assert twelve.max_duty * twelve.watts_per_resistor_full == pytest.approx(0.5)  # 0,5 W par résistance au plus

    five = HeaterConfig(volts=5)
    assert five.watts_per_resistor_full == pytest.approx(0.53, abs=0.01)
    assert five.max_duty == pytest.approx(0.94, abs=0.01)
    assert five.watts_full == pytest.approx(3.19, abs=0.01)  # 6 résistances
    assert five.watts_full * five.max_duty == pytest.approx(3.0, abs=0.01)
    assert (5 / five.total_ohms) == pytest.approx(0.64, abs=0.01)  # courant à 100 %


def test_five_resistors_at_5_volts():
    config = HeaterConfig(volts=5, resistors=5)
    assert config.total_ohms == pytest.approx(9.4)
    assert config.watts_full == pytest.approx(2.66, abs=0.01)
    assert config.max_duty == pytest.approx(0.94, abs=0.01)  # le plafond ne dépend que de la résistance unitaire
    assert config.watts_full * config.max_duty == pytest.approx(2.5, abs=0.01)


@pytest.mark.parametrize("value", ["true", "oui", "yes", "2", ""])
def test_unknown_heater_flag_is_refused(value):
    """Un « HEATER_ENABLED=true » désactivait le chauffage en silence : c'est maintenant une erreur."""
    with pytest.raises(ValueError, match="HEATER_ENABLED"):
        HeaterConfig.from_env({"HEATER_ENABLED": value})


def test_config_from_env():
    assert HeaterConfig.from_env({}) is None
    assert HeaterConfig.from_env({"HEATER_ENABLED": "0", "HEATER_SUPPLY_VOLTS": "12"}) is None
    with pytest.raises(ValueError, match="HEATER_SUPPLY_VOLTS"):
        HeaterConfig.from_env({"HEATER_ENABLED": "1"})  # pas de tension : refus
    config = HeaterConfig.from_env({"HEATER_ENABLED": "1", "HEATER_SUPPLY_VOLTS": "12", "HEATER_RESISTORS": "5"})
    assert config.resistors == 5 and config.gpio == 23 and config.margin == 4.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"HEATER_SUPPLY_VOLTS": "0"},
        {"HEATER_SUPPLY_VOLTS": "48"},
        {"HEATER_GPIO": "4"},  # 1-Wire
        {"HEATER_GPIO": "2"},  # I2C
        {"HEATER_GPIO": "40"},
        {"HEATER_GPIO": "18", "GPIO_RESERVED": "17,18,22,27"},  # pris par le focuser
        {"HEATER_GPIO": "17", "GPIO_RESERVED": "17, 18, 22, 27"},
        {"HEATER_MARGIN": "15"},
        {"HEATER_MAX_DOME_TEMP": "60"},
        {"HEATER_RESISTORS": "0"},
        {"HEATER_KP": "-1"},
    ],
)
def test_invalid_config_is_refused(overrides):
    env = {"HEATER_ENABLED": "1", "HEATER_SUPPLY_VOLTS": "12", **overrides}
    with pytest.raises(ValueError):
        HeaterConfig.from_env(env)


# ----------------------------------------------------------------------------- régulation


def make(volts=12.0, **kwargs):
    return HeaterController(HeaterConfig(volts=volts, **kwargs))


def test_no_heating_when_dome_is_warm_enough():
    controller = make()
    assert controller.update(dome=12.0, dew=4.0, now=0) == 0.0  # cible 8 °C


def test_heats_when_dome_is_below_target_and_never_exceeds_cap():
    for volts in (5, 12, 24):
        controller = make(volts=volts)
        duties = [controller.update(dome=0.0, dew=5.0, now=t * 10) for t in range(500)]
        assert max(duties) <= controller.config.max_duty + 1e-9
        assert duties[-1] == pytest.approx(controller.config.max_duty)  # écart énorme : plein régime autorisé


def test_fail_safe_when_a_measurement_is_missing_or_stale():
    controller = make()
    assert controller.update(dome=2.0, dew=5.0, now=0) > 0
    assert controller.update(dome=None, dew=5.0, now=60) > 0  # trou bref : on garde la dernière valeur
    assert controller.update(dome=None, dew=5.0, now=130) == 0.0  # plus de mesure du dôme depuis > 120 s
    assert controller.update(dome=2.0, dew=None, now=140) > 0  # point de rosée absent depuis 10 s seulement : toléré
    assert controller.update(dome=2.0, dew=None, now=260) == 0.0  # absent depuis plus de 120 s : coupure
    assert controller.update(dome=2.0, dew=5.0, now=270) > 0  # tout est revenu


def test_no_heating_without_any_measurement():
    assert make().update(dome=None, dew=None, now=0) == 0.0


def test_overtemperature_cuts_the_heater():
    controller = make(max_dome_temp=35)
    assert controller.update(dome=36.0, dew=30.0, now=0) == 0.0  # cible 34 °C mais dôme déjà trop chaud
    assert controller.update(dome=34.0, dew=30.0, now=10) == 0.0  # écart nul : pas de chauffe


def test_no_integral_windup_after_a_long_saturation():
    controller = make(volts=5)
    for t in range(0, 7200, 10):  # 2 h avec un dôme bien trop froid : sortie saturée
        controller.update(dome=-10.0, dew=5.0, now=t)
    assert controller.update(dome=20.0, dew=5.0, now=7210) == 0.0  # dôme chaud : coupure immédiate
    assert controller.update(dome=8.0, dew=5.0, now=7220) < controller.config.max_duty * 0.5  # et pas de surcharge d'intégrale


def closed_loop(volts, gain, tau1, tau2, delay=60.0, hours=6, t_out=5.0, dew=4.2):
    """Dôme simulé : résistance (tau1) -> air du dôme (tau2), avec un retard de transport."""
    controller = make(volts=volts)
    full = controller.config.watts_full
    dt, ambient = 10.0, t_out + 1.0
    mass = dome = ambient
    pipe = deque([0.0] * int(delay / dt))
    peak = 0.0
    for step in range(int(hours * 3600 / dt)):
        duty = controller.update(dome, dew, step * dt)
        pipe.append(duty * full)
        mass += dt * (ambient - mass + gain * pipe.popleft()) / tau1
        dome += dt * (ambient - dome + (mass - ambient)) / tau2
        peak = max(peak, dome)
    return dome - (dew + controller.config.margin), peak - (dew + controller.config.margin)


@pytest.mark.parametrize("volts", [5, 12])
@pytest.mark.parametrize(("gain", "tau1", "tau2"), [(1.0, 120, 600), (2.0, 300, 900), (4.0, 120, 1200)])
def test_closed_loop_reaches_target_without_overshoot(volts, gain, tau1, tau2):
    final_error, overshoot = closed_loop(volts, gain, tau1, tau2)
    assert abs(final_error) < 0.3
    assert overshoot < 0.5


def test_closed_loop_with_a_too_weak_heater_stays_safe():
    final_error, overshoot = closed_loop(12, gain=0.5, tau1=120, tau2=600)
    assert final_error < 0  # n'atteint pas la cible, sans emballement
    assert overshoot <= 0


# ----------------------------------------------------------------------------- sorties et divers


def test_systemd_watchdog_notification(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    sensors.systemd_notify(b"WATCHDOG=1")
    assert server.recv(64) == b"WATCHDOG=1"
    server.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    sensors.systemd_notify(b"WATCHDOG=1")  # sans systemd : sans effet, sans erreur


@pytest.fixture()
def mock_gpio():
    pytest.importorskip("gpiozero")
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory, MockPWMPin

    previous = Device.pin_factory
    Device.pin_factory = MockFactory(pin_class=MockPWMPin)
    yield Device.pin_factory
    Device.pin_factory.reset()
    Device.pin_factory = previous


def test_pwm_output_sets_and_cuts_the_duty_cycle(mock_gpio):
    output = sensors.PwmOutput(23, 100)
    output.set(0.4)
    assert mock_gpio.pin(23).state == pytest.approx(0.4)
    output.set(7.0)  # valeur aberrante : bornée à 100 %
    assert mock_gpio.pin(23).state == 1.0
    output.close()
    assert mock_gpio.pin(23).state == 0


def test_heater_test_is_capped_and_always_switched_off(mock_gpio, monkeypatch):
    seen = []
    monkeypatch.setattr(sensors.time, "sleep", lambda _s: seen.append(mock_gpio.pin(23).state))
    config = HeaterConfig(volts=12)
    assert sensors.heater_test(config, percent=100, seconds=1) == 0
    assert seen == [pytest.approx(config.max_duty)]  # 100 % demandé, plafonné
    assert mock_gpio.pin(23).state == 0


def run_main(monkeypatch, capsys, args, env):
    env = {"ENV_FILE": "", **env}  # par défaut, aucun fichier réel : tests reproductibles partout
    prefixes = (
        "HEATER_",
        "MQTT_",
        "ALTITUDE_",
        "MLX90614_",
        "DS18B20_",
        "BME280_",
        "ENABLED",
        "STATE_DIR",
        "ENV_FILE",
        "WEB_",
        "SKY_",
    )
    for key in [k for k in os.environ if k.startswith(prefixes)]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["allsky_sensors_mqtt.py", *args])
    code = sensors.main()
    return code, capsys.readouterr().out


def test_topic_list_includes_the_heater_only_when_enabled(monkeypatch, capsys):
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {})
    assert "dome_heater_pwm" not in out
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {"HEATER_ENABLED": "1"})
    assert out.strip().endswith("allsky/dome_heater_pwm")


def test_heater_without_voltage_refuses_to_start(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"HEATER_ENABLED": "1"})
    assert code == 2


def test_simulation_publishes_the_pwm_value_without_touching_gpio(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"HEATER_ENABLED": "1", "HEATER_SUPPLY_VOLTS": "12"})
    assert code == 0
    line = next(line for line in out.splitlines() if line.startswith("allsky/dome_heater_pwm "))
    assert 0 <= float(line.split()[1]) <= 100


def test_heater_gpio_defaults_to_23_and_accepts_a_free_pin_next_to_reserved_ones():
    env = {"HEATER_ENABLED": "1", "HEATER_SUPPLY_VOLTS": "5", "GPIO_RESERVED": "17,18,22,27"}
    assert HeaterConfig.from_env(env).gpio == 23
    assert HeaterConfig.from_env({**env, "HEATER_GPIO": "24"}).gpio == 24


def test_ds18b20_explains_why_it_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sensors, "W1_DEVICES", tmp_path / "absent")  # 1-Wire non activé : le dossier n'existe pas
    with pytest.raises(RuntimeError, match="raspi-config"):
        sensors.Ds18b20(None)

    bus = tmp_path / "w1"
    (bus / "w1_bus_master1").mkdir(parents=True)  # 1-Wire actif, aucun capteur
    monkeypatch.setattr(sensors, "W1_DEVICES", bus)
    with pytest.raises(RuntimeError, match="câblage"):
        sensors.Ds18b20(None)

    device = bus / "28-0123456789ab"
    device.mkdir()
    (device / "w1_slave").write_text(W1_OK)
    assert sensors.Ds18b20(None).read() == {"dome_temperature": 23.125}


def test_repeated_warnings_are_throttled(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(sensors.time, "monotonic", lambda: now[0])
    warn = sensors.WarnOnce(every=600)
    with caplog.at_level("WARNING", logger="allsky-sensors"):
        warn("a", "capteur %s indisponible", "x")
        now[0] += 10
        warn("a", "capteur %s indisponible", "x")  # trop tôt : ignoré
        warn("b", "autre cause")  # autre cause : affichée
        now[0] += 700
        warn("a", "capteur %s indisponible", "x")  # 10 minutes plus tard : affichée à nouveau
    assert [r.getMessage() for r in caplog.records] == ["capteur x indisponible", "autre cause", "capteur x indisponible"]


def test_sea_level_pressure():
    # atmosphère standard à 500 m : 954,6 hPa et 11,75 °C correspondent à 1013,25 hPa au niveau de la mer
    assert sensors.sea_level_pressure(954.61, 11.75, 500) == pytest.approx(1013.25, abs=0.5)
    assert sensors.sea_level_pressure(962.7, 23.1, 0) == pytest.approx(962.7)
    assert 1015 < sensors.sea_level_pressure(962.7, 23.1, 500) < 1024


def make_bme(altitude):
    from types import SimpleNamespace

    sensor = sensors.Bme280.__new__(sensors.Bme280)  # sans matériel : on court-circuite l'initialisation
    sensor._device = SimpleNamespace(temperature=23.1, relative_humidity=51.0, pressure=962.7)
    sensor._altitude = altitude
    return sensor


def test_bme280_publishes_station_pressure_only_without_altitude():
    values = make_bme(None).read()
    assert values["outdoor_pressure"] == 962.7
    assert "outdoor_pressure_station" not in values


def test_bme280_publishes_both_pressures_with_altitude():
    values = make_bme(500.0).read()
    assert values["outdoor_pressure_station"] == 962.7
    assert 1015 < values["outdoor_pressure"] < 1024
    assert values["outdoor_dew_point"] == pytest.approx(sensors.dew_point(23.1, 51.0))


def test_topic_list_follows_the_altitude_setting(monkeypatch, capsys):
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {})
    assert "outdoor_pressure_station" not in out
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {"ALTITUDE_M": "500"})
    names = [t.split("/")[1] for t in out.strip().split(",")]
    assert names[names.index("outdoor_pressure") + 1] == "outdoor_pressure_station"


@pytest.mark.parametrize("value", ["abc", "20000", "-900"])
def test_invalid_altitude_is_refused(monkeypatch, capsys, value):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"ALTITUDE_M": value})
    assert code == 2


def test_simulation_with_altitude_prints_both_pressures(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"ALTITUDE_M": "500"})
    assert code == 0
    pressures = {line.split()[0]: float(line.split()[1]) for line in out.splitlines() if "pressure" in line}
    assert pressures["allsky/outdoor_pressure"] > pressures["allsky/outdoor_pressure_station"] + 40


# ----------------------------------------------------------------------------- température du ciel (MLX90614)


def make_mlx(offset=0.0):
    from types import SimpleNamespace

    sensor = sensors.Mlx90614.__new__(sensors.Mlx90614)  # sans matériel : on court-circuite l'initialisation
    sensor._device = SimpleNamespace(object_temperature=-12.34)
    sensor._offset = offset
    return sensor


def test_mlx90614_publishes_the_sky_temperature():
    assert make_mlx().read() == {"sky_temperature": pytest.approx(-12.34)}
    assert make_mlx(offset=2.5).read() == {"sky_temperature": pytest.approx(-9.84)}


def test_mlx90614_discards_implausible_readings():
    sensor = make_mlx()
    sensor._device.object_temperature = -273.15  # capteur non alimenté
    assert sensor.read() == {}
    sensor._device.object_temperature = 382.19  # erreur de bus
    assert sensor.read() == {}


def test_build_sensor_uses_the_configured_mlx90614_address(monkeypatch):
    built = []
    monkeypatch.setattr(sensors, "Mlx90614", lambda address, offset: built.append((address, offset)))
    monkeypatch.delenv("MLX90614_ADDRESS", raising=False)
    monkeypatch.delenv("MLX90614_OFFSET_C", raising=False)
    sensors.build_sensor("mlx90614")
    monkeypatch.setenv("MLX90614_ADDRESS", "0x5B")
    monkeypatch.setenv("MLX90614_OFFSET_C", "-2")
    sensors.build_sensor("mlx90614")
    assert built == [(0x5A, 0.0), (0x5B, -2.0)]


def test_enabled_sensors_ignores_blank_entries(monkeypatch):
    monkeypatch.setenv("ENABLED", " bme280 , mlx90614 ,,")
    assert sensors.enabled_sensors() == ["bme280", "mlx90614"]
    monkeypatch.delenv("ENABLED")
    assert sensors.enabled_sensors() == ["bme280", "ds18b20"]


def test_temp_offsets_are_optional_and_bounded(monkeypatch):
    for variable in ("DS18B20_OFFSET_C", "MLX90614_OFFSET_C"):
        monkeypatch.delenv(variable, raising=False)
    assert sensors.ds18b20_offset() == 0.0
    assert sensors.mlx90614_offset() == 0.0
    monkeypatch.setenv("MLX90614_OFFSET_C", "-3.5")
    assert sensors.mlx90614_offset() == -3.5
    monkeypatch.setenv("DS18B20_OFFSET_C", "11")
    with pytest.raises(ValueError, match="entre -10 et 10"):
        sensors.ds18b20_offset()


@pytest.mark.parametrize("value", ["abc", "25", "-20.1"])
def test_invalid_mlx90614_offset_is_refused_by_the_program(monkeypatch, capsys, value):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"MLX90614_OFFSET_C": value})
    assert code == 2  # le programme refuse de démarrer : le journal dit quoi corriger


def test_topic_list_follows_the_enabled_sensors(monkeypatch, capsys):
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {})
    assert "allsky/sky_temperature" not in out  # MLX90614 non activé : topic jamais publié, donc absent de la liste
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {"ENABLED": "bme280,ds18b20,mlx90614"})
    names = [topic.split("/")[1] for topic in out.strip().split(",")]
    assert names == [
        "outdoor_temperature",
        "outdoor_humidity",
        "outdoor_pressure",
        "outdoor_dew_point",
        "dome_temperature",
        "sky_temperature",
    ]


def test_topic_list_stays_within_the_indi_allsky_limit(monkeypatch, capsys):
    env = {"ENABLED": "bme280,ds18b20,mlx90614", "ALTITUDE_M": "500", "HEATER_ENABLED": "1"}
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], env)
    assert len(out.strip().split(",")) <= 10  # le champ « Pin » d'indi-allsky n'accepte que 10 topics


def test_simulation_shows_a_sky_colder_than_the_air(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {})
    values = {line.split()[0]: float(line.split()[1]) for line in out.splitlines()}
    assert code == 0
    assert values["allsky/sky_temperature"] < values["allsky/outdoor_temperature"]


# ----------------------------------------------------------------------------- forçage du chauffage


def override_file(tmp_path, **values):
    """Fichier de forçage tel que l'écrit l'interface web, avec les défauts surchargeables."""
    path = tmp_path / "override.json"
    data = {"version": 1, "mode": "force", "target_c": 18.0, "minutes": 60, "created_at": 0.0, "expires_at": 3600.0, **values}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def keep_override(monkeypatch):
    """Simule un service déjà lancé : au démarrage, il annule la consigne, ici on la laisse vivre."""
    monkeypatch.setattr(sensors, "clear_override", lambda _path: False)


def published(output):
    """Valeurs imprimées par --dry-run : topic -> charge utile."""
    return dict(line.split() for line in output.splitlines() if line.startswith("allsky/"))


def test_missing_override_means_automatic_mode(tmp_path):
    assert sensors.load_override(tmp_path / "absent.json", now=0, max_target_c=35) == (None, None)


def test_override_is_bounded_and_deleted_once_expired(tmp_path):
    path = override_file(tmp_path, target_c=99.0)
    override, reason = sensors.load_override(path, now=100, max_target_c=35)
    assert reason is None
    assert override.target_c == 35.0  # bornée à HEATER_MAX_DOME_TEMP : on ne vise jamais la coupure
    assert override.remaining(now=100) == pytest.approx(3500)
    override, reason = sensors.load_override(path, now=3600, max_target_c=35)
    assert override is None and "expiré" in reason
    assert not path.exists()  # une consigne expirée est effacée


def test_override_expiry_is_capped_to_one_day(tmp_path):
    path = override_file(tmp_path, expires_at=10**9)  # date absurde, écrite à la main
    override, _ = sensors.load_override(path, now=1000, max_target_c=35)
    assert override.expires_at == pytest.approx(1000 + sensors.OVERRIDE_MAX_MINUTES * 60)


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "off"},  # mode inconnu
        {"version": 2},
        {"target_c": "18"},  # du texte
        {"target_c": True},  # un booléen n'est pas une température
        {"target_c": None},
        {"expires_at": "bientôt"},
        {"expires_at": None},
    ],
)
def test_unusable_override_is_ignored_and_kept(tmp_path, values):
    path = override_file(tmp_path, **values)
    override, reason = sensors.load_override(path, now=0, max_target_c=35)
    assert override is None and reason
    assert path.exists()  # conservé : il explique ce qui s'est passé


def test_broken_override_file_is_ignored(tmp_path):
    path = tmp_path / "override.json"
    path.write_text("{ pas du JSON", encoding="utf-8")
    override, reason = sensors.load_override(path, now=0, max_target_c=35)
    assert override is None and "illisible" in reason


def test_write_override_is_validated_and_readable(tmp_path):
    path = tmp_path / "state" / "override.json"
    override = sensors.write_override(path, target_c=20, minutes=30, now=1000.0, max_target_c=35)
    assert override.expires_at == pytest.approx(1000 + 30 * 60)
    assert not path.with_name("override.json.tmp").exists()  # écriture atomique : rien ne traîne
    loaded, reason = sensors.load_override(path, now=1100, max_target_c=35)
    assert reason is None and loaded.target_c == 20
    assert loaded.remaining(now=1100) == pytest.approx(30 * 60 - 100)


@pytest.mark.parametrize("values", [{"target_c": 50}, {"target_c": -30}, {"minutes": 1}, {"minutes": 5000}])
def test_write_override_refuses_out_of_range_values(tmp_path, values):
    arguments = {"target_c": 20, "minutes": 60, **values}
    with pytest.raises(ValueError, match="forçage"):
        sensors.write_override(tmp_path / "override.json", now=1000.0, max_target_c=35, **arguments)


def test_forced_target_ignores_the_dew_point():
    controller = make(volts=5)
    assert controller.update(dome=12.0, dew=11.0, now=0) > 0  # cible automatique 15 °C : il chaufferait
    assert controller.update(dome=12.0, dew=11.0, now=0, target=12.0) == 0.0  # consigne déjà atteinte


def test_forced_target_heats_up_to_the_power_cap():
    controller = make(volts=5)
    duties = [controller.update(dome=12.0, dew=4.0, now=step * 10, target=20.0) for step in range(300)]
    assert duties[0] > 0
    assert max(duties) <= controller.config.max_duty + 1e-9
    assert duties[-1] == pytest.approx(controller.config.max_duty)  # écart de 8 °C : plein régime autorisé


def test_forced_target_does_not_need_the_dew_point():
    controller = make()
    assert controller.update(dome=5.0, dew=None, now=0, target=18.0) > 0
    assert controller.update(dome=5.0, dew=None, now=300, target=18.0) > 0  # sans rosée depuis 5 min : toléré
    assert controller.setpoint == pytest.approx(18.0)


def test_forced_mode_keeps_the_dome_safeguards():
    controller = make()
    assert controller.update(dome=None, dew=None, now=0, target=18.0) == 0.0  # aucune mesure du dôme
    assert controller.update(dome=5.0, dew=None, now=10, target=18.0) > 0
    assert controller.update(dome=None, dew=None, now=200, target=18.0) == 0.0  # dôme périmé (> 120 s)
    assert controller.update(dome=40.0, dew=None, now=210, target=18.0) == 0.0  # dôme déjà trop chaud


def test_forced_target_is_capped_to_the_overtemperature_cutoff():
    controller = make(max_dome_temp=35)
    assert controller.update(dome=20.0, dew=None, now=0, target=99.0) > 0
    assert controller.setpoint == pytest.approx(35.0)


def test_switching_mode_clears_the_integral():
    controller = make(volts=5)
    for step in range(200):  # 33 min de saturation en régulation automatique
        controller.update(dome=0.0, dew=5.0, now=step * 10)
    duty = controller.update(dome=19.0, dew=5.0, now=2000, target=20.0)
    assert duty == pytest.approx(controller.config.kp * controller.config.max_duty, abs=0.02 * controller.config.max_duty)
    assert controller.update(dome=25.0, dew=5.0, now=2010) == 0.0  # retour en automatique : cible 9 °C
    assert controller.setpoint == pytest.approx(9.0)


def heater_env(tmp_path, **overrides):
    """Environnement d'un service chauffage actif, sans fichier d'environnement réel."""
    return {
        "HEATER_ENABLED": "1",
        "HEATER_SUPPLY_VOLTS": "12",
        "STATE_DIR": str(tmp_path),
        "ENV_FILE": str(tmp_path / "absent.env"),
        **overrides,
    }


def test_override_is_cleared_when_the_service_starts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "override.json"
    sensors.write_override(path, target_c=22.0, minutes=30, now=time.time(), max_target_c=35)
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path))
    assert code == 0
    assert not path.exists()  # un redémarrage du service repart en régulation automatique
    assert published(out)["allsky/heater_mode"] == "auto"


def test_forced_mode_is_published(tmp_path, monkeypatch, capsys):
    path = tmp_path / "override.json"
    sensors.write_override(path, target_c=22.0, minutes=30, now=time.time(), max_target_c=35)
    keep_override(monkeypatch)  # service déjà lancé : la consigne en place reste active
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path))
    values = published(out)
    assert code == 0
    assert values["allsky/heater_mode"] == "force"
    assert float(values["allsky/heater_target"]) == pytest.approx(22.0)
    assert float(values["allsky/dome_heater_pwm"]) > 0  # le dôme simulé est bien sous la consigne


def test_unusable_override_keeps_the_heater_in_automatic_mode(tmp_path, monkeypatch, capsys):
    (tmp_path / "override.json").write_text('{"mode": "force"}', encoding="utf-8")
    keep_override(monkeypatch)
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path))
    assert code == 0
    assert published(out)["allsky/heater_mode"] == "auto"


def test_override_is_dropped_when_the_heater_is_disabled(tmp_path, monkeypatch, capsys):
    path = tmp_path / "override.json"
    sensors.write_override(path, target_c=22.0, minutes=30, now=time.time(), max_target_c=35)
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"STATE_DIR": str(tmp_path)})
    assert code == 0
    assert not path.exists()  # une consigne sans chauffage n'a aucun sens : elle est effacée


def test_heater_status_topics_are_not_offered_to_indi_allsky(monkeypatch, capsys):
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {"HEATER_ENABLED": "1"})
    assert "dome_heater_pwm" in out
    assert "heater_mode" not in out and "heater_target" not in out  # ce ne sont pas des mesures


# ----------------------------------------------------------------------------- fichier d'environnement et commandes


def test_environment_file_parsing(tmp_path):
    path = tmp_path / "allsky-sensors.env"
    path.write_text(
        "# commentaire\nMQTT_HOST=broker.local\n\nDS18B20_OFFSET_C=3.0\nAVEC_ESPACES = 5 \nQUOTE=\"a b\"\nligne sans egal\n",
        encoding="utf-8",
    )
    assert sensors.read_env_file(path) == {
        "MQTT_HOST": "broker.local",
        "DS18B20_OFFSET_C": "3.0",
        "AVEC_ESPACES": "5",
        "QUOTE": "a b",
    }


def test_environment_file_tolerates_a_windows_bom(tmp_path):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("HEATER_ENABLED=1\n", encoding="utf-8-sig")  # Notepad et consorts ajoutent un BOM
    assert sensors.read_env_file(path) == {"HEATER_ENABLED": "1"}


def test_cli_env_survives_an_unreadable_environment_file(tmp_path, monkeypatch):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("HEATER_ENABLED=1\n", encoding="utf-8")

    def unreadable(_path):
        raise PermissionError("droits insuffisants")

    monkeypatch.setattr(sensors, "read_env_file", unreadable)
    monkeypatch.setenv("ENV_FILE", str(path))
    monkeypatch.setenv("STATE_DIR", "ailleurs")
    assert sensors.cli_env()["STATE_DIR"] == "ailleurs"  # le shell prend le relais, sans planter


def test_heater_test_reads_the_environment_file(tmp_path, monkeypatch, capsys):
    """Régression : lancé par SSH, --heater-test ne voyait ni HEATER_ENABLED ni HEATER_SUPPLY_VOLTS."""
    env_file = tmp_path / "allsky-sensors.env"
    env_file.write_text("HEATER_ENABLED=1\nHEATER_SUPPLY_VOLTS=5\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        sensors, "heater_test", lambda config, percent, seconds: calls.append((config.volts, percent, seconds)) or 0
    )
    code, _ = run_main(monkeypatch, capsys, ["--heater-test", "20", "--heater-test-seconds", "5"], {"ENV_FILE": str(env_file)})
    assert code == 0
    assert calls == [(5.0, 20.0, 5.0)]


def test_print_topics_reads_the_environment_file(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "allsky-sensors.env"
    env_file.write_text("ENABLED=bme280\nHEATER_ENABLED=1\nMQTT_PREFIX=meteo\n", encoding="utf-8")
    code, out = run_main(monkeypatch, capsys, ["--print-topics"], {"ENV_FILE": str(env_file)})
    assert code == 0
    assert out.strip().split(",") == [
        "meteo/outdoor_temperature",
        "meteo/outdoor_humidity",
        "meteo/outdoor_pressure",
        "meteo/outdoor_dew_point",
        "meteo/dome_heater_pwm",
    ]


def test_state_dir_sits_next_to_the_program_by_default(monkeypatch):
    monkeypatch.delenv("STATE_DIR", raising=False)
    assert sensors.override_path() == Path(sensors.__file__).resolve().parent / "state" / "override.json"
    monkeypatch.setenv("STATE_DIR", "D:/ailleurs")
    assert sensors.override_path() == Path("D:/ailleurs/override.json")


def test_heater_force_records_a_setpoint(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--heater-force", "18"], heater_env(tmp_path))
    assert code == 0
    override, reason = sensors.load_override(tmp_path / "override.json", now=time.time(), max_target_c=35)
    assert reason is None and override.target_c == 18.0
    assert override.remaining(time.time()) == pytest.approx(60 * 60, abs=60)  # 60 min par défaut


def test_heater_force_accepts_a_custom_duration(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--heater-force", "20", "--heater-force-minutes", "15"], heater_env(tmp_path))
    assert code == 0
    override, _ = sensors.load_override(tmp_path / "override.json", now=time.time(), max_target_c=35)
    assert override.remaining(time.time()) == pytest.approx(15 * 60, abs=60)


def test_heater_force_needs_the_heater_enabled(tmp_path, monkeypatch, capsys):
    env = {"STATE_DIR": str(tmp_path), "ENV_FILE": str(tmp_path / "absent.env")}
    code, _ = run_main(monkeypatch, capsys, ["--heater-force", "18"], env)
    assert code == 2
    assert not (tmp_path / "override.json").exists()


def test_heater_force_refuses_a_target_above_the_safety_cutoff(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--heater-force", "40"], heater_env(tmp_path))  # HEATER_MAX_DOME_TEMP=35
    assert code == 2
    assert not (tmp_path / "override.json").exists()


def test_heater_force_reads_the_environment_file(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "allsky-sensors.env"
    env_file.write_text(f"HEATER_ENABLED=1\nHEATER_SUPPLY_VOLTS=5\nSTATE_DIR={tmp_path}\n", encoding="utf-8")
    code, _ = run_main(monkeypatch, capsys, ["--heater-force", "18"], {"ENV_FILE": str(env_file)})
    assert code == 0
    override, reason = sensors.load_override(tmp_path / "override.json", now=time.time(), max_target_c=35)
    assert reason is None and override.target_c == 18.0


def test_heater_cancel_removes_the_setpoint(tmp_path, monkeypatch, capsys):
    path = tmp_path / "override.json"
    sensors.write_override(path, 18, 30, time.time(), 35)
    code, _ = run_main(monkeypatch, capsys, ["--heater-cancel"], heater_env(tmp_path))
    assert code == 0 and not path.exists()
    code, _ = run_main(monkeypatch, capsys, ["--heater-cancel"], heater_env(tmp_path))  # déjà en automatique
    assert code == 0


# ----------------------------------------------------------------------------- commandes MQTT


def test_mqtt_command_forces_a_target(tmp_path):
    path = tmp_path / "override.json"
    report = sensors.apply_heater_command("18", HeaterConfig(volts=12), path, now=1000.0)
    assert "18 °C" in report and "60 min" in report
    override, reason = sensors.load_override(path, now=1001, max_target_c=35)
    assert reason is None and override.target_c == 18.0


def test_mqtt_command_accepts_a_duration_and_a_french_decimal(tmp_path):
    path = tmp_path / "override.json"
    sensors.apply_heater_command("17,5 30", HeaterConfig(volts=12), path, now=1000.0)
    override, _ = sensors.load_override(path, now=1001, max_target_c=35)
    assert override.target_c == pytest.approx(17.5)
    assert override.expires_at == pytest.approx(1000 + 30 * 60)


def test_mqtt_cancel_command_drops_the_setpoint(tmp_path):
    path = tmp_path / "override.json"
    sensors.write_override(path, 18, 30, 1000.0, 35)
    assert "annulée" in sensors.apply_heater_command("cancel", HeaterConfig(volts=12), path, now=1001.0)
    assert not path.exists()
    assert "déjà" in sensors.apply_heater_command("cancel", HeaterConfig(volts=12), path, now=1002.0)


@pytest.mark.parametrize("text", ["", "chaud", "18 chaud", "18 30 40", "40", "18 1"])
def test_mqtt_command_refuses_garbage(tmp_path, text):
    path = tmp_path / "override.json"
    with pytest.raises(ValueError):
        sensors.apply_heater_command(text, HeaterConfig(volts=12), path, now=1000.0)
    assert not path.exists()  # une commande refusée ne laisse jamais de consigne derrière elle


def test_mqtt_message_text_reaches_the_handler():
    received = []
    publisher = sensors.Publisher("localhost", 1883, "", "", dry_run=True)
    publisher._on_command = received.append
    topic = publisher._command_topic
    publisher._on_message(None, None, SimpleNamespace(topic=topic, payload=b"18 30"))
    publisher._on_message(None, None, SimpleNamespace(topic=topic, payload=b"annuler"))
    assert received == ["18 30", "annuler"]


def test_mqtt_messages_are_ignored_when_no_command_is_expected():
    publisher = sensors.Publisher("localhost", 1883, "", "", dry_run=True)  # aucune souscription prévue
    message = SimpleNamespace(topic=f"{sensors.PREFIX}/{sensors.HEATER_COMMAND_TOPIC}", payload=b"18")
    publisher._on_message(None, None, message)  # sans effet et sans erreur


def test_mqtt_command_error_never_escapes_the_callback():
    def boom(_text):
        raise RuntimeError("panne simulée")

    publisher = sensors.Publisher("localhost", 1883, "", "", dry_run=True)
    publisher._on_command = boom
    message = SimpleNamespace(topic=publisher._command_topic, payload=b"18")
    publisher._on_message(None, None, message)  # avalé et journalisé


def test_subscription_result_is_logged(caplog):
    with caplog.at_level("INFO", logger="allsky-sensors"):
        sensors.Publisher._on_subscribe(None, None, 1, 0)
    assert "accepté" in caplog.text
    caplog.clear()
    with caplog.at_level("INFO", logger="allsky-sensors"):
        sensors.Publisher._on_subscribe(None, None, 1, 128)  # droits refusés par le broker
    assert "refusé" in caplog.text


def test_service_announces_a_disabled_heater(tmp_path, monkeypatch, capsys, caplog):
    env = {"STATE_DIR": str(tmp_path), "ENV_FILE": str(tmp_path / "absent.env")}
    with caplog.at_level("INFO", logger="allsky-sensors"):
        code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], env)
    assert code == 0
    assert "chauffage désactivé" in caplog.text  # la raison de l'absence de commande est dite


# ----------------------------------------------------------------------------- température du ciel par MQTT


SQMETER_STATE = {
    "timestamp": 1795452598,
    "timeValid": True,
    "dataAgeMs": 3053,
    "dataStale": False,
    "light": {"status": "missing"},
    "sky": {"status": "missing", "source": "sqm"},
    "environment": {"status": "missing"},
    "infrared": {"status": "ok", "ageMs": 3050, "skyTemperature": 23.9, "ambientTemperature": 26.4},
    "clouds": {
        "status": "ok",
        "coverPercent": 100,
        "condition": "overcast",
        "description": "Overcast",
        "temperatureDelta": -2.5,
        "correctedDelta": -2.9,
        "humidity": 53,
        "humiditySource": "assumed",
    },
}


def sqmeter_config(**overrides):
    return sensors.SkyConfig(topic="sqmeter/state", **overrides)


def sqmeter_payload(**overrides):
    data = json.loads(json.dumps(SQMETER_STATE))
    data.update(overrides)
    return json.dumps(data).encode()


def test_mqtt_sky_temperature_is_read_from_the_sqmeter_json():
    assert sensors.read_sky_payload(sqmeter_payload(), sqmeter_config()) == (pytest.approx(23.9), None)


def test_mqtt_sky_temperature_follows_the_configured_path():
    ambient, problem = sensors.read_sky_payload(sqmeter_payload(), sqmeter_config(key="infrared.ambientTemperature"))
    assert problem is None and ambient == pytest.approx(26.4)


def test_a_plain_number_topic_needs_an_empty_key():
    config = sensors.SkyConfig(topic="sqmeter/sky", key="", stale_key="", status_key="")
    assert sensors.read_sky_payload(b"-12.5", config) == (-12.5, None)


@pytest.mark.parametrize(
    ("overrides", "motif"),
    [
        ({"dataStale": True}, "périmée"),
        ({"infrared": {"status": "missing"}}, "ok"),
        ({"infrared": {"status": "ok"}}, "absente"),  # ni skyTemperature ni ambientTemperature
        ({"infrared": {"status": "ok", "skyTemperature": "chaud"}}, "n'est pas un nombre"),
        ({"infrared": {"status": "ok", "skyTemperature": True}}, "n'est pas un nombre"),
        ({"infrared": {"status": "ok", "skyTemperature": 400}}, "hors bornes"),
        ({"infrared": {"status": "ok", "skyTemperature": -100}}, "hors bornes"),
    ],
)
def test_unusable_mqtt_sky_messages_are_rejected(overrides, motif):
    value, problem = sensors.read_sky_payload(sqmeter_payload(**overrides), sqmeter_config())
    assert value is None and problem is not None and motif in problem


def test_mqtt_sky_payload_must_be_json_and_bounded():
    value, problem = sensors.read_sky_payload(b"pas du JSON", sqmeter_config())
    assert value is None and "JSON" in problem
    oversized = b"{" + b" " * sensors.SKY_MAX_PAYLOAD_BYTES + b"}"
    value, problem = sensors.read_sky_payload(oversized, sqmeter_config())
    assert value is None and "volumineux" in problem


def test_mqtt_sky_config_defaults_and_topic_validation():
    default = sensors.SkyConfig.from_env({})
    assert not default.enabled
    assert default.key == "infrared.skyTemperature"
    assert default.stale_key == "dataStale" and default.status_key == "infrared.status"
    assert default.max_age == 600
    config = sensors.SkyConfig.from_env({"SKY_TEMP_TOPIC": "sqmeter/state", "SKY_MAX_AGE": "120"})
    assert config.enabled and config.max_age == 120
    # Un espace de bord est neutralisé (le piège du 22/11 ne peut plus se produire) ; ailleurs, il est refusé.
    assert sensors.SkyConfig.from_env({"SKY_TEMP_TOPIC": " sqmeter/state  "}).topic == "sqmeter/state"
    for bad in ("sq meter/state", "sqmeter/#", "sqmeter/+/state"):
        with pytest.raises(ValueError, match="topic littéral"):
            sensors.SkyConfig.from_env({"SKY_TEMP_TOPIC": bad})


def test_mqtt_sky_feed_keeps_the_last_value_until_it_expires():
    feed = sensors.SkyFeed(sqmeter_config())
    assert feed.handle(sqmeter_payload()) == pytest.approx(23.9)
    reading = feed.reading(time.monotonic(), 600)
    assert reading is not None and reading[0] == pytest.approx(23.9)
    assert feed.reading(reading[1] + 601, 600) is None  # source muette : la valeur est oubliée


def test_mqtt_sky_feed_recovers_after_an_invalid_message(caplog):
    feed = sensors.SkyFeed(sqmeter_config())
    with caplog.at_level("WARNING", logger="allsky-sensors"):
        assert feed.handle(b"{}") is None
    assert "ignorée" in caplog.text
    assert feed.handle(sqmeter_payload()) == pytest.approx(23.9)


def test_mqtt_sky_is_used_only_without_a_local_sensor():
    feed = sensors.SkyFeed(sqmeter_config())
    feed.handle(sqmeter_payload())
    now = time.monotonic()
    measurement = sensors.sky_measurement(feed, sqmeter_config(), {}, now)
    assert measurement is not None and measurement[0] == pytest.approx(23.9)
    assert sensors.sky_measurement(feed, sqmeter_config(), {"sky_temperature": -5.0}, now) is None
    assert sensors.sky_measurement(feed, sensors.SkyConfig(), {}, now) is None  # source désactivée


def test_mqtt_sky_message_reaches_the_feed():
    feed = sensors.SkyFeed(sqmeter_config())
    publisher = sensors.Publisher(
        "localhost", 1883, "", "", dry_run=True, sky_topic="sqmeter/state", on_sky=feed.handle
    )
    message = SimpleNamespace(topic="sqmeter/state", payload=sqmeter_payload())
    publisher._on_message(None, None, message)
    assert feed.reading(time.monotonic(), 600)[0] == pytest.approx(23.9)


def test_mqtt_messages_from_other_topics_are_ignored():
    received = []
    feed = sensors.SkyFeed(sqmeter_config())
    publisher = sensors.Publisher(
        "localhost", 1883, "", "", dry_run=True, on_command=received.append,
        sky_topic="sqmeter/state", on_sky=feed.handle,
    )
    message = SimpleNamespace(topic="ailleurs/state", payload=sqmeter_payload())
    publisher._on_message(None, None, message)
    assert received == [] and feed.reading(time.monotonic(), 600) is None


def test_topic_list_includes_the_sky_from_mqtt(monkeypatch, capsys):
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], {"SKY_TEMP_TOPIC": "sqmeter/state"})
    names = [topic.split("/")[1] for topic in out.strip().split(",")]
    assert names.count("sky_temperature") == 1 and names[-1] == "sky_temperature"
    env = {"ENABLED": "bme280,ds18b20,mlx90614", "SKY_TEMP_TOPIC": "sqmeter/state"}
    _, out = run_main(monkeypatch, capsys, ["--print-topics"], env)
    assert out.count("sky_temperature") == 1  # une seule source, même si les deux sont configurées


def test_a_sky_topic_with_a_wildcard_is_refused_at_startup(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"SKY_TEMP_TOPIC": "sqmeter/#"})
    assert code == 2


def test_settings_read_the_sky_source():
    settings = sensors.Settings.from_env({"SKY_TEMP_TOPIC": "sqmeter/state", "SKY_MAX_AGE": "120"})
    assert settings.sky.topic == "sqmeter/state" and settings.sky.max_age == 120
    assert sensors.Settings.from_env({}).sky.enabled is False


# ----------------------------------------------------------------------------- anticipation (ciel dégagé)


def test_sky_anticipation_defaults_and_reading():
    default = sensors.SkyAnticipation.from_env({})
    assert not default.enabled and default.bonus_c == 0
    assert default.clear_delta_c == -10 and default.hysteresis_c == 2
    config = sensors.SkyAnticipation.from_env(
        {"SKY_ANTICIPATION_C": "2", "SKY_CLEAR_DELTA_C": "-12", "SKY_CLEAR_HYSTERESIS_C": "1,5"}
    )
    assert config.enabled and config.bonus_c == 2
    assert config.clear_delta_c == -12 and config.hysteresis_c == pytest.approx(1.5)


def test_sky_delta_needs_fresh_measurements():
    now = 1000.0
    readings = {"sky_temperature": (-15.0, now - 5)}
    assert sensors.sky_delta(readings, {"outdoor_temperature": 2.0}, now) == pytest.approx(-17.0)
    assert sensors.sky_delta({"sky_temperature": (-15.0, now - 121)}, {"outdoor_temperature": 2.0}, now) is None
    assert sensors.sky_delta({}, {"outdoor_temperature": 2.0}, now) is None
    assert sensors.sky_delta(readings, {}, now) is None


def test_anticipation_state_uses_hysteresis():
    config = sensors.SkyAnticipation(bonus_c=2, clear_delta_c=-10, hysteresis_c=2)
    assert sensors.anticipation_state(-12, config, False) is True
    assert sensors.anticipation_state(-9, config, False) is False  # pas encore sous le seuil
    assert sensors.anticipation_state(-9, config, True) is True  # reste dégagé (hystérésis)
    assert sensors.anticipation_state(-7, config, True) is False  # franchement au-dessus : retour
    assert sensors.anticipation_state(None, config, True) is False  # mesure perdue : marge nominale
    assert sensors.anticipation_state(-20, sensors.SkyAnticipation(), False) is False  # désactivé


def test_margin_bonus_applies_to_the_automatic_target_only():
    controller = make(volts=5)
    controller.update(dome=10.0, dew=5.0, now=0, margin_bonus=2.0)
    assert controller.setpoint == pytest.approx(5 + 4 + 2)
    controller.update(dome=10.0, dew=5.0, now=10, target=18.0, margin_bonus=2.0)
    assert controller.setpoint == pytest.approx(18.0)  # une consigne forcée ignore l'anticipation


def test_margin_bonus_never_exceeds_the_overtemperature_cutoff():
    controller = make(max_dome_temp=35)
    controller.update(dome=20.0, dew=30.0, now=0, margin_bonus=10.0)
    assert controller.setpoint == pytest.approx(35.0)


def test_anticipation_raises_the_automatic_target(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path, SKY_ANTICIPATION_C="2"))
    assert code == 0
    status = sensors.read_status(tmp_path / "status.json")
    assert status["target_c"] == pytest.approx(status["dew_c"] + 4 + 2)  # marge + anticipation
    assert status["anticipation_c"] == pytest.approx(2)
    assert status["sky_delta"] < -10  # le ciel simulé est bien plus froid que l'air


def test_anticipation_is_off_by_default_but_the_delta_is_published(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path))
    assert code == 0
    status = sensors.read_status(tmp_path / "status.json")
    assert status["target_c"] == pytest.approx(status["dew_c"] + 4)
    assert status["anticipation_c"] == 0
    assert status["sky_delta"] is not None  # observable pour régler le seuil, même sans bonus


def test_forced_mode_ignores_the_anticipation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "override.json"
    sensors.write_override(path, target_c=22.0, minutes=30, now=time.time(), max_target_c=35)
    keep_override(monkeypatch)
    code, out = run_main(
        monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path, SKY_ANTICIPATION_C="2")
    )
    assert code == 0
    assert float(published(out)["allsky/heater_target"]) == pytest.approx(22.0)
    status = sensors.read_status(tmp_path / "status.json")
    assert status["anticipation_c"] == pytest.approx(2)  # l'état reste visible, la consigne reste maîtresse


# ----------------------------------------------------------------------------- schéma, validation, réglages


EXAMPLE_ENV = Path(__file__).resolve().parent.parent / "allsky-sensors.env.example"


def parameter(name):
    return next(item for item in sensors.ENV_SCHEMA if item.name == name)


def test_schema_describes_every_variable_of_the_example_file():
    documented = set(sensors.read_env_file(EXAMPLE_ENV))
    assert documented <= sensors.ENV_KEYS  # chaque variable documentée est décrite par le schéma
    assert len(sensors.ENV_SCHEMA) == len(sensors.ENV_KEYS)  # pas de doublon
    for item in sensors.ENV_SCHEMA:
        assert item.section and item.label and item.kind


@pytest.mark.parametrize(
    ("name", "value", "valid"),
    [
        ("INTERVAL", "60", True),
        ("INTERVAL", "60.5", False),  # un entier, pas un flottant
        ("INTERVAL", "soixante", False),
        ("INTERVAL", "5", False),
        ("HEATER_MARGIN", "4,5", True),  # virgule décimale acceptée
        ("HEATER_MARGIN", "15", False),
        ("ENABLED", "bme280, mlx90614", True),
        ("ENABLED", "bme280,lunaire", False),
        ("ENABLED", "", False),
        ("GPIO_RESERVED", "17,18,22,27", True),
        ("GPIO_RESERVED", "17,40", False),
        ("BME280_ADDRESS", "0x77", True),
        ("BME280_ADDRESS", "0x80", False),
        ("BME280_ADDRESS", "76", True),  # hexadécimal sans préfixe
        ("WEB_PASSWORD", "mot-de-passe-solide", True),
        ("WEB_PASSWORD", "court", False),
        ("ALTITUDE_M", "", True),  # facultative
        ("HEATER_ENABLED", "2", False),
        ("MQTT_PORT", "1883", True),
        ("MQTT_PORT", "70000", False),
        ("SKY_ANTICIPATION_C", "2", True),
        ("SKY_ANTICIPATION_C", "-1", False),
        ("SKY_CLEAR_DELTA_C", "-12", True),
        ("SKY_CLEAR_DELTA_C", "2", False),
        ("SKY_CLEAR_HYSTERESIS_C", "2", True),
        ("SKY_CLEAR_HYSTERESIS_C", "20", False),
    ],
)
def test_parameter_validation(name, value, valid):
    message = parameter(name).validate_value(value)
    assert (message is None) is valid


def test_empty_environment_is_valid():
    assert sensors.validate_env({}) == []
    assert sensors.validate_env({"ENABLED": "bme280,ds18b20", "DS18B20_OFFSET_C": ""}) == []  # vide = 0


def test_validation_reports_every_problem():
    errors = sensors.validate_env({"INTERVAL": "2", "ENABLED": "bme280,lunaire", "HEATER_ENABLED": "1"})
    assert len(errors) == 3
    assert any("ENABLED" in message for message in errors)
    assert any("HEATER_SUPPLY_VOLTS" in message for message in errors)


def test_settings_read_values_and_defaults():
    settings = sensors.Settings.from_env(
        {"INTERVAL": "30", "ENABLED": "bme280,mlx90614", "ALTITUDE_M": "500", "MQTT_HOST": "broker.lan"}
    )
    assert settings.interval == 30
    assert settings.enabled == ("bme280", "mlx90614")
    assert settings.altitude == 500
    assert settings.heater is None and settings.mqtt.host == "broker.lan"
    default = sensors.Settings.from_env({})
    assert default.interval == 60 and default.enabled == ("bme280", "ds18b20") and default.altitude is None


def test_settings_accepts_french_decimals_and_hex_addresses():
    settings = sensors.Settings.from_env(
        {"HEATER_ENABLED": "1", "HEATER_SUPPLY_VOLTS": "12", "HEATER_MARGIN": "4,5", "MLX90614_ADDRESS": "0x5b"}
    )
    assert settings.heater.margin == pytest.approx(4.5)
    assert settings.mlx90614_address == 0x5B


def test_settings_refuses_an_invalid_file():
    with pytest.raises(sensors.ConfigError) as failure:
        sensors.Settings.from_env({"HEATER_MARGIN": "50"})
    assert failure.value.messages and "HEATER_MARGIN" in failure.value.messages[0]


def test_env_update_keeps_comments_and_unknown_variables(tmp_path):
    path = tmp_path / "allsky-sensors.env"
    original = EXAMPLE_ENV.read_text(encoding="utf-8").replace(
        "HEATER_MARGIN=4", "HEATER_MARGIN=4\n# ajouté à la main\nMA_VARIABLE=a moi"
    )
    path.write_text(original, encoding="utf-8")
    sensors.update_env_file(path, {"HEATER_MARGIN": "6", "ALTITUDE_M": "900"})
    text = path.read_text(encoding="utf-8")
    assert "HEATER_MARGIN=6" in text and "HEATER_MARGIN=4\n" not in text
    assert "MA_VARIABLE=a moi" in text and text.count("# ajouté à la main") == 1
    assert text.count("#") >= original.count("#")  # aucun commentaire perdu
    assert text.splitlines()[-1] == "ALTITUDE_M=900"  # clé absente (commentée) : ajoutée en fin de fichier
    assert not text.endswith("\n\n")  # pas de lignes vides en fin de fichier


def test_env_update_makes_a_backup_and_a_private_file(tmp_path):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("INTERVAL=60\n", encoding="utf-8")
    sensors.update_env_file(path, {"INTERVAL": "30"})
    backup = path.with_name("allsky-sensors.env.bak")
    assert backup.read_text(encoding="utf-8") == "INTERVAL=60\n"
    assert not path.with_name("allsky-sensors.env.tmp").exists()
    if os.name != "nt":  # Windows ne gère pas les permissions POSIX
        assert path.stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"VARIABLE_INCONNUE": "1"}, "clé inconnue"),
        ({"INTERVAL": "30\nHEATER_ENABLED=1"}, "saut de ligne"),
        ({"INTERVAL": "30\r\nHEATER_ENABLED=1"}, "saut de ligne"),
    ],
)
def test_env_update_refuses_injection(tmp_path, changes, message):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("INTERVAL=60\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        sensors.update_env_file(path, changes)
    assert path.read_text(encoding="utf-8") == "INTERVAL=60\n"  # rien n'a bougé


def test_env_update_creates_a_missing_file(tmp_path):
    path = tmp_path / "nouveau" / "allsky-sensors.env"
    sensors.update_env_file(path, {"INTERVAL": "45"})
    assert path.read_text(encoding="utf-8") == "INTERVAL=45\n"


def test_env_update_tolerates_a_bom(tmp_path):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("INTERVAL=60\n", encoding="utf-8-sig")
    sensors.update_env_file(path, {"INTERVAL": "30"})
    assert sensors.read_env_file(path)["INTERVAL"] == "30"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")  # le BOM disparaît


def test_status_round_trip(tmp_path):
    path = tmp_path / "status.json"
    sensors.write_status(path, {"mode": "auto", "duty_pct": 0.0})
    assert sensors.read_status(path) == {"version": 1, "mode": "auto", "duty_pct": 0.0}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o644  # lisible par l'interface web


def test_status_is_ignored_when_missing_or_broken(tmp_path):
    path = tmp_path / "status.json"
    assert sensors.read_status(path) is None
    path.write_text("{ pas du JSON", encoding="utf-8")
    assert sensors.read_status(path) is None
    path.write_text('{"version": 99, "mode": "auto"}', encoding="utf-8")
    assert sensors.read_status(path) is None


def test_status_path_follows_state_dir(tmp_path):
    assert sensors.status_path({"STATE_DIR": str(tmp_path)}) == tmp_path / "status.json"


# ----------------------------------------------------------------------------- état publié et rechargement à chaud


def test_settings_read_the_state_directory(tmp_path):
    assert sensors.Settings.from_env({"STATE_DIR": str(tmp_path)}).state_dir == tmp_path
    assert sensors.Settings.from_env({}).state_dir == sensors.STATE_DIR_DEFAULT


def test_service_publishes_its_state_for_the_web(tmp_path, monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], heater_env(tmp_path))
    assert code == 0
    status = sensors.read_status(tmp_path / "status.json")
    assert status is not None
    assert status["mode"] == "auto" and status["config_error"] is None
    assert status["dome_c"] is not None and status["dew_c"] is not None
    assert 0 <= status["duty_pct"] <= 100
    assert status["readings"]["dome_temperature"] == status["dome_c"]
    assert status["pid"] > 0 and status["settings_mtime"] is None  # pas de fichier surveillé ici


def test_heater_state_is_published_at_every_tick_not_only_every_minute(tmp_path, monkeypatch, capsys):
    """Une interface de pilotage doit voir la PWM changer vite : 10 s, pas 60 s."""
    calls = []

    def fake_wait(_event, timeout=None):
        calls.append(timeout)
        if len(calls) >= 3:
            _event.set()
        return False

    monkeypatch.setattr(sensors.threading.Event, "wait", fake_wait)
    code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run"], heater_env(tmp_path))
    assert code == 0
    assert out.count("allsky/dome_heater_pwm") == 3  # un envoi par tick
    assert out.count("allsky/outdoor_temperature") == 1  # les mesures restent à INTERVAL
    assert calls == [sensors.HEATER_TICK_SECONDS] * 3


def test_service_reloads_the_environment_file(tmp_path, monkeypatch, capsys, caplog):
    envelope = tmp_path / "allsky-sensors.env"

    def write(margin):
        envelope.write_text(
            f"ENABLED=bme280,ds18b20\nINTERVAL=60\nHEATER_ENABLED=1\nHEATER_SUPPLY_VOLTS=12\n"
            f"HEATER_MARGIN={margin}\nSTATE_DIR={tmp_path}\n",
            encoding="utf-8",
        )

    write(4)
    calls = []

    def fake_wait(_event, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            write(6)  # modifier le fichier pendant le service : il doit être rechargé au tick suivant
        else:
            _event.set()
        return False

    monkeypatch.setattr(sensors.threading.Event, "wait", fake_wait)
    with caplog.at_level("INFO", logger="allsky-sensors"):
        code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run"], {"ENV_FILE": str(envelope), "STATE_DIR": str(tmp_path)})
    assert code == 0
    assert "configuration rechargée depuis" in caplog.text
    status = sensors.read_status(tmp_path / "status.json")
    assert status is not None and status["settings_mtime"] is not None
    assert status["target_c"] == pytest.approx(status["dew_c"] + 6)  # la nouvelle marge est appliquée


def test_invalid_reloaded_configuration_is_ignored(tmp_path, monkeypatch, capsys, caplog):
    envelope = tmp_path / "allsky-sensors.env"
    envelope.write_text(f"INTERVAL=60\nHEATER_MARGIN=4\nSTATE_DIR={tmp_path}\n", encoding="utf-8")
    calls = []

    def fake_wait(_event, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            envelope.write_text("HEATER_MARGIN=50\n", encoding="utf-8")  # hors bornes
        else:
            _event.set()
        return False

    monkeypatch.setattr(sensors.threading.Event, "wait", fake_wait)
    with caplog.at_level("INFO", logger="allsky-sensors"):
        code, _ = run_main(monkeypatch, capsys, ["--simulate", "--dry-run"], {"ENV_FILE": str(envelope), "STATE_DIR": str(tmp_path)})
    assert code == 0  # le service continue
    assert "configuration non appliquée" in caplog.text
    status = sensors.read_status(tmp_path / "status.json")
    assert status is not None and "HEATER_MARGIN" in status["config_error"]  # visible sur le web


def test_service_reads_its_configuration_from_the_environment_file(tmp_path, monkeypatch, capsys, caplog):
    """Régression : une unité systemd mal écrite ne doit plus laisser le service sans configuration."""
    path = tmp_path / "allsky-sensors.env"
    path.write_text(
        f"ENABLED=bme280,ds18b20\nHEATER_ENABLED=1\nHEATER_SUPPLY_VOLTS=12\nSTATE_DIR={tmp_path}\n", encoding="utf-8"
    )
    with caplog.at_level("INFO", logger="allsky-sensors"):
        code, out = run_main(monkeypatch, capsys, ["--simulate", "--dry-run", "--once"], {"ENV_FILE": str(path)})
    assert code == 0
    assert "chauffage : 6 x 47 ohm" in caplog.text  # réglages lus dans le fichier, pas dans l'environnement
    assert "allsky/heater_mode" in out


def test_service_env_prefers_the_file_over_the_process_environment(tmp_path, monkeypatch):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("INTERVAL=30\n", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(path))
    monkeypatch.setenv("INTERVAL", "60")
    values = sensors.service_env()
    assert values["INTERVAL"] == "30"  # le fichier est la source de vérité, puisqu'il est rechargé à chaud
    assert values["PATH"] == os.environ["PATH"]  # le reste de l'environnement est conservé


def test_empty_env_file_setting_disables_the_watch(monkeypatch):
    monkeypatch.setenv("ENV_FILE", "")
    assert sensors.configured_env_file() is None


def test_default_environment_file_is_used_when_the_unit_forgets_it(tmp_path, monkeypatch):
    path = tmp_path / "allsky-sensors.env"
    path.write_text("INTERVAL=45\n", encoding="utf-8")
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setattr(sensors, "DEFAULT_ENV_FILE", path)
    assert sensors.configured_env_file() == path
    assert sensors.service_env()["INTERVAL"] == "45"
    monkeypatch.setattr(sensors, "DEFAULT_ENV_FILE", tmp_path / "absent.env")
    assert sensors.configured_env_file() is None  # aucun fichier à surveiller : l'environnement suffit

