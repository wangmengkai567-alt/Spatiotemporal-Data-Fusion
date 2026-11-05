from __future__ import annotations

from pyproj import CRS

DEFAULT_VTK_CRS = "EPSG:32649"


def looks_projected(xmin: float, xmax: float, ymin: float, ymax: float) -> bool:
    return max(abs(xmin), abs(xmax)) > 180.5 or max(abs(ymin), abs(ymax)) > 90.5


def looks_geographic(xmin: float, xmax: float, ymin: float, ymax: float) -> bool:
    return not looks_projected(xmin, xmax, ymin, ymax)


def is_geographic_crs(code: str) -> bool:
    try:
        return bool(CRS.from_user_input(code).is_geographic)
    except Exception:
        return False


def crs_equal(a, b) -> bool:
    """判断两个坐标系是否实质相同"""
    try:
        ca = a if isinstance(a, CRS) else CRS.from_user_input(a)
        cb = b if isinstance(b, CRS) else CRS.from_user_input(b)
        if ca == cb:
            return True
        ea, eb = ca.to_epsg(), cb.to_epsg()
        if ea and eb:
            return ea == eb
        return ca.equals(cb)
    except Exception:
        return str(a) == str(b)


def _as_epsg(crs: CRS | str) -> str:
    obj = crs if isinstance(crs, CRS) else CRS.from_user_input(crs)
    epsg = obj.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    return obj.to_string()


def resolve_fusion_crs(config) -> None:
    """气象 VTK 固定为 UTM 49N（EPSG:32649）；人口 GeoTIFF 用文件自带 CRS。

    不再根据重叠面积反推 VTK 坐标系。留空、auto，或误填经纬度 CRS 时都用 32649。
    输出网格与坐标系始终跟随气象 VTK。
    """
    requested = (config.vtk_crs or "").strip()
    if requested.lower() in {"", "auto"} or is_geographic_crs(requested):
        config.vtk_crs = DEFAULT_VTK_CRS
    else:
        config.vtk_crs = requested
    config.target_crs = config.vtk_crs
    config.vtk_crs = _as_epsg(config.vtk_crs)
    config.target_crs = _as_epsg(config.target_crs)
