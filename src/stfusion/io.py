from __future__ import annotations

from pathlib import Path

import numpy as np
import pyvista as pv
from pyproj import CRS, Transformer
from rasterio.warp import transform_bounds

from stfusion.crs import crs_equal, looks_projected
from stfusion.models import Bounds3D, GeoTiffSource, Source, SourceSpec, TurbulenceSource, VtkSource, WrfSource


class IoError(ValueError):
    pass


def load_source(spec: SourceSpec, target_crs: CRS, vtk_crs: CRS) -> Source:
    if spec.kind == "turbulence":
        if not spec.path.exists():
            raise IoError(f"找不到数据文件: {spec.path}")
        return load_turbulence(spec)
    if not spec.path.is_file():
        raise IoError(f"找不到数据文件: {spec.path}")
    if spec.kind == "vtk":
        crs = CRS.from_user_input(spec.crs) if spec.crs else vtk_crs
        return load_vtk(spec, crs, target_crs)
    if spec.kind == "wrf":
        return load_wrf(spec)
    crs = CRS.from_user_input(spec.crs) if spec.crs else None
    return load_geotiff(spec, target_crs, crs_override=crs)


def load_turbulence(spec: SourceSpec) -> TurbulenceSource:
    from stfusion.turbulence import load_turbulence_tiles

    tiles = load_turbulence_tiles(spec.path, spec.arrays)
    fields: list[str] = []
    for tile in tiles:
        for name in tile.fields:
            if name not in fields:
                fields.append(name)
    return TurbulenceSource(
        name=spec.name,
        path=spec.path,
        tiles=tiles,
        fields=fields,
        resampling=spec.resampling or "nearest",
    )


def load_wrf(spec: SourceSpec) -> WrfSource:
    return WrfSource(
        name=spec.name,
        path=spec.path,
        field=spec.field or spec.wrf_var or "QVAPOR",
        wrf_var=spec.wrf_var or "QVAPOR",
    )


def _to_target_xy_bounds(
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    src_crs: CRS,
    target_crs: CRS,
) -> tuple[float, float, float, float]:
    if src_crs == target_crs:
        return xmin, xmax, ymin, ymax
    left, bottom, right, top = transform_bounds(
        src_crs, target_crs, xmin, ymin, xmax, ymax, densify_pts=21
    )
    return float(left), float(right), float(bottom), float(top)


def _transform_spacing(
    origin: tuple[float, float],
    spacing_xy: tuple[float, float],
    src_crs: CRS,
    target_crs: CRS,
) -> tuple[float, float]:
    if src_crs == target_crs:
        return abs(spacing_xy[0]), abs(spacing_xy[1])
    transformer = Transformer.from_crs(src_crs, target_crs, always_xy=True)
    x0, y0 = origin
    dx, dy = spacing_xy
    xa, ya = transformer.transform(x0, y0)
    xb, yb = transformer.transform(x0 + dx, y0)
    xc, yc = transformer.transform(x0, y0 + dy)
    out_dx = float(np.hypot(xb - xa, yb - ya))
    out_dy = float(np.hypot(xc - xa, yc - ya))
    return out_dx, out_dy


def estimate_axis_pitch(values: np.ndarray) -> float:
    unique = np.unique(np.round(values.astype(np.float64), 6))
    if unique.size >= 2:
        diffs = np.diff(unique)
        diffs = diffs[diffs > 0]
        if diffs.size:
            return float(np.median(diffs))
    span = float(values.max() - values.min())
    return span if span > 0 else 1.0


def _n_arrays(data) -> int:
    """兼容不同 PyVista 版本，统计点数据或单元格数据的场数量。"""
    try:
        return len(data)
    except TypeError:
        return len(list(data.keys()))


