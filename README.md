<div align="center">

# 📊 Suivi BMAD

**Un tableau de bord local pour suivre l'avancement d'un projet [BMAD](https://github.com/bmad-code-org/BMAD-METHOD) 6.13.**

Un seul fichier Python, aucune dépendance, aucune base de données :
la page est régénérée à chaque rafraîchissement à partir des fichiers de votre projet.

![Python](https://img.shields.io/badge/Python-%E2%89%A5%203.11-3776AB?logo=python&logoColor=white)
![Dépendances](https://img.shields.io/badge/d%C3%A9pendances-aucune-2ea44f)
![Lecture seule](https://img.shields.io/badge/projet%20suivi-lecture%20seule-blue)
![Réseau](https://img.shields.io/badge/r%C3%A9seau-100%25%20local-lightgrey)

</div>

---

## ✨ Fonctionnalités

- **Epics et stories** repliables, avec statut (à faire, développée, en revue, terminée), filtres mémorisés et lignes teintées selon l'état.
- **Exigences et contraintes** (R…, C…) de l'initiative : un clic ouvre un panneau avec leurs sources (notes, architecture…), l'extrait de document concerné, les epics et les stories qui les couvrent.
- **Graphe des dépendances** entre epics, tracé dans le navigateur à partir des `after` des epics et des stories.
- **Équipe d'agents BMAD** (Mary, John, Sofia, Winston, Amelia, Sally) : l'agent actif est déduit du fichier modifié en dernier (« Au travail » sous 15 min, « Dernière activité » ensuite).
- **Synthèse IA** de chaque story, générée en direct à son dépliage par un [Ollama](https://ollama.com) local. Rien n'est enregistré, rien ne sort de votre machine. Facultatif.
- **Travaux reportés**, rétrospectives et avertissements sur les fichiers illisibles.
- **Interface soignée** : thème clair/sombre suivant le système, respect de `prefers-reduced-motion`, `-transparency` et `-contrast`.

## 🚀 Démarrage rapide

Seul prérequis : [`uv`](https://docs.astral.sh/uv/). `suivi.py` porte un en-tête PEP 723, `uv` s'occupe de Python.

```bash
cp suivi.toml.example suivi.toml      # puis renseigner `root`
./suivi.sh start
./suivi.sh url                        # http://127.0.0.1:8765
```

Ou en avant-plan, sans configuration :

```bash
uv run suivi.py --root /chemin/vers/_bmad-output/initiative-xxx --port 8765
```

## ⚙️ Configuration

Par priorité décroissante :

1. options `--root`, `--port`, `--host` ;
2. variables `SUIVI_ROOT`, `SUIVI_PORT`, `SUIVI_HOST` (lues par `suivi.sh` seulement) ;
3. `suivi.toml` à côté de `suivi.py` ;
4. valeurs par défaut : `127.0.0.1:8765`. Il n'y a pas de `root` par défaut.

| Clé          | Rôle                                                              | Défaut                    |
| ------------ | ----------------------------------------------------------------- | ------------------------- |
| `root`       | Dossier de l'initiative (contient `tickets/`). Relatif = depuis le fichier de conf | *(obligatoire)* |
| `port`       | Port d'écoute                                                     | `8765`                    |
| `host`       | Interface d'écoute (`0.0.0.0` ouvre au réseau, sans authentification) | `127.0.0.1`           |
| `ia_url`     | URL d'Ollama                                                      | `http://localhost:11434`  |
| `ia_model`   | Modèle pour la synthèse IA                                        | `qwen3.8-27b-96k`         |
| `ia_timeout` | Délai maximal de la synthèse, en secondes                         | `120`                     |

`suivi.toml` est ignoré par git (il contient un chemin propre au poste) ; `suivi.toml.example` sert de modèle.

## 🧰 Commandes `suivi.sh`

```text
./suivi.sh start   [--port N] [--host H] [--root CHEMIN]
./suivi.sh stop | restart | status [--json] | url | logs [-n N] [-f]
```

Non interactif et idempotent, donc pilotable par un humain comme par une IA. État, PID et journal sont dans `.run/`.

| Code | Sens                                          |
| ---- | --------------------------------------------- |
| 0    | ok                                            |
| 1    | échec du démarrage (voir `logs`)              |
| 2    | usage incorrect                               |
| 3    | serveur arrêté (`status`, `url`)              |
| 4    | déjà lancé avec d'autres réglages (`restart`) |
| 5    | port pris par un autre programme              |

`GET /healthz` répond `ok`.

## 🗂️ Ce que le tableau de bord lit

Sous `<root>/tickets/` :

```text
<root>/
├── <initiative>.md              # type: initiative (exigences R, contraintes C)
├── deferred-work.md             # travaux reportés
└── tickets/
    ├── tickets.toml             # [[epic]] : id, slug, title, covers, after…
    └── <slug>/
        ├── tickets.toml         # [[entry]] : les stories
        ├── <slug>.md            # frontmatter + Outcome, Done when, Requirements…
        ├── <slug>-retrospective.md
        └── tickets/*.md         # un plan par story (status: built | done…)
```

Une story sans plan est `todo`. **Le projet suivi n'est jamais modifié.**

## 🏗️ Architecture

Tout tient dans `suivi.py`, en trois couches enchaînées à chaque requête :

```text
load_all(root)  →  dict  →  render(data)  →  HTML
```

HTML en f-strings, CSS et JS embarqués, pas de framework, pas d'accès réseau (hors Ollama local). Les détails sont dans [`CLAUDE.md`](CLAUDE.md).

Le serveur ne recharge pas son code : après une modification de `suivi.py`, faire `./suivi.sh restart`. Les données du projet suivi, elles, sont relues à chaque requête. Il n'y a pas de suite de tests : on vérifie en lançant le serveur puis `curl`.

## 📦 Structure du dépôt

```text
suivi.py            serveur + rendu (stdlib seulement)
suivi.sh            gestion du service en arrière-plan
suivi.toml.example  modèle de configuration
images/             portraits des agents BMAD
.claude/skills/     skills utilisés pour le design de l'interface
CLAUDE.md           guide pour Claude Code
```

## 🔒 Sécurité

Le serveur écoute sur `127.0.0.1` par défaut et n'a aucune authentification : n'ouvrez `0.0.0.0` que sur un réseau de confiance. Le contexte du projet suivi ne part que vers `ia_url` : gardez-le local.
