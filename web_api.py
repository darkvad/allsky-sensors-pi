#!/usr/bin/env python3
"""Interface web de l'allsky : état des capteurs et du chauffage, et réglages.

- ``GET /`` : page d'état en français, alimentée par ``status.json`` écrit par ``allsky-sensors``
  (dossier ``STATE_DIR``, soit ``/opt/allsky-sensors/state`` sur la Pi) ; ``GET /sante`` pour la
  supervision (200 si l'état est frais, 503 sinon) ;
- ``GET /reglages`` : formulaire généré depuis ``ENV_SCHEMA``, enregistré dans ``ENV_FILE``
  (validation avant écriture, copie ``.bak``), puis appliqué par le service au tick suivant.

Authentification HTTP Basic obligatoire : ``WEB_USER`` et ``WEB_PASSWORD`` (12 caractères au moins,
sinon le serveur refuse de démarrer). Aucune dépendance nouvelle : la bibliothèque standard suffit, et
l'interface n'émet pas de JavaScript.

Lancement : /opt/allsky-sensors/venv/bin/python /opt/allsky-sensors/web_api.py
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import hmac
import html
import json
import logging
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import allsky_sensors_mqtt as sensors

log = logging.getLogger("allsky-sensors-web")

PASSWORD_MIN_LENGTH = 12  # un mot de passe d'interface exposée au réseau local, pas un code PIN
MAX_BODY_BYTES = 64 * 1024  # un formulaire de réglages tient très largement en deçà
STALE_STATUS_SECONDS = 300  # au-delà, l'état publié n'est plus frais (GET /sante répond alors 503)
REFRESH_SECONDS = 10  # la page se recharge seule (meta refresh), sans JavaScript

# Libellés des mesures publiées, dans l'ordre d'affichage (les clés inconnues passent à la fin).
READING_LABELS = {
    "outdoor_temperature": ("Température extérieure", "°C", 1),
    "outdoor_humidity": ("Humidité extérieure", "%", 0),
    "outdoor_pressure": ("Pression (niveau de la mer)", "hPa", 1),
    "outdoor_pressure_station": ("Pression mesurée", "hPa", 1),
    "outdoor_dew_point": ("Point de rosée", "°C", 1),
    "dome_temperature": ("Température du dôme", "°C", 1),
    "sky_temperature": ("Température du ciel", "°C", 1),
}

# Mesures déjà affichées en tête de tableau : ne pas les répéter dans la liste des mesures.
HEADLINE_READINGS = frozenset({"dome_temperature", "outdoor_dew_point"})

# Capteurs proposés en cases à cocher (variable ENABLED), dans l'ordre du schéma.
SENSOR_CHOICES = {
    "bme280": "BME280 — extérieur (température, humidité, pression)",
    "ds18b20": "DS18B20 — dôme",
    "mlx90614": "MLX90614 — ciel",
}


@dataclass(frozen=True)
class WebConfig:
    """Réglages d'écoute et d'authentification de l'interface.

    Le mot de passe est obligatoire et long : l'interface écoute sur le réseau local, où Basic auth
    circule en clair. Un tunnel SSH (ou un reverse proxy TLS) reste la bonne réponse hors du LAN.
    """

    bind: str = "0.0.0.0"
    port: int = 8080
    user: str = "admin"
    password: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WebConfig:
        """Construit la configuration ; lève ValueError (message en français) si elle est inutilisable."""
        bind = env.get("WEB_BIND", "0.0.0.0").strip() or "0.0.0.0"
        port_text = env.get("WEB_PORT", "8080").strip() or "8080"
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"WEB_PORT doit être un nombre entier entre 1 et 65535 (lu : {port_text!r})") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"WEB_PORT doit être compris entre 1 et 65535 (lu : {port})")
        user = env.get("WEB_USER", "admin").strip()
        if not user:
            raise ValueError("WEB_USER ne peut pas être vide")
        password = env.get("WEB_PASSWORD", "")  # pas de strip : les espaces font partie du secret
        if len(password) < PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"WEB_PASSWORD est obligatoire et doit faire au moins {PASSWORD_MIN_LENGTH} caractères "
                f"(lu : {len(password)} caractères)"
            )
        return cls(bind=bind, port=port, user=user, password=password)


def web_config(env: Mapping[str, str] | None = None) -> WebConfig:
    """Configuration de l'interface lue comme celle du service : le fichier l'emporte sur l'environnement."""
    return WebConfig.from_env(sensors.service_env(env))


