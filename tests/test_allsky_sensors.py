import os
import socket
import sys
from collections import deque

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
    prefixes = ("HEATER_", "MQTT_", "ALTITUDE_", "MLX90614_", "DS18B20_", "BME280_", "ENABLED")
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
