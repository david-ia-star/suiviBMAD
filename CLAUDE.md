# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ce que c'est

Un tableau de bord de suivi BMAD (6.13) : `suivi.py` est un serveur HTTP local (stdlib seulement) qui relit à chaque requête les fichiers BMAD d'un autre projet et sert une page HTML unique. Le projet suivi est **en lecture seule** : ne jamais y écrire. L'interface, les commentaires et les échanges avec l'utilisateur sont en français.

## Commandes

Il n'y a pas de `python3` système : tout passe par `uv` (`suivi.py` porte un en-tête PEP 723, `requires-python >=3.11`, aucune dépendance).

```
./suivi.sh start [--port N] [--host H] [--root CHEMIN]   # défauts : suivi.toml, puis 127.0.0.1:8765
./suivi.sh stop | restart | status [--json] | url | logs [-n N] [-f]
uv run suivi.py --port 8765                               # en avant-plan
```

- **Réglages** (`root`, `port`, `host`), par priorité décroissante : options `--root/--port/--host` → variables `SUIVI_ROOT/PORT/HOST` (lues par `suivi.sh` seulement) → `suivi.toml` à côté de `suivi.py` → port 8765 et hôte 127.0.0.1. `root` n'a **pas** de défaut codé en dur : sans lui, `suivi.py` s'arrête avec un message. Un `root` relatif du fichier de conf est résolu depuis le dossier de ce fichier. `suivi.sh` relit `port` et `host` dans `suivi.toml` avec un `sed` (`conf_get`) : n'y mettre que des clés simples `clé = valeur` sur une ligne ; `root` passe par `suivi.py`. Le `suivi.toml` du dépôt contient le chemin du poste de développement.
- `suivi.sh` est non interactif et idempotent ; état, PID et journal dans `.run/`. Codes de sortie : 0 ok, 1 échec de démarrage, 2 usage, 3 arrêté (`status`, `url`), 4 déjà lancé avec d'autres réglages, 5 port pris par un autre programme. `./suivi.sh help` détaille le reste.
- `GET /healthz` répond `ok` ; `GET /` régénère la page.
- Il n'y a ni suite de tests ni linter. Vérifier en lançant le serveur puis `curl`, ou avec le skill `dev-browser` (captures, clics). Pour tester le côté dynamique, copier le projet suivi dans un dossier temporaire et passer `--root` dessus, puis modifier la copie sans redémarrer.
- Le serveur ne recharge pas son code : après une modification de `suivi.py` (CSS et JS compris), `./suivi.sh restart` (reprend les réglages précédents). Les données du projet suivi, elles, sont relues à chaque requête.
- **Sur ce poste**, le service est géré par systemd utilisateur (`~/.config/systemd/user/suivi-bmad.service`, démarré au démarrage de WSL) : `suivi.sh` n'y fonctionne pas (port déjà pris), utiliser `systemctl --user {start|stop|restart|status} suivi-bmad` et `journalctl --user -u suivi-bmad`.
- Pour tester sans gêner l'instance habituelle (état partagé dans `.run/`, port 8765 souvent pris), lancer `uv run suivi.py --port 87xx` au premier plan, ou travailler sur une copie du dossier ; l'arrêter par son PID (`ss -ltnp`).
- Piège Bash : `pkill -f "suivi.py"` dans un appel d'outil tue le shell de l'outil (code 144). Utiliser `./suivi.sh stop`, ou `kill -TERM -- -PGID`. Les vérifications de port par `/dev/tcp` peuvent bloquer ici : utiliser `ss`.

## Architecture

Tout est dans `suivi.py`, en trois couches enchaînées à chaque requête : `load_all(root)` → un gros `dict` → `render(data)` → HTML. Aucune donnée du projet suivi n'est écrite dans le script.

**Lecture (`load_all`)** — conventions BMAD attendues sous `<root>/tickets/` :
- `tickets.toml` : `[[epic]]` (`id, slug, title, description, covers, after=[{epic, needs}]`).
- `<slug>/tickets.toml` : `[[entry]]` = les stories (`id, title, description, verify, covers, after, hitl, risk, references`). `after` mêle ids locaux (`1`) et stories d'autres epics (`"1.5"`).
- `<slug>/<slug>.md` : frontmatter + sections (`Outcome`, `Done when`, `Requirements`…), et `<slug>-retrospective.md` éventuel.
- `<slug>/tickets/*.md` : un plan par story, relié à son entry par `ticket == entry.id`. Le statut de la story vient du `status` du plan (`done`, `built`) ; pas de plan = `todo`.
- `deferred-work.md` (travaux reportés) et le `.md` d'initiative à la racine (`find_initiative` : `<dossier>.md`, sinon le `.md` racine avec `type: initiative`).
- Le frontmatter est lu par un mini-parseur maison (`parse_frontmatter`), pas de PyYAML. Un fichier illisible ajoute un avertissement affiché dans la page au lieu de planter.

**Dérivés calculés dans `load_all`** :
- `data["reqs"]` (`build_requirements`) : exigences R et contraintes C de l'initiative, avec leurs sources (`parse_source_refs` : « notes §Score, architecture AD-9 »…) résolues vers les documents du projet et leur section (`section_excerpt`), les epics qui les précisent ou les couvrent, et les stories qui les couvrent.
- `data["links"]` (`compute_epic_links`) : dépendances entre epics = les `after` du `tickets.toml` racine **plus** celles révélées par les `after` « E.S » des stories. Le `after:` du frontmatter de l'epic n'est pas lu.

