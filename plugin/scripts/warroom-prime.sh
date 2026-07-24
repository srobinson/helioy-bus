#!/usr/bin/env bash
# warroom-prime.sh — automate warroom pane PRIMING (standby line, then skills).
#
# Lives next to warroom.sh. Encodes the Priming & Compaction choreography from
# the warroom skill: context first, then skills; confirm every line via
# capture-pane; never assume a send-keys line landed. No existing prime helper
# was found (warroom.sh / helioy-warroom-cli only spawn/status/kill; bus nudge
# in _tmux.py is for mail wakeups, not role priming).
#
# Usage:
#   warroom-prime.sh --warroom NAME --roles-dir DIR --roster FILE
#   warroom-prime.sh --warroom NAME --roles-dir DIR --roster -          # roster on stdin
#   warroom-prime.sh --warroom NAME --roles-dir DIR --status-json FILE  # or - for stdin
#
# --warroom is mandatory. The script never primes "every pane it finds";
# status-json input is filtered to that warroom name only, and any roster
# row may optionally carry a 5th column warroom-name that must match.
#
# Roster lines (whitespace-separated, # comments and blank lines ok):
#   PANE_ID  RUNTIME  ROLE  [skills_csv]  [warroom_name]
#   %90      claude   builder  code-review,code-hygiene  myroom
#   %91      codex    reviewer code-review
#   %92      grok     assistant
#
# Role standby text is data, not code: roles-dir/ROLE.txt is one standby line
# (or a short multi-line block joined to a single send). Optional
# roles-dir/ROLE.skills lists skill names one per line when the roster omits
# the skills column.
#
# Runtime skill send:
#   claude | claude-opus  -> /skill
#   codex                 -> $skill  (typed first; Enter is a separate call)
#   grok | grok-fast      -> one plain-English "Load your X and Y skills now;
#                           review nothing yet." line (no / or $ commands)
# /compact is never rewritten to $compact.
#
# Exit 0 only if every pane PASS; non-zero if any FAIL.
# Do not point this at live mid-task panes unless you intend to re-prime them.
set -euo pipefail

WARROOM=""
ROLES_DIR=""
ROSTER=""
STATUS_JSON=""
DRY_RUN=0
NO_SKILLS=0
SLEEP_AFTER_SEND=1.5
SLEEP_AFTER_ENTER=0.8
CONFIRM_RETRIES=3
CAPTURE_TAIL=40

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --warroom) WARROOM="${2:-}"; shift 2 ;;
    --roles-dir) ROLES_DIR="${2:-}"; shift 2 ;;
    --roster) ROSTER="${2:-}"; shift 2 ;;
    --status-json) STATUS_JSON="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-skills) NO_SKILLS=1; shift ;;
    --sleep-after-send) SLEEP_AFTER_SEND="${2:-}"; shift 2 ;;
    --help|-h) usage ;;
    *) die "unknown arg: $1 (see --help)" ;;
  esac
done

[[ -n "$WARROOM" ]] || die "--warroom NAME is required (refuse to prime without an explicit warroom)"
[[ -n "$ROLES_DIR" ]] || die "--roles-dir DIR is required"
[[ -d "$ROLES_DIR" ]] || die "roles-dir not a directory: $ROLES_DIR"
if [[ -z "$ROSTER" && -z "$STATUS_JSON" ]]; then
  die "provide --roster FILE or --status-json FILE"
fi
if [[ -n "$ROSTER" && -n "$STATUS_JSON" ]]; then
  die "use only one of --roster or --status-json"
fi
command -v tmux >/dev/null || die "tmux not on PATH"
command -v python3 >/dev/null || die "python3 required to parse status JSON / build needles"

# --- helpers ---------------------------------------------------------------

pane_exists() {
  tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx -- "$1"
}

