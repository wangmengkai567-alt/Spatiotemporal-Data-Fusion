# 时空数据融合（VTK + 人口 + 湿度 + 湍流 + 地形 + 建筑）

把三维气象 VTK 当目标网格，把人口 GeoTIFF、WRF `wrfout` 湿度、风切变/湍流切片、地形和建筑高度采到同一套网格上，输出新的 VTK。

前端不是单独的 npm 项目。页面在 `src/stfusion/web/static/`，由同一个 Python 服务一起启动。

## 启动前端（网页）

第一次（只需做一次）：

```powershell
cd "D:\Drone\Spatiotemporal Data Fusion"
uv sync
```

`uv sync` 会创建项目虚拟环境并安装依赖，改源码后可直接重新启动。

以后每次启动：

```powershell
cd "D:\Drone\Spatiotemporal Data Fusion"
uv run python -B -m stfusion
```

终端出现监听提示后，浏览器打开：

**http://127.0.0.1:3006/**

这就是前端页面。选气象 VTK，可选人口 GeoTIFF、WRF `wrfout`、风切变/湍流 VTK、`terrain.zip` 和 `building.zip`，点「开始融合」。结果在项目 `output/` 目录。

风切变/湍流是多块 4m 切片：第 4 步可一次多选、选整个文件夹，或拖入文件夹。

湍流切片每块约 1GB，但 64m 气象网格只用到其中极少的格点。网页不会上传整个切片：浏览器先在本地读取各切片的 VTK 头，向服务端要到"需要哪些字节"，再只截取这些字节上传（安阳 7 块共 7.5GB，实际上传约 30MB，融合几秒完成），结果与上传完整切片逐位一致。气象 VTK 不是规则网格（ImageData）或切片格式不支持时，自动退回上传完整切片。

改端口：

```powershell
uv run python -B -m stfusion --host 0.0.0.0 --port 9000
```

装好后也可以直接用命令 `stfusion` 启动同一个服务。

## 当前规则

- **叠加方式**：在原始气象 VTK 上加入人口、湿度、风切变和湍流等属性，**不改网格、不拆矢量场**。
- **网格**：输出与气象 VTK 完全相同（Dimensions / Spacing / Origin）。
- **人口**：重投影到气象坐标系后按 XY 取样，再沿 Z 复制到每一层。
- **湿度**：读取 WRF 的 `QVAPOR`（三维水汽混合比），写入 VTK 时字段名仍为 `QVAPOR`。用 `XLAT`/`XLONG` 转到 VTK 的平面坐标，用 `(PH+PHB)/g − HGT` 得到距地高度，再插值到气象网格的每一层；**不沿 Z 复制**。如果无法读取垂直高度，则使用最低层并沿 Z 复制。
- **风切变 / 湍流**：读取 4m 切片 VTK（`HorizontalShear`、`VerticalShear`、`Epsilon`，以及可选 `AirMask`），按坐标抽样到气象网格。与安阳 64m 气象场原点对齐、分辨率恰好 16:1 时取共点；切片按 1 个格点重叠拼接；没有切片覆盖的格点为 NaN。
- **气象场**：原样保留（例如 `velocity` 仍是一个三分量矢量）。人口、湿度和湍流的新字段与已有字段重名时，会自动加上数据源名称作为前缀；地形和建筑按下述固定字段规则更新。
- **坐标系**：气象 VTK 固定为 **EPSG:32649（WGS 84 / UTM 49N）**。人口 GeoTIFF 用文件自带 CRS（安阳人口为 EPSG:4326）。湿度 `XLAT`/`XLONG` 按经纬度转到 UTM 49N。输出坐标与原始气象 VTK 一致。
- **地形**：上传包含 `layer.json` 的 quantized-mesh ZIP，按最高声明层级原三角网采样，写入 `terrain`，沿 Z 复制。无覆盖位置为 NaN。
- **建筑**：上传包含 `tileset.json` 和 B3DM 的 3D Tiles 1.0 ZIP，读取建筑 `Height`，写入 `building_height`，沿 Z 复制。目标点周围一个网格宽度范围内有建筑投影即计入，多栋取最大高度；数据完整性未确认的空白区域为 NaN。
- **新字段更新**：上传地形或建筑时更新对应同名点字段，未上传的字段保留。两个字段类型为 float32，单位米；不修改 AirMask，也不按高度清空建筑字段。
- **新增来源网格要求**：建筑和地形需要轴对齐、正间距的 ImageData，输出分辨率始终跟随气象 VTK。

