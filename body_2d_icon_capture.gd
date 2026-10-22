# body_2d_icon_capture.gd
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
class_name IVBody2DIconCapture
extends RefCounted

## Batch-captures body 2D icons; the in-sim half of
## [code]addons/tools/capture_body_icons.py[/code], which is where the pose
## specification, the file naming and the deploy live.
##
## Requires [code]addons/ivoyager_core[/code] (for [IVBodyVisual] and
## [IVBody2DCapturer]). A client calls these members through the
## [code]ivoyager_assistant[/code] plugin's [code]call[/code], addressing this object by
## its role, "Body2DIconCapture", once it is in the program:
## [code]program_refcounteds/Body2DIconCapture[/code] under
## [code][core_initializer][/code] in [code]ivoyager_override2.cfg[/code]. The Python
## driver adds that line itself and undoes it at exit. Every member waits for the
## simulator to start.[br][br]
##
## A body is staged exactly as the simulator draws it — cube shaders, cloud and limb
## shells, band-pattern bodies and packed craft models all come through
## [method IVBody.make_body_visual] — and posed by the point of the body facing the
## camera rather than by turntable angles, so a pose is a longitude and a latitude that
## survive a re-bake or a re-source. Lighting is the engine's own: a single directional
## source at [code]metering_key / albedo[/code] (a body's [code]meter_albedo[/code]
## where it has one), which is what [IVExposureManager] settles a body's light energy
## to once it fills the view.[br][br]
##
## Rendering needs frames, so a capture cannot answer within one call:
## [method capture_body_icon] starts one and returns immediately, and
## [method poll_icon_capture] returns the result once it lands.

const AUTOMATION_METHODS: Array[StringName] = [
	&"list_icon_bodies",
	&"capture_body_icon",
	&"poll_icon_capture",
	&"end_icon_capture",
]

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
## Camera distance in target radii — twice VIEW_ZOOM's, which is the app framing an icon is
## derived from.
##
## The projection has to be PERSPECTIVE for that, and it is not a cosmetic choice: a shader
## reading [code]VIEW[/code] gets it from the view-space POSITION, so under an orthographic
## projection a disc-photometry law's [code]mu[/code] reaches zero wherever the camera node
## happens to sit while the projection still draws the sphere out to its full radius. At the
## staging default of about 4 radii that printed a hard-edged black annulus 4 px wide on
## Venus, whose minnaert_k the law applies. Matching the app's projection removes it by
## construction, since mu then reaches zero exactly at the drawn silhouette.
##
## VIEW_ZOOM was measured back out of the app rather than read off the table: at focal length
## 24 mm (vertical fov 51.86 deg, Godot's KEEP_HEIGHT) a VIEW_ZOOM screenshot puts Mars' disc
## at an angular radius of 19.50 deg, i.e. 3.00 radii; Venus 2.97, Earth 3.11 (its limb shell
## inflates the measured disc). That agrees with views.tsv's view_position_z of 3.
##
## Icons are shot from TWICE that, because VIEW_ZOOM reads as wide-angle on a banded planet:
## at 3 radii a feature only 14 deg off the centre meridian already sits a third of the way
## out to the limb, which bulges Jupiter. 6 radii takes the same offset to 16.5 deg.
const VIEW_ZOOM_RADII := 6.0
## Focal length the app defaults to, in 35 mm-equivalent mm. Kept for the derivation above and
## nothing else: the fov below is solved to FRAME the body, so the focal length cancels out of
## the icon entirely and the distance alone sets the perspective.
const VIEW_ZOOM_FOCAL_LENGTH := 24.0
## The default of the optional array arguments. Godot reports only a constant as a
## parameter's default, and a client can omit an argument before one it gives only where
## the default is known.
const EMPTY: Array = []

# The Assistant's error codes (its SPECIFICATION.md section 3.3).
const ERR_INVALID_PARAMS := 2
const ERR_NOT_FOUND := 3
const ERR_NOT_ALLOWED := 5

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
var _farwarp_manager: Node
var _farwarp_process_mode := Node.PROCESS_MODE_INHERIT
var _capture_running := false
var _capture_result: Dictionary


func _init() -> void:
	IVStateManager.about_to_free_procedural_nodes.connect(_free_rig)


## Whether an automation client may use [param _member] now: every member waits for the
## simulator to start.
func is_automation_available(_member: StringName) -> bool:
	return IVStateManager.started


## Capturable bodies, each with its file prefix, albedo, model kind, size and shell tags,
## and the metering_key the captures are lit by.
func list_icon_bodies() -> Dictionary:
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


