# tools

A collection of tools (mainly Python scripts) used to generate assets and convert data for use in I, Voyager.

Every script carries its own specification in its module docstring — data source, frame and unit conventions, what it does and doesn't claim — and lists its arguments under `--help`. **This file is only an index**: which pipelines exist, and which script or document to open. Read that script (or its linked document) before running it.

## Conventions

**Add this submodule at `<project>/addons/tools` — not at the project root.** The location is load-bearing, not a preference: scripts that reach project files find the project by climbing three levels up from their own path, so anywhere else (`<project>/tools`, say) resolves *outside* the project, where every derived path is well-formed but wrong. `_project.project_dir()` centralizes that climb and refuses to return a directory holding no `project.godot`, so a misplaced submodule fails immediately and says so — **a new script that needs project files should call it rather than climbing on its own.** Nothing else about the location matters, so any project using the standard ivoyager layout works.

- **Run from the project directory** (the one holding `project.godot`): `python addons/tools/<script>.py ...`.
- **Sibling submodules a pipeline needs.** The trajectory scripts need `addons/ivoyager_core` (the tables they write into), and — for `verify_trajectory.py` and `horizons_trajectory.py --pre-fix` — `addons/ivoyager_assistant`, to drive the running sim; `verify_trajectory.py` won't even import without it. The asset builders need `addons/ivoyager_assets` unless given `--out-dir`.
- **Outputs** go to `addons/ivoyager_assets/` (`models/`, `maps/`, `starmaps/`) or to the Core plugin's `tables/`. The assets directory is not Git-tracked in any project — the editor plugin downloads it — so a regenerated asset reaches other people only through an [asset_downloads](https://github.com/ivoyager/asset_downloads) release.
- **A generated asset needs an attribution entry** in `IVOYAGER_ASSETS.md` — what it is, what it was made from, and its own copyright and license — plus a listing in `3RD_PARTY.md` if any of its content is third-party. Both are mastered in the asset_downloads repo.
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

`build_body_model.py` turns an equirectangular DEM plus an albedo map into a displaced-ellipsoid `.glb` whose vertices are in kilometers, like the NASA-derived models. The engine reads that scale from the output file name — `Name.1_1000.glb` is 1000 m per glb unit — so the name it writes is not cosmetic. Two modes: `mesh` (the default — displaced mesh with an embedded detail normal map) and `normal` (a full equirectangular normal map only, for bodies that stay generic spheroids).

`make_iapetus_dem.py` synthesizes an idealized Iapetus DEM (the fossil bulge and equatorial ridge) to feed that script, since no measured global DEM of Iapetus exists. It is **not** measured topography — see its docstring for what it does and does not claim.

Per-body sources for real DEMs are cited in `IVOYAGER_ASSETS.md`.

## Body models from a measured shape model

`build_shape_model.py` builds the `.glb` from a measured vertex-facet shape model (Gaskell stereophotoclinometry, PDS `..._ver###q.tab`) — for genuinely irregular bodies such as Phoebe, where displacing a sphere doesn't apply. It uses the same authoring frame as the DEM path, so the two line up in-engine.

## Surface texture channels

`build_earth_roughness.py` builds Earth's roughness map (a land/sea specular mask) from maps already shipped in `ivoyager_assets`, so the coastline tracks them exactly and nothing needs downloading. Its output is linear data and must be imported non-sRGB (`compress/channel_pack=1`); the docstring explains why.

## Cubemap body maps

`bake_cubemap.py` reprojects a body's equirectangular maps into Godot cubemaps, which sample by direction and so have no pole pinch (radial starburst), no ±180° seam, and no sliver-triangle shading — see [discussion #22](https://github.com/orgs/ivoyager/discussions/22). It writes a 6-face **strip** PNG per channel (3×2 by default) to `addons/ivoyager_assets/cubemaps/` (not `maps/`), with an `.import` that Godot's own layered-texture importer turns into a `CompressedCubemap` — sliced, mipped and VRAM-compressed at import, so the engine decodes nothing at load and each export platform gets a block format its GPU supports. `--batch` converts every channel map under `maps/` at once (albedo, normal, roughness, emission), including shell overlays such as `Earth.clouds.*` and bodies with no surface albedo such as the Sun; per-channel face size defaults to the smallest power of two at or above `source_width / 4` — a quarter matches the source's average texel density at the equator, and the power of two is what the importer stores, it having upscaled any other size at import (a 450 face cost exactly a 512 face's VRAM while carrying 450 texels of detail through a second resample); `--max-size` caps that per-channel size at a chosen power of two, for a project that wants cubemaps smaller than the source resolution would give (the in-editor dialog offers the same as a dropdown). Normal maps are reprojected to **object space** — a tangent-space map can't live on a cubemap (each face has a different tangent basis) — and import as BC7, since BC1 bands a normal badly. What is *stored* is the residual from each texel's own sphere direction, decoded as `normalize(dir + residual * normal_residual_scale)`; `--normal-residual-scale` must match the shader uniform of that name, and the default 1.0 clips nothing in any shipped map. Storing the normal itself puts the direction's full-range sweep into the data, and a block codec fitting that per 4×4 block prints a grid anywhere a specular lobe magnifies it — measured at 0.46° mean angular error with block-edge steps 1.9× the interior, against 0.35°/1.5× for the residual at the same scale, or 0.14°/1.3× at scale 0.25. A tangent-space map avoids this by being stored around a constant, which is why the equirect path never showed it. `--retile` rearranges an existing strip between layouts without an equirect source, for assets whose source is no longer available.

