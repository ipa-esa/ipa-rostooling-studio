#!/usr/bin/env python3
"""install_agent_skills.py -- Streamlined skill exporter for RosTooling agentic skills.

Makes RosTooling skills (.agents/skills/*) available to Claude Code, Google Antigravity,
GitHub Copilot, Cursor, and other agentic platforms.

Usage:
    # 1. Install to Claude Code globally (available in all projects):
    python3 scripts/install_agent_skills.py --platform claude --global

    # 2. Install to Claude Code for a specific workspace:
    python3 scripts/install_agent_skills.py --platform claude --workspace /path/to/ros_ws

    # 3. Install to all platforms (Claude Code + Antigravity):
    python3 scripts/install_agent_skills.py --platform all --global

    # 4. Check status of installed skills:
    python3 scripts/install_agent_skills.py --status

    # 5. Remove/uninstall skills:
    python3 scripts/install_agent_skills.py --platform claude --global --uninstall
"""

import argparse
import os
from pathlib import Path
import shutil
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
STUDIO_ROOT = SCRIPT_DIR.parent
SOURCE_SKILLS_DIR = STUDIO_ROOT / ".agents" / "skills"

# Standard target directories per platform
PLATFORM_PATHS = {
    "claude": {
        "global": Path.home() / ".claude" / "skills",
        "workspace_rel": Path(".claude") / "skills",
    },
    "antigravity": {
        "global": Path.home() / ".gemini" / "antigravity" / "skills",
        "workspace_rel": Path(".agents") / "skills",
    },
    "copilot": {
        "global": Path.home() / ".copilot" / "skills",
        "workspace_rel": Path(".github") / "skills",
    },
}


