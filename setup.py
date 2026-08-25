import os
import re
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class OptionalBuildExt(build_ext):
    """Allow extension building to fail gracefully on unsupported platforms."""

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as err:
            print(f"Warning: Optional C extension {ext.name} failed to build ({err}). Using Python fallback.")


ext_modules = []
if sys.platform.startswith("linux"):
    ext_modules = [
        Extension("ssserve._fastops", sources=["ssserve/_fastops.c"], libraries=["z"]),
        Extension("ssserve._server", sources=["ssserve/_server.c"], libraries=["pthread", "z"]),
    ]

with open("README.md") as f:
    readme = f.read()

# Extract description from README: first meaningful line after title/subtitle
desc = ""
lines = [l.strip() for l in readme.split("\n") if l.strip()]
for line in lines:
    if not line.startswith("#") and not line.startswith("(") and not line.startswith("["):
        desc = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", line)
        desc = re.sub(r"\s*\(.*?\)\s*", "", desc)
        desc = desc.strip(". ").strip()
        break

setup(
    description=desc,
    ext_modules=ext_modules,
    cmdclass={"build_ext": OptionalBuildExt},
)
