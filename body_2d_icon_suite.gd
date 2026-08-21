# body_2d_icon_suite.gd
# This file is part of I, Voyager
# https://ivoyager.dev
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# I, Voyager is a registered trademark of Charlie Whitfield in the US
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# *****************************************************************************
extends IVAssistantTestSuite

## Assistant API for batch-capturing body 2D icons; the in-sim half of
## [code]addons/tools/capture_body_icons.py[/code], which is where the pose
## specification, the file naming and the deploy live.
##
## Requires [code]addons/ivoyager_core[/code] (for [IVBodyVisual] and
## [IVBody2DCapturer]) and [code]addons/ivoyager_assistant[/code]. Register it in
## [code]ivoyager_override2.cfg[/code] under [code][assistant_test_suites][/code];
## the Python driver does that itself and undoes it at exit.[br][br]
##
## A body is staged exactly as the simulator draws it — cube shaders, cloud and limb
## shells, band-pattern bodies and packed craft models all come through
## [method IVBody.make_body_visual] — and posed by the point of the body facing the
## camera rather than by turntable angles, so a pose is a longitude and a latitude that
## survive a re-bake or a re-source. Lighting is the engine's own: a single directional
## source at [code]metering_key / albedo[/code], which is what [IVExposureManager]
## settles a body's light energy to once it fills the view.[br][br]
##
## Rendering needs frames, so a capture cannot answer within one JSON-RPC call:
## [code]capture_body_icon[/code] starts one and returns immediately, and
## [code]poll_icon_capture[/code] returns the result once it lands.

## Light energy is this over the body's albedo — superseded by
## [member IVExposureManager.metering_key] wherever the physical-light system is present.
const FALLBACK_METERING_KEY := 0.5
const DEFAULT_SUPERSAMPLE := 4 ## Render at the icon size times this, downsampled on save.
const FIT_ITERATIONS := 3 ## Measure-and-rescale passes behind the fill target.
## The fit is measured at this fraction of the frame and scaled to the requested fill
## afterwards. A silhouette that overflows the frame measures as exactly 1.0 however far it
## overflows, so a fit that targeted 1.0 directly could settle on a truncated body and call
## it a fit; orthographic scale is exactly linear in zoom, so measuring with headroom and
## scaling once is both safe and exact.
const FIT_MEASURE_FILL := 0.8
const MOMENT_SIZE := 256 ## Silhouette copy the auto-roll axis is measured on.
## Screen angle the auto-roll puts an elongated body's long axis at, counter-clockwise
## from frame right — so lower-left to upper-right.
const DIAGONAL_ANGLE_DEG := 45.0
const DEFAULT_ALBEDO := 0.3 ## For a body whose table row has none (every spacecraft).
const CLIP_LEVEL := 254.5 / 255.0 ## A channel at or above this counts as clipped.
## Camera distance in target radii — VIEW_ZOOM's own framing, which an icon reproduces so it
## looks like the app.
##
## The projection has to be PERSPECTIVE for that, and it is not a cosmetic choice: a shader
## reading [code]VIEW[/code] gets it from the view-space POSITION, so under an orthographic
## projection a disc-photometry law's [code]mu[/code] reaches zero wherever the camera node
## happens to sit while the projection still draws the sphere out to its full radius. At the
## staging default of about 4 radii that printed a hard-edged black annulus 4 px wide on
## Venus, whose minnaert_k the law applies. Matching the app's projection removes it by
## construction, since mu then reaches zero exactly at the drawn silhouette.
##
## Measured back out of the app rather than read off the table: at focal length 24 mm
## (vertical fov 51.86 deg, Godot's KEEP_HEIGHT) a VIEW_ZOOM screenshot puts Mars' disc at an
## angular radius of 19.50 deg, i.e. 3.00 radii; Venus 2.97, Earth 3.11 (its limb shell
## inflates the measured disc). That agrees with views.tsv's view_position_z of 3.
const VIEW_ZOOM_RADII := 3.0
## Focal length the app defaults to, in 35 mm-equivalent mm. Only the DISTANCE above sets the
## perspective; narrowing the fov from here to frame the icon is a pure crop.
const VIEW_ZOOM_FOCAL_LENGTH := 24.0

