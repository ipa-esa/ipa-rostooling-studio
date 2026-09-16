# CoreSense Studio — the local web app

A small, local, token-protected web service (`scripts/studio_server.py`) plus a browser front end
(`web/studio/`). You run it on your own machine, it serves one browser tab, and it keeps every
project as plain files in a directory you choose.

**This is not the same thing as `ros_studio.py`.** The repository ships two separate things that
both happen to be called "studio":

| | What it is | Entry point |
|---|---|---|
| **`ros_studio.py`** | A deterministic CLI, no UI: seed a `project.json`, generate `.ros2`/`.rossystem`/`.ros`, validate. | `scripts/ros_studio.py` — see the main [`README.md`](../README.md) |
| **CoreSense Studio** (this document) | A **local HTTP service** with a real project store on disk, multi-file projects, linked ROS source repositories, and a React front end. | `scripts/studio_server.py` |

`ros_studio.py` is imported *by* the web app as its model-generation and validation engine, so the
two share one emitter and one linter — CoreSense Studio is the interface for authoring and
checking a model; `ros_studio.py` is not meant to be used standalone for that.

## What it does, and what it deliberately does not

CoreSense Studio covers four things:

- **Project brief** — the system's purpose, acceptance criteria, behaviours and recovery actions,
  written as prose and kept next to the model.
- **Architecture** — the model graph: components, their interfaces, and the connections between
  them. This is the centre of the app.
- **Source repositories** — link a real ROS 2 repository (fetch a pinned commit by Git URL, or
  point at an existing checkout on disk), statically extract its nodes and interfaces, review what
  was found, and publish the result into a reusable catalogue you can place into your system. It
  also reads `*.repos` dependency manifests and Git submodules inside a linked repository, so the
  dependencies a repo declares for itself can be pulled too.
- **Checking a model** — the generation gate, the `rosmodel_lint` linter, and (when a suitable Java
  runtime is present) the real RosTooling language-server oracle.

It does **not** do deployment, containerization, live ROS telemetry, robot-asset simulation, or a
review/release workflow. Those used to live here and were split out into a separate, confidential
tool. If a menu entry, an old screenshot or an old tutorial suggests otherwise, that is a leftover,
not a feature that is merely switched off.

## Starting it

```bash
python scripts/studio_server.py --storage-root ~/ros-studio --port 0
```

It prints exactly one line, and that line is the only way in:

```
CoreSense Studio: http://127.0.0.1:41287/#token=8Qw2…
```

Open that URL. The fragment carries the session token; the front end reads it once and caches it in
`sessionStorage`, then sends it as an `X-Studio-Token` header on every API call. A request without
the header gets `401`.

| Flag | Default | Notes |
|---|---|---|
| `--port` | `8765` | `--port 0` asks the OS for a free port; the printed URL has the real one. |
| `--storage-root` | `~/ros-studio` | Where projects, cloned repositories and job output live. Point it somewhere with real disk space. |
| `--rotate-token` | off | Invalidate the previous session URL and mint a new one. |

The token is **persisted** at `<storage-root>/.studio-token` (mode `0600`) and reused across
restarts on purpose, so a bookmarked URL keeps working. Without that, every restart silently broke
every saved link. If the file cannot be written the run still works, it just uses an ephemeral
token for that process.

Two things the server refuses, by design:

- A `Host` header that is not `127.0.0.1:<port>` or `localhost:<port>` → `403`.
- An `Origin` header from anywhere else → `403`.

It binds `127.0.0.1` only. It is not meant to be reachable from another machine, and putting it
behind a reverse proxy will trip the `Host` check.

### Prerequisites

| Requirement | Needed for | If missing |
|---|---|---|
| Python 3.8+ | everything | won't start |
| PyYAML | the repositories feature (`repositories.repos` is a vcstool manifest) | **the server will not start** — verified: it dies with `ImportError: No module named 'yaml'` from `studio_repositories.py`, before it serves anything |
| `git` on `PATH` | fetching / inspecting linked repositories, submodules | those actions report "Git is unavailable" |
| A Java runtime | the optional language-server oracle inside a model check | the check still runs; the oracle row says `not-run` and names the reason |
| `codex`, `claude`, `gemini` or `agy` (Antigravity CLI) on `PATH` | **the coding-agent features** — see below | `503`, "… is not installed or not on PATH." |

