# FlightDeck

A local macOS app and JSON CLI for iOS release jobs, simulator leases and build storage. The source lives at `~/claude-projects/flightdeck`, along with the packaged `FlightDeck.zip`. The verified local app is installed at `~/Applications/FlightDeck.app`. Open that copy in Finder. If FlightDeck is already open, quit and reopen it to load the final build.

## What works in 0.1

- Native SwiftUI app with Releases, Devices and Storage views for the five configured projects.
- Readiness checks, local archive and TestFlight launch, captured logs and structured stages, persistent job history and cancellation. Closing the window does not stop a job.
- A shared SQLite job/lease registry. One managed release at a time bounds CPU/disk use; simulator leases are acquired atomically. Each project runs its existing checks and signing logic.
- Automatic 4/2/2-format build numbers reserved across managed releases; an explicit build-number override supports existing version trains. Check the latest uploaded number before the first release using a changed numbering scheme. Direct terminal scripts retain their existing defaults.
- Automatic read-only App Store Connect processing checks every 45 seconds for up to one hour after an upload, plus a manual refresh. These require the project's App Store Connect API key. Xcode-account uploads still work without that key; availability remains unverified until checked separately. No beta groups are edited.
- Simulator inventory, manual leases, boot/shutdown for leased devices, and retained app data. Telemetee can run its existing idempotent Solina course installer on a leased simulator with the app already installed.
- Storage measurement and explicit removal of selected per-release DerivedData only. Archives, dSYMs, simulator data and shared caches are not cleanup targets in this version. Active jobs, any leases or detected external builds block cleanup.

## First use

1. Open `~/Applications/FlightDeck.app`, select a project, and choose **Check Readiness**.
2. The integrations are now committed and merged to GitHub main in all five repositories, and the local main checkouts include them. The app still requires a clean worktree. Pitpass's R2 utility and output ignore rules are also merged, and its checkout now passes the clean-worktree check. Existing unrelated local application commits in Patchbridge, Telemetee and Cuekeeper were preserved without publishing them. See `integrations/merge-status.json` for PRs and verification results.
3. Optionally enter the absolute path of an existing Git worktree belonging to that project. The same integration commit must be present there. Blank means the configured main checkout.
4. Build a local archive first. The project still performs signing and any production CloudKit read/check gates. `Build Archive` does not upload. Tests are on by default.
5. Use **Release to TestFlight** when you intend to upload; the confirmation shows the source and whether tests will run. Uploads can create a local tag via the existing scripts. No tag is pushed automatically.

Release defaults use available devices of the required family, preferring booted devices. Runtime/scheme compatibility is ultimately checked by Xcode; use CLI `--device UUID` (repeat for Forkware's phone/tablet) to select explicit free simulators. Managed tests disable parallel simulator cloning. A release cannot reuse a manually held lease: release it first, or use another device.

## CLI and agents

`~/.local/bin/flightdeck` points to this folder's launcher. Add `~/.local/bin` to your shell PATH or use the full path. The launcher now points to `~/claude-projects/flightdeck/flightdeck`. The original chat output directory forwards here for compatibility with older links.

```sh
flightdeck projects
flightdeck preflight patchbridge
flightdeck start patchbridge --mode archive
flightdeck start patchbridge --worktree /absolute/path/to/worktree --mode testflight
flightdeck jobs
flightdeck detail RUN_ID
flightdeck cancel RUN_ID
flightdeck recover RUN_ID
flightdeck asc-status RUN_ID
flightdeck devices
flightdeck lease acquire --project patchbridge --worktree /absolute/path/to/worktree --device UUID --owner agent-task-name
flightdeck lease heartbeat --device UUID --token TOKEN
flightdeck device-action boot --device UUID --token TOKEN
flightdeck lease release --device UUID --token TOKEN
flightdeck seed telemetee --device UUID --token TOKEN
flightdeck storage
flightdeck cleanup SCAN_ID ITEM_ID --confirm
```

CLI results are JSON; errors return a nonzero exit code and an `error` property. A lease token is needed to heartbeat, release, boot/shutdown or seed. Use the returned UUID as the exact test/install/launch/capture destination. Do not use the ambiguous `booted` selector. A lease does not stop direct Xcode or simctl commands; every agent must follow the same protocol. Lease state persists without TTL reclamation. Use heartbeat to make ownership visible, then release explicitly. Recovery refuses live recorded processes/process groups and recent queued workers. After a crash, verify Apple's upload state before retrying a release.

The current release path invokes scripts in the selected checkout; do not edit that checkout while its job runs. A managed release excludes another managed release, but cannot lock unrelated tools or editors.

## Data and configuration

State and logs: `~/Library/Application Support/FlightDeck/`. The SQLite database and run logs are local. `FLIGHTDECK_STATE` overrides this location for isolated tests. No telemetry is collected.

Copy `engine/projects.json` to the state directory to override repository paths. `FLIGHTDECK_PROJECTS` selects an alternate configuration for CLI/test use. A custom worktree must have the same Git common directory as its configured repository.

Signing stays with the project's existing Xcode/Keychain setup. For Apple status checks, the runner reads only literal `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_KEY_PATH` values from the project's `.release.env` or the process environment. It does not execute that file for status checks. The existing release scripts retain their normal credential-loading behavior. Private keys are not copied into FlightDeck, written into job records, or logged. The status client uses OpenSSL to sign short-lived ES256 JWTs and HTTPS to Apple's API.

## Build and tests

Requires macOS 14+, full Xcode, Python 3.9+ and the project's existing dependencies. No third-party Python packages are required. The app uses Homebrew Python if present, then `/usr/bin/python3`. `FLIGHTDECK_PYTHON` and `FLIGHTDECK_ENGINE` can override those paths for development.

```sh
./build.sh
python3 -m unittest discover -s Tests -v
```

Build intermediates live in the project-local `.build/` directory by default; `FLIGHTDECK_BUILD_DIR` overrides that location. The build script creates an ad-hoc-signed local app. It is not notarized for distribution to other Macs. `engine/integrate.py` previews integration changes; `--apply` installs them in configured repositories, without committing. For a new checkout, also adopt the lease workflow in its agent instructions.

## Current boundaries

This is a local first version. It does not embed Device Hub screens, manage physical-device leases, provide reusable clean simulator clones, seed the other four apps, or automate broad storage retention. Those projects need their own idempotent seed hooks and schema/fixture contracts. Telemetee's seed records describe its course fixture, not all app/account data. Shared backend/CloudKit state is not isolated by a simulator lease.

There is no automatic restart after logout/reboot. Job and lease records survive; use recovery after confirming processes stopped. Processing checks stop after an hour or missing credentials; use the manual button afterward. Build cancellation cannot undo an upload Apple already accepted.

Validation: release app compiled and launched; live inventory returned 10 simulators; 16 isolated runner/launcher tests cover lease contention, cancellation, detached execution, cleanup boundaries, script events and beta state interpretation. All five integrated scripts passed shell syntax and help checks. The user subsequently verified a real Cuekeeper archive build with tests and TestFlight upload disabled. No upload, real cleanup, simulator reboot or seed installation was performed during development verification. The organization's computer-use policy blocked FlightDeck UI inspection, so visual layout and click-through behavior remain unverified.

The original Documents build location added Finder metadata to the build-folder app after signing. Source has since moved to `~/claude-projects/flightdeck`. The installed copy in `~/Applications` passed strict code-signature verification; `FlightDeck.zip` was created from that clean copy. Prefer the installed copy or ZIP for use.
