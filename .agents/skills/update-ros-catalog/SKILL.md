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

1. Read `references/popular-ros-packages.md` if it already exists, so the refresh can note
   what's new versus the previous version rather than silently overwriting history.
2. Fetch every source (the 3 standing ones plus any URLs found in `$ARGUMENTS`) with a web
   tool, extracting real package/repo names — never invent ones a fetch didn't actually
   return; if a fetch is degraded or JS-rendered and only returns a shell, say so rather than
   padding the list from general knowledge.
3. Cross-reference the fetched content against the existing file content (from step 1, if it
   existed) so entries can be marked `new this refresh` versus `already known`, and preserve
   any hand-written notes in the existing file rather than clobbering them.
4. Write the merged result back to `references/popular-ros-packages.md` in the same format
   as before (table or grouped list: name, one-line purpose, source(s), category if the
   awesome-list gives one).
5. Report back a short summary to the user: total packages, how many are new this run, and
   whether any source fetch was degraded. Do not silently swallow a "this fetch was
   degraded" note — that's exactly the kind of thing the user needs to know before trusting
   the refreshed list.

## Non-goals

This skill does not touch `assets/rosmodelscatalog/` or `assets/roscommonobjects/` (the
vendored, oracle-verified catalogs used for actual `.rossystem`/`.ros2` authoring — see
`scripts/sync_catalogue.sh` for those). `references/popular-ros-packages.md` is a
lighter-weight, unverified "packages worth knowing about" list for suggestions, not a
validated source of `from:`/`type:` references. Never resolve a `from:` or `type:` against
this file — only against `assets/type_index.json` / `assets/node_index.json` per the
`ros-model` skill.