var _capturer: IVBody2DCapturer
var _viewport: SubViewport
var _camera: Camera3D
var _key_light: DirectionalLight3D
var _fill_light: DirectionalLight3D
var _roll_pivot: Node3D
var _yaw_pivot: Node3D
var _pitch_pivot: Node3D
var _spin_pivot: Node3D
var _world_environment: WorldEnvironment
var _view_distance := 0.0
var _view_extent := 0.0
var _view_tangent := 0.0
var _exposure_manager: Node
var _exposure_process_mode := Node.PROCESS_MODE_INHERIT
var _capture_running := false
var _capture_result: Dictionary


func _on_about_to_free() -> void:
	_free_rig()


func get_method_names() -> Array[String]:
	return ["list_icon_bodies", "capture_body_icon", "poll_icon_capture", "end_icon_capture"]


func get_method_summaries() -> Dictionary:
	return {
		"list_icon_bodies": "List capturable bodies with file prefix, albedo and model kind.",
		"capture_body_icon": "Start one icon capture (returns at once; poll for the result).",
		"poll_icon_capture": "Return the running capture's result once it has landed.",
		"end_icon_capture": "Free the capture rig.",
	}


func dispatch(method: String, params: Dictionary) -> Variant:
	match method:
		"list_icon_bodies":
			return _list_icon_bodies()
		"capture_body_icon":
			return _start_capture(params)
		"poll_icon_capture":
			return _poll_capture()
		"end_icon_capture":
			_free_rig()
			return {"freed": true}
	return {"_error": {"code": ERR_UNKNOWN_METHOD, "message": "Unknown method: %s" % method}}


func _list_icon_bodies() -> Dictionary:
	const DISABLE_MODEL_SPACE := IVBody.BodyFlags.BODYFLAGS_DISABLE_MODEL_SPACE
	var asset_preloader: IVAssetPreloader = IVGlobal.program[&"AssetPreloader"]
	var rows: Array[Dictionary] = []
	var sorted_names := PackedStringArray()
	for body_name: StringName in IVBody.bodies:
		sorted_names.append(body_name)
	sorted_names.sort()
	for sorted_name in sorted_names:
		var body_name := StringName(sorted_name)
		var body: IVBody = IVBody.bodies[body_name]
		if body.flags & DISABLE_MODEL_SPACE:
			continue
		var prefix := asset_preloader.get_body_file_prefix(body_name)
		if prefix.is_empty():
			continue
		var model_kind := "sphere"
		if asset_preloader.get_body_packed_model(body_name):
			model_kind = "packed"
		elif asset_preloader.get_body_mesh(body_name):
			model_kind = "mesh"
		var shell_tags := PackedStringArray()
		for spec_variant: Variant in asset_preloader.get_body_shell_specs(body_name):
			var spec: Dictionary = spec_variant
			shell_tags.append(str(spec.get(&"tag", "")))
		var triaxial := body.get_triaxial_size()
		rows.append({
			"name": String(body_name),
			"prefix": prefix,
			"model_kind": model_kind,
			"albedo": _get_albedo(body),
			"mean_radius": body.mean_radius,
			"triaxial_size": [triaxial.x, triaxial.y, triaxial.z],
			"shell_tags": shell_tags,
		})
	return {"bodies": rows, "metering_key": _get_metering_key()}


func _poll_capture() -> Dictionary:
	if _capture_running:
		return {"done": false}
	if _capture_result.is_empty():
		return {"_error": {"code": ERR_INVALID_PARAMS, "message": "No capture has been started"}}
	var result := _capture_result
	_capture_result = {}
	return result