capture_tail() {
  # -S -N pulls scrollback so a just-submitted long line still matches
  # after the shell prints "command not found" or the TUI scrolls.
  tmux capture-pane -t "$1" -p -S -80 2>/dev/null | tail -n "$CAPTURE_TAIL"
}

# Strip all whitespace so pane-width wrap (which inserts newlines mid-word,
# e.g. ASSISTAN\nT) still matches the original standby needle.
_compact() {
  printf '%s' "$1" | tr -d '[:space:]'
}

# True if needle appears in the pane's recent capture (case-sensitive).
pane_has() {
  local pane="$1" needle="$2"
  local text
  text="$(_compact "$(capture_tail "$pane" || true)")"
  needle="$(_compact "$needle")"
  [[ -n "$needle" && "$text" == *"$needle"* ]]
}

# Send text literally, then submit with runtime-aware Enter choreography.
# Codex often needs text typed first and Enter as a separate call; a bare
# re-Enter covers the swallowed-first-Enter class on all runtimes.
send_line() {
  local pane="$1" runtime="$2" text="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  dry-run send-keys -t $pane -l <${#text} chars>  runtime=$runtime"
    return 0
  fi
  # Exit copy/scroll mode if active (same class of failure as bus nudge).
  local mode
  mode="$(tmux display-message -t "$pane" -p '#{pane_in_mode}' 2>/dev/null || echo 0)"
  if [[ "$mode" == "1" ]]; then
    tmux send-keys -t "$pane" -X cancel 2>/dev/null || true
  fi
  tmux send-keys -t "$pane" -l -- "$text"
  sleep "$SLEEP_AFTER_SEND"
  case "$runtime" in
    codex)
      # Separate Enter call; hex CR primes Codex input buffer (see _tmux.nudge).
      tmux send-keys -t "$pane" -H 0d
      sleep 0.15
      tmux send-keys -t "$pane" Enter
      ;;
    *)
      tmux send-keys -t "$pane" Enter
      ;;
  esac
  sleep "$SLEEP_AFTER_ENTER"
}

# After a send, confirm needle; if missing, bare Enter and re-check.
confirm_line() {
  local pane="$1" runtime="$2" needle="$3" label="$4"
  local i
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  dry-run confirm $label needle=${needle:0:48}..."
    return 0
  fi
  for ((i = 1; i <= CONFIRM_RETRIES; i++)); do
    if pane_has "$pane" "$needle"; then
      return 0
    fi
    # Swallowed Enter: text still at prompt or never submitted.
    tmux send-keys -t "$pane" Enter
    sleep "$SLEEP_AFTER_ENTER"
  done
  return 1
}

skill_token() {
  local runtime="$1" skill="$2"
  case "$runtime" in
    claude|claude-opus) printf '/%s' "$skill" ;;
    codex) printf '$%s' "$skill" ;;
    grok|grok-fast) printf '%s' "$skill" ;; # plain name for the load line
    *) die "unknown runtime for skill token: $runtime" ;;
  esac
}

