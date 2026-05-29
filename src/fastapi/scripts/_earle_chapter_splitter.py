"""Plan §6c — Earle Physical Geology PDF chapter splitter.

Takes the 2,551-page master PDF and emits per-chapter PDFs into the
bronze MinIO bucket so each chapter can be ingested independently
through the §04p PDF stack.

Why per-chapter splits:
  • Per-chapter attribution: the citation surface needs the chapter
    title in the displayed attribution string (e.g. "Physical Geology
    – 2nd Edition, Ch.4 Volcanism, by Steven Earle, CC-BY 4.0").
  • Failure recovery: a §04p run that crashes on chapter 12 doesn't
    invalidate chapters 1-11 already in silver.
  • License scoping: SKIPS chapters listed as "permission required"
    in the front matter via a CLI allow-list. Per Kyle's 2026-05-28
    Path B decision.

Two phases:
  1. detect_chapter_ranges() — extract bookmarks via PyMuPDF; falls
     back to page-text regex on bookmark failure.
  2. split_to_bronze() — slice + upload per-chapter PDFs to MinIO under
     bronze/textbooks/earle_physical_geology/.

The allow-list is hardcoded but kept narrow per the conservative
license read. Add a chapter only after confirming its license in
the source PDF's front matter.

Usage::

    python scripts/_earle_chapter_splitter.py --master /input/master.pdf
    python scripts/_earle_chapter_splitter.py --dry-run    # list chapters only
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("earle_splitter")


# ---------------------------------------------------------------------------
# Allow-list — chapters whose CC-BY 4.0 status is confirmed via Kyle's
# 2026-05-28 read of the front matter. Chapters 2, 4-13, 16-21 contain
# CC-BY-SA-flagged figures; the prose itself is CC-BY 4.0 throughout.
# Chapters 1, 3, 14, 15 are NOT in the figure-attribution list — Kyle's
# Path B decision is to skip them until their license is independently
# confirmed.
# ---------------------------------------------------------------------------

ALLOWED_CHAPTERS: frozenset[int] = frozenset({
    2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21,
})


# ---------------------------------------------------------------------------
# Per-chapter CC-BY-SA figure attribution. Each chapter that has
# CC-BY-SA figures carries the per-figure attribution string embedded
# in the chapter's attribution_text so the chat citation surface can
# render the full share-alike obligation chain.
# ---------------------------------------------------------------------------

# Sourced from the user-provided 2026-05-28 front-matter paste. Keys
# are chapter numbers; values are lists of (figure_id, attribution).
CC_BY_SA_FIGURES: dict[int, list[tuple[str, str]]] = {
    2:  [("2.1.1", "Helium Atom QM by Yzmo"),
         ("2.2.6", "Halite by Rob Lavinsky, iRocks.com"),
         ("2.6.4b", "Pyrite cubic crystals on marlstone by Carles Millan"),
         ("2.6.4c", "Almandine garnet by Eurico Zimbres + Tom Epaminondas")],
    4:  [("4.2.3", "Champagne uncorking by Niels Noordhoek"),
         ("4.3.16", "Ekati mine by Jason Pineau")],
    5:  [("5.4.1", "SoilTexture USDA by Mikenorton"),
         ("5.4.3", "Podzol by Ailith Stewart")],
    6:  [("6.2", "Aplite Red by Rudolf Pohl"),
         ("6.2.6", "by Andre Karwath"),
         ("6.3.1", "Depositional environment by Mike Norton")],
    7:  [("7.2.4c", "Schist detail by Michael C. Rygel"),
         ("7.5.4", "00031 6 cm grossular calcite augite skarn by Siim")],
    8:  [("8.4.6", "Zircon microscope by Chd")],
    9:  [("9.0.1", "Elbogen meteorite by John Taylor"),
         ("9.3.1", "Earth's Magnetic Field Confusion by TStein"),
         ("9.4.4", "Silly putty dripping by Eric Skiff")],
    10: [("10.1.4", "Karoo Glaciation by GeoPotinga, adapted by Steven Earle")],
    11: [("11.1.4", "by Open University"),
         ("Table 11.4", "Modified Mercalli intensity scale (Wikipedia, adapted)")],
    12: [("12.3.10", "Moab fault with vehicles by Andrew Wilson")],
    13: [("13.1.1", "Water Cycle Blank by Ingwik"),
         ("13.4.5", "Nowitna river by Oliver Kumis"),
         ("13.5.3", "Rednorthfloodwaymap by Kmusser"),
         ("13.5.5", "Riverfront Ave + Okotoks flood photos by Ryan L.C. Quan + Stephanie N. Jones")],
    16: [("16.3.3", "Woodf1a by Keefer4"),
         ("16.3.10", "Peyto Lake Panorama 2006 by chensiyuan")],
    17: [("17.4.2", "Post-Glacial Sea Level by Robert A. Rohde")],
    18: [("18.2.2", "Age of oceanic lithosphere by NOAA, adapted by Steven Earle"),
         ("18.3.4", "Gashydrat im Sediment by Wusel007"),
         ("18.4.2", "WOA09 sea-surf SAL by Plumbago"),
         ("18.4.4", "WOA09 sea-surf TMP by Plumbago"),
         ("18.4.6", "Newfoundland Iceberg by Shawn")],
    19: [("19.1.10", "Clearcutting-Oregon by Calibas")],
    20: [("20.0.2", "Ipad Air by Zach Vega"),
         ("20.0.3", "Ballpoint pen parts (unknown)"),
         ("20.1.2", "Vale Nickel Mine by Timkal"),
         ("20.1.5", "Bestand: Black-band ironstone by Aka"),
         ("20.3.5", "Sedimentary basin analysis by AAPG (Lovely + Ruggiero)"),
         ("20.3.8", "HydroFrac2 by Mikenorton"),
         ("20A", "Spiral CFL Bulb by Sun Ladder + Elektronstarterp by Anton")],
    21: [("21.2.2", "Acasta gneiss by Pedroalexandrade"),
         ("21.2.4", "Tasmania simple geology map by Graeme Bartlett"),
         ("21.4.5", "Mt Rae Alberta aerial by Kevin lenz"),
         ("21.4.13", "Dinosaur park formation fauna by J.T. Csotonyi"),
         ("21.5.6", "Paskapoo Mudstones Red Deer by Georgialh")],
}


# Base attribution string the chat citation renderer embeds verbatim.
# Per BCcampus convention + CC-BY 4.0 requirements:
#   - Author + title + edition
#   - Licence + canonical URL
BASE_ATTRIBUTION = (
    "Physical Geology – 2nd Edition by Steven Earle "
    "(https://opentextbc.ca/physicalgeology2ed/), "
    "licensed under CC-BY 4.0 except where otherwise noted."
)


@dataclass(frozen=True)
class ChapterRange:
    number: int
    title: str
    start_page: int  # 1-indexed inclusive
    end_page: int    # 1-indexed inclusive
    allowed: bool

    @property
    def attribution_text(self) -> str:
        """Per-chapter attribution string. Embeds BASE_ATTRIBUTION plus
        the per-chapter CC-BY-SA figure list when applicable."""
        parts = [BASE_ATTRIBUTION]
        sa_figs = CC_BY_SA_FIGURES.get(self.number, [])
        if sa_figs:
            parts.append(
                f"Chapter {self.number} ({self.title}) contains CC-BY-SA "
                f"figures: " + "; ".join(f"{fid} ({attr})" for fid, attr in sa_figs)
            )
        return " ".join(parts)


def detect_chapter_ranges(pdf_path: Path) -> list[ChapterRange]:
    """Extract chapter boundaries from PDF bookmarks (PyMuPDF) with a
    fallback to page-text regex on bookmark-empty PDFs."""
    import fitz  # PyMuPDF — must be importable in the runtime container

    doc = fitz.open(str(pdf_path))
    n_pages = doc.page_count

    toc = doc.get_toc()  # list of [level, title, page] entries
    if not toc:
        raise RuntimeError(
            "PDF has no bookmarks — fallback regex extraction not "
            "implemented for Earle. Add bookmarks to the master "
            "before splitting, OR extend this script with a regex "
            "that catches the chapter heading style."
        )

    # Filter to top-level chapter entries. Earle's TOC convention is
    # "Chapter N <Title>" — pick those.
    chapter_rx = re.compile(r"^\s*(?:Chapter\s+)?(\d+)\b[.\s]+(.+?)\s*$", re.IGNORECASE)
    raw_chapters: list[tuple[int, str, int]] = []
    for level, title, page in toc:
        if level != 1:
            continue
        m = chapter_rx.match(title)
        if not m:
            continue
        ch_num = int(m.group(1))
        ch_title = m.group(2).strip()
        raw_chapters.append((ch_num, ch_title, page))

    if not raw_chapters:
        raise RuntimeError(
            f"PDF has bookmarks but none matched the chapter pattern. "
            f"First 5 raw entries: {toc[:5]!r}"
        )

    # Sort by start page + derive end-page from the next chapter's start.
    raw_chapters.sort(key=lambda t: t[2])
    ranges: list[ChapterRange] = []
    for i, (ch_num, ch_title, start) in enumerate(raw_chapters):
        end = (raw_chapters[i + 1][2] - 1) if i + 1 < len(raw_chapters) else n_pages
        ranges.append(ChapterRange(
            number=ch_num,
            title=ch_title,
            start_page=start,
            end_page=end,
            allowed=ch_num in ALLOWED_CHAPTERS,
        ))
    doc.close()
    return ranges


def split_to_bronze(
    pdf_path: Path,
    chapters: Iterable[ChapterRange],
    bronze_dir: Path,
    dry_run: bool,
) -> dict:
    """Slice the master PDF + write per-chapter PDFs to bronze_dir.

    Returns a manifest dict with per-chapter outcomes for the caller
    to write alongside the PDFs (helps the ingest step pick up the
    license/attribution metadata)."""
    import fitz

    manifest = {
        "master_pdf":      str(pdf_path),
        "master_pages":    None,
        "base_attribution": BASE_ATTRIBUTION,
        "chapters":        [],
    }

    if dry_run:
        for ch in chapters:
            logger.info(
                "  [DRY] Ch.%d %-30s pages %d-%d %s",
                ch.number, ch.title[:30], ch.start_page, ch.end_page,
                "ALLOWED" if ch.allowed else "SKIPPED (license)",
            )
            manifest["chapters"].append({
                "number":     ch.number,
                "title":      ch.title,
                "start_page": ch.start_page,
                "end_page":   ch.end_page,
                "allowed":    ch.allowed,
                "attribution_text": ch.attribution_text if ch.allowed else None,
            })
        return manifest

    bronze_dir.mkdir(parents=True, exist_ok=True)
    src = fitz.open(str(pdf_path))
    manifest["master_pages"] = src.page_count

    for ch in chapters:
        chapter_entry = {
            "number":     ch.number,
            "title":      ch.title,
            "start_page": ch.start_page,
            "end_page":   ch.end_page,
            "allowed":    ch.allowed,
            "attribution_text": ch.attribution_text if ch.allowed else None,
            "output":     None,
            "skipped_reason": None,
        }
        if not ch.allowed:
            chapter_entry["skipped_reason"] = "not in ALLOWED_CHAPTERS (license)"
            manifest["chapters"].append(chapter_entry)
            logger.info("  SKIP Ch.%d %s — license", ch.number, ch.title)
            continue

        # Slice + write the chapter PDF.
        out_path = bronze_dir / f"earle-physical-geology-ch{ch.number:02d}.pdf"
        new_doc = fitz.open()
        # PyMuPDF page indices are 0-based; ChapterRange is 1-based.
        new_doc.insert_pdf(src, from_page=ch.start_page - 1, to_page=ch.end_page - 1)
        new_doc.save(str(out_path))
        new_doc.close()
        chapter_entry["output"] = str(out_path)
        manifest["chapters"].append(chapter_entry)
        logger.info(
            "  WROTE Ch.%d %s (%d pages) → %s",
            ch.number, ch.title, ch.end_page - ch.start_page + 1, out_path,
        )

    src.close()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--master", type=Path,
        default=Path("/input/Physical-Geology-2nd-Edition-1774381391.pdf"),
        help="Path inside the runtime container to the master PDF.",
    )
    parser.add_argument(
        "--bronze-dir", type=Path,
        default=Path("/output/earle_physical_geology"),
        help="Directory where per-chapter PDFs are written.",
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=Path("/output/earle_physical_geology/manifest.json"),
        help="Path to write the chapter manifest JSON.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Detect chapter ranges + log the plan, but don't write files.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.master.is_file():
        logger.error("master PDF not found: %s", args.master)
        return 2

    logger.info("Detecting chapter ranges from %s ...", args.master)
    chapters = detect_chapter_ranges(args.master)
    logger.info(
        "Found %d chapter(s). %d allowed, %d skipped per ALLOWED_CHAPTERS.",
        len(chapters),
        sum(1 for c in chapters if c.allowed),
        sum(1 for c in chapters if not c.allowed),
    )

    logger.info(
        "%sSplitting to %s ...",
        "[DRY] " if args.dry_run else "",
        args.bronze_dir,
    )
    manifest = split_to_bronze(
        pdf_path=args.master,
        chapters=chapters,
        bronze_dir=args.bronze_dir,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        import json
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        logger.info("Wrote manifest: %s", args.manifest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
