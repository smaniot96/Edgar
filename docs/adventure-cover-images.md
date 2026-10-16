# Adventure cover image contract

`cover_image` is optional metadata for a decorative, browser-visible image. Store artwork in
`frontend/public/assets/` and refer to it as `assets/<filename>` or a nested path below that
directory. This form follows Vite's configured base (`/ui/`) in development and production.
Legacy values `ui/assets/<path>` and `/ui/assets/<path>` remain supported and resolve to the
same served asset directory.

Same-origin API asset references may use `api/<path>` or `/api/<path>`. They remain rooted at
the configured API base, independently of the SPA base. Cover references are local paths only:
remote URLs, URL schemes, data URLs, query/fragment suffixes, encoded paths, backslashes, empty
segments, and `.`/`..` traversal components are rejected. Old invalid metadata is treated as
absent by the UI. A missing or broken optional image displays the adventure's initial fallback.