**Personas** (`PERSONAS`, `persona_for`, `build_crew`, `render_crew`) : les 6 agents BMAD (Mary, John, Sofia, Winston, Amelia, Sally). Rien dans les fichiers du projet ne nomme l'agent actif : on le **déduit** de l'emplacement du fichier le plus récemment modifié (`architecture-*` → Winston, `ux-*` → Sally, `notes-*` → Mary, plans de story, `plan-*`, rétrospectives, `walkthrough-*` → Amelia, le reste → Sofia). Sous 15 min (`LIVE_SECONDS`) : « Au travail » ; au-delà : « Dernière activité ». Les images viennent de `images/` (à côté de `suivi.py`, hors du projet suivi), retrouvées par le prénom dans le nom de fichier et servies par `GET /personas/<id>` (ETag, 304). Pas d'image = initiale en repli.

**Rendu (`render*`)** : HTML construit en f-strings, CSS et JS embarqués dans les constantes `CSS` et `JS`. Pas de framework, pas de réseau.
- Epics et stories sont des `<details>` ; l'état d'ouverture des epics est dans `sessionStorage`, les filtres dans `localStorage`.
- Le panneau d'exigence est un `<dialog>` alimenté par un JSON embarqué (`#reqs-data`) ; les tags `R…` sont des `<button class="tag req" data-req>`. `[data-jump]` ouvre l'epic ou la story visée.
- Le graphe des dépendances est dessiné côté navigateur : le serveur émet des `<i hidden class="dedge" data-from data-to data-state>` et le JS trace les courbes SVG entre les nœuds `.dnode`.

**Synthèse IA** (`story_brief`, `stream_synthese`, route `GET /api/synthese/<epic>-<story>`) : au dépliage d'une story, le bloc « Synthèse IA » (juste au-dessus de « Description ») se remplit en direct. Le JS appelle l'endpoint ; le serveur relit les fichiers, assemble le contexte de la story (epic, description, vérification, exigences couvertes, dépendances, sections du plan) et interroge **Ollama local** (`POST {ia_url}/api/chat`, `stream: true`, **`think: false`** : sans cela Qwen réfléchit longtemps avant le premier mot). Réponse en NDJSON `{"t":…}` / `{"done":true}` / `{"err":…}`. **Rien n'est enregistré** : ni disque ni cache serveur ; le JS garde le texte dans une `Map` en mémoire de la page (perdue au F5). Replier la story en cours de génération annule le `fetch`, le serveur ferme alors la connexion vers Ollama. Réglages dans `suivi.toml` (clés simples) : `ia_url` (défaut `http://localhost:11434`), `ia_model` (défaut `qwen3.8-27b-96k`), `ia_timeout` (120 s). Ollama est facultatif : sans lui le bloc affiche une erreur avec « Réessayer ». Le contexte du projet suivi ne part que vers `ia_url` : le garder local.

## Pièges d'écriture

- `JS` est une chaîne **brute** (`r"""`) : y écrire les regex et échappements JS tels quels. `CSS` et le HTML sont des chaînes normales.
- Rester compatible Python 3.11 : pas de guillemets identiques ni de backslash dans les expressions `{}` des f-strings.
- Le JS ne reçoit jamais de HTML construit à partir de données : il remplit le panneau avec `textContent`. Garder cette règle pour tout nouveau contenu issu des fichiers du projet.
- Couleurs : « Développée » (statut `built`) est en vert clair (`--built`), distinct du vert « Terminé » (`--done`) ; le bleu sert au statut `in-review` (pill et ligne de story, via `.pill.info`/`--info-bg`), au point « live » (`--live`), à l'accent et aux pills neutres `.pill.info` (ne pas réutiliser la classe `built` pour autre chose qu'un statut de story). Dans le graphe des dépendances, `--wip` (jaune) colore l'epic en cours et vert l'epic livré. Les **lignes de story** sont teintées de la couleur de leur pill de statut (`.story[data-status]>.srow`, variable `--tint`), le pill étant en fin de ligne : demandé par l'utilisateur après qu'il avait d'abord refusé cette teinte ; en revanche il a refusé de teinter les **cartes d'epic** : ne pas le faire.
- Le thème suit le système (`prefers-color-scheme`) ; les réglages `prefers-reduced-motion`, `-transparency` et `-contrast` sont gérés en fin de CSS. Les principes de design viennent du skill `apple-design` (`.claude/skills/apple-design/SKILL.md`).

## Livrable

Le projet n'est pas un dépôt git. Pour l'installer sur un autre poste, on archive (zip/tar.gz, `zip` n'est pas installé : passer par `zipfile` via `uv run python -I`) : `suivi.py`, `suivi.sh`, `images/*.png`, `.claude/`, `CLAUDE.md`, un `LISEZMOI.md` et un `suivi.toml` **modèle** (`root` commenté, jamais le chemin local). Exclure `.run/`, `__pycache__` et les `*:Zone.Identifier` de `images/`. Prérequis sur la cible : `uv` seulement. Refaire l'archive après toute modification de `suivi.py`.

## Maquette

La refonte visuelle a été prototypée avec Claude Design : https://claude.ai/artifact/FaRhetPvjXcjbgPhya9Mom (privée). La maquette contient des données écrites à la main ; `suivi.py` reste la source de vérité.
