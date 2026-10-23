# <Feature> — feature page template

> Copy to `docs/features/<feature>.md`. Short: an agent should be able to work on the feature after reading it, without rereading the whole module.

## Purpose
One sentence: what the feature is for, and for whom.

## Where the code is
Modules, routers, models, tests.

## Business rules
Numbered, each one tied to the function that applies it. A rule with no code behind it is marked "not enforced".

## States and transitions
If any: which states, who changes them, where.

## Functional edge cases
Handled ones, and open ones (to be decided by a human).

## Checks
Commands and tests that prove the feature.
