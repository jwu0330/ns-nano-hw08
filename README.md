# Neurosynth Dissociation API

FastAPI backend for NTU Psychoinformatics HW08, built on
[spiketren/ns-nano](https://github.com/spiketren/ns-nano). It answers
**functional dissociation** queries on the Neurosynth database: which studies
mention A but not B, for terms or MNI coordinates.

**Live:** https://ns-nano-hw08.onrender.com — interactive docs at
[`/docs`](https://ns-nano-hw08.onrender.com/docs). The free instance sleeps
when idle; the first request can take up to a minute.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /dissociate/terms/{term_a}/{term_b}` | studies that mention `term_a` but not `term_b` |
| `GET /dissociate/locations/{x1_y1_z1}/{x2_y2_z2}` | studies with a focus near `[x1,y1,z1]` but none near `[x2,y2,z2]` |
| `GET /terms/{term}/studies` · `GET /locations/{x_y_z}/studies` | one set only |
| `GET /terms?q=cingul` | vocabulary search |
| `GET /` · `/img` · `/test_db` · `/health` | web page, template smoke tests, health check |

Options for the dissociation endpoints:

| Parameter | Default | Meaning |
|---|---|---|
| `threshold` | `0.001` | a study mentions a term when its TF-IDF weight is above this (Neurosynth convention) |
| `radius` | `6` | mm; a study mentions a location when it reports a focus within this distance (neurosynth package default) |
| `both` | `false` | also return B but not A |
| `analyze` | `false` | add the terms (and, for term queries, the 10 mm brain cells) that best separate the two sides, ranked by log-odds-ratio z score |
| `limit`, `offset` | all, `0` | paging of the study lists (newest first) |

Terms may use `_` for spaces (`posterior_cingulate`). Unknown terms return 404
with suggestions; malformed input returns 400 with a hint.

```bash
curl "https://ns-nano-hw08.onrender.com/dissociate/terms/posterior_cingulate/ventromedial_prefrontal?both=true&limit=3"
curl "https://ns-nano-hw08.onrender.com/dissociate/locations/0_-52_26/-2_50_-6?both=true&analyze=true&limit=3"
```

Response shape:

```json
{
  "query":  {"type": "locations", "a": [0, -52, 26], "b": [-2, 50, -6], "radius_mm": 6.0},
  "counts": {"a": 479, "b": 497, "a_and_b": 73, "a_not_b": 406, "b_not_a": 424},
  "a_not_b": {"count": 406, "studies": [{"study_id": "...", "title": "...", "authors": "...", "journal": "...", "year": 2015}]},
  "b_not_a": {"count": 424, "studies": ["..."]}
}
```

## Differences from the template

The template files are not modified; these are fixed in this repo's own code:

1. **Term names.** The template's `create_db.py` strips the column prefix with
   `^terms_[^_]*__`, which never matches `terms_abstract_tfidf__…`, so its
   database stores `terms_abstract_tfidf__amygdala` and a query for `amygdala`
   finds nothing. `scripts/load_db.py` stores the bare term.
2. **Driver.** SQLAlchemy 2.1 defaults to psycopg 3, but the template installs
   psycopg2, so a fresh install fails with `No module named 'psycopg'`. This
   repo pins versions and names the driver explicitly.
3. **Geometry.** MNI coordinates are millimetres, so points use SRID 0 instead
   of 4326 (longitude/latitude), with an n-D GiST index that `ST_3DDWithin`
   can use.
4. **Atomic reload.** Data is loaded into a staging schema, checked against the
   parquet row counts, and swapped in with one transaction.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DB_URL="postgresql://USER:PASSWORD@HOST:5432/DBNAME"   # PostgreSQL 12+ with PostGIS
export NS_DATA_DIR=/path/to/ns-nano                           # clone of spiketren/ns-nano (parquet files)
python scripts/load_db.py
uvicorn app:app --reload                                      # http://127.0.0.1:8000
pytest                                                        # needs DB_URL and NS_DATA_DIR
```

`tests/reference.py` recomputes every answer from the parquet files with
pandas alone; `tests/test_oracle.py` checks the API against it study by study.

## Deploy on Render

`render.yaml` describes a free PostgreSQL and a free web service. Start
command: `gunicorn app:app -k uvicorn_worker.UvicornWorker --bind 0.0.0.0:$PORT`.
Load the data once with `scripts/load_db.py` using the database's external URL.

## License

MIT (see LICENSE), as the template. `nsapi/static/amygdala.gif` and the data come from
[spiketren/ns-nano](https://github.com/spiketren/ns-nano).
