# AROS Meta Loop

Autonomous meta-cognition engine for the AROS ecosystem. Runs a 6-step self-improvement cycle that monitors agent performance, evaluates meta-goals, and tunes operational policy — with human-in-the-loop guardrails.

## Architecture

```
PERCEIVE → SELF-MODEL UPDATE → CRITIQUE → POLICY REVISION → IDENTITY CHECK → PERSIST
    ↑                                          |                    |            |
    |          Channel A (re-query)  ←─────────┘                    |            |
    |          Channel B (rejection) ←──────────────────────────────┘            |
    |          Channel E (drift restart) ←──────────────────────────────────────┘
    └── Channel G (persist confirmation) ←──────────────────────────────────────┘
```

**Feedback channels A–G** provide intra-cycle and inter-cycle loops with bounded iteration guards.

## Core Components

| Component | Description |
|-----------|-------------|
| **MetaLoopEngine** | 6-step cycle with async lock, abort flag, and phase gates |
| **L1 Collector** | Operational metrics from `meta_events` (tokens, success rate, errors) |
| **L2 Evaluator** | Scores 6 meta-goals (G1 Truthful → G6 Self-Know) from 0.0–1.0 |
| **L3 Signal Deriver** | Cross-validated signals (drift, calibration, recurrence) |
| **PolicyChangeClassifier** | AUTO_APPROVE / HUMAN_REVIEW / NEVER permission tiers |
| **CadenceController** | Rate limits (4 cycles/hr, 20/day, 2 emergency/hr) with per-step timeouts |
| **StateManager** | Atomic TOML config, JSONL evolution log, file-based signal queue |

## Meta-Goals (G1–G6)

| Goal | Metric | Default Threshold |
|------|--------|-------------------|
| G1 Truthful | 1.0 − error_rate | 0.80 |
| G2 Efficient | Token efficiency vs 50K baseline | 0.70 |
| G3 Reliable | Tasks without retries / total | 0.85 |
| G4 Aligned | Tool call success rate | 0.90 |
| G5 Ambitious | Task throughput proxy | 0.50 |
| G6 Self-Know | Calibration accuracy | 0.60 |

## Permission Model

- **AUTO_APPROVE**: Parameter tuning within ±20%, tightening constraints
- **HUMAN_REVIEW**: Meta-goal changes, loosening constraints, >20% change
- **NEVER**: Modifying L1 event log, disabling Critic, removing human-review

## API Endpoints

```
POST /api/meta-loop/trigger          Start a cycle
GET  /api/meta-loop/status           Status + L2 scores
POST /api/meta-loop/signal           Inject signal (low/normal/high/urgent)
GET  /api/meta-loop/evolution-log    Paginated cycle history
POST /api/meta-loop/approve/{id}     Approve pending change
GET  /api/meta-loop/pending-approvals
POST /api/meta-loop/event            Webhook (receives events from mini-claude-bot)
POST /api/meta-loop/nirmana          Activate/deactivate autonomous mode
```

## Cadence Modes

| Mode | Interval | Use Case |
|------|----------|----------|
| `balanced` | 4 hours | Normal operation |
| `aggressive` | 30 minutes | Nirmana `/away` mode |
| `conservative` | 8 hours | Low-priority background |
| `frozen` | Paused | Manual-only triggers |

## State Directory (`~/.aros/`)

```
~/.aros/
├── meta-cognition.toml    # Cadence, goals (G1–G6)
├── self-model.toml        # Capabilities, calibration
├── policy.toml            # Harness and meta-loop params
├── evolution-log.jsonl    # Append-only cycle audit trail
├── data/
│   └── meta-loop.db       # SQLite (meta_events, meta_iterations)
├── signals/               # File-based signal queue
├── pending-review/        # Changes awaiting human approval
└── state/
    ├── last_commit.json   # Channel G persistence
    └── pending_evals.json # Channel F delayed evaluations
```

## Setup

```bash
# Install
make install

# Run tests (115 tests)
make test

# Start service on port 8200
make run
```

Requires Python 3.12+.

## Integration

- **mini-claude-bot**: MetaLoopBridge pushes events via webhook, injects status into Claude context
- **telegram-claude-hero**: `/metastatus` command with G1–G6 progress bars
- **MCP tools**: 6 proxy tools (status, trigger, signal, approve, etc.)

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