### The coding-agent features need a CLI actually installed

This is the prerequisite that catches people out, so it gets its own heading.

Two features shell out to a coding-agent CLI on your machine. There is no API key, no SDK and no
network call from the server itself — it runs the binary that is already on your `PATH`:

- **"Resolve with coding agent"**, on the Repositories tab. When static extraction cannot resolve
  an expression in a launch file (a computed topic name, a parameter read from elsewhere), it
  flags it. This button runs an agent **headlessly** over the repository's source to trace what
  each flagged expression actually resolves to, and reports its findings for you to accept by
  hand. It never edits your source or your project.
- **The "✧ Coding agent" handoff drawer**, which prepares a task briefing and opens an
  **interactive** terminal session for you to drive.

Supported for the **interactive** handoff drawer — all four:

| Provider | Binary | Interactive invocation |
|---|---|---|
| Codex | `codex` | `codex "<prompt>"` |
| Claude Code | `claude` | `claude "<prompt>"` |
| Gemini CLI | `gemini` | `gemini "<prompt>"` |
| Antigravity CLI | `agy` | `agy -i "<prompt>"` (seeded-interactive mode) |

Supported for **"Resolve with coding agent"** (headless, background job, output parsed) — only
three:

| Provider | Binary | Headless invocation |
|---|---|---|
| Codex | `codex` | `codex exec "<prompt>"` |
| Claude Code | `claude` | `claude -p "<prompt>"` |
| Gemini CLI | `gemini` | `gemini -p "<prompt>"` |

**Antigravity CLI is deliberately excluded from the headless feature.** Two things were checked
against a real installed `agy` (1.2.4) before deciding this, not assumed:

