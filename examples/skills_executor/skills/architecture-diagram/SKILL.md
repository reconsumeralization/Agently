---
name: architecture-diagram
description: >-
  Create a professional, dark-themed software/system architecture diagram as a
  single self-contained HTML file with inline SVG. Use when asked to visualize
  system components, layers, services, or how parts of a codebase fit together.
keywords: [architecture diagram, system design, svg, html, components, layers]
---

# Architecture Diagram

Produce ONE self-contained HTML document (embedded CSS + inline SVG, no
JavaScript, no external images; Google Fonts link is allowed). It must render
correctly when opened directly in a browser.

## Visual design system
- Background `#020617` with a subtle 40px grid pattern.
- Font: JetBrains Mono (monospace) via Google Fonts.
- Component boxes: rounded rects (`rx="6"`), 1.5px stroke, semi-transparent
  fills. Colour components by role:
  - facade / developer surface: fill `rgba(8,51,68,0.4)`, stroke `#22d3ee`
  - core contracts: fill `rgba(6,78,59,0.4)`, stroke `#34d399`
  - plugins / providers: fill `rgba(120,53,15,0.3)`, stroke `#fbbf24`
  - execution environment / capabilities: fill `rgba(76,29,149,0.4)`, stroke `#a78bfa`
  - observation / event bus: fill `rgba(251,146,60,0.3)`, stroke `#fb923c`
  - external / generic: fill `rgba(30,41,59,0.5)`, stroke `#94a3b8`
- Component name at 11px white bold; sublabel at 8-9px `#94a3b8`.
- Arrows via an SVG `marker` arrowhead; draw connecting arrows before boxes so
  they sit behind them.

## Layout rules
- Lay the system out as labelled horizontal bands stacked top-to-bottom, one band
  per architectural layer, with each layer's components inside its band.
- Keep ≥40px vertical gaps between stacked rows; never overlap boxes.
- Add a short header (title + subtitle) and a small legend mapping each colour to
  a layer. Place the legend outside every band.

## Output
Return the complete HTML document as a single string. Do not include commentary
outside the HTML.
