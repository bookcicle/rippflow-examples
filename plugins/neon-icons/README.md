# Neon Studio Icons (`neon-icons`)

An example **Icon Pack plugin** for Rippflow Studio that overrides built-in `IconName` and custom SVG app icons across the entire Studio UI (toolbars, file tree, tabs, settings, and window controls).

## Install & Activate

```bash
# Install via CLI
rippflow plugin add plugins/neon-icons/neon-icons.yaml

# Or install all example plugins
bash plugins/install.sh
```

Then activate in Rippflow Studio:
- **Settings → Appearance & Theme → Icon Pack → Neon Studio Icons**, or
- **Settings → Plugins & Extensions → Neon Studio Icons → Use Icons**

## Complete Studio App Icon Inventory

Place any `<key>.svg` file in `icons/` (or alias filenames via `icons/icons.json`) to override that icon across Studio.

### Built-in `IconName` Keys (36)
`book-open`, `check`, `chevron-down`, `chevron-right`, `chevron-up`, `close`, `copy`, `cpu`, `delete`, `external-link`, `eye`, `file`, `folder`, `folder-closed`, `folder-open`, `globe`, `hard-drive`, `info`, `maximize`, `memory-stick`, `menu`, `minimize`, `network`, `play`, `plus`, `redo`, `search`, `settings`, `square-terminal`, `triangle-alert`, `undo`, `undo-2`, `window-close`, `window-maximize`, `window-minimize`, `window-restore`

### Custom Studio SVG Keys (45)
`agents-icon`, `agy-logo`, `archive-icon`, `arrow-up-icon`, `claude-logo`, `codex-logo`, `collapse-focus-icon`, `docker-logo`, `ellipsis-horizontal`, `ellipsis-vertical`, `expand-focus-icon`, `eye-off`, `eye-open`, `gemini-logo`, `github-logo`, `gitlab-logo`, `git-branch`, `hamburger-menu`, `incognito-glasses`, `java-logo`, `lock-closed`, `lock-open`, `modern-terminal-icon`, `named-tabs-label`, `node-logo`, `ollama-logo`, `panel-left-filled`, `panel-left-outline`, `paste-clipboard-icon`, `pencil`, `pin-icon`, `preview-sparkle`, `python-logo`, `rippflow-glyph`, `rust-logo`, `secrets-import`, `secrets-refresh`, `secrets-vault`, `settings-gear`, `split-down-icon`, `split-pane-icon`, `stop-icon`, `terminal-chevron`, `threads-icon`, `vertical-dots`
