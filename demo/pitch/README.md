# Social Match — pitch deck

A single-page deck: `index.html` + `style.css` + `script.js`. No build step, no CDN, no fonts to
download. Open the file and present.

```bash
# simplest
open index.html                # or: xdg-open index.html

# if you prefer serving it (works over a projector, and gives you a URL to share)
python3 -m http.server 8000 -d demo/pitch    # → http://localhost:8000
```

## Presenting

| Key | Does |
| --- | --- |
| `→` `↓` `space` `enter` `PgDn` | next slide |
| `←` `↑` `Backspace` `PgUp` | previous slide |
| `Home` / `End` | first / last slide |
| `F` | fullscreen |
| `N` | speaker notes for the current slide |
| `?` | re-show the key hint |
| click left/right edge | page back / forward (projector-friendly) |
| wheel, trackpad, swipe | one slide per gesture; inside a tall slide it scrolls first |

Every slide is deep-linkable: `index.html#7` opens on slide 7, and the URL follows you as you move.

## Order (10 slides)

1. Title — the one-liner
2. The problem — "so when are you free?" (11 messages, 3 days)
3. The idea — machines decide the *what*, humans decide the *whether*
4. The loop — five beats, one decision
5. Architecture — four pieces, one firewall
6. Consent — five grants, re-read at the moment of use
7. Privacy — what crosses the wire vs what never does
8. Conflicting constraints — the budget → ramen counter, reason codes only
9. The one line — an agent may propose; a human confirms
10. Try it — two accounts, sixty seconds, and the closing pun

Speaker notes live in each slide's `data-notes` attribute — press `N` to see them while presenting.

## Editing

- Colours and spacing are CSS variables at the top of `style.css` (`--accent` is the "human yes" lime).
- A slide is a `<section class="slide">`; its first `.eyebrow` is used as the dot's tooltip.
- `data-count="544"` animates a number up when the slide is first shown.