def load_vtk(spec: SourceSpec, src_crs: CRS, target_crs: CRS) -> VtkSource:
    mesh = pv.read(str(spec.path))
    if mesh.n_points == 0:
        raise IoError(f"VTK 没有点: {spec.path}")

    if _n_arrays(mesh.cell_data) and not _n_arrays(mesh.point_data):
        mesh = mesh.cell_data_to_point_data()

    xmin, xmax, ymin, ymax, zmin, zmax = (float(v) for v in mesh.bounds)
    txmin, txmax, tymin, tymax = _to_target_xy_bounds(
        xmin, xmax, ymin, ymax, src_crs, target_crs
    )

    if isinstance(mesh, pv.ImageData):
        ox, oy, oz = (float(v) for v in mesh.origin)
        dx, dy, dz = (float(v) for v in mesh.spacing)
        nx, ny, nz = (int(v) for v in mesh.dimensions)
        spacing_xy = _transform_spacing((ox, oy), (dx, dy), src_crs, target_crs)
        z_spacing = abs(dz) if nz > 1 else (abs(dz) if dz else 1.0)
        z_origin = oz
        if crs_equal(src_crs, target_crs):
            origin = (ox, oy, oz)
        else:
            transformer = Transformer.from_crs(src_crs, target_crs, always_xy=True)
            tox, toy = transformer.transform(ox, oy)
            origin = (float(tox), float(toy), oz)
    else:
        pts = np.asarray(mesh.points)
        dx = estimate_axis_pitch(pts[:, 0])
        dy = estimate_axis_pitch(pts[:, 1])
        dz = estimate_axis_pitch(pts[:, 2])
        zs = np.unique(np.round(pts[:, 2], 6))
        nz = max(int(zs.size), 1)
        spacing_xy = _transform_spacing((xmin, ymin), (dx, dy), src_crs, target_crs)
        z_spacing = float(dz) if nz > 1 else 1.0
        z_origin = float(zmin)
        origin = (xmin, ymin, zmin)
        nx = max(int(round((xmax - xmin) / dx)) + 1, 1) if dx else 1
        ny = max(int(round((ymax - ymin) / dy)) + 1, 1) if dy else 1

    return VtkSource(
        name=spec.name,
        mesh=mesh,
        crs=src_crs,
        bounds=Bounds3D(txmin, txmax, tymin, tymax, float(zmin), float(zmax)),
        spacing_xy=spacing_xy,
        origin=origin,
        z_spacing=z_spacing,
        z_origin=z_origin,
        nz=max(nz, 1),
        array_filter=spec.arrays,
    )


def load_geotiff(
    spec: SourceSpec,
    target_crs: CRS,
    crs_override: CRS | None = None,
) -> GeoTiffSource:
    import rasterio
    from rasterio.warp import transform_bounds as rio_transform_bounds

    with rasterio.open(spec.path) as src:
        src_crs = crs_override or src.crs
        if src_crs is None:
            raise IoError(
                f"GeoTIFF 缺少 CRS: {spec.path}。请在该 source 下设置 crs: EPSG:xxxx"
            )
        src_crs = CRS.from_user_input(src_crs)
        left, bottom, right, top = rio_transform_bounds(
            src_crs, target_crs, *src.bounds, densify_pts=21
        )
        dx = abs(float(right) - float(left)) / max(src.width, 1)
        dy = abs(float(top) - float(bottom)) / max(src.height, 1)
        nodata = src.nodata

    field = spec.field or spec.path.stem
    return GeoTiffSource(
        name=spec.name,
        path=spec.path,
        crs=src_crs,
        bounds=Bounds3D(float(left), float(right), float(bottom), float(top), 0.0, 0.0),
        spacing_xy=(dx, dy),
        field=field,
        band=spec.band,
        resampling=spec.resampling,
        nodata=float(nodata) if nodata is not None else None,
    )


def inspect_info(path: Path) -> dict:
    path = Path(path)
    if path.is_dir():
        from stfusion.turbulence import inspect_turbulence

        return inspect_turbulence(path)

    suffix = path.suffix.lower()
    if suffix in {".tif", ".tiff", ".geotiff"}:
        import rasterio

        with rasterio.open(path) as src:
            b = src.bounds
            return {
                "kind": "geotiff",
                "type": "GeoTIFF",
                "name": path.name,
                "crs": str(src.crs) if src.crs else None,
                "width": int(src.width),
                "height": int(src.height),
                "bands": int(src.count),
                "dtype": str(src.dtypes[0]),
                "bounds": [float(b.left), float(b.bottom), float(b.right), float(b.top)],
                "res": [float(src.res[0]), float(src.res[1])],
                "nodata": None if src.nodata is None else float(src.nodata),
            }

    from stfusion.wrf import inspect_wrf, looks_like_wrf

    if looks_like_wrf(path):
        return inspect_wrf(path)

    try:
        return _inspect_vtk(path)
    except Exception as vtk_exc:
        try:
            return inspect_wrf(path)
        except Exception:
            raise vtk_exc from None


