# TrainWatching

This site uses the Alpine Earth design system (`web/alpine-earth/`). Build UI with its `ae-` classes and tokens; see the alpine-earth skill.

- Don't edit files in `web/alpine-earth/`; it is a copy of the kit from the `skills` repo. Put project styles in `web/style.css`, using the `--ae-*` tokens.
- `url()` inside a custom property resolves against the stylesheet that uses it, so textures used from `web/style.css` are declared there (`--tw-*`) with paths relative to that file.
- Map colours in `web/app.js` (`readTheme`) are read from the tokens of the active theme at runtime; don't hard-code colours there.
