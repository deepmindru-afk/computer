import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        frontend_dir = Path(self.root) / "cptr" / "frontend"
        subprocess.run(["bun", "install"], cwd=frontend_dir, check=True)
        subprocess.run(["bun", "run", "build"], cwd=frontend_dir, check=True)