# --------------------------------------------------------------------------- sécurité des formulaires


def form_token(password: str) -> str:
    """Jeton anti-CSRF des formulaires, dérivé du mot de passe : imprévisible pour un autre site.

    Il ne remplace pas le contrôle d'Origin (un navigateur en annonce toujours un sur un POST), il le
    double : une page tierce ne peut pas lire ce jeton (même origine oblige) et ne peut donc pas fabriquer
    un formulaire valide, même si le navigateur de la victime omettait l'en-tête Origin.
    """
    return hmac.new(password.encode("utf-8"), b"allsky-sensors-web/formulaire", hashlib.sha256).hexdigest()


def _origin_matches_host(source: str, host: str) -> bool:
    """Vrai si l'origine annoncée désigne la même machine que l'en-tête Host.

    Le port n'est comparé que s'il est annoncé des deux côtés : derrière un reverse proxy, le Host peut
    arriver sans port alors que le navigateur annonce l'URL publique complète.
    """
    try:
        origin = urlsplit(source)
        target = urlsplit(f"//{host}")
        origin_host, target_host = origin.hostname, target.hostname
        if not origin_host or not target_host or origin_host.lower() != target_host.lower():
            return False
        return origin.port is None or target.port is None or origin.port == target.port
    except ValueError:  # port illisible : on refuse
        return False



# --------------------------------------------------------------------------- mise en forme


def _number(value: object) -> float | None:
    """Nombre fini tiré de l'état JSON, ou None (les booléens et les textes ne sont pas des mesures)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _decimal(value: object, digits: int = 1) -> str:
    """Nombre à la française (virgule décimale) ou tiret cadratin s'il manque."""
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}".replace(".", ",")


