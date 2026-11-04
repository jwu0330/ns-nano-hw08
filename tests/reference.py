"""Independent reference implementation (oracle) for the dissociation queries.

Works directly on the teacher's parquet files with pandas/numpy and shares no
code with the API or the loader, so agreement between the two is evidence that
both are right.
"""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

# Folder with the teacher's parquet files; override with NS_DATA_DIR.
DEFAULT_DATA = Path(os.getenv("NS_DATA_DIR", Path(__file__).resolve().parents[2] / "raw" / "ns-nano"))


class Reference:
    def __init__(self, data_dir: Path = DEFAULT_DATA):
        self.data_dir = Path(data_dir)

    @cached_property
    def coords(self) -> pd.DataFrame:
        return pd.read_parquet(self.data_dir / "coordinates.parquet", columns=["study_id", "x", "y", "z"])

    @cached_property
    def ann(self) -> pd.DataFrame:
        a = pd.read_parquet(self.data_dir / "annotations.parquet").set_index("study_id")
        a = a[[c for c in a.columns if c.startswith("terms_abstract_tfidf__")]]
        a.columns = [c.removeprefix("terms_abstract_tfidf__") for c in a.columns]
        return a

    def studies_with_term(self, term: str, threshold: float = 0.001) -> set[str]:
        return set(self.ann.index[self.ann[term].to_numpy() > threshold])

    def studies_near(self, xyz: tuple[float, float, float], radius: float = 6.0) -> set[str]:
        d = self.coords[["x", "y", "z"]].to_numpy() - np.asarray(xyz, dtype=float)
        hit = np.sqrt((d * d).sum(axis=1)) <= radius
        return set(self.coords["study_id"].to_numpy()[hit])

    def dissociate_terms(self, a: str, b: str, threshold: float = 0.001) -> tuple[set[str], set[str]]:
        sa, sb = self.studies_with_term(a, threshold), self.studies_with_term(b, threshold)
        return sa - sb, sb - sa

    def dissociate_locations(self, a, b, radius: float = 6.0) -> tuple[set[str], set[str]]:
        sa, sb = self.studies_near(a, radius), self.studies_near(b, radius)
        return sa - sb, sb - sa

    # ---- counts behind the optional analysis (characteristic terms / locations)
    def term_count(self, studies: set[str], term: str, threshold: float = 0.001) -> int:
        return int((self.ann.loc[sorted(studies), term].to_numpy() > threshold).sum())

    def cell_count(self, studies: set[str], center: tuple[int, int, int], cell_mm: int = 10) -> int:
        """Studies with a focus whose nearest 10 mm grid point is ``center``."""
        c = self.coords[self.coords["study_id"].isin(studies)]
        grid = np.rint(c[["x", "y", "z"]].to_numpy() / cell_mm) * cell_mm
        hit = (grid == np.asarray(center)).all(axis=1)
        return int(c["study_id"][hit].nunique())
