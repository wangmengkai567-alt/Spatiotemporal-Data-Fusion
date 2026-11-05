"""把 WRF wrfout 中的湿度采到原始气象 VTK 网格上。

水平：XLAT/XLONG（经纬度）转到 VTK 坐标系后再插值。
垂直：位势高度 (PH+PHB)/g 减地形 HGT，得到距地高度，再对到 VTK 的 z。
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import numpy as np
import pyvista as pv
from pyproj import CRS, Transformer

from stfusion.models import TargetGrid, VtkSource, WrfSource

logger = logging.getLogger(__name__)

GRAVITY = 9.81
WRF_SUFFIXES = {".nc", ".nc4", ".cdf"}


class WrfError(ValueError):
    pass


def _netcdf_uri(path: Path, var: str) -> str:
    return f"NETCDF:{path.resolve()}:{var}"


def wrf_lonlat_envelope(path: Path) -> tuple[float, float, float, float]:
    """wrfout 经纬度包络：(west, south, east, north)。"""
    xlat = _squeeze_xy(_read_var(path, "XLAT"))
    xlong = _squeeze_xy(_read_var(path, "XLONG"))
    return (
        float(np.nanmin(xlong)),
        float(np.nanmin(xlat)),
        float(np.nanmax(xlong)),
        float(np.nanmax(xlat)),
    )


def looks_like_wrf(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith("wrfout") or "wrfout" in name:
        return True
    if path.suffix.lower() in WRF_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            magic = handle.read(8)
    except OSError:
        return False
    if not magic.startswith(b"\x89HDF"):
        return False
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(_netcdf_uri(path, "QVAPOR")) as ds:
                return ds.count >= 1
    except Exception:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
                with rasterio.open(_netcdf_uri(path, "Q2")) as ds:
                    return ds.count >= 1
        except Exception:
            return False


def _read_var(path: Path, name: str) -> np.ndarray:
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    uri = _netcdf_uri(path, name)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(uri) as ds:
                if ds.count == 1:
                    return np.asarray(ds.read(1), dtype=np.float64)
                return np.asarray(ds.read(), dtype=np.float64)
    except Exception as exc:
        raise WrfError(f"无法读取 wrfout 变量 {name}: {exc}") from exc


def _squeeze_xy(arr: np.ndarray) -> np.ndarray:
    """去掉多余的时间维，保留 (y, x) 或 (z, y, x)。"""
    while arr.ndim > 3:
        arr = arr[0]
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    return arr


def _read_dx(path: Path) -> float:
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(path) as ds:
                tags = ds.tags() or {}
    except Exception:
        return 1000.0
    for key, value in tags.items():
        if str(key).upper().endswith("DX"):
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                continue
    return 1000.0


def read_wrf_fields(path: Path, var: str = "QVAPOR") -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None, str]:
    xlat = _squeeze_xy(_read_var(path, "XLAT"))
    xlong = _squeeze_xy(_read_var(path, "XLONG"))
    if xlat.ndim != 2 or xlong.ndim != 2:
        raise WrfError(f"XLAT/XLONG 应为二维，实际 {xlat.shape} / {xlong.shape}")

    used = var
    try:
        q = _squeeze_xy(_read_var(path, var))
    except WrfError:
        if var.upper() != "Q2":
            q = _squeeze_xy(_read_var(path, "Q2"))
            used = "Q2"
            logger.warning("wrfout 中没有 %s，改用 Q2", var)
        else:
            raise

    z_agl: np.ndarray | None = None
    if q.ndim == 3:
        try:
            ph = _squeeze_xy(_read_var(path, "PH"))
            phb = _squeeze_xy(_read_var(path, "PHB"))
            hgt = _squeeze_xy(_read_var(path, "HGT"))
            if hgt.ndim != 2:
                raise WrfError(f"HGT 应为二维地形，实际 {hgt.shape}")
            z_full = (ph + phb) / GRAVITY
            if z_full.shape[0] == q.shape[0] + 1:
                z_mass = 0.5 * (z_full[:-1] + z_full[1:])
            elif z_full.shape[0] == q.shape[0]:
                z_mass = z_full
            else:
                raise WrfError(
                    f"PH 层数 {z_full.shape[0]} 与 {used} 层数 {q.shape[0]} 对不上"
                )
            if z_mass.shape[1:] != q.shape[1:] or hgt.shape != q.shape[1:]:
                raise WrfError("PH/HGT 水平网格与湿度不一致")
            z_agl = z_mass - hgt
        except WrfError:
            logger.warning("无法计算距地高度，改用 %s 最低层并沿 Z 复制", used)
            q = q[0]
            z_agl = None
    elif q.ndim != 2:
        raise WrfError(f"{used} 维数异常: {q.shape}")

    if q.shape[-2:] != xlat.shape:
        raise WrfError(f"{used} 水平形状 {q.shape} 与 XLAT {xlat.shape} 不一致")
    return xlat, xlong, q, z_agl, used


def lonlat_to_crs(xlong: np.ndarray, xlat: np.ndarray, crs: CRS) -> tuple[np.ndarray, np.ndarray]:
    """把 WRF 经纬度转到气象 VTK 所用的平面坐标（米或度）。"""
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = transformer.transform(
        np.asarray(xlong, dtype=np.float64),
        np.asarray(xlat, dtype=np.float64),
    )
    return np.asarray(x), np.asarray(y)


def interp_along_z(z_agl: np.ndarray, q: np.ndarray, z_out: np.ndarray) -> np.ndarray:
    """每个 WRF 柱上把湿度插到 VTK 的 z（距地）。"""
    nz, ny, nx = q.shape
    z_out = np.asarray(z_out, dtype=np.float64)
    out = np.empty((z_out.size, ny, nx), dtype=np.float64)
    z_agl = np.ascontiguousarray(z_agl, dtype=np.float64)
    q = np.ascontiguousarray(q, dtype=np.float64)
    for j in range(ny):
        for i in range(nx):
            zp = z_agl[:, j, i]
            qp = q[:, j, i]
            if zp[-1] < zp[0]:
                zp = zp[::-1]
                qp = qp[::-1]
            out[:, j, i] = np.interp(z_out, zp, qp, left=qp[0], right=qp[-1])
    return out


def _typical_spacing(wx: np.ndarray, wy: np.ndarray) -> float:
    if wx.shape[1] > 1:
        d = np.hypot(np.diff(wx, axis=1), np.diff(wy, axis=1))
        med = float(np.nanmedian(d))
        if med > 0:
            return med
    if wx.shape[0] > 1:
        d = np.hypot(np.diff(wx, axis=0), np.diff(wy, axis=0))
        med = float(np.nanmedian(d))
        if med > 0:
            return med
    return 1.0


def _interp_xy_layer(
    wx: np.ndarray,
    wy: np.ndarray,
    values: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    tri=None,
) -> tuple[np.ndarray, object]:
    """把 WRF 一层水平场插到 VTK 的 XY。"""
    xx, yy = np.meshgrid(xs, ys, indexing="ij")
    vals = np.asarray(values, dtype=np.float64).ravel()
    try:
        from matplotlib.tri import LinearTriInterpolator, Triangulation

        if tri is None:
            tri = Triangulation(wx.ravel(), wy.ravel())
        interpolator = LinearTriInterpolator(tri, vals)
        sampled = interpolator(xx, yy)
        layer = np.ma.filled(np.ma.asarray(sampled), np.nan).astype(np.float32)
        return layer, tri
    except Exception as exc:
        logger.warning("三角网插值不可用（%s），改用高斯核", exc)
        return _interp_xy_pyvista(wx, wy, values, xs, ys), tri


def _interp_xy_pyvista(
    wx: np.ndarray,
    wy: np.ndarray,
    values: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
) -> np.ndarray:
    radius = _typical_spacing(wx, wy) * 2.5
    src = pv.PolyData(
        np.column_stack([wx.ravel(), wy.ravel(), np.zeros(wx.size, dtype=np.float64)])
    )
    src["v"] = np.ascontiguousarray(values.ravel(), dtype=np.float32)
    xx, yy = np.meshgrid(xs, ys, indexing="ij")
    tgt = pv.PolyData(
        np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size, dtype=np.float64)])
    )
    sampled = tgt.interpolate(
        src,
        radius=float(radius),
        sharpness=2.0,
        strategy="null_value",
        null_value=float("nan"),
        pass_cell_data=False,
    )
    return np.asarray(sampled["v"], dtype=np.float32).reshape(xx.shape)


def humidity_volume_from_arrays(
    xlat: np.ndarray,
    xlong: np.ndarray,
    q: np.ndarray,
    z_agl: np.ndarray | None,
    grid: TargetGrid,
    vtk_crs: CRS,
) -> np.ndarray:
    """采到 ImageData 点序，形状 (nx, ny, nz)。"""
    wx, wy = lonlat_to_crs(xlong, xlat, vtk_crs)
    xs, ys, zs = grid.xs(), grid.ys(), grid.zs()
    nx, ny, nz = grid.nx, grid.ny, grid.nz
    volume = np.empty((nx, ny, nz), dtype=np.float32)

    if q.ndim == 2:
        layer, _ = _interp_xy_layer(wx, wy, q, xs, ys)
        for k in range(nz):
            volume[:, :, k] = layer
        return volume

    if z_agl is None:
        raise WrfError("三维湿度缺少距地高度")
    qz = interp_along_z(z_agl, q, zs)
    tri = None
    for k in range(nz):
        volume[:, :, k], tri = _interp_xy_layer(wx, wy, qz[k], xs, ys, tri=tri)
    return volume


def sample_wrf_to_grid(source: WrfSource, grid: TargetGrid, vtk_crs: CRS) -> np.ndarray:
    xlat, xlong, q, z_agl, used = read_wrf_fields(source.path, source.wrf_var)
    if used != source.wrf_var:
        logger.info("请求变量 %s，实际使用 %s", source.wrf_var, used)
    volume = humidity_volume_from_arrays(xlat, xlong, q, z_agl, grid, vtk_crs)
    finite = np.isfinite(volume)
    logger.info(
        "湿度 %s -> 网格 %s，有效点 %s/%s",
        used,
        volume.shape,
        int(finite.sum()),
        finite.size,
    )
    if not np.any(finite):
        raise WrfError("湿度插值后全是空值，请检查 wrfout 与 VTK 是否空间重叠、CRS 是否正确")
    return volume


def overlay_wrf_humidity(mesh: pv.DataSet, vtk: VtkSource, wrfs: list[WrfSource]) -> pv.DataSet:
    if not wrfs:
        return mesh
    if not isinstance(mesh, pv.ImageData):
        raise WrfError("WRF 湿度叠加目前只支持 ImageData 气象 VTK")

    from stfusion.resample import grid_from_image

    grid = grid_from_image(mesh, vtk.crs)
    used = set(mesh.point_data.keys()) | set(mesh.cell_data.keys())
    n = grid.nx * grid.ny * grid.nz
    for src in wrfs:
        name = src.field if src.field not in used else f"{src.name}_{src.field}"
        volume = sample_wrf_to_grid(src, grid, vtk.crs)
        mesh.point_data[name] = np.ascontiguousarray(volume.reshape(n, order="F"))
        used.add(name)
    return mesh


def inspect_wrf(path: Path) -> dict:
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    used = "QVAPOR"
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(_netcdf_uri(path, "QVAPOR")) as ds:
                bottom_top = int(ds.count)
                south_north = int(ds.height)
                west_east = int(ds.width)
    except Exception:
        used = "Q2"
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(_netcdf_uri(path, "Q2")) as ds:
                bottom_top = int(ds.count)
                south_north = int(ds.height)
                west_east = int(ds.width)
    xlat = _squeeze_xy(_read_var(path, "XLAT"))
    xlong = _squeeze_xy(_read_var(path, "XLONG"))
    has_height = False
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(_netcdf_uri(path, "PH")) as ds:
                has_height = ds.count >= 2
    except Exception:
        has_height = False
    info = {
        "kind": "wrf",
        "type": "WRF wrfout",
        "name": path.name,
        "var": used,
        "west_east": west_east,
        "south_north": south_north,
        "bottom_top": bottom_top,
        "lat": [float(np.nanmin(xlat)), float(np.nanmax(xlat))],
        "lon": [float(np.nanmin(xlong)), float(np.nanmax(xlong))],
        "dx": _read_dx(path),
        "has_height": has_height,
    }
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
            with rasterio.open(path) as ds:
                tags = ds.tags() or {}
        for key in ("CEN_LAT", "CEN_LON", "MAP_PROJ", "TITLE"):
            for tk, tv in tags.items():
                if str(tk).upper().endswith(key):
                    info[key.lower()] = tv
                    break
    except Exception:
        pass
    return info
