# Changelog

## 0.6.0 (2026-11-17)

## What's Changed
* feat: listen to the hub's event stream; count motions and remote presses by @Yornik in https://github.com/Yornik/dirigera-exporter/pull/37
* feat: number the rooms and export their icon and color by @Yornik in https://github.com/Yornik/dirigera-exporter/pull/39


**Full Changelog**: https://github.com/Yornik/dirigera-exporter/compare/v0.5.0...v0.6.0

## 0.5.0 (2026-11-17)

## What's Changed
* feat: export lights, motion sensors, light sensors and remote batteries by @Yornik in https://github.com/Yornik/dirigera-exporter/pull/35
* chore(deps): update dependency dirigera to v1.6.1 by @renovate[bot] in https://github.com/Yornik/dirigera-exporter/pull/27
* chore(deps): update dependency prometheus-client to v0.26.0 by @renovate[bot] in https://github.com/Yornik/dirigera-exporter/pull/25
* chore(deps): update dependency ruff to v0.17.5 by @renovate[bot] in https://github.com/Yornik/dirigera-exporter/pull/24
* chore(deps): update dependency pytest to v9.2.0 by @renovate[bot] in https://github.com/Yornik/dirigera-exporter/pull/16


**Full Changelog**: https://github.com/Yornik/dirigera-exporter/compare/v0.4.1...v0.5.0

## 0.4.1 (2026-11-16)

## What's Changed
* fix: label Matter sub-devices (TIMMERFLOTTE) with their sibling's name and room by @djvdorp in https://github.com/Yornik/dirigera-exporter/pull/31
* fix: ignore a relationId that is not a string; name device ids in the duplicate warning by @Yornik in https://github.com/Yornik/dirigera-exporter/pull/33
* chore: credit contributors in the release notes by @Yornik in https://github.com/Yornik/dirigera-exporter/pull/34

## New Contributors
* @djvdorp made their first contribution in https://github.com/Yornik/dirigera-exporter/pull/31

**Full Changelog**: https://github.com/Yornik/dirigera-exporter/compare/v0.4.0...v0.4.1

## [0.4.0](https://github.com/Yornik/dirigera-exporter/compare/v0.3.0...v0.4.0) (2026-10-25)


### Features

* device health metrics; publish only current, valid readings ([#29](https://github.com/Yornik/dirigera-exporter/issues/29)) ([797c4ac](https://github.com/Yornik/dirigera-exporter/commit/797c4ac697794efc3035922d563c7af68e25884c))