- A publicly reported bug where `agy -p`/`--print` silently drops stdout when it isn't attached
  to a real terminal
  ([google-antigravity/antigravity-cli#76](https://github.com/google-antigravity/antigravity-cli/issues/76))
  did **not** reproduce — tested against the exact production invocation (`subprocess.Popen`,
  stdout redirected to a file, its own process group), including a full run of the
  marker-delimited-JSON protocol this feature actually uses. Output came through intact both
  times.
- What actually blocks it: headless `agy -p` auto-**denies** any tool call it cannot prompt for
  approval on — including a plain file read — unless the run passes a permission-widening flag.
  Confirmed live: asking it to read one file headlessly produced "no output produced — a tool
  required the 'read_file' permission that headless mode cannot prompt for, so it was
  auto-denied". Two ways around that were tried, not just assumed:
  - `--dangerously-skip-permissions` (its own name for what it is) — read the file and answered
    correctly, but also auto-approves everything else, including writes and shell commands
    anywhere on the machine.
  - `--add-dir <sourceRoot>`, hoping for something narrower — it did let the read through, but
    it also silently approved a **write** inside that same directory in a follow-up test (a
    file the agent was asked to create there was actually created, no error, no denial). That
    directly breaks this feature's own guarantee that it never edits your source or your
    project, so it's not the narrow fix it looked like.

  Nothing found grants headless, read-only-only access scoped to one directory without either
  widening to real write/exec access too, or requiring a standing edit to `agy`'s own
  `settings.json` on the machine (its error message mentions a `permissions.allow` rule) — which
  this app also won't do on its own, since that's a persistent change to software outside its
  control, not a per-request setting. Since this feature's entire job is reading real source
  under a directory you point it at, and the two per-invocation options both grant materially
  more than that, Antigravity stays out of it.

Choosing Antigravity for "Resolve with coding agent" returns a clear `unsupported_provider`
error explaining this, rather than silently failing or requiring you to discover it by trial.
It works normally for the interactive drawer, which opens a real terminal a person drives — the
person approves each tool call themselves, the same way they would in any interactive `agy`
session.

If your chosen provider isn't installed, the app still works — you just cannot use that feature
with it. It fails cleanly with a `missing_agent` error naming the provider, rather than hanging
or half-working. Pick which one to use in the "✧ Coding assistant" panel's "Coding tool"
dropdown; the default is `codex`. (There's no separate default-provider control under
⚙ Preferences yet — the dropdown in the assistant panel is the only place this is set today.)

## Changing the front end

`web/studio/` is served as static files straight off disk. It is in two halves:

- **Plain scripts, loaded as-is** — `app.js`, `engineering.js`, `evidence.js`, `toolbox.js`,
  `tutorial.js`, `styles.css`, `index.html`, `tutorials.json`, `tools.json`. Edit one, reload the
  browser, done. No build step.
- **A React bundle** — everything under `web/studio/frontend/src/*.jsx` is bundled by esbuild into
  `web/studio/dist/studio.js`. **Editing a `.jsx` file changes nothing until you rebuild.** This
  is the single most common way to waste twenty minutes on this codebase.

```bash
cd web/studio/frontend
npm install          # once
npm run build        # after every change to src/*.jsx
```

Then reload the browser. A server restart is only needed after changing a `scripts/*.py` file.

`web/studio/dist/` is committed on purpose, so running the app needs no `npm` at all;
`web/studio/frontend/node_modules/` is git-ignored.

## What you see in the app

Landing page: **New project**, **Open project or model** (a `project.json` or a `.rossystem` on
disk), **Learn with a ROS example** (creates a separate publisher/subscriber practice project),
**Import an existing ROS source workspace**, and your recent projects.

Inside a project there are two workspaces:

- **Architecture**, with four tabs:
  - *Project brief* — purpose, acceptance criteria, behaviour and recovery.
  - *Architecture* — the graph. Select a node to inspect its package, interfaces and types; drag
    to rearrange; draw connections with live legality checking.
  - *Repositories* — link, extract, review, publish to the catalogue; check a linked repo for
    `*.repos` dependencies and Git submodules.
  - *Data types* — message, service and action definitions, persisted for generation.
- **Validate → Checks** — run the model checks and read the result.

## Checking a model

The check button runs up to three things in order, and stops early if an earlier one fails:

1. **Model generation gate** (`ros_studio.validate_project`) — can this project be emitted at all?
2. **ROS model linter** (`rosmodel_lint`) — the 81 static rules, run over the freshly generated
   files in a scratch directory.
3. **Language server oracle** (`ask_oracle`) — the real RosTooling language servers, only if a
   usable Java runtime is found. A cheap preflight checks the Java version first, so a missing or
   too-old JRE is reported in milliseconds with the actual reason instead of surfacing as a
   timed-out LSP handshake a minute later.

Each run is saved as an evidence record under the project directory. The oracle can take minutes,
so it deliberately runs *outside* the project lock — an edit made during that window is still
caught by the revision check when the result is recorded.

## Where things live on disk

Everything is under `--storage-root`:

```
<storage-root>/
  .studio-token                     the persisted session token (0600)
  projects/<name>-<id>/
    project.json                    the model itself
    repositories.repos              vcstool manifest of linked repositories
    src/vendor/<name>/              fetched checkouts
    .studio/local.json              local-only pointers to existing checkouts
    .studio/agent/                  prepared agent briefings
  repository-authorizations/        per-machine receipts for located checkouts
```

A project that names a local checkout it did not fetch itself is not trusted on sight: the path
carries a per-machine authorization receipt, and a project copied from another machine makes you
locate the checkout explicitly before it will read from it.

## API surface

Every route lives in `_route()` in `scripts/studio_server.py` — read it there rather than trusting
a list that can drift. In outline: `/api/bootstrap`, `/api/projects` (create/open/source),
`/api/tools`, `/api/plugins`, `/api/settings`, and then per project under
`/api/projects/<id>/…`: the project itself, `session`, `changes`, `reconcile`, `artifacts`,
`file`, `generation/preview|apply`, `validate`, `evidence`, `recovery`, `source/…`,
`repositories`, `repositories/dependencies`, `repositories/submodules`, `catalogue/…`,
`handoff`, `tools/open`, `runtime` and `jobs/…`.
