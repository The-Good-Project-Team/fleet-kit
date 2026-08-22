# fleet-kit runtime image.
#
# WHY THIS EXISTS: the first real deployment (2026-08-20, box "dino") was provisioned by hand
# over ssh/multipass -- packages installed ad hoc, credentials copied in one at a time, cron
# state accumulated from THREE prior unrelated projects on the same VM (an old "lucky" API
# daemon's env file, a retired magikarp install, stray ssh configs pointing at a since-renamed
# host). None of it was reproducible; rebuilding meant redoing every step from a chat transcript.
# This image is the fix: everything fleet-kit needs to run baked in, nothing box-specific baked
# in. A dead box becomes `docker run` on a new one, not an archaeology session.
#
# What stays OUTSIDE the image (by design, never bake these in):
#   - Claude credentials         -> mount at /root/.claude-<account>/  (one dir per FLEET_ACCOUNTS entry)
#   - GitHub token                -> GH_TOKEN env var at `docker run` time (gh CLI reads it directly)
#   - fleet.env                   -> mount at /fleet-kit/fleet.env
#   - the target repo             -> either clone at container start (entrypoint does this if
#                                    FLEET_REPO doesn't exist) or bind-mount a host clone
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# git: worktree builder. python3: board_github.py/run_report.py/maxx_reader.py. curl+ca-certs: gh
# CLI install + claude CLI install. cron: schedule cadences inside the container without a
# host-level launchd/systemd dependency (schedulers/ templates remain for host-native installs).
RUN apt-get update -qq && apt-get install -y -qq \
      git python3 python3-pip curl ca-certificates cron gnupg jq \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — official apt repo, not a hand-rolled binary fetch (fewer moving parts to break
# on an arch/OS change).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update -qq && apt-get install -y -qq gh && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. The installer writes to $HOME/.local/bin under whatever HOME it sees at
# build time (root here) -- confirmed on the live box this lands at /root/.local/bin/claude.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /fleet-kit
COPY . /fleet-kit

RUN chmod +x /fleet-kit/scripts/*.sh /fleet-kit/entrypoint.sh

# fleet_view_server.py (started by entrypoint.sh's cron-foreground mode) -- documents the
# port for anyone inspecting the image; actual publishing still needs `-p` at `podman run`.
EXPOSE 8420

ENTRYPOINT ["/fleet-kit/entrypoint.sh"]
CMD ["cron-foreground"]