## Starts one capture of [param body] to the PNG at [param out_path] and returns at once;
## poll [method poll_icon_capture] for the result. A pose is the sub-camera
## [param longitude] and [param latitude] in degrees, a screen [param roll] (or
## [param auto_roll], laying an elongated body's long axis on the diagonal), and
## [param fill], [param zoom] and [param pan] (frame fractions) for the framing.
## capture_body_icons.py's module docstring specifies every argument, as the columns of
## its pose table; [param hide_shell_tags] is its hide_shells, [param pan] its pan_x and
## pan_y, and [param supersample], [param albedo] and [param metering_key] (0.0 takes the
## body's own and the engine's) are set here only.
func capture_body_icon(body: String, out_path: String, longitude := 0.0, latitude := 0.0,
		roll := 0.0, auto_roll := false, fill := 1.0, zoom := 1.0, pan := Vector2.ZERO,
		auto_center := true, light_left := 15.0, light_up := -10.0, brightness := 0.0,
		ev := 0.0, ev_auto := false, clip_limit := 0.01, camera_radii := VIEW_ZOOM_RADII,
		exposure := 0.0, ambient := 0.0, env_ambient := 0.0,
		hide_shell_tags: Array = EMPTY, shells: Array = EMPTY, shell_spin := 0.0,
		width := IVBody2DCapturer.ICON_SIZE, height := IVBody2DCapturer.ICON_SIZE,
		supersample := DEFAULT_SUPERSAMPLE, albedo := 0.0, metering_key := 0.0
		) -> Dictionary:
	if _capture_running:
		return _error(ERR_NOT_ALLOWED, "A capture is already running")
	var body_name := StringName(body)
	if !IVBody.bodies.has(body_name):
		return _error(ERR_NOT_FOUND, "No body %s" % body_name)
	if out_path.is_empty():
		return _error(ERR_INVALID_PARAMS, "Missing out_path")
	_capture_running = true
	_capture_result = {}
	# A render needs frames, so the capture runs on and publishes through _capture_result
	# and _capture_running for poll_icon_capture to collect.
	_run_capture(body_name, out_path, {
		"longitude": longitude,
		"latitude": latitude,
		"roll": roll,
		"auto_roll": auto_roll,
		"fill": fill,
		"zoom": zoom,
		"pan": pan,
		"auto_center": auto_center,
		"light_left": light_left,
		"light_up": light_up,
		"brightness": brightness,
		"ev": ev,
		"ev_auto": ev_auto,
		"clip_limit": clip_limit,
		"camera_radii": camera_radii,
		"exposure": exposure,
		"ambient": ambient,
		"env_ambient": env_ambient,
		"hide_shell_tags": hide_shell_tags,
		"shells": shells,
		"shell_spin": shell_spin,
		"width": width,
		"height": height,
		"supersample": supersample,
		"albedo": albedo,
		"metering_key": metering_key,
	})
	return {"started": true}


## The running capture's result once it has landed, with [code]done[/code] true: what was
## rendered and its measured fill, clipping and colour. Until then only
## [code]{"done": false}[/code]; a capture that failed answers its error.
func poll_icon_capture() -> Dictionary:
	if _capture_running:
		return {"done": false}
	if _capture_result.is_empty():
		return _error(ERR_INVALID_PARAMS, "No capture has been started")
	var result := _capture_result
	_capture_result = {}
	return result


## Frees the capture rig.
func end_icon_capture() -> Dictionary:
	_free_rig()
	return {"freed": true}


