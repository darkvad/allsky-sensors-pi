#!/usr/bin/env bash
# =============================================================================
# allsky-sensors — installation sur Raspberry Pi (Debian / Raspberry Pi OS)
#
#   sudo ./install.sh                     installation interactive (dépôt cloné)
#   sudo bash install.sh                  depuis la release : télécharge l'archive de la version
#   sudo ./install.sh --release 1.0.0     installe une release précise (vérifiée par SHA256)
#   sudo ./install.sh --upgrade           met à jour le programme (garde le .env)
#   sudo ./install.sh --uninstall         arrête et retire les services
#   sudo ./install.sh --dry-run ...       affiche les actions, ne modifie rien
#
# Options :
#   --user COMPTE          compte qui fait tourner les services (défaut : SUDO_USER)
#   --prefix DOSSIER       dossier du programme          (défaut : /opt/allsky-sensors)
#   --config-dir DOSSIER   dossier de configuration      (défaut : /etc/allsky-sensors)
#   --non-interactive      ne pose aucune question : lit les réponses dans l'environnement
#   --keep-config          réutilise le .env existant (nouvelle installation)
#   --no-services          n'installe/ne touche pas aux unités systemd (essais)
#   --no-packages          n'installe aucun paquet système
#   --no-boot-config       ne propose pas d'activer I2C / 1-Wire
#   --recreate-venv        recrée l'environnement Python
#   --release VERSION      installe cette release GitHub (téléchargée et vérifiée)
#   --local                exige les sources à côté du script (pas de téléchargement)
#   --no-verify            ne vérifie pas SHA256SUMS (essais seulement)
#   --dry-run              n'exécute aucune action (les questions restent posées)
#   --help
#
# Le script est idempotent : relancé, il remet les fichiers à jour, corrige les
# permissions et laisse la configuration en place (sauf demande contraire).
#
# Ce qu'il ne fait pas : installer le broker Mosquitto (il vit sur l'hôte
# d'indi-allsky), redémarrer la machine à ta place, ni exposer l'interface web
# sur Internet. Les questions sensibles (mots de passe) ne sont jamais
# journalisées ; le fichier de configuration est écrit en 600.
# =============================================================================
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

PREFIX=/opt/allsky-sensors
CONFIG_DIR=/etc/allsky-sensors
SERVICE_USER=${SUDO_USER:-}
SCRIPT_VERSION="1.0.0"  # doit rester égal à sensors.VERSION (vérifié par les tests)
RELEASE_VERSION="$SCRIPT_VERSION"
RELEASE_BASE="https://github.com/darkvad/allsky-sensors-pi/releases/download"
RELEASE_TMP=""
FORCE_RELEASE=0
VERIFY_RELEASE=1
LOCAL_ONLY=0
SERVICE_GROUP=""
MODE=install                 # install | upgrade | uninstall
INTERACTIVE=1
DRY_RUN=0
WITH_SERVICES=1
WITH_PACKAGES=1
WITH_BOOT_CONFIG=1
RECREATE_VENV=0
KEEP_CONFIG=0
NEED_REBOOT=0
ENV_TMP=""
WEB_PASSWORD_GENERATED=0

# Variables de configuration : remplies par les questions, ou par l'environnement
# (mode --non-interactive). FROM_ENV retient celles qui ont été fournies
# explicitement, pour ne pas les redemander.
CONFIG_VARS=(
    MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASSWORD MQTT_PREFIX
    ENABLED BME280_ADDRESS ALTITUDE_M DS18B20_ID DS18B20_OFFSET_C
    MLX90614_ADDRESS MLX90614_OFFSET_C INTERVAL
    SKY_TEMP_TOPIC SKY_TEMP_KEY SKY_STALE_KEY SKY_STATUS_KEY SKY_MAX_AGE
    SKY_ANTICIPATION_C SKY_CLEAR_DELTA_C SKY_CLEAR_HYSTERESIS_C
    HEATER_ENABLED HEATER_SUPPLY_VOLTS HEATER_GPIO GPIO_RESERVED HEATER_PWM_HZ
    HEATER_RESISTORS HEATER_RESISTOR_OHMS HEATER_RESISTOR_RATING_WATTS
    HEATER_MARGIN HEATER_MAX_DOME_TEMP
    WEB_BIND WEB_PORT WEB_USER WEB_PASSWORD
)

declare -A FROM_ENV=()
for __variable in "${CONFIG_VARS[@]}"; do
    if [[ -n ${!__variable-} ]]; then FROM_ENV[$__variable]=1; fi
done
unset __variable

# Les variables absentes de l'environnement partent vides : les questions poseront
# leur défaut. Celles qui ont été fournies gardent leur valeur (et ne sont pas reposées).
for __variable in "${CONFIG_VARS[@]}"; do
    if [[ -z ${FROM_ENV[$__variable]-} ]]; then
        printf -v "$__variable" '%s' ""
    fi
done
unset __variable

# Réglages du régulateur non demandés (à ajuster ensuite dans /reglages).
HEATER_KP=0.3
HEATER_KI=0.0002

# ----------------------------------------------------------------------------- messages

if [[ -t 1 ]]; then
    C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_OFF=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

log()  { printf '%s[info]%s %s\n' "$C_INFO" "$C_OFF" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[attention]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%s[erreur]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------- exécution

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# Exécute une commande en tant que compte du service (le venv lui appartient).
as_user() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '[dry-run] (en tant que %s) %s\n' "$SERVICE_USER" "$*"
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        sudo -u "$SERVICE_USER" -- "$@"
    else
        runuser -u "$SERVICE_USER" -- "$@"
    fi
}

