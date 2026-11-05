const vtkInput = document.getElementById("vtk-input");
const popInput = document.getElementById("pop-input");
const wrfInput = document.getElementById("wrf-input");
const turbInput = document.getElementById("turb-input");
const turbFolder = document.getElementById("turb-folder");
const turbFileList = document.getElementById("turb-file-list");
const turbPickFiles = document.getElementById("turb-pick-files");
const turbPickFolder = document.getElementById("turb-pick-folder");
const turbClear = document.getElementById("turb-clear");
const fuseBtn = document.getElementById("fuse-btn");
const healthBanner = document.getElementById("health-banner");
const outputList = document.getElementById("output-list");

const VTK_EXTS = [".vtk", ".vti", ".vtp", ".vts", ".vtu", ".vtr"];
const VTK_DTYPE_SIZES = {
  bit: 1,
  unsigned_char: 1,
  char: 1,
  unsigned_short: 2,
  short: 2,
  unsigned_int: 4,
  int: 4,
  unsigned_long: 8,
  long: 8,
  float: 4,
  double: 8,
};

let turbFiles = [];
let vtkInfoPromise = Promise.resolve(null);

function errorDetail(payload, fallback) {
  const detail = payload && payload.detail;
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("；");
  }
  return JSON.stringify(detail);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function isVtkFile(file) {
  const name = (file && file.name ? file.name : "").toLowerCase();
  return VTK_EXTS.some((ext) => name.endsWith(ext));
}

