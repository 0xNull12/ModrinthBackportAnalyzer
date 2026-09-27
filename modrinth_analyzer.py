"""
Modrinth Backport Analyzer
-------------------------------------------------------------------------------------

CLI usage:
    python modrinth_analyzer.py --source 1.16.5 --target 1.12.2 --loader forge \\
        --category optimization --category technology --open-source-only \\
        --show-obsolete-only --top 30

GUI usage:
    python modrinth_analyzer.py            # o --gui
    
-------------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, asdict
from typing import Iterator, Callable

import requests

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "backport-analyzer/0.5 (you@example.com)"
LOADERS = {"fabric", "forge", "quilt", "neoforge", "liteloader", "rift"}

OPEN_SOURCE_LICENSES = {
    "mit", "apache-2.0", "gpl-2.0", "gpl-3.0", "lgpl-2.1", "lgpl-3.0",
    "agpl-3.0", "mpl-2.0", "bsd-2-clause", "bsd-3-clause", "isc",
    "unlicense", "cc0-1.0", "zlib", "epl-2.0", "artistic-2.0", "eupl-1.2",
    "gpl-2.0-only", "gpl-2.0-or-later", "gpl-3.0-only", "gpl-3.0-or-later",
    "lgpl-2.1-only", "lgpl-2.1-or-later", "lgpl-3.0-only", "lgpl-3.0-or-later",
    "agpl-3.0-only", "agpl-3.0-or-later",
}

HARD_CATEGORIES = {"library", "optimization", "cursed", "game-mechanics"}
EASY_CATEGORIES = {"magic", "technology", "adventure", "decoration",
                   "food", "equipment", "mobs", "storage", "worldgen",
                   "transportation", "utility", "social", "economy"}

DEFAULT_MOD_CATEGORIES = sorted(EASY_CATEGORIES | HARD_CATEGORIES)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_open_source(lic: str | None) -> bool:
    """Fijate si la licencia esta en el set de licencias open source"""
    return bool(lic) and lic.strip().lower() in OPEN_SOURCE_LICENSES


def version_key(v: str):
    """Ordena versiones de MC. Aca tambien caen los snapshots tipo 23w45a"""
    v = v.strip()
    m = re.match(r"^(\d{2})w(\d{2})([a-z])?$", v)
    if m:
        return (2000 + int(m.group(1)), int(m.group(2)),
                ord(m.group(3) or "a") - ord("a"), 0)
    parts = []
    for p in v.split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def sort_versions(versions) -> list[str]:
    """Ordena una lista de versiones y si algo falla, cae a sorted() pelao."""
    try:
        return sorted(versions, key=version_key)
    except Exception:
        return sorted(versions)


def days_since(iso_date: str) -> int:
    """Cuenta cuantos dias pasaron desde esa fecha ISO."""
    try:
        last = time.mktime(time.strptime(iso_date[:19], "%Y-%m-%dT%H:%M:%S"))
        return max(0, int((time.time() - last) / 86400))
    except Exception:
        return 0


def version_gap(source: str, target: str, ordered: list[str]) -> int:
    """Distancia en versiones release entre source y target."""
    try:
        return abs(ordered.index(source) - ordered.index(target))
    except ValueError:
        ks, kt = version_key(source), version_key(target)
        return abs(ks[1] - kt[1])


# --------------------------------------------------------------------------- #
# Cliente API
# --------------------------------------------------------------------------- #
class ModrinthClient:
    """
    Cliente HTTP con cache en SQLite y throttling
    Vos pegas una sola vez y despues todo sale del cache
    """

    def __init__(self, cache_path: str = "cache.sqlite",
                 min_interval: float = 0.25,
                 cache_ttl: int = 24 * 3600):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self._last_req = 0.0
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache(url TEXT PRIMARY KEY, body TEXT, ts REAL)"
        )
        self.db.commit()
        self._categories_cache: list[str] | None = None

    def _throttle(self) -> None:
        """Espera lo justo para no pasarte del rate limit."""
        delta = time.time() - self._last_req
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_req = time.time()

    def _get(self, path: str, params: dict | None = None):
        """
        GET con cache y reintentos. Si te comes un 429, esperas lo que
        diga Retry-After. Si es 404, devolves lista vacia.
        """
        key = path + "?" + json.dumps(params or {}, sort_keys=True)
        row = self.db.execute(
            "SELECT body, ts FROM cache WHERE url=?", (key,)
        ).fetchone()
        if row and (time.time() - row[1]) < self.cache_ttl:
            return json.loads(row[0])

        url = f"{API_BASE}{path}"
        for _ in range(5):
            self._throttle()
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5)))
                continue
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            self.db.execute(
                "INSERT OR REPLACE INTO cache(url, body, ts) VALUES (?,?,?)",
                (key, json.dumps(data), time.time()),
            )
            self.db.commit()
            return data
        raise RuntimeError(f"Fallo tras reintentos: {url}")

    def get_mod_categories(self) -> list[str]:
        """
        Trae la lista de categorias para mods desde /tag/category.
        Aca cacheas en memoria para no repetir la llamada.
        """
        if self._categories_cache is not None:
            return self._categories_cache
        data = self._get("/tag/category")
        if not isinstance(data, list):
            self._categories_cache = DEFAULT_MOD_CATEGORIES
            return self._categories_cache
        cats = sorted({
            c["name"] for c in data
            if c.get("project_type") == "mod" and c.get("name")
        })
        self._categories_cache = cats or DEFAULT_MOD_CATEGORIES
        return self._categories_cache

    def iter_mods(self, loader: str | None = None,
                  categories: set[str] | None = None,
                  page_size: int = 100,
                  progress: Callable[[int, int], None] | None = None
                  ) -> Iterator[dict]:
        """
        Itera mods paginando /search. Las categorias van como facet OR:
        si pones varias, te devuelve mods de cualquiera de ellas
        """
        offset = 0
        facets: list[list[str]] = [["project_type:mod"]]
        if loader:
            facets.append([f"categories:{loader}"])
        if categories:
            facets.append([f"categories:{c}" for c in sorted(categories)])

        while True:
            data = self._get("/search", {
                "facets": json.dumps(facets),
                "limit": page_size,
                "offset": offset,
            })
            hits = data.get("hits", [])
            if not hits:
                return
            yield from hits
            offset += len(hits)
            if progress:
                progress(offset, data.get("total_hits", 0))
            if offset >= data.get("total_hits", 0):
                return

    def get_project_dependencies(self, project_id: str) -> list[dict]:
        """Trae las deps de un proyecto puntual."""
        data = self._get(f"/project/{project_id}/dependencies")
        return data if isinstance(data, list) else []

    def get_release_versions(self) -> list[str]:
        """Lista de versiones release de Minecraft, ordenadas."""
        data = self._get("/tag/game_version")
        if not isinstance(data, list):
            return []
        releases = [v["version"] for v in data
                    if v.get("version_type") == "release"]
        return sort_versions(releases)


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    """Un mod candidato con toda la data del analisis."""
    project_id: str
    slug: str
    title: str
    license: str
    categories: str
    downloads: int
    follows: int
    loaders: str
    versions: str
    missing_in: str
    date_modified: str
    days_since_update: int
    obsolete: bool
    deps_required: int
    deps_optional: int
    deps_missing_in_target: int
    viability: int
    url: str


# --------------------------------------------------------------------------- #
# Analisis
# --------------------------------------------------------------------------- #
def find_candidates(mods: list[dict], source: str, target: str,
                    loaders: set[str] | None = None,
                    min_downloads: int = 0,
                    only_open_source: bool = False,
                    max_obsolete_days: int = 365,
                    hide_obsolete: bool = False,
                    show_obsolete_only: bool = False) -> list[Candidate]:
    """
    Devuelve los mods que soportan `source` pero NO `target`.
    Si pones hide_obsolete, dejas fuera los abandonados.
    Si pones show_obsolete_only, solo te muestra los abandonados
    (esto ignora hide_obsolete).
    """
    out: list[Candidate] = []
    for m in mods:
        versions = set(m.get("versions", []))
        if source not in versions or target in versions:
            continue

        mod_loaders = LOADERS & set(m.get("categories", []))
        if loaders and not (mod_loaders & loaders):
            continue
        if m.get("downloads", 0) < min_downloads:
            continue

        lic = (m.get("license") or "unknown").lower()
        if only_open_source and not is_open_source(lic):
            continue

        days = days_since(m.get("date_modified", ""))
        obsolete = days > max_obsolete_days

        if show_obsolete_only:
            if not obsolete:
                continue
        elif hide_obsolete and obsolete:
            continue

        mod_cats = set(m.get("categories", [])) - LOADERS

        out.append(Candidate(
            project_id=m["project_id"],
            slug=m.get("slug", ""),
            title=m.get("title", ""),
            license=lic,
            categories=", ".join(sorted(mod_cats)),
            downloads=m.get("downloads", 0),
            follows=m.get("follows", 0),
            loaders=",".join(sorted(mod_loaders)),
            versions=", ".join(sort_versions(versions)),
            missing_in=target,
            date_modified=m.get("date_modified", ""),
            days_since_update=days,
            obsolete=obsolete,
            deps_required=0,
            deps_optional=0,
            deps_missing_in_target=0,
            viability=0,
            url=f"https://modrinth.com/mod/{m.get('slug', '')}",
        ))
    return out


def enrich_with_dependencies(client: ModrinthClient,
                             candidates: list[Candidate],
                             mods_by_id: dict[str, dict],
                             target: str,
                             progress: Callable[[int, int], None] | None = None
                             ) -> None:
    """
    Rellena los campos deps_* de cada candidato.
    Aca pegas una request por candidato (queda cacheada).
    """
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        deps = client.get_project_dependencies(c.project_id)
        req = [d for d in deps if d.get("dependency_type") == "required"]
        opt = [d for d in deps if d.get("dependency_type") == "optional"]
        c.deps_required = len(req)
        c.deps_optional = len(opt)

        missing = 0
        for d in req:
            dep_id = d.get("project_id")
            if not dep_id:
                continue
            dep_mod = mods_by_id.get(dep_id)
            if dep_mod is None:
                continue
            if target not in dep_mod.get("versions", []):
                missing += 1
        c.deps_missing_in_target = missing

        if progress:
            progress(i, total)


def compute_viability(c: Candidate, gap: int, deps_fetched: bool) -> int:
    """
    Score 0-100 (mas alto = mas atractivo para backportear).
    Suma cinco factores: popularidad, recencia, deps, gap y categoria.
    """
    score = 0.0

    # Popularidad: escala logaritmica, 1k ronda 12 y 1M toca 25
    pop = min(25.0, math.log10(max(1, c.downloads)) / 6 * 25)
    score += pop

    # Recencia: actualizado hoy = 25, un anio = 0
    score += max(0.0, min(25.0, 25 * (1 - c.days_since_update / 365)))

    # Dependencias: cada requerida resta, y cada faltante pega mas fuerte
    if deps_fetched:
        penalty = min(25.0, c.deps_missing_in_target * 8 + c.deps_required * 2)
        score += 25 - penalty
    else:
        score += 12.5

    # Gap de versiones: mientras mas lejos, peor
    score += max(0.0, 15 - min(15.0, gap * 4))

    # Categoria: los content mods suelen ser mas faciles que libs/optimizacion
    cats = {s.strip() for s in c.categories.split(",") if s.strip()}
    if cats & HARD_CATEGORIES:
        score += 2
    elif cats & EASY_CATEGORIES:
        score += 10
    else:
        score += 6

    return int(round(score))


# --------------------------------------------------------------------------- #
# Exportadores
# --------------------------------------------------------------------------- #
def export_csv(cands: list[Candidate], path: str) -> None:
    """Vuelca los candidatos a CSV, una fila por mod."""
    fields = list(Candidate.__dataclass_fields__.keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in cands:
            w.writerow(asdict(c))


def export_markdown(cands: list[Candidate], path: str,
                    source: str, target: str, loader: str | None,
                    categories: set[str] | None = None,
                    obsolete_mode: str = "all") -> None:
    """Genera un Markdown con encabezado y tabla de candidatos."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Backport candidates: `{source}` -> `{target}`\n\n")
        f.write(f"- **Date:** {time.strftime('%Y-%m-%d %H:%M')}\n")
        if loader:
            f.write(f"- **Loader:** `{loader}`\n")
        if categories:
            f.write(f"- **Categories:** {', '.join(sorted(categories))}\n")
        f.write(f"- **Obsolete mode:** {obsolete_mode}\n")
        f.write(f"- **Total:** {len(cands)} mods\n\n")
        f.write("| # | Viability | Mod | Categories | License | Downloads | "
                "Deps (req/missing) | Days idle | Versions | Missing in |\n")
        f.write("|---|----------:|-----|------------|---------|----------:|"
                "------------------:|----------:|----------|------------|\n")
        for i, c in enumerate(cands, 1):
            obs = " (obsolete)" if c.obsolete else ""
            f.write(
                f"| {i} | **{c.viability}** | [{c.title}]({c.url}) | "
                f"{c.categories} | `{c.license}` | {c.downloads:,} | "
                f"{c.deps_required} / {c.deps_missing_in_target} | "
                f"{c.days_since_update}{obs} | {c.versions} | **{c.missing_in}** |\n"
            )