func _start_capture(params: Dictionary) -> Variant:
	if _capture_running:
		return {"_error": {"code": ERR_NOT_ALLOWED, "message": "A capture is already running"}}
	var body_name := StringName(str(params.get("body", "")))
	if !IVBody.bodies.has(body_name):
		return {"_error": {"code": ERR_BODY_NOT_FOUND, "message": "No body %s" % body_name}}
	if str(params.get("out_path", "")).is_empty():
		return {"_error": {"code": ERR_INVALID_PARAMS, "message": "Missing out_path"}}
	_capture_running = true
	_capture_result = {}
	_run_capture(body_name, params)
	return {"started": true}


# The coroutine behind capture_body_icon(). Its caller cannot await it (dispatch is
# synchronous), so completion is published through _capture_result / _capture_running and
# collected by poll_icon_capture.
func _run_capture(body_name: StringName, params: Dictionary) -> void:
	var out_path := str(params.get("out_path", ""))
	var width := _read_int(params, "width", IVBody2DCapturer.ICON_SIZE)
	var height := _read_int(params, "height", IVBody2DCapturer.ICON_SIZE)
	var supersample := _read_int(params, "supersample", DEFAULT_SUPERSAMPLE)
	_build_rig(Vector2i(width * supersample, height * supersample))
	var body: IVBody = IVBody.bodies[body_name]
	var visual := body.make_body_visual()
	if !visual:
		_fail("%s has no model" % body_name)
		return
	var aabb := _capturer.stage_visual(visual)
	_hide_shells_by_tag(body_name, params.get("hide_shell_tags"))
	_apply_shell_visibility(params.get("shells"))

	var albedo := _read_float(params, "albedo", 0.0)
	if albedo <= 0.0:
		albedo = _get_albedo(body)
	var metering_key := _read_float(params, "metering_key", 0.0)
	if metering_key <= 0.0:
		metering_key = _get_metering_key()
	var brightness := _read_float(params, "brightness", 0.0)
	if brightness <= 0.0:
		brightness = metering_key / albedo
	_capturer.set_fill_light_enabled(false)
	var key_azimuth := deg_to_rad(-90.0 + _read_float(params, "light_left", 15.0))
	# light_up is where the SOURCE sits relative to the camera axis, so the shine
	# direction takes the opposite elevation: a source above shines downward.
	var key_elevation := deg_to_rad(-_read_float(params, "light_up", -10.0))
	_capturer.set_key_light(key_azimuth, key_elevation)
	_set_sun_direction(visual, -IVBody2DCapturer.azimuth_elevation_to_direction(
			key_azimuth, key_elevation))
	_capturer.set_brightness(brightness)
	_capturer.set_ambient(_read_float(params, "ambient", 0.0))
	_set_environment_ambient(_read_float(params, "env_ambient", 0.0))
	_hold_self_luminous_scale(_read_float(params, "exposure", 0.0))

	# The turntable's own fit can only bound the unrotated box, and the pose here spins the
	# body under it (see _apply_pose), so frame on the box's diagonal — a bound no rotation
	# can exceed — and let the measured fit below close on the real silhouette.
	var extent := aabb.size.length()
	var bounding_cube := AABB(Vector3.ONE * -0.5 * extent, Vector3.ONE * extent)
	# The bounding sphere the fit starts from is the AABB's, which circumscribes the body;
	# the camera distance is the body's OWN radius, the same one IVCamera frames on.
	_view_extent = maxf(extent, 0.001)
	var bound := _view_extent * 0.5
	# VIEW_ZOOM frames on the body's own camera radius, but a PACKED MODEL's table radius is a
	# placeholder (every spacecraft carries 5 m) against a model spanning tens of metres, so
	# the same multiple would put the camera inside it. Its own bounding radius is the honest
	# stand-in; the floor is the backstop for anything else that surprises us.
	var reference_radius := maxf(body.get_camera_radius(), 0.001)
	var asset_preloader: IVAssetPreloader = IVGlobal.program[&"AssetPreloader"]
	if asset_preloader.get_body_packed_model(body_name):
		reference_radius = bound
	_view_distance = maxf(reference_radius * _read_float(params, "camera_radii",
			VIEW_ZOOM_RADII), bound * 1.05)
	bound = minf(bound, _view_distance * 0.999)
	_view_tangent = bound / sqrt(maxf(_view_distance * _view_distance - bound * bound, 1e-12))
	var longitude := _read_float(params, "longitude", 0.0)
	var latitude := _read_float(params, "latitude", 0.0)
	var roll := _read_float(params, "roll", 0.0)
	var zoom := _read_float(params, "zoom", 1.0)
	var pan := _parse_pan(params.get("pan"))
	_apply_pose(bounding_cube, longitude, latitude, roll, zoom, pan)

	if _read_bool(params, "auto_roll", false):
		roll = await _solve_diagonal_roll(bounding_cube, longitude, latitude, roll, zoom, pan)
	# The measured fit runs at zoom 1 and the zoom column multiplies its answer, rather than
	# seeding it: a fit target above 1 can never be measured (the used rect saturates at the
	# frame), so cropping has to be expressed as a factor on a fit that did converge.
	var fill := _read_float(params, "fill", 1.0)
	if fill > 0.0:
		var fitted := await _solve_fill_zoom(bounding_cube, longitude, latitude, roll, 1.0, pan)
		if _read_bool(params, "auto_center", true):
			pan += await _solve_center_pan(bounding_cube, longitude, latitude, roll, fitted, pan)
		zoom *= fitted * FIT_MEASURE_FILL / fill
		_apply_pose(bounding_cube, longitude, latitude, roll, zoom, pan)
		# The analytic step above is exact for a sphere, whose projected radius really is
		# proportional to 1 / tan(half fov); an extended model under perspective spans depths
		# and is not, so measure once more and correct -- but only with headroom left, since a
		# saturated silhouette measures 1.0 however far it overflows.
		zoom = await _refine_fill_zoom(bounding_cube, longitude, latitude, roll, zoom, pan, fill)

	# Exposure compensation in stops, on top of the physical rig. The engine meters a body on
	# its SUBSOLAR luminance, which is a fair proxy at the phase angles the app views from and
	# is the WHOLE DISC at the near-zero phase an icon is lit at -- so an icon can need a stop
	# the app never takes. Its whole job is protecting highlights, though, so under ev_auto a
	# body that does not clip keeps ev 0 and renders at exactly the app's level.
	var ev := _read_float(params, "ev", 0.0)
	if _read_bool(params, "ev_auto", false):
		var unprotected := await _render()
		if _measure_clip_fraction(unprotected) <= _read_float(params, "clip_limit", 0.01):
			ev = 0.0
	if ev != 0.0:
		_capturer.set_brightness(brightness * 2.0 ** ev)

	var image := await _render()
	var report := _measure(image)
	image.resize(width, height, Image.INTERPOLATE_LANCZOS)
	var save_error := image.save_png(out_path)
	_capturer.clear_visual()
	_release_self_luminous_scale()
	if save_error != OK:
		_fail("Failed to save %s (error %s)" % [out_path, save_error])
		return
	report["path"] = out_path
	report["body"] = String(body_name)
	report["albedo"] = albedo
	report["brightness"] = brightness * 2.0 ** ev
	report["ev"] = ev
	report["longitude"] = longitude
	report["latitude"] = latitude
	report["roll"] = roll
	report["zoom"] = zoom
	report["done"] = true
	_capture_result = report
	_capture_running = false


