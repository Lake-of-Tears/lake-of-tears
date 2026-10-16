# Docs source (thelakeoftears.com/wiki/)

These markdown files are rendered in the browser by `index.html`; there is no build step. GitHub Pages serves this folder from `main`.

## Add a page

1. Add `Your-Page-Title.md` here. Use `##` and `###` headings (they become the "On this page" list), fenced code blocks, tables, and ` ```mermaid ` diagrams.
2. Put images in `images/` and reference them as `images/name.jpg`.
3. Add an entry to `pages.json`: `{ "slug": "your-page-title", "title": "Your Page Title", "file": "Your-Page-Title.md" }`. Pages appear in the sidebar in the order listed.
4. Link to another page with its file name, e.g. `[Catalogs](Workspaces-and-Catalog-Management.md)`.
5. Open the PR as `docs/<issue#>-...` and check the page at `/wiki/#/<slug>` before requesting review.

## Preview locally

From the repo root: `python3 -m http.server 8099 --directory docs`, then open http://localhost:8099/wiki/. (Opening the file directly will not work: the viewer loads the markdown with `fetch`.)
