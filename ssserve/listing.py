import html
import math
import os
import time


def format_size(size: int) -> str:
    if size == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size, 1024))) if size > 0 else 0
    i = min(i, len(units) - 1)
    s = size / (1024**i)
    return f"{s:.1f} {units[i]}" if i > 0 else f"{s:.0f} B"


def format_date(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


LISTING_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Index of {path}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Oxygen,Ubuntu,Cantarell,sans-serif;font-size:14px;color:#333;background:#fafafa;padding:20px}}
h1{{font-weight:500;font-size:18px;margin-bottom:16px;color:#111}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-weight:500;font-size:12px;text-transform:uppercase;color:#888;padding:8px 12px;border-bottom:2px solid #eaeaea}}
td{{padding:8px 12px;border-bottom:1px solid #eee}}
tr:hover td{{background:#f5f5f5}}
a{{color:#067df7;text-decoration:none}}
a:hover{{text-decoration:underline}}
.icon{{width:20px;display:inline-block;text-align:center;margin-right:8px;color:#888}}
.size{{text-align:right;font-variant-numeric:tabular-nums;color:#666}}
.date{{color:#888}}
.folder .icon{{color:#f5a623}}
.file .icon{{color:#666}}
.parent{{font-weight:500}}
.footer{{margin-top:20px;font-size:12px;color:#999;text-align:center}}
</style>
</head>
<body>
<h1>Index of {path}</h1>
<table>
<tr><th>Name</th><th class="size">Size</th><th class="date">Modified</th></tr>
{parent_row}
{entries}
</table>
<div class="footer">ssserve · Python port of vercel/serve</div>
</body>
</html>"""


def render_listing(path: str, base_dir: str, entries: list[os.DirEntry]) -> str:
    normalized = "/" + path.strip("/")
    rows = []

    if path != "/":
        parent = "/" if normalized == "/" else normalized.rsplit("/", 1)[0] or "/"
        rows.append(
            '<tr class="parent">'
            f'<td><a href="{html.escape(parent)}"><span class="icon">&#128193;</span>..</a></td>'
            '<td class="size">-</td>'
            '<td class="date">-</td>'
            "</tr>"
        )

    for entry in sorted(entries, key=lambda e: (not e.is_dir(), e.name.lower())):
        name = entry.name
        href = normalized.rstrip("/") + "/" + name
        is_dir = entry.is_dir()

        if is_dir:
            icon = "&#128193;"
            size_display = "-"
            cls = "folder"
        else:
            icon = "&#128196;"
            try:
                stat = entry.stat()
                size_display = format_size(stat.st_size)
                date_display = format_date(stat.st_mtime)
            except OSError:
                size_display = "?"
                date_display = "-"
            cls = "file"

        try:
            stat = entry.stat()
            date_display = format_date(stat.st_mtime)
        except OSError:
            date_display = "-"

        dir_slash = "/" if is_dir else ""
        rows.append(
            f'<tr class="{cls}">'
            f'<td><a href="{html.escape(href)}"><span class="icon">{icon}</span>{html.escape(name)}{dir_slash}</a></td>'
            f'<td class="size">{size_display}</td>'
            f'<td class="date">{date_display}</td>'
            "</tr>"
        )

    return LISTING_TEMPLATE.format(
        path=html.escape(normalized),
        parent_row="\n".join(rows[:1]) if path != "/" else "",
        entries="\n".join(rows[1:] if path != "/" else rows),
    )
