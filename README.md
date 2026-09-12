# Sims 4 Career Forge

Type a career idea. A language model drafts it, this tool validates it and
packages it into installable `.package` and `.ts4script` files.

```
python main.py                              # web interface at 127.0.0.1:7878
python main.py --gui                        # desktop window instead
python main.py --idea "a ghost lawyer"      # one-shot, no interface
python main.py --spec career.spec.json      # rebuild a saved career, offline
```

## Read this first

Everything the tool writes is checked against the game's own data, as of
game version 1.127.41:

- **Container.** The DBPF header and index match EA's packages. String
  tables follow EA's layout, including the string-length field. Getting that
  field wrong (the original crash) overflows a buffer in the game's native
  loader.
- **Tuning.** Field names and structure come from the game's own
  `careers/career_tuning` code and EA's current `career_Adult_Writer`,
  decoded from `SimulationDeltaBuild0.package`. Every EA id the tuning
  points at (go-to-work interaction, notifications, tones, buffs) is base
  game and generic. See `forge/ea_refs.py`.
- **SimData.** The UI reads careers from SimData, not XML. Without it the
  Find a Job picker has nothing to show. `forge/simdata.py` rebuilds all
  21,913 SimData resources in the game byte for byte
  (`python tools/check_simdata.py`). The career, track, level, statistic
  and aspiration SimData it writes decode identically to EA's own.

A future patch can still rename fields. The build report says which version
the tuning targets, and `lastException*.txt` names any field the game
rejects.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # macOS / Linux

pip install -r requirements.txt
copy .env.example .env           # then put your key in it
python main.py
```

Get a key at <https://openrouter.ai/keys>. Any model that follows JSON
instructions works; stronger ones need fewer repair attempts.

The desktop window is optional and needs one more package:
`pip install dearpygui`, then `python main.py --gui`.

### The .env file

`.env` is read automatically from the project folder, and from up to four
parent folders, so running from a subfolder still finds it.

```ini
OPENROUTER_API_KEY=sk-or-v1-...

# optional
CAREER_FORGE_MODEL=anthropic/claude-sonnet-4.5
CAREER_FORGE_TEMPERATURE=0.8
CAREER_FORGE_OUTPUT=C:\Users\you\Documents\CareerForge
CAREER_FORGE_MODS_DIR=C:\Users\you\Documents\Electronic Arts\The Sims 4\Mods
```

Real environment variables beat the file, so a shell export or CI secret wins
over a stale `.env`. A key that comes from the environment or `.env` is never
copied into the settings file — it stays in one place. `.env` is gitignored.

## How it works

```
your idea
   -> model drafts a CareerSpec as JSON      (forge/llm.py)
   -> spec is validated                      (forge/schema.py)
   -> failures go back to the model to fix, up to N attempts
   -> tuning XML generated from the spec     (forge/tuning.py, forge/ea_refs.py)
   -> SimData for each tuning resource       (forge/career_simdata.py, forge/simdata.py)
   -> ids derived by FNV hash                (forge/ids.py)
   -> text collected into a string table     (forge/stbl.py)
   -> everything packed into a .package      (forge/dbpf.py)
   -> the package re-read and checked        (forge/validate.py)
   -> diagnostic script zipped as .ts4script (forge/script_mod.py)
```

A career is a Career, one CareerTrack per branch, one CareerLevel per rung, a
performance Statistic, and an AspirationCareer for each level that has skill
requirements. Every one of those gets its own SimData.

The model never writes XML, never picks ids, never touches bytes. It fills in
a validated data structure and nothing else. That boundary is deliberate: it
is what stops a creative-but-careless model from producing a package that
corrupts a save.

Validation rejects unknown skills, non-consecutive levels, pay that decreases
with rank, more than one base track, out-of-range hours, and branches that
split anywhere except after the base track's last level, which is the only
place the game allows. Errors go back to the model in its own terms, which is
why "slap in an idea" usually works in one or two attempts rather than being a
coin flip.

## Output

Each build produces a folder containing:

| file | purpose |
|---|---|
| `<key>.package` | tuning, SimData and strings; the career itself |
| `<key>.ts4script` | console command to confirm the mod loaded |
| `<key>.spec.json` | edit and rebuild to tweak without re-prompting |
| `<key>.manifest.json` | every resource id, for conflict checking |
| `BUILD_REPORT.txt` | the career laid out, install steps, troubleshooting |

Ids are deterministic: the same spec always produces the same ids. This
matters because a player who updates the mod would otherwise get orphaned
references in saves where a sim already holds the career.

## Verifying an install

Copy both files into `Documents/Electronic Arts/The Sims 4/Mods`, enable
script mods in Game Options > Other, delete `localthumbcache.package`, and
restart fully.

Then open the console with Ctrl+Shift+C and run `forge.<your_mod_key>`.

- Command responds, career appears — done.
- Command responds, career missing or Find a Job broken — the tuning did not
  resolve. `lastException*.txt` in the Sims 4 folder names the tuning file and
  field.
- Unknown command — the `.ts4script` is not loading. Script mods are probably
  disabled, or the file is nested more than one folder deep.

## Promotion

Promotion works like EA's careers. A daily work-performance meter uses EA's
pacing for each level. A level's skill requirements gate promotion out of
that level, through EA's own skill objectives ("Reach Logic skill level 5").

EA only ships base-game objectives for some skills and levels. A requirement
with no exact objective uses the nearest lower level. Pack skills (robotics,
entrepreneur, ...) and level-1 requirements can't be enforced without the pack,
so they are skipped. Either case is listed under NOTES in the build report.
Pack objectives are never referenced, because the game treats a missing one as
a tuning error for players without that pack.

## Limitations

- Careers only. No custom interactions, buffs, uniforms, or career events.
- The work uniform is EA's office outfit, and the go-to-work interaction is
  the Business career's, which has no themed work events.
- The icon is picked from EA's career icons by keyword; no custom art.
- `promotion_message` in the spec is not used; the game shows its standard
  promotion notification.
- English strings only. The string table is built for one language.
- Requires an OpenRouter key and network access to draft a career. Rebuilding
  a saved spec is fully offline.

## License

Do what you like with it. Not affiliated with or endorsed by EA or Maxis.
