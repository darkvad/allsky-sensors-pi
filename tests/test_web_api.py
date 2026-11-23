"""Tests de l'interface web (état, réglages, forçage) : un vrai serveur HTTP démarre sur le port 0."""

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
    # Le dossier d'état suit le fichier de réglages : les tests n'écrivent rien dans le dépôt.
    env_file.write_text(f"{ENV_TEMPLATE}\nSTATE_DIR={tmp_path.as_posix()}\n", encoding="utf-8")
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


def test_ciel_et_anticipation_affiches(serveur):
    _, port, status_file = serveur
    sensors.write_status(status_file, status_payload(sky_delta=-12.5, anticipation_c=2.0))
    _, _, body = call(port)
    assert "Ciel − air extérieur" in body and "-12,5 °C" in body
    assert "Anticipation" in body and "+2,0 °C (ciel dégagé)" in body


def test_anticipation_inactive_affichee(serveur):
    _, port, status_file = serveur
    sensors.write_status(status_file, status_payload(sky_delta=-3.0, anticipation_c=0))
    _, _, body = call(port)
    assert "Ciel − air extérieur" in body and "-3,0 °C" in body
    assert "inactive" in body


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
    code, _, body = call(port, method="POST", path="/action-inconnue", headers={"Origin": f"http://127.0.0.1:{port}"}, body=b"")
    assert code == 404  # route inexistante, mais les contrôles sont passés
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
    for section in ("Broker MQTT", "Capteurs", "Température du ciel (MQTT)", "Anticipation (ciel dégagé)", "Chauffage", "Interface web"):
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





# ----------------------------------------------------------------------------- forçage du chauffage


def override_path(server):
    """Fichier de consigne tel que le web l'écrit : STATE_DIR du fichier de réglages."""
    return sensors.override_path(server.app.read_env())


def post_form(port, path, fields, *, content_type="application/x-www-form-urlencoded", origin="same", token=None, include_token=True):
    """POST d'un formulaire, jeton compris ; ``origin=None`` : navigateur qui n'annonce pas son origine."""
    if include_token:
        fields = [*fields, ("_jeton", web_api.form_token(PASSWORD) if token is None else token)]
    headers = {"Content-Type": content_type}
    if origin == "same":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin is not None:
        headers["Origin"] = origin
    return call(port, method="POST", path=path, headers=headers, body=urlencode(fields).encode("utf-8"))


def post_force(port, target="18", minutes="30", **options):
    return post_form(port, "/forcer", [("target_c", target), ("minutes", minutes)], **options)


def post_cancel(port, **options):
    return post_form(port, "/forcer/annuler", [], **options)


def test_page_chauffage_affiche_l_etat_et_les_formulaires(serveur):
    _, port, _ = serveur
    code, headers, body = call(port, path="/chauffage")
    assert code == 200 and headers["content-type"] == "text/html; charset=utf-8"
    assert "Automatique" in body and "Cible du chauffage" in body
    assert 'action="/forcer"' in body and 'action="/forcer/annuler"' in body
    assert f'name="_jeton" value="{web_api.form_token(PASSWORD)}"' in body
    assert 'name="target_c" value="18"' in body  # proposition par défaut
    assert 'name="minutes" value="60"' in body
    assert "HEATER_MAX_DOME_TEMP" in body and "35 °C" in body  # bornes rappelées
    assert PASSWORD not in body


def test_page_chauffage_sans_etat_publie(serveur):
    _, port, status_file = serveur
    status_file.unlink()
    code, _, body = call(port, path="/chauffage")
    assert code == 200 and "Aucun état publié" in body


def test_page_chauffage_montre_le_mode_force_et_le_temps_restant(serveur):
    _, port, status_file = serveur
    sensors.write_status(
        status_file,
        status_payload(mode="force", target_c=20.0, override_target_c=20.0, override_expires_at=time.time() + 1800),
    )
    body = call(port, path="/chauffage")[2]
    assert "Forcé" in body and "Consigne forcée" in body and "20,0 °C" in body and "encore" in body
    assert 'name="target_c" value="20"' in body  # la consigne en cours est proposée


def test_la_page_etat_renvoie_vers_le_forcage(serveur):
    _, port, _ = serveur
    assert 'href="/chauffage"' in call(port)[2]