def _inspect_vtk(path: Path) -> dict:
    from stfusion.vtk_legacy import VtkLegacyError, parse_structured_points

    size = path.stat().st_size
    header_info: dict | None = None
    try:
        header = parse_structured_points(path)
        bounds = [float(v) for v in header["bounds"]]
        fields = [item["name"] for item in header["fields"]]
        header_info = {
            "kind": "vtk",
            "type": "VTK ImageData",
            "name": path.name,
            "points": int(header["n_points"]),
            "cells": int(
                max(header["dimensions"][0] - 1, 0)
                * max(header["dimensions"][1] - 1, 0)
                * max(header["dimensions"][2] - 1, 0)
            ),
            "bounds": bounds,
            "fields": fields,
            "looks_projected": looks_projected(bounds[0], bounds[1], bounds[2], bounds[3]),
            "dimensions": [int(v) for v in header["dimensions"]],
            "origin": [float(v) for v in header["origin"]],
            "spacing": [float(v) for v in header["spacing"]],
        }
        if any(name in {"HorizontalShear", "VerticalShear", "Epsilon"} for name in fields):
            header_info["kind"] = "turbulence"
            header_info["type"] = "风切变/湍流 VTK"
            header_info["tiles"] = 1
        # 1GB 切片只读头；PyVista 写出的小文件常用 SCALARS，头解析可能不完整
        if size > 80_000_000 or (fields and header_info["kind"] == "turbulence"):
            return header_info
    except VtkLegacyError:
        header_info = None

    mesh = pv.read(str(path))
    info = {
        "kind": "vtk",
        "type": f"VTK {type(mesh).__name__}",
        "name": path.name,
        "points": int(mesh.n_points),
        "cells": int(mesh.n_cells),
        "bounds": [float(v) for v in mesh.bounds],
        "fields": list(mesh.point_data.keys()) + list(mesh.cell_data.keys()),
        "looks_projected": looks_projected(
            float(mesh.bounds[0]),
            float(mesh.bounds[1]),
            float(mesh.bounds[2]),
            float(mesh.bounds[3]),
        ),
    }
    if isinstance(mesh, pv.ImageData):
        info["dimensions"] = [int(v) for v in mesh.dimensions]
        info["origin"] = [float(v) for v in mesh.origin]
        info["spacing"] = [float(v) for v in mesh.spacing]
    return info


def inspect_path(path: Path) -> str:
    info = inspect_info(path)
    lines = [f"type: {info['type']}", f"path: {path}"]
    if info["kind"] == "geotiff":
        lines.extend(
            [
                f"crs: {info['crs']}",
                f"size: {info['width']} x {info['height']}",
                f"bands: {info['bands']}",
                f"dtype: {info['dtype']}",
                f"bounds: {info['bounds']}",
                f"nodata: {info['nodata']}",
                f"res: {info['res']}",
            ]
        )
        return "\n".join(lines)
    if info["kind"] == "wrf":
        lines.extend(
            [
                f"var: {info.get('var')}",
                f"grid: {info.get('west_east')} x {info.get('south_north')} x {info.get('bottom_top')}",
                f"lon: {info.get('lon')}",
                f"lat: {info.get('lat')}",
                f"dx: {info.get('dx')}",
            ]
        )
        return "\n".join(lines)
    if info["kind"] == "turbulence":
        lines.extend(
            [
                f"tiles: {info.get('tiles')}",
                f"fields: {', '.join(info.get('fields') or [])}",
                f"spacing: {info.get('spacing')}",
                f"bounds: {info.get('bounds')}",
            ]
        )
        if info.get("tile_names"):
            lines.append("files: " + ", ".join(info["tile_names"]))
        return "\n".join(lines)
    lines.extend(
        [
            f"points: {info['points']}",
            f"cells: {info['cells']}",
            f"bounds: {info['bounds']}",
        ]
    )
    if "dimensions" in info:
        lines.extend(
            [
                f"dimensions: {info['dimensions']}",
                f"origin: {info['origin']}",
                f"spacing: {info['spacing']}",
            ]
        )
    if info.get("fields"):
        lines.append("fields: " + ", ".join(info["fields"]))
    return "\n".join(lines)