## HTTP API

本机基地址为 `http://127.0.0.1:3006`。经反向代理部署到 `/fuse/` 时，将示例地址替换为 `https://你的域名/fuse/api/...`。以下示例使用 PowerShell 的 `curl.exe`；在 Linux/macOS 中将命令名换为 `curl` 即可。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 检查融合依赖 |
| POST | `/api/inspect` | 读取单个数据文件的元信息 |
| POST | `/api/turbulence/plan` | 规划湍流切片的子集上传字节范围 |
| POST | `/api/fuse` | 上传数据并生成融合 VTK |
| GET | `/api/outputs` | 查询已生成的结果 |
| GET | `/api/outputs/{name}` | 下载结果 VTK |
| DELETE | `/api/outputs/{name}` | 删除结果及对应元数据 |

跨域调用当前仅允许来源 `https://spatiotemporal.cn`；同源页面使用相对路径调用即可。

上传接口使用 `multipart/form-data`，不要手动设置 `Content-Type`，让客户端生成 boundary。湍流规划接口使用 `application/json`。文件用 `@路径` 上传，普通表单值不用 `@`。网页入口为 `GET /`，静态资源位于 `/static/`。

### `GET /api/health`

无需参数：

```powershell
curl.exe http://127.0.0.1:3006/api/health
```

返回 `200`：

```json
{"ready": true, "missing": []}
```

`ready` 表示 `numpy`、`pyvista`、`rasterio`、`pyproj` 是否可导入，`missing` 是缺失包名数组。缺依赖时该接口仍返回 `200`，但 `ready=false`；它不表示某次融合是否完成。

### `POST /api/inspect`

| 表单字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | 文件 | 是 | 单个 VTK、GeoTIFF 或 WRF wrfout 文件 |

```powershell
curl.exe -F "file=@data/weather.vtk" http://127.0.0.1:3006/api/inspect
```

成功返回 `200`，JSON 内容由文件类型决定：

| 类型 | 主要返回字段 |
| --- | --- |
| VTK | `kind`、`type`、`name`、`points`、`cells`、`bounds`、`fields`、`looks_projected`；ImageData 另含 `dimensions`、`origin`、`spacing` |
| GeoTIFF | `kind=geotiff`、`type`、`name`、`crs`、`width`、`height`、`bands`、`dtype`、`bounds`、`res`、`nodata` |
| WRF | `kind=wrf`、`type`、`name`、`var`、`west_east`、`south_north`、`bottom_top`、`lat`、`lon`、`dx`、`has_height` |
| 湍流 VTK | 传统 STRUCTURED_POINTS 文件头识别到标准湍流字段时，`kind=turbulence`，附带 `tiles=1`，以及网格、范围和字段信息 |

VTK 的 `bounds` 顺序为 `[xmin, xmax, ymin, ymax, zmin, zmax]`；GeoTIFF 为 `[left, bottom, right, top]`。VTK 的 `fields` 是字段名数组，`dimensions`、`origin`、`spacing` 都是三个分量。

本接口不接收地形或建筑 ZIP；这两个 ZIP 直接提交给 `/api/fuse`。预览接口会上传整个文件，不用于湍流子集规划时读取原始二进制字段偏移。

### `POST /api/fuse`

上传一个气象 VTK，并至少提供人口、WRF、湍流、地形、建筑中的一类。接口在融合完成后返回 JSON，**不会直接返回 VTK 文件，也不会自动下载**。

