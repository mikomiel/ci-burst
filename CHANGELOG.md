# Changelog

This changelog records released source history. It is not deployment,
runtime, or qualification evidence: a merge is neither a release nor a
deployment, and a released section is not a deployment record.

The version, this file and the release tag are maintained by
[release-please](https://github.com/googleapis/release-please) from merged
conventional commits on `main`; [CONTRIBUTING.md](CONTRIBUTING.md#releases)
owns the release boundary. Do not edit a released section by hand.

## [0.12.1](https://github.com/mikomiel/ci-burst/compare/v0.12.0...v0.12.1) (2026-11-25)


### Fixed

* **aws:** refuse SDK errors while the worker adapter builds its session and clients ([#180](https://github.com/mikomiel/ci-burst/issues/180)) ([#215](https://github.com/mikomiel/ci-burst/issues/215)) ([75afeb0](https://github.com/mikomiel/ci-burst/commit/75afeb0f4830830e973288d62ac771101f9e4fb4))
* **config:** the worker lifetime has one clock ([#138](https://github.com/mikomiel/ci-burst/issues/138)) ([#218](https://github.com/mikomiel/ci-burst/issues/218)) ([138bfa5](https://github.com/mikomiel/ci-burst/commit/138bfa5b12d08aab9c18d0af1619c5ed284873bc))
* **controller:** refuse a resume more than 900 s after the intent ([#230](https://github.com/mikomiel/ci-burst/issues/230)) ([8464575](https://github.com/mikomiel/ci-burst/commit/8464575278d7df4aea49f53a9898bf344b06d65e))
* **image:** a console read refused at the deadline ends the build CONSOLE_BLIND ([#203](https://github.com/mikomiel/ci-burst/issues/203)) ([#212](https://github.com/mikomiel/ci-burst/issues/212)) ([08c887c](https://github.com/mikomiel/ci-burst/commit/08c887c72baf6955bfc64a9505d3285b555e05c2))
* **image:** keep plain status at exit 0 when the check-time write fails ([#208](https://github.com/mikomiel/ci-burst/issues/208)) ([#210](https://github.com/mikomiel/ci-burst/issues/210)) ([5d12e5d](https://github.com/mikomiel/ci-burst/commit/5d12e5d5d358cd5b94f7953e68fb9fadbf101dc6))
* **image:** pin_change omits the pin line's trailing comment ([#198](https://github.com/mikomiel/ci-burst/issues/198)) ([#211](https://github.com/mikomiel/ci-burst/issues/211)) ([80f06ba](https://github.com/mikomiel/ci-burst/commit/80f06ba6c865d9664ed91abb7456f938e4ac7f6c))
* **image:** promote refuses a state that the file it plans from no longer names ([#199](https://github.com/mikomiel/ci-burst/issues/199)) ([#217](https://github.com/mikomiel/ci-burst/issues/217)) ([e8dd2f2](https://github.com/mikomiel/ci-burst/commit/e8dd2f230120a77f20ac2b9a79d7a46f5656d51c))
* **image:** recover a hot SQLite journal under the writer lock ([#194](https://github.com/mikomiel/ci-burst/issues/194)) ([#213](https://github.com/mikomiel/ci-burst/issues/213)) ([b9627bc](https://github.com/mikomiel/ci-burst/commit/b9627bc53888886d80e81ad1a749b4a031d2df04))

## [0.12.0](https://github.com/mikomiel/ci-burst/compare/v0.11.0...v0.12.0) (2026-11-22)


### Added

* **aws:** add the image-build provider ([#175](https://github.com/mikomiel/ci-burst/issues/175)) ([95702d4](https://github.com/mikomiel/ci-burst/commit/95702d4589869af18324432bdae14f2481d0c521))
* **forgejo:** read the image source with a dedicated token ([#174](https://github.com/mikomiel/ci-burst/issues/174)) ([bfe81a1](https://github.com/mikomiel/ci-burst/commit/bfe81a1c0c29841aeca086f7a8a934a517828198))
* **iam:** add the image-build role policy and its tests ([#170](https://github.com/mikomiel/ci-burst/issues/170)) ([5d0f137](https://github.com/mikomiel/ci-burst/commit/5d0f137e013e3fdfdbfb2e673eb507fa9650312b))
* **image:** build pipeline - bounded passes, supervisor, reconcile, events ([#187](https://github.com/mikomiel/ci-burst/issues/187)) ([ba40a84](https://github.com/mikomiel/ci-burst/commit/ba40a84df577a31101bec1e3485d0a4986dc4d93))
* **image:** image plan, run, status and promote commands (tasks 6.1-6.7) ([#188](https://github.com/mikomiel/ci-burst/issues/188)) ([71a5211](https://github.com/mikomiel/ci-burst/commit/71a521169cb36036ba9361f1980e826540eb15eb))
* **image:** marker reader, inert version check, failure bounds, deletion rule ([#186](https://github.com/mikomiel/ci-burst/issues/186)) ([369d78c](https://github.com/mikomiel/ci-burst/commit/369d78cfbdaa98c72acff7d701b517bc2dcbe720))
* **image:** pure build inputs - basis digest, build decision, user data ([#182](https://github.com/mikomiel/ci-burst/issues/182)) ([0ec0c48](https://github.com/mikomiel/ci-burst/commit/0ec0c48e2a6f01f6fcfb560cf77a8718407bab24))


### Fixed

* **forgejo:** refuse raw parser errors and truncated bodies ([#179](https://github.com/mikomiel/ci-burst/issues/179)) ([82238d3](https://github.com/mikomiel/ci-burst/commit/82238d3f423cd85500f6d18fb8c9cc43b628e3b3))
* **image:** cover strip_components in the basis digest ([#191](https://github.com/mikomiel/ci-burst/issues/191)) ([60a88ab](https://github.com/mikomiel/ci-burst/commit/60a88ab4421b5146b5ff22f5537540fec916654d)), closes [#185](https://github.com/mikomiel/ci-burst/issues/185)
* **image:** promote preview plans as the write would after resolving a pending row ([#193](https://github.com/mikomiel/ci-burst/issues/193)) ([77d80fb](https://github.com/mikomiel/ci-burst/commit/77d80fb43ad18724aefcae809217195b2208556a))
* **image:** protect the promoted image and bind gate evidence to the launch ([#196](https://github.com/mikomiel/ci-burst/issues/196)) ([2c044e2](https://github.com/mikomiel/ci-burst/commit/2c044e212b088ca555abede437d711ab977dca26))

## [0.11.0](https://github.com/mikomiel/ci-burst/compare/v0.10.0...v0.11.0) (2026-11-18)


### Added

* **config:** add the optional strict image tables ([#169](https://github.com/mikomiel/ci-burst/issues/169)) ([15afa88](https://github.com/mikomiel/ci-burst/commit/15afa88899fb2f78a4836dc761793c6f7b3e12b2))
* **image:** add a separate image build state file ([#168](https://github.com/mikomiel/ci-burst/issues/168)) ([ad5bf75](https://github.com/mikomiel/ci-burst/commit/ad5bf75e751f20acc924129596a60ab0bbc6f812))

## [0.10.0](https://github.com/mikomiel/ci-burst/compare/v0.9.0...v0.10.0) (2026-11-17)


### Added

* **export:** publish the instance type in worker events ([#165](https://github.com/mikomiel/ci-burst/issues/165)) ([9847730](https://github.com/mikomiel/ci-burst/commit/9847730d6082ac133e0afa9d1b94600fa1b449af))

## [0.9.0](https://github.com/mikomiel/ci-burst/compare/v0.8.0...v0.9.0) (2026-11-16)


### Added

* **image:** add an optional warm-cache layer and one cache environment ([#159](https://github.com/mikomiel/ci-burst/issues/159)) ([dee803d](https://github.com/mikomiel/ci-burst/commit/dee803d8cf350e231ecb773fd9d3b4ddbb7a3743))


### Fixed

* **runner:** limit ptrace before the claim loop polls ([#151](https://github.com/mikomiel/ci-burst/issues/151)) ([3eb3d1e](https://github.com/mikomiel/ci-burst/commit/3eb3d1ecd26d154dee050e222f71ecfff61530a1))

## [0.8.0](https://github.com/mikomiel/ci-burst/compare/v0.7.0...v0.8.0) (2026-11-15)


### Added

* **controller:** subtract recent launches from waiting demand ([#147](https://github.com/mikomiel/ci-burst/issues/147)) ([1dbeb23](https://github.com/mikomiel/ci-burst/commit/1dbeb236265b31f3676fd7c6e9e10f566ca1e515)), closes [#137](https://github.com/mikomiel/ci-burst/issues/137)
* **runner:** end an idle worker after a bounded claim window ([#146](https://github.com/mikomiel/ci-burst/issues/146)) ([da59b51](https://github.com/mikomiel/ci-burst/commit/da59b517036d7bf6f586f79fbffbe243a1cb4407))

## [0.7.0](https://github.com/mikomiel/ci-burst/compare/v0.6.0...v0.7.0) (2026-11-14)


### Added

* **worker:** publish a marker when the Spot interruption notice arrives ([#133](https://github.com/mikomiel/ci-burst/issues/133)) ([3f2a1e3](https://github.com/mikomiel/ci-burst/commit/3f2a1e39ddede6e149dae65aa9622d8ddd037e35))


### Fixed

* **refresh:** retain a bounded set of image tags ([#134](https://github.com/mikomiel/ci-burst/issues/134)) ([802b96c](https://github.com/mikomiel/ci-burst/commit/802b96cfc790e879104e014689c2424e2de62353))

## [0.6.0](https://github.com/mikomiel/ci-burst/compare/v0.5.1...v0.6.0) (2026-11-13)


### Added

* **observe:** record a bounded fleet snapshot per apply pass ([#130](https://github.com/mikomiel/ci-burst/issues/130)) ([3b0be80](https://github.com/mikomiel/ci-burst/commit/3b0be80b766be317384a2fe6837bd216078317a7))

## [0.5.1](https://github.com/mikomiel/ci-burst/compare/v0.5.0...v0.5.1) (2026-11-13)


### Fixed

* **deploy:** normalise engine image IDs across prefix forms ([#128](https://github.com/mikomiel/ci-burst/issues/128)) ([5aa08ff](https://github.com/mikomiel/ci-burst/commit/5aa08ffae5f0f3603304937f0649037fcaf4b96e))

## [0.5.0](https://github.com/mikomiel/ci-burst/compare/v0.4.0...v0.5.0) (2026-11-13)


### Added

* **deploy:** publish deployment state after each refresh ([#123](https://github.com/mikomiel/ci-burst/issues/123)) ([98f0e12](https://github.com/mikomiel/ci-burst/commit/98f0e128ba8f91405ffc043d23c65cdf9f025521))

## [0.4.0](https://github.com/mikomiel/ci-burst/compare/v0.3.0...v0.4.0) (2026-11-13)


### Added

* **aws:** bound provider reads and report an interrupted pass ([#118](https://github.com/mikomiel/ci-burst/issues/118)) ([278d10b](https://github.com/mikomiel/ci-burst/commit/278d10b226885ac4f2e6ea503d5a779b43f07655))
* **cost:** report burstable CPU-credit exposure in plan evidence ([#115](https://github.com/mikomiel/ci-burst/issues/115)) ([87f1bfb](https://github.com/mikomiel/ci-burst/commit/87f1bfbebe2b42625f0e29161274ca04e0212260))
* **deploy:** record immutable build inputs for built artefacts ([#116](https://github.com/mikomiel/ci-burst/issues/116)) ([76d4d3a](https://github.com/mikomiel/ci-burst/commit/76d4d3aaa13c6326902254109d1b22102d8c7178))


### Fixed

* **aws:** refuse a malformed inventory instead of reading it as empty ([#120](https://github.com/mikomiel/ci-burst/issues/120)) ([2831d2b](https://github.com/mikomiel/ci-burst/commit/2831d2bf7c1e3756aa3b49f959ba3159e2e70b47))
* **aws:** resolve a mixed not-found instance batch per identifier ([#103](https://github.com/mikomiel/ci-burst/issues/103)) ([01b3204](https://github.com/mikomiel/ci-burst/commit/01b320440cf467367ea3c823d876421521758213))
* **cleanup:** report a degraded cleanup result and its evidence ([#106](https://github.com/mikomiel/ci-burst/issues/106)) ([d0cecff](https://github.com/mikomiel/ci-burst/commit/d0cecff8e6e7e8b60fcdb30e351f7d4ac8d0e0a8))
* **controller:** refuse a foreign deployment scope and report batch headroom ([#105](https://github.com/mikomiel/ci-burst/issues/105)) ([019091c](https://github.com/mikomiel/ci-burst/commit/019091cef0886b5635ade80b6291863e01327bf8))
* **cost:** freeze admitted cost inputs and bill the minimum once ([#111](https://github.com/mikomiel/ci-burst/issues/111)) ([4f4717d](https://github.com/mikomiel/ci-burst/commit/4f4717d6c900b385f76708d381a067765f823b8b))
* **export:** observe bounded evidence during a supported drain ([#112](https://github.com/mikomiel/ci-burst/issues/112)) ([cf14861](https://github.com/mikomiel/ci-burst/commit/cf14861aedd8e2791e52f497f591aeb4779e76f5))
* **prerequisites:** treat an enabled source-destination check as drift ([#104](https://github.com/mikomiel/ci-burst/issues/104)) ([5c39db3](https://github.com/mikomiel/ci-burst/commit/5c39db38d7e8b81724e6864703d6f13389b89532))
* **worker:** preserve the one-job process result through cleanup ([#110](https://github.com/mikomiel/ci-burst/issues/110)) ([8a83e98](https://github.com/mikomiel/ci-burst/commit/8a83e988a755ff1836797e38c333a8dfb3f87277))

## [0.3.0](https://github.com/mikomiel/ci-burst/compare/v0.2.0...v0.3.0) (2026-11-12)


### Added

* **config:** allow ordered instance-type pools beside zones ([#78](https://github.com/mikomiel/ci-burst/issues/78)) ([037fa5f](https://github.com/mikomiel/ci-burst/commit/037fa5f5de2e2f5c2a4eeed62dc7aa1d7f0f7f75))
* **config:** raise the concurrency ceiling to twelve ([#70](https://github.com/mikomiel/ci-burst/issues/70)) ([3eba851](https://github.com/mikomiel/ci-burst/commit/3eba85139c3c18150fa213727c4ada04765df076))
* **cost:** settle tracked worker cost against the allowance ([#73](https://github.com/mikomiel/ci-burst/issues/73)) ([0a9f1f6](https://github.com/mikomiel/ci-burst/commit/0a9f1f6bf7f8b869f3d0e0ea25bfcdccc0e5f65f))
* **deploy:** record the worker image build recipe ([#75](https://github.com/mikomiel/ci-burst/issues/75)) ([645364b](https://github.com/mikomiel/ci-burst/commit/645364b109a02c488217d459d3cbefbcf2dcdaaf))
* **export:** surface worker console evidence as events and metrics ([#69](https://github.com/mikomiel/ci-burst/issues/69)) ([098359c](https://github.com/mikomiel/ci-burst/commit/098359cabaf2014246ee347ab1655f537cd28ee3))
* **router:** ship the optional in-VPC router as a deployment artefact ([#68](https://github.com/mikomiel/ci-burst/issues/68)) ([657dae8](https://github.com/mikomiel/ci-burst/commit/657dae855fb268d0505e4a14929e6853a134579d))


### Fixed

* **aws:** treat an exhausted Spot quota as a definite refusal ([#72](https://github.com/mikomiel/ci-burst/issues/72)) ([e4bf73f](https://github.com/mikomiel/ci-burst/commit/e4bf73f28b8228a5bc914a5fc3150786ebc9b202))
* **ci:** recognise the generated release PR without a review ([#113](https://github.com/mikomiel/ci-burst/issues/113)) ([d8a9c17](https://github.com/mikomiel/ci-burst/commit/d8a9c17373686b20302cdd76896e43a3d6086200))
* **deploy:** refresh the image on a cleanup-only host ([#64](https://github.com/mikomiel/ci-burst/issues/64)) ([22a0db5](https://github.com/mikomiel/ci-burst/commit/22a0db5cc1797eb8e56b359a524d8dd1057b801d))
* **runner:** make the repository bootstrap the deployed template user data ([#66](https://github.com/mikomiel/ci-burst/issues/66)) ([ca17763](https://github.com/mikomiel/ci-burst/commit/ca17763d3d90d5f36e50b2134c550d9f19feff4b))
* **runner:** run the job with the fleet's umask ([#65](https://github.com/mikomiel/ci-burst/issues/65)) ([abe5092](https://github.com/mikomiel/ci-burst/commit/abe5092ebd83b4a6daa21f508cb467ae3f1f7df6))

## [0.2.0] - 2026-11-11

### Added

- Run a bounded fleet of up to four workers ([#59](https://github.com/mikomiel/ci-burst/pull/59)).

### Fixed

- A terminated unrecorded instance stops blocking admission ([#54](https://github.com/mikomiel/ci-burst/pull/54)).

### Documentation

- How to prove the cleanup identity terminates ([#55](https://github.com/mikomiel/ci-burst/pull/55)).
- The policy names the subnets the deployment may use ([#56](https://github.com/mikomiel/ci-burst/pull/56)).
- Record the shipped stage and archive the delivered change ([#61](https://github.com/mikomiel/ci-burst/pull/61)).

### CI

- Report a stale generated Unreleased section ([#58](https://github.com/mikomiel/ci-burst/pull/58)).

## [0.1.0] - 2026-11-10

### Added

- Add the bounded AWS Spot controller ([#4](https://github.com/mikomiel/ci-burst/pull/4)).
- Add ordered availability-zone fallback to the Spot launch ([#25](https://github.com/mikomiel/ci-burst/pull/25)).
- Add container image and independent apply/reap units ([#24](https://github.com/mikomiel/ci-burst/pull/24)).
- Verify worker prerequisites against the deployment ([#37](https://github.com/mikomiel/ci-burst/pull/37)).
- Advertise the runner architecture label ([#38](https://github.com/mikomiel/ci-burst/pull/38)).
- Refresh the controller image from merged main ([#39](https://github.com/mikomiel/ci-burst/pull/39)).
- Report unit drift without installing anything ([#49](https://github.com/mikomiel/ci-burst/pull/49)).

### Fixed

- Read review status creators from the status list ([#3](https://github.com/mikomiel/ci-burst/pull/3)).
- Send the mandatory queue label filter and accept a null job list ([#28](https://github.com/mikomiel/ci-burst/pull/28)).
- Keep the dotenv filename out of the private-dns-name scan rule ([#27](https://github.com/mikomiel/ci-burst/pull/27)).
- Treat blocked status as the dependency signal ([#36](https://github.com/mikomiel/ci-burst/pull/36)).
- Run the units and the smoke on the host's MTU network ([#41](https://github.com/mikomiel/ci-burst/pull/41)).
- Measure quote freshness and release the allowance honestly ([#44](https://github.com/mikomiel/ci-burst/pull/44)).

### Documentation

- Propose bounded AWS Spot queue burst ([#1](https://github.com/mikomiel/ci-burst/pull/1)).
- Add a self-service AWS runner setup tutorial ([#5](https://github.com/mikomiel/ci-burst/pull/5)).
- Add runner setup follow-ups from live testing ([#22](https://github.com/mikomiel/ci-burst/pull/22)).
- Add the deployment walkthrough and control-plane policies ([#33](https://github.com/mikomiel/ci-burst/pull/33)).
- Add a changelog and the release boundary ([#23](https://github.com/mikomiel/ci-burst/pull/23)).
- Record the exclusive routing label and match the config example to it ([#46](https://github.com/mikomiel/ci-burst/pull/46)).
- Dependency-blocked work is not listed by the endpoint ([#47](https://github.com/mikomiel/ci-burst/pull/47)).
- Burst capacity joins the strong-ci pool ([#50](https://github.com/mikomiel/ci-burst/pull/50)).
- Size the window for launches, not for dispatches ([#51](https://github.com/mikomiel/ci-burst/pull/51)).

### CI

- Gate merges on exact-head Gandalf approval ([#2](https://github.com/mikomiel/ci-burst/pull/2)).
- Add dependency automation and document the merge scope ([#8](https://github.com/mikomiel/ci-burst/pull/8)).
- Relay review events to the review gate ([#26](https://github.com/mikomiel/ci-burst/pull/26)).
- Build the container image without credentials ([#45](https://github.com/mikomiel/ci-burst/pull/45)).
