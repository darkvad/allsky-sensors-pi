"""Tests de l'interface web (socle) : un vrai serveur HTTP démarre sur le port 0."""

import base64
import errno
import html
import http.client
import json
import threading
import time
from urllib.parse import urlencode

import pytest

import allsky_sensors_mqtt as sensors
import web_api

USER = "admin"
PASSWORD = "mot-de-passe-solide"  # 20 caractères

# Fichier de réglages de test : commentaires et ordre à préserver, chauffage activé pour que la
# validation croisée (tension obligatoire) ait quelque chose à vérifier.
ENV_TEMPLATE = f"""# réglages de test
MQTT_HOST=broker.local
MQTT_PORT=1883
MQTT_USER=allsky-pi
MQTT_PASSWORD=mot-de-passe-mqtt
ENABLED=bme280,ds18b20
INTERVAL=60

# chauffage
HEATER_ENABLED=1
HEATER_SUPPLY_VOLTS=5
HEATER_MARGIN=4

# interface
WEB_USER=admin
WEB_PASSWORD={PASSWORD}
"""


def status_payload(**overrides) -> dict[str, object]:
    """État publié tel que le service l'écrit (voir write_status)."""
    now = time.time()
    data: dict[str, object] = {
        "updated_at": now,
        "mode": "auto",
        "target_c": 7.5,
        "override_target_c": None,
        "override_expires_at": None,
        "dome_c": 11.2,
        "dew_c": 3.5,
        "duty_pct": 0.0,
        "readings": {"outdoor_temperature": 10.4, "dome_temperature": 11.2},
        "sensors_unavailable": [],
        "config_error": None,
        "settings_mtime": now - 60,
        "pid": 4242,
    }
    data.update(overrides)
    return data