#### 文件字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `vtk` | 单个文件 | 是 | 气象 VTK，决定输出网格与原有属性 |
| `geotiff` | 文件，可重复 | 否 | 一个或多个人口/其他 GeoTIFF 栅格，以同名字段重复提交 |
| `wrf` | 单个文件 | 否 | WRF 湿度来源，支持 wrfout 文件名及 NetCDF 文件 |
| `turbulence` | 文件，可重复 | 否 | 完整湍流 VTK 切片，与子集上传方式二选一 |
| `terrain` | 单个 ZIP 文件 | 否 | 包含唯一 `layer.json` 的 quantized-mesh 地形包 |
| `building` | 单个 ZIP 文件 | 否 | 包含唯一 `tileset.json` 和 B3DM 的 3D Tiles 1.0 建筑包 |
| `turbulence_subset` | 文件，可重复 | 否 | 按规划字节区间拼接的切片子集，必须配合 `turbulence_manifest` |

#### 普通表单字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `geotiff_field` | 字符串，可重复 | 第一份 `population`，其余文件名主干 | 与 `geotiff` 按提交顺序对应；上传多个栅格时建议逐个提供字段名 |
| `wrf_var` | 字符串 | `QVAPOR` | 读取的 WRF 变量；当前实现无法读取该变量时会尝试 `Q2` |
| `wrf_field` | 字符串 | `wrf_var` | 写入 VTK 的字段名，不是读取变量名 |
| `vtk_crs` | 字符串 | `EPSG:32649` | 气象坐标系，本项目安阳数据使用 UTM 49N |
| `target_crs` | 字符串 | `EPSG:32649` | 当前实现最终使用 `vtk_crs`，输出不会据此更换网格或坐标系 |
| `resampling` | 字符串 | `bilinear` | GeoTIFF 采样方式：`bilinear` 或 `nearest`；不控制地形、建筑或完整湍流切片的采样方式 |
| `turbulence_manifest` | JSON 字符串 | 空 | `/api/turbulence/plan` 返回的 `manifest`，作为普通表单文本提交 |

输出分辨率始终跟随气象 VTK，没有可选的分辨率策略参数。完整湍流切片使用 nearest；子集上传使用规划清单中的采样方式。

#### 调用示例

气象加人口：

```powershell
curl.exe -F "vtk=@data/weather.vtk" -F "geotiff=@data/population.tif" -F "geotiff_field=population" http://127.0.0.1:3006/api/fuse
```

气象加地形、建筑；也可以只保留其中一个 ZIP：

```powershell
curl.exe -F "vtk=@data/weather.vtk" -F "terrain=@terrain.zip" -F "building=@building.zip" http://127.0.0.1:3006/api/fuse
```

全部来源一起提交；实际文件路径按本机数据替换，湍流切片可继续重复添加：

```powershell
curl.exe -F "vtk=@data/weather.vtk" -F "geotiff=@data/population.tif" -F "geotiff_field=population" -F "wrf=@data/wrfout_d03" -F "wrf_var=QVAPOR" -F "turbulence=@data/tile_01.vtk" -F "turbulence=@data/tile_02.vtk" -F "terrain=@terrain.zip" -F "building=@building.zip" http://127.0.0.1:3006/api/fuse
```

#### 成功响应

返回 `200`，结构为 `{"item": {...}}`，示例：

```json
{
  "item": {
    "name": "weather_fused.vtk",
    "size": 402602869,
    "size_text": "383.95 MB",
    "mtime": "2026/11/5 22:45:19",
    "fields": ["velocity", "population", "QVAPOR", "HorizontalShear", "VerticalShear", "Epsilon", "AirMask", "terrain", "building_height"],
    "source": "weather.vtk",
    "vtk_crs": "EPSG:32649",
    "turbulence_tiles": 2
  }
}
```

| 返回字段 | 类型 | 含义 |
| --- | --- | --- |
| `name` | 字符串 | 结果文件名，后续下载或删除时使用 |
| `size` | 整数 | 文件字节数 |
| `size_text` | 字符串 | 便于显示的文件大小 |
| `mtime` | 字符串 | 文件修改时间，使用服务器本地时间 |
| `fields` | 字符串数组 | 结果字段名；内容随输入和已选择来源变化 |
| `source` | 字符串 | 输入气象 VTK 的文件名 |
| `vtk_crs` | 字符串 | 融合使用的气象坐标系 |
| `turbulence_tiles` | 整数 | 本次提交的湍流切片数量，无湍流为 0 |

