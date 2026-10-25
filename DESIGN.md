---
name: FlightDeck
description: Native macOS Operate interface for a local development workspace.
spacing:
  compact: "5pt"
  row: "7pt"
  inset: "8pt"
  log: "12pt"
  form: "14pt"
  section: "16pt"
  detail: "20pt"
  page: "22pt"
---

# Design System: FlightDeck

## Overview

**Design direction: Native macOS Operate.** A compact working interface built from SwiftUI navigation, lists, fields, and standard controls. Information hierarchy comes from system text styles, alignment, dividers, and explicit state labels. Lists carry operational information without decorative cards.

This document merges the original design brief with `Sources/FlightDeck/FlightDeck.swift`. The source is the implementation authority. Native semantic color and font declarations are preserved in `.impeccable/design.json`; they are not translated into invented CSS values.

**Verification:** source inspection only. UI visual verification is incomplete. Organizational policy blocked CUA access to `local.flightdeck.app`; no screenshots or app-control verification were performed for this documentation.

## Colors

### Primary

Use `Color.accentColor` for the active workspace label and selected-project checkmark. The prominent release button inherits the native accent treatment.

### Secondary

Use SwiftUI `.orange` for warnings, job errors, failed readiness checks, and leased-device labels. Use `.green` for successful readiness checks and available-device labels. State text and SF Symbols accompany color.

### Neutral

Use `.primary` and `.secondary` foreground styles for primary content and supporting metadata. The log surface uses `NSColor.textBackgroundColor` with `NSColor.separatorColor` at its border. Other surfaces inherit native appearance. Error and notice strips apply low-opacity orange and green backgrounds; exact opacity declarations are captured in the native sidecar.

**The System Appearance Rule.** Preserve semantic colors and native appearance adaptation. Do not freeze them to a sampled light-mode palette.

## Typography

System text is the default. Page/project headings use `.title2.weight(.semibold)`; job stages and row titles use `.headline`. Supporting copy uses `.caption` and `.caption2`, with semibold captions for small section labels. The TestFlight processing explanation uses `.callout`.

Monospaced text is reserved for run output (11 pt) and device identifiers (10 pt). Storage sizes use `.monospacedDigit()` while retaining the surrounding system font. Leave native text-style metrics and line heights to SwiftUI; no custom font family, scale ratio, or tracking is defined.

## Layout

The app has a `NavigationSplitView` with a Workspace/Projects sidebar and a detail surface. Releases add an `HSplitView` for recent runs and selected-run details; Devices and Storage use full-width lists. This is a desktop layout, not a mobile breakpoint system.

The window minimum is 980 × 660 pt and its default is 1180 × 780 pt. The recent-runs column has minimum/ideal/maximum widths of 230/270/330 pt; job detail has a minimum width of 370 pt. The build-number field is 230 pt wide. Logs scroll on both axes; native lists handle their own scrolling.

Use the frontmatter spacing vocabulary for page, detail, form, section, and row rhythm. The source also uses native `.padding()` where system inset values are appropriate. Release and page headings sit above dividers or list content; details align to the leading edge. No custom responsive breakpoints are implemented. Minimum-size fit and long-text behavior remain visually unverified.

## Elevation & Depth

No custom shadows or materials are defined. Native sidebar/list surfaces, separators, and low-opacity status strips provide grouping. Preserve system-managed depth for controls and dialogs rather than adding decorative elevation.

## Shapes

Use native button, rounded-border field, list, and dialog shapes. No custom corner-radius scale is defined. The log area is rectangular with a half-point semantic separator border. SF Symbols express navigation and state; they do not replace explanatory text.

## Components

### Navigation

The sidebar groups Releases, Devices, and Storage above the five configured projects. Workspace rows use text and SF Symbols, accenting the active section. Project rows use a checkmark for the selected project. These are plain-style buttons within a sidebar list; recent-run selection uses the native selectable `List` mechanism.

### Buttons and fields

The TestFlight action is the sole explicitly `.borderedProminent` button. Other actions use native buttons, with destructive roles for storage removal. Worktree and build-number fields use `.roundedBorder`; Skip tests is a native toggle with help text. Native controls own hover, focus, pressed, and disabled rendering. Busy state disables the controls specified in source and shows a small toolbar progress indicator; do not assume every action is disabled.

### Release workbench

A heading and action row lead into explicit worktree/build inputs and a readiness `DisclosureGroup`. Recent-run rows show mode, stage, abbreviated commit, and timestamp. Job detail combines stage/actions, source path, conditional error or distribution copy, and a selectable monospaced log. Simulator allocation is automatic; the release form does not expose a device picker.

### Device rows

Each inset-list row shows device family, name, runtime/state, availability or lease ownership, worktree, optional retained seed profile/revision, and identifier. Actions include lease/release, boot/shutdown while leased, and the Telemetee-specific seed action. Copy states that ending a lease retains app data.

### Storage rows

Rows show project/kind, path, eligibility reason, formatted size, Reveal, and an eligible-only destructive Remove action. Scan-before-cleanup and a confirmation dialog expose the concrete path and retention behavior. Unknown size remains explicitly unknown.

### Feedback and confirmations

Errors and notices appear above the active detail surface with Dismiss actions. `ContentUnavailableView` supplies actionable empty states for releases, details, inventory, and storage. Native confirmation dialogs explain uploads, lease release, and destructive cleanup. Use selectable text for diagnostic paths, errors, identifiers, and logs as implemented. There are no custom animation or transition tokens.

## Do's and Don'ts

- **Do** preserve system appearance, system typography, native controls, and text-backed state indicators.
- **Do** make project/worktree ownership and operational consequences readable at the point of action.
- **Do** preserve the distinction between upload completion and tester availability.
- **Do** treat source-derived layout values as implementation facts, not evidence of verified visual fit.
- **Don't** introduce custom cards, hard-coded light/dark palettes, or decorative shadows into this established interface.
- **Don't** describe native focus, accessibility, resize behavior, or color contrast as visually tested without a completed verification pass.