# The coroutine behind capture_body_icon(), whose arguments [param spec] holds.
func _run_capture(body_name: StringName, out_path: String, spec: Dictionary) -> void:
	var width: int = spec.width
	var height: int = spec.height
	var supersample: int = spec.supersample
	_build_rig(Vector2i(width * supersample, height * supersample))
	var body: IVBody = IVBody.bodies[body_name]
	var visual := body.make_body_visual()
	if !visual:
		_fail("%s has no model" % body_name)
		return
	var aabb := _capturer.stage_visual(visual)
	var hide_shell_tags: Array = spec.hide_shell_tags
	var shells: Array = spec.shells
	var shell_spin: float = spec.shell_spin
	_hide_shells_by_tag(body_name, hide_shell_tags)
	_apply_shell_visibility(shells)
	_apply_shell_spin(body_name, shell_spin)

	var albedo: float = spec.albedo
	if albedo <= 0.0:
		albedo = _get_albedo(body)
	var metering_key: float = spec.metering_key
	if metering_key <= 0.0:
		metering_key = _get_metering_key()
	var brightness: float = spec.brightness
	if brightness <= 0.0:
		brightness = metering_key / albedo
	_capturer.set_fill_light_enabled(false)
	var light_left: float = spec.light_left
	var light_up: float = spec.light_up
	var key_azimuth := deg_to_rad(-90.0 + light_left)
	# light_up is where the SOURCE sits relative to the camera axis, so the shine
	# direction takes the opposite elevation: a source above shines downward.
	var key_elevation := deg_to_rad(-light_up)
	_capturer.set_key_light(key_azimuth, key_elevation)
	_capturer.set_brightness(brightness)
	var ambient: float = spec.ambient
	var env_ambient: float = spec.env_ambient
	var exposure: float = spec.exposure
	_capturer.set_ambient(ambient)
	_set_environment_ambient(env_ambient)
	_hold_render_globals(exposure)

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
	var camera_radii: float = spec.camera_radii
	_view_distance = maxf(reference_radius * camera_radii, bound * 1.05)
	# The rig replaces the capturer's camera with a perspective one at its own distance, so a
	# star's disc has to be rescaled to it. Skipping this leaves the photosphere's alias fades
	# resolving against the staging camera and the Sun renders as a flat disc.
	visual.set_preview_camera_distance(_view_distance)
	bound = minf(bound, _view_distance * 0.999)
	_view_tangent = bound / sqrt(maxf(_view_distance * _view_distance - bound * bound, 1e-12))
	var longitude: float = spec.longitude
	var latitude: float = spec.latitude
	var roll: float = spec.roll
	var zoom: float = spec.zoom
	var pan: Vector2 = spec.pan
	_apply_pose(bounding_cube, longitude, latitude, roll, zoom, pan)

	var auto_roll: bool = spec.auto_roll
	if auto_roll:
		roll = await _solve_diagonal_roll(bounding_cube, longitude, latitude, roll, zoom, pan)
	# The measured fit runs at zoom 1 and the zoom column multiplies its answer, rather than
	# seeding it: a fit target above 1 can never be measured (the used rect saturates at the
	# frame), so cropping has to be expressed as a factor on a fit that did converge.
	var fill: float = spec.fill
	if fill > 0.0:
		var fitted := await _solve_fill_zoom(bounding_cube, longitude, latitude, roll, 1.0, pan)
		var auto_center: bool = spec.auto_center
		if auto_center:
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
	var ev: float = spec.ev
	var ev_auto: bool = spec.ev_auto
	if ev_auto:
		var unprotected := await _render()
		var clip_limit: float = spec.clip_limit
		if _measure_clip_fraction(unprotected) <= clip_limit:
			ev = 0.0
	if ev != 0.0:
		_capturer.set_brightness(brightness * 2.0 ** ev)

	var image := await _render()
	var report := _measure(image)
	# Resize first, then convert: the viewport hands back PREMULTIPLIED colour, which is the
	# only form a filter may average, and a PNG means straight alpha.
	image.resize(width, height, Image.INTERPOLATE_LANCZOS)
	IVBody2DCapturer.unpremultiply_alpha(image)
	var save_error := image.save_png(out_path)
	_capturer.clear_visual()
	_release_render_globals()
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
		var used := IVBody2DCapturer.get_silhouette_rect(image)
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
	var used := IVBody2DCapturer.get_silhouette_rect(image)
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
	var used := IVBody2DCapturer.get_silhouette_rect(image)
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
	var used := IVBody2DCapturer.get_silhouette_rect(image)
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


# Hides every shell whose shells.tsv tag is named; nothing by default.
func _hide_shells_by_tag(body_name: StringName, tags: Array) -> void:
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


func _apply_shell_visibility(flags: Array) -> void:
	var shells := _get_shells()
	for index in mini(flags.size(), shells.size()):
		shells[index].visible = _to_bool(flags[index], true)


# Poses a drifting shell about the body's polar axis, degrees east of the phase its map was
# built at, whatever the capture's date.
#
# Only a shell whose shells.tsv 'process' is _rotate may be turned, and that is the whole
# rule: such a shell has no orientation to preserve, its angle being wherever the sim clock
# has carried it, so the 0 of its map is one arbitrary phase among many rather than a
# registration. A shell without it is fixed to the body, and turning it would slide a map
# off the surface it was built against.
#
# A built shell poses itself at the clock's phase, IVShellsModel.get_spin(), and a static
# preview never moves it again, so the turn is taken from there. The same goes for shell 0's
# cloud-shadow lookup, which was aimed at the deck's built phase and must follow it here or
# the shadows are cast by clouds the icon does not show.
#
# Same call as IVShellsModel._rotate, so it inherits that convention exactly: an overlay
# shell's own basis is a pure scale and its parent carries the body's model basis, which is
# what makes rotate_y() the body's own polar axis.
func _apply_shell_spin(body_name: StringName, spin_deg: float) -> void:
	var spin := deg_to_rad(spin_deg)
	var asset_preloader: IVAssetPreloader = IVGlobal.program[&"AssetPreloader"]
	var specs := asset_preloader.get_body_shell_specs(body_name)
	var shells := _get_shells()
	for index in mini(specs.size(), shells.size()):
		var spec: Dictionary = specs[index]
		var process_args: Array = spec.get(&"process_args", [])
		if spec.get(&"process", &"") != &"_rotate" or !process_args:
			continue
		var rate: float = process_args[0]
		var built_spin := IVShellsModel.get_spin(rate)
		shells[index].rotate_y(spin - built_spin)
		var overrides: Dictionary = spec.get(&"overrides", {})
		if !overrides.get(&"clouds_two_stream_map", false):
			continue
		var surface_material := shells[0].get_surface_override_material(0) as ShaderMaterial
		if surface_material:
			surface_material.set_shader_parameter(&"clouds_shadow_spin",
					Vector2(cos(spin), sin(spin)))


