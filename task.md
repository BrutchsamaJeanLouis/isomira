# Task

[Plain language description of what needs to happen. 2-4 sentences max.
Be specific about the deliverable -- file names, function names, behaviour.]

## Scope

[List the file paths that are in play, relative to workspace. One path per line.]

workspace/my_module.py

## Domain Knowledge

[CRITICAL SECTION. This is your hallucination shield.
Front-load every fact the models need that they might fabricate.
API parameter ranges, library function signatures, algorithm specifics,
data format details. If a quantized model might guess wrong, state it here.]

## Constraints

[What the models must NOT do. Packages to avoid. Patterns to follow.
Negative constraints are as important as positive ones.]

- Dependencies: stdlib only.
- All public methods must have type hints.
