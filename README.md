# tools

A collection of tools (mainly Python scripts) used to generate assets and convert data for use in I, Voyager.

Every script carries its own specification in its module docstring — data source, frame and unit conventions, what it does and doesn't claim — and lists its arguments under `--help`. **This file is only an index**: which pipelines exist, and which script or document to open. Read that script (or its linked document) before running it.

## Conventions

**Add this submodule at `<project>/addons/tools` — not at the project root.** The location is load-bearing, not a preference: scripts that reach project files find the project by climbing three levels up from their own path, so anywhere else (`<project>/tools`, say) resolves *outside* the project, where every derived path is well-formed but wrong. `_project.project_dir()` centralizes that climb and refuses to return a directory holding no `project.godot`, so a misplaced submodule fails immediately and says so — **a new script that needs project files should call it rather than climbing on its own.** Nothing else about the location matters, so any project using the standard ivoyager layout works.

- **Run from the project directory** (the one holding `project.godot`): `python addons/tools/<script>.py ...`.
- **Sibling submodules a pipeline needs.** The trajectory scripts need `addons/ivoyager_core` (the tables they write into), and — for `verify_trajectory.py` and `horizons_trajectory.py --pre-fix` — `addons/ivoyager_assistant`, to drive the running sim; `verify_trajectory.py` won't even import without it. The asset builders need `addons/ivoyager_assets` unless given `--out-dir`.
- **Outputs** go to `addons/ivoyager_assets/` (`models/`, `maps/`, `starmaps/`) or to the Core plugin's `tables/`. The assets directory is not Git-tracked in any project — the editor plugin downloads it — so a regenerated asset reaches other people only through an [asset_downloads](https://github.com/ivoyager/asset_downloads) release.
- **A generated asset needs an attribution entry**, in `IVOYAGER_WORKS.md` (our original work, plus the public-domain source data it derives from) or `3RD_PARTY.md` (third-party files). Both are mastered in the asset_downloads repo.
- **Downloaded source data** belongs in `source_data/` here (Git-ignored); each script's docstring cites the archive to fetch it from.
- **Dependencies:** the trajectory scripts are stdlib-only. The asset builders need `numpy`, `Pillow`, `tifffile`, and `pygltflib` (the `.glb` writers).
- **After writing a `.tsv` or a new asset, refresh imports before launching:** `<godot-console> --path . --import --headless`. Headless runs use cached table data and silently ignore un-reimported edits. Godot keys reimport on the source's md5, so a regenerate that reproduces identical bytes is correctly a no-op (mtime is not consulted). Note that `--editor --headless --quit` quits *before* the async import scan completes — use `--import`, which waits for imports then exits.

## Spacecraft trajectories & ephemerides

Patched-conic trajectory data for real craft (Voyager 1 & 2, Pioneer 10, Juno, New Horizons), converted from NASA/JPL HORIZONS osculating elements into the Core plugin's `orbits.tsv`, `trajectories.tsv` and `spacecrafts.tsv`.

- `horizons_trajectory.py` — the converter. Segmentation is hand-maintained in its `CRAFT` dict.
- `verify_trajectory.py` — drives the running sim to check each segment's primary and its joins.
- `orbital.py` — the shared two-body and Lambert math (a library, not a command).

**[TRAJECTORIES.md](TRAJECTORIES.md) is the reference** for the model, the HORIZONS source and query, the element → column mapping, gap closing, segmentation, the time base, and known imprecisions. Read it before creating or editing a trajectory.

## Body models from a DEM

`build_body_model.py` turns an equirectangular DEM plus an albedo map into a displaced-ellipsoid `.glb` whose vertices are in kilometers (so it drops in at `model_scale = 1000`, like the NASA-derived models). Two modes: `mesh` (the default — displaced mesh with an embedded detail normal map) and `normal` (a full equirectangular normal map only, for bodies that stay generic spheroids). It prints the `file_adjustments.tsv` row to add.

`make_iapetus_dem.py` synthesizes an idealized Iapetus DEM (the fossil bulge and equatorial ridge) to feed that script, since no measured global DEM of Iapetus exists. It is **not** measured topography — see its docstring for what it does and does not claim.

Per-body sources for real DEMs are cited in `IVOYAGER_WORKS.md`.

## Body models from a measured shape model

`build_shape_model.py` builds the `.glb` from a measured vertex-facet shape model (Gaskell stereophotoclinometry, PDS `..._ver###q.tab`) — for genuinely irregular bodies such as Phoebe, where displacing a sphere doesn't apply. It uses the same authoring frame as the DEM path, so the two line up in-engine.

## Surface texture channels

`build_earth_roughness.py` builds Earth's roughness map (a land/sea specular mask) from maps already shipped in `ivoyager_assets`, so the coastline tracks them exactly and nothing needs downloading. Its output is linear data and must be imported non-sRGB (`compress/channel_pack=1`); the docstring explains why.

