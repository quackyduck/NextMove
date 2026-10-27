# Next Move design system

Phase 1.1 gives Next Move its own visual identity. The reference was the
Awwwards Site of the Day [Heron AI](https://www.awwwards.com/sites/heron-ai)
(live site heronaiapp.com, by Bearplus). We took its *style*, not its content:
no Heron names, logos, copy, imagery or font files are used.

## What the reference does

Measured from the live stylesheet and the Open Graph image:

| Trait | Heron AI | Why it matters to us |
| --- | --- | --- |
| Surface | Warm off-white "drafting paper" (`#f4f2ee`-ish), with whole sections inverted to graphite | A calm, non-white base reads as paper and suits a long reading session |
| Ink | Graphite `#282828`, secondary `#414140`, `#626260` | Near-black instead of pure black, warm grays |
| Accent | One signal orange-red (`#fa3600` / `#f15534`), used only for alerts, callouts and the active marker | A single accent lets status jump out of a dense table |
| Type | BT Grotesk (commercial grotesk), tight `-0.02em` tracking, uppercase display headlines; Geist Mono for small technical labels | The grotesk/mono pairing is the core of the look |
| Annotation | Bracketed indices `[01]`–`[04]`, coordinate readouts, mono uppercase captions | Gives every number and label a "spec sheet" voice |
| Structure | Hairline blueprint grid lines, crosshair `+` registration marks, dashed tick rulers | Structure comes from lines, not from shadows or color blocks |
| Shape | Square corners (0–3px radius) | Crisp, technical |
| Elevation | Flat offset shadows (`.6rem .6rem` at 6% black), no blur | Depth without softness |
| Motion | `0.4s` color/opacity/transform transitions on `cubic-bezier(.23,1,.32,1)` and `cubic-bezier(.625,.05,0,1)` | Fast start, long settle |

## How it translates to a dense product UI

Heron is a scroll-driven marketing page; Next Move is a workspace people scan
every day. So the rules are:

1. **Paper, ink, one signal.** The neutral palette carries everything. The
   accent appears only in small amounts: the active nav marker, eyebrow
   squares, the heading period, high priority, the offer stage, the newest
   timeline point, focus rings and dialog top rules.
2. **Grotesk for content, mono for annotation.** Role titles, body copy,
   form labels and buttons use Geist. Eyebrows, table headers, stage badges,
   priorities, lanes, timestamps, counts, section headings and footers use
   Geist Mono, uppercase with `0.08em` tracking. Page `h1`s are uppercase
   Geist with `-0.03em` tracking; long role titles on the detail page stay in
   sentence case.
3. **Lines, not boxes.** Panels are hairline-ruled with square corners. The
   stats become one ruled strip numbered `[01]`–`[04]`, the "Next actions"
   cell inverted to graphite. The board and login get a faint blueprint grid,
   and the login card has crosshair registration marks.
4. **Status as a progression.** Stage badges go from dashed or outlined
   (needs review, interesting), to a gray fill (resume updated, outreach,
   applied), to graphite (interviews), to signal orange (offer).
5. **Quiet motion.** Only color, border and `transform` transitions. Board
   cards and the primary button lift by 1–2px onto an offset shadow; table
   rows only change background. Dialogs fade up once. Nothing animates on load
   or loops, and `prefers-reduced-motion` sets every duration to zero.

## Themes

Light is the default. Dark follows `prefers-color-scheme: dark` with no toggle,
so there is no behavior change. Dark is the inverted drawing: graphite paper,
light ink, the same signal accent. `color-scheme: light dark` is declared so
native controls (date pickers, file inputs, scrollbars) match.

## Tokens

All tokens live at the top of `frontend/src/style.css`. Bootstrap's `--bs-*`
variables and component variables (`.btn`, `.alert`, `.table`, form controls)
are mapped onto them, so a new screen built from Bootstrap primitives and the
shared classes (`page-header`, `eyebrow`, `heading-dot`, `form-panel`,
`section-heading`, `form-footer`, `manual-note`, `stage-badge`, `mono-label`)
picks up the system without new CSS.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--color-paper` | `#f2f1ec` | `#161615` | Page background |
| `--color-surface` | `#fbfbf8` | `#1f1f1d` | Panels, inputs, cards |
| `--color-sunken` | `#e9e8e2` | `#2a2a27` | Wells, filled badges, read-only inputs |
| `--color-hover` | `#efeee8` | `#262624` | Row and nav hover |
| `--color-ink` | `#1f1f1e` | `#efeee8` | Primary text |
| `--color-ink-2` | `#41413f` | `#c9c8c1` | Secondary text, labels |
| `--color-muted` | `#5e5e5a` | `#a5a49d` | Captions, meta (AA on every surface) |
| `--color-line` | `#dcdbd4` | `#33332f` | Decorative hairlines |
| `--color-line-strong` | `#85847e` | `#74736d` | Control borders (≥3:1 non-text contrast) |
| `--color-accent` | `#fa3600` | `#fa3600` | Signal fills and marks (non-text, large text) |
| `--color-accent-strong` | `#c22a00` | `#ff6a3d` | Accent text, hover fills, offer badge |
| `--color-on-accent` | `#ffffff` | `#161615` | Text on accent-strong |
| `--color-inverse` / `--color-on-inverse` | `#1f1f1e` / `#f2f1ec` | `#efeee8` / `#161615` | Primary buttons, inverted cells, code blocks |
| `--color-accent-on-inverse` | `#fa3600` | `#c22a00` | Accent on inverted cells |
| `--color-success` / `-wash` | `#2f6b45` / `#e3eee5` | `#7fc796` / `#1b2a20` | Success alerts, outreach badge |
| `--color-warning` / `-wash` | `#7a5a00` / `#f5ecd0` | `#e4c26a` / `#2d2715` | Warning alerts |
| `--color-danger` / `-wash` | `#a3241a` / `#f8e1dd` | `#ff8f80` / `#351b18` | Errors, destructive buttons |
| `--font-sans` | Geist, metric-matched Arial fallback | | Content |
| `--font-mono` | Geist Mono, metric-matched Menlo fallback | | Annotation |
| `--text-2xs` … `--text-display` | 11, 12, 13, 14, 16, 20, 28, 40, 48px | | Type scale (phone: `xl` 24, `2xl` 30) |
| `--weight-*` | 400, 500, 600, 700 | | |
| `--tracking-display` / `-tight` / `-label` | `-0.03em` / `-0.015em` / `0.08em` | | |
| `--space-1` … `--space-16` | 4, 8, 12, 16, 20, 24, 32, 40, 48, 64px | | 4px grid |
| `--radius-0` / `-1` / `-2` | 0 / 2 / 4px | | Panels / controls / large |
| `--control-height` | 40px (48px for phone inputs on capture screens) | | |
| `--shadow-1` / `-2` / `-dialog` | 3px, 6px, 10px offset, no blur | stronger alpha | Elevation |
| `--ease-out` / `--ease-in-out` | `cubic-bezier(.23,1,.32,1)` / `cubic-bezier(.625,.05,0,1)` | | |
| `--duration-fast` / `-base` / `-slow` | 120 / 240 / 400ms (0 with reduced motion) | | |
| `--focus-ring` | `2px solid var(--color-accent)`, offset 2px | | Every focusable element |

## Fonts and performance

- [Geist and Geist Mono](https://github.com/vercel/geist-font), SIL OFL 1.1
  (`frontend/src/fonts/OFL.txt`). Self-hosted variable woff2, latin and
  latin-ext subsets only (29 + 23 KB for the latin files that every page uses;
  latin-ext loads only if such characters appear).
- The previous Google Fonts `@import` was blocked by the production CSP
  (`default-src 'self'`), so DM Sans and Manrope never loaded in production.
  Self-hosting fixes that without touching the CSP.
- `font-display: swap`, `<link rel="preload">` for the two latin files, and
  fallback faces with `size-adjust` / `ascent-override` / `descent-override`
  measured by `frontend/visual/measure-fallback.mjs`, so the swap does not
  shift layout.
- No new runtime dependency. Bootstrap is still the full build (about 230 KB
  of the 270 KB CSS, 38 KB gzip). Trimming it to used partials is a possible
  follow-up once the Phase 1 screens land.

## Accessibility

- Text tokens meet WCAG AA (4.5:1) on paper, surface and sunken in both
  themes; control borders meet 3:1.
- `frontend/visual/screens.visual.js` runs axe's `color-contrast` rule on every
  screen at desktop and phone widths in both themes, and checks that every
  keyboard stop on the pipeline shows a 2px or wider focus outline.
- Decorative generated content (`[01]` indices, `/` lane prefix, brackets)
  uses the `content: "…" / ""` alt syntax so screen readers skip it.

## Visual review

```
cd frontend && npm run build && cd ..
.venv/bin/python scripts/design_screenshots.py after --dark --strict
```

This captures every screen (login, pipeline table and board, job detail,
capture a role, quick capture, agent access, password dialog, session
recovery) at 1440×900 and 390×844 into `docs/design/screenshots/<label>/`,
with `contrast.json` next to them. `--strict` fails on any contrast violation.