def _temperature(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"{_decimal(number)} °C"


def _percent(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.0f} %"


def _age_text(seconds: float | None) -> str:
    """Âge d'une mesure en français (« il y a 12 s », « il y a 3 min »)."""
    if seconds is None:
        return "inconnu"
    total = max(int(seconds), 0)
    if total < 90:
        return f"il y a {total} s"
    minutes = total // 60
    if minutes < 90:
        return f"il y a {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"il y a {hours} h {minutes:02d} min"


def _remaining_text(seconds: float | None) -> str:
    """Temps restant avant l'expiration d'une consigne forcée."""
    if seconds is None:
        return "—"
    total = max(int(seconds), 0)
    if total <= 0:
        return "expirée"
    if total < 60:
        return "moins d'une minute"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


# --------------------------------------------------------------------------- pages HTML

STYLE = """
:root { color-scheme: dark light; }
body { font-family: system-ui, sans-serif; margin: 1.5rem auto; max-width: 46rem; padding: 0 1rem; line-height: 1.5; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; }
p.discret { color: #888; font-size: .9rem; margin-top: 0; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #8884; }
th { width: 45%; font-weight: 600; }
td.mode { font-weight: 700; }
p.alerte { border-left: 4px solid #c33; padding: .5rem .75rem; background: #c331; }
p.alerte ul { margin: .3rem 0 0 1rem; }
p.ok { border-left: 4px solid #2a2; padding: .5rem .75rem; background: #2a22; }
code { background: #8882; padding: 0 .2rem; border-radius: .2rem; }
fieldset { border: 1px solid #8884; border-radius: .4rem; margin: 1rem 0; padding: .5rem .9rem; }
legend { font-weight: 700; padding: 0 .3rem; }
div.champ { margin: .8rem 0; }
div.champ label, p.etiquette { display: block; font-weight: 600; margin-bottom: .2rem; }
input[type=text], input[type=password], select { width: 100%; max-width: 26rem; padding: .35rem .45rem; font: inherit; }
label.case { display: block; font-weight: 400; }
p.aide { color: #888; font-size: .85rem; margin: .2rem 0 0; }
span.badge { font-size: .75rem; font-weight: 400; border: 1px solid #8886; border-radius: .6rem; padding: 0 .4rem; color: #888; }
button { font: inherit; padding: .45rem 1rem; margin-top: .5rem; }
"""


def _row(label: str, value: str, css: str = "") -> str:
    """Ligne de tableau, libellé et valeur échappés (aucune donnée de configuration n'est interprétée)."""
    attribute = f' class="{css}"' if css else ""
    return f"<tr><th>{html.escape(label)}</th><td{attribute}>{html.escape(value)}</td></tr>"


def _status_rows(status: Mapping[str, object], now: float) -> str:
    """Lignes du tableau d'état : chauffage, températures, fraîcheur des données."""
    mode = str(status.get("mode", "auto"))
    rows = [_row("Mode", "Forcé" if mode == "force" else "Automatique", "mode")]
    rows.append(_row("Cible du chauffage", _temperature(status.get("target_c"))))
    forced = _number(status.get("override_target_c"))
    expires = _number(status.get("override_expires_at"))
    if forced is not None and expires is not None:
        detail = "expirée" if expires <= now else f"encore {_remaining_text(expires - now)}"
        rows.append(_row("Consigne forcée", f"{_decimal(forced)} °C, {detail}"))
    rows.append(_row("Température du dôme", _temperature(status.get("dome_c"))))
    rows.append(_row("Point de rosée", _temperature(status.get("dew_c"))))
    rows.append(_row("Chauffage appliqué", _percent(status.get("duty_pct"))))
    updated = _number(status.get("updated_at"))
    rows.append(_row("Dernière mise à jour", _age_text(None if updated is None else now - updated)))
    settings = _number(status.get("settings_mtime"))
    if settings is not None:
        rows.append(_row("Réglages modifiés", _age_text(now - settings)))
    pid = status.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        rows.append(_row("Processus (PID)", str(pid)))
    return "".join(rows)


def _reading_rows(readings: Mapping[str, object]) -> str:
    """Mesures retenues par le service, avec leur unité ; une clé inconnue est affichée telle quelle."""
    known = [key for key in READING_LABELS if key in readings and key not in HEADLINE_READINGS]
    unknown = sorted(key for key in readings if key not in READING_LABELS)
    rows = []
    for key in known + unknown:
        label, unit, digits = READING_LABELS.get(key, (key, "", 1))
        value = _decimal(readings[key], digits)
        rows.append(_row(label, value if value == "—" or not unit else f"{value} {unit}"))
    return "".join(rows)


def _notes_html(status: Mapping[str, object]) -> str:
    """Avertissements affichés sous le tableau : configuration refusée, capteurs muets."""
    notes = []
    error = status.get("config_error")
    if error:
        notes.append(f'<p class="alerte">Configuration refusée : {html.escape(str(error))}</p>')
    unavailable = status.get("sensors_unavailable")
    if isinstance(unavailable, list) and unavailable:
        names = ", ".join(str(name) for name in unavailable)
        notes.append(f'<p class="alerte">Capteurs indisponibles : {html.escape(names)}</p>')
    return "".join(notes)


def render_page(status: Mapping[str, object] | None, status_file: Path, now: float, refresh: int = REFRESH_SECONDS) -> str:
    """Page d'état en HTML (UTF-8, sans JavaScript) — aucun secret n'y figure."""
    if status is None:
        content = (
            '<p class="alerte">Aucun état publié : le service n\'a pas encore écrit '
            f"<code>{html.escape(str(status_file))}</code>, ou le fichier est illisible.</p>"
        )
    else:
        readings = status.get("readings")
        table = _status_rows(status, now)
        if isinstance(readings, Mapping):
            table += _reading_rows(readings)
        content = f"<table>{table}</table>" + _notes_html(status)
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh)}">
<title>Allsky — capteurs et chauffage</title>
<style>{STYLE}</style>
</head>
<body>
<header>
<h1>Allsky — capteurs et chauffage</h1>
<p class="discret">État publié par <code>allsky-sensors</code> · <a href="/reglages">réglages</a> · <a href="/sante">/sante</a> · <a href="/">rafraîchir</a></p>
</header>
<main>
{content}
</main>
<footer><p class="discret">Page rafraîchie toutes les {int(refresh)} s.</p></footer>
</body>
</html>
"""


def render_message(title: str, text: str = "") -> str:
    """Page courte pour les erreurs et les réponses du socle (le texte est échappé ici)."""
    body = f"<p>{html.escape(text)}</p>" if text else ""
    return f"""<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body>
