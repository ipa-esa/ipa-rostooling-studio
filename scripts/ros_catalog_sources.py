#!/usr/bin/env python3
"""
ros_catalog_sources.py -- fetch the package lists behind references/popular-ros-packages.md and
print which repositories are new, so an agent only has to describe and file those.

It writes nothing to the reference file: hand-written notes there stay under the editor's control.

Usage:
    python ros_catalog_sources.py [<extra-url> ...] [--existing <file.md>] [--json]

Exit 0 when every source was read, 2 when a source failed or returned too little to trust.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request

SOURCES = [
    "https://metrics.ros.org/repos_table.html",
    "https://raw.githubusercontent.com/vovaekb/awesome_ros_packages_and_tools/HEAD/README.md",
    "https://raw.githubusercontent.com/fkromer/awesome-ros2/HEAD/README.md",
]
MIN_REPOS = 10  # fewer than this from a standing source means a shell page or a moved file
REPO = re.compile(r"https?://(?:www\.)?(?:github|gitlab)\.com/([\w.-]+)/([\w.-]+)", re.I)
MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)\s*(?:[-–—:]\s*)?(.*)")
NOT_OWNERS = {"sponsors", "topics", "features", "about", "apps", "orgs"}
DEFAULT_EXISTING = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                                "references", "popular-ros-packages.md")


def repo_key(url):
    match = REPO.match(url)
    if not match or match.group(1).lower() in NOT_OWNERS:
        return None
    name = match.group(2).rstrip(".")
    if name.lower().endswith(".git"):
        name = name[:-4]
    return ("%s/%s" % (match.group(1), name)).lower() if name else None


def parse(text):
    """{owner/repo: {"url", "name", "about"}} from Markdown or HTML."""
    found = {}
    for line in text.splitlines():
        md = MD_LINK.search(line)
        about = {}
        if md and repo_key(md.group(2)):
            about[repo_key(md.group(2))] = (md.group(1).strip(), md.group(3).strip()[:160])
        for match in REPO.finditer(html.unescape(line)):
            key = repo_key(match.group(0))
            if key is None or key in found:
                continue
            name, summary = about.get(key, (key.split("/")[1], ""))
            found[key] = {"url": "https://%s" % match.group(0).split("://", 1)[1].rstrip("./"),
                          "name": name, "about": summary}
    return found


def fetch(source):
    if os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if not source.lower().startswith(("https://", "http://")):
        raise ValueError("not a URL or a file")
    request = urllib.request.Request(source, headers={"User-Agent": "ros-catalog-refresh"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def collect(sources, existing_text, standing=()):
    known = set(parse(existing_text))
    repos, report = {}, []
    for source in sources:
        try:
            found = parse(fetch(source))
        except Exception as exc:
            report.append({"source": source, "status": "failed", "detail": str(exc), "repos": 0})
            continue
        thin = len(found) < (MIN_REPOS if source in standing else 1)
        report.append({"source": source, "status": "degraded" if thin else "ok",
                       "repos": len(found)})
        for key, info in found.items():
            entry = repos.setdefault(key, dict(info, sources=[]))
            entry["sources"].append(source)
            entry["about"] = entry["about"] or info["about"]
    new = [repos[k] for k in sorted(repos) if k not in known]
    return {"sources": report, "total": len(repos), "known": len(repos) - len(new), "new": new}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("extra", nargs="*")
    ap.add_argument("--existing", default=DEFAULT_EXISTING)
    ap.add_argument("--only", action="store_true", help="read just the given sources")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    existing = ""
    if os.path.isfile(args.existing):
        with open(args.existing, encoding="utf-8") as fh:
            existing = fh.read()
    standing = [] if args.only else SOURCES
    result = collect(standing + args.extra, existing, standing=standing)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for src in result["sources"]:
            print("%-8s %4d repos  %s %s" % (src["status"], src["repos"], src["source"],
                                             src.get("detail", "")))
        print("%d repositories, %d already listed, %d new:" % (
            result["total"], result["known"], len(result["new"])))
        for item in result["new"]:
            print("- [%s](%s)%s" % (item["name"], item["url"],
                                    " - " + item["about"] if item["about"] else ""))
    return 0 if all(s["status"] == "ok" for s in result["sources"]) else 2


if __name__ == "__main__":
    sys.exit(main())
