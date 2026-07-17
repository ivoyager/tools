# _project.py
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
"""Locate the Godot project hosting this 'tools' submodule.

Scripts here reach project files (the Core plugin's tables, ivoyager_assets) by
climbing from their own path, so the submodule's install location is load-bearing:
it must be <project>/addons/tools. Installed anywhere else, the climb lands outside
the project, where every path derived from it is well-formed but wrong -- a script
would read nothing, or write an output tree, somewhere unrelated. project_dir()
fails there instead, naming the required location.
"""

import pathlib
import sys

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_DIR = _TOOLS_DIR.parent.parent


def project_dir():
    """Return the hosting Godot project's directory. Exits the process, naming the
    required install location, if this submodule is not at <project>/addons/tools."""
    if not (_PROJECT_DIR / "project.godot").is_file():
        sys.exit(f"No Godot project at '{_PROJECT_DIR}' (no project.godot).\n"
                 f"The 'tools' submodule must be installed at <project>/addons/tools, "
                 f"but is at '{_TOOLS_DIR}'.")
    return _PROJECT_DIR
