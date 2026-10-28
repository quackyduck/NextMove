# Next Move design system

The current look follows the Awwwards Site of the Day
[Neutomni](https://www.awwwards.com/sites/neutomni) (live site neutomni.com, a
Webflow studio). It replaced the earlier Heron AI–inspired drafting-paper
system. We took its *style*, not its content: no Neutomni names, wordmark,
copy, imagery or font files are used.

## What the reference does

Measured from the live stylesheet and the Awwwards screenshots:

| Trait | Neutomni | Why it matters to us |
| --- | --- | --- |
| Color | Two-colour print: red `#cf1830` on white, used for *all* text, headings and rules; pale grey `#f8f4f4` as the second surface; sky blue `#6db1f0` as a rare accent | One ink gives the app an unmistakable identity with no extra palette to maintain |
| Display | A huge custom stencil wordmark in all caps | A stencil display face makes titles and figures feel printed |
| Text | Vazirmatn (sans), sentence case | A clean, neutral sans keeps dense screens readable |
| Accent type | Editorial New italic for an aside ("*as much as we do*") | An italic serif adds warmth against the mechanical display face |
| Labels | Fira Code, uppercase ("LOCAL TIME", "◆ OPEN FOR PROJECTS") | The monospace voice carries status and metadata |
| Controls | Bracketed labels, `[ Reserve your spot ]` | A playful retro, terminal-like affordance |
| Structure | Thin red rules, dashed "tear along line" dividers, ticket-like cards, `01`–`04` numbering | Structure comes from rules, not shadows |
| Shape | Mostly square, soft large shadows used sparingly | Crisp, print-like |

## How it translates to a dense product UI

1. **One ink.** Text, borders, fills and the focus ring all use the red ink.
   Hierarchy comes from size, weight, the typeface and fills (the
   "Next actions" stat, primary buttons and the sign-in frame are solid red with
   white text), not from extra colours. Green stays only for the outreach
   badge and success alerts, so status still reads.
2. **Stencil for titles and figures.** Page `h1`s, the wordmark and the
   pipeline numbers use Big Shoulders Stencil Display. Long role titles on the
   detail page stay in sentence case.
3. **Sans for content, mono for annotation, serif for asides.** Role titles,
   body copy and form labels use Vazirmatn. Table headers, stage badges,
   priorities, lanes, timestamps, section headings and buttons use Fira Code,
   uppercase. Company names and `<em>` use Instrument Serif italic.
4. **Brackets and tear lines.** Every button reads `[ LABEL ]` (decorative
   brackets with empty alt text). Section headings, form footers and table
   rows are separated by dashed red rules, like tear-off lines on a ticket.
   Eyebrow markers are ◆ diamonds.
5. **A red frame for sign-in.** The sign-in and create-account screens put the
   white card on a full red page, echoing the reference's framed hero.
6. **Quiet motion.** Colour, border and soft-shadow transitions only. Nothing
   animates on load or loops, and `prefers-reduced-motion` sets every duration
   to zero.

## Themes

Light is the default. Dark follows `prefers-color-scheme: dark` with no toggle:
the same print run on near-black paper (`#140a0b`), with the red lifted to
`#ff7380` so text keeps AA contrast. `color-scheme: light dark` is declared so
native controls match.

## Tokens

