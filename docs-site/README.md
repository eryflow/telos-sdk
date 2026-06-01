# TELOS Documentation Site

The TELOS docs, built with [Mintlify](https://mintlify.com). Bilingual: English (`en/`) and 简体中文 (`zh/`).

## Local preview

```bash
# install the Mintlify CLI once
npm i -g mint

# from this directory
cd docs-site
mint dev            # serves at http://localhost:3000
```

Validate links and config:

```bash
mint broken-links   # report broken internal links
```

## Structure

```
docs-site/
├── docs.json        # theme, colors, navbar, and the bilingual navigation tree
├── logo/            # light/dark logo (the TELOS "stone tablet" mascot)
├── favicon.svg
├── images/          # diagrams copied from ../assets (English: *.en.svg, Chinese: *.svg)
├── en/              # English pages (MDX)
└── zh/              # 简体中文 pages (MDX)
```

Navigation groups (per language): **Get Started · Concepts · Guides · Observability · Benchmark · Reference**.

## Deployment

Deploy with the [Mintlify GitHub app](https://mintlify.com/docs/quickstart): connect the
`learningCatHD/telos-sdk` repository and set the **docs content root to `docs-site`**. Pushes to
the default branch publish automatically.

The repository's existing `vercel.json` serves the separate marketing landing page (`site/`) and is
unaffected by this docs site.

## Editing

- Every page is MDX with `title` / `description` frontmatter.
- English pages use the `*.en.svg` diagram variants; Chinese pages use the `*.svg` variants.
- When adding a page, also add it to the matching language group in `docs.json`.
- Keep `en/` and `zh/` trees in lockstep so the language switcher never 404s.
