---
title: MDBList ↔ Trakt List Sync
description: Sync your own lists or follow public curator lists between MDBList and Trakt. Self-host in GitHub Actions with your credentials, preview first, and keep encrypted state.
permalink: /
---

<p class="eyebrow">Open source · User operated · GitHub Actions</p>

# Reelign

<p class="tagline">Your lists. Perfectly aligned.</p>

Keep your own MDBList and Trakt lists in step. Or follow another curator’s **publicly readable list** into a list you own. Reelign synchronizes movies and TV shows from your own GitHub repository, with your own provider credentials.

<p class="actions"><a class="button" href="{{ '/setup/' | relative_url }}">Set up your list sync</a> <a class="button secondary" href="https://github.com/JediRhymeTrix/reelign">View on GitHub</a></p>

## Your watchlist didn’t ask to be part of the breakup.

When MDBList and Trakt stop talking, your carefully curated lists shouldn’t become collateral damage. Reelign handles synchronization from your own GitHub repository, using credentials you control.

Less platform drama. More movies.

## MDBList to Trakt sync, without a hosted middleman

- **Own-list syncing:** keep your MDBList and Trakt collections aligned.
- **Public-list following:** read a curator’s accessible public list; write only to your own authorized destination.
- **Your policy:** one-way or bidirectional, mirror or additions-only, with explicit deletion limits.
- **Your timing:** named cron/timezone profiles or manual-only operation in GitHub Actions.
- **Your review:** preview change counts before applying; verify writes before advancing the baseline.
- **Your credentials:** a personal Trakt application, your MDBList key, and encrypted durable state.

There is no artificial rule-count cap, but provider quotas, permissions, list sizes, GitHub Actions limits, and execution time remain real constraints. This is list synchronization—not watch-history migration or a full backup service.

## Start small. Preview. Then align.

1. Create a **private** repository from the template. Public copies expose committed list URLs.
2. Register your own Trakt app, authorize locally, and install your own secrets.
3. Configure a disabled example with your own URLs; enable the rule when ready to preview.
4. Initialize encrypted state and run a dry run in your repository.
5. Review the plan, explicitly enable apply, and run the first sync.

[Follow the complete setup guide →]({{ '/setup/' | relative_url }})

## Read the contract, not just the feature list

| Question | Guide |
| --- | --- |
| Which config fields and URLs are accepted? | [Configuration]({{ '/configuration/' | relative_url }}) |
| How do deletes, ordering, and conflicts work? | [Sync behavior]({{ '/behavior/' | relative_url }}) |
| How do schedules and OAuth maintenance interact? | [Scheduling]({{ '/scheduling/' | relative_url }}) |
| What if authorization or state persistence fails? | [Recovery]({{ '/recovery/' | relative_url }}) |
| What is trusted, stored, or exposed? | [Security]({{ '/security/' | relative_url }}) |

## Independent, not exempt

Reelign is not affiliated with, endorsed by, or sponsored by Trakt or MDBList. **Reelign uses Trakt but is not endorsed or certified by Trakt.** Provider names and marks belong to their respective owners.

Use only access you are authorized to use, following provider terms, application requirements, permissions, and rate limits. A user-operated tool is not automatically exempt from provider restrictions. Reelign offers no compliance guarantee and must not be used to evade access restrictions.