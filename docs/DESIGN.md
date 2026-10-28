# Next Move design system

Next Move takes visual cues from [Affirm](https://www.affirm.com/): large,
light sans-serif headings, generous space, pill actions and softly rounded
panels. It uses the user's [Coolors palette](https://coolors.co/ff6f59-254441-43aa8b-b2b09b-ef3054).
The app keeps its existing workflows and navigation. No Affirm assets, fonts,
branding or copy are included.

## Palette and hierarchy

| Color | Role |
| --- | --- |
| Coral `#ff6f59` | Primary actions, brand mark and small accents |
| Deep teal `#254441` | Light-theme text, selected controls, sign-in canvas |
| Mint `#43aa8b` | Board dividers and sign-in background accent; pale mint summary cards |
| Sage `#b2b09b` | Offers summary card, mixed with the current surface |
| Raspberry `#ef3054` | Heading punctuation; darker/lighter variants for error text |

Bright palette colors are used for fills and decoration. Coral buttons use
near-black teal `#142724` text for AA contrast. Muted text, semantic messages
and control borders use contrasting variants. The warning and success states
retain their text labels, as do every stage and outreach badge.

## Typography and shape

Vazirmatn is the single UI family: regular-weight, sentence-case page titles,
medium labels and controls, and semibold role titles. Fira Code is reserved for
code and setup snippets. Both are self-hosted SIL OFL 1.1 fonts (see
`frontend/src/fonts/OFL.txt`), with `font-display: swap` and the existing
`font-src 'self'` CSP. Only the body font is preloaded; the previous stencil
and serif files are no longer loaded.

Panels have 16–24px corners, inputs 12px corners and buttons/badges pill
shapes. Quiet solid dividers replace the old ticket rules and decorative
button brackets. The three summary figures sit on separate tinted cards.
The sign-in card sits on a teal canvas with subtle mint and coral washes.

## Tokens and themes

Tokens live in `frontend/src/style.css`. Bootstrap variables map to them, so
shared classes and Bootstrap controls inherit the same treatment. The
`--radius-0` name is retained for existing consumers and now means the
standard 16px panel radius. `--rule-divider` is the shared quiet separator.

Light uses warm off-white paper and white surfaces. Dark follows
`prefers-color-scheme: dark` with deep teal surfaces, cream text and the same
coral action color. Native controls follow `color-scheme: light dark`.
Keyboard focus uses a 2px contrasting text-color outline, including the
search wrapper. Reduced motion disables transitions and animations.

At phone widths the sidebar becomes wrapped navigation, page actions wrap,
forms stack and the table/board keep their existing horizontal scroll areas.

## Visual review

Build, then capture against disposable data:

```sh
cd frontend && npm run build && cd ..
.venv/bin/python scripts/design_screenshots.py affirm-palette --dark --strict
```

This captures sign-in, signup and validation, onboarding, pipeline table and
board, details, manual/URL capture, agent access, password and session recovery
at 1440×900 and 390×844 in both themes. Screenshots and `contrast.json` live in
`docs/design/screenshots/affirm-palette/`. The strict check runs axe's
`color-contrast` rule and the suite also checks pipeline keyboard focus.
