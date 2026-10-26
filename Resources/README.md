# FlightDeck app icon

Open `FlightDeck.icon` in Apple's Icon Composer to edit the app icon. The document contains three independent SVG layers: Simulator, Takeoff trail, and Flight deck. The SVG files inside its `Assets` folder are the canonical artwork; no flattened concept-sheet image is used by the app.

The selected design is a simulator launching from a flight deck, in aubergine (`#35283F`), apricot (`#F3B184`), and warm cream (`#F6EADB`). The background and platform masking are supplied by Icon Composer.

Run `./build.sh` with full Xcode installed. Apple's asset compiler creates `Assets.car` for the modern icon and `FlightDeck.icns` for older macOS versions, and its generated icon metadata is merged into the app's Info.plist before signing. The built app is `FlightDeck.app` at the project root. Building does not replace a separate installed copy in `~/Applications` or refresh `FlightDeck.zip`.
