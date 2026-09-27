<div align=center>
<h1>Modrinth Backport Analyzer</h1>

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3%20License-orange?style=flat-square)](https://www.gnu.org/licenses/gpl-3.0.html)

**I have no plans to update this tool UNLESS it's necessary, since it fulfills its primary function**

Analysis tool to find Minecraft mods that serve as candidates for backport, forward-port or adoption of abandoned projects, using the public Modrinth API.

</div>

---

## What is it for

When a mod exists for a Minecraft version but not for another, there is an opportunity: porting it. This tool automates the search for those cases.

Typical example: a mod is published for **1.16.5** but never arrived to **1.12.2**. That mod is a candidate to backport. If it is also abandoned and has an open source license, it is an ideal candidate to adopt it, improve it and publish it.

The tool does three things:

1. **Indexes** all mods from Modrinth (or those of a specific loader).
2. **Compares** what Minecraft versions each one supports.
3. **Filters and scores** those that are missing in your target version, sorting them by how attractive they are to port.

---

## What it shows

For each candidate mod, the tool calculates and shows:

- **Supported Versions**: the Minecraft versions the mod is compatible with.
- **Missing Version**: the target version that is not supported.
- **License**: whether it is open source (MIT, Apache, GPL, etc.) or not.
- **Categories**: optimization, technology, magic, decoration, etc.
- **Dependencies**: how many dependencies it requires and how many of those are also missing in the target version.
- **Days since last update**: to identify abandoned mods.
- **Feasibility score (0–100)**: how suitable the mod is for porting, based on a combination of popularity, recent activity, dependencies, the version gap, and the type of mod.

---

## Usage modes

**Normal mode**: finds mods that exist in one version and are missing in another.

**Obsolete mode**: shows only abandoned mods (more than X days without update), ideal to adopt.

**Diff-versions mode**: instead of a table, shows for each mod the complete list of supported versions, useful to see the exact gap.

---

## Available filters

- Origin version and target version.
- Loader (Fabric, Forge, Quilt, NeoForge, etc.).
- Categories (multiple selection).
- Minimum downloads.
- Only open source mods.
- Obsolete mods (three modes: all, hide, only abandoned).
- Dependency analysis (can be omitted to go faster).

---

## Outputs

- **On-screen table** sortable by any column
- **Export CSV**: one row per mod with all data.
- **Export Markdown**: formatted table with header and metadata

---

## How to use

It has two ways of being executed:

**Graphical interface**: it opens with double click or from console without arguments. It has filters, category selector, sortable table, search and export buttons

**Command line**: to automate, run on server or integrate in scripts. All filters are available as arguments or if you are in a Linux distro without graphical interface

---

## Example use case

**Goal**: find optimization and technology mods that are in 1.16.5, missing in 1.12.2, are open source and are abandoned.

The tool returns a list sorted by viability. The first results are the ones most worth checking: popular, without problematic dependencies, with permissive license and without recent activity from the original author

---

## Limitations

- Only analyzes mods published on Modrinth. Exclusive CurseForge mods do not appear.
- The viability score is a heuristic. It guides, does not decide.
- The API indicates if a mod exists for a version, but not if the backport is easy. That depends on internal changes of Minecraft, use of Mixins, deep dependencies and other factors that require human review (unless you want to use AI for that, your mod, your rules)
- The real difficulty of the backport cannot be calculated automatically

The tool accelerates the search, but the final decision and the porting work remain your decisions

---

## Requirements

- Python 3.10 or superior.
- `requests` library.
- Tkinter (comes included with Python in most installations).

---

## Notes

- The results are cached in a local SQLite file. The first execution takes longer, the next ones are instantaneous
- Respects the rate limit of the Modrinth API (300 requests per minute)
- Does not require account nor API key


> **This idea comes from me since I have the goal of porting the majority of mods to 1.12.2 and this helps me of doing a quick analysis instead of searching**