结果保存在项目 `output/` 下，并生成同名 JSON 元数据。文件名由输入气象文件名清理后加 `_fused.vtk`，例如 `weather.vti` 对应 `weather_fused.vtk`；同名结果会覆盖。客户端应使用响应中的 `item.name`，不自行猜测文件名。

收到 `200` 且存在 `item.name` 表示结果已生成；页面会提示成功，并在结果列表提供下载。结果列表刷新失败不会改变已经确认的融合成功状态。网关错误、网络中断或无效的完成响应会提示“未收到融合完成确认”，可点击“刷新结果”查询，不能据此断言融合失败。当前融合没有任务 ID、后台任务查询或进度接口。

地形和建筑需要轴对齐、正间距的 ImageData。它们分别写入固定字段 `terrain`、`building_height`，沿 Z 复制；上传对应来源会更新已有同名点字段。与这两类来源一起提交时，GeoTIFF 和 WRF 的字段名不能使用这两个名称；同名单元字段也会报冲突。

### `POST /api/turbulence/plan`

该接口只计算切片需要上传的字节区间，不上传文件，也不执行融合。调用方需先读取原始 **BINARY STRUCTURED_POINTS** 切片头；所用场应是单分量 `float` 或 `double`。文件名应唯一。

| JSON 字段 | 类型 | 说明 |
| --- | --- | --- |
| `grid` | 对象 | 气象 ImageData 的 `origin`、`spacing`、`dimensions`，各三个分量；维度是点数 |
| `tiles` | 对象数组 | 非空；每项包含 `name`、`header`，建议同时提供原始文件字节数 `size` |
| `resampling` | 字符串 | 默认 `nearest`，需要三线性插值时使用 `trilinear` |

`header` 包含 `dimensions`、`origin`、`spacing`、`n_points`、`fields`。每个场描述包含 `name`、`ncomp`、`nvals`、`dtype`、`offset`；`offset` 是该场二进制数据在原文件中的起始字节偏移，而非数组下标。头结构对应 `stfusion.vtk_legacy.parse_structured_points()` 的结果，不是 `/api/inspect` 返回的字段名数组。规划端按本项目 EPSG:32649 网格解释坐标，没有独立的 CRS 请求参数。

简化的请求示例（下述尺寸、偏移和文件大小必须替换为真实切片头）：

```json
{
  "grid": {"origin": [800100, 3991320, 0], "spacing": [4, 4, 4], "dimensions": [2, 2, 2]},
  "resampling": "nearest",
  "tiles": [
    {
      "name": "tile.vtk",
      "size": 232,
      "header": {
        "dimensions": [2, 2, 2], "origin": [800100, 3991320, 0], "spacing": [4, 4, 4],
        "n_points": 8,
        "fields": [{"name": "HorizontalShear", "ncomp": 1, "nvals": 8, "dtype": "float", "offset": 200}]
      }
    }
  ]
}
```

将请求保存为 `plan-request.json` 后提交：

```powershell
curl.exe -H "Content-Type: application/json" --data-binary "@plan-request.json" http://127.0.0.1:3006/api/turbulence/plan
```

成功返回 `200`：

| 返回字段 | 说明 |
| --- | --- |
| `manifest` | 含 `version`、`resampling`、`tiles`，每个切片含原头与子集索引；融合时原样交回 |
| `transfers` | 每项含 `name`、`ranges`、`nbytes`；`ranges` 为 `[起始字节, 字节长度]` 数组 |
| `total_bytes` | 全部切片子集需要上传的总字节数 |

上述简化示例的传输项为 `{"name":"tile.vtk","ranges":[[200,32]],"nbytes":32}`，`total_bytes=32`。对真实切片，按返回的 `ranges` **原顺序**截取原始字节并拼接，不增加 VTK 头、不转换字节序。`ranges` 为空的切片不用上传子集文件，但保留在清单中。

将返回的 `manifest` 对象单独保存为 `manifest.json`，将某切片拼接后的字节保存为 `tile-subset.bin`，随后提交：

```powershell
curl.exe -F "vtk=@data/weather.vtk" -F "turbulence_manifest=<manifest.json" -F "turbulence_subset=@tile-subset.bin;filename=tile.vtk" http://127.0.0.1:3006/api/fuse
```

