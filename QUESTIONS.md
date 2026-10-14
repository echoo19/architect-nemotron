# Questions
- Please provide real human-edit logs when available. Until then all examples and quality claims are synthetic; no production improvement claim.
- Real provider API keys are needed only for opt-in live adapter validation; offline tests continue without them.
- What ARC-AGI-3 submission-time tools/models/compilation are permitted? Assume runtime selection and tuning only, no stage 3; official SDK 0.9.9 is available. Public rules confirm offline Kaggle evaluation but exact caps and submission tooling permissions remain unverified; see docs/research.md. Local toy games are test fixtures, not a claim of competition compliance.
- A GitHub remote and CI run access are required to prove push CI; this new repository currently has neither. Local clean-clone tests will not be described as remote CI evidence.

## Phase 6 official ARC evidence (in progress)

The official SDK and engine are installed and pinned, not missing. Official score normalization still needs a documented unit/range and a verified public scorecard fixture. Unknown scores must fail unavailable, never become local win scores. The adapter must distinguish repeatable public OFFLINE development episodes from the one-card/one-make/all-environment submission gateway constraints. The current GameFactory/provider manifest seam is being reviewed for official targets; local puzzle-instance tests are not official ARC results. No numeric Kaggle-cap or submission-compliance claim is authorized.

### Resolution: documented official normalization implemented

The documented completed-card percentage conversion is now implemented and pinned to SDK 0.9.9 / engine 0.9.3. Its metric is `relative_human_action_efficiency.percent.v1`; raw values must be finite numbers in 0..100. The generic OFFLINE manifest/factory path and per-game versus whole-card decoding have controlled transport tests. The remaining external gap is a real staged-public-game/model smoke run and confirmed submission rules, not missing normalization code.
