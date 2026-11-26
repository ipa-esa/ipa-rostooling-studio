---
name: update-ros-catalog
description: Refresh the popular-ROS-packages reference by fetching metrics.ros.org and the two awesome-list sources (plus any extra URLs passed in $ARGUMENTS).
---

## What this skill does

Refreshes the workspace's top-level `references/popular-ros-packages.md` (this repo's
`references/` folder, not this skill's own — every `references/popular-ros-packages.md`
mention below is that same repo-root file) — a reference table of well-known ROS 2 packages
the `ros-model` skill can draw on when suggesting packages to a user, so it isn't limited to
whatever happens to be in `assets/rosmodelscatalog/` and `assets/roscommonobjects/`.

**Cost note:** this is a web scrape and a merge, not modeling work — if whoever is steering
can hand it to a smaller model or a lighter sub-agent, it does not need a full reasoning pass.

## Standing sources (always fetched)

1. `https://metrics.ros.org/repos_table.html` — table of ROS 2 repositories with metrics.
2. `https://github.com/vovaekb/awesome_ros_packages_and_tools` — curated "awesome list" of
   ROS packages/tools, organized by category. **Known to overlap with source 1** — dedupe,
   don't double-list.
3. `https://github.com/fkromer/awesome-ros2` — a second, differently-maintained curated
   "awesome list" for ROS 2 specifically. **Known to overlap with source 2** — same "awesome
   list" genre, likely different but partially-overlapping contents — dedupe against both
   sources 1 and 2, don't double-list.

## Extra sources (from $ARGUMENTS)

The caller may pass additional URLs as arguments to this skill: `$ARGUMENTS`. Treat each
whitespace-separated token in `$ARGUMENTS` that looks like a URL as an additional source to
merge in alongside the three standing sources above. If `$ARGUMENTS` is empty, just refresh
the three standing sources.

## How to run it

1. Run the script from the workspace root. It reads the three standing sources and your extra
   URLs, extracts the repositories, removes duplicates, and compares them with
   `references/popular-ros-packages.md`:
   ```bash
   python3 scripts/ros_catalog_sources.py $ARGUMENTS
   ```
   It prints one status line per source, then only the repositories that are not listed yet. It
   writes nothing.
2. Add the new entries to `references/popular-ros-packages.md` in the file's existing format
   (name, one-line purpose, source(s), category if known), marked `new this refresh`. Leave existing
   entries and hand-written notes as they are. Never invent a package, and leave the purpose out
   rather than guessing it.
3. A source marked `failed` or `degraded` (the script exits 2) was not read properly, for example a
   JavaScript-rendered page that returns only a shell. Fetch that one with a web tool if you can,
   and say so in your report either way.
4. Report the total, the number of new entries, and every degraded source.

## Non-goals

This skill does not touch `assets/rosmodelscatalog/` or `assets/roscommonobjects/` (the
vendored, oracle-verified catalogs used for actual `.rossystem`/`.ros2` authoring — see
`scripts/sync_catalogue.sh` for those). `references/popular-ros-packages.md` is a
lighter-weight, unverified "packages worth knowing about" list for suggestions, not a
validated source of `from:`/`type:` references. Never resolve a `from:` or `type:` against
this file — only against `assets/type_index.json` / `assets/node_index.json` per the
`ros-model` skill.
