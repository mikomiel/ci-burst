## Summary

<!-- What changed and why. -->

## Linked issue

<!-- Closes # -->

## Checks run

- `npm ci --ignore-scripts --no-audit --no-fund`
- `npm run check:policy`
- `npm run check:spec`

## Checklist

- [ ] Behaviour changes carry an OpenSpec change and its acceptance evidence.
- [ ] No credentials, cloud account identifiers, private addresses or deployment details.
- [ ] The change touches one decision and does not include unrelated refactors.