# Sub-camera longitude is a spin about the body's own polar axis, applied INSIDE the
# turntable (_spin_pivot is the capturer's model holder); sub-camera latitude is the
# turntable's pitch, which lays the pole from the camera axis to screen up at latitude 0;
# roll is the screen-plane rotation, outside both so it stays screen-aligned.
#
# The spin's -90 constant is the engine's own longitude zero, alpha = atan2(-x, -z) in the
# body frame -- NOT the +90 that body_visual.gd's "longitude 0 on model +Z" comment implies,
# which is the UV zero and sits half a turn away on a prime-meridian-centred master.
# Verified against Earth: longitude 0 must render Africa, and it does.
func _apply_pose(bounding_cube: AABB, longitude: float, latitude: float, roll: float,
		zoom: float, pan: Vector2) -> void:
	_spin_pivot.rotation = Vector3(0.0, 0.0, deg_to_rad(-90.0 - longitude))
	_roll_pivot.rotation = Vector3(0.0, 0.0, deg_to_rad(roll))
	# frame_camera drives the turntable and would frame an ORTHOGRAPHIC camera; the camera
	# itself is rebuilt here as the app's perspective one. Distance is fixed at VIEW_ZOOM's,
	# so zoom narrows the fov instead -- a crop, which leaves the perspective untouched.
	_capturer.frame_camera(bounding_cube, 0.0, deg_to_rad(latitude - 90.0), zoom, pan)
	var tangent := maxf(_view_tangent * zoom, 1e-6)
	_camera.projection = Camera3D.PROJECTION_PERSPECTIVE
	_camera.keep_aspect = Camera3D.KEEP_HEIGHT
	_camera.fov = rad_to_deg(2.0 * atan(tangent))
	_camera.near = maxf((_view_distance - _view_extent * 0.5) * 0.5, 1e-5)
	_camera.far = _view_distance + _view_extent
	var frame_height := 2.0 * _view_distance * tangent
	_camera.position = Vector3(-pan.x * frame_height, pan.y * frame_height, _view_distance)