def discover_source_skills() -> list[Path]:
    """Find all valid skill folders containing SKILL.md."""
    if not SOURCE_SKILLS_DIR.is_dir():
        return []
    skills = []
    for entry in sorted(SOURCE_SKILLS_DIR.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            skills.append(entry)
    return skills


def install_skills(
    skills: list[Path],
    dest_dir: Path,
    use_symlink: bool = True,
    dry_run: bool = False,
) -> int:
    """Install or symlink skills into destination directory."""
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    installed_count = 0
    for skill in skills:
        target = dest_dir / skill.name
        if target.is_symlink() or target.is_file():
            if not dry_run:
                target.unlink()
        elif target.is_dir():
            if not dry_run:
                shutil.rmtree(target)

        if dry_run:
            print(f"[dry-run] Would {'symlink' if use_symlink else 'copy'} {skill.name} -> {target}")
            installed_count += 1
            continue

        if use_symlink:
            try:
                target.symlink_to(skill.resolve(), target_is_directory=True)
                print(f"  ✓ Symlinked {skill.name} -> {target}")
                installed_count += 1
            except OSError as e:
                print(f"  ✗ Failed to symlink {skill.name}: {e}, falling back to copy.")
                shutil.copytree(skill, target)
                print(f"  ✓ Copied {skill.name} -> {target}")
                installed_count += 1
        else:
            shutil.copytree(skill, target)
            print(f"  ✓ Copied {skill.name} -> {target}")
            installed_count += 1

    return installed_count


def uninstall_skills(skills: list[Path], dest_dir: Path, dry_run: bool = False) -> int:
    """Remove installed skills from destination directory."""
    if not dest_dir.is_dir():
        print(f"Destination {dest_dir} does not exist.")
        return 0

    removed_count = 0
    for skill in skills:
        target = dest_dir / skill.name
        if target.is_symlink() or target.is_file():
            if not dry_run:
                target.unlink()
            print(f"  ✓ Removed {target}")
            removed_count += 1
        elif target.is_dir():
            if not dry_run:
                shutil.rmtree(target)
            print(f"  ✓ Removed {target}")
            removed_count += 1
    return removed_count


def print_status(skills: list[Path]):
    """Show current installation status across all platforms."""
    print(f"RosTooling source skills directory: {SOURCE_SKILLS_DIR}")
    print(f"Available skills ({len(skills)}): {[s.name for s in skills]}\n")

    for platform, paths in PLATFORM_PATHS.items():
        gpath = paths["global"]
        print(f"[{platform.upper()}] Global ({gpath}):")
        if not gpath.is_dir():
            print("  (Directory not present)")
        else:
            found = []
            for s in skills:
                t = gpath / s.name
                if t.is_symlink():
                    found.append(f"{s.name} (symlink -> {t.resolve()})")
                elif t.is_dir():
                    found.append(f"{s.name} (directory copy)")
            if found:
                for item in found:
                    print(f"  • {item}")
            else:
                print("  (No RosTooling skills installed)")
        print()


def show_mcp_config():
    """Display the MCP server configuration for platforms supporting MCP."""
    mcp_script = (
        STUDIO_ROOT.parent
        / "RosTooling_Extension"
        / "rostooling-languages"
        / "bin"
        / "rostooling-mcp.js"
    )
    if not mcp_script.is_file():
        mcp_script = STUDIO_ROOT / "scripts" / "studio_server.py"

    node_bin = shutil.which("node") or "node"

    print("=== RosTooling MCP Server Configuration ===")
    print("To give Claude Code direct headless tool access (validate, generate wrappers, etc.):\n")
    print(f"  claude mcp add rostooling {node_bin} {mcp_script}\n")
    print("Or add to ~/.claude/mcp.json / ~/.claude/claude_desktop_config.json:")
    print("```json")
    config = {
        "mcpServers": {
            "rostooling": {
                "command": node_bin,
                "args": [str(mcp_script)],
            }
        }
    }
    import json
    print(json.dumps(config, indent=2))
    print("```\n")


def main():
    parser = argparse.ArgumentParser(
        description="Streamlined RosTooling skill exporter for Claude Code, Antigravity, and other agents."
    )
    parser.add_argument(
        "--platform",
        "-p",
        choices=["claude", "antigravity", "copilot", "all"],
        default="claude",
        help="Target platform (default: claude)",
    )
    parser.add_argument(
        "--global",
        "-g",
        dest="is_global",
        action="store_true",
        help="Install user-wide (e.g. ~/.claude/skills)",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=str,
        default="",
        help="Install into a specific workspace folder (e.g. /home/user/ros_ws)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy skill directories instead of creating symlinks",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove RosTooling skills from target location",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show status of skills across known platform paths",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Display Claude Code MCP server configuration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to disk",
    )

    args = parser.parse_args()

    skills = discover_source_skills()
    if not skills:
        print(f"Error: No skills found under {SOURCE_SKILLS_DIR}", file=sys.stderr)
        sys.exit(1)

    if args.mcp:
        show_mcp_config()
        if not (args.is_global or args.workspace or args.status):
            return

    if args.status:
        print_status(skills)
        return

    if not args.is_global and not args.workspace:
        # Default to global if neither specified
        args.is_global = True

    platforms = (
        ["claude", "antigravity", "copilot"] if args.platform == "all" else [args.platform]
    )

    action = "Uninstalling" if args.uninstall else "Installing"
    mode = "copy" if args.copy else "symlink"

    for plat in platforms:
        cfg = PLATFORM_PATHS[plat]
        dest_dirs = []
        if args.is_global:
            dest_dirs.append(cfg["global"])
        if args.workspace:
            ws = Path(args.workspace).expanduser().resolve()
            dest_dirs.append(ws / cfg["workspace_rel"])

        for d in dest_dirs:
            print(f"{action} {len(skills)} skills for {plat.upper()} at {d} ({mode})...")
            if args.uninstall:
                count = uninstall_skills(skills, d, dry_run=args.dry_run)
                print(f"Removed {count} skill(s) from {d}.\n")
            else:
                count = install_skills(
                    skills, d, use_symlink=not args.copy, dry_run=args.dry_run
                )
                print(f"Successfully installed {count} skill(s) to {d}.\n")


if __name__ == "__main__":
    main()
