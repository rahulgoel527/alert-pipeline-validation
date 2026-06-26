# Alert Pipeline Lab — Presentation Guide

## Why Option G (Custom Pipeline)

Off-shelf stacks (ELK, Wazuh, OpenSearch) abstract away exactly the failure modes this assignment asks you to test — alerts disappearing, stuck workers, dedup instability under load. A custom pipeline exposes them by design. Injectable behaviors (5% failure rate, 2% hung workers, 10% slowness, 10% generator duplicates) give the test suite real conditions to assert against, not synthetic mocks. Every layer is owned, so every layer is testable and every state transition is observable. The Postgres ledger exists specifically because you cannot write the troubleshooting SQL against a black-box SIEM.

---

## Session Agenda (60 minutes)

| Time | Block |
|------|-------|
| 0–5 min | Architecture walkthrough — README diagram, 8 services, state machine |
| 5–20 min | Live demo — pipeline running, dashboard (`:8050`), Dejavu (`:1358`), API stats |
| 20–40 min | Test suite walkthrough — unit → E2E → load, dual-source assertion strategy, key design decisions |
| 40–50 min | Troubleshooting exercise — incident reproduction, 3 hypotheses, validation steps |
| 50–60 min | Q&A |

---