@pytest.fixture
def serveur(tmp_path):
    """Serveur réel sur le port 0 (le noyau choisit un port libre) : rien n'est simulé."""
    status_file = tmp_path / "status.json"
    sensors.write_status(status_file, status_payload())
    env_file = tmp_path / "allsky-sensors.env"
    env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
    config = web_api.WebConfig(bind="127.0.0.1", port=0, user=USER, password=PASSWORD)
    app = web_api.WebApp(config=config, status_file=status_file, env_file=env_file)
    server = web_api.WebServer(app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1], status_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(port, method="GET", path="/", *, auth=True, password=PASSWORD, headers=None, body=b""):
    """Requête HTTP brute, sans suivi de redirection ni cache (comme un navigateur, en plus direct)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = dict(headers or {})
    if auth:
        token = base64.b64encode(f"{USER}:{password}".encode()).decode()
        request_headers["Authorization"] = f"Basic {token}"
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        received = {key.lower(): value for key, value in response.getheaders()}
        return response.status, received, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


# ----------------------------------------------------------------------------- authentification


def test_sans_identifiants_tout_est_refuse(serveur):
    _, port, _ = serveur
    for path in ("/", "/sante"):
        code, headers, body = call(port, path=path, auth=False)
        assert code == 401
        assert headers["www-authenticate"].startswith("Basic ")
        assert "Authentification requise" in body


def test_mauvais_mot_de_passe_refuse(serveur):
    _, port, _ = serveur
    assert call(port, password="mauvais-mot-de-passe")[0] == 401


def test_post_sans_identifiants_refuse_avant_tout_controle(serveur):
    _, port, _ = serveur
    assert call(port, method="POST", path="/forcer", auth=False, body=b"x")[0] == 401


# ----------------------------------------------------------------------------- page d'état


def test_page_etat_en_francais(serveur):
    _, port, _ = serveur
    code, headers, body = call(port)
    assert code == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert 'lang="fr"' in body and 'charset="utf-8"' in body
    assert "Automatique" in body and "11,2 °C" in body and "10,4 °C" in body
    assert "il y a" in body  # âge des mesures
    assert "python" not in headers["server"].lower()  # pas de version offerte aux curieux


def test_la_page_n_affiche_aucun_secret(serveur):
    _, port, _ = serveur
    _, _, body = call(port)
    assert PASSWORD not in body
    assert "authorization" not in body.lower()


def test_page_etat_sans_fichier(serveur):
    _, port, status_file = serveur
    status_file.unlink()
    code, _, body = call(port)
    assert code == 200
    assert "Aucun état publié" in body


def test_consigne_forcee_affichee_avec_le_temps_restant(serveur):
    _, port, status_file = serveur
    sensors.write_status(
        status_file,
        status_payload(mode="force", target_c=20.0, override_target_c=20.0, override_expires_at=time.time() + 1800),
    )
    _, _, body = call(port)
    assert "Forcé" in body and "Consigne forcée" in body and "20,0 °C" in body and "encore" in body


def test_config_error_est_affichee_et_echappee(serveur):
    _, port, status_file = serveur
    sensors.write_status(status_file, status_payload(config_error="HEATER_MARGIN : <script>alert(1)</script>"))
    _, _, body = call(port)
    assert "Configuration refusée" in body
    assert "&lt;script&gt;" in body and "<script>alert" not in body


def test_mesures_affichees_avec_leurs_unites(serveur):
    _, port, status_file = serveur
    readings = {"outdoor_humidity": 62.44, "outdoor_pressure": 1019.78, "inconnu": 1.5}
    sensors.write_status(status_file, status_payload(readings=readings))
    _, _, body = call(port)
    assert "62 %" in body and "1019,8 hPa" in body and "inconnu" in body


def test_pas_de_doublon_entre_entete_et_mesures(serveur):
    """Le dôme et le point de rosée figurent en tête : les répéter dans la liste serait bavard."""
    _, port, _ = serveur
    _, _, body = call(port)
    assert body.count("Température du dôme") == 1
    assert body.count("Point de rosée") == 1

def test_route_inconnue(serveur):
    _, port, _ = serveur
    assert call(port, path="/nimporte-quoi")[0] == 404


# ----------------------------------------------------------------------------- supervision


def test_sante_ok(serveur):
    _, port, _ = serveur
    code, headers, body = call(port, path="/sante")
    assert code == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    payload = json.loads(body)
    assert payload["etat"] == "ok" and payload["mode"] == "auto"


def test_sante_degradee_quand_l_etat_est_vieux(serveur):
    _, port, status_file = serveur
    sensors.write_status(status_file, status_payload(updated_at=time.time() - 1000))
    code, _, body = call(port, path="/sante")
    assert code == 503
    assert json.loads(body)["etat"] == "degrade"


def test_sante_sans_fichier(serveur):
    _, port, status_file = serveur
    status_file.unlink()
    code, _, body = call(port, path="/sante")
    assert code == 503
    assert json.loads(body)["mise_a_jour_s"] is None


# ----------------------------------------------------------------------------- POST (socle)


def test_post_sans_origine_sans_jeton_est_refuse(serveur):
    _, port, _ = serveur
    assert post_settings(port, [("INTERVAL", "30")], include_token=False, origin=None)[0] == 403


def test_post_sans_origine_avec_jeton_est_accepte(serveur):
    """Régression du 22/11 : un navigateur qui n'annonce pas Origin recevait 403 sur l'enregistrement."""
    server, port, _ = serveur
    env_file = server.app.env_file
    code, _, _ = post_settings(port, form_fields(env_file, INTERVAL="30"), origin=None)
    assert code == 303
    assert "INTERVAL=30" in env_file.read_text(encoding="utf-8")


def test_post_d_une_autre_origine_refuse(serveur):
    server, port, _ = serveur
    code, _, body = post_settings(port, form_fields(server.app.env_file, INTERVAL="30"), origin="http://ailleurs.example")
    assert code == 403
    assert "Requête refusée" in body


def test_post_avec_un_jeton_invalide_refuse(serveur):
    server, port, _ = serveur
    before = server.app.env_file.read_text(encoding="utf-8")
    assert post_settings(port, form_fields(server.app.env_file, INTERVAL="30"), token="faux-jeton")[0] == 403
    assert server.app.env_file.read_text(encoding="utf-8") == before


def test_formulaire_contient_un_jeton(serveur):
    _, port, _ = serveur
    assert f'name="_jeton" value="{web_api.form_token(PASSWORD)}"' in call(port, path="/reglages")[2]


def test_origine_compare_la_machine_pas_l_url_brute():
    """Derrière un reverse proxy, le Host peut arriver sans port : on compare les machines."""
    assert web_api._origin_matches_host("http://127.0.0.1:8080", "127.0.0.1")
    assert web_api._origin_matches_host("http://127.0.0.1:8080", "127.0.0.1:8080")
    assert web_api._origin_matches_host("http://INDICAM-PI:8080", "indicam-pi")
    assert not web_api._origin_matches_host("http://ailleurs.example", "127.0.0.1:8080")
    assert not web_api._origin_matches_host("http://127.0.0.1:8080", "127.0.0.1:8081")


def test_origine_masquee_avec_jeton_est_accepte(serveur):
    """`Referrer-Policy: no-referrer` faisait sérialiser Origin à « null » : le jeton prend le relais."""
    server, port, _ = serveur
    env_file = server.app.env_file
    code, _, _ = post_settings(port, form_fields(env_file, HEATER_MARGIN="5"), origin="null")
    assert code == 303
    assert "HEATER_MARGIN=5" in env_file.read_text(encoding="utf-8")


def test_politique_de_referrer_compatible_avec_l_origine(serveur):
    """« no-referrer » neutralise Origin : la politique doit rester « same-origin »."""
    _, port, _ = serveur
    assert call(port, path="/reglages")[1]["referrer-policy"] == "same-origin"


def test_post_de_la_meme_origine_atteint_le_socle(serveur):
    _, port, _ = serveur
    code, _, body = call(port, method="POST", path="/forcer", headers={"Origin": f"http://127.0.0.1:{port}"}, body=b"")
    assert code == 404  # aucune action branchée ici, mais les contrôles sont passés
    assert "Aucune action" in body


def test_post_trop_volumineux_refuse(serveur):
    _, port, _ = serveur
    body = b"a" * (web_api.MAX_BODY_BYTES + 1)
    code, _, _ = call(port, method="POST", path="/reglages", headers={"Origin": f"http://127.0.0.1:{port}"}, body=body)
    assert code == 413


# ----------------------------------------------------------------------------- configuration


def test_mot_de_passe_web_obligatoire():
    with pytest.raises(ValueError, match="WEB_PASSWORD"):
        web_api.WebConfig.from_env({})
    with pytest.raises(ValueError, match="12"):
        web_api.WebConfig.from_env({"WEB_PASSWORD": "court"})


def test_configuration_web_par_defaut():
    config = web_api.WebConfig.from_env({"WEB_PASSWORD": "x" * 12})
    assert (config.bind, config.port, config.user) == ("0.0.0.0", 8080, "admin")


@pytest.mark.parametrize("value", ["abc", "0", "70000"])
def test_port_web_invalide(value):
    with pytest.raises(ValueError, match="WEB_PORT"):
        web_api.WebConfig.from_env({"WEB_PASSWORD": "x" * 12, "WEB_PORT": value})


def test_utilisateur_web_vide_refuse():
    with pytest.raises(ValueError, match="WEB_USER"):
        web_api.WebConfig.from_env({"WEB_PASSWORD": "x" * 12, "WEB_USER": "  "})


def test_le_schema_decrit_deja_les_variables_web():
    """Le formulaire de l'étape 5 se génère depuis ENV_SCHEMA : la section web y est déjà décrite."""
    assert {"WEB_BIND", "WEB_PORT", "WEB_USER", "WEB_PASSWORD"} <= sensors.ENV_KEYS
    password = next(param for param in sensors.ENV_SCHEMA if param.name == "WEB_PASSWORD")
    assert password.min_length == web_api.PASSWORD_MIN_LENGTH and password.kind == "secret"


