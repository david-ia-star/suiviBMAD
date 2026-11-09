#!/usr/bin/env bash
# Démarre / arrête le tableau de bord Suivi BMAD (suivi.py) en arrière-plan.
#
#   ./suivi.sh start   [--port N] [--host H] [--root CHEMIN]
#   (sans option : lit suivi.toml, à côté de ce script)
#   ./suivi.sh stop
#   ./suivi.sh restart [--port N] [--host H] [--root CHEMIN]   (sans option : mêmes réglages qu'avant)
#   ./suivi.sh status  [--json]
#   ./suivi.sh url
#   ./suivi.sh logs    [-n LIGNES] [-f]
#
# Pensé pour être piloté par un humain ou par une IA :
#   - jamais interactif, jamais de couleur, une ligne de sortie lisible par commande ;
#   - idempotent : `start` sur un serveur déjà lancé et `stop` sur un serveur arrêté réussissent ;
#   - `status --json` donne un objet JSON stable ;
#   - le serveur survit à la fermeture du terminal ; état, PID et journal sont dans .run/.
#
# Codes de sortie :
#   0 ok · 1 échec du démarrage (voir `logs`) · 2 usage · 3 serveur arrêté (status, url)
#   4 déjà lancé avec d'autres réglages (utiliser restart) · 5 port déjà pris par un autre programme
#
# Variables d'environnement (valeurs par défaut) : SUIVI_PORT (8765), SUIVI_HOST (127.0.0.1), SUIVI_ROOT (défauts : suivi.toml, puis réglages de suivi.py).

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$DIR/.run"
STATE="$RUN/suivi.state"
LOG="$RUN/suivi.log"
START_TIMEOUT=15   # secondes d'attente de /healthz
STOP_TIMEOUT=5     # secondes avant SIGKILL

die() { local code="${2:-1}"; echo "suivi: $1" >&2; exit "$code"; }

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }

# --- état ------------------------------------------------------------------

S_PID="" S_HOST="" S_PORT="" S_ROOT="" S_STARTED=""

load_state() {
  S_PID="" S_HOST="" S_PORT="" S_ROOT="" S_STARTED=""
  [ -f "$STATE" ] || return 1
  # shellcheck disable=SC1090
  . "$STATE" 2>/dev/null || return 1
  S_PID="${PID:-}" S_HOST="${HOST:-}" S_PORT="${PORT:-}" S_ROOT="${ROOT:-}" S_STARTED="${STARTED:-}"
  [ -n "$S_PID" ] && [ -n "$S_PORT" ]
}

save_state() { # pid host port root
  mkdir -p "$RUN"
  {
    printf 'PID=%q\nHOST=%q\nPORT=%q\nROOT=%q\nSTARTED=%q\n' "$1" "$2" "$3" "$4" "$(date -Iseconds)"
  } > "$STATE"
}

# Le PID est-il bien notre serveur (et pas un PID réutilisé par autre chose) ?
is_ours() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q 'suivi\.py'
}

healthy() { # host port
  command -v curl >/dev/null 2>&1 || return 0   # pas de curl : on se contente du PID
  [ "$(curl -fsS -m 2 "http://$1:$2/healthz" 2>/dev/null)" = "ok" ]
}

port_busy() { # host port : quelque chose écoute déjà ?
  if command -v ss >/dev/null 2>&1; then
    [ -n "$(ss -H -ltn "sport = :$2" 2>/dev/null)" ]
  else
    timeout 2 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>/dev/null
  fi
}

url_of() { echo "http://$1:$2"; }

jstr() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

# --- options ---------------------------------------------------------------

OPT_HOST="" OPT_PORT="" OPT_ROOT=""
parse_opts() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --port)  [ $# -ge 2 ] || die "--port attend un numéro" 2; OPT_PORT="$2"; shift 2 ;;
      --host)  [ $# -ge 2 ] || die "--host attend une adresse" 2; OPT_HOST="$2"; shift 2 ;;
      --root)  [ $# -ge 2 ] || die "--root attend un chemin" 2; OPT_ROOT="$2"; shift 2 ;;
      *) die "option inconnue : $1 (voir ./suivi.sh help)" 2 ;;
    esac
  done
  if [ -n "$OPT_PORT" ] && ! [[ "$OPT_PORT" =~ ^[0-9]+$ ]]; then die "port invalide : $OPT_PORT" 2; fi
}

