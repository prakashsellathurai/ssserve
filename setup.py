import re

from setuptools import setup

with open("README.md") as f:
    readme = f.read()

# Extract description from README: first meaningful line after title/subtitle
lines = [l.strip() for l in readme.split("\n") if l.strip()]
for line in lines:
    if not line.startswith("#") and not line.startswith("(") and not line.startswith("["):
        desc = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", line)
        desc = re.sub(r"\s*\(.*?\)\s*", "", desc)
        desc = desc.strip(". ").strip()
        setup(description=desc)
        break