# Rescales until the rendered silhouette's longer dimension covers
# [constant FIT_MEASURE_FILL] of the frame. Measuring what rendered is the only fit that
# holds for a cloud shell or a spacecraft boom, neither of which the model AABB describes.
func _solve_fill_zoom(bounding_cube: AABB, longitude: float, latitude: float, roll: float,
		zoom: float, pan: Vector2) -> float:
	var solved := zoom
	for _iteration in FIT_ITERATIONS:
		var image := await _render()
		var used := image.get_used_rect()
		var fraction := maxf(float(used.size.x) / float(image.get_width()),
				float(used.size.y) / float(image.get_height()))
		if fraction <= 0.0:
			break
		solved *= fraction / FIT_MEASURE_FILL
		_apply_pose(bounding_cube, longitude, latitude, roll, solved, pan)
	return solved


# One measured correction after the analytic scale; see its call site.
func _refine_fill_zoom(bounding_cube: AABB, longitude: float, latitude: float, roll: float,
		zoom: float, pan: Vector2, fill: float) -> float:
	var image := await _render()
	var used := image.get_used_rect()
	var fraction := maxf(float(used.size.x) / float(image.get_width()),
			float(used.size.y) / float(image.get_height()))
	if fraction <= 0.0 or fraction >= 0.999 or absf(fraction - fill) < 0.005:
		return zoom
	var refined := zoom * fraction / fill
	_apply_pose(bounding_cube, longitude, latitude, roll, refined, pan)
	return refined


# Returns the pan that centers the rendered silhouette. Staging centers a body's AABB on
# the turntable pivot, which is not where an irregular body's SILHOUETTE sits once it is
# posed -- Phobos ran 6 px clear of one edge and hard against the other, and a fill of 1.0
# then truncates the side it drifted toward. Measured at the fit's own safe scale, where the
# whole body is in frame; pan is in frame fractions, so it survives the later zoom unchanged.
func _solve_center_pan(bounding_cube: AABB, longitude: float, latitude: float, roll: float,
		zoom: float, pan: Vector2) -> Vector2:
	_apply_pose(bounding_cube, longitude, latitude, roll, zoom, pan)
	var image := await _render()
	var used := image.get_used_rect()
	if used.size.x <= 0 or used.size.y <= 0:
		return Vector2.ZERO
	var center := Vector2(used.position) + Vector2(used.size) * 0.5
	var frame := Vector2(float(image.get_width()), float(image.get_height()))
	return (frame * 0.5 - center) / frame


# Returns the roll that puts the silhouette's long axis on the frame diagonal. The axis is
# undirected, so the 180-degree ambiguity is harmless: both answers are the same diagonal.
func _solve_diagonal_roll(bounding_cube: AABB, longitude: float, latitude: float, roll: float,
		zoom: float, pan: Vector2) -> float:
	var solved := roll
	for _iteration in 2:
		var image := await _render()
		# Screen angle (counter-clockwise, y up) is the negative of the image-space one.
		solved += DIAGONAL_ANGLE_DEG + _measure_silhouette_angle(image)
		_apply_pose(bounding_cube, longitude, latitude, solved, zoom, pan)
	return solved


