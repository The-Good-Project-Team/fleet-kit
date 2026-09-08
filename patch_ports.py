#!/usr/bin/env python3
"""Stage 'test': add the selftest. Stage 'fix': patch entrypoint.sh. Run in the worktree root."""
import pathlib, sys

root = pathlib.Path(sys.argv[1]); stage = sys.argv[2]

if stage == "test":
    p = root / "scripts/selftest.py"
    s = p.read_text()
    anchor = "def _entrypoint_crontab_forwards_fleet_share_dir():"
    assert anchor in s
    fn = '''def _entrypoint_container_port_env_wins_over_fleet_env():
    """Rolling deploys (gh#625) hand each container its own port pair with `-e FLEET_VIEW_PORT`
    / `-e FLEET_WEBHOOK_PORT`. PR#708 made entrypoint.sh `set -a; . fleet.env` at boot, and
    fleet.env carries the instance's default FLEET_VIEW_PORT -- so a green candidate on the
    other pair bound fleet.env's port inside its own namespace and never answered its health
    check (2026-09-08 07:27 CDT: "fleet_view_server started on :8420" while mapped to 8591,
    FAILED twice, every deploy to the B pair dead). Run entrypoint's own sourcing block and
    assert the container's env wins for the ports while fleet.env still fills the rest.
    """
    import subprocess, tempfile
    entry = (Path(__file__).parent.parent / "entrypoint.sh").read_text()
    begin, end = "# fleet-env-source-begin", "# fleet-env-source-end"
    if begin in entry and end in entry:
        block = entry[entry.index(begin):entry.index(end)]
    else:  # pre-fix shape: the bare early sourcing line at column 0
        block = next(l for l in entry.splitlines()
                     if l.startswith("[ -f") and 'set -a; . "${FLEET_ENV_FILE' in l)
    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / "fleet.env"
        env_file.write_text("FLEET_VIEW_PORT=8420\\nFLEET_WEBHOOK_PORT=8562\\nFLEET_ACCOUNTS=gmail tgp\\n")
        script = block + '\\necho "$FLEET_VIEW_PORT $FLEET_WEBHOOK_PORT $FLEET_ACCOUNTS"\\n'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10,
                              env={"PATH": "/usr/bin:/bin", "FLEET_ENV_FILE": str(env_file),
                                   "FLEET_VIEW_PORT": "8591", "FLEET_WEBHOOK_PORT": "8592"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "8591 8592 gmail tgp", (
        "entrypoint.sh let fleet.env override the ports deploy.sh handed the container: "
        f"got {proc.stdout.strip()!r}, want '8591 8592 gmail tgp'")


'''
    s = s.replace(anchor, fn + anchor)
    reg = '''    check("entrypoint.sh's crontab-wide env block forwards FLEET_SHARE_DIR (gh#569)", _entrypoint_crontab_forwards_fleet_share_dir)'''
    assert reg in s
    s = s.replace(reg, reg + '''\n    check("entrypoint.sh: the container's port env wins over fleet.env (rolling deploy pairs, gh#625/#708)", _entrypoint_container_port_env_wins_over_fleet_env)''')
    p.write_text(s); print("test added")

elif stage == "fix":
    p = root / "entrypoint.sh"
    s = p.read_text()
    old = '\n[ -f "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}"; set +a; }\n'
    assert s.count(old) == 1, "entrypoint anchor"
    new = '''
# fleet-env-source-begin
# fleet.env fills in what the container env left unset -- but the port pair deploy.sh hands
# THIS container (`-e FLEET_VIEW_PORT` / `-e FLEET_WEBHOOK_PORT`, gh#625 rolling deploys
# alternate two pairs) must win over fleet.env's instance default, or a green candidate on
# the other pair binds the live pair's port inside its own namespace and never answers its
# health check (2026-09-08: every deploy to the B pair FAILED). selftest runs this block.
_fk_view="${FLEET_VIEW_PORT-}"; _fk_webhook="${FLEET_WEBHOOK_PORT-}"
[ -f "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-/fleet-kit/fleet.env}"; set +a; }
[ -n "$_fk_view" ] && export FLEET_VIEW_PORT="$_fk_view"
[ -n "$_fk_webhook" ] && export FLEET_WEBHOOK_PORT="$_fk_webhook"
unset _fk_view _fk_webhook
# fleet-env-source-end
'''
    p.write_text(s.replace(old, new)); print("entrypoint fixed")
