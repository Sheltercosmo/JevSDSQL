# Project requirements

Build general mechanisms from the user's objective, catalog and data. Keep dataset names, benchmark IDs and expected answers out of production decision logic.

Preserve independent work in a shared stage DAG. Document dependencies and parallel placement in [architecture](docs/ARCHITECTURE.md).

Keep VALUE, UNKNOWN and NOT_EVALUATED distinct from execution status. A skipped branch never becomes false. Preserve held proposals for user review.

Use separate development, validation and final test cases. Check paraphrases, Simplified Chinese, schema renaming and relevant NULL, duplicate and tie behavior. Distinguish deterministic test success from measured language accuracy.

Keep code readable and comments concise. Update operator usage and regenerate the function reference when public contracts change. See [CONTRIBUTING.md](CONTRIBUTING.md).