**A shell overlay's file name and its alpha are both load-bearing.** The shell token stays attached to the prefix (`Earth.clouds` in, `Earth.clouds.albedo.<face>.png` out) — that is how IVAssetPreloader finds the strip again, and in single-body mode it is on you to pass `--name Earth.clouds`, since `--name Earth` would write over the body's surface strip. A cloud map is flat white with all of its coverage in alpha, so alpha is reprojected alongside rgb and the strip imports BC7; alpha that is opaque everywhere is dropped instead, since carrying it would cost BC7's 8 bpp where rgb alone compresses to BC1's 4.

**This script is no longer the only route.** `ivoyager_core` now ships an in-editor equivalent at Project > Tools > "Map Convert…" (`editor/map_convert_dialog.gd`, reprojecting on the GPU via `editor/equirect_to_cube.gdshader`), so a project consuming only the Core plugin can convert its own maps without Python, numpy or Pillow. It covers the same scope as `--batch`, alpha included, and is held to matching this script's output: on the shipped maps, 99.3-99.99% of channel values are identical and the rest differ by one count, which is 32-bit arithmetic against numpy's 64-bit. **Anything that changes the conventions below must change in both**, and the shipped strips are the regression test for that.

Two names are load-bearing and were both established empirically, so don't "correct" them: the importer is **`cubemap_texture`** (plain `cubemap` is not registered and silently falls back to the default PNG importer, yielding a 2D texture), and `slices/arrangement` is an unlabeled enum in the order `1x6, 2x3, 3x2, 6x1` (only the matching value slices a strip into square faces). Note also that **`CompressedCubemap` does not extend `Cubemap`** — both derive from `TextureLayered`, so engine-side type tests must use `TextureLayered`.

**Precedence / coexistence:** the equirect path is unchanged and fully supported. IVAssetPreloader scans `maps/` then `cubemaps/`, so **when a body has both a `maps/` and a `cubemaps/` version of a channel, the cubemap wins.** A project — or body, or single channel — can be all-equirect, all-cube, or mixed, with identical tables; the asset type alone selects the shader.

`unwrap_cubemap.py` does the reverse (cubemap strip → equirectangular), optionally rotated 90° so the poles land on the equator — a diagnostic for telling baked-in source-data polar defects (which survive the rotation, showing at the well-sampled equator) from equirect projection artifacts (which the cubemap removes).

## Body 2D icons

`capture_body_icons.py` renders the `bodies_2d/` icon set — the flat images a GUI shows for a body — by launching the project and staging each body through `IVBody.make_body_visual()` into an off-screen rig, so an icon is the body *as the simulator draws it*: cube shaders, cloud and limb shells, band-pattern bodies and packed spacecraft models all come through with no special handling. Its in-sim half is `body_2d_icon_suite.gd`, reached over the `ivoyager_assistant` TCP server; the script registers it in `ivoyager_override2.cfg` for the run and restores the file afterwards, so it needs **both** `addons/ivoyager_core` and `addons/ivoyager_assistant` present. Two conventions carry the work: a pose is a **sub-camera longitude and latitude** rather than a turntable angle, so it survives a re-bake or a change of map registration and can be checked against a published landmark; and lighting is the engine's own physical sunlight with its compensating camera, one directional source at `metering_key / albedo`, so nothing needs per-body brightness tuning. The pose table is a project file passed with `--specs` (which face of a body is interesting is a project's decision, not a tool default); its columns, and the `roll auto` diagonal fit for elongated bodies, are documented in the script's docstring.

## Shader compile times

`time_shader_compiles.py` reports what each Core shader costs the GPU driver to compile and link — the frame in which a fresh material first draws, and the frame after the light is hidden, which is one further specialization. It generates a throwaway Godot project holding a copy of `addons/ivoyager_core/shaders/`, the hosting project's `[shader_globals]`, and `shader_compile_timer.gd` as its main scene, then runs **one Godot process per shader**: in a sequence the driver leaks work from earlier compiles into later first-draw frames, and a cache answers a rerun of unchanged source in milliseconds, so each run gets a fresh project and a uniquely named uniform appended to the shader under test. Because the copy is made fresh every run there is nothing here to keep in sync with the plugin. Needs `addons/ivoyager_core` and a Godot executable (by default the newest `Godot_v*_console.exe` beside the project). Compatibility is the default renderer, that being the one that hurts and the one the web export uses.