# meter_albedo ahead of albedo, the order IVExposureManager._get_albedo() takes. An icon
# is lit at the energy the app's own metering settles on, so a body whose two columns
# differ is lit wrongly by their whole ratio if this reads only the catalog value.
func _get_albedo(body: IVBody) -> float:
	for field: StringName in [&"meter_albedo", &"albedo"]:
		var albedo_variant: Variant = body.characteristics.get(field)
		if typeof(albedo_variant) != TYPE_FLOAT:
			continue
		var albedo: float = albedo_variant
		if albedo > 0.0:
			return albedo
	return DEFAULT_ALBEDO


func _get_metering_key() -> float:
	var exposure_manager: IVExposureManager = IVGlobal.program.get(&"ExposureManager")
	if exposure_manager:
		return exposure_manager.metering_key
	return FALLBACK_METERING_KEY


# A shells flag arrives as JSON gave it: a bool, or written as a number, an int.
func _to_bool(value: Variant, fallback: bool) -> bool:
	if typeof(value) == TYPE_BOOL:
		var as_bool: bool = value
		return as_bool
	if typeof(value) == TYPE_INT:
		var as_int: int = value
		return as_int != 0
	return fallback


func _fail(message: String) -> void:
	if _capturer:
		_capturer.clear_visual()
	_release_render_globals()
	_capture_result = _error(ERR_INVALID_PARAMS, message)
	_capture_running = false


func _error(code: int, message: String) -> Dictionary:
	return {"_error": {"code": code, "message": message}}


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
	IVGlobal.get_tree().root.add_child(_viewport)
	_capturer = IVBody2DCapturer.new()
	_capturer.bind_nodes(_viewport, _camera, _key_light, _fill_light, _yaw_pivot, _pitch_pivot,
			_spin_pivot)


# Rendering-server globals that a manager rewrites EVERY FRAME from what the app's own camera
# happens to be doing. An icon that inherited one would depend on where the simulator's camera
# was parked, so each manager is stopped for the render and its global pinned.
#
# iv_exposure and iv_emission_luminance_scale scale SELF-LUMINOUS output -- a star's
# photosphere, a body's emission map: 0.0 (the default) suppresses it outright, which is what
# a body lit at near-zero phase wants, and a star sets its own. iv_limb_scale and
# iv_emission_energy_scale are written once at activation, not per frame, so stopping the
# exposure manager holds them at their live values rather than reverting the limb glow to 50x.
#
# iv_farwarp_start is the distance beyond which a vertex is pulled inward, and IVFarwarpManager
# sets it to the LIVE camera's distance to its own parent x 1e4. The rig stages a body tens of
# thousands of km from its own camera, so a simulator camera parked close to something small --
# a spacecraft, whose reference radius is metres -- compresses the whole rig to a fraction of
# its near plane and the icon renders empty. 0.0 disables the remap, which is what a rig whose
# geometry all sits at one distance wants.
func _hold_render_globals(exposure: float) -> void:
	_exposure_manager = IVGlobal.program.get(&"ExposureManager")
	if _exposure_manager:
		_exposure_process_mode = _exposure_manager.process_mode
		_exposure_manager.process_mode = Node.PROCESS_MODE_DISABLED
		RenderingServer.global_shader_parameter_set(&"iv_exposure", exposure)
		RenderingServer.global_shader_parameter_set(&"iv_emission_luminance_scale",
				exposure * IVExposureManager.gain)
	_farwarp_manager = IVGlobal.program.get(&"FarwarpManager")
	if _farwarp_manager:
		_farwarp_process_mode = _farwarp_manager.process_mode
		_farwarp_manager.process_mode = Node.PROCESS_MODE_DISABLED
		RenderingServer.global_shader_parameter_set(&"iv_farwarp_start", 0.0)


func _release_render_globals() -> void:
	if _exposure_manager:
		_exposure_manager.process_mode = _exposure_process_mode
		_exposure_manager = null
	if _farwarp_manager:
		_farwarp_manager.process_mode = _farwarp_process_mode
		_farwarp_manager = null


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
	_release_render_globals()
	_capture_running = false
	_capture_result = {}
