# Configuration examples

All example rules are **disabled** and contain placeholder list URLs. Copy the
desired file to the root `config.toml`, replace the URLs with lists you can read
and destinations you can write, then enable individual rules for preview.

| File | Use case |
| --- | --- |
| [public-curator.toml](public-curator.toml) | Follow a public MDBList curator into your own Trakt list |
| [own-bidirectional.toml](own-bidirectional.toml) | Two-way mirror of lists you control on both services |
| [multiple-schedules.toml](multiple-schedules.toml) | Several lists with hourly, local-nightly and manual timing |

```text
python manage.py validate
python manage.py render-workflows
python manage.py render-workflows --check
```

Commit configuration and generated workflows together. Initialize state once,
preview, review counts, then explicitly enable apply. A first bidirectional mirror
needs `--bootstrap-state` for preview and first apply; never use bootstrap to
silently replace an established baseline. See the [setup guide](../docs/setup.md).