## Cubemap body maps

`bake_cubemap.py` reprojects a body's equirectangular maps into Godot cubemaps, which sample by direction and so have no pole pinch (radial starburst), no ±180° seam, and no sliver-triangle shading — see [discussion #22](https://github.com/orgs/ivoyager/discussions/22). It writes a 6-face **strip** PNG per channel (3×2 by default) to `addons/ivoyager_assets/cubemaps/` (not `maps/`), with an `.import` that Godot's own layered-texture importer turns into a `CompressedCubemap` — sliced, mipped and VRAM-compressed at import, so the engine decodes nothing at load and each export platform gets a block format its GPU supports. `--batch` converts every channel map under `maps/` at once (albedo, normal, roughness, emission), including shell overlays such as `Earth.clouds.*` and bodies with no surface albedo such as the Sun; per-channel face size defaults to the smallest power of two at or above `source_width / 4` — a quarter matches the source's average texel density at the equator, and the power of two is what the importer stores, it having upscaled any other size at import (a 450 face cost exactly a 512 face's VRAM while carrying 450 texels of detail through a second resample). Normal maps are reprojected to **object space** — a tangent-space map can't live on a cubemap (each face has a different tangent basis) — and import as BC7, since BC1 bands a normal badly. What is *stored* is the residual from each texel's own sphere direction, decoded as `normalize(dir + residual * normal_residual_scale)`; `--normal-residual-scale` must match the shader uniform of that name, and the default 1.0 clips nothing in any shipped map. Storing the normal itself puts the direction's full-range sweep into the data, and a block codec fitting that per 4×4 block prints a grid anywhere a specular lobe magnifies it — measured at 0.46° mean angular error with block-edge steps 1.9× the interior, against 0.35°/1.5× for the residual at the same scale, or 0.14°/1.3× at scale 0.25. A tangent-space map avoids this by being stored around a constant, which is why the equirect path never showed it. `--retile` rearranges an existing strip between layouts without an equirect source, for assets whose source is no longer available.

**A shell overlay's file name and its alpha are both load-bearing.** The shell token stays attached to the prefix (`Earth.clouds` in, `Earth.clouds.albedo.<face>.png` out) — that is how IVAssetPreloader finds the strip again, and in single-body mode it is on you to pass `--name Earth.clouds`, since `--name Earth` would write over the body's surface strip. A cloud map is flat white with all of its coverage in alpha, so alpha is reprojected alongside rgb and the strip imports BC7; alpha that is opaque everywhere is dropped instead, since carrying it would cost BC7's 8 bpp where rgb alone compresses to BC1's 4.

**This script is no longer the only route.** `ivoyager_core` now ships an in-editor equivalent at Project > Tools > "Map Convert…" (`editor/map_convert_dialog.gd`, reprojecting on the GPU via `editor/equirect_to_cube.gdshader`), so a project consuming only the Core plugin can convert its own maps without Python, numpy or Pillow. It covers the same scope as `--batch`, alpha included, and is held to matching this script's output: on the shipped maps, 99.3-99.99% of channel values are identical and the rest differ by one count, which is 32-bit arithmetic against numpy's 64-bit. **Anything that changes the conventions below must change in both**, and the shipped strips are the regression test for that.

Two names are load-bearing and were both established empirically, so don't "correct" them: the importer is **`cubemap_texture`** (plain `cubemap` is not registered and silently falls back to the default PNG importer, yielding a 2D texture), and `slices/arrangement` is an unlabeled enum in the order `1x6, 2x3, 3x2, 6x1` (only the matching value slices a strip into square faces). Note also that **`CompressedCubemap` does not extend `Cubemap`** — both derive from `TextureLayered`, so engine-side type tests must use `TextureLayered`.

**Precedence / coexistence:** the equirect path is unchanged and fully supported. IVAssetPreloader scans `maps/` then `cubemaps/`, so **when a body has both a `maps/` and a `cubemaps/` version of a channel, the cubemap wins.** A project — or body, or single channel — can be all-equirect, all-cube, or mixed, with identical tables; the asset type alone selects the shader.

`unwrap_cubemap.py` does the reverse (cubemap strip → equirectangular), optionally rotated 90° so the poles land on the equator — a diagnostic for telling baked-in source-data polar defects (which survive the rotation, showing at the well-sampled equator) from equirect projection artifacts (which the cubemap removes).

## Star field

`build_star_binaries.py` bakes the ESA Hipparcos Main Catalogue (`hip_main.dat`, VizieR I/239) into the magnitude-binned `.ivbinary` point clouds that `IVStarsVisual` loads on init. Stdlib-only. Its magnitude bin edges must stay matched to `IVStarsVisual.BINARY_FILE_MAGNITUDES`.