# --- commandes -------------------------------------------------------------

# Lit une clé simple (`clé = "valeur"` ou `clé = 123`) de suivi.toml ; vide si absente.
conf_get() {
  [ -f "$DIR/suivi.toml" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*[\"']\{0,1\}\([^\"'#]*[^\"'# ]\).*/\1/p" "$DIR/suivi.toml" | head -n 1
}

cmd_start() {
  parse_opts "$@"
  local host="${OPT_HOST:-${SUIVI_HOST:-$(conf_get host)}}"; host="${host:-127.0.0.1}"
  local port="${OPT_PORT:-${SUIVI_PORT:-$(conf_get port)}}"; port="${port:-8765}"
  local root="${OPT_ROOT:-${SUIVI_ROOT:-}}"

  if load_state && is_ours "$S_PID"; then
    # déjà lancé : réussite si les réglages demandés (s'il y en a) sont les mêmes
    if { [ -n "$OPT_PORT" ] && [ "$OPT_PORT" != "$S_PORT" ]; } \
       || { [ -n "$OPT_HOST" ] && [ "$OPT_HOST" != "$S_HOST" ]; } \
       || { [ -n "$OPT_ROOT" ] && [ "$OPT_ROOT" != "$S_ROOT" ]; }; then
      die "déjà lancé (pid $S_PID, $(url_of "$S_HOST" "$S_PORT")) avec d'autres réglages ; utiliser : ./suivi.sh restart ..." 4
    fi
    echo "déjà lancé pid=$S_PID url=$(url_of "$S_HOST" "$S_PORT")"
    exit 0
  fi
  rm -f "$STATE"   # état périmé

  command -v uv >/dev/null 2>&1 || die "uv est introuvable dans le PATH" 1
  if port_busy "$host" "$port"; then
    die "le port $port est déjà utilisé par un autre programme (essayer --port N)" 5
  fi

  mkdir -p "$RUN"
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 1000 ]; then tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
  printf '\n=== %s start host=%s port=%s root=%s ===\n' "$(date -Iseconds)" "$host" "$port" "${root:-défaut}" >> "$LOG"

  local args=(--host "$host" --port "$port")
  [ -n "$root" ] && args+=(--root "$root")

  cd "$DIR" || die "dossier introuvable : $DIR" 1
  # setsid : nouvelle session, donc le serveur survit au terminal et on peut tuer tout son groupe.
  setsid nohup uv run suivi.py "${args[@]}" >> "$LOG" 2>&1 < /dev/null &
  local pid=$!
  save_state "$pid" "$host" "$port" "${root:-}"

  local i max=$((START_TIMEOUT * 5))
  for ((i = 0; i < max; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$STATE"
      echo "suivi: le serveur s'est arrêté au démarrage. Dernières lignes du journal :" >&2
      tail -n 15 "$LOG" >&2
      exit 1
    fi
    if healthy "$host" "$port" && port_busy "$host" "$port"; then
      echo "lancé pid=$pid url=$(url_of "$host" "$port") log=$LOG"
      exit 0
    fi
    sleep 0.2
  done
  echo "suivi: pas de réponse après ${START_TIMEOUT}s ; arrêt." >&2
  tail -n 15 "$LOG" >&2
  kill -TERM -- "-$pid" 2>/dev/null; rm -f "$STATE"
  exit 1
}

cmd_stop() {
  [ $# -eq 0 ] || die "stop n'accepte aucune option : $*" 2
  if ! load_state; then echo "déjà arrêté"; exit 0; fi
  local pid="$S_PID"
  if ! is_ours "$pid"; then
    rm -f "$STATE"; echo "déjà arrêté (état périmé nettoyé)"; exit 0
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  local i max=$((STOP_TIMEOUT * 5))
  for ((i = 0; i < max; i++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    sleep 0.3
  fi
  if kill -0 "$pid" 2>/dev/null; then die "impossible d'arrêter le pid $pid" 1; fi
  rm -f "$STATE"
  echo "arrêté pid=$pid"
}

cmd_restart() {
  # sans option, on reprend les réglages de l'instance en cours
  local prev_host="" prev_port="" prev_root=""
  if load_state; then prev_host="$S_HOST" prev_port="$S_PORT" prev_root="$S_ROOT"; fi
  "$0" stop || exit $?
  local args=("$@")
  if [ ${#args[@]} -eq 0 ]; then
    [ -n "$prev_host" ] && args+=(--host "$prev_host")
    [ -n "$prev_port" ] && args+=(--port "$prev_port")
    [ -n "$prev_root" ] && args+=(--root "$prev_root")
  fi
  exec "$0" start "${args[@]}"
}

cmd_status() {
  local json=0
  case "${1:-}" in
    "") ;;
    --json) json=1 ;;
    *) die "option inconnue : $1" 2 ;;
  esac
  local running=0 ok=0
  if load_state && is_ours "$S_PID"; then
    running=1
    healthy "$S_HOST" "$S_PORT" && ok=1
  fi
  if [ "$json" = 1 ]; then
    if [ "$running" = 1 ]; then
      printf '{"running":true,"healthy":%s,"pid":%s,"host":%s,"port":%s,"url":%s,"root":%s,"started":%s,"log":%s}\n' \
        "$([ "$ok" = 1 ] && echo true || echo false)" "$S_PID" "$(jstr "$S_HOST")" "$S_PORT" \
        "$(jstr "$(url_of "$S_HOST" "$S_PORT")")" "$(jstr "${S_ROOT:-défaut}")" "$(jstr "$S_STARTED")" "$(jstr "$LOG")"
    else
      printf '{"running":false,"healthy":false,"log":%s}\n' "$(jstr "$LOG")"
    fi
  else
    if [ "$running" = 1 ]; then
      echo "lancé pid=$S_PID url=$(url_of "$S_HOST" "$S_PORT") healthy=$([ "$ok" = 1 ] && echo oui || echo non) depuis=$S_STARTED root=${S_ROOT:-défaut}"
    else
      echo "arrêté"
    fi
  fi
  [ "$running" = 1 ] && exit 0 || exit 3
}

cmd_url() {
  [ $# -eq 0 ] || die "url n'accepte aucune option : $*" 2
  if load_state && is_ours "$S_PID"; then url_of "$S_HOST" "$S_PORT"; exit 0; fi
  echo "arrêté" >&2; exit 3
}

cmd_logs() {
  local n=50 follow=0
  while [ $# -gt 0 ]; do
    case "$1" in
      -n) [ $# -ge 2 ] || die "-n attend un nombre" 2; n="$2"; shift 2 ;;
      -f) follow=1; shift ;;
      *) die "option inconnue : $1" 2 ;;
    esac
  done
  [ -f "$LOG" ] || { echo "pas encore de journal ($LOG)" >&2; exit 0; }
  if [ "$follow" = 1 ]; then tail -n "$n" -f "$LOG"; else tail -n "$n" "$LOG"; fi
}

case "${1:-help}" in
  start)   shift; cmd_start "$@" ;;
  stop)    shift; cmd_stop "$@" ;;
  restart) shift; cmd_restart "$@" ;;
  status)  shift; cmd_status "$@" ;;
  url)     shift; cmd_url "$@" ;;
  logs)    shift; cmd_logs "$@" ;;
  help|-h|--help) usage ;;
  *) echo "suivi: commande inconnue : $1" >&2; usage >&2; exit 2 ;;
esac
