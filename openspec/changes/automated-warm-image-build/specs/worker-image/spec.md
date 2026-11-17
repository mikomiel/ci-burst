# Spec Delta

## ADDED Requirements

### Requirement: The archive prefix is an explicit input
`warm-cache.sh` MUST accept one optional input `WARM_SRC_STRIP_COMPONENTS` with the value 0 or 1, default 0, and MUST pass it to the extraction. Any other value MUST end the script before any consumer command runs and without the ready marker. The value MUST come from the environment, never from the archive.

#### Scenario: A forge archive with one top-level directory
- **WHEN** `WARM_SRC_STRIP_COMPONENTS` is 1
- **THEN** the extraction removes the one leading path component
- **AND** the warm commands run from the extracted source root

#### Scenario: The input is absent
- **WHEN** `WARM_SRC_STRIP_COMPONENTS` is unset
- **THEN** the extraction keeps every path component, as before

#### Scenario: The input is invalid
- **WHEN** `WARM_SRC_STRIP_COMPONENTS` is 2, negative or not an integer
- **THEN** the script exits non-zero before any consumer command
- **AND** it prints no ready marker
