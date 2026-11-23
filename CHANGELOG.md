# Journal des versions

## 1.0.0 — 23/11/2026

Première version stable.

- capteurs BME280 (extérieur), DS18B20 (dôme) et MLX90614 (ciel), point de rosée et pression au
  niveau de la mer ;
- chauffage anti-buée régulé (PI), plafond de dérating, coupures de sécurité, chien de garde systemd ;
- forçage du chauffage par fichier, par MQTT (`allsky/heater_command`) et par l'interface web, avec
  jeton anti-CSRF et expiration ;
- interface web : état, `/sante`, réglages à chaud, forçage, `/chauffage` ;
- rechargement à chaud de la configuration et `status.json` pour l'interface ;
- température du ciel par MQTT (SQMeter) et anticipation par ciel dégagé ;
- `install.sh` interactif : validation du `.env` avant écriture, permissions, unités systemd,
  groupes, I2C/1-Wire ; `--upgrade`, `--uninstall`, `--dry-run`, `--non-interactive` ;
- installation depuis les releases GitHub : archive et `SHA256SUMS` vérifiés ;
- 271 tests (`pytest`) et `ruff`.