def test_forcer_ecrit_la_consigne(serveur):
    server, port, _ = serveur
    code, headers, _ = post_force(port, target="18", minutes="30")
    assert code == 303 and headers["location"].startswith("/chauffage?force=")
    data = json.loads(override_path(server).read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["mode"] == "force"
    assert data["target_c"] == 18.0 and data["minutes"] == 30
    assert data["expires_at"] - data["created_at"] == 30 * 60
    assert override_path(server).parent == server.app.status_file.parent  # même dossier d'état que le service


def test_forcer_accepte_la_virgule_decimale(serveur):
    server, port, _ = serveur
    assert post_force(port, target="17,5", minutes="15")[0] == 303
    assert json.loads(override_path(server).read_text(encoding="utf-8"))["target_c"] == 17.5


def test_forcer_duree_vide_vaut_la_duree_par_defaut(serveur):
    server, port, _ = serveur
    assert post_force(port, target="20", minutes="")[0] == 303
    assert json.loads(override_path(server).read_text(encoding="utf-8"))["minutes"] == sensors.OVERRIDE_DEFAULT_MINUTES



def test_forcer_refuse_une_consigne_au_dessus_du_plafond(serveur):
    server, port, _ = serveur
    code, _, body = post_force(port, target="40")
    assert code == 200  # le formulaire est réaffiché avec l'erreur
    assert "température attendue entre" in body and "35" in body
    assert not override_path(server).exists()


def test_forcer_refuse_une_consigne_trop_basse(serveur):
    server, port, _ = serveur
    code, _, body = post_force(port, target="-40")
    assert code == 200 and "température attendue entre" in body
    assert not override_path(server).exists()


def test_forcer_refuse_une_duree_hors_bornes(serveur):
    server, port, _ = serveur
    for minutes in ("2", "1500"):
        code, _, body = post_force(port, minutes=minutes)
        assert code == 200 and "durée de forçage attendue entre 5 et 1440 minutes" in body
    assert not override_path(server).exists()


def test_forcer_refuse_une_valeur_illisible(serveur):
    server, port, _ = serveur
    code, _, body = post_force(port, target="chaud")
    assert code == 200 and "Température illisible" in body and "chaud" in body
    code, _, body = post_force(port, target="")
    assert code == 200 and "manquante" in body
    code, _, body = post_force(port, minutes="longtemps")
    assert code == 200 and "Durée illisible" in body
    assert not override_path(server).exists()


def test_forcer_sans_jeton_refuse(serveur):
    server, port, _ = serveur
    code, _, body = post_force(port, include_token=False)
    assert code == 403 and "rechargez la page du chauffage" in body
    assert not override_path(server).exists()


def test_forcer_avec_un_jeton_invalide_refuse(serveur):
    server, port, _ = serveur
    assert post_force(port, token="faux-jeton")[0] == 403
    assert not override_path(server).exists()


def test_forcer_d_une_autre_origine_refuse(serveur):
    server, port, _ = serveur
    assert post_force(port, origin="http://ailleurs.example")[0] == 403
    assert not override_path(server).exists()


def test_forcer_mauvais_type_de_contenu_refuse(serveur):
    server, port, _ = serveur
    assert post_force(port, content_type="application/json")[0] == 415
    assert not override_path(server).exists()


def test_forcer_refuse_si_le_chauffage_est_desactive(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    env_file.write_text(env_file.read_text(encoding="utf-8").replace("HEATER_ENABLED=1", "HEATER_ENABLED=0"), encoding="utf-8")
    code, _, body = post_force(port)
    assert code == 409 and "HEATER_ENABLED=0" in body
    assert not override_path(server).exists()
    page = call(port, path="/chauffage")[2]  # les formulaires sont retirés, le refus est expliqué
    assert "Forçage impossible" in page and 'action="/forcer"' not in page


def test_forcer_refuse_une_configuration_inutilisable(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    text = env_file.read_text(encoding="utf-8").replace("HEATER_SUPPLY_VOLTS=5\n", "")
    env_file.write_text(text, encoding="utf-8")
    code, _, body = post_force(port)
    assert code == 409 and "HEATER_SUPPLY_VOLTS" in body
    assert not override_path(server).exists()


def test_annuler_supprime_la_consigne(serveur):
    server, port, _ = serveur
    assert post_force(port)[0] == 303
    assert override_path(server).exists()
    code, headers, _ = post_cancel(port)
    assert code == 303 and headers["location"] == "/chauffage?annule=1"
    assert not override_path(server).exists()


def test_annuler_sans_consigne(serveur):
    _, port, _ = serveur
    code, headers, _ = post_cancel(port)
    assert code == 303 and headers["location"] == "/chauffage?annule=0"


def test_annuler_sans_jeton_refuse(serveur):
    server, port, _ = serveur
    assert post_force(port)[0] == 303
    assert post_cancel(port, include_token=False)[0] == 403
    assert override_path(server).exists()  # la consigne est restée en place


def test_annuler_refuse_si_le_chauffage_est_desactive(serveur):
    server, port, _ = serveur
    env_file = server.app.env_file
    env_file.write_text(env_file.read_text(encoding="utf-8").replace("HEATER_ENABLED=1", "HEATER_ENABLED=0"), encoding="utf-8")
    assert post_cancel(port)[0] == 409


def test_forcage_ecriture_refusee_explique_readwritepaths(serveur, monkeypatch):
    server, port, _ = serveur

    def refuse(_path, _data, mode=0o600):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(sensors, "write_json_atomic", refuse)
    code, _, body = post_force(port)
    assert code == 200 and "ReadWritePaths" in body
    assert not override_path(server).exists()


def test_message_apres_forcage_et_annulation(serveur):
    _, port, _ = serveur
    body = call(port, path="/chauffage?force=12%3A00%3A00")[2]
    assert "Consigne de forçage enregistrée à 12:00:00" in body
    body = call(port, path="/chauffage?annule=1")[2]
    assert "Consigne annulée" in body
    body = call(port, path="/chauffage?annule=0")[2]
    assert "Aucune consigne à annuler" in body


def test_message_de_forcage_est_echappe(serveur):
    """Le contenu de l'URL est réaffiché : il doit être échappé comme le reste."""
    _, port, _ = serveur
    body = call(port, path="/chauffage?force=%3Cscript%3Ealert(1)%3C/script%3E")[2]
    assert "&lt;script&gt;" in body and "<script>alert" not in body

