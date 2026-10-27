"""Build data/places.csv.gz and data/zips.csv.gz from public-domain Census Gazetteer files + GeoNames.

Run once; the outputs are committed so nobody else needs to download anything.

    python manage.py build_gazetteer --download

Sources (all free):
  Census 2026 Gazetteer: places, county subdivisions, ZCTAs (public domain, pipe-delimited)
  GeoNames US.zip populated places (CC-BY 4.0): unincorporated communities Census doesn't list
"""

import csv
import gzip
import io
import tempfile
import zipfile
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from fuel.services.names import US_STATES, census_aliases, place_key

CENSUS = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_{}_national.zip"
GEONAMES = "https://download.geonames.org/export/dump/US.zip"
GEONAMES_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLL", "PPLS", "PPLF"}
SOURCE_RANK = {"census_place": 0, "census_cousub": 1, "geonames": 2}
FIELDS = ["key", "name", "state", "lat", "lon", "land_area_sqmi", "population", "source"]


def _read_census(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        header = fh.readline()
        delimiter = "|" if "|" in header else "\t"
        fh.seek(0)
        for row in csv.DictReader(fh, delimiter=delimiter):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


class Command(BaseCommand):
    help = "Build the offline gazetteer (places + ZIPs) from Census and GeoNames files."

    def add_arguments(self, parser):
        parser.add_argument("--download", action="store_true", help="Download the source files first.")
        parser.add_argument("--src", type=Path, help="Directory holding the extracted source .txt files.")
        parser.add_argument("--out", type=Path, default=settings.BASE_DIR / "data")

    def handle(self, *args, download: bool, src: Path | None, out: Path, **opts):
        if download:
            src = Path(tempfile.mkdtemp(prefix="gazetteer-"))
            for kind in ("place", "cousubs", "zcta"):
                self._fetch(CENSUS.format(kind), src)
            self._fetch(GEONAMES, src)
        if src is None:
            raise SystemExit("pass --download or --src DIR")

        best: dict[tuple[str, str], dict] = {}

        def offer(row: dict) -> None:
            ident = (row["key"], row["state"])
            current = best.get(ident)
            if current is None or self._better(row, current):
                best[ident] = row

        for source, pattern in (("census_place", "*place_national.txt"), ("census_cousub", "*cousubs_national.txt")):
            for rec in _read_census(next(src.glob(pattern))):
                if rec["USPS"] not in US_STATES:
                    continue
                for i, name in enumerate(census_aliases(rec["NAME"])):
                    key = place_key(name)
                    if key:
                        offer({
                            "key": key, "name": name, "state": rec["USPS"],
                            "lat": float(rec["INTPTLAT"]), "lon": float(rec["INTPTLONG"]),
                            "land_area_sqmi": float(rec["ALAND_SQMI"] or 0), "population": "", "source": source,
                            "alias": i > 0,
                        })  # fmt: skip

        with (src / "US.txt").open(encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) < 15 or f[6] != "P" or f[7] not in GEONAMES_CODES or f[10] not in US_STATES:
                    continue
                for name in dict.fromkeys([f[1], f[2]]):
                    key = place_key(name)
                    if key:
                        offer({
                            "key": key, "name": f[1], "state": f[10], "lat": float(f[4]), "lon": float(f[5]),
                            "land_area_sqmi": "", "population": int(f[14] or 0), "source": "geonames",
                            "alias": False,
                        })  # fmt: skip

        out.mkdir(parents=True, exist_ok=True)
        rows = sorted(best.values(), key=lambda r: (r["state"], r["key"]))
        with gzip.open(out / "places.csv.gz", "wt", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        zips = sorted(_read_census(next(src.glob("*zcta_national.txt"))), key=lambda r: r["GEOID"])
        with gzip.open(out / "zips.csv.gz", "wt", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["code", "lat", "lon"])
            writer.writerows([z["GEOID"], z["INTPTLAT"], z["INTPTLONG"]] for z in zips)

        by_source: dict[str, int] = {}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        self.stdout.write(self.style.SUCCESS(f"places: {len(rows)} {by_source}; zips: {len(zips)}"))

    @staticmethod
    def _better(new: dict, old: dict) -> bool:
        """Real names beat aliases, Census beats GeoNames, then the bigger place wins (never 'last row read')."""
        new_rank, old_rank = (new["alias"], SOURCE_RANK[new["source"]]), (old["alias"], SOURCE_RANK[old["source"]])
        if new_rank != old_rank:
            return new_rank < old_rank
        size = "population" if new["source"] == "geonames" else "land_area_sqmi"
        return float(new[size] or 0) > float(old[size] or 0)

    def _fetch(self, url: str, dest: Path) -> None:
        self.stdout.write(f"downloading {url}")
        resp = httpx.get(url, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for member in zf.namelist():
                if member.endswith(".txt") and not member.startswith("readme"):
                    zf.extract(member, dest)
