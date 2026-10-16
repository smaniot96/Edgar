# Adventure path confinement

Adventure slugs used as filesystem names follow `^[a-z0-9][a-z0-9_-]{0,254}$`, matching
the existing source-binding rule. This retains generated names and existing valid names such
as `legacy-adventure_2`. Reserved rules and campaign-lore collection names remain invalid
source bindings.

The metadata, PDF, attempt, generation-checkpoint and preparation-checkpoint path helpers
validate their slug or opaque identity before composing a path. They resolve existing path
components and reject traversal or symlink components, including metadata/PDF files and
per-adventure directories. Adventure GET, PATCH, DELETE and manifest handlers validate the
binding before using the metadata-file fallback. Collection and metadata listings omit names
that cannot be safe adventure bindings.

The old fallback construction, `ADVENTURES_DIR / f"{slug}.json"`, resolves a slug of
`../../outside` to `outside.json` above the configured adventure directory. The regression
test creates that file in a temporary directory, confirms the old expression points to it,
then verifies metadata read/write/lock helpers and route handlers reject that slug without
changing the sentinel. A separate test verifies an encoded slash traversal does not match the
FastAPI slug route. This reproduces the path-construction flaw with disposable files; it does
not establish that a percent-encoded slash reached the old HTTP handler through the deployed
ASGI route matcher.

Run the focused checks with:

```sh
uv run pytest tests/api/test_adventure_path_confinement.py tests/api/test_adventures_meta.py
uv run ruff check apps/api/services/adventures_meta.py apps/api/services/campaign_sources.py apps/api/services/adventure_manifests.py apps/api/routers/adventures.py apps/api/routers/adventure_manifests.py tests/api/test_adventure_path_confinement.py
```
