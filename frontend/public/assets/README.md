Adventure cover files referenced by metadata as `assets/<filename>` belong in this directory.
Vite serves them below `/ui/assets/` during development and copies them to the production
frontend bundle, where the ASGI app serves the same `/ui/assets/` path.