All tokens live at the top of `frontend/src/style.css`. Bootstrap's `--bs-*`
variables and component variables (`.btn`, `.alert`, `.table`, form controls)
are mapped onto them, so a new screen built from Bootstrap primitives and the
shared classes (`page-header`, `eyebrow`, `heading-dot`, `form-panel`,
`section-heading`, `form-footer`, `manual-note`, `stage-badge`, `mono-label`,
`serif-accent`) picks up the system without new CSS.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--color-paper` / `--color-surface` | `#ffffff` / `#ffffff` | `#140a0b` / `#1b0f11` | Page, panels, inputs |
| `--color-sunken` | `#f8f4f4` | `#251416` | Wells, filled badges, table headers |
| `--color-hover` | `#fcf1f2` | `#2a1619` | Row and nav hover |
| `--color-ink` | `#cf1830` | `#ff7380` | All primary text, headings |
| `--color-ink-2` | `#b0142a` | `#f6a0a8` | Secondary text, company names |
| `--color-muted` | `#8e3440` | `#d99aa1` | Captions, meta (AA on every surface) |
| `--color-line` / `--color-line-strong` | `#f0cdd2` / `#cf1830` | `#43242a` / `#ff7380` | Hairlines / control borders and tear lines |
| `--color-accent` / `-strong` | `#cf1830` / `#9a0e21` | `#ff5363` / `#ff9aa3` | Fills, marks, hover |
| `--color-inverse` / `--color-on-inverse` | `#cf1830` / `#ffffff` | `#ff7380` / `#140a0b` | Primary buttons, inverted cells |
| `--color-signal` | `#6db1f0` | | Decoration only (fails text contrast) |
| `--color-success` / `-wash` | `#2f6b45` / `#e3eee5` | `#7fc796` / `#1b2a20` | Outreach badge, success alerts |
| `--color-danger` / `-wash` | `#8a0c1c` / `#fbe7ea` | `#ffb4bb` / `#3a1419` | Errors, destructive buttons |
| `--font-sans` | Vazirmatn | | Content |
| `--font-mono` | Fira Code | | Labels, buttons, code |
| `--font-display` | Big Shoulders Stencil Display | | Titles, wordmark, figures |
| `--font-serif` | Instrument Serif italic | | Company names, `<em>` |
| `--text-2xs` … `--text-display` | 11, 12, 13, 14, 16, 20, 28, 40, 64px | | Type scale (phone `h1` and figures: 48px) |
| `--weight-*` | 400, 500, 600, 700, display 800 | | |
| `--space-1` … `--space-16` | 4, 8, 12, 16, 20, 24, 32, 40, 48, 64px | | 4px grid |
| `--radius-*` | 0 | | Square everywhere |
| `--rule-dashed` | `1px dashed var(--color-line-strong)` | | Tear lines |
| `--shadow-1` / `-2` / `-dialog` | soft, red-tinted, 24–60px blur | darker | Elevation |
| `--focus-ring` | `2px solid var(--color-accent)`, offset 2px | | Every focusable element |

## Fonts and performance

- All four families are SIL OFL 1.1 (`frontend/src/fonts/OFL.txt`), self-hosted
  as woff2 subsets from Fontsource. Every page loads the latin files it uses:
  Vazirmatn 34 KB, Fira Code 36 KB, Big Shoulders Stencil 37 KB and
  Instrument Serif italic 22 KB; latin-ext files load only if such characters
  appear.
- Editorial New, the reference's serif, is commercial, so Instrument Serif
  stands in. The reference's wordmark is custom; Big Shoulders Stencil
  Display gives the same stencil feel.
- `font-display: swap` and `<link rel="preload">` for the text and display
  faces. The CSP stays `font-src 'self'`.

## Accessibility

- Text tokens meet WCAG AA (4.5:1) on paper, surface and sunken in both
  themes; control borders meet 3:1. White on the red ink is 5.5:1, and red on the grey surface 5.0:1.
- `frontend/visual/screens.visual.js` runs axe's `color-contrast` rule on every
  screen at desktop and phone widths in both themes, and checks that every
  keyboard stop on the pipeline shows a 2px or wider focus outline.
- Decorative generated content (`[01]` indices, button brackets, `/` lane
  prefix) uses the `content: "…" / ""` alt syntax so screen readers skip it.

## Visual review

```
cd frontend && npm run build && cd ..
.venv/bin/python scripts/design_screenshots.py after --dark --strict
```

This captures every screen (sign in, create account and its validation
state, resume upload and agent setup steps, pipeline with the setup
checklist, pipeline table and board, job detail, capture a role, quick
capture, agent access, password dialog, session recovery) at 1440×900 and
390×844 into `docs/design/screenshots/<label>/`,
with `contrast.json` next to them. `--strict` fails on any contrast violation.