# ----------------------------------------------------------------------------- mise en forme


def test_mise_en_forme_francaise():
    assert web_api._decimal(11.26) == "11,3"
    assert web_api._decimal(None) == "—"
    assert web_api._temperature(None) == "—"
    assert web_api._percent(94.0) == "94 %"
    assert web_api._age_text(30) == "il y a 30 s"
    assert web_api._age_text(600) == "il y a 10 min"
    assert web_api._age_text(7200) == "il y a 2 h 00 min"
    assert web_api._remaining_text(30) == "moins d'une minute"
    assert web_api._remaining_text(0) == "expirée"
    assert web_api._remaining_text(3600) == "1 h 00 min"


# ----------------------------------------------------------------------------- formulaire de réglages


def form_fields(env_file, **overrides):
    """Champs que le navigateur renverrait : valeurs du fichier, secrets laissés vides."""
    env = sensors.read_env_file(env_file)
    fields = []
    for param in sensors.ENV_SCHEMA:
        value = env.get(param.name, param.default or "")
        if param.kind == "secret":
            continue  # vide = inchangé
        if param.kind == "sensors":
            fields += [(param.name, name) for name in value.split(",") if name]
        else:
            fields.append((param.name, value))
    for name, value in overrides.items():
        fields = [item for item in fields if item[0] != name]
        if name == "ENABLED":
            fields += [("ENABLED", sensor) for sensor in value.split(",") if sensor]
        else:
            fields.append((name, value))
    return fields