# --------------------------------------------------------------------------- #
# Salida consola
# --------------------------------------------------------------------------- #
def print_table(cands: list[Candidate]) -> None:
    """Muestra la tabla compacta en consola."""
    print(f"\n{'#':<3} {'Viab':>4} {'Mod':<32} {'Cat':<18} {'Lic':<11} "
          f"{'Downloads':>10} {'Deps':>6} {'Days':>5}  URL")
    print("-" * 130)
    for i, c in enumerate(cands, 1):
        title = (c.title[:29] + "..") if len(c.title) > 31 else c.title
        cat = (c.categories[:15] + "..") if len(c.categories) > 17 else c.categories
        deps = f"{c.deps_required}/{c.deps_missing_in_target}"
        age = f"{c.days_since_update}{'!' if c.obsolete else ''}"
        print(f"{i:<3} {c.viability:>4} {title:<32} {cat:<18} {c.license:<11} "
              f"{c.downloads:>10,} {deps:>6} {age:>5}  {c.url}")


def print_diff(cands: list[Candidate], source: str, target: str) -> None:
    """Muestra el diff completo de versiones, bloque por mod."""
    print(f"\nVersion diff ({source} -> missing {target})\n")
    for i, c in enumerate(cands, 1):
        print(f"=== {i}. [{c.viability}] {c.title} - {c.loaders} - {c.license} ===")
        print(f"    Categories : {c.categories}")
        print(f"    Versions   : {c.versions}")
        print(f"    Missing in : {c.missing_in}")
        print(f"    Deps       : {c.deps_required} required, "
              f"{c.deps_missing_in_target} also missing in {target}")
        print(f"    Updated    : {c.days_since_update} days ago"
              f"{' (OBSOLETE)' if c.obsolete else ''}")
        print(f"    URL        : {c.url}\n")


