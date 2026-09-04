# shader_compile_timer.gd
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
extends Node3D

## Times one shader's first draw; the in-engine half of
## [code]addons/tools/time_shader_compiles.py[/code], which is where the
## specification, the generated project and the cache busting live.
##
## Not part of the hosting project's scene tree and never added to it: the driver
## copies this into a throwaway project whose main scene it is, and runs one
## process per shader. Takes [code]--shader=<name>[/code] after [code]--[/code],
## naming a file in that project's [code]shaders/[/code], and prints one
## [code]SHADER_TIMER[/code] line before quitting.

## Frames the trivial shader is left to draw. Its own first draw also pays the
## probe quad's, which is why the measured shader is never the first drawn.
const SETTLE_FRAMES := 3
## Quad width, as a fraction of its distance from the camera.
const QUAD_SIZE_FRACTION := 0.5
const TRIVIAL_SHADER_CODE := "shader_type spatial;\nvoid fragment() {\n\tALBEDO = vec3(0.5);\n}\n"

var _light: DirectionalLight3D
var _material: ShaderMaterial
var _shader_name := ""
var _frame := 0
var _last_usec := 0
var _first_draw_msec := 0.0


func _ready() -> void:
	_shader_name = _get_shader_name_argument()
	if _shader_name.is_empty():
		_fail("no --shader=<name> argument")
		return
	var camera := Camera3D.new()
	add_child(camera)
	camera.current = true
	_light = DirectionalLight3D.new()
	_light.shadow_enabled = false
	add_child(_light)
	_material = ShaderMaterial.new()
	_material.shader = _make_trivial_shader()
	var distance := camera.near * 4.0
	var mesh := QuadMesh.new()
	mesh.size = Vector2.ONE * distance * QUAD_SIZE_FRACTION
	var quad := MeshInstance3D.new()
	quad.mesh = mesh
	quad.material_override = _material
	quad.position = Vector3(0.0, 0.0, -distance)
	camera.add_child(quad)
	_last_usec = Time.get_ticks_usec()


func _process(_delta: float) -> void:
	# A frame's cost is only known once the next one begins, so the shader is
	# assigned on one frame and its duration read on the following one.
	var now := Time.get_ticks_usec()
	var duration_msec := (now - _last_usec) / 1000.0
	_last_usec = now
	_frame += 1
	match _frame:
		SETTLE_FRAMES:
			var shader := _load_measured_shader()
			if !shader:
				return
			_material.shader = shader
		SETTLE_FRAMES + 1:
			_first_draw_msec = duration_msec
			_light.visible = false # forces one more specialization of a lit shader
		SETTLE_FRAMES + 2:
			print("SHADER_TIMER %s first_ms=%.0f spec_ms=%.0f" % [_shader_name,
					_first_draw_msec, duration_msec])
			get_tree().quit()


func _get_shader_name_argument() -> String:
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--shader="):
			return argument.trim_prefix("--shader=")
	return ""


func _make_trivial_shader() -> Shader:
	var shader := Shader.new()
	shader.code = TRIVIAL_SHADER_CODE
	return shader


func _load_measured_shader() -> Shader:
	# Loaded from a file rather than built at runtime: a runtime Shader has no
	# resource path, so the relative #includes the shipped files use cannot
	# resolve. This is why the driver copies the whole shaders directory.
	var path := "res://shaders/%s.gdshader" % _shader_name
	if !ResourceLoader.exists(path):
		_fail("no such shader '%s'" % path)
		return null
	var shader: Shader = load(path)
	if !shader:
		_fail("could not load '%s'" % path)
		return null
	if shader.get_mode() != Shader.MODE_SPATIAL:
		_fail("'%s' is not a spatial shader; it cannot go on a mesh" % _shader_name)
		return null
	return shader


func _fail(message: String) -> void:
	printerr("SHADER_TIMER_ERROR %s" % message)
	get_tree().quit(1)