# Major-axis angle of the alpha mask in degrees, clockwise from frame right (image
# coordinates, y down). Second moments rather than a bounding box: a box reports the
# frame's own diagonal for anything already tilted.
func _measure_silhouette_angle(source: Image) -> float:
	var image := Image.new()
	image.copy_from(source)
	image.resize(MOMENT_SIZE, MOMENT_SIZE, Image.INTERPOLATE_LANCZOS)
	var total := 0.0
	var sum_x := 0.0
	var sum_y := 0.0
	for y in MOMENT_SIZE:
		for x in MOMENT_SIZE:
			var weight := image.get_pixel(x, y).a
			total += weight
			sum_x += weight * x
			sum_y += weight * y
	if total <= 0.0:
		return 0.0
	var mean_x := sum_x / total
	var mean_y := sum_y / total
	var moment_xx := 0.0
	var moment_yy := 0.0
	var moment_xy := 0.0
	for y in MOMENT_SIZE:
		for x in MOMENT_SIZE:
			var weight := image.get_pixel(x, y).a
			var offset_x := x - mean_x
			var offset_y := y - mean_y
			moment_xx += weight * offset_x * offset_x
			moment_yy += weight * offset_y * offset_y
			moment_xy += weight * offset_x * offset_y
	return rad_to_deg(0.5 * atan2(2.0 * moment_xy, moment_xx - moment_yy))


# Fraction of the body's own texels with a channel at the 8-bit ceiling.
func _measure_clip_fraction(image: Image) -> float:
	var covered := 0
	var clipped := 0
	for y in image.get_height():
		for x in image.get_width():
			var color := image.get_pixel(x, y)
			if color.a < 0.5:
				continue
			covered += 1
			if maxf(color.r, maxf(color.g, color.b)) >= CLIP_LEVEL:
				clipped += 1
	if covered == 0:
		return 0.0
	return float(clipped) / float(covered)


func _measure(image: Image) -> Dictionary:
	var used := image.get_used_rect()
	var covered := 0
	var sum := Vector3.ZERO
	var peak := Vector3.ZERO
	for y in image.get_height():
		for x in image.get_width():
			var color := image.get_pixel(x, y)
			if color.a < 0.5:
				continue
			covered += 1
			sum += Vector3(color.r, color.g, color.b)
			peak = Vector3(maxf(peak.x, color.r), maxf(peak.y, color.g), maxf(peak.z, color.b))
	var pixels := image.get_width() * image.get_height()
	var mean := Vector3.ZERO if covered == 0 else sum / covered
	return {
		"used_rect": [used.position.x, used.position.y, used.size.x, used.size.y],
		"extent_fraction": maxf(float(used.size.x) / float(image.get_width()),
				float(used.size.y) / float(image.get_height())),
		"covered_fraction": float(covered) / float(pixels),
		"mean_rgb": [mean.x, mean.y, mean.z],
		"peak_rgb": [peak.x, peak.y, peak.z],
		"clip_fraction": _measure_clip_fraction(image),
	}


# Hides every shell whose shells.tsv tag is named, LIMB by default. An atmosphere limb is
# additive glow that is invisible at an icon's exposure, but it draws ALPHA on a transparent
# background, so what it actually contributes is a ring of near-black opaque texels outside
# the disc -- a black border, and a silhouette the fill above would then fit to.
func _hide_shells_by_tag(body_name: StringName, value: Variant) -> void:
	var tags: Array = ["LIMB"]
	if typeof(value) == TYPE_ARRAY:
		tags = value
	if tags.is_empty():
		return
	var asset_preloader: IVAssetPreloader = IVGlobal.program[&"AssetPreloader"]
	var specs := asset_preloader.get_body_shell_specs(body_name)
	var shells := _get_shells()
	for index in mini(specs.size(), shells.size()):
		var spec: Dictionary = specs[index]
		if tags.has(str(spec.get(&"tag", ""))):
			shells[index].visible = false