`turbulence_manifest=<manifest.json` 将文件内容作为普通文本表单字段发送，不能改成 `@manifest.json` 文件上传。`filename=tile.vtk` 必须与清单中的切片名一致；多块子集以同一个 `turbulence_subset` 字段重复提交。不要同时上传完整 `turbulence`；有非空子集索引的切片必须提供相应数据文件，且字节数必须与规划一致。规划和融合须使用同一个气象网格。

### `GET /api/outputs`

无需参数，按修改时间倒序返回结果：

```powershell
curl.exe http://127.0.0.1:3006/api/outputs
```

返回 `200`，结构为 `{"items": [...]}`。每项与 `/api/fuse` 的 `item` 结构一致；没有结果时返回 `{"items": []}`。列表字段信息来自同名 JSON 元数据；元数据缺失时可能为空，不代表 VTK 中没有字段。

### `GET /api/outputs/{name}`

`name` 为融合响应或结果列表中的文件名。含中文、空格等字符时，应对路径中的文件名进行 URL 编码。

```powershell
curl.exe --fail --output "weather_fused.vtk" http://127.0.0.1:3006/api/outputs/weather_fused.vtk
```

成功返回 `200`，响应体是 VTK 文件，`Content-Type: application/octet-stream`，并附带下载文件名。该接口使用 GET，不是 JSON 接口。

### `DELETE /api/outputs/{name}`

删除指定 VTK 和同名 JSON 元数据：

```powershell
curl.exe -X DELETE http://127.0.0.1:3006/api/outputs/weather_fused.vtk
```

成功返回 `200`：

```json
{"ok": true, "name": "weather_fused.vtk"}
```

### 错误响应与超时

后端业务错误通常返回 `{"detail":"错误原因"}`，例如：

```json
{"detail": "请至少提供人口、湿度、湍流、地形或建筑数据"}
```

| HTTP 状态码 | 含义 |
| --- | --- |
| `400` | 文件类型、来源组合、清单、网格或数据解析/融合出错；下载/删除接口也用于表示不支持的扩展名或无效文件名 |
| `404` | 请求的结果文件不存在 |
| `405` | 请求方法不支持，例如用 POST 下载结果 |
| `422` | FastAPI 请求校验失败，例如缺少 `vtk` 或 `file`；`detail` 是错误对象数组 |
| `503` | 预览、规划、融合接口所需依赖未就绪 |
| `504` | 网关等待后端响应超时，通常由反向代理返回，响应体可能是 HTML |

客户端应先检查 HTTP 状态，再按响应内容解析；不能把网关的 HTML 错误页直接当作 JSON。当前应用未为上传设置专用的 `413` 响应；若代理返回 `413`，应检查其请求体大小限制。

融合接口会等处理完成后返回。若使用 Nginx，可在现有 `/fuse/` 的 `location` 块内设置：

```nginx
proxy_read_timeout 600s;
proxy_send_timeout 600s;
```

保留原有 `proxy_pass` 和路径转发规则，运行 `nginx -t`，通过后运行 `nginx -s reload`。这两项只调整等待时间；外层代理也需允许足够的等待时间。上传大小上限应容纳本次提交的全部文件。

出现 `504` 后，后台可能仍在融合。可稍后调用 `/api/outputs` 查询是否已生成结果，避免立即重复提交。同名结果会覆盖，不能仅凭文件名判断它是否来自本次请求，应同时查看修改时间。

## 支持的文件扩展名

| 类型 | 扩展名 |
| --- | --- |
| VTK | `.vtk`、`.vti`、`.vtp`、`.vts`、`.vtu`、`.vtr` |
| GeoTIFF | `.tif`、`.tiff`、`.geotiff` |
| WRF | `.nc`、`.nc4`、`.cdf`；无扩展名但文件名包含 `wrfout` 时网页也可识别。 |
| 风切变/湍流 | `.vtk` 切片文件（网页可一次多选、选文件夹或拖入文件夹）。 |
| 地形 | `.zip`，含 `layer.json` 和 `.terrain` 瓦片。 |
| 建筑 | `.zip`，含 `tileset.json` 和 `.b3dm` 瓦片。 |