# ----------------------------------------------------------------------------- saisie

ask() {
    local var=$1 prompt=$2 default=${3-} secret=${4-}
    local answer
    if [[ -n ${FROM_ENV[$var]-} ]]; then
        return 0  # fournie par l'environnement : on ne la redemande pas
    fi
    if [[ $INTERACTIVE -eq 0 ]]; then
        printf -v "$var" '%s' "$default"
        return 0
    fi
    if [[ $secret == secret ]]; then
        read -r -s -p "$prompt${default:+ [$default]}: " answer
        printf '\n'
    else
        read -r -p "$prompt${default:+ [$default]}: " answer
    fi
    printf -v "$var" '%s' "${answer:-$default}"
}

ask_yes_no() {
    local var=$1 prompt=$2 default=${3:-n}
    while :; do
        ask "$var" "$prompt (o/n)" "$default"
        case ${!var,,} in
            o|oui|y|yes|1) printf -v "$var" '%s' 1; return 0 ;;
            n|non|no|0)    printf -v "$var" '%s' 0; return 0 ;;
        esac
        [[ $INTERACTIVE -eq 1 ]] || die "$var doit valoir oui ou non (reçu : « ${!var} »)"
        warn "réponds par oui ou non"
        unset "$var"
    done
}

valid_number() {
    local value=${1//,/\.} min=$2 max=$3
    [[ $value =~ ^-?[0-9]+([.][0-9]+)?$ ]] || return 1
    awk -v v="$value" -v lo="$min" -v hi="$max" 'BEGIN { exit !(v + 0 >= lo + 0 && v + 0 <= hi + 0) }'
}

ask_number() {
    local var=$1 prompt=$2 min=$3 max=$4 default=${5-} value
    while :; do
        ask "$var" "$prompt" "$default"
        value=${!var}
        if valid_number "$value" "$min" "$max"; then
            printf -v "$var" '%s' "${value//,/\.}"
            return 0
        fi
        [[ $INTERACTIVE -eq 1 ]] || die "$var : nombre attendu entre $min et $max (reçu : « $value »)"
        warn "nombre attendu entre $min et $max (reçu : « $value »)"
        unset "$var"
    done
}

ask_choice() {
    local var=$1 prompt=$2 choices=$3 default=$4 choice
    while :; do
        ask "$var" "$prompt ($choices)" "$default"
        for choice in $choices; do
            [[ ${!var} == "$choice" ]] && return 0
        done
        [[ $INTERACTIVE -eq 1 ]] || die "$var : valeur attendue parmi $choices (reçu : « ${!var} »)"
        warn "valeur attendue parmi : $choices"
        unset "$var"
    done
}

ask_list() {
    local var=$1 prompt=$2 default=$3 allowed=$4 value item
    local -a items
    while :; do
        ask "$var" "$prompt" "$default"
        value=${!var// /}
        if [[ -z $value ]]; then
            [[ $INTERACTIVE -eq 1 ]] || die "$var ne peut pas être vide (attendu : $allowed)"
            warn "indique au moins un capteur parmi : $allowed"
            unset "$var"; continue
        fi
        IFS=, read -r -a items <<< "$value"
        local ok=1
        for item in "${items[@]}"; do
            [[ " $allowed " == *" $item "* ]] || ok=0
        done
        if [[ $ok -eq 1 ]]; then
            printf -v "$var" '%s' "$value"
            return 0
        fi
        [[ $INTERACTIVE -eq 1 ]] || die "$var : capteur inconnu (attendu : $allowed)"
        warn "capteur inconnu ; valeurs possibles : $allowed"
        unset "$var"
    done
}

ask_hex() {
    local var=$1 prompt=$2 default=$3 value
    while :; do
        ask "$var" "$prompt" "$default"
        value=${!var#0x}; value=${value#0X}
        if [[ $value =~ ^[0-9A-Fa-f]{2}$ ]]; then
            printf -v "$var" '0x%s' "${value^^}"
            return 0
        fi
        [[ $INTERACTIVE -eq 1 ]] || die "$var : adresse hexadécimale attendue (ex. 0x76)"
        warn "adresse attendue en hexadécimal sur deux chiffres (ex. 0x76)"
        unset "$var"
    done
}

generate_password() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(18))'
}

ask_password() {
    local var=$1 prompt=$2 minimum=$3 confirm=${4:-0} value again
    while :; do
        ask "$var" "$prompt (vide : mot de passe généré)" "" secret
        value=${!var}
        if [[ -z $value ]]; then
            value=$(generate_password)
            printf -v "$var" '%s' "$value"
            WEB_PASSWORD_GENERATED=1
            return 0
        fi
        if ((${#value} < minimum)); then
            [[ $INTERACTIVE -eq 1 ]] || die "$var doit faire au moins $minimum caractères"
            warn "au moins $minimum caractères (reçu : ${#value})"
            unset "$var"; continue
        fi
        if [[ $confirm -eq 1 && $INTERACTIVE -eq 1 && -z ${FROM_ENV[$var]-} ]]; then
            read -r -s -p "Répète le mot de passe : " again
            printf '\n'
            if [[ $again != "$value" ]]; then
                warn "les deux saisies diffèrent"
                unset "$var"; continue
            fi
        fi
        return 0
    done
}


# ----------------------------------------------------------------------------- options et contrôles

usage() {
    sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

parse_args() {
    while (($#)); do
        case $1 in
            --user) SERVICE_USER=${2-}; shift 2 ;;
            --prefix) PREFIX=${2-}; shift 2 ;;
            --config-dir) CONFIG_DIR=${2-}; shift 2 ;;
            --upgrade) MODE=upgrade; shift ;;
            --uninstall) MODE=uninstall; shift ;;
            --non-interactive) INTERACTIVE=0; shift ;;
            --keep-config) KEEP_CONFIG=1; shift ;;
            --no-services) WITH_SERVICES=0; shift ;;
            --no-packages) WITH_PACKAGES=0; shift ;;
            --no-boot-config) WITH_BOOT_CONFIG=0; shift ;;
            --recreate-venv) RECREATE_VENV=1; shift ;;
            --release) RELEASE_VERSION=${2-}; FORCE_RELEASE=1; shift 2 ;;
            --local) LOCAL_ONLY=1; shift ;;
            --no-verify) VERIFY_RELEASE=0; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) usage; exit 0 ;;
            --version) printf 'allsky-sensors installateur %s\n' "$SCRIPT_VERSION"; exit 0 ;;
            *) die "option inconnue : $1 (voir --help)" ;;
        esac
    done
    [[ $PREFIX == /* ]] || die "--prefix doit être un chemin absolu"
    [[ $CONFIG_DIR == /* ]] || die "--config-dir doit être un chemin absolu"
}

need_root() {
    if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
        die "lance ce script avec sudo : sudo ./install.sh"
    fi
}

resolve_user() {
    if [[ -z $SERVICE_USER ]]; then
        SERVICE_USER=${SUDO_USER-}
    fi
    if [[ -z $SERVICE_USER || $SERVICE_USER == root ]]; then
        if [[ $INTERACTIVE -eq 1 && $DRY_RUN -eq 0 ]]; then
            ask SERVICE_USER "Compte qui fera tourner les services (pas root)" ""
        else
            SERVICE_USER=$(id -un)
        fi
    fi
    [[ $SERVICE_USER != root ]] || die "le service ne doit pas tourner en root ; choisis un compte normal (--user)"
    if [[ $DRY_RUN -eq 0 ]] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
        die "compte inconnu : $SERVICE_USER"
    fi
    SERVICE_GROUP=$(id -gn "$SERVICE_USER" 2>/dev/null | head -n 1 || true)
    [[ -n $SERVICE_GROUP ]] || SERVICE_GROUP=$SERVICE_USER
    log "compte du service : $SERVICE_USER (groupe $SERVICE_GROUP)"
}

check_sources() {
    local missing=0 file
    for file in allsky_sensors_mqtt.py web_api.py requirements.txt \
                allsky-sensors.service allsky-sensors-web.service allsky-sensors.env.example; do
        if [[ ! -f $SCRIPT_DIR/$file ]]; then
            warn "fichier manquant dans le dépôt : $file"
            missing=1
        fi
    done
    if [[ $missing -ne 0 ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
            warn "fichiers absents (dry-run) : la release serait téléchargée"
            return 0
        fi
        die "lance le script depuis le dépôt cloné (git clone … puis sudo ./install.sh) ou depuis la release"
    fi
    command -v python3 >/dev/null 2>&1 || die "python3 est requis (sudo apt install python3)"
}

install_packages() {
    [[ $WITH_PACKAGES -eq 1 ]] || { log "paquets système : installation ignorée (--no-packages)"; return 0; }
    local packages=() package
    command -v python3 >/dev/null 2>&1 || packages+=(python3)
    python3 -c 'import venv' >/dev/null 2>&1 || packages+=(python3-venv)
    command -v i2cdetect >/dev/null 2>&1 || packages+=(i2c-tools)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    if ((${#packages[@]} == 0)); then
        log "paquets système : déjà présents"
        return 0
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        warn "apt-get introuvable : installe à la main : ${packages[*]}"
        return 0
    fi
    log "installation des paquets : ${packages[*]}"
    run apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"
}

# Récupère l'archive de la release demandée : c'est le mode « installateur seul », lancé hors
# du dépôt (téléchargé depuis la page Releases). Vérifie la somme SHA256 avant toute extraction.
fetch_release() {
    local archive url
    local -a auth=()
    summary_header "release v$RELEASE_VERSION"
    command -v curl >/dev/null 2>&1 || die "curl est requis pour télécharger la release"
    command -v tar >/dev/null 2>&1 || die "tar est requis pour extraire la release"
    if [[ -n ${GITHUB_TOKEN-} ]]; then
        auth=(--header "Authorization: Bearer $GITHUB_TOKEN")
        log "téléchargement authentifié avec GITHUB_TOKEN"
    fi
    RELEASE_TMP=$(mktemp -d)
    archive="$RELEASE_TMP/allsky-sensors-pi-$RELEASE_VERSION.tar.gz"
    url="$RELEASE_BASE/v$RELEASE_VERSION/allsky-sensors-pi-$RELEASE_VERSION.tar.gz"
    log "téléchargement : $url"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '[dry-run] curl -fsSL --max-time 300 -o %s %s\n' "$archive" "$url"
        printf '[dry-run] vérification de SHA256SUMS puis extraction dans %s\n' "$RELEASE_TMP"
        SCRIPT_DIR=$RELEASE_TMP
        return 0
    fi
    curl "${auth[@]}" --fail --location --silent --show-error --max-time 300 --retry 2 --output "$archive" "$url" \
        || die "téléchargement impossible : $url (release inexistante, dépôt privé sans GITHUB_TOKEN, ou réseau)"
    if [[ $VERIFY_RELEASE -eq 1 ]]; then
        command -v sha256sum >/dev/null 2>&1 || die "sha256sum est requis pour vérifier la release (ou --no-verify)"
        curl "${auth[@]}" --fail --location --silent --show-error --max-time 60 \
            --output "$RELEASE_TMP/SHA256SUMS" \
            "$RELEASE_BASE/v$RELEASE_VERSION/SHA256SUMS" \
            || die "SHA256SUMS introuvable pour la release v$RELEASE_VERSION (dépôt privé sans GITHUB_TOKEN ?)"
        if ! grep -qE "  allsky-sensors-pi-$RELEASE_VERSION\.tar\.gz$" "$RELEASE_TMP/SHA256SUMS"; then
            die "SHA256SUMS ne mentionne pas allsky-sensors-pi-$RELEASE_VERSION.tar.gz"
        fi
        (cd "$RELEASE_TMP" && grep -E "  allsky-sensors-pi-$RELEASE_VERSION\.tar\.gz$" SHA256SUMS | sha256sum -c -) \
            || die "somme SHA256 incorrecte : téléchargement corrompu ou release modifiée"
    else
        warn "vérification SHA256 ignorée (--no-verify)"
    fi
    if tar -tzf "$archive" | grep -qE '(^/|(^|/)\.\./)'; then
        die "archive refusée : chemin absolu ou remontée de dossier"
    fi
    tar -xzf "$archive" -C "$RELEASE_TMP" --no-same-owner --strip-components=1 \
        || die "extraction impossible : $archive"
    SCRIPT_DIR=$RELEASE_TMP
    [[ -f $SCRIPT_DIR/allsky_sensors_mqtt.py ]] || die "archive inattendue : programme absent"
    log "release v$RELEASE_VERSION extraite dans $SCRIPT_DIR"
}

summary_header() {
    printf '\n%s\n' "==============================================================================="
    printf '%s\n' " allsky-sensors — $1"
    printf '%s\n\n' "==============================================================================="
}


# ----------------------------------------------------------------------------- configuration

valid_topic() {
    local topic=$1
    [[ -n $topic && $topic != *" "* && $topic != *$'\t'* && $topic != *+* && $topic != *#* ]]
}

ask_topic() {
    local var=$1 prompt=$2 default=${3-} value
    while :; do
        ask "$var" "$prompt" "$default"
        value=${!var}
        if [[ -z $value ]] || valid_topic "$value"; then
            printf -v "$var" '%s' "$value"
            return 0
        fi
        [[ $INTERACTIVE -eq 1 ]] || die "$var : topic littéral attendu, sans espace, « + » ni « # » (reçu : « $value »)"
        warn "topic littéral attendu, sans espace, « + » ni « # »"
        unset "$var"
    done
}

ask_gpio_list() {
    local var=$1 prompt=$2 default=$3 value item
    local -a items
    while :; do
        ask "$var" "$prompt" "$default"
        value=${!var// /}
        IFS=, read -r -a items <<< "$value"
        local ok=1
        for item in "${items[@]}"; do
            [[ $item =~ ^[0-9]+$ ]] && ((item >= 0 && item <= 27)) || ok=0
        done
        [[ $ok -eq 1 ]] && { printf -v "$var" '%s' "$value"; return 0; }
        [[ $INTERACTIVE -eq 1 ]] || die "$var : numéros de GPIO (0 à 27) séparés par des virgules"
        warn "numéros de GPIO (0 à 27) séparés par des virgules"
        unset "$var"
    done
}

detect_ds18b20() {
    local found
    found=$(compgen -G "/sys/bus/w1/devices/28-*" | head -n 1 || true)
    [[ -n $found ]] && basename "$found" || true
}

collect_settings() {
    local ds18b20_detected="" reserved_item write_confirm=""
    local -a heater_reserved=()

    summary_header "configuration"
    printf '%s\n' "Entrée accepte la valeur entre crochets. Les mots de passe ne s'affichent pas."

    printf '\n%s\n' "— Broker MQTT (celui d'indi-allsky)"
    ask MQTT_HOST "Adresse du broker" "localhost"
    ask_number MQTT_PORT "Port du broker" 1 65535 "1883"
    ask MQTT_USER "Utilisateur MQTT" "allsky-pi"
    ask MQTT_PASSWORD "Mot de passe MQTT (vide : anonyme)" "" secret
    [[ -n $MQTT_PASSWORD ]] || warn "broker sans mot de passe : à éviter si Mosquitto écoute sur le réseau"
    ask MQTT_PREFIX "Préfixe des topics" "allsky"

    printf '\n%s\n' "— Capteurs de la Pi"
    ask_list ENABLED "Capteurs à lire (bme280,ds18b20,mlx90614)" "bme280,ds18b20" "bme280 ds18b20 mlx90614"
    if [[ ,$ENABLED, == *,bme280,* ]]; then
        ask_hex BME280_ADDRESS "Adresse I2C du BME280" "0x76"
    fi
    ask ALTITUDE_M "Altitude du capteur (m, vide si inconnue)" ""
    if [[ ,$ENABLED, == *,ds18b20,* ]]; then
        ds18b20_detected=$(detect_ds18b20)
        ask DS18B20_ID "Identifiant du DS18B20${ds18b20_detected:+ (détecté : $ds18b20_detected)}" "$ds18b20_detected"
        ask_number DS18B20_OFFSET_C "Correction du DS18B20 (°C)" -10 10 "0"
    fi
    if [[ ,$ENABLED, == *,mlx90614,* ]]; then
        ask_hex MLX90614_ADDRESS "Adresse I2C du MLX90614" "0x5A"
        ask_number MLX90614_OFFSET_C "Correction du MLX90614 (°C)" -20 20 "0"
    fi
    ask_number INTERVAL "Période de publication (s)" 10 3600 "60"

    printf '\n%s\n' "— Température du ciel par MQTT (SQMeter), si tu n'as pas de MLX90614 sur la Pi"
    ask_topic SKY_TEMP_TOPIC "Topic du SQMeter (vide : pas de source externe)" ""
    if [[ -n $SKY_TEMP_TOPIC ]]; then
        ask SKY_TEMP_KEY "Chemin de la température dans le JSON" "infrared.skyTemperature"
        ask SKY_STALE_KEY "Chemin du drapeau « périmé » (vide : aucun)" "dataStale"
        ask SKY_STATUS_KEY "Chemin du statut (vide : aucun)" "infrared.status"
        ask_number SKY_MAX_AGE "Fraîcheur admise (s)" 10 86400 "600"
    fi

    printf '\n%s\n' "— Anticipation par ciel dégagé (facultatif)"
    ask_number SKY_ANTICIPATION_C "Marge ajoutée quand le ciel est dégagé (°C, 0 = désactivé)" 0 10 "0"
    if valid_number "$SKY_ANTICIPATION_C" 0.0001 10; then
        ask_number SKY_CLEAR_DELTA_C "Seuil ciel dégagé : ciel − air (°C)" -60 0 "-10"
        ask_number SKY_CLEAR_HYSTERESIS_C "Hystérésis de retour (°C)" 0 10 "2"
    fi


    printf '\n%s\n' "— Chauffage anti-buée"
    ask_yes_no HEATER_ENABLED "Activer le chauffage" "n"
    if [[ $HEATER_ENABLED -eq 1 ]]; then
        ask_number HEATER_SUPPLY_VOLTS "Tension qui alimente les résistances (V)" 0.1 30 ""
        ask_gpio_list GPIO_RESERVED "GPIO déjà utilisés par d'autres matériels" "17,18,22,27"
        IFS=, read -r -a heater_reserved <<< "$GPIO_RESERVED"
        while :; do
            ask_number HEATER_GPIO "GPIO de commande du chauffage" 0 27 "23"
            local gpio_ok=1
            for reserved_item in 2 3 4 "${heater_reserved[@]}"; do
                [[ $HEATER_GPIO == "$reserved_item" ]] && gpio_ok=0
            done
            [[ $gpio_ok -eq 1 ]] && break
            [[ $INTERACTIVE -eq 1 ]] || die "GPIO $HEATER_GPIO déjà utilisé (I2C, 1-Wire ou GPIO_RESERVED)"
            warn "GPIO $HEATER_GPIO déjà utilisé (I2C, 1-Wire ou GPIO_RESERVED) : choisis-en un autre"
            unset HEATER_GPIO
        done
        ask_number HEATER_RESISTORS "Nombre de résistances" 1 20 "6"
        ask_number HEATER_RESISTOR_OHMS "Valeur d'une résistance (Ω)" 0.1 1000 "47"
        ask_number HEATER_RESISTOR_RATING_WATTS "Puissance nominale d'une résistance (W)" 0.01 100 "1"
        ask_number HEATER_MARGIN "Marge au-dessus du point de rosée (°C)" 0 10 "4"
        ask_number HEATER_MAX_DOME_TEMP "Coupure en surchauffe du dôme (°C)" 20 45 "35"
        ask_number HEATER_PWM_HZ "Fréquence PWM (Hz)" 1 1000 "100"
    fi

    printf '\n%s\n' "— Interface web"
    ask WEB_BIND "Adresse d'écoute (0.0.0.0 = réseau local, 127.0.0.1 = cette machine)" "0.0.0.0"
    [[ -n $WEB_BIND ]] || die "WEB_BIND ne peut pas être vide"
    ask_number WEB_PORT "Port d'écoute" 1 65535 "8080"
    ask WEB_USER "Utilisateur de l'interface" "admin"
    ask_password WEB_PASSWORD "Mot de passe de l'interface (12 caractères au moins)" 12 1

    printf '\n%s\n' "— Récapitulatif (sans les mots de passe)"
    printf '  broker        : %s:%s (%s, préfixe %s)\n' "$MQTT_HOST" "$MQTT_PORT" "$MQTT_USER" "$MQTT_PREFIX"
    printf '  capteurs      : %s%s\n' "$ENABLED" "${SKY_TEMP_TOPIC:+, ciel MQTT sur $SKY_TEMP_TOPIC}"
    if [[ $HEATER_ENABLED -eq 1 ]]; then
        printf '  chauffage     : oui, %s V, GPIO %s, %s × %s Ω\n' "$HEATER_SUPPLY_VOLTS" "$HEATER_GPIO" "$HEATER_RESISTORS" "$HEATER_RESISTOR_OHMS"
    else
        printf '  chauffage     : non\n'
    fi
    printf '  anticipation  : %s °C (déclenchement sous %s °C)\n' "$SKY_ANTICIPATION_C" "$SKY_CLEAR_DELTA_C"
    printf '  interface web : http://%s:%s (%s)\n' "$WEB_BIND" "$WEB_PORT" "$WEB_USER"
    ask_yes_no write_confirm "Écrire cette configuration" "o"
    [[ $write_confirm -eq 1 ]] || die "installation annulée, rien n'a été modifié"
}


# ----------------------------------------------------------------------------- écriture et validation

render_env() {
    local file=$1
    {
        printf '# allsky-sensors — configuration de production\n'
        printf '# Généré par install.sh le %s ; ce fichier est en 600, ne le versionne jamais.\n\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf '# --- Broker MQTT (celui d’indi-allsky) ---\n'
        printf 'MQTT_HOST=%s\n' "$MQTT_HOST"
        printf 'MQTT_PORT=%s\n' "$MQTT_PORT"
        printf 'MQTT_USER=%s\n' "$MQTT_USER"
        printf 'MQTT_PASSWORD=%s\n' "$MQTT_PASSWORD"
        printf 'MQTT_PREFIX=%s\n\n' "$MQTT_PREFIX"
        printf '# --- Capteurs lus sur la Pi ---\n'
        printf 'ENABLED=%s\n' "$ENABLED"
        printf 'BME280_ADDRESS=%s\n' "$BME280_ADDRESS"
        printf 'ALTITUDE_M=%s\n' "$ALTITUDE_M"
        printf 'DS18B20_ID=%s\n' "$DS18B20_ID"
        printf 'DS18B20_OFFSET_C=%s\n' "$DS18B20_OFFSET_C"
        printf 'MLX90614_ADDRESS=%s\n' "$MLX90614_ADDRESS"
        printf 'MLX90614_OFFSET_C=%s\n' "$MLX90614_OFFSET_C"
        printf 'INTERVAL=%s\n\n' "$INTERVAL"
        printf '# --- Température du ciel par MQTT (SQMeter) ---\n'
        printf 'SKY_TEMP_TOPIC=%s\n' "$SKY_TEMP_TOPIC"
        printf 'SKY_TEMP_KEY=%s\n' "$SKY_TEMP_KEY"
        printf 'SKY_STALE_KEY=%s\n' "$SKY_STALE_KEY"
        printf 'SKY_STATUS_KEY=%s\n' "$SKY_STATUS_KEY"
        printf 'SKY_MAX_AGE=%s\n\n' "$SKY_MAX_AGE"
        printf '# --- Anticipation par ciel dégagé ---\n'
        printf 'SKY_ANTICIPATION_C=%s\n' "$SKY_ANTICIPATION_C"
        printf 'SKY_CLEAR_DELTA_C=%s\n' "$SKY_CLEAR_DELTA_C"
        printf 'SKY_CLEAR_HYSTERESIS_C=%s\n\n' "$SKY_CLEAR_HYSTERESIS_C"
        printf '# --- Chauffage anti-buée ---\n'
        printf 'HEATER_ENABLED=%s\n' "$HEATER_ENABLED"
        if [[ $HEATER_ENABLED -eq 1 ]]; then
            printf 'HEATER_SUPPLY_VOLTS=%s\n' "$HEATER_SUPPLY_VOLTS"
        fi
        printf 'HEATER_GPIO=%s\n' "$HEATER_GPIO"
        printf 'GPIO_RESERVED=%s\n' "$GPIO_RESERVED"
        printf 'HEATER_PWM_HZ=%s\n' "$HEATER_PWM_HZ"
        printf 'HEATER_RESISTORS=%s\n' "$HEATER_RESISTORS"
        printf 'HEATER_RESISTOR_OHMS=%s\n' "$HEATER_RESISTOR_OHMS"
        printf 'HEATER_RESISTOR_RATING_WATTS=%s\n' "$HEATER_RESISTOR_RATING_WATTS"
        printf 'HEATER_MARGIN=%s\n' "$HEATER_MARGIN"
        printf 'HEATER_MAX_DOME_TEMP=%s\n' "$HEATER_MAX_DOME_TEMP"
        printf 'HEATER_KP=%s\n' "$HEATER_KP"
        printf 'HEATER_KI=%s\n\n' "$HEATER_KI"
        printf '# --- Interface web (réseau local uniquement ; tunnel SSH depuis l’extérieur) ---\n'
        printf 'WEB_BIND=%s\n' "$WEB_BIND"
        printf 'WEB_PORT=%s\n' "$WEB_PORT"
        printf 'WEB_USER=%s\n' "$WEB_USER"
        printf 'WEB_PASSWORD=%s\n' "$WEB_PASSWORD"
    } > "$file"
    chmod 600 "$file" 2>/dev/null || true
}

validate_env_file() {
    local file=$1 output
    if [[ $DRY_RUN -eq 1 ]]; then
        log "[dry-run] validation du fichier ignorée (aucun fichier écrit)"
        return 0
    fi
    if output=$(python3 - "$file" "$SCRIPT_DIR" 2>&1 <<'PY'
import sys

sys.path.insert(0, sys.argv[2])
import allsky_sensors_mqtt as sensors

errors = sensors.validate_env(sensors.read_env_file(sys.argv[1]))
for message in errors:
    print(message)
raise SystemExit(1 if errors else 0)
PY
    ); then
        return 0
    fi
    warn "le programme refuse cette configuration :"
    printf '%s\n' "$output" >&2
    return 1
}


# ----------------------------------------------------------------------------- installation

install_files() {
    summary_header "programme et environnement Python"
    run install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$PREFIX"
    run install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$PREFIX/doc"
    run install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$PREFIX/state"
    local file
    for file in allsky_sensors_mqtt.py web_api.py requirements.txt; do
        run install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SCRIPT_DIR/$file" "$PREFIX/$file"
    done
    for file in README.md INSTALLATION.md CABLAGE.md; do
        if [[ -f $SCRIPT_DIR/$file ]]; then
            run install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SCRIPT_DIR/$file" "$PREFIX/doc/$file"
        fi
    done
    if [[ ! -x $PREFIX/venv/bin/python || $RECREATE_VENV -eq 1 ]]; then
        run as_user python3 -m venv "$PREFIX/venv"
    else
        log "environnement Python existant conservé : $PREFIX/venv"
    fi
    run as_user "$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip || warn "mise à jour de pip ignorée"
    if ! run as_user "$PREFIX/venv/bin/python" -m pip install --quiet -r "$PREFIX/requirements.txt"; then
        die "installation des dépendances impossible : vérifie le réseau, puis relance (les fichiers déjà copiés seront remplacés)"
    fi
    ok "programme installé dans $PREFIX"
}

install_config() {
    summary_header "fichier de configuration"
    run install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CONFIG_DIR"
    local target=$CONFIG_DIR/allsky-sensors.env
    if [[ -f $target ]]; then
        if [[ $KEEP_CONFIG -eq 1 ]]; then
            log "configuration existante conservée : $target"
            return 0
        fi
        local backup=$target.bak-$(date '+%Y%m%d-%H%M%S')
        run install -m 600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$target" "$backup"
        log "ancienne configuration sauvegardée : $backup"
    fi
    run install -m 600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$ENV_TMP" "$target"
    ok "configuration écrite dans $target (600, propriétaire $SERVICE_USER)"
}

prepare_config() {
    ENV_TMP=$(mktemp)
    render_env "$ENV_TMP"
    while ! validate_env_file "$ENV_TMP"; do
        [[ $INTERACTIVE -eq 1 ]] || die "configuration refusée : rien n'a été installé"
        local again="" var
        ask_yes_no again "Recommencer la saisie" "o"
        [[ $again -eq 1 ]] || die "installation annulée, rien n'a été modifié"
        FROM_ENV=()
        for var in "${CONFIG_VARS[@]}"; do unset "$var"; done
        collect_settings
        render_env "$ENV_TMP"
    done
    ok "configuration validée par le programme"
}


# ----------------------------------------------------------------------------- services et matériel

install_units() {
    if [[ $WITH_SERVICES -eq 0 ]]; then
        log "unités systemd : ignorées (--no-services)"
        return 0
    fi
    summary_header "services systemd"
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl introuvable : unités non installées"
        return 0
    fi
    local unit source target
    for unit in allsky-sensors allsky-sensors-web; do
        source=$SCRIPT_DIR/$unit.service
        target=/etc/systemd/system/$unit.service
        if [[ $DRY_RUN -eq 1 ]]; then
            printf '[dry-run] écrire %s avec User=%s\n' "$target" "$SERVICE_USER"
            continue
        fi
        sed "s|^User=.*|User=$SERVICE_USER|" "$source" > "$target.tmp"
        if ! grep -qx "User=$SERVICE_USER" "$target.tmp"; then
            rm -f "$target.tmp"
            die "substitution de User= impossible dans $unit.service"
        fi
        install -m 644 "$target.tmp" "$target"
        rm -f "$target.tmp"
        log "unité installée : $target"
    done
    run systemctl daemon-reload
    if [[ $MODE == upgrade ]]; then
        run systemctl restart allsky-sensors allsky-sensors-web
    else
        run systemctl enable --now allsky-sensors allsky-sensors-web
    fi
    if [[ $DRY_RUN -eq 0 ]]; then
        sleep 2
        for unit in allsky-sensors allsky-sensors-web; do
            if systemctl is-active --quiet "$unit"; then
                ok "service $unit actif"
            else
                warn "service $unit inactif ; dernières lignes du journal :"
                journalctl -u "$unit" -n 15 --no-pager >&2 || true
            fi
        done
    fi
}



check_groups() {
    local group
    for group in i2c gpio spi; do
        if ! getent group "$group" >/dev/null 2>&1; then
            continue
        fi
        if [[ $DRY_RUN -eq 0 ]] && id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$group"; then
            log "groupe $group : $SERVICE_USER en est déjà membre"
            continue
        fi
        run usermod -aG "$group" "$SERVICE_USER"
        log "compte $SERVICE_USER ajouté au groupe $group (effectif après reconnexion ou redémarrage)"
    done
}

check_boot_config() {
    [[ $WITH_BOOT_CONFIG -eq 1 ]] || return 0
    local config=/boot/firmware/config.txt
    [[ -f $config ]] || config=/boot/config.txt
    if [[ ! -f $config ]]; then
        log "config.txt introuvable : active I2C et 1-Wire à la main si nécessaire"
        return 0
    fi
    local changed=0 backup
    if ! grep -q '^dtparam=i2c_arm=on' "$config"; then
        if command -v raspi-config >/dev/null 2>&1; then
            run raspi-config nonint do_i2c 0 && changed=1
        else
            backup=$config.bak-$(date '+%Y%m%d-%H%M%S')
            run cp -a "$config" "$backup"
            run sh -c "printf '\n# allsky-sensors : I2C\ndtparam=i2c_arm=on\n' >> '$config'" && changed=1
        fi
    fi
    if ! grep -q '^dtoverlay=w1-gpio' "$config"; then
        if command -v raspi-config >/dev/null 2>&1; then
            run raspi-config nonint do_onewire 0 && changed=1
        else
            backup=$config.bak-$(date '+%Y%m%d-%H%M%S')
            run cp -a "$config" "$backup"
            run sh -c "printf '\n# allsky-sensors : 1-Wire\ndtoverlay=w1-gpio\n' >> '$config'" && changed=1
        fi
    fi
    if [[ $changed -eq 1 ]]; then
        NEED_REBOOT=1
        log "I2C / 1-Wire activés : un redémarrage sera nécessaire"
    fi
}

uninstall() {
    summary_header "désinstallation"
    local confirm=""
    ask_yes_no confirm "Arrêter et retirer les services allsky-sensors" "n"
    [[ $confirm -eq 1 ]] || die "désinstallation annulée"
    if command -v systemctl >/dev/null 2>&1; then
        run systemctl disable --now allsky-sensors allsky-sensors-web || true
        run rm -f /etc/systemd/system/allsky-sensors.service /etc/systemd/system/allsky-sensors-web.service
        run systemctl daemon-reload
    fi
    if [[ -f $CONFIG_DIR/allsky-sensors.env ]]; then
        local backup=/root/allsky-sensors.env.$(date '+%Y%m%d-%H%M%S')
        run install -m 600 "$CONFIG_DIR/allsky-sensors.env" "$backup"
        log "configuration sauvegardée : $backup"
    fi
    confirm=""
    ask_yes_no confirm "Supprimer aussi $PREFIX et $CONFIG_DIR" "n"
    if [[ $confirm -eq 1 ]]; then
        case $PREFIX in
            /opt/*|/home/*|/srv/*) ;;
            *) die "refus de supprimer $PREFIX : chemin inattendu" ;;
        esac
        case $CONFIG_DIR in
            /etc/allsky-*) ;;
            *) die "refus de supprimer $CONFIG_DIR : chemin inattendu" ;;
        esac
        run rm -rf "$PREFIX" "$CONFIG_DIR"
    fi
    ok "services retirés ; le compte $SERVICE_USER et les paquets système sont conservés"
}

summary() {
    summary_header "$([[ $MODE == upgrade ]] && echo 'mise à jour terminée' || echo 'installation terminée') · v$SCRIPT_VERSION"
    if [[ -z $WEB_PORT && -f $CONFIG_DIR/allsky-sensors.env ]]; then
        WEB_PORT=$(grep -E '^WEB_PORT=' "$CONFIG_DIR/allsky-sensors.env" | tail -n 1 | cut -d= -f2 || true)
        WEB_USER=$(grep -E '^WEB_USER=' "$CONFIG_DIR/allsky-sensors.env" | tail -n 1 | cut -d= -f2 || true)
    fi
    printf '  programme     : %s\n' "$PREFIX"
    printf '  configuration : %s/allsky-sensors.env (600, %s)\n' "$CONFIG_DIR" "$SERVICE_USER"
    printf '  état          : %s/state\n' "$PREFIX"
    if [[ -n $WEB_PORT ]]; then
        printf '  interface web : http://<adresse-de-la-pi>:%s/ (%s)\n' "$WEB_PORT" "$WEB_USER"
    fi
    if [[ $WEB_PASSWORD_GENERATED -eq 1 ]]; then
        printf '\n  Mot de passe web généré : %s\n' "$WEB_PASSWORD"
        printf '  Note-le maintenant ; il est aussi dans %s/allsky-sensors.env (600).\n' "$CONFIG_DIR"
    fi
    printf '\n  Vérifications :\n'
    printf '    journalctl -u allsky-sensors -f\n'
    printf '    journalctl -u allsky-sensors-web -f\n'
    printf '    %s/venv/bin/python %s/allsky_sensors_mqtt.py --print-topics\n' "$PREFIX" "$PREFIX"
    if [[ -n $WEB_PORT ]]; then
        printf '    curl -u %s:<mot-de-passe> http://127.0.0.1:%s/sante\n' "$WEB_USER" "$WEB_PORT"
    fi
    if [[ $NEED_REBOOT -eq 1 ]]; then
        printf '\n'
        warn "un redémarrage est nécessaire (I2C, 1-Wire ou groupes) : sudo reboot"
    fi
    printf '\n'
    ok "documentation installée : $PREFIX/doc/INSTALLATION.md et $PREFIX/doc/README.md"
}

cleanup() {
    if [[ -n ${ENV_TMP-} && -f ${ENV_TMP-} ]]; then
        rm -f "$ENV_TMP"
    fi
    if [[ -n ${RELEASE_TMP-} && -d ${RELEASE_TMP-} ]]; then
        rm -rf "$RELEASE_TMP"
    fi
}
trap cleanup EXIT

main() {
    parse_args "$@"
    need_root
    resolve_user
    if [[ $MODE == uninstall ]]; then
        uninstall
        return 0
    fi
    install_packages
    if [[ $FORCE_RELEASE -eq 1 || ! -f $SCRIPT_DIR/allsky_sensors_mqtt.py ]]; then
        if [[ $LOCAL_ONLY -eq 1 ]]; then
            die "sources absentes et --local demandé : lance le script depuis le dépôt"
        fi
        fetch_release
    fi
    check_sources
    if [[ $MODE == install ]]; then
        collect_settings
        prepare_config
        install_files
        install_config
    else
        log "mise à jour : la configuration existante n'est pas modifiée"
        install_files
    fi
    check_groups
    check_boot_config
    install_units
    summary
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi

