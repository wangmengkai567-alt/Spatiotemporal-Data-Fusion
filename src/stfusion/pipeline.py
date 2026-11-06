from __future__ import annotations

import logging
from pathlib import Path

from pyproj import CRS

from stfusion.crs import resolve_fusion_crs
from stfusion.io import load_source
from stfusion.models import BuildingSource, FusionConfig, GeoTiffSource, TerrainSource, TurbulenceSource, VtkSource, WrfSource
from stfusion.resample import overlay_geotiffs_on_vtk
from stfusion.building import sample_buildings
from stfusion.terrain import sample_terrain
from stfusion.surface import add_surface, surface_grid
from stfusion.turbulence import overlay_turbulence
from stfusion.wrf import overlay_wrf_humidity

logger = logging.getLogger(__name__)


class FusionError(ValueError):
    pass


def fuse_to_vtk(config: FusionConfig) -> Path:
    resolve_fusion_crs(config)
    config.target_crs = config.vtk_crs
    vtk_crs = CRS.from_user_input(config.vtk_crs)
    logger.info("保持气象 VTK 网格，坐标系 %s", config.vtk_crs)

    sources = [load_source(spec, vtk_crs, vtk_crs) for spec in config.sources]
    vtk_sources = [s for s in sources if isinstance(s, VtkSource)]
    geotiffs = [s for s in sources if isinstance(s, GeoTiffSource)]
    wrfs = [s for s in sources if isinstance(s, WrfSource)]
    turbs = [s for s in sources if isinstance(s, TurbulenceSource)]
    if not vtk_sources:
        raise FusionError("需要至少一个 VTK 数据源")
    terrains = [s for s in sources if isinstance(s, TerrainSource)]
    buildings = [s for s in sources if isinstance(s, BuildingSource)]
    if not geotiffs and not wrfs and not turbs and not terrains and not buildings:
        raise FusionError("需要至少一个人口、湿度、湍流、地形或建筑数据源")
    if len(terrains) > 1 or len(buildings) > 1:
        raise FusionError("每次只能选择一个地形 ZIP 和一个建筑 ZIP")

    vtk0 = vtk_sources[0]
    grid = surface_grid(vtk0.mesh, vtk0.crs) if terrains or buildings else None
    reserved = {"terrain", "building_height"}
    if terrains or buildings:
        for src in geotiffs + wrfs:
            if src.field in reserved:
                raise FusionError(f"{src.field} 是地形或建筑的固定字段名")
    mesh = overlay_geotiffs_on_vtk(vtk0, geotiffs)
    mesh = overlay_wrf_humidity(mesh, vtk0, wrfs)
    mesh = overlay_turbulence(mesh, vtk0, turbs)
    if terrains:
        add_surface(mesh, grid, "terrain", sample_terrain(terrains[0], grid))
    if buildings:
        add_surface(mesh, grid, "building_height", sample_buildings(buildings[0], grid))
    logger.info(
        "输出网格 dimensions=%s origin=%s spacing=%s fields=%s",
        getattr(mesh, "dimensions", None),
        getattr(mesh, "origin", None),
        getattr(mesh, "spacing", None),
        list(mesh.point_data.keys()),
    )

    output = config.output
    output.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(str(output))
    logger.info("已写出 %s", output)
    return output