send_skills() {
  local pane="$1" runtime="$2" skills_csv="$3"
  [[ "$NO_SKILLS" -eq 1 ]] && return 0
  [[ -z "$skills_csv" ]] && return 0

  local -a skills=()
  IFS=',' read -r -a skills <<<"$skills_csv"
  # trim empties
  local -a cleaned=()
  local s
  for s in "${skills[@]}"; do
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    [[ -n "$s" ]] && cleaned+=("$s")
  done
  [[ ${#cleaned[@]} -eq 0 ]] && return 0

  case "$runtime" in
    grok|grok-fast)
      local joined="" name
      for name in "${cleaned[@]}"; do
        if [[ -z "$joined" ]]; then
          joined="$name"
        else
          joined="$joined and $name"
        fi
      done
      local load="Load your ${joined} skills now; review nothing yet."
      send_line "$pane" "$runtime" "$load"
      # Confirm ◆ Skill markers when present; fall back to the load line itself.
      local ok=0
      for name in "${cleaned[@]}"; do
        if confirm_line "$pane" "$runtime" "Skill ${name}" "skill:$name" \
          || confirm_line "$pane" "$runtime" "◆ Skill ${name}" "skill:$name"; then
          ok=1
        fi
      done
      if [[ "$ok" -eq 0 ]]; then
        # Marker may lag; require the load instruction at least.
        confirm_line "$pane" "$runtime" "Load your ${joined} skills" "grok-load" || return 1
      fi
      return 0
      ;;
    *)
      local tok
      for name in "${cleaned[@]}"; do
        tok="$(skill_token "$runtime" "$name")"
        send_line "$pane" "$runtime" "$tok"
        confirm_line "$pane" "$runtime" "$tok" "skill:$name" || return 1
      done
      return 0
      ;;
  esac
}

read_role_text() {
  local role="$1"
  local f="$ROLES_DIR/${role}.txt"
  [[ -f "$f" ]] || die "missing role standby file: $f"
  # Collapse to a single line for send-keys (newlines -> spaces).
  tr '\n' ' ' <"$f" | sed 's/[[:space:]]\{1,\}/ /g; s/^ //; s/ $//'
}

read_role_skills() {
  local role="$1"
  local f="$ROLES_DIR/${role}.skills"
  if [[ -f "$f" ]]; then
    # one skill per line -> csv
    grep -v '^[[:space:]]*#' "$f" | grep -v '^[[:space:]]*$' | paste -sd, -
  fi
}

# Needle for standby confirmation: leading distinctive fragment (role lines
# start with STANDBY / role name). Flattened match ignores terminal wrap.
standby_needle() {
  local text="$1"
  python3 -c '
import sys
t = " ".join(sys.argv[1].split())
print(t[:56] if len(t) > 56 else t)
' "$text"
}

# --- load roster rows into ROWS as "pane|runtime|role|skills" --------------

ROWS=()

load_roster_file() {
  local src="$1"
  local line pane runtime role skills wr
  local stream
  if [[ "$src" == "-" ]]; then
    stream="/dev/stdin"
  else
    [[ -f "$src" ]] || die "roster not found: $src"
    stream="$src"
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    # shellcheck disable=SC2086
    set -- $line
    pane="${1:-}"; runtime="${2:-}"; role="${3:-}"; skills="${4:-}"; wr="${5:-}"
    [[ -n "$pane" && -n "$runtime" && -n "$role" ]] || die "bad roster line: $line"
    if [[ -n "$wr" && "$wr" != "$WARROOM" ]]; then
      die "roster row warroom '$wr' != --warroom '$WARROOM' (pane $pane)"
    fi
    if [[ -z "$skills" ]]; then
      skills="$(read_role_skills "$role" || true)"
    fi
    ROWS+=("${pane}|${runtime}|${role}|${skills}")
  done <"$stream"
}

load_status_json() {
  local src="$1"
  local raw
  if [[ "$src" == "-" ]]; then
    raw="$(cat)"
  else
    [[ -f "$src" ]] || die "status-json not found: $src"
    raw="$(cat "$src")"
  fi
  # Filter to --warroom only. Accept either a warroom_status list or a flat
  # members array that already carries warroom_id / warroom / name.
  local parsed
  parsed="$(WARROOM_NAME="$WARROOM" python3 -c '
import json, os, sys
name = os.environ["WARROOM_NAME"]
data = json.load(sys.stdin)

def members_from(obj):
    out = []
    if isinstance(obj, dict):
        wid = obj.get("warroom_id") or obj.get("name") or obj.get("warroom") or ""
        for m in obj.get("members") or []:
            mm = dict(m)
            mm["_warroom"] = wid
            out.append(mm)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "members" in obj[0]:
            for wr in obj:
                out.extend(members_from(wr))
        else:
            for m in obj:
                mm = dict(m)
                mm["_warroom"] = m.get("warroom_id") or m.get("warroom") or m.get("name") or ""
                out.append(mm)
    return out

rows = []
for m in members_from(data):
    wid = m.get("_warroom") or ""
    if wid != name:
        continue
    pane = m.get("pane_id") or ""
    runtime = m.get("runtime") or m.get("desired_runtime") or ""
    role = m.get("role") or m.get("prime_role") or m.get("desired_role") or ""
    skills = m.get("skills") or ""
    if isinstance(skills, list):
        skills = ",".join(skills)
    if not pane or not runtime or not role:
        print(f"skip incomplete member: pane={pane!r} runtime={runtime!r} role={role!r}", file=sys.stderr)
        continue
    # warroom_status desired_role is often the agent type (e.g. general), not
    # the slice role; still emit so the caller can use role files named that way,
    # or put explicit role/prime_role/skills on a hand-built JSON roster.
    print(f"{pane}|{runtime}|{role}|{skills}")
' <<<"$raw")" || die "failed to parse status-json for warroom=$WARROOM"

  if [[ -z "$parsed" ]]; then
    die "no members for --warroom '$WARROOM' in status-json (refusing to act on other warrooms)"
  fi
  local row pane runtime role skills
  while IFS= read -r row || [[ -n "$row" ]]; do
    [[ -z "$row" ]] && continue
    ROWS+=("$row")
  done <<<"$parsed"
}

if [[ -n "$ROSTER" ]]; then
  load_roster_file "$ROSTER"
else
  load_status_json "$STATUS_JSON"
fi

[[ ${#ROWS[@]} -gt 0 ]] || die "roster empty after load"

# --- prime each pane -------------------------------------------------------

pass_n=0
fail_n=0
results=()

for row in "${ROWS[@]}"; do
  IFS='|' read -r pane runtime role skills <<<"$row"
  echo "== $pane  runtime=$runtime  role=$role  warroom=$WARROOM"

  if ! pane_exists "$pane"; then
    echo "FAIL $pane  cause=pane-missing"
    results+=("FAIL $pane pane-missing")
    fail_n=$((fail_n + 1))
    continue
  fi

  if [[ ! -f "$ROLES_DIR/${role}.txt" ]]; then
    echo "FAIL $pane  cause=missing-role-file $ROLES_DIR/${role}.txt"
    results+=("FAIL $pane missing-role-file")
    fail_n=$((fail_n + 1))
    continue
  fi

  standby="$(read_role_text "$role")"
  [[ -n "$standby" ]] || {
    echo "FAIL $pane  cause=empty-standby"
    results+=("FAIL $pane empty-standby")
    fail_n=$((fail_n + 1))
    continue
  }
  needle="$(standby_needle "$standby")"

  if ! send_line "$pane" "$runtime" "$standby"; then
    echo "FAIL $pane  cause=send-standby"
    results+=("FAIL $pane send-standby")
    fail_n=$((fail_n + 1))
    continue
  fi
  if ! confirm_line "$pane" "$runtime" "$needle" "standby"; then
    echo "FAIL $pane  cause=standby-not-confirmed"
    results+=("FAIL $pane standby-not-confirmed")
    fail_n=$((fail_n + 1))
    continue
  fi

  if ! send_skills "$pane" "$runtime" "$skills"; then
    echo "FAIL $pane  cause=skills-not-confirmed"
    results+=("FAIL $pane skills-not-confirmed")
    fail_n=$((fail_n + 1))
    continue
  fi

  echo "PASS $pane  role=$role runtime=$runtime"
  results+=("PASS $pane")
  pass_n=$((pass_n + 1))
done

echo "----"
echo "warroom=$WARROOM  pass=$pass_n  fail=$fail_n  total=${#ROWS[@]}"
for r in "${results[@]}"; do
  echo "$r"
done

[[ "$fail_n" -eq 0 ]]
