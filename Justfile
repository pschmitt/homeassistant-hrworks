set shell := ["bash", "-euo", "pipefail", "-c"]

format:
    uvx ruff format .

lint:
    uvx ruff check .
