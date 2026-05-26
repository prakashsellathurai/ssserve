# ssserve

(spelled hisss serve)

Python port of [vercel/serve](https://github.com/vercel/serve) — static file serving and directory listing. (vibecoded)


## Run

run directly without installing:

```bash
uvx ssserve
```

## Install

```bash
uv tool install ssserve
```


## Usage

```bash
ssserve [path] [options]
```

Defaults to current directory and port 3000.

### Options

| Flag | Description |
|---|---|
| `-l, --listen URI` | Listen endpoint (default: `tcp://0.0.0.0:3000`, multi-allowed) |
| `-s, --single` | SPA mode — rewrite 404s to `index.html` |
| `-C, --cors` | Enable CORS headers |
| `-u, --no-compression` | Disable gzip compression |
| `-S, --symlinks` | Resolve symlinks |
| `-L, --no-request-logging` | Disable request logging |
| `-c, --config PATH` | Path to `serve.json` |
| `--no-etag` | Disable ETag (use `Last-Modified`) |
| `--ssl-cert FILE` | SSL certificate (PEM) |
| `--ssl-key FILE` | SSL private key (PEM) |
| `--ssl-pass FILE` | SSL passphrase file |
| `--no-port-switching` | Don't auto-switch if port is taken |
| `-d, --debug` | Debug output |
| `--version` | Show version |
| `--help` | Show help |

### Examples

```bash
# Serve current directory on port 3000
ssserve

# Serve specific directory on port 5000 with CORS
ssserve -l 5000 -C ./my-site

# Serve with HTTPS
ssserve --ssl-cert cert.pem --ssl-key key.pem

# SPA mode for React/Vue apps
ssserve -s dist

# Multiple listeners
ssserve -l tcp://0.0.0.0:3000 -l unix:/tmp/serve.sock
```

## Configuration

Create a `serve.json` file in the served directory:

```json
{
  "public": "_site",
  "cleanUrls": true,
  "rewrites": [
    { "source": "/api/**", "destination": "/index.html" }
  ],
  "redirects": [
    { "source": "/old", "destination": "/new", "type": 302 }
  ],
  "headers": [
    {
      "source": "**/*.@(jpg|png)",
      "headers": [
        { "key": "Cache-Control", "value": "max-age=7200" }
      ]
    }
  ],
  "directoryListing": false,
  "trailingSlash": true,
  "etag": true,
  "symlinks": false,
  "renderSingle": false
}
```

## Testing

E2E tests verify behaviour, performance, and resource consumption against a live server process.

```bash
# Run all tests
uv run pytest tests/ -v

# Run only e2e tests
uv run pytest tests/e2e/ -v
```

### Test layout

| File | Tests | What it covers |
|---|---|---|
| `tests/e2e/test_behavior.py` | 47 | File serving, directory listing, clean URLs, redirects, rewrites, CORS, gzip, ETag/304, Range requests, SPA mode, custom headers, symlinks, path traversal |
| `tests/e2e/test_performance.py` | 8 | Latency (p50/p95/p99), TTFB, gzip vs raw, concurrent throughput — measure-only |
| `tests/e2e/test_resources.py` | 7 | RSS memory, CPU usage, file descriptor count — measure-only |

Dependencies: `pytest`, `psutil` (see `[dependency-groups]` in `pyproject.toml`).

Tests run automatically on CI via `.github/workflows/tests.yml`.

## Benchmarks

Micro-benchmarks using [airspeed velocity](https://asv.readthedocs.io/) track performance of core operations across commits:

sserve [benchmark](https://prakashsellathurai.com/ssserve/benchmarks/)

| Benchmark | What it measures |
|---|---|
| `TimeRouteToRegex` | Pattern compilation for route matching |
| `TimeMatchGlob` | Glob matching and exclusion lists |
| `TimeParseByteRange` | HTTP Range header parsing |
| `TimeFormatSize` | File size formatting |
| `TimeFormatDate` | Date formatting |
| `TimeParseListen` | Listen URI parsing |
| `TimeMergeConfig` | Config merging from `serve.json` |
| `TimeRenderListing` | HTML directory listing rendering (100 files) |

```bash
# Validate benchmarks
uv run asv check --python=same

# Quick run
uv run asv run --python=same --quick

# Full run across configured pythons
uv run asv run
```

Benchmarks run automatically on CI via `.github/workflows/benchmarks.yml`.

## License

MIT
