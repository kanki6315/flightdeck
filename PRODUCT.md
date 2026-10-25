# FlightDeck
<!-- impeccable:product-schema 1 -->

## Platform
macOS

## Stack
SwiftUI native app and Python 3 standard-library local runner, SQLite state, JSON CLI.

## Users and purpose
A developer coordinating TestFlight releases, Apple Device Hub simulators, agent worktrees, and build storage across Pitpass, Patchbridge, Telemetee, Cuekeeper, and Forkware.

## Operating context
Local repositories under ~/claude-projects. Existing project scripts own signing, tests and CloudKit gates. The app and agents use the same durable runner. Jobs survive closing the GUI. No uploads or cleanup occur during development verification.

## Principles
Explicit project and worktree ownership; preserve project-specific checks; report real stages; protect active resources; show errors with recovery actions.

## Scope
Native release history, preflight and release launch, simulator inventory and exclusive leases, persistent seed metadata, storage inventory and guarded cleanup. App Store Connect processing is separate from successful upload. Working name is provisional.
