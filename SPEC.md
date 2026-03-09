# ALP-1118: Fleet Presets for warroom.sh

## Summary

Extend warroom.sh with named fleet presets defined in `~/.helioy/fleets.toml` and explicit `--fleet` / `--agents` flags. Replaces the current positional argument for role-mode.

## CLI Interface

```
warroom.sh                                            # repo-mode (unchanged)
warroom.sh --fleet design                             # spawn a named fleet
warroom.sh --agents "frontend-engineer backend-engineer"  # ad-hoc agents
warroom.sh --fleet design --agents "coordinator"      # additive: fleet + extras
warroom.sh kill [warroom|crew|all]                    # teardown (unchanged)
```

The old positional syntax (`warroom.sh "type1 type2"`) is removed. `--agents` replaces it.

## Fleet Configuration

File: `~/.helioy/fleets.toml`

TOML structure (Option B: rich objects):

```toml
[fleets.design]
agents = ["ux-researcher", "visual-designer", "ux-designer", "frontend-engineer"]

[fleets.fullstack]
agents = ["backend-engineer", "frontend-engineer"]

[fleets.mobile]
agents = ["mobile-engineer", "ux-designer"]
```

## Edge Cases

### Unknown fleet name

Use fzf to present available fleets for interactive selection. If fzf is not installed, print the list and exit with error.

```
$ warroom.sh --fleet nope
Fleet "nope" not found.
  > design
    fullstack
    mobile
```

### Missing fleets.toml

Prompt the user to create with defaults. Print the default content, ask for confirmation. Write on Y, abort on N/ctrl-c.

```
$ warroom.sh --fleet design
~/.helioy/fleets.toml not found.
Create with defaults? [Y/n]

[fleets.design]
agents = ["ux-researcher", "visual-designer", "ux-designer", "frontend-engineer"]

[fleets.fullstack]
agents = ["backend-engineer", "frontend-engineer"]

[fleets.mobile]
agents = ["mobile-engineer", "ux-designer"]

Created ~/.helioy/fleets.toml
```

### Duplicate agents

When `--fleet` and `--agents` combine, or when multiple fleets overlap, deduplicate silently. Preserve order (first occurrence wins).

### Multiple fleets

`--fleet design --fleet fullstack` is supported. Agents are merged additively, deduplicated.

### Malformed TOML

Print a clear error with details. No fallback, no silent defaults.

```
$ warroom.sh --fleet design
Error parsing ~/.helioy/fleets.toml: expected "=" after key at line 3
```

### Empty fleet

Error at parse time.

```
$ warroom.sh --fleet empty
Fleet "empty" has no agents defined.
```

### No tmux

Error and exit (unchanged from current behavior).

### fzf not installed

Fall back to plain list output for fleet selection. Not a hard dependency.

## TOML Parsing

bash has no native TOML parser. Options:
- python3 one-liner using `tomllib` (Python 3.11+, already a dependency for bus server)
- `tomlq` from yq (external dep, avoid)

Use python3 with tomllib. The script already uses python3 for other operations (bus-register.sh). Consistent tooling.

## Changes to warroom.sh

1. Replace positional arg parsing with getopt-style `--fleet` and `--agents` flags
2. Add `parse_fleets()` function: reads fleets.toml via python3, returns agent list
3. Add `prompt_create_fleets()` function: interactive creation of default fleets.toml
4. Add `select_fleet_fzf()` function: fzf picker for unknown fleet names
5. Merge and deduplicate agents from `--fleet` + `--agents`
6. Rest of role-mode spawning logic unchanged (window=crew, setup_pane, lock_window_titles)

## Success Criteria

- [ ] `warroom.sh --fleet design` spawns agents defined in fleets.toml
- [ ] `warroom.sh --agents "backend-engineer frontend-engineer"` spawns ad-hoc agents
- [ ] `warroom.sh --fleet design --agents "coordinator"` merges both sets
- [ ] `warroom.sh --fleet design --fleet fullstack` merges multiple fleets
- [ ] Unknown fleet triggers fzf selection (fallback to plain list)
- [ ] Missing fleets.toml prompts for creation with defaults
- [ ] Duplicate agents deduplicated silently
- [ ] Malformed TOML prints clear error
- [ ] Empty fleet prints clear error
- [ ] Old positional syntax removed
- [ ] Repo-mode (`warroom.sh` with no args) unchanged
- [ ] Kill semantics unchanged

## Files

| File | Change |
|------|--------|
| `plugin/scripts/warroom.sh` | Add flag parsing, fleet resolution, fzf selection |
| `~/.helioy/fleets.toml` | Created on first use (user space, not tracked in git) |

## Dependencies

- python3 with tomllib (Python 3.11+)
- fzf (optional, graceful fallback)
