#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tableau de bord BMAD : lit les tickets d'une initiative et sert une page HTML.

Usage :  uv run suivi.py [--root CHEMIN] [--port 8765] [--host 127.0.0.1]

Réglages : options de la ligne de commande, sinon `suivi.toml` (à côté de ce script,
clés `root`, `port`, `host`), sinon valeurs par défaut (port 8765, hôte 127.0.0.1).

Chaque requête relit les fichiers sur disque : F5 = données fraîches.
Lecture seule sur le projet suivi.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

CONFIG_FILE = Path(__file__).resolve().parent / "suivi.toml"

# --------------------------------------------------------------------------
# Lecture des fichiers
# --------------------------------------------------------------------------


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _inline_list(v: str) -> list:
    v = v.strip()
    try:
        out = json.loads(v)
        if isinstance(out, list):
            return out
    except ValueError:
        pass
    inner = v[1:-1]
    return [_scalar(x) for x in inner.split(",") if x.strip()]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Mini-parseur YAML plat : `k: v`, `k: [a, b]`, `k:` suivi de `- item`."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.S)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"\s+-\s+(.*)", line)
        if item and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"([A-Za-z_][\w-]*):\s*(.*)", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val == "":
            meta[key] = []
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = _inline_list(val)
        else:
            meta[key] = _scalar(val)
    return meta, body


def sections(body: str) -> dict[str, str]:
    """Découpe un corps markdown en sections `## Titre`."""
    out: dict[str, str] = {}
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    for i in range(1, len(parts), 2):
        out[parts[i].strip()] = parts[i + 1].strip()
    return out


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_toml(path: Path, warnings: list[str]) -> dict:
    try:
        return tomllib.loads(read(path))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - on veut afficher, pas planter
        warnings.append(f"{path.name} illisible : {e}")
        return {}


