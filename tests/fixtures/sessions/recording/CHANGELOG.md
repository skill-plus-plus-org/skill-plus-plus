# Changelog

All notable changes to Skycast are documented in this file. The format is
based on Keep a Changelog.

## [1.4.0] - 2026-09-22

### Added
- Home-screen widget with today's high, low and chance of rain.
- A setting to choose between Celsius and Fahrenheit independently of the
  phone's region.

### Fixed
- The wrong temperature unit was shown after switching regions until the app
  was restarted.
- Forecasts for locations near the date line showed the previous day.

### Changed
- Internal: moved the forecast cache into its own module.
- Internal: bumped test dependencies.

## [1.3.0] - 2026-08-11

### Added
- Pull to refresh on the forecast screen.

### Fixed
- The app crashed when location access was denied at first launch.