func _get_shells() -> Array[IVShellsModel]:
	var shells: Array[IVShellsModel] = []
	var surface := _capturer.get_staged_model() as IVShellsModel
	if !surface:
		return shells
	shells.append(surface)
	for child in surface.get_children():
		var shell := child as IVShellsModel
		if shell:
			shells.append(shell)
	return shells


func _apply_shell_visibility(value: Variant) -> void:
	if typeof(value) != TYPE_ARRAY:
		return
	var flags: Array = value
	var shells := _get_shells()
	for index in mini(flags.size(), shells.size()):
		shells[index].visible = _to_bool(flags[index], true)


# The disc-photometry laws (minnaert_k, lunar_lambert) and the ring/eclipse occlusion take
# the sun from a sun_direction uniform, which IVSunOcclusionManager feeds per frame to the
# LIVE body's materials only -- a staged visual is a separate node tree the manager never
# sees, so its uniform sits at the shader default. Feed it the rig's own key light, or a
# body with a photometric law is shaded against one sun and lit by another.
# [param toward_sun] is a unit vector in the capture world's space.
func _set_sun_direction(node: Node, toward_sun: Vector3) -> void:
	var mesh_instance := node as MeshInstance3D
	if mesh_instance:
		var material := mesh_instance.get_surface_override_material(0) as ShaderMaterial
		if material:
			material.set_shader_parameter(&"sun_direction", toward_sun)
	for child in node.get_children():
		_set_sun_direction(child, toward_sun)


func _get_albedo(body: IVBody) -> float:
	var albedo_variant: Variant = body.characteristics.get(&"albedo")
	if typeof(albedo_variant) == TYPE_FLOAT:
		var albedo: float = albedo_variant
		if albedo > 0.0:
			return albedo
	return DEFAULT_ALBEDO


func _get_metering_key() -> float:
	var exposure_manager: IVExposureManager = IVGlobal.program.get(&"ExposureManager")
	if exposure_manager:
		return exposure_manager.metering_key
	return FALLBACK_METERING_KEY


# JSON-RPC hands every number back as a Variant, and an integral one arrives as TYPE_INT
# however it was written, so each read has to accept both and fall back rather than convert.
func _to_float(value: Variant, fallback: float) -> float:
	if typeof(value) == TYPE_FLOAT:
		var as_float: float = value
		return as_float
	if typeof(value) == TYPE_INT:
		var as_int: int = value
		return float(as_int)
	return fallback


func _to_bool(value: Variant, fallback: bool) -> bool:
	if typeof(value) == TYPE_BOOL:
		var as_bool: bool = value
		return as_bool
	if typeof(value) == TYPE_INT:
		var as_int: int = value
		return as_int != 0
	return fallback


func _read_float(params: Dictionary, key: String, fallback: float) -> float:
	return _to_float(params.get(key), fallback)


func _read_int(params: Dictionary, key: String, fallback: int) -> int:
	return int(_read_float(params, key, float(fallback)))


func _read_bool(params: Dictionary, key: String, fallback: bool) -> bool:
	return _to_bool(params.get(key), fallback)


func _parse_pan(value: Variant) -> Vector2:
	if typeof(value) != TYPE_ARRAY:
		return Vector2.ZERO
	var pan: Array = value
	if pan.size() != 2:
		return Vector2.ZERO
	return Vector2(_to_float(pan[0], 0.0), _to_float(pan[1], 0.0))


func _fail(message: String) -> void:
	if _capturer:
		_capturer.clear_visual()
	_release_self_luminous_scale()
	_capture_result = {"_error": {"code": ERR_INVALID_PARAMS, "message": message}}
	_capture_running = false


