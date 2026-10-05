#!/usr/bin/env bash
# Run the loop on the HACC build host inside tmux, so it keeps going when the
# ssh session, the laptop or the VPN goes away.
#
#   agentic/hacc_tmux.sh --resume --run hacc-real200 --model opus
#
# Detach with Ctrl-b d; come back with `tmux attach -t agentic`. Everything
# after the script name is passed to agentic/loop.py unchanged.
#
# Window "loop" runs agentic/loop.py at nice 10 with the host's settings:
#   AGENTIC_HACC_HOST=local     Vivado runs here, not over ssh (hacc.py)
#   AGENTIC_EDA_SUITE           the OSS CAD Suite in /local/home/$USER/eda
#   AGENTIC_SIM_SLOTS=22        every draw of two candidates at once (64
#                               cores, ~350 GB; the laptop had 8 slots)
#   CLAUDE_CODE_OAUTH_TOKEN     read from /local/home/$USER/.claude-oauth-token
#                               (make one with `claude setup-token`)
# Window "backup" copies the run folders and every branch to the NFS home
# every hour, because /local/home is not backed up.
#
# A reboot of the host ends the tmux session; start it again with --resume.

set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
LOCAL=${AGENTIC_LOCAL:-/local/home/$USER}
SESSION=${AGENTIC_TMUX_SESSION:-agentic}
BACKUP=${AGENTIC_BACKUP:-$HOME/agentic-backup}
SLOTS=${AGENTIC_SIM_SLOTS:-22}

if [ $# -eq 0 ]; then
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
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
if [ ! -s "$LOCAL/.claude-oauth-token" ] && [ ! -s "$HOME/.claude/.credentials.json" ]; then
  echo "no Claude login on this host: run 'claude setup-token', then save the"
  echo "token with: umask 077; cat > $LOCAL/.claude-oauth-token"
  exit 1
fi

# The windows source these instead of inheriting them, so a tmux server that
# was started earlier, with another environment, cannot change what runs.
ENVFILE="$LOCAL/.agentic-env-$SESSION.sh"
cat > "$ENVFILE" <<EOF
. "$LOCAL/venv/bin/activate"
export AGENTIC_HACC_HOST=local
export AGENTIC_EDA_SUITE="$LOCAL/eda/oss-cad-suite"
export AGENTIC_SIM_SLOTS=$SLOTS
unset ANTHROPIC_API_KEY
if [ -s "$LOCAL/.claude-oauth-token" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="\$(cat "$LOCAL/.claude-oauth-token")"
fi
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
mkdir -p "$BACKUP/runs"
while true; do
  # -rlt, not -a: the NFS home refuses the host's group (chgrp: Invalid
  # argument), and that failure used to skip the branches as well.
  rsync -rlt "$REPO/.agentic/runs/" "$BACKUP/runs/"; r=\$?
  git -C "$REPO" bundle create "$BACKUP/agenticRTL.bundle.tmp" --all 2>/dev/null \\
    && mv "$BACKUP/agenticRTL.bundle.tmp" "$BACKUP/agenticRTL.bundle"; g=\$?
  echo "\$(date '+%F %T') runs: rsync rc \$r (24 = files vanished mid-copy, harmless);" \\
       "branches: \$([ \$g -eq 0 ] && echo ok || echo FAILED)"
  sleep 3600
done
EOF

tmux new-session -d -s "$SESSION" -n loop "bash '$RUNFILE'"
tmux new-window -d -t "$SESSION" -n backup "bash '$BACKFILE'"

echo "started tmux session '$SESSION' (windows: loop, backup), $SLOTS simulator slots"
echo "  watch it:   tmux attach -t $SESSION        (detach again: Ctrl-b d)"
echo "  the log:    $REPO/.agentic/runs/<run>/loop.log"
echo "  dashboard:  on the laptop, python agentic/mirror.py --run <run> --gui"
