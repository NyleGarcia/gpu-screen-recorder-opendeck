# [2.1.0](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/compare/v2.0.0...v2.1.0) (2026-11-27)


### Bug Fixes

* read GPU Screen Recorder's config from the Flathub app too ([2e81bff](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/2e81bff1ca4a5762216914e0f5f6b5f33119976b))
* support OpenDeck and GPU Screen Recorder installed from Flathub ([88bdfbb](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/88bdfbb422f0dfa08d6d29922cd60961b08440d4)), closes [#1](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/issues/1)


### Features

* Save Section action and Stream Deck+ dial support ([ef9a828](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/ef9a82804df42bedb02b16789dca4dad07a6dabc))
* translate the plugin, with English and French ([7e73b24](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/7e73b24560695b1a769f9ded096d7f22da006364))

# [2.0.0](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/compare/v1.0.1...v2.0.0) (2026-10-12)


* feat!: rename bundle ID to systems.garcia.gsr, author to NyleGarcia ([2a5e858](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/2a5e8589c529cc8bc1259328616e947d84d51d6e))


### BREAKING CHANGES

* the plugin directory and every action UUID change from
dev.gsr.* to systems.garcia.gsr.*. OpenDeck treats this as a new plugin, so keys
configured against the old actions need to be re-added after updating.

## [1.0.1](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/compare/v1.0.0...v1.0.1) (2026-09-29)


### Bug Fixes

* **render:** fit long status lines inside the key ([d5fb93d](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/d5fb93d747b3fc1134077620d3b37b0b53737a79))

# 1.0.0 (2026-09-07)


### Features

* GPU Screen Recorder plugin for OpenDeck ([ad88da2](https://github.com/NyleGarcia/gpu-screen-recorder-opendeck/commit/ad88da2c9012af4ac27b661c4b21df56a7574933))
