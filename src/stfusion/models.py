from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pyvista as pv
from pyproj import CRS


@dataclass(frozen=True)
class Bounds3D:
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    zmin: float
    zmax: float

    def xy_intersection(self, other: Bounds3D) -> Bounds3D:
        return Bounds3D(
            xmin=max(self.xmin, other.xmin),
            xmax=min(self.xmax, other.xmax),
            ymin=max(self.ymin, other.ymin),
            ymax=min(self.ymax, other.ymax),
            zmin=self.zmin,
            zmax=self.zmax,
        )

    @property
    def empty_xy(self) -> bool:
        return self.xmin >= self.xmax or self.ymin >= self.ymax

    def pretty(self) -> str:
        return (
            f"x[{self.xmin:.6f}, {self.xmax:.6f}] "
            f"y[{self.ymin:.6f}, {self.ymax:.6f}] "
            f"z[{self.zmin:.6f}, {self.zmax:.6f}]"
        )


@dataclass(frozen=True)
class TargetGrid:
    crs: CRS
    origin: tuple[float, float, float]
    spacing: tuple[float, float, float]
    dimensions: tuple[int, int, int]  # 点数：nx, ny, nz

    @property
    def nx(self) -> int:
        return self.dimensions[0]

    @property
    def ny(self) -> int:
        return self.dimensions[1]

    @property
    def nz(self) -> int:
        return self.dimensions[2]

    @property
    def bounds(self) -> Bounds3D:
        ox, oy, oz = self.origin
        dx, dy, dz = self.spacing
        nx, ny, nz = self.dimensions
        return Bounds3D(
            xmin=ox,
            xmax=ox + (nx - 1) * dx,
            ymin=oy,
            ymax=oy + (ny - 1) * dy,
            zmin=oz,
            zmax=oz + (nz - 1) * dz,
        )

    def xs(self) -> np.ndarray:
        ox, dx, nx = self.origin[0], self.spacing[0], self.nx
        return ox + np.arange(nx, dtype=np.float64) * dx

    def ys(self) -> np.ndarray:
        oy, dy, ny = self.origin[1], self.spacing[1], self.ny
        return oy + np.arange(ny, dtype=np.float64) * dy

    def zs(self) -> np.ndarray:
        oz, dz, nz = self.origin[2], self.spacing[2], self.nz
        return oz + np.arange(nz, dtype=np.float64) * dz


@dataclass
class VtkSource:
    name: str
    mesh: pv.DataSet
    crs: CRS
    bounds: Bounds3D
    spacing_xy: tuple[float, float]
    origin: tuple[float, float, float]
    z_spacing: float
    z_origin: float
    nz: int
    array_filter: list[str] | None = None
    kind: Literal["vtk"] = "vtk"


@dataclass
class GeoTiffSource:
    name: str
    path: Path
    crs: CRS
    bounds: Bounds3D
    spacing_xy: tuple[float, float]
    field: str
    band: int
    resampling: str
    nodata: float | None = None
    kind: Literal["geotiff"] = "geotiff"


@dataclass
class WrfSource:
    name: str
    path: Path
    field: str
    wrf_var: str = "QVAPOR"
    kind: Literal["wrf"] = "wrf"


@dataclass
class TileSubset:
    """切片里被气象网格用到的子块：k_index × j_index × [i_start, i_start + i_count)。

    data 每个场形状为 (len(k_index), len(j_index), i_count)。索引均为 0 长度时表示该切片不参与融合。
    """

    k_index: np.ndarray
    j_index: np.ndarray
    i_start: int
    i_count: int
    data: dict[str, np.ndarray]


@dataclass
class TurbulenceTile:
    path: Path
    origin: tuple[float, float, float]
    spacing: tuple[float, float, float]
    dimensions: tuple[int, int, int]
    n_points: int
    fields: dict
    subset: TileSubset | None = None


@dataclass
class TurbulenceSource:
    name: str
    path: Path
    tiles: list[TurbulenceTile]
    fields: list[str]
    resampling: str = "nearest"
    kind: Literal["turbulence"] = "turbulence"


Source = VtkSource | GeoTiffSource | WrfSource | TurbulenceSource


@dataclass
class SourceSpec:
    name: str
    kind: Literal["vtk", "geotiff", "wrf", "turbulence"]
    path: Path
    field: str | None = None
    band: int = 1
    arrays: list[str] | None = None
    resampling: str = "bilinear"
    crs: str | None = None
    wrf_var: str = "QVAPOR"


@dataclass
class FusionConfig:
    target_crs: str
    vtk_crs: str
    strategy: Literal["coarsest", "finest"]
    extent: Literal["intersection"]
    sources: list[SourceSpec]
    output: Path
    config_dir: Path = field(default_factory=lambda: Path("."))