# Two awaits, then read back — mirroring IVBody2DCapturer._render_once(): the viewport
# draws continuously, so one frame_post_draw can return inside a frame whose draw already
# happened and hand back the previous contents.
func _render() -> Image:
	await RenderingServer.frame_post_draw
	await RenderingServer.frame_post_draw
	var image := _viewport.get_texture().get_image()
	if image.get_format() != Image.FORMAT_RGBA8:
		image.convert(Image.FORMAT_RGBA8)
	return image


func _build_rig(render_size: Vector2i) -> void:
	if _viewport:
		_viewport.size = render_size
		return
	_viewport = SubViewport.new()
	_viewport.name = &"Body2DIconViewport"
	_viewport.own_world_3d = true
	_viewport.transparent_bg = true
	_viewport.msaa_3d = Viewport.MSAA_4X
	_viewport.size = render_size
	_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	_camera = Camera3D.new()
	_camera.current = true
	_viewport.add_child(_camera)
	_key_light = DirectionalLight3D.new()
	_viewport.add_child(_key_light)
	_fill_light = DirectionalLight3D.new()
	_viewport.add_child(_fill_light)
	_roll_pivot = Node3D.new()
	_viewport.add_child(_roll_pivot)
	_yaw_pivot = Node3D.new()
	_roll_pivot.add_child(_yaw_pivot)
	_pitch_pivot = Node3D.new()
	_yaw_pivot.add_child(_pitch_pivot)
	_spin_pivot = Node3D.new()
	_pitch_pivot.add_child(_spin_pivot)
	_server.add_child(_viewport)
	_capturer = IVBody2DCapturer.new()
	_capturer.bind_nodes(_viewport, _camera, _key_light, _fill_light, _yaw_pivot, _pitch_pivot,
			_spin_pivot)


# Self-luminous output -- a star's photosphere, a body's emission map -- is scaled by
# rendering-server globals that IVExposureManager rewrites EVERY FRAME from what the app's
# own camera happens to be metering. An icon that inherited those would depend on where the
# simulator's camera was parked, so the manager is stopped for the render and the globals
# pinned: 0.0 (the default) suppresses self-luminous contributions outright, which is what a
# body lit at near-zero phase wants, and a star sets its own. iv_limb_scale and
# iv_emission_energy_scale are written once at activation, not per frame, so stopping the
# manager holds them at their live values rather than reverting the limb glow to 50x.
func _hold_self_luminous_scale(exposure: float) -> void:
	_exposure_manager = IVGlobal.program.get(&"ExposureManager")
	if !_exposure_manager:
		return
	_exposure_process_mode = _exposure_manager.process_mode
	_exposure_manager.process_mode = Node.PROCESS_MODE_DISABLED
	RenderingServer.global_shader_parameter_set(&"iv_exposure", exposure)
	RenderingServer.global_shader_parameter_set(&"iv_emission_luminance_scale",
			exposure * IVExposureManager.gain)


func _release_self_luminous_scale() -> void:
	if !_exposure_manager:
		return
	_exposure_manager.process_mode = _exposure_process_mode
	_exposure_manager = null


# Engine ambient is what reaches a packed craft model, whose StandardMaterial3D takes no
# ambient_light uniform; the body shaders take that uniform instead (set_ambient).
func _set_environment_ambient(energy: float) -> void:
	if energy <= 0.0:
		if _world_environment:
			_world_environment.queue_free()
			_world_environment = null
		return
	if !_world_environment:
		var environment := Environment.new()
		environment.background_mode = Environment.BG_CLEAR_COLOR
		environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
		environment.ambient_light_color = Color.WHITE
		_world_environment = WorldEnvironment.new()
		_world_environment.environment = environment
		_viewport.add_child(_world_environment)
	_world_environment.environment.ambient_light_energy = energy


func _free_rig() -> void:
	if _capturer:
		_capturer.clear_visual()
		_capturer = null
	if _viewport:
		_viewport.queue_free()
		_viewport = null
	_camera = null
	_key_light = null
	_fill_light = null
	_roll_pivot = null
	_yaw_pivot = null
	_pitch_pivot = null
	_spin_pivot = null
	_world_environment = null
	_release_self_luminous_scale()
	_capture_running = false
	_capture_result = {}