function formatSize(nbytes) {
  if (nbytes < 1024) return `${nbytes} B`;
  if (nbytes < 1024 * 1024) return `${(nbytes / 1024).toFixed(1)} KB`;
  if (nbytes < 1024 * 1024 * 1024) return `${(nbytes / (1024 * 1024)).toFixed(2)} MB`;
  return `${(nbytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function setDropState(input, hasFile) {
  const zone = input.closest(".dropzone");
  if (!zone) return;
  zone.classList.toggle("has-file", hasFile);
  const nameEl = zone.querySelector(".filename");
  if (hasFile && input.files && input.files.length) {
    const label = nameEl || document.createElement("span");
    label.className = "filename";
    label.textContent =
      input.files.length === 1 ? input.files[0].name : `${input.files.length} 个文件`;
    if (!nameEl) zone.appendChild(label);
  } else if (nameEl) {
    nameEl.remove();
  }
}

function readAllEntries(reader) {
  return new Promise((resolve, reject) => {
    const entries = [];
    const readBatch = () => {
      reader.readEntries((batch) => {
        if (!batch.length) {
          resolve(entries);
          return;
        }
        entries.push(...batch);
        readBatch();
      }, reject);
    };
    readBatch();
  });
}

function entryToFile(entry) {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function collectEntry(entry, files) {
  if (entry.isFile) {
    files.push(await entryToFile(entry));
    return;
  }
  if (!entry.isDirectory) return;
  const children = await readAllEntries(entry.createReader());
  for (const child of children) {
    await collectEntry(child, files);
  }
}

async function filesFromDrop(event) {
  const items = event.dataTransfer && event.dataTransfer.items;
  if (items && items.length) {
    const files = [];
    const tasks = [];
    for (const item of items) {
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if (entry) {
        tasks.push(collectEntry(entry, files));
      } else if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    await Promise.all(tasks);
    if (files.length) return files;
  }
  return Array.from((event.dataTransfer && event.dataTransfer.files) || []);
}

function wireDropzone(dropId, input) {
  const zone = document.getElementById(dropId);
  ["dragenter", "dragover"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
    });
  });
  zone.addEventListener("drop", (event) => {
    const files = event.dataTransfer.files;
    if (!files || !files.length) return;
    if (files.length > 1) {
      alert("这一项只能选 1 个文件。多块风切变/湍流切片请放到第 4 步。");
      return;
    }
    assignFile(input, files[0]);
  });
}

function assignFile(input, file) {
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  input.dispatchEvent(new Event("change"));
}

function addTurbFiles(files) {
  const incoming = Array.from(files || []).filter(isVtkFile);
  if (!incoming.length) {
    alert("没有找到 VTK 切片（.vtk / .vti 等）");
    return;
  }
  const byName = new Map(turbFiles.map((file) => [file.name, file]));
  for (const file of incoming) {
    byName.set(file.name, file);
  }
  turbFiles = Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name, "en"));
  renderTurbFiles();
}

function renderTurbFiles() {
  const zone = document.getElementById("turb-drop");
  const meta = document.getElementById("turb-meta");
  zone.classList.toggle("has-file", turbFiles.length > 0);
  turbClear.hidden = turbFiles.length === 0;
  if (!turbFiles.length) {
    turbFileList.hidden = true;
    turbFileList.innerHTML = "";
    meta.hidden = true;
    return;
  }
  const total = turbFiles.reduce((sum, file) => sum + (file.size || 0), 0);
  turbFileList.hidden = false;
  turbFileList.innerHTML = turbFiles
    .map(
      (file) =>
        `<li><span class="file-name">${escapeHtml(file.name)}</span><span class="file-size">${formatSize(file.size || 0)}</span></li>`
    )
    .join("");
  meta.hidden = false;
  const warn =
    turbFiles.length === 1
      ? "<div>当前只选了 1 个切片。安阳湍流是多块拼接的，只融一块会出现大面积 NaN。</div>"
      : "";
  meta.innerHTML =
    `<div>已选 ${turbFiles.length} 个切片，合计 ${formatSize(total)}</div>` + warn;
}

function renderMeta(target, info) {
  target.hidden = false;
  if (info.kind === "vtk") {
    const dims = info.dimensions ? `网格 ${info.dimensions.join(" × ")}` : `${info.points} 个点`;
    const fields = (info.fields || []).join("、") || "无标量场";
    const hint = info.looks_projected
      ? "<div>坐标为投影米。融合按 EPSG:32649（UTM 49N），人口用 GeoTIFF 自带 CRS。</div>"
      : "";
    target.innerHTML = `<div>${info.type}</div><div>${dims}</div><div>属性：${fields}</div>${hint}`;
    return;
  }
  if (info.kind === "wrf") {
    const lon = info.lon || [];
    const lat = info.lat || [];
    const lonText = lon.length === 2 ? `${Number(lon[0]).toFixed(3)}–${Number(lon[1]).toFixed(3)} E` : "";
    const latText = lat.length === 2 ? `${Number(lat[0]).toFixed(3)}–${Number(lat[1]).toFixed(3)} N` : "";
    const height = info.has_height ? "含位势高度" : "无高度场，将沿 Z 复制";
    target.innerHTML =
      `<div>${info.type} · ${info.var || "QVAPOR"}</div>` +
      `<div>网格 ${info.west_east} × ${info.south_north} × ${info.bottom_top}</div>` +
      `<div>${lonText}，${latText}</div>` +
      `<div>水平间距 ${info.dx} m · ${height}</div>`;
    return;
  }
  if (info.kind === "turbulence") {
    const spacing = info.spacing || [];
    const spacingText = spacing.length ? spacing.join(" × ") + " m" : "";
    const fields = (info.fields || []).join("、") || "HorizontalShear、VerticalShear、Epsilon";
    const tiles = info.tiles != null ? `${info.tiles} 个切片` : "1 个切片";
    target.innerHTML =
      `<div>${info.type} · ${tiles}</div>` +
      `<div>分辨率 ${spacingText || "—"}</div>` +
      `<div>属性：${fields}</div>`;
    return;
  }
  const crs = info.crs || "未声明 CRS";
  target.innerHTML = `<div>${info.type} · ${info.width} × ${info.height}</div><div>${crs}</div><div>分辨率 ${info.res.map((v) => v.toPrecision(4)).join(" × ")}</div>`;
}

async function inspectFile(file, target) {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch("./api/inspect", { method: "POST", body });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    target.hidden = false;
    target.textContent = errorDetail(payload, "无法读取该文件");
    return null;
  }
  renderMeta(target, payload);
  return payload;
}

async function onVtkChange() {
  setDropState(vtkInput, Boolean(vtkInput.files[0]));
  const meta = document.getElementById("vtk-meta");
  if (!vtkInput.files[0]) {
    vtkInfoPromise = Promise.resolve(null);
    meta.hidden = true;
    return;
  }
  meta.hidden = false;
  meta.textContent = "正在读取…";
  vtkInfoPromise = inspectFile(vtkInput.files[0], meta).catch(() => null);
  await vtkInfoPromise;
}

// 与 stfusion.vtk_legacy.parse_structured_points 对应：只读文件头，不把 GB 级切片读进内存。
function makeLineReader(file) {
  let winStart = 0;
  let win = new Uint8Array(0);
  const reader = {
    pos: 0,
    async readLine() {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        if (this.pos < winStart || this.pos >= winStart + win.length) {
          winStart = this.pos;
          win = new Uint8Array(await file.slice(winStart, winStart + 8192).arrayBuffer());
          if (!win.length) return null;
        }
        const off = this.pos - winStart;
        let end = win.indexOf(10, off);
        const atEof = winStart + win.length >= file.size;
        if (end < 0 && !atEof) {
          win = new Uint8Array(0);
          continue;
        }
        if (end < 0) end = win.length;
        this.pos = winStart + Math.min(end + 1, win.length);
        return new TextDecoder("latin1").decode(win.subarray(off, end)).trim();
      }
      throw new Error("VTK 头行过长");
    },
    async readNonEmptyLine() {
      let line = await this.readLine();
      while (line === "") line = await this.readLine();
      return line;
    },
  };
  return reader;
}

async function parseVtkHeader(file) {
  const reader = makeLineReader(file);
  const version = (await reader.readLine()) || "";
  if (!version.toLowerCase().startsWith("# vtk")) throw new Error(`${file.name} 不是 VTK 文件`);
  await reader.readLine();
  const fmt = ((await reader.readLine()) || "").toUpperCase();
  if (!fmt.startsWith("BINARY")) throw new Error(`${file.name} 不是 BINARY VTK`);
  const dataset = ((await reader.readLine()) || "").toUpperCase();
  if (!dataset.includes("STRUCTURED_POINTS")) throw new Error(`${file.name} 不是 STRUCTURED_POINTS`);

  const header = { dimensions: null, origin: null, spacing: null, n_points: null, fields: [] };
  const nums = (parts, parse) => parts.slice(1, 4).map(parse);
  const pushField = (name, ncomp, nvals, dtype) => {
    const size = VTK_DTYPE_SIZES[dtype] || 4;
    header.fields.push({ name, ncomp, nvals, dtype, offset: reader.pos });
    reader.pos += nvals * ncomp * size;
  };

  for (;;) {
    const lineStart = reader.pos;
    const line = await reader.readLine();
    if (!line) break;
    const parts = line.split(/\s+/);
    const key = parts[0].toUpperCase();
    if (key === "DIMENSIONS") header.dimensions = nums(parts, (v) => parseInt(v, 10));
    else if (key === "ORIGIN") header.origin = nums(parts, Number);
    else if (key === "SPACING") header.spacing = nums(parts, Number);
    else if (key === "POINT_DATA") header.n_points = parseInt(parts[1], 10);
    else if (key === "CELL_DATA") break;
    else if (key === "FIELD") {
      const count = parseInt(parts[parts.length - 1], 10);
      for (let i = 0; i < count; i += 1) {
        const fieldLine = await reader.readNonEmptyLine();
        if (!fieldLine) break;
        const fp = fieldLine.split(/\s+/);
        if (fp.length < 4) throw new Error(`无法解析 FIELD 行: ${fieldLine}`);
        pushField(fp[0], parseInt(fp[1], 10), parseInt(fp[2], 10), fp[3].toLowerCase());
      }
      break;
    } else if (["SCALARS", "VECTORS", "NORMALS", "TENSORS", "COLOR_SCALARS"].includes(key)) {
      reader.pos = lineStart;
      for (;;) {
        const arrayLine = await reader.readNonEmptyLine();
        if (!arrayLine || header.n_points == null) break;
        const ap = arrayLine.split(/\s+/);
        const arrayKey = ap[0].toUpperCase();
        const dtype = (ap[2] || "float").toLowerCase();
        if (arrayKey === "SCALARS") {
          const lookup = (await reader.readLine()) || "";
          if (!lookup.toUpperCase().startsWith("LOOKUP_TABLE")) break;
          pushField(ap[1], ap[3] ? parseInt(ap[3], 10) : 1, header.n_points, dtype);
        } else if (arrayKey === "VECTORS") {
          pushField(ap[1], 3, header.n_points, dtype);
        } else {
          break;
        }
      }
      break;
    }
  }
  if (!header.dimensions || !header.origin || !header.spacing) {
    throw new Error(`${file.name} 的 VTK 头缺少 DIMENSIONS/ORIGIN/SPACING`);
  }
  if (header.n_points == null) {
    header.n_points = header.dimensions[0] * header.dimensions[1] * header.dimensions[2];
  }
  return header;
}

// 只上传气象网格用得到的切片行；失败时返回 null，由调用方改为上传完整切片。
async function buildTurbulenceSubset(setStatus) {
  setStatus("正在读取气象 VTK 网格…");
  const vtkInfo = await vtkInfoPromise;
  if (!vtkInfo || !vtkInfo.dimensions || !vtkInfo.origin || !vtkInfo.spacing) return null;
  try {
    setStatus("正在读取湍流切片头…");
    const tiles = [];
    for (const file of turbFiles) {
      tiles.push({ name: file.name, size: file.size, header: await parseVtkHeader(file) });
    }
    setStatus("正在计算需要上传的切片数据…");
    const response = await fetch("./api/turbulence/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        grid: { origin: vtkInfo.origin, spacing: vtkInfo.spacing, dimensions: vtkInfo.dimensions },
        resampling: "nearest",
        tiles,
      }),
    });
    const plan = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(plan, "无法生成切片子集"));
    const byName = new Map(turbFiles.map((file) => [file.name, file]));
    const parts = [];
    for (const transfer of plan.transfers || []) {
      if (!transfer.ranges || !transfer.ranges.length) continue;
      const file = byName.get(transfer.name);
      const blob = new Blob(transfer.ranges.map(([start, length]) => file.slice(start, start + length)));
      if (blob.size !== transfer.nbytes) throw new Error(`${transfer.name} 子集大小不一致`);
      parts.push({ name: transfer.name, blob });
    }
    return { manifest: plan.manifest, parts, totalBytes: plan.total_bytes || 0 };
  } catch (error) {
    console.warn("切片子集不可用，改为上传完整切片：", error);
    return null;
  }
}

async function onPopChange() {
  setDropState(popInput, Boolean(popInput.files[0]));
  const meta = document.getElementById("pop-meta");
  if (!popInput.files[0]) {
    meta.hidden = true;
    return;
  }
  meta.hidden = false;
  meta.textContent = "正在读取…";
  await inspectFile(popInput.files[0], meta);
}

async function onWrfChange() {
  setDropState(wrfInput, Boolean(wrfInput.files[0]));
  const meta = document.getElementById("wrf-meta");
  if (!wrfInput.files[0]) {
    meta.hidden = true;
    return;
  }
  meta.hidden = false;
  meta.textContent = "正在读取…";
  await inspectFile(wrfInput.files[0], meta);
}

function renderOutputs(items) {
  if (!items.length) {
    outputList.innerHTML = '<p class="output-empty">暂无融合结果</p>';
    return;
  }
  outputList.innerHTML = items
    .map((item) => {
      const extra = [];
      if (item.turbulence_tiles) extra.push(`${item.turbulence_tiles} 个湍流切片`);
      const tags = [".vtk", item.size_text, item.mtime]
        .concat(extra)
        .concat((item.fields || []).slice(0, 8))
        .filter(Boolean)
        .map((tag) => `<li>${escapeHtml(tag)}</li>`)
        .join("");
      const source = item.source
        ? `原始文件: ${escapeHtml(item.source)}`
        : "原始文件: —";
      const href = `./api/outputs/${encodeURIComponent(item.name)}`;
      return `
        <article class="output-item">
          <h3>${escapeHtml(item.name)}</h3>
          <ul class="output-tags">${tags}</ul>
          <p class="output-note">${source}</p>
          <div class="output-actions">
            <a class="ghost" href="${href}" download="${escapeHtml(item.name)}">下载 VTK</a>
            <button type="button" class="ghost danger" data-delete="${escapeHtml(item.name)}">删除</button>
          </div>
        </article>
      `;
    })
    .join("");
}

async function loadOutputs() {
  try {
    const response = await fetch("./api/outputs");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(payload, "无法读取融合结果"));
    renderOutputs(payload.items || []);
  } catch {
    renderOutputs([]);
  }
}

async function deleteOutput(name) {
  if (!window.confirm(`确定删除「${name}」？`)) return;
  try {
    const response = await fetch(`./api/outputs/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(payload, "删除失败"));
    await loadOutputs();
  } catch (error) {
    alert(error.message);
  }
}