# --------------------------------------------------------------------------- #
# Pipeline compartido
# --------------------------------------------------------------------------- #
def run_analysis(client: ModrinthClient,
                 source: str, target: str,
                 loader: str | None,
                 min_downloads: int,
                 only_open_source: bool,
                 categories: set[str] | None,
                 max_obsolete_days: int,
                 hide_obsolete: bool,
                 show_obsolete_only: bool,
                 skip_deps: bool,
                 log: Callable[[str], None] = print
                 ) -> list[Candidate]:
    """
    Pipeline completo: indexar, filtrar, enriquecer deps y puntuar.
    La GUI y el CLI comparten esto para no duplicar logica.
    """
    log(f"Indexing mods (loader={loader or 'any'}, "
        f"categories={sorted(categories) if categories else 'all'})...")
    mods = list(client.iter_mods(
        loader=loader,
        categories=categories,
        progress=lambda d, t: log(f"  indexing {d}/{t}"),
    ))
    log(f"Indexed: {len(mods)} mods")
    mods_by_id = {m["project_id"]: m for m in mods}

    cands = find_candidates(
        mods, source, target,
        loaders={loader} if loader else None,
        min_downloads=min_downloads,
        only_open_source=only_open_source,
        max_obsolete_days=max_obsolete_days,
        hide_obsolete=hide_obsolete,
        show_obsolete_only=show_obsolete_only,
    )
    log(f"Candidates after filters: {len(cands)}")

    if not skip_deps and cands:
        log(f"Fetching dependencies ({len(cands)} mods)...")
        enrich_with_dependencies(
            client, cands, mods_by_id, target,
            progress=lambda d, t: log(f"  deps {d}/{t}"),
        )

    log("Fetching release versions list...")
    releases = client.get_release_versions()

    for c in cands:
        gap = version_gap(source, target, releases) if releases else 0
        c.viability = compute_viability(c, gap, deps_fetched=not skip_deps)

    cands.sort(key=lambda c: c.viability, reverse=True)
    return cands


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cli_main(argv: list[str] | None = None) -> None:
    """Punto de entrada del modo consola."""
    p = argparse.ArgumentParser(
        description="Find backport candidates on Modrinth.")
    p.add_argument("--source", required=True,
                   help="Version where the mod DOES exist (e.g. 1.16.5)")
    p.add_argument("--target", required=True,
                   help="Version where the mod does NOT exist (e.g. 1.12.2)")
    p.add_argument("--loader", choices=sorted(LOADERS),
                   help="Loader to filter by (fabric/forge/quilt/neoforge)")
    p.add_argument("--category", action="append", default=[],
                   help="Category (repeatable). E.g. --category optimization "
                        "--category technology")
    p.add_argument("--min-downloads", type=int, default=1000,
                   help="Minimum downloads to consider a mod")
    p.add_argument("--open-source-only", action="store_true",
                   help="Only include open source licenses")
    p.add_argument("--max-obsolete-days", type=int, default=365,
                   help="Days without updates to consider a mod obsolete")
    p.add_argument("--hide-obsolete", action="store_true",
                   help="Exclude mods idle for more than max-obsolete-days")
    p.add_argument("--show-obsolete-only", action="store_true",
                   help="Show ONLY abandoned mods (good adoption candidates)")
    p.add_argument("--skip-deps", action="store_true",
                   help="Skip dependency analysis (faster)")
    p.add_argument("--diff-versions", action="store_true",
                   help="Show full version list per mod instead of a table")
    p.add_argument("--top", type=int, default=50,
                   help="How many rows to print to console")
    p.add_argument("--csv", default="candidates.csv",
                   help="CSV output path (default: candidates.csv)")
    p.add_argument("--md", default=None,
                   help="Optional Markdown output path")
    args = p.parse_args(argv)

    client = ModrinthClient()
    cands = run_analysis(
        client,
        source=args.source, target=args.target,
        loader=args.loader,
        min_downloads=args.min_downloads,
        only_open_source=args.open_source_only,
        categories=set(args.category) if args.category else None,
        max_obsolete_days=args.max_obsolete_days,
        hide_obsolete=args.hide_obsolete,
        show_obsolete_only=args.show_obsolete_only,
        skip_deps=args.skip_deps,
    )

    top = cands[:args.top]
    if args.diff_versions:
        print_diff(top, args.source, args.target)
    else:
        print_table(top)

    export_csv(cands, args.csv)
    print(f"\nCSV: {args.csv} ({len(cands)} rows)")
    if args.md:
        mode = ("obsolete only" if args.show_obsolete_only
                else "hidden" if args.hide_obsolete else "all")
        export_markdown(cands, args.md, args.source, args.target,
                        args.loader, set(args.category) or None, mode)
        print(f"Markdown: {args.md}")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def gui_main() -> None:
    """Punto de entrada de la interfaz grafica con Tkinter."""
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    # Mapeo: columna del Treeview -> atributo de Candidate (para ordenar)
    SORT_MAP = {
        "rank": None,
        "viability": "viability",
        "title": "title",
        "categories": "categories",
        "license": "license",
        "downloads": "downloads",
        "deps": "deps_required",
        "age": "days_since_update",
        "missing": "missing_in",
    }

    class AnalyzerApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Modrinth Backport Analyzer v0.5")
            self.geometry("1520x820")
            self.minsize(1150, 640)
            self.client = ModrinthClient()
            self.candidates: list[Candidate] = []     # resultados crudos
            self.view: list[Candidate] = []           # tras filtro + orden
            self._running = False
            self._sort_col: str | None = "viability"  # orden actual
            self._sort_desc: bool = True
            self._last_params: dict | None = None
            self.categories = self.client.get_mod_categories()
            self._build_ui()

        def _build_ui(self):
            """Arma toda la ventana: filtros, categorias, tabla y barra."""
            top = ttk.Frame(self, padding=10)
            top.pack(fill="x")

            # --- Fila 0 ---
            ttk.Label(top, text="Source:").grid(row=0, column=0, sticky="w")
            self.source_var = tk.StringVar(value="1.16.5")
            ttk.Entry(top, textvariable=self.source_var, width=10).grid(
                row=0, column=1, sticky="w")

            ttk.Label(top, text="Target:").grid(row=0, column=2, sticky="w",
                                                padx=(12, 0))
            self.target_var = tk.StringVar(value="1.12.2")
            ttk.Entry(top, textvariable=self.target_var, width=10).grid(
                row=0, column=3, sticky="w")

            ttk.Label(top, text="Loader:").grid(row=0, column=4, sticky="w",
                                                padx=(12, 0))
            self.loader_var = tk.StringVar(value="forge")
            ttk.Combobox(top, textvariable=self.loader_var, width=13,
                         values=["(any)"] + sorted(LOADERS),
                         state="readonly").grid(row=0, column=5, sticky="w")

            # --- Fila 1 ---
            ttk.Label(top, text="Min downloads:").grid(row=1, column=0,
                                                       sticky="w", pady=(8, 0))
            self.min_dl_var = tk.IntVar(value=1000)
            ttk.Entry(top, textvariable=self.min_dl_var, width=10).grid(
                row=1, column=1, sticky="w", pady=(8, 0))

            self.os_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(top, text="Open source only",
                            variable=self.os_var).grid(
                row=1, column=2, sticky="w", pady=(8, 0))

            self.obs_mode = tk.StringVar(value="all")
            obs_frame = ttk.Frame(top)
            obs_frame.grid(row=1, column=3, columnspan=3, sticky="w",
                           pady=(8, 0))
            ttk.Label(obs_frame, text="Obsolete:").pack(side="left")
            ttk.Radiobutton(obs_frame, text="All", variable=self.obs_mode,
                            value="all").pack(side="left", padx=(6, 0))
            ttk.Radiobutton(obs_frame, text="Hide", variable=self.obs_mode,
                            value="hide").pack(side="left", padx=(6, 0))
            ttk.Radiobutton(obs_frame, text="Obsolete only",
                            variable=self.obs_mode,
                            value="only").pack(side="left", padx=(6, 0))

            self.skip_deps_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(top, text="Skip deps (fast)",
                            variable=self.skip_deps_var).grid(
                row=1, column=6, sticky="w", pady=(8, 0), padx=(12, 0))

            # --- Fila 2: categorias ---
            cat_frame = ttk.LabelFrame(
                top,
                text="Categories (click = toggle, Ctrl/Shift = multi-select)",
                padding=6)
            cat_frame.grid(row=2, column=0, columnspan=6, sticky="we",
                           pady=(10, 0))

            listbox_wrap = ttk.Frame(cat_frame)
            listbox_wrap.pack(fill="both", expand=True)

            self.cat_listbox = tk.Listbox(listbox_wrap, selectmode="extended",
                                          height=6, exportselection=False,
                                          font=("TkDefaultFont", 9))
            for c in self.categories:
                self.cat_listbox.insert("end", c)
            self.cat_listbox.pack(side="left", fill="both", expand=True)

            cat_scroll = ttk.Scrollbar(listbox_wrap, orient="vertical",
                                       command=self.cat_listbox.yview)
            cat_scroll.pack(side="right", fill="y")
            self.cat_listbox.configure(yscrollcommand=cat_scroll.set)
            self.cat_listbox.bind("<Button-1>", self._on_cat_click)

            btn_row = ttk.Frame(cat_frame)
            btn_row.pack(fill="x", pady=(4, 0))
            ttk.Button(btn_row, text="Clear", width=10,
                       command=self._clear_cats).pack(side="left")
            ttk.Button(btn_row, text="Select all", width=12,
                       command=self._select_all_cats).pack(side="left",
                                                           padx=(4, 0))
            ttk.Label(btn_row, text="(no selection = all categories)",
                      foreground="#666").pack(side="left", padx=(10, 0))

            # --- Botones principales ---
            btns = ttk.Frame(top)
            btns.grid(row=0, column=7, rowspan=3, padx=(20, 0), sticky="ns")
            self.btn_run = ttk.Button(btns, text="Analyze",
                                      command=self.on_analyze)
            self.btn_run.pack(fill="x")
            ttk.Button(btns, text="Export CSV",
                       command=self.on_export_csv).pack(fill="x", pady=(4, 0))
            ttk.Button(btns, text="Export Markdown",
                       command=self.on_export_md).pack(fill="x", pady=(4, 0))

            # --- Barra de busqueda ---
            search_frame = ttk.Frame(self, padding=(10, 8, 10, 0))
            search_frame.pack(fill="x")
            ttk.Label(search_frame, text="Search:").pack(side="left")
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write",
                                      lambda *_: self._apply_filter())
            self.search_entry = ttk.Entry(search_frame,
                                          textvariable=self.search_var)
            self.search_entry.pack(side="left", fill="x", expand=True,
                                   padx=(6, 6))
            self.search_clear_btn = ttk.Button(search_frame, text="X",
                                               width=3,
                                               command=self._clear_search)
            self.search_clear_btn.pack(side="left")
            ttk.Label(search_frame,
                      text="multiple terms = AND (e.g. 'optimization mit')",
                      foreground="#666").pack(side="left", padx=(10, 0))

            # --- Treeview ---
            tf = ttk.Frame(self, padding=(10, 6, 10, 0))
            tf.pack(fill="both", expand=True)
            cols = ("rank", "viability", "title", "categories", "license",
                    "downloads", "deps", "age", "missing")
            self.tree = ttk.Treeview(tf, columns=cols, show="headings")
            spec = [
                ("rank", "#", 40, "center"),
                ("viability", "Viability", 75, "center"),
                ("title", "Mod", 250, "w"),
                ("categories", "Categories", 190, "w"),
                ("license", "License", 90, "center"),
                ("downloads", "Downloads", 95, "e"),
                ("deps", "Deps req/missing", 130, "center"),
                ("age", "Days idle", 85, "center"),
                ("missing", "Missing in", 85, "center"),
            ]
            for cid, txt, w, anchor in spec:
                self.tree.heading(cid, text=txt,
                                  command=lambda c=cid: self._sort_by(c))
                self.tree.column(cid, width=w, anchor=anchor)
            vsb = ttk.Scrollbar(tf, orient="vertical",
                                command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            self.tree.bind("<Double-1>", self._open_url)

            # --- Status ---
            self.status_var = tk.StringVar(value="Ready.")
            ttk.Label(self, textvariable=self.status_var, relief="sunken",
                      anchor="w", padding=(8, 2)).pack(fill="x", side="bottom")

            self._refresh_headers()

        # -------------------- Categorias: interaccion -------------------- #
        def _on_cat_click(self, event):
            """
            Click simple -> toggle del item bajo el cursor.
            Ctrl/Shift   -> deja que Tk haga su multi-seleccion nativa.
            """
            lb = self.cat_listbox
            idx = lb.nearest(event.y)
            if idx < 0:
                return "break"
            if event.state & 0x0005:   # Ctrl o Shift
                return
            if idx in lb.curselection():
                lb.selection_clear(idx)
            else:
                lb.selection_clear(0, "end")
                lb.selection_set(idx)
            return "break"

        def _clear_cats(self):
            self.cat_listbox.selection_clear(0, "end")

        def _select_all_cats(self):
            self.cat_listbox.selection_set(0, "end")

        # -------------------- Busqueda -------------------- #
        def _clear_search(self):
            self.search_var.set("")

        def _apply_filter(self):
            """Filtra self.candidates -> self.view y repinta la tabla."""
            query = self.search_var.get().strip().lower()
            if not query:
                self.view = list(self.candidates)
            else:
                terms = query.split()
                filtered = []
                for c in self.candidates:
                    haystack = " ".join([
                        c.title, c.slug, c.categories, c.license,
                        c.loaders, c.missing_in,
                        f"{c.downloads}", f"{c.viability}",
                    ]).lower()
                    if all(t in haystack for t in terms):
                        filtered.append(c)
                self.view = filtered

            self._sort_view()
            self._repopulate_tree()

        # -------------------- Ordenamiento -------------------- #
        def _sort_by(self, col: str):
            """Alterna orden asc/desc para la columna que clickeaste."""
            attr = SORT_MAP.get(col)
            if attr is None:      # la columna "#" no se ordena
                return
            if self._sort_col == col:
                self._sort_desc = not self._sort_desc
            else:
                self._sort_col = col
                # Por defecto: descendente para numeros, ascendente para texto
                self._sort_desc = col in ("viability", "downloads", "deps", "age")
            self._sort_view()
            self._repopulate_tree()
            self._refresh_headers()

        def _sort_view(self):
            if not self._sort_col:
                return
            attr = SORT_MAP.get(self._sort_col)
            if not attr:
                return
            def key(c):
                v = getattr(c, attr)
                if isinstance(v, str):
                    return v.lower()
                return v
            self.view.sort(key=key, reverse=self._sort_desc)

        def _refresh_headers(self):
            """Pinta la flecha asc/desc en el encabezado activo."""
            labels = {
                "rank": "#", "viability": "Viability", "title": "Mod",
                "categories": "Categories", "license": "License",
                "downloads": "Downloads", "deps": "Deps req/missing",
                "age": "Days idle", "missing": "Missing in",
            }
            for col, base in labels.items():
                if col == self._sort_col:
                    arrow = " (desc)" if self._sort_desc else " (asc)"
                    self.tree.heading(col, text=base + arrow)
                else:
                    self.tree.heading(col, text=base)

        # -------------------- Repintado de tabla -------------------- #
        def _repopulate_tree(self):
            self.tree.delete(*self.tree.get_children())
            skip = self._last_params["skip_deps"] if self._last_params else False
            for i, c in enumerate(self.view, 1):
                deps = f"{c.deps_required} / {c.deps_missing_in_target}" \
                    if not skip else "-"
                age = f"{c.days_since_update}{' (obs)' if c.obsolete else ''}"
                self.tree.insert("", "end", iid=str(i - 1), values=(
                    i, c.viability, c.title, c.categories, c.license,
                    f"{c.downloads:,}", deps, age, c.missing_in,
                ))
            total = len(self.candidates)
            shown = len(self.view)
            if total == shown:
                msg = f"{shown} results"
            else:
                msg = f"{shown} of {total} results (filtered)"
            if self._last_params:
                msg += (f"  -  {self._last_params['source']} -> "
                        f"missing {self._last_params['target']}")
            self.status_var.set(msg)

        # ------------------------ Acciones ------------------------ #
        def _loader(self) -> str | None:
            v = self.loader_var.get()
            return None if v == "(any)" else v

        def _selected_categories(self) -> set[str]:
            return {self.cat_listbox.get(i)
                    for i in self.cat_listbox.curselection()}

        def on_analyze(self):
            """Toma los filtros de la UI y lanza el analisis en un hilo."""
            if self._running:
                return
            cats = self._selected_categories()
            mode = self.obs_mode.get()
            params = dict(
                source=self.source_var.get().strip(),
                target=self.target_var.get().strip(),
                loader=self._loader(),
                min_dl=int(self.min_dl_var.get() or 0),
                os_only=bool(self.os_var.get()),
                categories=cats or None,
                hide_obs=(mode == "hide"),
                show_obs_only=(mode == "only"),
                skip_deps=bool(self.skip_deps_var.get()),
            )
            if not params["source"] or not params["target"]:
                messagebox.showwarning("Missing info",
                                       "Fill in Source and Target.")
                return
            self._running = True
            self.btn_run.state(["disabled"])
            self.tree.delete(*self.tree.get_children())
            self.candidates = []
            self.view = []
            threading.Thread(target=self._run, args=(params,),
                             daemon=True).start()

        def _run(self, p: dict):
            """Corre el pipeline en background y actualiza la UI al volver."""
            try:
                cands = run_analysis(
                    self.client,
                    source=p["source"], target=p["target"],
                    loader=p["loader"],
                    min_downloads=p["min_dl"],
                    only_open_source=p["os_only"],
                    categories=p["categories"], max_obsolete_days=365,
                    hide_obsolete=p["hide_obs"],
                    show_obsolete_only=p["show_obs_only"],
                    skip_deps=p["skip_deps"],
                    log=lambda s: self._set_status(s),
                )
                self.candidates = cands
                self._last_params = p
                self.after(0, self._apply_filter)  # aplica busqueda + orden
            except Exception as e:
                err = str(e)
                self.after(0, lambda: messagebox.showerror("Error", err))
                self._set_status(f"Error: {err}")
            finally:
                self._running = False
                self.after(0, lambda: self.btn_run.state(["!disabled"]))

        def _open_url(self, _e):
            """Doble click en una fila abre la pagina del mod."""
            item = self.tree.focus()
            if not item:
                return
            idx = int(item)
            if 0 <= idx < len(self.view):
                webbrowser.open(self.view[idx].url)

        def _set_status(self, msg: str):
            self.after(0, lambda: self.status_var.set(msg))

        # ------------------------ Exportar ------------------------ #
        def on_export_csv(self):
            if not self.view:
                messagebox.showinfo("No data",
                                    "Run an analysis first.")
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", initialfile="candidates.csv",
                filetypes=[("CSV", "*.csv"), ("All", "*.*")])
            if path:
                export_csv(self.view, path)
                self._set_status(f"CSV saved: {path} ({len(self.view)} rows)")

        def on_export_md(self):
            if not self.view:
                messagebox.showinfo("No data",
                                    "Run an analysis first.")
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".md", initialfile="candidates.md",
                filetypes=[("Markdown", "*.md"), ("All", "*.*")])
            if path:
                mode = {"all": "all", "hide": "hidden",
                        "only": "obsolete only"}[self.obs_mode.get()]
                export_markdown(self.view, path,
                                self.source_var.get(),
                                self.target_var.get(),
                                self._loader(),
                                self._selected_categories() or None,
                                mode)
                self._set_status(
                    f"Markdown saved: {path} ({len(self.view)} rows)")

    AnalyzerApp().mainloop()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if len(sys.argv) == 1 or "--gui" in sys.argv:
        gui_main()
    else:
        cli_main()
