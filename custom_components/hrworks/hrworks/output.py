"""Pretty TSV in a terminal, literal TSV in pipes, JSON without decoration."""

import csv
import io
import json
import sys
from collections.abc import Mapping

from rich.console import Console
from rich.table import Table
from rich.text import Text

COLORS = ("cyan", "green", "magenta", "white", "yellow", "blue")


def rows_from(data):
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        return [{"key": key, "value": value} for key, value in data.items()]
    return [{"value": data}]


def emit(data, *, as_json=False, no_color=False, rows=None):
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
        return
    rows = rows_from(data) if rows is None else rows
    headers = list(dict.fromkeys(key for row in rows for key in row))
    if not headers:
        headers = ["result"]
        rows = [{"result": "No data"}]

    def value(cell):
        if cell is None:
            return "N/A"
        if isinstance(cell, (dict, list)):
            return json.dumps(cell, ensure_ascii=False)
        if isinstance(cell, bool):
            return "yes" if cell else "no"
        return str(cell)

    if not sys.stdout.isatty():
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows([[value(row.get(key)) for key in headers] for row in rows])
        sys.stdout.write(stream.getvalue())
        return
    console = Console(no_color=no_color, highlight=False)
    table = Table(box=None, show_edge=False, pad_edge=False, header_style="bold", padding=(0, 1))
    for index, header in enumerate(headers):
        table.add_column(header.upper(), style=COLORS[index % len(COLORS)], overflow="fold")
    for row in rows:
        table.add_row(
            *[
                Text(
                    " ".join(value(row.get(key)).split()),
                    style="red bold" if str(row.get(key, "")).startswith(("-", "−")) else "",
                )
                for key in headers
            ]
        )
    console.print(table)