def post_settings(port, fields, *, content_type="application/x-www-form-urlencoded", origin="same", token=None, include_token=True):
    """Envoie le formulaire ; ``origin=None`` simule un navigateur qui n'annonce pas son origine."""
    if include_token:
        fields = [*fields, ("_jeton", web_api.form_token(PASSWORD) if token is None else token)]
    headers = {"Content-Type": content_type}
    if origin == "same":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin is not None:
        headers["Origin"] = origin
    return call(port, method="POST", path="/reglages", headers=headers, body=urlencode(fields).encode("utf-8"))


def test_formulaire_affiche_les_sections_et_masque_les_secrets(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    code, headers, body = call(port, path="/reglages")
    assert code == 200 and headers["content-type"] == "text/html; charset=utf-8"
    for section in ("Broker MQTT", "Capteurs", "Chauffage", "Interface web"):
        assert section in body
    assert 'name="HEATER_MARGIN" value="4"' in body  # valeur du fichier
    assert 'type="password"' in body and "au redémarrage" in body
    assert PASSWORD not in body and "mot-de-passe-mqtt" not in body
    assert html.escape(str(env_file)) in body


def test_enregistrement_valide_conserve_commentaires_et_ordre(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    before = env_file.read_text(encoding="utf-8")
    code, headers, _ = post_settings(port, form_fields(env_file, HEATER_MARGIN="6"))
    assert code == 303
    assert headers["location"].startswith("/reglages?applique=")
    assert "redemarrage" not in headers["location"]
    after = env_file.read_text(encoding="utf-8")
    assert "HEATER_MARGIN=6" in after and "HEATER_MARGIN=4" not in after
    assert after.splitlines()[0] == "# réglages de test"  # commentaires et ordre préservés
    assert after.index("MQTT_HOST") < after.index("HEATER_MARGIN")
    assert env_file.with_name(env_file.name + ".bak").read_text(encoding="utf-8") == before


def test_valeur_hors_bornes_refusee_sans_toucher_au_fichier(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    before = env_file.read_text(encoding="utf-8")
    code, _, body = post_settings(port, form_fields(env_file, HEATER_MARGIN="50"))
    assert code == 200  # le formulaire est réaffiché avec l'erreur
    assert "HEATER_MARGIN" in body and "au plus" in body
    assert env_file.read_text(encoding="utf-8") == before
    assert not env_file.with_name(env_file.name + ".bak").exists()


def test_secret_vide_laisse_inchange(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    code, _, _ = post_settings(port, form_fields(env_file, INTERVAL="30"))
    assert code == 303
    text = env_file.read_text(encoding="utf-8")
    assert "INTERVAL=30" in text
    assert "WEB_PASSWORD=mot-de-passe-solide" in text
    assert "MQTT_PASSWORD=mot-de-passe-mqtt" in text


def test_secret_remplace_et_redemarrage_annonce(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    fields = form_fields(env_file) + [("WEB_PASSWORD", "nouveau-mot-de-passe")]
    code, headers, _ = post_settings(port, fields)
    assert code == 303 and "redemarrage=1" in headers["location"]
    assert "WEB_PASSWORD=nouveau-mot-de-passe" in env_file.read_text(encoding="utf-8")


def test_mot_de_passe_web_trop_court_refuse(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    before = env_file.read_text(encoding="utf-8")
    code, _, body = post_settings(port, form_fields(env_file) + [("WEB_PASSWORD", "court")])
    assert code == 200 and "12 caractères" in body
    assert env_file.read_text(encoding="utf-8") == before


def test_enregistrement_sans_changement_ne_reecrit_pas(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    before = env_file.stat().st_mtime_ns
    time.sleep(0.01)  # laisse filer l'horloge de modification
    code, headers, _ = post_settings(port, form_fields(env_file))
    assert code == 303 and headers["location"].endswith("inchange=1")
    assert env_file.stat().st_mtime_ns == before  # pas de réécriture, donc pas de rechargement inutile


def test_cle_hors_schema_ignoree(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    fields = form_fields(env_file, INTERVAL="30") + [("PATH", "/evil"), ("INJECTEE", "1")]
    assert post_settings(port, fields)[0] == 303
    text = env_file.read_text(encoding="utf-8")
    assert "INTERVAL=30" in text and "evil" not in text and "INJECTEE" not in text


def test_saut_de_ligne_refuse_par_l_ecriture(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    before = env_file.read_text(encoding="utf-8")
    code, _, body = post_settings(port, form_fields(env_file, DS18B20_ID="a\nb"))
    assert code == 200 and "écriture impossible" in body
    assert env_file.read_text(encoding="utf-8") == before


def test_fichier_de_reglages_absent(serveur):
    server, port, _ = serveur
    server.app.env_file.unlink()
    assert call(port, path="/reglages")[0] == 409
    assert post_settings(port, [("INTERVAL", "30")])[0] == 409


def test_mauvais_type_de_contenu_refuse(serveur):
    _, port, _ = serveur
    assert post_settings(port, [("INTERVAL", "30")], content_type="application/json")[0] == 415


def test_la_page_etat_renvoie_vers_les_reglages(serveur):
    _, port, _ = serveur
    assert 'href="/reglages"' in call(port)[2]


def test_message_clair_quand_l_ecriture_est_refusee(serveur, monkeypatch):
    """Un EROFS ou un EACCES doit dire quoi corriger (ReadWritePaths, propriétaire du dossier)."""
    server, port, _ = serveur
    env_file = server.app.env_file

    def refuse(_path, _changes):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(sensors, "update_env_file", refuse)
    code, _, body = post_settings(port, form_fields(env_file, HEATER_MARGIN="6"))
    assert code == 200 and "ReadWritePaths" in body

    def interdit(_path, _changes):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(sensors, "update_env_file", interdit)
    code, _, body = post_settings(port, form_fields(env_file, HEATER_MARGIN="6"))
    assert code == 200 and "compte du service" in body


def test_collect_changes_normalise_nombres_adresses_et_listes():
    env = {"HEATER_MARGIN": "4", "ENABLED": "bme280,ds18b20", "BME280_ADDRESS": "0x76"}
    params = {"HEATER_MARGIN": ["4,0"], "ENABLED": ["ds18b20", "bme280"], "BME280_ADDRESS": ["0x76"]}
    changes, _ = web_api.collect_changes(params, env)
    assert "HEATER_MARGIN" not in changes  # 4,0 et 4 sont la même valeur
    assert "ENABLED" not in changes  # même liste, autre ordre
    assert "BME280_ADDRESS" not in changes



