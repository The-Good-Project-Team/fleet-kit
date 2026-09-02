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

# python3.11-minimal/libpython3.11-stdlib: the TARGET repo's own .venv (bind-mounted at /repo,
# built by uv) symlinks its interpreter at /usr/bin/python3.11. This image ships 22.04's default
# python3 (3.10) only, so that symlink dangled and EVERY in-container attempt to run the app's
# test suite died with "No such file or directory" -- which is why the fleet believed for
# generations that philanthropy had "no importable app runtime on this box". It always had one;
# the interpreter the venv pointed at was simply never installed. Found 2026-09-02 by the minion
# on item #3940, which apt-installed it by hand inside the running container -- a fix a rebuild
# would have silently erased. The venv's site-packages were fine all along (fastapi 0.141.1,
# jinja2 3.1.6, pytest 9.0.3 all import the moment the interpreter exists).
# git: worktree builder. python3: board_github.py/run_report.py/maxx_reader.py. curl+ca-certs: gh
# CLI install + claude CLI install. cron: schedule cadences inside the container without a
# host-level launchd/systemd dependency (schedulers/ templates remain for host-native installs).
# sqlite3: the CLI, so a charter can query fleet.db the obvious way. Added 2026-08-26 after
# BOTH gru's and nerd's charters shipped `sqlite3 "$FLEET_LOG_DIR/fleet.db" ...` commands that
# died on `sh: sqlite3: not found` -- gru's cost-calibration step was reading NO data and
# nothing said so, because a shell command that fails still lets the pass continue.
RUN apt-get update -qq && apt-get install -y -qq \
      git python3 python3-pip curl ca-certificates cron gnupg jq sqlite3 \
      python3.11-minimal libpython3.11-stdlib \
    && rm -rf /var/lib/apt/lists/*

# Python libs the ANALYSIS lanes need (datta/nerd). The container shipped with NO third-party
# python at all -- not even requests -- so a nerd told to read GSC or GA4 could not, and would
# have filed "no credential" forever even once the credentials landed.
#   requests      : ordinary HTTP with a real timeout story; urllib works but every caller
#                   re-implements retries/headers badly.
#   google-auth   : service-account signing for GSC + GA4. Deliberately NOT
#                   google-api-python-client: the target repo's own gsc_pages.py proves the
#                   Search Console REST API needs only google-auth + urllib, and the heavier
#                   client pulls a large dependency tree for no gain here.
# No --break-system-packages: this base ships pip 22.0.2, which predates that flag and exits
# "no such option", failing the build. Verified against the real image 2026-08-26 rather than
# assumed -- the flag is correct on newer bases and would have looked right in review.
RUN pip3 install --no-cache-dir requests google-auth playwright

# A real browser, because some numbers exist ONLY in a rendered page. Google's Page Indexing
# report -- the one holding "3.7M discovered, currently not indexed" and "1.68M excluded by
# noindex" -- has NO API at all (its sitemaps.list `indexed` field has reported ~0% since it
# was deprecated in 2019), so the ONLY way to read those buckets and their example URLs is to
# open the page. Same for judging a UI surface as a human sees it rather than as a template.
#
# playwright's OWN chromium, not apt's: Ubuntu 22.04 ships `chromium-browser` as a snap stub
# that cannot run in a container, and `chromium` has no candidate at all (verified on the real
# image). --with-deps pulls the shared libraries headless chromium needs; without it the
# binary installs fine and then fails at launch, which is the worst shape to debug.
#
# Costs ~115MB for the headless shell plus its system libs. Deliberate: the alternative is a
# lane that files "cannot read, no browser" every pass forever.
RUN playwright install --with-deps chromium

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

# gh#201: the commit this image was built from, so a running container can answer "what SHA is
# actually live" without /fleet-kit being a git checkout (it isn't -- .dockerignore excludes
# .git/ above, deliberately, per this Dockerfile's own history). deploy.sh passes this at build
# time from the checkout it built FROM; scripts/deploy_staleness_check.sh reads it back to tell
# whether the live tree has drifted from main.
ARG DEPLOY_SHA=unknown
RUN echo "$DEPLOY_SHA" > /fleet-kit/.deploy_sha

# fleet_view_server.py (started by entrypoint.sh's cron-foreground mode) -- documents the
# port for anyone inspecting the image; actual publishing still needs `-p` at `podman run`.
EXPOSE 8420

ENTRYPOINT ["/fleet-kit/entrypoint.sh"]
CMD ["cron-foreground"]
