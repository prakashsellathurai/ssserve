from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Rewrite:
    source: str
    destination: str


@dataclass
class Redirect:
    source: str
    destination: str
    type: int = 301


@dataclass
class HeaderRule:
    source: str
    headers: list[dict[str, str | None]]


@dataclass
class Config:
    public: str | None = None
    clean_urls: bool | list[str] = True
    rewrites: list[Rewrite] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    headers: list[HeaderRule] = field(default_factory=list)
    directory_listing: bool | list[str] = True
    unlisted: list[str] = field(default_factory=lambda: [".DS_Store", ".git"])
    trailing_slash: bool | None = None
    render_single: bool = False
    symlinks: bool = False
    etag: bool = False

    @classmethod
    def defaults(cls) -> Config:
        return cls()


def merge_config(base: Config, overrides: dict) -> Config:
    kw = {}
    for key, val in overrides.items():
        match key:
            case "public":
                kw["public"] = str(val) if val else None
            case "cleanUrls":
                if isinstance(val, bool):
                    kw["clean_urls"] = val
                elif isinstance(val, list):
                    kw["clean_urls"] = val
            case "rewrites":
                kw["rewrites"] = [Rewrite(**r) for r in val]
            case "redirects":
                kw["redirects"] = [Redirect(**r) for r in val]
            case "headers":
                kw["headers"] = [HeaderRule(**h) for h in val]
            case "directoryListing":
                if isinstance(val, bool):
                    kw["directory_listing"] = val
                elif isinstance(val, list):
                    kw["directory_listing"] = val
            case "unlisted":
                kw["unlisted"] = list(val)
            case "trailingSlash":
                kw["trailing_slash"] = bool(val)
            case "renderSingle":
                kw["render_single"] = bool(val)
            case "symlinks":
                kw["symlinks"] = bool(val)
            case "etag":
                kw["etag"] = bool(val)
    return Config(**kw)


def load_config(path: str | None, base_dir: str | None = None) -> Config:
    if path:
        config_path = Path(path)
    else:
        search_dir = base_dir or os.getcwd()
        for name in ("serve.json",):
            p = Path(search_dir) / name
            if p.exists():
                config_path = p
                break
        else:
            return Config.defaults()

    if not config_path.exists():
        return Config.defaults()

    with open(config_path) as f:
        data = json.load(f)

    return merge_config(Config.defaults(), data)