async function runFusion() {
  if (!vtkInput.files[0]) {
    alert("请先选择气象 VTK");
    return;
  }
  const hasTurb = turbFiles.length > 0;
  if (!popInput.files[0] && !wrfInput.files[0] && !hasTurb) {
    alert("请至少选择人口 GeoTIFF、湿度文件或风切变/湍流数据");
    return;
  }
  if (turbFiles.length === 1) {
    const ok = window.confirm(
      "当前只选了 1 个湍流切片。安阳数据通常是 7 块，只融一块会有大面积空值。仍要继续吗？"
    );
    if (!ok) return;
  }
  fuseBtn.disabled = true;
  const setStatus = (text) => {
    fuseBtn.textContent = text;
  };
  setStatus(wrfInput.files[0] ? "正在融合（湿度插值较慢）…" : "正在融合…");

  const body = new FormData();
  body.append("vtk", vtkInput.files[0]);
  if (popInput.files[0]) {
    body.append("geotiff", popInput.files[0]);
    body.append("geotiff_field", document.getElementById("pop-field").value || "population");
  }
  if (wrfInput.files[0]) {
    const wrfVar = document.getElementById("wrf-var").value || "QVAPOR";
    body.append("wrf", wrfInput.files[0]);
    body.append("wrf_var", wrfVar);
    body.append("wrf_field", wrfVar);
  }
  body.append("vtk_crs", document.getElementById("vtk-crs").value.trim() || "EPSG:32649");
  body.append("target_crs", document.getElementById("target-crs").value);
  body.append("strategy", document.getElementById("strategy").value);
  body.append("resampling", document.getElementById("resampling").value);

  try {
    if (hasTurb) {
      const subset = await buildTurbulenceSubset(setStatus);
      if (subset) {
        body.append("turbulence_manifest", JSON.stringify(subset.manifest));
        for (const part of subset.parts) {
          body.append("turbulence_subset", part.blob, part.name);
        }
        setStatus(`正在融合（${turbFiles.length} 个湍流切片，上传 ${formatSize(subset.totalBytes)}）…`);
      } else {
        for (const file of turbFiles) {
          body.append("turbulence", file);
        }
        const total = turbFiles.reduce((sum, file) => sum + (file.size || 0), 0);
        setStatus(`正在上传完整湍流切片（${formatSize(total)}，较慢）…`);
      }
    }
    const response = await fetch("./api/fuse", { method: "POST", body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(errorDetail(payload, "融合失败"));
    }
    await loadOutputs();
  } catch (error) {
    alert(error.message);
  } finally {
    fuseBtn.disabled = false;
    fuseBtn.textContent = "开始融合";
  }
}

async function checkHealth() {
  try {
    const response = await fetch("./api/health");
    const data = await response.json();
    if (data.ready) return;
    healthBanner.classList.remove("hidden");
    healthBanner.classList.add("error");
    healthBanner.textContent =
      "页面已可用，但融合依赖未安装（" +
      (data.missing || []).join("、") +
      "）。请先执行 python -m pip install -e .";
  } catch {
    healthBanner.classList.remove("hidden");
    healthBanner.textContent = "无法连接融合服务。";
  }
}

wireDropzone("vtk-drop", vtkInput);
wireDropzone("pop-drop", popInput);
wireDropzone("wrf-drop", wrfInput);
vtkInput.addEventListener("change", onVtkChange);
popInput.addEventListener("change", onPopChange);
wrfInput.addEventListener("change", onWrfChange);
turbPickFiles.addEventListener("click", () => turbInput.click());
turbPickFolder.addEventListener("click", () => turbFolder.click());
turbClear.addEventListener("click", () => {
  turbFiles = [];
  turbInput.value = "";
  turbFolder.value = "";
  renderTurbFiles();
});
turbInput.addEventListener("change", () => {
  addTurbFiles(turbInput.files);
  turbInput.value = "";
});
turbFolder.addEventListener("change", () => {
  addTurbFiles(turbFolder.files);
  turbFolder.value = "";
});
document.getElementById("turb-drop").addEventListener("dragenter", (event) => {
  event.preventDefault();
  event.currentTarget.classList.add("dragover");
});
document.getElementById("turb-drop").addEventListener("dragover", (event) => {
  event.preventDefault();
  event.currentTarget.classList.add("dragover");
});
document.getElementById("turb-drop").addEventListener("dragleave", (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragover");
});
document.getElementById("turb-drop").addEventListener("drop", async (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragover");
  const files = await filesFromDrop(event);
  addTurbFiles(files);
});
fuseBtn.addEventListener("click", runFusion);
outputList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-delete]");
  if (!button) return;
  deleteOutput(button.getAttribute("data-delete"));
});
checkHealth();
loadOutputs();
