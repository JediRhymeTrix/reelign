---
title: Contributing and documentation development
description: Contribute to Reelign with offline tests, sanitized fixtures, trusted workflow review, and GitHub Pages-compatible Jekyll documentation checks.
permalink: /contributing/
---

# Contributing

Start with [CONTRIBUTING.md](https://github.com/JediRhymeTrix/reelign/blob/main/CONTRIBUTING.md) for contribution scope, test expectations, and reporting guidelines. Use fake list URLs and mocked provider/GitHub calls; never attach personal runtime configuration or credentials.

## Validate a change locally

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python manage.py validate
python manage.py render-workflows --check
```

Tests must run offline and must not consume real OAuth refresh tokens or mutate provider lists. Scheduling changes need deterministic renderer coverage; semantic changes need baseline/legacy compatibility and safety tests. Describe user-visible behavior changes and update the relevant guide/changelog.

## Documentation site

The site source is `docs/`, built with GitHub Pages’ supported Jekyll implementation. It uses a local layout/CSS/SVG and standard Liquid/Kramdown, with no custom Ruby plugins or remote theme dependency. `jekyll-relative-links` is a GitHub Pages-supported plugin used to make same-guide Markdown links work both in GitHub source and on the site. Metadata, canonical URLs, sitemap, and robots are source-controlled.

For an optional local preview, install Ruby/Bundler appropriate to your platform, then:

```sh
bundle install --gemfile docs/Gemfile
bundle exec --gemfile docs/Gemfile jekyll serve --source docs --destination docs/_site --baseurl /reelign
```

Open `http://127.0.0.1:4000/reelign/`. Do not commit generated `_site`, local Ruby caches, or secret-bearing config. The upstream Pages workflow builds/deploys the real hosted docs; publishing is restricted to the upstream repository. Forks/template copies need not deploy a site for syncing.

When reviewing documentation, check both source and rendered navigation, relative links, anchors, keyboard focus, mobile layout, canonical/OpenGraph descriptions, sitemap entries, and any CLI/config examples against the actual parsers. Keep the exact brand tagline and original cheeky section. Do not claim unverified provider enforcement events or promise policy compliance.