#!/usr/bin/env bash
# Run the loop on the HACC build host inside tmux, so it keeps going when the
# ssh session, the laptop or the VPN goes away.
#
#   agentic/hacc_tmux.sh --resume --run <run> --model opus
#
# Detach with Ctrl-b d; come back with `tmux attach -t agentic`. Everything
# after the script name is passed to agentic/loop.py unchanged. To stop the
# loop, press Ctrl-C in its window: that marks the run as stopped on purpose.
#
# Window "loop" runs agentic/loop.py at nice 10 with the host's settings:
#   AGENTIC_HACC_HOST=local     Vivado runs here, not over ssh (hacc.py)
#   AGENTIC_EDA_SUITE           the OSS CAD Suite in /local/home/$USER/eda
#   AGENTIC_SIM_SLOTS=22        every draw of two candidates at once (64
#                               cores, ~350 GB; the laptop had 8 slots)
#   CLAUDE_CODE_OAUTH_TOKEN     read from /local/home/$USER/.claude-oauth-token
#                               (make one with `claude setup-token`)
#   HOME=/local/home/$USER      and the XDG_*_HOME folders under it, not the
#                               NFS home, which is Kerberos-secured and
#                               unreadable without a ticket (after a reboot,
#                               or once the last password login's ticket
#                               expires); Claude Code, git and Vivado keep
#                               files there
# Window "backup" copies the run folders and every branch to the NFS home
# every hour, because /local/home is not backed up.
#
# A reboot of the host ends the tmux session. Every start records its
# arguments, and `hacc_tmux.sh --reboot` starts again with them unless that
# run was stopped on purpose, finished or crashed. The crontab calls it at
# boot, with HOME set so cron does not start in the NFS home:
#   HOME=/local/home/vdixit
#   @reboot /local/home/vdixit/agenticRTL/agentic/hacc_tmux.sh --reboot >> /local/home/vdixit/agentic-reboot.log 2>&1

set -e

ME=${USER:-$(id -un)}          # cron does not always set USER
REPO=$(cd "$(dirname "$0")/.." && pwd)
LOCAL=${AGENTIC_LOCAL:-/local/home/$ME}
SESSION=${AGENTIC_TMUX_SESSION:-agentic}
REALHOME=$(getent passwd "$ME" | cut -d: -f6)
BACKUP=${AGENTIC_BACKUP:-${REALHOME:-$HOME}/agentic-backup}
SLOTS=${AGENTIC_SIM_SLOTS:-22}
# The loop's HOME (Claude Code's transcripts and config live there): its own
# folder per run that must not share them with another.
HOMEDIR=${AGENTIC_HOME:-$LOCAL}
ARGSFILE="$LOCAL/.agentic-args-$SESSION"

if [ "$1" = "--reboot" ]; then
  echo "$(date '+%F %T') host booted; was a run cut off?"
  if [ ! -s "$ARGSFILE" ]; then
    echo "  no start recorded in $ARGSFILE; nothing to do"
    exit 0
  fi
  eval "set -- $(cat "$ARGSFILE")"
  RUN=
  prev=
  for a in "$@"; do
    if [ "$prev" = "--run" ]; then RUN=$a; fi
    prev=$a
  done
  # A run the loop itself ended (Ctrl-C, the iteration cap, a crash) says
  # "finished" or "failed"; one the reboot cut off is still mid-phase.
  PHASE=$(sed -n 's/^ *"phase": *"\([^"]*\)".*/\1/p' \
          "$REPO/.agentic/runs/$RUN/status.json" 2>/dev/null | head -1)
  case "$PHASE" in
    finished|failed|'')
      echo "  run '$RUN' is ${PHASE:-unknown}: ended on purpose or by itself; not restarting"
      exit 0 ;;
  esac
  DELAY=${AGENTIC_REBOOT_DELAY:-120}
  echo "  run '$RUN' was in phase '$PHASE'; resuming in $DELAY s: loop.py $*"
  sleep "$DELAY"                 # let the network come up first
  # cron points these at the NFS home, where tmux and git would find their
  # config unreadable until someone logs in.
  export HOME="$HOMEDIR" XDG_CONFIG_HOME="$HOMEDIR/.config"
  export TERM=${TERM:-xterm-256color}
