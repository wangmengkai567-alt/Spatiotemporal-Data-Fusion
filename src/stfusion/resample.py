from __future__ import annotations

import numpy as np
import pyvista as pv
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from stfusion.models import GeoTiffSource, TargetGrid, VtkSource


_RESAMPLING = {
    "bilinear": Resampling.bilinear,
    "nearest": Resampling.nearest,
}


def grid_from_image(mesh: pv.ImageData, crs) -> TargetGrid:
    ox, oy, oz = (float(v) for v in mesh.origin)
    dx, dy, dz = (float(v) for v in mesh.spacing)
    nx, ny, nz = (int(v) for v in mesh.dimensions)
    return TargetGrid(
        crs=crs,
        origin=(ox, oy, oz),
        spacing=(dx, dy, dz),
        dimensions=(nx, ny, nz),
    )


def overlay_geotiffs_on_vtk(vtk: VtkSource, geotiffs: list[GeoTiffSource]) -> pv.DataSet:
    """复制原始气象网格和全部原有场（含矢量）"""
    mesh = vtk.mesh.copy(deep=True)
    used = set(mesh.point_data.keys()) | set(mesh.cell_data.keys())
    if isinstance(mesh, pv.ImageData):
        grid = grid_from_image(mesh, vtk.crs)
        n = grid.nx * grid.ny * grid.nz
        for src in geotiffs:
            name = src.field if src.field not in used else f"{src.name}_{src.field}"
            volume = sample_geotiff(src, grid)
            mesh.point_data[name] = np.ascontiguousarray(volume.reshape(n, order="F"))
            used.add(name)
        return mesh

    pts = np.asarray(mesh.points)
    for src in geotiffs:
        name = src.field if src.field not in used else f"{src.name}_{src.field}"
        mesh.point_data[name] = _sample_geotiff_at_xy(src, pts[:, 0], pts[:, 1], vtk.crs)
        used.add(name)
    return mesh


def _sample_geotiff_at_xy(
    source: GeoTiffSource,
    xs: np.ndarray,
    ys: np.ndarray,
    vtk_crs,
) -> np.ndarray:
    transformer = Transformer.from_crs(vtk_crs, source.crs, always_xy=True)
    lon, lat = transformer.transform(xs, ys)
    coords = list(zip(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)))
    with rasterio.open(source.path) as src:
        values = np.array([row[source.band - 1] for row in src.sample(coords)], dtype=np.float32)
        nodata = src.nodata
    if nodata is not None:
        values[values == nodata] = np.nan
    return values


def sample_geotiff(source: GeoTiffSource, grid: TargetGrid) -> np.ndarray:
    nx, ny, nz = grid.dimensions
    xmin, ymin, _ = grid.origin
    dx, dy, _ = grid.spacing
    north = ymin + (ny - 1) * dy
    dst_transform = from_origin(xmin - dx / 2.0, north + dy / 2.0, dx, dy)
    dst = np.full((ny, nx), np.nan, dtype=np.float64)
    resampling = _RESAMPLING.get(source.resampling, Resampling.bilinear)

    with rasterio.open(source.path) as src:
        src_crs = src.crs or source.crs
        reproject(
            source=rasterio.band(src, source.band),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            src_nodata=src.nodata,
            dst_transform=dst_transform,
            dst_crs=grid.crs,
            dst_nodata=np.nan,
            resampling=resampling,
        )

    xy = np.flipud(dst).astype(np.float32)
    volume = np.repeat(xy.T[:, :, np.newaxis], nz, axis=2)
    return volume