def parse_deferred(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    cur: dict | None = None
    for line in read(path).splitlines():
        m = re.match(r"^-\s+(\w+):\s*(.*)", line)
        if m:
            if m.group(1) == "source_plan" or cur is None:
                cur = {}
                items.append(cur)
            cur[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^\s+(\w+):\s*(.*)", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    return items


SOURCE_PATTERNS = {
    "notes": "notes-*.md",
    "EXPERIENCE": "EXPERIENCE.md",
    "DESIGN": "DESIGN.md",
    "architecture": "architecture-*.md",
}


def find_initiative(root: Path) -> Path:
    """Fichier de l'initiative : `<dossier>.md` s'il existe, sinon le .md racine déclarant `type: initiative`."""
    named = root / f"{root.name}.md"
    if named.exists():
        return named
    for p in sorted(root.glob("*.md")):
        try:
            if parse_frontmatter(read(p))[0].get("type") == "initiative":
                return p
        except Exception:  # noqa: BLE001
            continue
    return named


def find_source(root: Path, key: str) -> Path | None:
    """Trouve le document cité par une exigence (le moins profond, hors revues)."""
    pattern = SOURCE_PATTERNS.get(key)
    if not pattern:
        return None
    cands = [
        p
        for p in root.rglob(pattern)
        if "reviews" not in p.parts and not p.name.startswith(".") and "tickets" not in p.parts
    ]
    return min(cands, key=lambda p: len(p.parts)) if cands else None


def parse_decisions(path: Path | None) -> list[dict]:
    """Décisions d'architecture `### AD-n — titre` (+ puces Binds / Prevents / Rule) du document d'architecture."""
    if not path:
        return []
    out: list[dict] = []
    cur: dict | None = None
    for line in read(path).splitlines():
        m = re.match(r"###\s+(AD-\d+)\s*[—–-]\s*(.+?)\s*$", line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "fields": {}, "extra": []}
            out.append(cur)
            continue
        if re.match(r"#{1,3}\s", line):
            cur = None
            continue
        if cur is None:
            continue
        f = re.match(r"-\s+\*\*([^*:]+):\*\*\s*(.*)", line)
        if f:
            cur["fields"][f.group(1).strip()] = f.group(2).strip()
        elif line.strip():
            cur["extra"].append(line.strip())
    return out


def display_path(root: Path, p: Path) -> str:
    base = root.parent.parent if root.parent.name == "_bmad-output" else root.parent
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def parse_source_refs(paren: str) -> list[dict]:
    """`notes §Session, décision de David 2026-11-02, architecture AD-9, AD-13` → références."""
    paren = re.sub(r"(décision de David),\s*(\d{4}-\d{2}-\d{2})", r"\1 \2", paren)
    refs: list[dict] = []
    key = None
    for raw in re.split(r"[;,]", paren):
        item = raw.strip()
        if not item:
            continue
        m = re.match(r"(notes|EXPERIENCE|DESIGN|architecture)\b\s*(.*)", item)
        if m:
            key, anchor = m.group(1), m.group(2).strip()
        elif key and (item.startswith("§") or re.match(r"AD-\d+", item)):
            anchor = item
        else:
            refs.append({"kind": "text", "label": item})
            key = None
            continue
        refs.append({"kind": "file", "key": key, "anchor": anchor, "label": f"{key} {anchor}".strip()})
    return refs


def section_excerpt(path: Path, anchor: str, limit: int = 1800) -> dict | None:
    """Extrait la section `## Nom` ou `### AD-9 — …` d'un document markdown."""
    name = anchor.lstrip("§").strip().lower()
    if not name:
        return None
    lines = read(path).splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if not m:
            continue
        title = m.group(2).strip()
        low = title.lower()
        if low == name or low.startswith(name + " ") or low.startswith(name + "—") or low.startswith(name + ":"):
            level, body = len(m.group(1)), []
            for nxt in lines[i + 1 :]:
                h = re.match(r"(#{1,6})\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                body.append(nxt)
            text = "\n".join(body).strip()
            if len(text) > limit:
                text = text[:limit].rsplit(" ", 1)[0] + " […]"
            return {"heading": title, "line": i + 1, "excerpt": text}
    return None


def build_requirements(root: Path, data: dict, warnings: list[str]) -> dict:
    """Exigences (R) et contraintes (C) de l'initiative, avec sources, epics et stories qui les couvrent."""
    init_md = find_initiative(root)
    reqs: dict[str, dict] = {}
    # Diagnostic affiché quand rien n'est trouvé : qu'a-t-on cherché, et où ça coince.
    diag = {"path": display_path(root, init_md), "exists": init_md.exists(), "headings": [], "sections": [], "unparsed": []}
    data["reqs_diag"] = diag
    if not init_md.exists():
        return reqs
    cache: dict[tuple, dict | None] = {}
    section = None
    try:
        for n, line in enumerate(read(init_md).splitlines(), 1):
            h = re.match(r"##\s+(.+?)\s*$", line)
            if h:
                section = h.group(1)
                diag["headings"].append(section)
                if section in ("Requirements", "Constraints"):
                    diag["sections"].append(section)
                continue
            if section not in ("Requirements", "Constraints"):
                continue
            m = re.match(r"-\s+([RC]\d+)\s+\((.*?)\):\s*(.*)", line)
            if not m:
                if line.lstrip().startswith("-") and len(diag["unparsed"]) < 3:
                    diag["unparsed"].append(line.strip()[:120])
                continue
            rid, paren, text = m.groups()
            sources = []
            for ref in parse_source_refs(paren):
                if ref["kind"] == "text":
                    sources.append(ref)
                    continue
                path = find_source(root, ref["key"])
                if not path:
                    sources.append({"kind": "text", "label": ref["label"]})
                    continue
                ck = (str(path), ref["anchor"])
                if ck not in cache:
                    cache[ck] = section_excerpt(path, ref["anchor"])
                ex = cache[ck] or {}
                sources.append(
                    {
                        "kind": "file",
                        "label": ref["label"],
                        "path": display_path(root, path),
                        "line": ex.get("line"),
                        "heading": ex.get("heading"),
                        "excerpt": ex.get("excerpt"),
                    }
                )
            reqs[rid] = {
                "id": rid,
                "kind": "exigence" if rid.startswith("R") else "contrainte",
                "text": text.strip(),
                "path": display_path(root, init_md),
                "line": n,
                "sources": sources,
                "epics": [],
                "stories": [],
            }
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Exigences illisibles : {e}")

    def norm(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip()

    for epic in data["epics"]:
        eid = str(epic["id"])
        own: dict[str, str] = {}
        for sec_name in ("Requirements", "Constraints"):
            for line in epic["sections"].get(sec_name, "").splitlines():
                m = re.match(r"-\s+([RC]\d+)\s+\(.*?\):\s*(.*)", line)
                if m:
                    own[m.group(1)] = m.group(2)
        for rid in sorted(set(own) | set(epic.get("covers", []))):
            r = reqs.get(rid)
            if not r:
                continue
            refined = rid in own and norm(own[rid]) != norm(r["text"])
            r["epics"].append(
                {
                    "id": eid,
                    "title": epic.get("title", ""),
                    "status": epic_status(epic),
                    "text": own[rid].strip() if refined else "",
                }
            )
        for s in epic["stories"]:
            for rid in s.get("covers", []):
                if rid in reqs:
                    reqs[rid]["stories"].append(
                        {"num": f"{eid}.{s['id']}", "title": s.get("title", ""), "status": s["status"], "epic": eid}
                    )
    return reqs


def compute_epic_links(data: dict) -> dict:
    """Dépendances entre epics : celles déclarées (`after`) + celles que les stories révèlent."""
    ids = [str(e["id"]) for e in data["epics"]]
    edges: dict[tuple[str, str], dict] = {}
    for e in data["epics"]:
        for a in e.get("after") or []:
            src, dst = str(a.get("epic")), str(e["id"])
            if src in ids:
                edges[(src, dst)] = {"from": src, "to": dst, "needs": a.get("needs", ""), "implicit": False, "stories": []}
    for e in data["epics"]:
        dst = str(e["id"])
        for s in e["stories"]:
            per_src: dict[str, list[str]] = {}
            for ref in s.get("after", []):
                r = str(ref)
                if "." in r:
                    src = r.split(".", 1)[0]
                    if src != dst and src in ids:
                        per_src.setdefault(src, []).append(r)
            for src, refs in per_src.items():
                edge = edges.setdefault(
                    (src, dst), {"from": src, "to": dst, "needs": "", "implicit": True, "stories": []}
                )
                edge["stories"].append({"num": f"{dst}.{s['id']}", "title": s.get("title", ""), "deps": refs})

    level = {i: 0 for i in ids}
    for _ in range(len(ids) + 1):  # plus long chemin ; la borne protège d'un cycle
        for (src, dst) in edges:
            level[dst] = max(level[dst], level[src] + 1)
    prereq = {i: [src for (src, dst) in edges if dst == i] for i in ids}
    unlocks = {i: [dst for (src, dst) in edges if src == i] for i in ids}
    return {"edges": sorted(edges.values(), key=lambda x: (level[x["to"]], x["to"], x["from"])),
            "level": level, "prereq": prereq, "unlocks": unlocks}


def load_all(root: Path) -> dict:
    warnings: list[str] = []
    tickets_dir = root / "tickets"
    data: dict = {"root": str(root), "warnings": warnings, "epics": []}

    init_md = find_initiative(root)
    if init_md.exists():
        meta, body = parse_frontmatter(read(init_md))
        data["initiative"] = {"meta": meta, "title": meta.get("title", root.name)}
    else:
        data["initiative"] = {"meta": {}, "title": root.name}

    top = load_toml(tickets_dir / "tickets.toml", warnings)
    if not top:
        warnings.append(f"Aucun tickets.toml trouvé dans {tickets_dir}")

    mtimes: list[float] = []

    def touch(p: Path):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            pass

    for e in top.get("epic", []):
        slug = e.get("slug", f"epic-{e.get('id')}")
        edir = tickets_dir / slug
        epic: dict = {**e, "slug": slug, "stories": [], "meta": {}, "sections": {}}

        md = edir / f"{slug}.md"
        if md.exists():
            touch(md)
            try:
                meta, body = parse_frontmatter(read(md))
                epic["meta"], epic["sections"] = meta, sections(body)
            except Exception as ex:  # noqa: BLE001
                warnings.append(f"{md.name} illisible : {ex}")
        retro = edir / f"{slug}-retrospective.md"
        if retro.exists():
            rmeta, rbody = parse_frontmatter(read(retro))
            epic["retro"] = {"meta": rmeta, "sections": sections(rbody)}

        stoml = edir / "tickets" / "tickets.toml"
        touch(stoml)
        entries = load_toml(stoml, warnings).get("entry", [])

        plans: dict[str, dict] = {}
        for p in sorted((edir / "tickets").glob("*.md")) if (edir / "tickets").is_dir() else []:
            touch(p)
            try:
                meta, body = parse_frontmatter(read(p))
            except Exception as ex:  # noqa: BLE001
                warnings.append(f"{p.name} illisible : {ex}")
                continue
            if "ticket" in meta:
                plans[str(meta["ticket"])] = {
                    "meta": meta, "sections": sections(body), "file": p.name, "mtime": p.stat().st_mtime,
                }

        # Les commentaires `# ...` après hitl/risk ne sont pas gardés par tomllib.
        for ent in entries:
            plan = plans.get(str(ent.get("id")))
            status = "todo"
            if plan:
                status = str(plan["meta"].get("status", "todo")).strip("'\"") or "todo"
            epic["stories"].append({**ent, "status": status, "plan": plan})
        epic["_has_stories"] = bool(entries)
        data["epics"].append(epic)

    data["reqs"] = build_requirements(root, data, warnings)
    data["links"] = compute_epic_links(data)
    data["deferred"] = parse_deferred(root / "deferred-work.md")
    touch(root / "deferred-work.md")
    arch = find_source(root, "architecture")
    data["decisions"] = parse_decisions(arch)
    if arch:
        touch(arch)
    data["mtime"] = datetime.fromtimestamp(max(mtimes)) if mtimes else None
    data["crew"] = build_crew(root)
    data["last_done"] = last_done(data)
    return data


def last_done(data: dict) -> str | None:
    """Id (« epic-story ») de la dernière story réalisée : plan done/built le plus récent.

    Clé = date `created` du plan, puis date de modification du fichier (pour départager les
    plans d'un même jour). Les plans touchés en masse n'écrasent donc pas les plus récents.
    """
    best, best_key = None, None
    for e in data["epics"]:
        for st in e["stories"]:
            plan = st.get("plan")
            if not plan or st["status"] not in ("done", "built"):
                continue
            key = (str(plan["meta"].get("created", "")), plan.get("mtime", 0))
            if best_key is None or key > best_key:
                best, best_key = f'{e["id"]}-{st["id"]}', key
    return best


# --------------------------------------------------------------------------
# Personas BMAD : qui a travaillé en dernier, déduit de l'activité des fichiers
# --------------------------------------------------------------------------

# (id, prénom, rôle) dans l'ordre du flux BMAD :
# Mary (cadrage) → John (PRD) → Winston + Sally (archi + UX) → Sofia (tickets) → Amelia (build, review, rétro).
PERSONAS = [
    ("mary", "Mary", "Business Analyst"),
    ("john", "John", "Product Manager"),
    ("winston", "Winston", "Architecte"),
    ("sally", "Sally", "UX Designer"),
    ("sofia", "Sofia", "Product Owner"),
    ("amelia", "Amelia", "Développeuse"),
]
# Fiches affichées dans la popup : (à quoi ça sert, quand l'appeler, commande).
PERSONA_INFO = {
    "mary": (
        "Explorer et cadrer le problème avant d'écrire quoi que ce soit : idées, recherche marché/domaine/technique, "
        "choix techno, analyse concurrentielle, voix utilisateur, product brief, PRFAQ, contexte projet.",
        "Tout début de projet, ou quand une décision manque d'éléments.",
        "bmad-agent-analyst",
    ),
    "john": (
        "Rédiger, mettre à jour ou valider le PRD (le « quoi » et le « pourquoi » du produit).",
        "Une fois l'idée cadrée, pour figer les exigences.",
        "bmad-agent-pm",
    ),
    "winston": (
        "Produire l'architecture spine : les décisions techniques qui garantissent que des parties construites "
        "séparément restent cohérentes.",
        "Après le PRD, avant de découper/coder.",
        "bmad-agent-architect",
    ),
    "sally": (
        "Formaliser la vision UX dans DESIGN.md et EXPERIENCE.md.",
        "En parallèle de l'architecture, dès qu'il y a une interface.",
        "bmad-agent-ux-designer",
    ),
    "sofia": (
        "Transformer le PRD, un brief ou des notes en tickets ; découper épics/initiatives, affiner, tenir le board, "
        "valider, replanifier en cours de route, brancher le tracker.",
        "Entre la planification et le dev, puis tout au long du delivery.",
        "bmad-agent-po",
    ),
    "amelia": (
        "Coder un ticket (revu + vérifié), générer les tests E2E, faire la code review, mener la rétrospective d'épic.",
        "Phase d'implémentation.",
        "bmad-agent-dev",
    ),
}
IMAGES_DIR = Path(__file__).resolve().parent / "images"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
LIVE_SECONDS = 15 * 60  # en dessous : « au travail » ; au-delà : « dernière activité »


def persona_image(pid: str) -> Path | None:
    """Image du persona : le premier fichier d'images/ dont le nom contient son prénom."""
    if not IMAGES_DIR.is_dir():
        return None
    for p in sorted(IMAGES_DIR.iterdir()):
        if p.suffix.lower() not in IMAGE_TYPES or not p.is_file():
            continue
        flat = unicodedata.normalize("NFKD", p.name).encode("ascii", "ignore").decode().lower()
        if pid in flat:
            return p
    return None


def persona_for(rel: Path) -> str | None:
    """Persona qu'on associe à un fichier de l'initiative d'après son emplacement."""
    parts = [x.lower() for x in rel.parts]
    top, name = parts[0], parts[-1]
    if top.startswith("architecture"):
        return "winston"
    if top.startswith("ux"):
        return "sally"
    if top.startswith(("notes", "brief", "research")):
        return "mary"
    if top.startswith("prd") or name.startswith("prd"):
        return "john"
    if top.startswith(("walkthrough", "plan-", "passe-")) or "retrospective" in name:
        return "amelia"
    if top == "tickets":
        # tickets/<epic>/tickets/<plan>.md = le plan d'une story, construite par Amelia
        if len(parts) >= 4 and parts[2] == "tickets" and name.endswith(".md"):
            return "amelia"
        return "sofia"  # tickets.toml, fichiers d'epic
    return "sofia"  # initiative, deferred-work.md, .memlog.md à la racine


def build_crew(root: Path) -> list[dict]:
    last: dict[str, tuple[float, str]] = {}
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            mt = p.stat().st_mtime
        except OSError:
            continue
        rel = p.relative_to(root)
        pid = persona_for(rel)
        if pid and (pid not in last or mt > last[pid][0]):
            last[pid] = (mt, str(rel))
    return [
        {
            "id": pid,
            "name": name,
            "role": role,
            "mtime": last[pid][0] if pid in last else None,
            "file": last[pid][1] if pid in last else "",
            "img": persona_image(pid) is not None,
        }
        for pid, name, role in PERSONAS
    ]


def ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    return f"il y a {s // 86400} j"


# --------------------------------------------------------------------------
# Calculs
# --------------------------------------------------------------------------


def counts(stories: list[dict]) -> dict:
    c = {"done": 0, "built": 0, "todo": 0, "other": 0}
    for s in stories:
        c[s["status"] if s["status"] in c else "other"] += 1
    c["total"] = len(stories)
    return c


def epic_status(epic: dict) -> str:
    declared = epic["meta"].get("status")
    if not epic["_has_stories"]:
        return declared or "non découpé"
    c = counts(epic["stories"])
    if c["done"] == c["total"]:
        return "done"
    if c["todo"] == c["total"]:
        return declared or "à faire"
    return "en cours"


def task_progress(plan: dict | None) -> tuple[int, int]:
    if not plan:
        return 0, 0
    sec = plan["sections"].get("Tasks & Acceptance", "")
    done = len(re.findall(r"^\s*- \[[xX]\]", sec, re.M))
    todo = len(re.findall(r"^\s*- \[ \]", sec, re.M))
    return done, done + todo


SEVERITIES = ("high", "medium", "low", "false", "maybe-false")


def parse_findings(plan: dict | None) -> list[dict]:
    """Constats du `Review Triage Log`, en puces (`[low] [patch] (source) texte`) ou en tableau (Verdict | Route | Constat)."""
    if not plan:
        return []
    sec = plan["sections"].get("Review Triage Log", "")
    out: list[dict] = []
    passe = ""
    cols: dict = {}
    for line in sec.splitlines():
        h = re.match(r"###\s+(.*)", line)
        if h:
            passe = h.group(1).strip()
            continue
        pm = re.match(r"(Passe\s+\d+[^:]*):", line)
        if pm:
            passe = pm.group(1).strip()
            continue
        b = re.match(r"\s*-\s+`\[([^\]]+)\]`\s+`\[([^\]]+)\]`\s*(?:\(([^)]*)\)\s*)?(.*)", line)
        if b:
            out.append({"pass": passe, "sev": b.group(1), "route": b.group(2), "source": b.group(3) or "", "text": b.group(4).strip()})
            continue
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            low = [c.lower() for c in cells]
            if "verdict" in low:  # en-tête : on retient la place de chaque colonne
                cols = {name: low.index(name) for name in ("verdict", "route") if name in low}
                cols["text"] = [i for i, c in enumerate(low) if c.startswith("constat") or c.startswith("preuve")]
                continue
            if cols and len(cells) >= len(low):
                sev = cells[cols["verdict"]].lower()
                if sev in SEVERITIES:
                    out.append(
                        {
                            "pass": passe,
                            "sev": sev,
                            "route": cells[cols["route"]] if "route" in cols else "",
                            "source": "",
                            "text": " — ".join(cells[i] for i in cols["text"] if cells[i]),
                        }
                    )
    return out


def review_findings(plan: dict | None) -> int:
    return len(parse_findings(plan))


SEV_KIND = {"high": "high", "medium": "med", "low": "low"}
ROUTE_KIND = {"patch": "info", "defer": "afaire", "bad_plan": "high"}


def render_findings(plan: dict | None) -> str:
    items = parse_findings(plan)
    if not items:
        return ""
    groups: dict[str, list[dict]] = {}
    for f in items:
        groups.setdefault(f["pass"], []).append(f)
    out = []
    for label, fs in groups.items():
        lis = "".join(
            f'<li>{pill(SEV_KIND.get(f["sev"], "todo"), f["sev"])}{pill(ROUTE_KIND.get(f["route"], "todo"), f["route"])}'
            f'<span class="ftxt">{inline_md(f["text"])}'
            + (f'<small class="muted">{esc(f["source"])}</small>' if f["source"] else "")
            + "</span></li>"
            for f in fs
        )
        head = f'<p class="tlabel">{esc(label)} · {len(fs)}</p>' if label else ""
        out.append(f'{head}<ul class="findings">{lis}</ul>')
    inner = "".join(out)
    return f'<div class="blk"><details class="sub"><summary>Constats de revue ({len(items)})</summary>{inner}</details></div>'


def resolve_after(epic: dict, data: dict, ref) -> tuple[str, str]:
    """Retourne (étiquette, titre) pour une dépendance de story."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        sid, e = int(ref), epic
        label = f"{epic['id']}.{sid}"
    else:
        a, _, b = str(ref).partition(".")
        sid = int(b) if b.isdigit() else -1
        e = next((x for x in data["epics"] if str(x.get("id")) == a), None)
        label = str(ref)
    if e:
        for s in e["stories"]:
            if s.get("id") == sid:
                return label, s.get("title", "")
    return label, ""


# --------------------------------------------------------------------------
# Rendu HTML
# --------------------------------------------------------------------------

esc = html.escape


def inline_md(s: str) -> str:
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def md(text: str) -> str:
    """Markdown minimal : paragraphes, listes à puces, tables ignorées en texte."""
    out: list[str] = []
    in_list = False
    para: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + inline_md(" ".join(para)) + "</p>")
            para.clear()

    for line in text.splitlines():
        m = re.match(r"\s*(?:[-*]|\d+\.)\s+(.*)", line)
        if m:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline_md(re.sub(r"^\[[xX ]\]\s*", "", m.group(1))) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            flush_para()
        else:
            para.append(line.strip())
    flush_para()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def retro_info(sec: dict[str, str]) -> dict:
    """Actions (A1…), questions ouvertes et identifiants d'actions encore ouvertes d'une rétrospective."""
    text = sec.get("Action items", "")
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"A\d+", cells[0]):
            rows.append(cells)
    questions = [
        m.group(1).strip()
        for m in re.finditer(r"^[-*]\s+(.*)", sec.get("Open questions", ""), re.M)
    ]
    open_ids: set[str] = set()
    for q in questions:
        open_ids.update(re.findall(r"\bA\d+\b", q.split(":")[0]))
    m = re.search(r"Restent ouvertes?\s*:?\**\s*(.*?)(?:\n\n|$)", text, re.S)
    if m:
        open_ids.update(re.findall(r"\bA\d+\b", m.group(1)))
    open_ids.update(re.findall(r"Nouvelles? actions?\s*\**(A\d+)", text))
    # une action ajoutée après coup (A12…) n'a pas de ligne dans la table : on la garde quand même
    known = {r[0] for r in rows}
    return {"rows": rows, "questions": questions, "open": open_ids, "known": known}


def retro_actions(info: dict) -> str:
    rows = info["rows"]
    if not rows:
        return ""
    items = "".join(
        f'<li class="{"aopen" if c[0] in info["open"] else ""}"><span class="aid">{esc(c[0])}</span>'
        f'<span class="atxt">{inline_md(c[1])}<small>{esc(c[2])} · {esc(c[3])}'
        f'</small></span>{pill("med", "ouverte") if c[0] in info["open"] else ""}</li>'
        for c in rows
    )
    n_open = len([c for c in rows if c[0] in info["open"]])
    tail = f" {pill('med', f'{n_open} ouvertes')}" if n_open else ""
    return (
        f"<details class='sub retro-actions'><summary>Action items ({len(rows)}){tail}</summary>"
        f"<ol>{items}</ol></details>"
    )


def retro_open_block(info: dict) -> str:
    """Liste toujours visible des points ouverts d'une rétrospective."""
    if not info["questions"]:
        return ""
    def item(q: str) -> str:
        m = re.match(r"([AF]\d+)\s*:\s*(.*)", q, re.S)
        if m:
            return f'<li>{pill("med" if m.group(1).startswith("A") else "todo", m.group(1))} {inline_md(m.group(2))}</li>'
        return f"<li>{inline_md(q)}</li>"

    lis = "".join(item(q) for q in info["questions"])
    return f'<div class="retro-open"><h4>Points ouverts ({len(info["questions"])})</h4><ul>{lis}</ul></div>'


def render_open_items(data: dict) -> str:
    """Carte récapitulative en tête de page : points ouverts de toutes les rétrospectives."""
    blocks = []
    total = 0
    for e in data["epics"]:
        r = e.get("retro")
        if not r:
            continue
        verdict = str(r["meta"].get("verdict", ""))
        info = retro_info(r["sections"])
        if "open" not in verdict and not info["questions"]:
            continue
        total += len(info["questions"])
        blocks.append(
            f'<div class="oi-epic"><button type="button" class="oi-title" data-jump="epic-{esc(str(e["id"]))}">'
            f'Epic {esc(str(e["id"]))} · {esc(e.get("slug", ""))}</button> {pill("med", verdict)}'
            f'{retro_open_block(info)}</div>'
        )
    if not blocks:
        return ""
    return f"""
<section class="card openitems" id="open-items">
  <div class="dhead"><div>
    <h2>Points ouverts des rétrospectives</h2>
    <p class="muted">Ce qui reste à trancher ou à faire pour les epics « accepted-with-open-items ».</p>
  </div><div class="pills">{pill('med', f'{total} points')}</div></div>
  {''.join(blocks)}
</section>"""


STATUS_LABEL = {"done": "Terminé", "built": "Développée", "todo": "À faire", "in-progress": "En cours", "other": "Autre"}
RISK_LABEL = {"high": "Élevé", "medium": "Moyen", "low": "Faible"}
RISK_CLASS = {"high": "high", "medium": "med", "low": "low"}
INIT_STATUS_FR = {"in-progress": "en cours", "done": "terminée", "todo": "à faire"}
RISK_FR = {"high": "élevé", "medium": "moyen", "low": "faible"}
EPIC_STATUS = {
    "done": ("Terminé", "done"),
    "en cours": ("En cours", "built"),
    "non découpé": ("Non découpé", "nodecoupe"),
    "à faire": ("À faire", "afaire"),
}

CHEVRON = (
    '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 3.5L12 9l-5.5 5.5"/></svg>'
)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n > 1 else ''}"


def fmt_date(value) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    return f"{m.group(3)}/{m.group(2)}" if m else "—"


def pill(kind: str, text: str) -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


HUMAN_ICON = (
    '<svg class="hico" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="4.8" r="2.7"/><path d="M2.6 14c.4-3 2.4-4.6 5.4-4.6s5 1.6 5.4 4.6"/></svg>'
)


def hitl_pill() -> str:
    return f'<span class="pill hitl">{HUMAN_ICON}HITL</span>'


def status_pill(status: str) -> str:
    kind = status if status in ("done", "built") else "wip" if status == "in-progress" else "info" if status == "in-review" else "afaire"
    return pill(kind, STATUS_LABEL.get(status, status))


def risk_pill(risk: str) -> str:
    label = RISK_LABEL.get(risk, "Inconnu")
    return (
        f'<span class="pill {esc(RISK_CLASS.get(risk, "todo"))}" title="Risque : {esc(label.lower())}">{esc(label)}</span>'
    )


def block(title: str, inner: str) -> str:
    return f'<div class="blk"><div class="lbl">{esc(title)}</div>{inner}</div>'


def req_tag(rid, data: dict) -> str:
    """Tag d'exigence : bouton qui ouvre le panneau si l'exigence est connue."""
    rid = str(rid)
    if rid in data.get("reqs", {}):
        return (
            f'<button type="button" class="tag req" data-req="{esc(rid)}" '
            f'aria-haspopup="dialog" title="Voir {esc(rid)}">{esc(rid)}</button>'
        )
    return f'<span class="tag">{esc(rid)}</span>'


def linkify_reqs(html_text: str, data: dict) -> str:
    """Dans une liste markdown rendue, `<li>R2 …` devient un bouton vers le panneau."""
    return re.sub(
        r"<li>([RC]\d+)\b",
        lambda m: "<li>" + req_tag(m.group(1), data),
        html_text,
    )


def bar(c: dict, cls: str = "bar") -> str:
    t = c["total"]
    if not t:
        return ""
    label = f"{c['done']} terminées, {c['built']} développées, {c['todo']} à coder sur {t}"
    return (
        f'<div class="{cls}" role="img" aria-label="{esc(label)}">'
        f'<i class="done" style="width:{c["done"] / t * 100:.2f}%"></i>'
        f'<i class="built" style="width:{c["built"] / t * 100:.2f}%"></i></div>'
    )


CHECK_SVG = (
    '<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 6.5l2.3 2.3L9.5 3.5"/></svg>'
)


def render_tasks(text: str) -> str:
    """Section « Tasks & Acceptance » : les cases `- [x]` / `- [ ]` du plan, cochées ou non."""
    out: list[str] = []
    in_list = False
    for line in text.splitlines():
        m = re.match(r"\s*-\s+\[([xX ])\]\s+(.*)", line)
        b = re.match(r"\s*-\s+(.*)", line)
        h = re.match(r"\*\*(.+?)\*\*\s*$", line.strip())
        if m or b:
            if not in_list:
                out.append('<ul class="tasks">')
                in_list = True
            if m:
                on = m.group(1) in "xX"
                out.append(
                    f'<li class="{"on" if on else "off"}"><span class="cb" role="img" '
                    f'aria-label="{"cochée" if on else "à faire"}">{CHECK_SVG if on else ""}</span>'
                    f'<span>{inline_md(m.group(2))}</span></li>'
                )
            else:
                out.append(f"<li class='plain'>{inline_md(b.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if h:
            out.append(f'<p class="tlabel">{esc(h.group(1))}</p>')
        elif line.strip():
            out.append(f"<p>{inline_md(line.strip())}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def render_story(epic: dict, s: dict, data: dict) -> str:
    plan = s["plan"]
    td, tt = task_progress(plan)
    findings = review_findings(plan)
    risk = str(s.get("risk", "")) or "?"
    hitl = s.get("hitl")
    covers = "".join(req_tag(c, data) for c in s.get("covers", []))
    created = fmt_date(plan["meta"].get("created")) if plan else "—"
    deps = ""
    if s.get("after"):
        resolved = [resolve_after(epic, data, r) for r in s["after"]]
        tip = " ; ".join(f"{lab} {t}".strip() for lab, t in resolved)
        badges = "".join(f'<span class="tag">{esc(lab)}</span>' for lab, _ in resolved)
        deps = f'<span class="sdeps" title="Dépend de : {esc(tip)}"><span class="muted">après</span>{badges}</span>'

    parts: list[str] = []
    if covers:
        parts.append(f'<div class="chips">{covers}</div>')
    parts.append(
        '<div class="blk ia" data-ia>'
        '<div class="lbl">Synthèse IA <span class="ia-badge" title="Générée à l\'ouverture par un modèle local (Ollama), jamais enregistrée">locale</span>'
        '<button type="button" class="ia-regen" hidden>Régénérer</button></div>'
        '<p class="ia-text" aria-live="polite"></p></div>'
    )
    if s.get("description"):
        parts.append(block("Description", f"<p>{inline_md(s['description'])}</p>"))
    if s.get("verify"):
        parts.append(block("Vérification attendue", f"<p>{inline_md(s['verify'])}</p>"))
    if plan:
        sec = plan["sections"]
        if sec.get("Intent"):
            parts.append(block("Intent (plan)", md(sec["Intent"])))
        if sec.get("Implementation Notes"):
            parts.append(block("Notes d'implémentation", md(sec["Implementation Notes"])))
        if sec.get("Tasks & Acceptance"):
            tt_label = f"Tâches ({td}/{tt})" if tt else "Tâches"
            parts.append(block(tt_label, render_tasks(sec["Tasks & Acceptance"])))
        parts.append(render_findings(plan))
    else:
        parts.append('<p class="muted">Pas encore de plan pour cette story.</p>')
    if s.get("after"):
        items = []
        for ref in s["after"]:
            lab, title = resolve_after(epic, data, ref)
            items.append(f"<li><b>{esc(lab)}</b> {esc(title)}</li>")
        parts.append(block("Dépend de", "<ul>" + "".join(items) + "</ul>"))
    if s.get("references"):
        refs = "".join(f"<li><code>{esc(r)}</code></li>" for r in s["references"])
        parts.append(block("Références", f"<ul>{refs}</ul>"))
    if plan:
        typ = esc(str(plan["meta"].get("type", "")))
        parts.append(f'<p class="muted small">{typ} · <code>{esc(plan["file"])}</code></p>')

    last = ""
    if data.get("last_done") == f'{epic["id"]}-{s["id"]}':
        last = '<span class="lastdot" role="img" aria-label="Dernière story réalisée" title="Dernière story réalisée"></span>'
    meta = ""
    if plan and tt:
        meta += f'<span class="meta" title="Tâches cochées">☑ {td}/{tt}</span>'
    if plan:
        meta += f'<span class="meta" title="Constats de revue">{plural(findings, "constat")}</span>'

    return f"""
<details class="story" id="story-{esc(str(epic['id']))}-{esc(str(s['id']))}" data-status="{esc(s['status'])}" data-risk="{esc(risk)}" data-hitl="{'1' if hitl else '0'}">
  <summary class="srow">{last}
    <span class="num">{esc(str(epic['id']))}.{esc(str(s['id']))}</span>
    <span class="stitle">{esc(s.get('title', ''))} {risk_pill(risk)}{hitl_pill() if hitl else ''}{deps}</span>
    {meta}
    <span class="meta date">{created}</span>
    <span class="pills sstatus">{status_pill(s['status'])}</span>
  </summary>
  <div class="sbody">{''.join(parts)}</div>
</details>"""


def render_epic(epic: dict, data: dict) -> str:
    c = counts(epic["stories"])
    status = epic_status(epic)
    s_label, s_kind = EPIC_STATUS.get(status, (status.capitalize(), "todo"))
    risk = str(epic["meta"].get("risk", "")) or "?"
    covers = "".join(req_tag(x, data) for x in epic.get("covers", []))

    deps = ""
    if epic.get("after"):
        li = "".join(
            f"<li>Epic {esc(str(a.get('epic')))} — <span class='muted'>{esc(a.get('needs', ''))}</span></li>"
            for a in epic["after"]
        )
        deps = block("Après", f"<ul>{li}</ul>")
    unl = [e for e in data.get("links", {}).get("edges", []) if e["from"] == str(epic["id"])]
    if unl:
        li = "".join(
            f"<li>Epic {esc(e['to'])}"
            + (f" — <span class='muted'>{esc(e['needs'])}</span>" if e["needs"] else "")
            + "</li>"
            for e in unl
        )
        deps += block("Débloque", f"<ul>{li}</ul>")

    sec = epic["sections"]
    extra = ""
    if "Outcome" in sec:
        extra += block("Résultat attendu", md(sec["Outcome"]))
    if "Done when" in sec:
        extra += block("Terminé quand", md(sec["Done when"]))
    reqs = sec.get("Requirements")
    if reqs:
        n_reqs = len(re.findall(r"^- R\d+", reqs, re.M))
        extra += f"<details class='sub'><summary>Exigences ({n_reqs})</summary>{linkify_reqs(md(reqs), data)}</details>"
    details = f"<details class='sub'><summary>Détails de l'epic</summary>{deps}{extra}</details>" if (deps or extra) else ""

    retro = ""
    if epic.get("retro"):
        rm = epic["retro"]["meta"]
        info = retro_info(epic["retro"]["sections"])
        v = str(rm.get("verdict", "?"))
        vk = "med" if "open" in v else ("high" if "reject" in v else "done")
        retro = (
            f'<p class="retro">Rétrospective : {pill(vk, v)} '
            f'({esc(str(rm.get("date", "")))})</p>'
        ) + retro_open_block(info) + retro_actions(info)

    pct = round((c["done"] + c["built"]) / c["total"] * 100) if c["total"] else 0
    if epic["_has_stories"]:
        summary = (
            f'{plural(c["done"], "terminée")} · {plural(c["built"], "développée")} · '
            f'{c["todo"]} à coder sur {c["total"]} · {pct} % codées'
        )
        stories = sorted(epic["stories"], key=lambda s: s["id"])
        stories_html = (
            '<div class="stories">'
            + "".join(render_story(epic, s, data) for s in stories)
            + "</div>"
        )
    else:
        summary = "Stories pas encore découpées"
        stories_html = (
            '<p class="note">Les stories de cet epic ne sont pas encore découpées : '
            "elles le seront quand l'epic précédent sera terminé.</p>"
        )

    eid = esc(str(epic["id"]))
    last_done = str(data.get("last_done") or "")
    last = ""
    if last_done.split("-")[0] == str(epic["id"]):
        tip = f'Contient la dernière story réalisée ({last_done.replace("-", ".")})'
        last = f'<span class="lastdot lastdot-e" role="img" aria-label="{esc(tip)}" title="{esc(tip)}"></span>'
    return f"""
<details class="epic" id="epic-{eid}" data-epic="{eid}">
  <summary class="ehead">
    <span class="etop">
      <span class="chev">{CHEVRON}</span>
      <span class="eid">Epic {eid}</span>
      {pill('wip' if s_kind == 'built' else s_kind, s_label)}
      {risk_pill(risk)}{last}
    </span>
    <span class="etitle">{esc(epic['slug'])}</span>
    <span class="edesc"><strong>{esc(epic.get('title', ''))}</strong><br>{inline_md(epic.get('description', ''))}</span>
    <span class="chips">{covers}</span>
    <span class="eprog">{bar(c)}<span class="esum">{esc(summary)}</span></span>
  </summary>
  <div class="ebody">
    {details}
    {stories_html}
    {retro}
  </div>
</details>"""


# Icône d'onglet : la même coche blanche sur carré sombre que le logo de la barre du haut.
FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#161A22"/>'
    '<path d="M8 16.5l5.5 5.5L24 10" fill="none" stroke="#fff" stroke-width="3.2" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def avatar(c: dict, size: str) -> str:
    if c["img"]:
        return f'<img class="av {size}" src="/personas/{c["id"]}" alt="" decoding="async">'
    return f'<span class="av {size} av-fb" aria-hidden="true">{esc(c["name"][:1])}</span>'


def persona_dialog(c: dict) -> str:
    sert, quand, cmd = PERSONA_INFO[c["id"]]
    pid = esc(c["id"])
    if c["mtime"]:
        act = f'Dernier fichier modifié {ago(c["mtime"])} : <code>{esc(c["file"])}</code>'
    else:
        act = "Aucun fichier de ce persona trouvé dans l'initiative pour l'instant."
    return f"""<dialog class="dlg dlg-persona" id="persona-{pid}" aria-labelledby="persona-{pid}-t"><div class="rd">
    <header class="rd-head"><div class="ph">{avatar(c, "av-l")}
      <div><p class="eyebrow">Agent BMAD</p><h2 id="persona-{pid}-t">{esc(c["name"])} <span class="muted">· {esc(c["role"])}</span></h2></div></div>
      <button type="button" class="rd-close" aria-label="Fermer"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13"/></svg></button></header>
    <div class="rd-body pinfo">
      <section><h3 class="tlabel">À quoi ça sert</h3><p>{esc(sert)}</p></section>
      <section><h3 class="tlabel">Quand l'appeler</h3><p>{esc(quand)}</p></section>
      <section><h3 class="tlabel">Commande</h3><p><code>{esc(cmd)}</code></p></section>
      <section><h3 class="tlabel">Dans ce projet</h3><p class="small muted">{act}</p></section>
    </div></div></dialog>"""


def render_crew(data: dict) -> str:
    crew = data.get("crew") or []
    seen = [c for c in crew if c["mtime"]]
    if not seen:
        return ""
    cur = max(seen, key=lambda c: c["mtime"])
    live = time.time() - cur["mtime"] < LIVE_SECONDS
    eyebrow = "Au travail en ce moment" if live else "Dernière activité"
    dot = '<span class="live" aria-hidden="true"></span>' if live else ""
    items = "".join(
        f'<li class="{"on" if c is cur else ""}">'
        f'<button type="button" class="crew-btn" data-persona="{esc(c["id"])}" aria-haspopup="dialog" '
        f'title="{esc(c["name"])} · {esc(c["role"])}'
        f'{(" · " + esc(c["file"])) if c["file"] else ""}">'
        f'{avatar(c, "av-s")}<b>{esc(c["name"])}</b>'
        f'<small>{ago(c["mtime"]) if c["mtime"] else "pas encore vu"}</small></button></li>'
        for c in crew
    )
    dialogs = "".join(persona_dialog(c) for c in crew)
    return f"""<section class="card crew" aria-label="Personas BMAD">
    <div class="crew-now"><button type="button" class="crew-av" data-persona="{esc(cur["id"])}" aria-haspopup="dialog" aria-label="Fiche de {esc(cur["name"])}">{avatar(cur, "av-l")}</button>
      <div><p class="eyebrow">{dot}{eyebrow}</p>
      <h2>{esc(cur["name"])} <span class="muted">· {esc(cur["role"])}</span></h2>
      <p class="small muted">fichier modifié {ago(cur["mtime"])} : <code>{esc(cur["file"])}</code></p></div>
    </div>
    <ul class="crew-list">{items}</ul>
    {dialogs}
  </section>"""


def render_requirements_missing(data: dict) -> str:
    """Panneau « pas d'exigences » : dit quel fichier a été lu et ce qui manque, sans rien inventer."""
    d = data.get("reqs_diag") or {}
    path = esc(d.get("path", "?"))
    if not d.get("exists"):
        why = f"Le fichier d'initiative <code>{path}</code> n'existe pas."
    elif not d.get("sections"):
        seen = ", ".join(esc(h) for h in d.get("headings", [])[:8]) or "aucun titre <code>##</code>"
        why = f"<code>{path}</code> ne contient ni section <code>## Requirements</code> ni <code>## Constraints</code> (titres trouvés : {seen})."
    else:
        n_c = len(data.get("reqs", {}))  # seules des contraintes C… ont été lues
        names = " et ".join("<code>## " + esc(x) + "</code>" for x in d["sections"])
        if n_c:
            why = f"<code>{path}</code> ne déclare que {n_c} contrainte(s) <code>C…</code> : pas de section <code>## Requirements</code> avec des lignes <code>R…</code>."
        else:
            why = f"<code>{path}</code> a bien une section {names}, mais aucune ligne n'a le format attendu."
            if d.get("unparsed"):
                why += "<br>Exemples de lignes ignorées : " + " ; ".join(f"<code>{esc(x)}</code>" for x in d["unparsed"])
    return f"""
<section class="card deferred-card" id="exigences">
  <div class="dhead"><div><h2>Exigences</h2></div><div class="pills">{pill('todo', 'non présentes')}</div></div>
  <p class="muted">Aucune exigence n'a été trouvée pour ce projet. {why}</p>
  <details class="sub"><summary>Format attendu</summary>
    <p class="small muted">Le fichier d'initiative est <code>&lt;dossier&gt;.md</code> à la racine du dossier suivi, ou à défaut le <code>.md</code> racine
    dont le frontmatter contient <code>type: initiative</code>. Il doit porter des sections <code>## Requirements</code> et/ou <code>## Constraints</code> avec une ligne par exigence :</p>
    <pre class="small">- R1 (notes §Score, architecture AD-9): énoncé de l'exigence. Vérifié par …
- C1 (architecture AD-2): énoncé de la contrainte.</pre>
  </details>
</section>"""


def render_requirements(data: dict) -> str:
    reqs = sorted(
        (r for r in data.get("reqs", {}).values() if r["kind"] == "exigence"),
        key=lambda r: int(re.sub(r"\D", "", r["id"]) or 0),
    )
    if not reqs:
        return render_requirements_missing(data)
    rows = []
    for r in reqs:
        sts = r["stories"]
        n_done = sum(1 for x in sts if x["status"] == "done")
        cov = f"{n_done}/{len(sts)} stories terminées" if sts else "aucune story"
        eps = ", ".join(f"epic {esc(e['id'])}" for e in r["epics"])
        i = r["text"].find(" Vérifié")
        stmt, verif = (r["text"][:i], r["text"][i:]) if i >= 0 else (r["text"], "")
        rows.append(
            f'<li><button type="button" class="req reqrow" data-req="{esc(r["id"])}" aria-haspopup="dialog">'
            f'<span class="n">{esc(r["id"])}</span><span class="t">{esc(stmt.strip())}'
            + (f'<small class="muted">{esc(verif.strip())}</small>' if verif else "")
            + f'<small class="muted">{esc(cov)}{" · " + eps if eps else ""}</small></span></button></li>'
        )
    return f"""
<section class="card deferred-card" id="exigences">
  <div class="dhead"><div>
    <h2>Exigences</h2>
  </div><div class="pills">{pill('info', f'{len(reqs)} exigences')}</div></div>
  <details class="sub"><summary>Voir la liste ({len(reqs)})</summary><ul class="rd-list reqlist">{''.join(rows)}</ul></details>
</section>"""


def render_links(data: dict) -> str:
    links = data.get("links") or {}
    edges = links.get("edges", [])
    if not edges:
        return ""
    by_id = {str(e["id"]): e for e in data["epics"]}
    done = {i for i, e in by_id.items() if epic_status(e) == "done"}

    cols: dict[int, list[str]] = {}
    for i in by_id:
        cols.setdefault(links["level"][i], []).append(i)

    def node(i: str) -> str:
        e = by_id[i]
        st = epic_status(e)
        label, kind = EPIC_STATUS.get(st, (st.capitalize(), "todo"))
        waiting = [p for p in links["prereq"][i] if p not in done]
        if i in done:
            state = "Livré"
        elif waiting:
            state = "Attend " + ", ".join(f"epic {p}" for p in waiting)
        elif links["prereq"][i]:
            state = "Prérequis livrés"
        else:
            state = "Point de départ"
        if e["stories"]:
            n_done = sum(1 for x in e["stories"] if x["status"] in ("done", "built"))
            state += f" ({n_done}/{len(e['stories'])})"
        return (
            f'<button type="button" class="dnode" data-state="{kind}" data-epic="{esc(i)}" data-jump="epic-{esc(i)}">'
            f'<span class="dn-top"><span class="eid">Epic {esc(i)}</span>{pill('wip' if kind == 'built' else kind, label)}</span>'
            f'<span class="dn-title">{esc(e.get("slug", ""))}</span>'
            f'<span class="dn-state">{esc(state)}</span></button>'
        )

    cols_html = "".join(
        f'<div class="dcol">{"".join(node(i) for i in sorted(ids, key=int))}</div>'
        for _, ids in sorted(cols.items())
    )

    # Les liens ne sont plus listés : seuls leurs attributs servent à tracer les flèches.
    items = []
    for ed in edges:
        state = "done" if ed["from"] in done else "pending"
        items.append(f'<i hidden class="dedge" data-from="{esc(ed["from"])}" data-to="{esc(ed["to"])}" data-state="{state}"></i>')

    return f"""
<section class="card deps" id="deps" aria-label="Dépendances entre epics">
  <div class="dhead">
    <div>
      <h2>Dépendances entre epics</h2>
      <p class="muted">Une flèche va de l'epic qui livre vers l'epic qui en a besoin. Clique sur un epic pour l'ouvrir.</p>
    </div>
    <div class="legend"><span><i style="background:var(--done)"></i>Livré</span><span><i style="background:var(--wip)"></i>En cours</span><span><i style="background:var(--muted)"></i>À livrer</span></div>
  </div>
  <div class="dgraph" id="dgraph">{cols_html}<svg class="dsvg" id="dsvg" aria-hidden="true"></svg></div>
  {"".join(items)}
</section>"""


def render_deferred(items: list[dict]) -> str:
    if not items:
        return ""
    open_ = [i for i in items if not i.get("resolved")]
    rows = []
    for i in items:
        src = i.get("source_plan", "")
        src = Path(src).name.replace("-plan.md", "") if src else ""
        res = i.get("resolved")
        rows.append(
            f"<li class='{'res' if res else 'opn'}'>{pill('done' if res else 'todo', 'résolu' if res else 'ouvert')} "
            f"{inline_md(i.get('summary', ''))}"
            f"<div class='muted small'>{esc(src)}</div>"
            f"<div class='muted small'>{inline_md(i.get('evidence', ''))}</div>"
            + (f"<div class='muted small'>→ {inline_md(res)}</div>" if res else "")
            + "</li>"
        )
    return f"""
<section class="card deferred-card" id="deferred">
  <div class="dhead">
    <div>
      <h2>Travaux reportés</h2>
      <p class="muted">Constats de revue gardés pour plus tard, d'après <code>deferred-work.md</code>.</p>
    </div>
    <div class="pills">{pill('todo', f'{len(open_)} ouverts')}{pill('done', f'{len(items) - len(open_)} résolus')}</div>
  </div>
  <details class="sub"><summary>Voir la liste ({len(items)})</summary><ul class="deferred">{''.join(rows)}</ul></details>
</section>"""


def render_decisions(items: list[dict]) -> str:
    if not items:
        return ""
    labels = (("Binds", "Exigences liées"), ("Prevents", "Évite"), ("Rule", "Règle"))
    rows = []
    for d in items:
        f = d["fields"]
        body = "".join(
            f"<p><span class='muted small'>{lab}</span><br>{inline_md(f[k])}</p>" for k, lab in labels if f.get(k)
        )
        rows.append(
            f"<li id='ad-{esc(d['id'].lower())}'><details class='sub'><summary><strong>{esc(d['id'])}</strong> · "
            f"{esc(d['title'])}</summary>{body}</details></li>"
        )
    return f"""
<section class="card deferred-card" id="decisions">
  <div class="dhead"><div>
    <h2>Décisions d'architecture</h2>
    <p class="muted">Les AD de la spine d'architecture (<code>architecture-*.md</code>).</p>
  </div><div class="pills">{pill('todo', f'{len(items)} décisions')}</div></div>
  <details class="sub"><summary>Voir la liste ({len(items)})</summary><ul class="deferred ads">{''.join(rows)}</ul></details>
</section>"""


CSS = """
:root{
  --bg:#F4F5F7;--card:#FFFFFF;--ink:#161A22;--muted:#566070;--line:#E3E6EC;--hover:#F0F2F6;--soft:#F4F5F7;
  --chip:#EBEDF2;--track:#D9DDE5;--glass:rgba(244,245,247,.72);--accent:#2F5BEA;
  --done:#2F9E5B;--built:#84B82A;--live:#3B6FE0;--info-bg:#E3EBFD;--info-fg:#1D4ED8;
  --done-bg:#E2F3E8;--done-fg:#1B6B3A;--built-bg:#EAF4D2;--built-fg:#43650F;--todo-bg:#ECEEF2;--todo-fg:#4B5563;
  --high-bg:#FDE8E6;--high-fg:#B42318;--med-bg:#FDF0D8;--med-fg:#8A5200;--low-bg:#E2F3E8;--low-fg:#1B6B3A;
  --hitl-bg:#F1E8FA;--hitl-fg:#6B2FA5;
  --wip:#E0AA00;--wip-bg:#FFF0B3;--wip-fg:#5C4500;--last:#E5484D;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0F1115;--card:#181B21;--ink:#F2F4F7;--muted:#A3ABB8;--line:#2A2F38;--hover:#1F232B;--soft:#1F232B;
  --chip:#262B34;--track:#2F343E;--glass:rgba(15,17,21,.72);--accent:#6D95F2;
  --done:#4CC27F;--built:#A6D65C;--live:#6D95F2;--info-bg:#162A52;--info-fg:#9DBBFF;
  --done-bg:#14301F;--done-fg:#7FD39B;--built-bg:#26340F;--built-fg:#C4E58B;--todo-bg:#272B33;--todo-fg:#B4BBC8;
  --high-bg:#3A1613;--high-fg:#FF9B91;--med-bg:#38290F;--med-fg:#F2C26B;--low-bg:#14301F;--low-fg:#7FD39B;
  --hitl-bg:#2D1B42;--hitl-fg:#D2B0F5;
  --wip:#F2C230;--wip-bg:#3D3300;--wip-fg:#FFD84D;--last:#FF6B6B;
}}
*{box-sizing:border-box}
html{scroll-padding-top:5rem;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;font-optical-sizing:auto}
button{font:inherit;color:inherit}
code,.slug{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:var(--chip);padding:0 5px;border-radius:4px;font-size:.88em}
p{margin:.4em 0}
ul{margin:.3em 0;padding-left:1.3em}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.muted{color:var(--muted)}.small{font-size:.85rem}
.hidden{display:none!important}
.wrap{max-width:70rem;margin:0 auto;padding:0 1.5rem}

/* Barre flottante translucide */
.top{position:sticky;top:0;z-index:10;background:var(--glass);
  backdrop-filter:blur(20px) saturate(180%);-webkit-backdrop-filter:blur(20px) saturate(180%);box-shadow:0 1px 0 var(--line)}
.top .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:.75rem 1.25rem;padding-top:.75rem;padding-bottom:.75rem}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:650;letter-spacing:-.01em;margin-right:.5rem}
.logo{width:28px;height:28px;border-radius:8px;background:var(--ink);display:inline-flex;align-items:center;justify-content:center}
.idesc{margin:.35rem 0 0;font-size:.95rem;font-style:italic;color:var(--muted)}
.ihead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:.75rem 1rem}
.foldbar{display:flex;gap:.5rem}
.nav{display:flex;flex-wrap:wrap;gap:.4rem;flex:1 1 20rem}
.chipbtn{color:var(--ink);text-decoration:none;border:0;cursor:pointer;padding:.25rem .75rem;border-radius:999px;font-size:.82rem;font-weight:500;background:var(--chip);
  transition:background-color 120ms ease-out,transform 120ms ease-out}
.chipbtn:hover{background:var(--track)}.chipbtn:active{transform:scale(.96)}
.cta{background:var(--ink);color:var(--bg);border:0;border-radius:10px;padding:.5rem 1rem;min-height:44px;font-size:.9rem;font-weight:600;cursor:pointer;text-decoration:none;
  display:inline-flex;align-items:center;transition:transform 120ms ease-out,filter 120ms ease-out}
.cta:hover{filter:brightness(1.15)}.cta:active{transform:scale(.97)}

main{padding-top:2.5rem;padding-bottom:4.5rem;display:flex;flex-direction:column;gap:1.75rem}
.eyebrow{margin:0 0 .4rem;font-size:.8rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
h1{margin:0;font-size:clamp(1.7rem,3.2vw,2.5rem);line-height:1.08;letter-spacing:-.025em;font-weight:700;max-width:52rem}
h2{margin:0;font-size:1.25rem;line-height:1.2;letter-spacing:-.015em;font-weight:650}

.card{background:var(--card);border:1px solid var(--line);border-radius:20px}
.global{padding:1.75rem;display:flex;flex-wrap:wrap;gap:1.75rem 3rem;align-items:center}
.gleft{flex:1 1 17rem;min-width:0}
.big{display:flex;align-items:baseline;gap:.5rem}
.big b{font-size:4rem;line-height:1;letter-spacing:-.04em;font-weight:700}
.big span{font-size:1.5rem;font-weight:600;color:var(--muted)}
.gleft p.cap{color:var(--muted);margin:.4rem 0 1rem}
.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--track);gap:2px}
.eprog .bar{height:8px;border-radius:4px}
.bar i{display:block;height:100%}.bar .done{background:var(--done)}.bar .built{background:var(--built)}
.legend{display:flex;flex-wrap:wrap;gap:.35rem 1.1rem;margin-top:.75rem;font-size:.82rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:.4rem}
.legend i{width:10px;height:10px;border-radius:3px;display:inline-block}
.kpis{flex:1 1 24rem;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.75rem}
.kpi{background:var(--soft);border-radius:14px;padding:.85rem 1rem}
.kpi b{display:block;font-size:1.75rem;line-height:1.1;letter-spacing:-.03em;font-weight:700}
.kpi-btn{all:unset;box-sizing:border-box;background:var(--soft);border-radius:14px;padding:.85rem 1rem;cursor:pointer;transition:background .15s}
.kpi-btn{position:relative;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--hitl-fg) 35%,transparent)}
.kpi-btn::after{content:"Voir la liste ›";position:absolute;top:.7rem;right:.9rem;font-size:.75rem;font-weight:600;color:var(--hitl-fg)}
.kpi-btn:hover{background:var(--hitl-bg)}
.kpi-btn:hover::after{text-decoration:underline}.kpi-btn:focus-visible{outline:2px solid var(--accent)}
.kpi-btn b{display:block;font-size:1.75rem;line-height:1.1;letter-spacing:-.03em;font-weight:700}
.kpi-btn span{font-size:.82rem;color:var(--hitl-fg)}
.rd-list small{font-size:.78rem}
.reqrow{align-items:flex-start!important}
.reqrow .t{flex:1 1 0;min-width:0;line-height:1.45;text-align:left}
.reqrow small{display:block;margin-top:.2rem}
.reqlist{margin-top:.5rem}
.kpi span{font-size:.82rem;color:var(--muted)}

/* Personas */
.crew{padding:1.25rem 1.75rem;display:flex;flex-wrap:wrap;gap:1.25rem 2.5rem;align-items:center}
.crew-now{display:flex;align-items:center;gap:1rem;flex:1 1 20rem;min-width:0}
.crew-now .eyebrow{margin:0 0 .15rem;display:flex;align-items:center;gap:.45rem}
.crew-now p{margin:.15rem 0 0;overflow-wrap:anywhere}
.av{display:block;flex:none;border-radius:50%;object-fit:cover;background:var(--chip)}
.av-l{width:72px;height:72px}.av-s{width:44px;height:44px}
.av-fb{display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:var(--muted)}
.crew-list{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:.9rem 1.5rem;flex:0 1 auto;justify-content:center}
.crew-list li+li::before{content:"→";content:"→" / "";position:absolute;left:-.75rem;top:11px;transform:translateX(-50%);
  font-size:1rem;line-height:22px;color:var(--muted)}
@media (max-width:720px){.crew-list li+li::before{display:none}.crew-list{gap:.9rem 1rem}}
.crew-list li{position:relative;display:flex;flex-direction:column;align-items:center;width:5rem;text-align:center}
.crew-btn{all:unset;box-sizing:border-box;cursor:pointer;display:flex;flex-direction:column;align-items:center;width:100%;border-radius:12px;padding:.25rem 0;
  transition:transform 120ms ease-out}
.crew-btn:active{transform:scale(.95)}
.crew-btn:focus-visible,.crew-av:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.crew-btn>*{opacity:.55;transition:opacity 120ms ease-out}
.crew-btn:hover>*,.crew-list li.on .crew-btn>*{opacity:1}
.crew-av{all:unset;cursor:pointer;display:block;flex:none;border-radius:50%}
.dlg-persona{width:min(34rem,calc(100vw - 2rem))}
.ph{display:flex;align-items:center;gap:1rem}
.ph h2{font-size:1.4rem}
.pinfo{gap:1.1rem}
.pinfo .tlabel{margin:0 0 .25rem}
.pinfo p{margin:0;line-height:1.55}
.crew-list li.on .av{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--ink)}
.crew-list b{margin-top:.4rem;font-size:.85rem;font-weight:600}
.crew-list small{font-size:.72rem;color:var(--muted);line-height:1.2}
.live{width:8px;height:8px;border-radius:50%;background:var(--live);animation:pulse 1.8s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}

.ghost{border:1px solid var(--line);background:transparent;cursor:pointer;padding:.5rem .9rem;border-radius:10px;font-size:.88rem;font-weight:500;min-height:44px;
  transition:background-color 120ms ease-out,transform 120ms ease-out}
.ghost:hover{background:var(--hover)}.ghost:active{transform:scale(.97)}
.btns{display:flex;gap:.5rem}

.pill{display:inline-block;font-size:.75rem;font-weight:600;line-height:1;padding:5px 10px;border-radius:999px;white-space:nowrap}
.pill.done,.pill.low{background:var(--done-bg);color:var(--done-fg)}
.pill.wip{background:var(--wip-bg);color:var(--wip-fg)}
.pill.built{background:var(--built-bg);color:var(--built-fg)}
.pill.info{background:var(--info-bg);color:var(--info-fg)}
.pill.afaire{background:var(--hitl-bg);color:var(--hitl-fg)}
.pill.nodecoupe{background:var(--high-bg);color:var(--high-fg)}
.pill.todo{background:var(--todo-bg);color:var(--todo-fg)}
.pill.high{background:var(--high-bg);color:var(--high-fg)}
.pill.med{background:var(--med-bg);color:var(--med-fg)}
.pill.hitl{background:var(--hitl-bg);color:var(--hitl-fg);display:inline-flex;align-items:center;gap:4px}
.pill .hico{flex:none}
.pills{display:inline-flex;flex-wrap:wrap;align-items:center;gap:.4rem}
.tag{display:inline-block;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--chip)}
.chips{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem}
.slug{font-size:.75rem;color:var(--muted);margin-left:.4rem}

/* Epic : carte repliable */
details.epic{background:var(--card);border:1px solid var(--line);border-radius:20px;overflow:hidden}
summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}
.ehead{display:block;padding:1.5rem 1.75rem;transition:background-color 120ms ease-out}
.ehead:hover{background:var(--hover)}
.ehead>span{display:block}
.ehead>.etop{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .75rem}
.ehead>.chips{display:flex}
.chev{display:inline-flex!important;color:var(--muted);transition:transform 200ms ease-out}
details[open]>.ehead .chev{transform:rotate(90deg)}
.eid{font-size:.8rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.etitle{margin:.65rem 0 0 1.9rem;font-size:1.5rem;line-height:1.2;letter-spacing:-.02em;font-weight:650}
.edesc{margin:.5rem 0 0 1.9rem;color:var(--muted);max-width:52rem}
.ehead>.chips{margin:.9rem 0 0 1.9rem}
.eprog{margin:1.1rem 0 0 1.9rem}
.esum{margin-top:.5rem;font-size:.88rem;color:var(--muted)}
.ebody{border-top:1px solid var(--line)}
.ebody>.sub{margin:0;padding:1rem 1.75rem;border-bottom:1px solid var(--line)}
.note,.empty-note{margin:0;padding:1.4rem 1.75rem;color:var(--muted)}
.retro{margin:0;padding:1rem 1.75rem;border-top:1px solid var(--line);color:var(--muted);font-size:.88rem}
.retro-open{margin:0;padding:.25rem 1.75rem .75rem}
.retro-open h4{margin:0 0 .35rem;font-size:.88rem}
.retro-open ul{margin:0;padding:0;list-style:none;font-size:.88rem;line-height:1.5}
.retro-open li{margin:.4rem 0}
.retro-open li .pill{margin-right:.35rem}
.retro-actions li{align-items:flex-start}
.retro-actions li .pill{flex:none;margin-left:auto}
.retro-actions summary .pill{margin-left:.4rem}
.openitems{padding:1.5rem 1.75rem}
.oi-epic{margin-top:1rem}
.oi-epic .retro-open{padding:.4rem 0 0}
.oi-title{all:unset;cursor:pointer;font-weight:600}
.oi-title:hover{text-decoration:underline}
.retro-actions{margin:0 0 0 10px;padding:0 1.75rem 1rem}
.retro-actions ol{list-style:none;margin:.5rem 0 0;padding:0;display:grid;gap:.5rem}
.retro-actions li{display:flex;gap:.75rem;font-size:.88rem;line-height:1.45}
.retro-actions .aid{flex:none;font-weight:600;min-width:2.2rem;color:var(--ink)}
.retro-actions small{display:block;color:var(--muted);margin-top:.1rem}
.retro strong{color:var(--ink);font-weight:600}

/* Story */
.story{border-bottom:1px solid var(--line)}.story:last-of-type{border-bottom:0}
.srow{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .9rem;padding:.9rem 1.75rem;min-height:52px;transition:background-color 120ms ease-out}
.lastdot.lastdot-e{position:static;display:inline-block;margin:0 0 0 .15rem;flex:none}
.srow{position:relative}
.lastdot{position:absolute;left:.7rem;top:50%;width:7px;height:7px;margin-top:-3.5px;border-radius:50%;background:var(--last)}
.srow:hover{background:var(--hover)}.srow:active{background:var(--chip)}
.story[data-status]{--tint:var(--hitl-bg)}
.story[data-status=in-review]{--tint:var(--info-bg)}
.story[data-status=done]{--tint:var(--done-bg)}.story[data-status=built]{--tint:var(--built-bg)}
.story[data-status=in-progress]{--tint:var(--wip-bg)}.story[data-status]>.srow{background:color-mix(in srgb,var(--tint,transparent) 60%,transparent)}
.story[data-status]>.srow:hover{background:color-mix(in srgb,var(--tint,var(--hover)) 90%,transparent)}
.story[data-status]>.srow:active{background:var(--tint,var(--chip))}
.sstatus{margin-left:auto;display:inline-flex;min-width:5.6rem;justify-content:flex-end}
.num{width:2.8rem;font-weight:650;color:var(--muted);font-variant-numeric:tabular-nums}
.tasks{list-style:none;margin:.2rem 0 .6rem;padding:0;display:grid;gap:.45rem}
.tasks li{display:flex;gap:.6rem;align-items:flex-start;line-height:1.45}
.tasks li.plain{padding-left:1.6rem;color:var(--muted);font-size:.92rem}
.tasks .cb{flex:none;width:18px;height:18px;margin-top:2px;border-radius:5px;border:1.5px solid var(--track);display:inline-flex;align-items:center;justify-content:center;color:#fff}
.tasks li.on .cb{background:var(--done);border-color:var(--done)}
.tasks li.on>span:last-child{color:var(--muted)}
.findings{list-style:none;margin:.3rem 0 .6rem;padding:0;display:grid;gap:.6rem}
.findings li{display:flex;gap:.4rem;align-items:flex-start;line-height:1.45;font-size:.9rem}
.findings .pill{flex:none;margin-top:2px}
.findings .ftxt{flex:1 1 0;min-width:0;margin-left:.3rem}
.findings small{display:block;margin-top:.15rem}
.tlabel{margin:.6rem 0 .25rem;font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.stitle{flex:1 1 16rem;min-width:0;font-weight:500}
.stitle .pill{margin-left:.4rem;vertical-align:1px}
.sdeps{display:inline-flex;align-items:center;gap:.2rem;margin-left:.5rem;vertical-align:1px;font-size:.7rem;font-weight:500;font-variant-numeric:tabular-nums;white-space:nowrap}
.sdeps .tag{font-size:.66rem;line-height:1.3;padding:0 5px;border-radius:5px;font-variant-numeric:tabular-nums}
.meta{font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.meta.date{width:4rem;text-align:right}
.sbody{padding:.25rem 1.75rem 1.4rem 5.4rem;display:flex;flex-direction:column;gap:.8rem;animation:arrive 200ms ease-out}
@keyframes arrive{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.lbl{font-size:.72rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:.2rem}
.blk p{max-width:50rem}
.blk.ia{background:var(--chip);border-left:3px solid var(--accent);border-radius:10px;padding:.7rem 1rem .8rem}
.blk.ia .lbl{display:flex;align-items:center;gap:.5rem}
.ia-badge{font-size:.62rem;letter-spacing:.04em;padding:0 6px;border-radius:999px;background:var(--info-bg);color:var(--info-fg);text-transform:none;font-weight:600}
.ia-regen{margin-left:auto;font:inherit;font-size:.78rem;font-weight:500;text-transform:none;letter-spacing:0;color:var(--accent);background:none;border:0;padding:.1rem .3rem;border-radius:6px;cursor:pointer}
.ia-regen:hover{text-decoration:underline}
.ia-text{white-space:pre-line;line-height:1.55}
.ia-text.wait,.ia-text.err{color:var(--muted)}
.ia-text.wait{animation:iapulse 1.4s ease-in-out infinite}
.ia-text.err button{font:inherit;color:var(--accent);background:none;border:0;padding:0 .2rem;cursor:pointer;text-decoration:underline}
@keyframes iapulse{50%{opacity:.45}}
.sub>summary{color:var(--accent);font-weight:500}
.sub[open]>summary{margin-bottom:.5rem}
.deferred-card{padding:1.5rem 1.75rem}
.deferred-card pre{margin:.5rem 0 0;padding:.8rem 1rem;background:var(--soft);border-radius:10px;font:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow:auto}
.dhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem 1.5rem;margin-bottom:.5rem}
.deferred li{margin:.7rem 0}.deferred .res{opacity:.65}
.warn{background:var(--med-bg);color:var(--med-fg);border-radius:12px;padding:.6rem 1rem}
footer.foot{color:var(--muted);font-size:.82rem}

@media (max-width:760px){
  .wrap{padding:0 1rem}
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .global{padding:1.25rem}
  .ehead{padding:1.1rem 1.1rem}.etitle,.edesc,.ehead>.chips,.eprog{margin-left:0}
  .srow{padding:.8rem 1.1rem}.sbody{padding:.25rem 1.1rem 1.2rem}
  .ebody>.sub,.note,.empty-note,.retro{padding-left:1.1rem;padding-right:1.1rem}
}
/* Graphe des dépendances */
.deps{padding:1.5rem 1.75rem}
.dgraph{position:relative;display:flex;align-items:stretch;gap:4.5rem;margin:1.25rem 0 1.5rem;padding:.25rem 0}
.dcol{flex:1 1 0;min-width:0;display:flex;flex-direction:column;justify-content:center;gap:1rem}
.dsvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
.dnode{position:relative;z-index:1;text-align:left;border:1px solid var(--line);background:var(--soft);border-radius:14px;padding:.8rem 1rem;cursor:pointer;
  display:flex;flex-direction:column;gap:.45rem;transition:background-color 120ms ease-out,transform 120ms ease-out,border-color 120ms ease-out}
.dnode:hover,.dnode.hl{background:var(--hover);border-color:var(--muted)}
.dnode[data-state=done]{background:color-mix(in srgb,var(--done) 10%,var(--card));border-color:color-mix(in srgb,var(--done) 50%,var(--line))}
.dnode[data-state=built]{background:color-mix(in srgb,var(--wip) 14%,var(--card));border-color:color-mix(in srgb,var(--wip) 60%,var(--line))}
.dnode[data-state=done]:hover,.dnode[data-state=done].hl{background:color-mix(in srgb,var(--done) 17%,var(--card));border-color:var(--done)}
.dnode[data-state=built]:hover,.dnode[data-state=built].hl{background:color-mix(in srgb,var(--wip) 22%,var(--card));border-color:var(--wip)}
.dnode .pill.built{background:var(--wip-bg);color:var(--wip-fg)}
.dnode:active{transform:scale(.98)}
.dn-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.4rem}
.dn-title{font-weight:600;line-height:1.3;letter-spacing:-.01em;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.dn-state{font-size:.8rem;color:var(--muted)}
.dpath{fill:none;stroke-width:2;stroke:var(--muted);stroke-dasharray:5 5;opacity:.75;transition:opacity 120ms ease-out,stroke-width 120ms ease-out}
.dpath.done{stroke:var(--done);stroke-dasharray:none;opacity:.9}
.dpath.hl{stroke-width:3.5;opacity:1}
.dsvg.has-hl .dpath:not(.hl){opacity:.18}
.dlist{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));gap:.75rem}
.dedge{border:1px solid var(--line);border-radius:14px;padding:.85rem 1rem;transition:background-color 120ms ease-out}
.dedge:hover,.dedge.hl{background:var(--hover)}
.de-head{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .6rem}
.de-needs{margin:.4rem 0 0;color:var(--muted);font-size:.9rem}
.de-stories{margin-top:.6rem;display:flex;flex-wrap:wrap;gap:.35rem}
.de-stories .lbl{width:100%;margin:0}
.linkbtn{border:0;background:var(--chip);color:var(--ink);cursor:pointer;border-radius:8px;padding:2px 9px;min-height:24px;font-size:.78rem;font-variant-numeric:tabular-nums;
  transition:background-color 120ms ease-out,transform 120ms ease-out}
.linkbtn:hover{background:var(--track)}.linkbtn:active{transform:scale(.96)}
@media (max-width:760px){
  .dgraph{flex-direction:column;gap:2.6rem}
  .deps{padding:1.1rem}
}

/* Tags d'exigence cliquables + panneau */
button.tag.req{border:0;cursor:pointer;font:inherit;font-size:.75rem;font-weight:600;color:var(--ink);min-height:24px;padding:2px 9px;
  transition:background-color 120ms ease-out,transform 120ms ease-out}
button.tag.req:hover{background:var(--track)}button.tag.req:active{transform:scale(.95)}
body:has(dialog[open]){overflow:hidden}
dialog.dlg{border:0;padding:0;border-radius:20px;background:var(--card);color:var(--ink);width:min(46rem,calc(100vw - 2rem));
  max-height:min(86vh,60rem);box-shadow:0 24px 64px -12px rgba(0,0,0,.45),0 0 0 1px var(--line);overflow:hidden}
dialog.dlg[open]{display:flex;animation:pop 220ms cubic-bezier(.2,.8,.2,1)}
dialog.dlg::backdrop{background:rgba(10,12,16,.38);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
@keyframes pop{from{opacity:0;transform:scale(.97) translateY(6px)}to{opacity:1;transform:none}}
.rd{display:flex;flex-direction:column;min-height:0;width:100%}
.rd-head{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;padding:1.4rem 1.6rem 1rem;border-bottom:1px solid var(--line)}
.rd-head .eyebrow{margin:0 0 .15rem}
.rd-head h2{font-size:1.6rem;letter-spacing:-.025em}
.rd-close{border:0;background:var(--chip);color:var(--ink);width:44px;height:44px;border-radius:999px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
  transition:background-color 120ms ease-out,transform 120ms ease-out;flex:none}
.rd-close:hover{background:var(--track)}.rd-close:active{transform:scale(.94)}
.rd-body{overflow:auto;padding:1.2rem 1.6rem 1.6rem;display:flex;flex-direction:column;gap:1.3rem;overscroll-behavior:contain}
.rd-quote{margin:0;padding:.9rem 1.1rem;background:var(--soft);border-radius:14px;font-size:1.02rem;line-height:1.55;white-space:pre-wrap}
.rd-quote.verify{font-size:.95rem;color:var(--muted)}
.rd-quote+.rd-quote{margin-top:.5rem}
.rd-quote+.rd-path{margin-top:.5rem}
.rd-src{display:flex;flex-direction:column;gap:.35rem;padding:.8rem 1rem;border:1px solid var(--line);border-radius:14px}
.rd-src b{font-weight:600}
.rd-path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;color:var(--muted);overflow-wrap:anywhere;line-height:1.45}
.rd-src details>summary{color:var(--accent);font-weight:500;font-size:.88rem;margin-top:.15rem}
.rd-src pre{margin:.5rem 0 0;padding:.8rem 1rem;background:var(--soft);border-radius:10px;font:inherit;font-size:.86rem;line-height:1.5;white-space:pre-wrap;max-height:16rem;overflow:auto}
.rd-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.rd-list button{width:100%;text-align:left;border:0;background:transparent;cursor:pointer;padding:.55rem .6rem;border-radius:10px;display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .7rem;min-height:44px;
  transition:background-color 120ms ease-out}
.rd-list button:hover{background:var(--hover)}
.rd-list .n{font-weight:650;color:var(--muted);min-width:2.6rem;font-variant-numeric:tabular-nums}
.rd-list .t{flex:1 1 14rem}
.rd-epic-text{margin:.2rem 0 0 .6rem;padding-left:.9rem;border-left:2px solid var(--line);color:var(--muted);font-size:.9rem;white-space:pre-wrap}

/* Accessibilité : mouvement, transparence, contraste */
@media (prefers-reduced-motion:reduce){
  dialog.dlg[open]{animation:fade 150ms ease}
  html{scroll-behavior:auto}
  *{transition:none!important}
  .live{animation:none}
  .ia-text.wait{animation:none}
  .sbody{animation:fade 150ms ease}
  .crew-btn:active,.chipbtn:active,.cta:active,.ghost:active,button.tag.req:active,.rd-close:active,.dnode:active,.linkbtn:active{transform:none}
}
@keyframes fade{from{opacity:0}to{opacity:1}}
@media (prefers-reduced-transparency:reduce){
  .top{background:var(--bg);backdrop-filter:none;-webkit-backdrop-filter:none}
  dialog.dlg::backdrop{backdrop-filter:none;-webkit-backdrop-filter:none;background:rgba(10,12,16,.6)}
}
@media (prefers-contrast:more){
  :root{--line:#6b6b6b}
  .top{background:var(--bg);backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:0 1px 0 var(--ink)}
  .pill{outline:1px solid currentColor}
}
"""

JS = r"""
const q=s=>[...document.querySelectorAll(s)];
const $=id=>document.getElementById(id);
/* Epics pliables : l'état survit au rafraîchissement, pas à un nouvel onglet */
const epics=q('details.epic');
function saveOpen(){try{const o={};epics.forEach(d=>o[d.id]=d.open);sessionStorage.setItem('suivi-open',JSON.stringify(o))}catch(e){}}
try{const o=JSON.parse(sessionStorage.getItem('suivi-open')||'{}');epics.forEach(d=>{if(d.id in o)d.open=o[d.id]})}catch(e){}
epics.forEach(d=>d.addEventListener('toggle',saveOpen));
$('fold-all').onclick=()=>epics.forEach(d=>d.open=false);
$('unfold-all').onclick=()=>epics.forEach(d=>d.open=true);
/* ---- Panneau d'exigence ---- */
const REQS=JSON.parse($('reqs-data').textContent||'{}');
const dlg=$('req-dialog');
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const STAT={done:['done','Terminé'],built:['built','Développée'],'in-progress':['wip','En cours'],'in-review':['info','in-review'],todo:['afaire','À faire']};
const pillEl=(st)=>{const [k,l]=STAT[st]||['afaire',st];return el('span','pill '+k,l)};
function section(title,...kids){const d=el('div');d.append(el('div','lbl',title),...kids);return d}
function jump(id){
  const t=document.getElementById(id);if(!t)return;
  dlg.close();if(hdlg)hdlg.close();
  let n=t;while(n){if(n.tagName==='DETAILS')n.open=true;n=n.parentElement}
  t.scrollIntoView({block:'center'});
}
function openReq(id){
  const r=REQS[id];if(!r)return;
  $('req-kind').textContent=r.kind==='exigence'?'Exigence':'Contrainte';
  $('req-title').textContent=r.id;
  const body=$('req-body');body.replaceChildren();
  const i=r.text.search(/\sVérifié(?=\s)/);
  const stmt=i>=0?r.text.slice(0,i).trim():r.text, verif=i>=0?r.text.slice(i).trim():'';
  const origin=el('div','rd-path','Défini dans '+r.path+' · ligne '+r.line);
  const ex=section('Extrait',el('p','rd-quote',stmt));
  if(verif)ex.append(el('p','rd-quote verify',verif));
  ex.append(origin);body.append(ex);

  if(r.sources.length){
    const box=section('Sources');
    r.sources.forEach(s=>{
      const c=el('div','rd-src');
      c.append(el('b',null,s.label));
      if(s.kind==='file'){
        c.append(el('div','rd-path',s.path+(s.line?' · ligne '+s.line:'')+(s.heading?' · '+s.heading:'')));
        if(s.excerpt){const d=el('details');d.append(el('summary',null,'Extrait de la source'),el('pre',null,s.excerpt));c.append(d)}
        else c.append(el('span','muted small','Section introuvable dans le document.'));
      }else c.append(el('span','muted small','Pas de document : décision ou remarque citée dans l\'exigence.'));
      box.append(c);
    });
    box.querySelectorAll('.rd-src').forEach(x=>x.style.marginTop='.5rem');
    body.append(box);
  }
  const refined=r.epics.filter(e=>e.text);
  if(refined.length){
    const box=section('Précisé dans les epics');
    refined.forEach(e=>{const w=el('div');w.style.marginTop='.4rem';w.append(el('b',null,'Epic '+e.id+' — '+e.title),el('p','rd-epic-text',e.text));box.append(w)});
    body.append(box);
  }
  if(r.epics.length){
    const ul=el('ul','rd-list');
    r.epics.forEach(e=>{const li=el('li'),b=el('button');b.type='button';
      b.append(el('span','n','Epic '+e.id),el('span','t',e.title));
      b.addEventListener('click',()=>jump('epic-'+e.id));li.append(b);ul.append(li)});
    body.append(section('Epics concernés',ul));
  }
  if(r.stories.length){
    const ul=el('ul','rd-list');
    r.stories.forEach(s=>{const li=el('li'),b=el('button');b.type='button';
      b.append(el('span','n',s.num),el('span','t',s.title),pillEl(s.status));
      b.addEventListener('click',()=>jump('story-'+s.num.replace('.','-')));li.append(b);ul.append(li)});
    body.append(section('Stories qui la couvrent ('+r.stories.length+')',ul));
  }
  dlg.showModal();body.scrollTop=0;
}
document.addEventListener('click',ev=>{
  const b=ev.target.closest('button.req');
  if(b){ev.preventDefault();ev.stopPropagation();openReq(b.dataset.req)}
});
$('req-close').onclick=()=>dlg.close();
const hdlg=$('hitl-dialog');
if(hdlg){$('hitl-open').onclick=()=>hdlg.showModal();$('hitl-close').onclick=()=>hdlg.close();
  hdlg.addEventListener('click',ev=>{if(ev.target===hdlg)hdlg.close()})}
dlg.addEventListener('click',ev=>{if(ev.target===dlg)dlg.close()});
/* ---- Fiches des personas ---- */
document.addEventListener('click',ev=>{
  const b=ev.target.closest('[data-persona]');
  if(b){const d=$('persona-'+b.dataset.persona);if(d)d.showModal();return}
  const x=ev.target.closest('dialog.dlg-persona .rd-close');
  if(x){x.closest('dialog').close();return}
  if(ev.target.matches('dialog.dlg-persona'))ev.target.close();
});

/* ---- Graphe des dépendances ---- */
const dg=$('dgraph'), dsvg=$('dsvg');
const SVGNS='http://www.w3.org/2000/svg';
function drawEdges(){
  if(!dg||!dsvg)return;
  const gr=dg.getBoundingClientRect();
  const vertical=matchMedia('(max-width:760px)').matches;
  dsvg.replaceChildren();
  const defs=document.createElementNS(SVGNS,'defs');
  [['done','var(--done)'],['pending','var(--muted)']].forEach(([k,c])=>{
    const m=document.createElementNS(SVGNS,'marker');
    m.setAttribute('id','arrow-'+k);m.setAttribute('viewBox','0 0 10 10');m.setAttribute('refX','9');m.setAttribute('refY','5');
    m.setAttribute('markerWidth','7');m.setAttribute('markerHeight','7');m.setAttribute('orient','auto-start-reverse');
    const p=document.createElementNS(SVGNS,'path');p.setAttribute('d','M1 1L9 5L1 9z');p.setAttribute('style','fill:'+c);
    m.append(p);defs.append(m)});
  dsvg.append(defs);
  q('.dedge').forEach(li=>{
    const a=dg.querySelector('.dnode[data-epic="'+li.dataset.from+'"]'),b=dg.querySelector('.dnode[data-epic="'+li.dataset.to+'"]');
    if(!a||!b)return;
    const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();
    let d;
    if(!vertical){
      const x1=ra.right-gr.left,y1=ra.top+ra.height/2-gr.top,x2=rb.left-gr.left-2,y2=rb.top+rb.height/2-gr.top,dx=Math.max(24,(x2-x1)/2);
      d='M'+x1+','+y1+' C'+(x1+dx)+','+y1+' '+(x2-dx)+','+y2+' '+x2+','+y2;
    }else{
      const x1=ra.left+ra.width/2-gr.left,y1=ra.bottom-gr.top,x2=rb.left+rb.width/2-gr.left,y2=rb.top-gr.top-2,dy=Math.max(20,(y2-y1)/2);
      d='M'+x1+','+y1+' C'+x1+','+(y1+dy)+' '+x2+','+(y2-dy)+' '+x2+','+y2;
    }
    const p=document.createElementNS(SVGNS,'path');
    p.setAttribute('d',d);p.setAttribute('class','dpath '+li.dataset.state);
    p.setAttribute('marker-end','url(#arrow-'+li.dataset.state+')');
    p.dataset.from=li.dataset.from;p.dataset.to=li.dataset.to;dsvg.append(p);
  });
}
function highlight(from,to,epic){
  let any=false;
  q('.dpath').forEach(p=>{const on=epic?(p.dataset.from===epic||p.dataset.to===epic):(p.dataset.from===from&&p.dataset.to===to);p.classList.toggle('hl',on);any=any||on});
  q('.dedge').forEach(li=>{const on=epic?(li.dataset.from===epic||li.dataset.to===epic):(li.dataset.from===from&&li.dataset.to===to);li.classList.toggle('hl',on)});
  q('.dnode').forEach(n=>{const e=n.dataset.epic;const on=epic?e===epic||q('.dedge.hl').some(li=>li.dataset.from===e||li.dataset.to===e):(e===from||e===to);n.classList.toggle('hl',on&&(any||epic))});
  dsvg.classList.toggle('has-hl',any);
}
function clearHl(){highlight(null,null,null);q('.dnode,.dedge,.dpath').forEach(x=>x.classList.remove('hl'));dsvg.classList.remove('has-hl')}
if(dg){
  drawEdges();
  new ResizeObserver(drawEdges).observe(dg);
  matchMedia('(max-width:760px)').addEventListener('change',drawEdges);
  if(document.fonts&&document.fonts.ready)document.fonts.ready.then(drawEdges);
  q('.dnode').forEach(n=>{
    n.addEventListener('mouseenter',()=>highlight(null,null,n.dataset.epic));n.addEventListener('focus',()=>highlight(null,null,n.dataset.epic));
    n.addEventListener('mouseleave',clearHl);n.addEventListener('blur',clearHl)});
  q('.dedge').forEach(li=>{
    li.addEventListener('mouseenter',()=>highlight(li.dataset.from,li.dataset.to,null));li.addEventListener('mouseleave',clearHl)});
}
/* ---- Synthèse IA : générée à l'ouverture d'une story, gardée en mémoire de la page seulement ---- */
const iaDone=new Map(), iaRun=new Map();
function iaStart(story,force){
  const box=story.querySelector('[data-ia]');if(!box)return;
  const txt=box.querySelector('.ia-text'),regen=box.querySelector('.ia-regen');
  const key=story.id.replace(/^story-/,'');
  if(iaRun.has(key))return;
  if(!force&&iaDone.has(key)){txt.className='ia-text';txt.textContent=iaDone.get(key);regen.hidden=false;return}
  const ctl=new AbortController();iaRun.set(key,ctl);
  regen.hidden=true;txt.className='ia-text wait';txt.textContent="L'IA lit la story…";
  let acc='',started=false;
  const fail=msg=>{
    txt.className='ia-text err';txt.textContent=msg+' ';
    const b=document.createElement('button');b.type='button';b.textContent='Réessayer';
    b.onclick=()=>iaStart(story,true);txt.append(b);
  };
  const onEvent=ev=>{
    if(ctl.signal.aborted)return;
    if(ev.t){if(!started){started=true;txt.className='ia-text';acc=''}acc+=ev.t;txt.textContent=acc.trimStart()}
    else if(ev.err)throw new Error(ev.err);
  };
  (async()=>{
    try{
      const r=await fetch('/api/synthese/'+key,{signal:ctl.signal,cache:'no-store'});
      if(!r.ok)throw new Error(r.status===404?'Story introuvable.':'Erreur du serveur ('+r.status+').');
      const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
      for(;;){
        const {value,done}=await rd.read();
        buf+=dec.decode(value||new Uint8Array(),{stream:!done});
        const lines=buf.split('\n');buf=done?'':lines.pop();
        lines.forEach(l=>{if(l.trim())onEvent(JSON.parse(l))});
        if(done)break;
      }
      if(!started)throw new Error("L'IA n'a rien répondu.");
      acc=acc.trim();txt.textContent=acc;iaDone.set(key,acc);regen.hidden=false;
    }catch(e){
      if(e.name==='AbortError')return;
      fail(e.message||'Synthèse indisponible.');
    }finally{if(iaRun.get(key)===ctl)iaRun.delete(key)}
  })();
}
document.addEventListener('toggle',ev=>{
  const d=ev.target;if(!d.matches||!d.matches('details.story'))return;
  if(d.open)iaStart(d,false);
  else{const k=d.id.replace(/^story-/,''),c=iaRun.get(k);if(c){iaRun.delete(k);c.abort()}}
},true);
document.addEventListener('click',ev=>{
  const b=ev.target.closest('.ia-regen');if(!b)return;
  ev.preventDefault();iaStart(b.closest('details.story'),true);
});
q('details.story[open]').forEach(d=>iaStart(d,false));

/* tout élément [data-jump] mène à un epic ou à une story */
document.addEventListener('click',ev=>{
  const b=ev.target.closest('[data-jump]');if(b){ev.preventDefault();jump(b.dataset.jump)}
});

/* un lien de navigation déplie l'epic visé */
q('.nav a').forEach(a=>a.addEventListener('click',()=>{
  const t=document.querySelector(a.getAttribute('href'));if(t&&t.tagName==='DETAILS')t.open=true}));
"""


def render(data: dict) -> str:
    all_stories = [s for e in data["epics"] for s in e["stories"]]
    c = counts(all_stories)
    hitl_open = [
        (e, s) for e in data["epics"] for s in e["stories"] if s.get("hitl") and s["status"] != "done"
    ]
    undecided = sum(1 for e in data["epics"] if not e["_has_stories"])
    imeta = data["initiative"]["meta"]
    mt = data["mtime"].strftime("%d/%m/%Y %H:%M:%S") if data["mtime"] else "—"
    warns = "".join(f'<div class="warn">⚠ {esc(w)}</div>' for w in data["warnings"])

    hitl_label = (
        "HITL en attente · " + ", ".join(f"{e['id']}.{s['id']}" for e, s in hitl_open)
        if hitl_open
        else "HITL en attente"
    )
    pct = round((c["done"] + c["built"]) / c["total"] * 100) if c["total"] else 0
    i_status = INIT_STATUS_FR.get(str(imeta.get("status", "")), str(imeta.get("status", "?")))
    i_risk = RISK_FR.get(str(imeta.get("risk", "")), str(imeta.get("risk", "?")))

    kpis = [
        (len(data["epics"]), f"epics, dont {undecided} non découpés"),
        (c["total"], "stories découpées"),
        (c["done"], "terminées"),
        (c["todo"], "à coder"),
        (len(hitl_open), hitl_label),
    ]
    kpis_html = "".join(
        (
            f'<button type="button" class="kpi kpi-btn" id="hitl-open" aria-haspopup="dialog">'
            f'<b>{v}</b><span>{esc(lab)}</span></button>'
            if lab.startswith("HITL") and v
                        else f'<div class="kpi"><b>{v}</b><span>{esc(lab)}</span></div>'
        )
        for v, lab in kpis
    )
    hitl_rows = "".join(
        f'<li><button type="button" data-jump="story-{esc(str(e["id"]))}-{esc(str(st["id"]))}">'
        f'<span class="n">{esc(str(e["id"]))}.{esc(str(st["id"]))}</span>'
        f'<span class="t">{esc(st.get("title", ""))}<small class="muted"> {esc(e.get("slug", ""))}</small></span>'
        f'{status_pill(st["status"])}</button></li>'
        for e, st in hitl_open
    )
    hitl_dialog = (
        f'''<dialog class="dlg" id="hitl-dialog" aria-labelledby="hitl-title"><div class="rd">
    <header class="rd-head"><div><p class="eyebrow">Intervention humaine</p><h2 id="hitl-title">HITL en attente ({len(hitl_open)})</h2></div>
      <button type="button" class="rd-close" id="hitl-close" aria-label="Fermer"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13"/></svg></button></header>
    <div class="rd-body"><ul class="rd-list">{hitl_rows}</ul></div></div></dialog>'''
        if hitl_open
        else ""
    )

    epics_html = "".join(render_epic(e, data) for e in data["epics"])
    reqs_json = json.dumps(data.get("reqs", {}), ensure_ascii=False).replace("<", "\\u003c")
    nav = "".join(
        f'<a class="chipbtn" href="#epic-{esc(str(e["id"]))}" title="{esc(e.get("title", ""))}">Epic {esc(str(e["id"]))}</a>'
        for e in data["epics"]
    )
    if True:  # le panneau « Exigences » existe toujours (liste, ou explication si absentes)
        nav = '<a class="chipbtn" href="#exigences">Exigences</a>' + nav
    if "id=\"open-items\"" in render_open_items(data):
        nav = '<a class="chipbtn" href="#open-items">Points ouverts</a>' + nav
    if data.get("links", {}).get("edges"):
        nav = '<a class="chipbtn" href="#deps">Dépendances</a>' + nav
    if data["deferred"]:
        nav += '<a class="chipbtn" href="#deferred">Travaux reportés</a>'
    if data.get("decisions"):
        nav += '<a class="chipbtn" href="#decisions">Décisions</a>'

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(Path(data['root']).name)} · Suivi BMAD</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}"><style>{CSS}</style></head>
<body>
<header class="top"><div class="wrap">
  <div class="brand"><span class="logo" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="var(--bg)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8.5l3.5 3.5L14 4"/></svg></span>Suivi BMAD</div>
  <nav class="nav" aria-label="Aller à">{nav}</nav>
  <a class="cta" href="" onclick="location.reload();return false">Rafraîchir</a>
</div></header>
<main class="wrap">
  <section class="ihead">
    <div>
    <p class="eyebrow" aria-hidden="true">&nbsp;</p>
    <h1>{esc(Path(data['root']).name)}</h1>
    <p class="idesc">{esc(data['initiative']['title'])}</p>
    </div>
    <div class="foldbar">
      <button type="button" class="ghost" id="fold-all">Tout plier</button>
      <button type="button" class="ghost" id="unfold-all">Tout déplier</button>
    </div>
  </section>
  {warns}
  <section class="card global" aria-label="Avancement global">
    <div class="gleft">
      <div class="big"><b>{pct}</b><span>%</span></div>
      <p class="cap">des stories découpées sont codées (terminées ou développées)</p>
      {bar(c)}
      <div class="legend">
        <span><i style="background:var(--done)"></i>Terminées</span>
        <span><i style="background:var(--built)"></i>Développées</span>
        <span><i style="background:var(--track)"></i>À faire</span>
      </div>
    </div>
    <div class="kpis">{kpis_html}</div>
  </section>
  {render_crew(data)}
  {render_requirements(data)}
  {render_links(data)}
  {epics_html}
  {render_deferred(data['deferred'])}
  {render_decisions(data.get('decisions', []))}
  {render_open_items(data)}
  <footer class="foot">Généré le {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} · dernière modif des fichiers : {mt} · <code>{esc(data['root'])}</code> · lecture seule</footer>
</main>
{hitl_dialog}
<dialog class="dlg" id="req-dialog" aria-labelledby="req-title">
  <div class="rd">
    <header class="rd-head">
      <div><p class="eyebrow" id="req-kind"></p><h2 id="req-title"></h2></div>
      <button type="button" class="rd-close" id="req-close" aria-label="Fermer">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13"/></svg>
      </button>
    </header>
    <div class="rd-body" id="req-body"></div>
  </div>
</dialog>
<script type="application/json" id="reqs-data">{reqs_json}</script>
<script>{JS}</script></body></html>"""


# --------------------------------------------------------------------------
# Serveur
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Synthèse IA (Ollama local) : générée à la demande, jamais enregistrée
# --------------------------------------------------------------------------

IA_DEFAULTS = {"url": "http://localhost:11434", "model": "qwen3.8-27b-96k", "timeout": 120}
IA_SYSTEM = (
    "Tu expliques une user story d'un projet logiciel à quelqu'un qui ne connaît ni le projet ni la méthode BMAD. "
    "Réponds en français, en texte brut (pas de markdown, pas de listes à puces, pas de gras), sans jargon : "
    "si un terme technique est indispensable, explique-le avec des mots simples. "
    "N'écris aucun identifiant (R12, C3, 2.4, AD-9…). "
    "Fais 3 à 5 phrases courtes, dans cet ordre : ce que la story fait concrètement ; à quoi ça sert ; "
    "comment on saura que c'est réussi. "
    "Base-toi uniquement sur les informations fournies, n'invente rien ; si une information manque, dis-le simplement."
)


def ia_settings(conf: dict) -> dict:
    """Réglages `ia_url`, `ia_model`, `ia_timeout` de suivi.toml, avec valeurs par défaut."""
    try:
        timeout = float(conf.get("ia_timeout") or IA_DEFAULTS["timeout"])
    except (TypeError, ValueError):
        timeout = IA_DEFAULTS["timeout"]
    return {
        "url": str(conf.get("ia_url") or IA_DEFAULTS["url"]).rstrip("/"),
        "model": str(conf.get("ia_model") or IA_DEFAULTS["model"]),
        "timeout": timeout,
    }


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def story_brief(data: dict, epic_id: str, story_id: str) -> str | None:
    """Contexte en texte brut d'une story, pour l'IA. None si la story n'existe pas."""
    epic = next((e for e in data["epics"] if str(e.get("id")) == epic_id), None)
    story = next((s for s in epic["stories"] if str(s.get("id")) == story_id), None) if epic else None
    if not epic or not story:
        return None
    lines = [
        f"Projet : {data['initiative']['title']}",
        f"Epic (grand lot de travail) : {epic.get('title', '')}",
    ]
    if epic.get("description"):
        lines.append(f"But de l'epic : {_clip(str(epic['description']), 800)}")
    lines += ["", f"User story : {story.get('title', '')}"]
    if story.get("description"):
        lines.append(f"Description : {_clip(str(story['description']), 2500)}")
    if story.get("verify"):
        lines.append(f"Vérification attendue : {_clip(str(story['verify']), 1200)}")
    lines.append(f"Avancement : {STATUS_LABEL.get(story['status'], story['status'])}")
    reqs = [data["reqs"][str(c)] for c in story.get("covers", []) if str(c) in data.get("reqs", {})]
    if reqs:
        lines += ["", "Exigences du projet que cette story couvre :"]
        lines += [f"- {_clip(str(r['text']), 500)}" for r in reqs[:8]]
    if story.get("after"):
        deps = [resolve_after(epic, data, r)[1] for r in story["after"]]
        deps = [d for d in deps if d]
        if deps:
            lines += ["", "À faire avant (dépendances) : " + " ; ".join(deps)]
    plan = story.get("plan")
    if plan:
        sec = plan["sections"]
        for key, label, limit in (
            ("Intent", "Intention du plan", 2000),
            ("Implementation Notes", "Notes d'implémentation", 2500),
            ("Tasks & Acceptance", "Tâches et critères d'acceptation", 3500),
        ):
            if sec.get(key):
                lines += ["", f"{label} :", _clip(sec[key], limit)]
    return "\n".join(lines)


def stream_synthese(brief: str, ia: dict):
    """Générateur d'événements {"t": texte} puis {"done": True}, ou {"err": message}.
    La fermeture du générateur ferme la connexion vers Ollama, donc arrête la génération."""
    body = json.dumps({
        "model": ia["model"],
        "stream": True,
        "think": False,  # sinon Qwen « réfléchit » longuement avant le premier mot
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": IA_SYSTEM},
            {"role": "user", "content": brief},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        ia["url"] + "/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=ia["timeout"]) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("error"):
                    yield {"err": f"Ollama : {msg['error']}"}
                    return
                piece = (msg.get("message") or {}).get("content") or ""
                if piece:
                    yield {"t": piece}
                if msg.get("done"):
                    break
        yield {"done": True}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            yield {"err": f"Modèle « {ia['model']} » introuvable dans Ollama (ollama pull {ia['model']})."}
        else:
            yield {"err": f"Ollama a répondu une erreur {e.code}."}
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, TimeoutError):
            yield {"err": "Ollama met trop de temps à répondre."}
        else:
            yield {"err": f"Ollama ne répond pas sur {ia['url']}. Est-il lancé ?"}
    except (TimeoutError, OSError):
        yield {"err": "La connexion avec Ollama a été interrompue ou a dépassé le délai."}


def make_handler(root: Path, ia: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")
            elif path.startswith("/api/synthese/"):
                self._synthese(path.removeprefix("/api/synthese/"))
            elif path == "/":
                try:
                    body = render(load_all(root))
                    self._send(200, body)
                except Exception as e:  # noqa: BLE001
                    self._send(500, f"<pre>Erreur : {esc(repr(e))}</pre>")
            elif path.startswith("/personas/"):
                self._persona(path.removeprefix("/personas/"))
            else:
                self._send(404, "Introuvable", "text/plain; charset=utf-8")

        def _synthese(self, key: str):
            eid, _, sid = key.partition("-")
            try:
                brief = story_brief(load_all(root), eid, sid)
            except Exception as e:  # noqa: BLE001
                self._send(500, f"Erreur : {e!r}", "text/plain; charset=utf-8")
                return
            if brief is None:
                self._send(404, "Story introuvable", "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            events = stream_synthese(brief, ia)
            try:
                for ev in events:
                    self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # page fermée ou story repliée : on arrête
            finally:
                events.close()  # coupe la connexion vers Ollama

        def _persona(self, pid: str):
            img = persona_image(pid) if pid in {p[0] for p in PERSONAS} else None
            if img is None:
                self._send(404, "Introuvable", "text/plain; charset=utf-8")
                return
            st = img.stat()
            etag = f'"{int(st.st_mtime)}-{st.st_size}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._send(
                200, img.read_bytes(), IMAGE_TYPES[img.suffix.lower()],
                cache="no-cache", etag=etag,
            )

        def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
                  cache: str = "no-store", etag: str | None = None):
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", cache)
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):  # silencieux
            pass

    return Handler


def load_config() -> dict:
    """Lit suivi.toml (facultatif). Un fichier illisible arrête le programme avec un message clair."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"suivi.toml illisible ({CONFIG_FILE}) : {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="dossier de l'initiative (contient tickets/) ; défaut : `root` de suivi.toml")
    ap.add_argument("--port", type=int, help="défaut : `port` de suivi.toml, sinon 8765")
    ap.add_argument("--host", help="défaut : `host` de suivi.toml, sinon 127.0.0.1")
    args = ap.parse_args()
    conf = load_config()

    raw_root = args.root or conf.get("root")
    if not raw_root:
        raise SystemExit(
            f"Aucun dossier à suivre. Renseigner `root = \"...\"` dans {CONFIG_FILE} ou passer --root CHEMIN."
        )
    root = Path(str(raw_root)).expanduser()
    if not root.is_absolute() and not args.root:
        root = CONFIG_FILE.parent / root  # chemin relatif du fichier de conf : relatif au fichier
    root = root.resolve()
    if not (root / "tickets").is_dir():
        raise SystemExit(f"Dossier tickets/ introuvable dans {root}")
    host = args.host or conf.get("host") or "127.0.0.1"
    port = args.port or int(conf.get("port") or 8765)

    ia = ia_settings(conf)
    srv = ThreadingHTTPServer((host, port), make_handler(root, ia))
    print(f"Suivi BMAD sur http://{host}:{port}  (racine : {root})  — Ctrl+C pour arrêter")
    print(f"Synthèse IA : {ia['model']} via {ia['url']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