</html>
"""


# --------------------------------------------------------------------------- formulaire de réglages


def _list_items(text: str) -> list[str]:
    """Éléments d'une liste séparée par des virgules (même règle que le service)."""
    return [word.strip() for word in text.split(",") if word.strip()]


def _effective(env: Mapping[str, str], param: sensors.Param) -> str:
    """Valeur effective d'une variable : celle du fichier, sinon le défaut du schéma."""
    return env.get(param.name, param.default or "")


def _canonical(param: sensors.Param, value: str) -> object:
    """Forme comparable : « 4 » et « 4,0 » sont la même valeur, comme deux listes dans un autre ordre."""
    text = value.strip()
    if not text:
        return ""
    if param.kind in ("int", "float"):
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return text  # invalide : considéré comme différent, la validation le dira
    if param.kind == "hex":
        try:
            return int(text, 16)
        except ValueError:
            return text
    if param.kind == "ints":
        try:
            return tuple(int(word) for word in _list_items(text))
        except ValueError:
            return text
    if param.kind == "sensors":
        return tuple(sorted(_list_items(text)))
    return text


def collect_changes(params: Mapping[str, list[str]], env: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Formulaire -> (changements à écrire, valeurs à réafficher après un refus).

    Un champ absent du formulaire vaut vide ; un secret laissé vide signifie « inchangé » (sa valeur
    n'est jamais renvoyée au navigateur). Les clés hors schéma sont ignorées : on ne peut pas écrire
    une variable arbitraire dans le fichier du service.
    """
    submitted: dict[str, str] = {}
    changes: dict[str, str] = {}
    for param in sensors.ENV_SCHEMA:
        current = _effective(env, param)
        if param.kind == "sensors":
            value = ",".join(name for name in SENSOR_CHOICES if name in params.get(param.name, []))
        else:
            value = params.get(param.name, [""])[0].strip()
        if param.kind == "secret":
            submitted[param.name] = ""  # un secret n'est jamais réaffiché
            if not value:
                continue  # vide = inchangé
        else:
            submitted[param.name] = value
        if _canonical(param, value) != _canonical(param, current):
            changes[param.name] = value
    return changes, submitted


def _field(param: sensors.Param, value: str) -> str:
    """Un champ du formulaire : libellé, unité, aide et mention « au redémarrage » si nécessaire."""
    field_id = f"champ-{param.name}"
    label = param.label + (f" ({param.unit})" if param.unit else "")
    badge = ' <span class="badge">au redémarrage</span>' if param.restart else ""
    if param.kind == "sensors":
        chosen = set(_list_items(value))
        boxes = "".join(
            f'<label class="case"><input type="checkbox" name="{param.name}" value="{name}"'
            f'{" checked" if name in chosen else ""}> {html.escape(text)}</label>'
            for name, text in SENSOR_CHOICES.items()
        )
        control = f'<p class="etiquette">{html.escape(label)}{badge}</p>{boxes}'
    elif param.kind == "flag":
        options = "".join(
            f'<option value="{option}"{" selected" if option == value else ""}>{html.escape(text)}</option>'
            for option, text in (("1", "activé"), ("0", "désactivé"))
        )
        control = (
            f'<label for="{field_id}">{html.escape(label)}{badge}</label>'
            f'<select id="{field_id}" name="{param.name}">{options}</select>'
        )
    elif param.kind == "secret":
        control = (
            f'<label for="{field_id}">{html.escape(label)}{badge}</label>'
            f'<input id="{field_id}" type="password" name="{param.name}" value="" autocomplete="new-password"'
            ' placeholder="vide : inchangé (la valeur n\'est jamais affichée)">'
        )
    else:
        hint = ' inputmode="decimal"' if param.kind in ("int", "float") else ""
        control = (
            f'<label for="{field_id}">{html.escape(label)}{badge}</label>'
            f'<input id="{field_id}" type="text" name="{param.name}" value="{html.escape(value)}"{hint}>'
        )
    help_text = f'<p class="aide">{html.escape(param.help)}</p>' if param.help else ""
    return f'<div class="champ">{control}{help_text}</div>'


def _write_error(error: Exception, env_file: Path) -> str:
    """Message d'écriture lisible : les deux pièges connus (bac à sable systemd, droits du dossier)."""
    if isinstance(error, OSError) and error.errno == errno.EROFS:
        return (
            f"système de fichiers en lecture seule ({error}) : le dossier {env_file.parent} doit figurer dans "
            "ReadWritePaths= de allsky-sensors-web.service, en tant que dossier et non en tant que fichier"
        )
    if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM):
        return (
            f"droits insuffisants sur {env_file.parent} ({error}) : ce dossier doit appartenir au compte du "
            "service allsky-sensors-web (mode 750)"
        )
    return f"écriture impossible : {error}"


def render_settings(
    env_file: Path,
    env: Mapping[str, str],
    *,
    errors: Sequence[str] = (),
    notice: str | None = None,
    submitted: Mapping[str, str] | None = None,
    token: str = "",
    now: float | None = None,
) -> str:
    """Formulaire de réglages généré depuis ENV_SCHEMA : sections, unités, aide, secrets masqués."""
    values = {param.name: _effective(env, param) for param in sensors.ENV_SCHEMA}
    values.update(submitted or {})  # après un refus : on réaffiche la saisie, pas le fichier
    sections: dict[str, list[str]] = {}
    for param in sensors.ENV_SCHEMA:
        sections.setdefault(param.section, []).append(_field(param, values[param.name]))
    fieldsets = "".join(
        f"<fieldset><legend>{html.escape(section)}</legend>{''.join(fields)}</fieldset>" for section, fields in sections.items()
    )
    messages = ""
    if errors:
        items = "".join(f"<li>{html.escape(message)}</li>" for message in errors)
        messages += f'<div class="alerte"><p>Rien n\'a été enregistré :</p><ul>{items}</ul></div>'
    if notice:
        messages += f'<p class="ok">{html.escape(notice)}</p>'
    try:
        age = _age_text((time.time() if now is None else now) - env_file.stat().st_mtime)
    except OSError:
        age = "inconnu"
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allsky — réglages</title>
<style>{STYLE}</style>
</head>
<body>
<header>
<h1>Réglages</h1>
<p class="discret"><code>{html.escape(str(env_file))}</code> · modifié {html.escape(age)} · <a href="/">état</a></p>
</header>
<main>
{messages}
<form method="post" action="/reglages">
<input type="hidden" name="_jeton" value="{html.escape(token)}">
{fieldsets}
<button type="submit">Enregistrer</button>
<p class="aide">Le service relit ce fichier au tick suivant (10 s). Les valeurs marquées « au redémarrage »
ne changent qu'après un redémarrage du service concerné.</p>
</form>
</main>
</body>
</html>
"""




# --------------------------------------------------------------------------- application


@dataclass
class WebApp:
    """Ce dont les requêtes ont besoin : réglages, fichiers d'état et de configuration, seuils."""

    config: WebConfig
    status_file: Path
    env_file: Path | None = None
    refresh: int = REFRESH_SECONDS
    stale_after: float = STALE_STATUS_SECONDS

    def read_status(self) -> dict[str, object] | None:
        """État publié par le service, ou None s'il est absent (service arrêté, fichier abîmé)."""
        return sensors.read_status(self.status_file)

    def read_env(self) -> dict[str, str]:
        """Variables effectives : environnement du service, puis le fichier de réglages par-dessus."""
        if self.env_file is None:
            return dict(os.environ)
        return sensors.service_env({"ENV_FILE": str(self.env_file)})

    def health(self) -> tuple[int, dict[str, object]]:
        """État de santé pour la supervision : 200 si l'état publié est frais, 503 sinon."""
        status = self.read_status()
        updated = _number(status.get("updated_at")) if status else None
        age = None if updated is None else time.time() - updated
        healthy = age is not None and age <= self.stale_after
        payload: dict[str, object] = {
            "etat": "ok" if healthy else "degrade",
            "mise_a_jour_s": None if age is None else round(age, 1),
            "mode": status.get("mode") if status else None,
            "config_error": status.get("config_error") if status else None,
            "pid": status.get("pid") if status else None,
            "fichier": str(self.status_file),
        }
        return (200 if healthy else 503), payload


class Handler(BaseHTTPRequestHandler):
    """Routes du socle : ``GET /`` (état) et ``GET /sante`` ; les POST sont contrôlés, sans action."""

    server_version = "allsky-sensors-web"
    sys_version = ""  # ne révèle pas la version de Python dans l'en-tête Server

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    # --- réponses

    def _send(self, code: int, body: str | bytes, content_type: str, extra: Mapping[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")  # « no-referrer » ferait sérialiser Origin à null
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _page(self, code: int, title: str, text: str = "", extra: Mapping[str, str] | None = None) -> None:
        self._send(code, render_message(title, text), "text/html; charset=utf-8", extra)

    # --- contrôles

    def _authorized(self) -> bool:
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "basic" or not token.strip():
            return False
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        # Comparaison en temps constant, en octets : un mot de passe accentué reste valable.
        expected = self.app.config
        return hmac.compare_digest(user.encode("utf-8"), expected.user.encode("utf-8")) and hmac.compare_digest(
            password.encode("utf-8"), expected.password.encode("utf-8")
        )

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._page(
            401,
            "Authentification requise",
            "Identifiants absents ou incorrects.",
            {"WWW-Authenticate": 'Basic realm="allsky-sensors", charset="UTF-8"'},
        )
        return False

    def _same_origin(self) -> bool:
        """Origine du POST, quand le navigateur l'annonce (Origin et/ou Referer).

        Deux cas se présentent : l'en-tête manque, ou il vaut « null » (origine masquée : politique de
        confidentialité, iframe en bac à sable). La requête n'est alors pas refusée ici : c'est le jeton
        caché du formulaire qui fait foi. Une origine annoncée et étrangère, elle, est refusée.
        """
        announced = [
            value
            for value in (self.headers.get("Origin", "").strip(), self.headers.get("Referer", "").strip())
            if value and value != "null"
        ]
        if not announced:
            return True
        host = self.headers.get("Host", "").strip()
        return all(_origin_matches_host(value, host) for value in announced)

    def _require_origin(self) -> bool:
        if self._same_origin():
            return True
        log.warning(
            "POST refusé (%s) : Origin=%r Referer=%r Host=%r",
            urlsplit(self.path).path,
            self.headers.get("Origin"),
            self.headers.get("Referer"),
            self.headers.get("Host"),
        )
        self._page(403, "Requête refusée", "En-tête Origin (ou Referer) d'une autre origine.")
        return False

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._page(411, "Longueur requise", "Content-Length manquant ou invalide.")
            return None
        if length < 0:
            self._page(400, "Requête invalide", "Content-Length négatif.")
            return None
        if length > MAX_BODY_BYTES:
            self._page(413, "Requête trop volumineuse", f"Corps limité à {MAX_BODY_BYTES} octets.")
            return None
        return self.rfile.read(length) if length else b""

    # --- enregistrement des réglages

    def _redirect(self, location: str) -> None:
        """303 après un POST réussi : le navigateur recharge la page en GET (pas de renvoi du formulaire)."""
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})

    def _settings_page(
        self,
        *,
        errors: Sequence[str] = (),
        submitted: Mapping[str, str] | None = None,
        notice: str | None = None,
    ) -> None:
        env_file = self.app.env_file
        if env_file is None or not env_file.is_file():
            self._page(
                409,
                "Réglages indisponibles",
                "Aucun fichier de réglages à modifier : ENV_FILE n'est pas défini ou le fichier est introuvable.",
            )
            return
        env = self.app.read_env()
        token = form_token(self.app.config.password)
        body = render_settings(env_file, env, errors=errors, notice=notice, submitted=submitted, token=token)
        self._send(200, body, "text/html; charset=utf-8")

    def _save_settings(self, body: bytes) -> None:
        env_file = self.app.env_file
        if env_file is None or not env_file.is_file():
            self._page(
                409,
                "Réglages indisponibles",
                "Aucun fichier de réglages à modifier : ENV_FILE n'est pas défini ou le fichier est introuvable.",
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self._page(415, "Format non pris en charge", "Le formulaire doit être envoyé en application/x-www-form-urlencoded.")
            return
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            self._page(400, "Requête invalide", "Corps du formulaire en UTF-8 attendu.")
            return
        params = parse_qs(text, keep_blank_values=True)
        expected = form_token(self.app.config.password)
        received = params.get("_jeton", [""])[0]
        if not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
            log.warning("POST refusé (%s) : jeton de formulaire absent ou invalide", urlsplit(self.path).path)
            self._page(403, "Requête refusée", "Formulaire expiré ou d'une autre page : rechargez la page des réglages.")
            return
        env = self.app.read_env()
        changes, submitted = collect_changes(params, env)
        if not changes:
            self._redirect("/reglages?inchange=1")
            return
        errors = sensors.validate_env({**env, **changes})
        if errors:
            self._settings_page(errors=errors, submitted=submitted)  # rien n'est écrit
            return
        try:
            sensors.update_env_file(env_file, changes)
        except (OSError, ValueError) as error:
            log.error("réglages non enregistrés dans %s : %s", env_file, error)
            self._settings_page(errors=[_write_error(error, env_file)], submitted=submitted)
            return
        restart = any(param.restart for param in sensors.ENV_SCHEMA if param.name in changes)
        # Les noms des variables suffisent au journal : jamais les valeurs (mots de passe MQTT ou web).
        log.info("réglages enregistrés dans %s : %s", env_file, ", ".join(sorted(changes)))
        location = f"/reglages?applique={quote(time.strftime('%H:%M:%S'))}"
        self._redirect(location + ("&redemarrage=1" if restart else ""))


    # --- routes

    def do_GET(self) -> None:  # nom imposé par http.server
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            status = self.app.read_status()
            body = render_page(status, self.app.status_file, time.time(), self.app.refresh)
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/reglages":
            self._settings_page(notice=self._settings_notice())
        elif path == "/sante":
            code, payload = self.app.health()
            self._send(code, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "application/json; charset=utf-8")
        else:
            self._page(404, "Page inconnue", f"Adresse inconnue : {path}")

    def _settings_notice(self) -> str | None:
        """Message affiché après la redirection 303 (enregistrement, ou absence de changement)."""
        query = parse_qs(urlsplit(self.path).query)
        if "applique" in query:
            notice = f"Réglages enregistrés à {query['applique'][0]} : le service les applique au tick suivant (10 s)."
            if query.get("redemarrage", ["0"])[0] == "1":
                notice += " Une valeur marquée « au redémarrage » attend un redémarrage pour agir."
            return notice
        if "inchange" in query:
            return "Aucune modification : le fichier n'a pas été réécrit."
        return None

    def do_POST(self) -> None:
        # L'ordre compte : identifiants, puis origine (anti-CSRF), puis taille du corps.
        if not self._require_auth() or not self._require_origin():
            return
        body = self._read_body()
        if body is None:
            return
        path = urlsplit(self.path).path
        if path == "/reglages":
            self._save_settings(body)
        else:  # le forçage du chauffage se branchera ici à l'étape 6
            self._page(404, "Action inconnue", "Aucune action n'est encore disponible sur cette page.")

    def log_message(self, format: str, *args: object) -> None:  # signature imposée par http.server
        log.info("%s %s", self.address_string(), format % args)


class WebServer(ThreadingHTTPServer):
    """Serveur multi-thread : une requête lente ne bloque pas la page d'état."""

    daemon_threads = True

    def __init__(self, app: WebApp) -> None:
        self.app = app
        try:
            super().__init__((app.config.bind, app.config.port), Handler)
        except OSError as error:
            raise OSError(f"écoute impossible sur {app.config.bind}:{app.config.port} : {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--bind", help="adresse d'écoute (défaut : WEB_BIND, sinon 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port d'écoute (défaut : WEB_PORT, sinon 8080)")
    parser.add_argument("--status-file", type=Path, help="état publié par le service (défaut : STATE_DIR/status.json)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = sensors.service_env()
    if args.bind:
        env["WEB_BIND"] = args.bind
    if args.port is not None:
        env["WEB_PORT"] = str(args.port)
    try:
        config = WebConfig.from_env(env)
    except ValueError as error:
        log.error("%s", error)
        return 2
    status_file = args.status_file or sensors.status_path(env)
    env_file = sensors.configured_env_file()
    if env_file is None:
        log.warning("aucun fichier de réglages suivi (ENV_FILE non défini) : la page /reglages répondra 409")
    try:
        server = WebServer(WebApp(config=config, status_file=status_file, env_file=env_file))
    except OSError as error:
        log.error("%s", error)
        return 2
    log.info("interface web à l'écoute sur http://%s:%s (utilisateur %s)", config.bind, config.port, config.user)
    log.info("état lu dans %s ; réglages dans %s", status_file, env_file or "aucun")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("arrêt demandé")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())



