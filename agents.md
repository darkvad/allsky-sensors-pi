# Règles du projet — à lire et à respecter avant toute action

Ce fichier contient les règles **absolues** applicables à ce dépôt (`allsky-sensors-pi`).
Elles valent pour tout agent (Cline ou autre) comme pour toute personne qui intervient dessus.

## 1. Règle absolue : pas de push sans une demande expresse

**Aucun `git push` — vers `origin` ou vers n'importe quel autre remote — ne doit être exécuté sans une demande expresse de l'utilisateur, formulée explicitement dans son message en cours.**

- « Commite » ne signifie pas « pousse ». Un commit local peut être demandé ; le push doit être demandé séparément et explicitement.
- Sont également concernés, et soumis à la même autorisation expresse : `git push --force` / `--force-with-lease`, `git push --tags`, la création ou la suppression de branches distantes, et toute action qui envoie du code vers GitHub.
- Une autorisation donnée une fois ne vaut pas pour les fois suivantes : chaque push exige sa propre demande expresse.
- En cas de doute sur l'intention de l'utilisateur : **ne pas pousser**, et poser la question.
- Quand un push est explicitement demandé, ne pousser que la branche ou les refs demandées, sans élargir la portée.

## 2. Conséquences pratiques

- Le travail local (modifications, commits, tests) reste possible sans autorisation : seule la publication vers un remote est verrouillée.
- Avant toute opération destructrice sur l'historique (`reset --hard`, `clean`, `rebase`, `stash drop`), sauvegarder le travail en cours (par exemple `git stash push -u`) et vérifier l'état du dépôt (`git status`, `git log`, `git fetch`).
- Ne jamais versionner de secrets : `.env`, `allsky-sensors.env` et fichiers équivalents sont ignorés par `.gitignore`.
