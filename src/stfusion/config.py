from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from stfusion.models import FusionConfig, SourceSpec


class ConfigError(ValueError):
    pass


def _as_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def load_config(path: Path) -> FusionConfig:
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("配置文件必须是 YAML 映射（字典）")

    base = path.parent
    crs = raw.get("crs") or {}
    grid = raw.get("grid") or {}
    output = raw.get("output") or {}
    sources_raw = raw.get("sources")

    if not sources_raw:
        raise ConfigError("sources 不能为空")

    target_crs = str(crs.get("target") or "EPSG:32649")
    vtk_crs = str(crs.get("vtk") or "EPSG:32649")
    extent = str(grid.get("extent") or "intersection")
    if extent != "intersection":
        raise ConfigError("目前仅支持 grid.extent: intersection")

    out_path = output.get("path")
    if not out_path:
        raise ConfigError("output.path 不能为空")

    sources = [_parse_source(item, base) for item in sources_raw]
    kinds = {s.kind for s in sources}
    if "vtk" not in kinds:
        raise ConfigError("至少需要一个 type: vtk 的气象数据源，以提供三维网格和高度层")

    return FusionConfig(
        target_crs=target_crs,
        vtk_crs=vtk_crs,
        extent="intersection",
        sources=sources,
        output=_as_path(str(out_path), base),
        config_dir=base,
    )


def _parse_source(item: Any, base: Path) -> SourceSpec:
    if not isinstance(item, dict):
        raise ConfigError("每个 source 必须是映射")
    name = item.get("name")
    kind = item.get("type") or item.get("kind")
    path = item.get("path")
    if not name or not kind or not path:
        raise ConfigError("source 需要 name、type、path")
    kind = str(kind).lower()
    if kind not in {"vtk", "geotiff", "wrf", "turbulence", "terrain", "building"}:
        raise ConfigError(f"不支持的 source type: {kind}（vtk | geotiff | wrf | turbulence | terrain | building）")

    resampling = str(item.get("resampling") or ("nearest" if kind == "turbulence" else "bilinear")).lower()
    if kind == "geotiff" and resampling not in {"bilinear", "nearest"}:
        raise ConfigError("resampling 只能是 bilinear 或 nearest")
    if kind == "turbulence" and resampling not in {"nearest", "trilinear", "bilinear"}:
        raise ConfigError("湍流 resampling 只能是 nearest 或 trilinear")

    arrays = item.get("arrays")
    if arrays is not None:
        arrays = [str(a) for a in arrays]

    field = item.get("field")
    if kind == "geotiff" and not field:
        field = Path(str(path)).stem
    wrf_var = str(item.get("var") or item.get("wrf_var") or "QVAPOR")
    if kind == "wrf" and not field:
        field = wrf_var
    if kind == "turbulence" and arrays is None and item.get("fields"):
        arrays = [str(a) for a in item["fields"]]

    return SourceSpec(
        name=str(name),
        kind=kind,  # type: ignore[arg-type]
        path=_as_path(str(path), base),
        field=str(field) if field else None,
        band=int(item.get("band") or 1),
        arrays=arrays,
        resampling=resampling,
        crs=str(item["crs"]) if item.get("crs") else None,
        wrf_var=wrf_var,
    )


def config_from_cli(
    vtk: Path,
    geotiffs: list[tuple[Path, str]],
    output: Path,
    target_crs: str,
    vtk_crs: str,
    resampling: str = "bilinear",
    wrf: Path | None = None,
    wrf_field: str | None = None,
    wrf_var: str = "QVAPOR",
    turbulence: Path | None = None,
    turbulence_fields: list[str] | None = None,
    turbulence_resampling: str = "nearest",
    terrain: Path | None = None,
    building: Path | None = None,
) -> FusionConfig:
    sources = [
        SourceSpec(name="meteorology", kind="vtk", path=vtk.resolve()),
    ]
    for i, (path, field) in enumerate(geotiffs):
        sources.append(
            SourceSpec(
                name=field if field != "population" or i == 0 else f"{field}_{i}",
                kind="geotiff",
                path=path.resolve(),
                field=field,
                resampling=resampling,
            )
        )
    if wrf is not None:
        used_var = wrf_var or "QVAPOR"
        used_field = wrf_field or used_var
        sources.append(
            SourceSpec(
                name=used_field,
                kind="wrf",
                path=wrf.resolve(),
                field=used_field,
                wrf_var=used_var,
            )
        )
    if turbulence is not None:
        turb_resampling = (turbulence_resampling or "nearest").lower()
        if turb_resampling not in {"nearest", "trilinear", "bilinear"}:
            raise ConfigError("湍流 resampling 只能是 nearest 或 trilinear")
        sources.append(
            SourceSpec(
                name="turbulence",
                kind="turbulence",
                path=turbulence.resolve(),
                arrays=list(turbulence_fields) if turbulence_fields else None,
                resampling=turb_resampling,
            )
        )
    for kind, path in (("terrain", terrain), ("building", building)):
        if path is not None:
            sources.append(SourceSpec(name=kind, kind=kind, path=path.resolve()))
    return FusionConfig(
        target_crs=target_crs,
        vtk_crs=vtk_crs,
        extent="intersection",
        sources=sources,
        output=output.resolve(),
        config_dir=Path.cwd(),
    )