**[SHADER_COMPILE_COST.md](https://github.com/ivoyager/ivoyager_core/blob/develop/SHADER_COMPILE_COST.md) in the Core plugin is the record** — what the numbers are, what drives them, what an edit to a given `.gdshaderinc` costs, and the traps this script exists to encode. Read it before acting on anything this prints.

## Planetary rings

`build_saturn_rings.py` bakes Bjoern Joensson's five radial ring profiles
(https://bjj.mmedia.is/data/s_rings) into `rings/saturn.rings.<w>.exr` -- one file that
imports as a `CompressedTexture2DArray` of three width x 1 layers (backscatter, forward
scatter, unlit side), each holding linear scattering strength in rgb and the occluded
fraction at normal incidence in alpha. `rings.gdshader` samples it with one `textureLod`
per layer.

What the file holds is NOT the published brightness. A profile is an observation at one
ring opening angle, so shipping it as-is freezes that geometry; the build divides each
profile by the single-scattering slab's geometry term and stores what is left, which is a
property of the particles, and the shader multiplies the term back at the angles it is
rendering. The observing geometry is not published and is fitted from each profile's own
optical-depth dependence -- and that the fit works is the check on the whole construction,
the quotient coming out nearly flat across the C ring, the B ring, the Cassini Division and
the A ring. One fitted value, `unlit_floor`, has to reach the shader as well and is printed
as a `rings.tsv` cell to paste.

Three more things are load-bearing and are argued in the script's docstring: the source
profiles are **premultiplied** (a brightness profile is exactly 0 at every radius where the
transparency is exactly 1), so the shader composites with `blend_premul_alpha` and must not
multiply by alpha again; they are **linear**, which the script tests rather than assumes by
fitting the single-scattering slab model both ways; and the file is **half float**, because
an 8-bit one must either band the faint rings or let the engine average sRGB-encoded codes
when it generates mipmaps -- and a distant ring is nothing but its own mip chain.

`exr_writer.py` is the ~60-line uncompressed half-float EXR writer it uses, so the pipeline
needs numpy, scipy and nothing else. `--verify` refits the model and re-reads the written
file.

## Star field

`build_star_binaries.py` bakes the ESA Hipparcos Main Catalogue (`hip_main.dat`, VizieR I/239) into the magnitude-binned `.ivbinary` point clouds that `IVStarsVisual` loads on init. Stdlib-only. Its magnitude bin edges must stay matched to `IVStarsVisual.BINARY_FILE_MAGNITUDES`.

## Asteroids

`build_asteroid_binaries.py` bakes AstDyS-2 osculating and synthetic proper elements (`allnum.cat`, `ufitobs.cat`, `all.syn`, `tno.syn`, `secres.syn`, `tro.syn`), plus asteroid names and discovery designations from the JPL Small-Body Database query API, into the group- and magnitude-binned `.ivbinary` point clouds that `IVBinaryAsteroidsBuilder` loads on init. Stdlib-only; `--fetch-sbdb` downloads both JPL snapshots, `--verify` runs the self-checks, `--dry-run` reports without writing.

Three cross-file invariants it cannot import and so checks or reads instead. Its magnitude bin edges must stay matched to `IVBinaryAsteroidsBuilder.BINARY_FILE_MAGNITUDES` (`--verify` compares them). Group membership criteria, magnitude cutoffs and row order come from the Core plugin's `tables/small_bodies_groups.tsv` at build time, first row that passes, so retuning a group is a table edit and not a code edit. And Jupiter Trojan libration phase is solved against Jupiter's row in `tables/orbits.tsv`, whose mean-longitude polynomial `--verify` checks against JPL's published value.

**AstDyS re-keys an asteroid when it is numbered, but republishes proper elements only every year or two.** A body numbered inside that window is in the catalogs under its number and in the `.syn` files under its discovery designation, so it matches neither — which costs a Trojan the libration elements that put it in a cloud at all, and it is discarded as a suspect rather than merely losing its precession rates. The SBDB designation snapshot closes the gap: it aliases a retired designation onto the numbered catalog row, and only where that designation is not itself a live key, so it can never displace a direct match. Against the June 2026 catalogs the June 2024 proper elements stranded 2926 Trojans this way.

**AstDyS publishes `n` as the mean *longitude* rate.** Mean anomaly therefore advances at `n - g`, which the point shader applies and `IVSmallBodiesGroup.get_mean_anomaly_rate()` exposes. Reading `n` as the mean anomaly rate adds `g` to every asteroid's mean motion — negligible-looking at 0.02 % for the main belt, but for a resonant body `g` *is* the locking rate (`3*n_Jupiter - 2*n` for a Hilda), so it unlocks the group from its resonance entirely.