fi

if [ $# -eq 0 ]; then
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already exists: tmux attach -t $SESSION"
  exit 1
fi
for need in "$LOCAL/venv/bin/activate" "$LOCAL/eda/oss-cad-suite/bin/ghdl"; do
  if [ ! -e "$need" ]; then
    echo "missing $need (see 'Running on the HACC host' in agentic/README.md)"
    exit 1
  fi
done
if [ ! -s "$LOCAL/.claude-oauth-token" ]; then
  echo "no Claude login on this host: run 'claude setup-token', then save the"
  echo "token with: umask 077; cat > $LOCAL/.claude-oauth-token"
  exit 1
fi

# The windows source these instead of inheriting them, so a tmux server that
# was started earlier, with another environment, cannot change what runs.
ENVFILE="$LOCAL/.agentic-env-$SESSION.sh"
cat > "$ENVFILE" <<EOF
. "$LOCAL/venv/bin/activate"
mkdir -p "$HOMEDIR"
export HOME="$HOMEDIR"
export XDG_CONFIG_HOME="$HOMEDIR/.config" XDG_CACHE_HOME="$HOMEDIR/.cache"
export XDG_DATA_HOME="$HOMEDIR/.local/share" XDG_STATE_HOME="$HOMEDIR/.local/state"
export AGENTIC_HACC_HOST=local
export AGENTIC_EDA_SUITE="$LOCAL/eda/oss-cad-suite"
export AGENTIC_SIM_SLOTS=$SLOTS
unset ANTHROPIC_API_KEY
export CLAUDE_CODE_OAUTH_TOKEN="\$(cat "$LOCAL/.claude-oauth-token")"
cd "$REPO"
EOF

RUNFILE="$LOCAL/.agentic-run-$SESSION.sh"
{
  echo ". \"$ENVFILE\""
  printf 'nice -n 10 python agentic/loop.py'
  printf ' %q' "$@"
  echo
  echo 'rc=$?; echo; echo "loop.py exited (rc $rc) at $(date); this window stays open"'
  echo 'exec bash'
} > "$RUNFILE"

BACKFILE="$LOCAL/.agentic-backup-$SESSION.sh"
cat > "$BACKFILE" <<EOF
while true; do
  # -rlt, not -a: the NFS home refuses the host's group (chgrp: Invalid
  # argument), and that failure used to skip the branches as well. Without
  # a Kerberos ticket (after a reboot, until a password login) both fail.
  mkdir -p "$BACKUP/runs" && rsync -rlt "$REPO/.agentic/runs/" "$BACKUP/runs/"; r=\$?
  git -C "$REPO" bundle create "$BACKUP/agenticRTL.bundle.tmp" --all 2>/dev/null \\
    && mv "$BACKUP/agenticRTL.bundle.tmp" "$BACKUP/agenticRTL.bundle"; g=\$?
  echo "\$(date '+%F %T') runs: rsync rc \$r (24 = files vanished mid-copy, harmless);" \\
       "branches: \$([ \$g -eq 0 ] && echo ok || echo FAILED)"
  sleep 3600
done
EOF

printf ' %q' "$@" > "$ARGSFILE"
tmux new-session -d -s "$SESSION" -n loop "bash '$RUNFILE'"
tmux new-window -d -t "$SESSION" -n backup "bash '$BACKFILE'"

echo "started tmux session '$SESSION' (windows: loop, backup), $SLOTS simulator slots"
echo "  watch it:   tmux attach -t $SESSION        (detach again: Ctrl-b d)"
echo "  the log:    $REPO/.agentic/runs/<run>/loop.log"
echo "  dashboard:  on the laptop, python agentic/mirror.py --run <run> --gui"
