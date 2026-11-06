# Ordinary Windows startup reliability

The user reports v0.4.0 exits immediately after launch. Reproduce the shipped
ordinary entry point before changing production code. Existing `--ui-smoke-test`
coverage cannot establish that an ordinary profile starts correctly.

Test the complete extracted portable package on Windows with test-owned profiles:
missing language bootstrap, legacy schema 1, system, zh-CN, en-US and malformed
bootstrap. Use no real credentials, no provider requests, no messages, no registry
changes and no user deployment. Preserve saved configuration and all prior data.

If supported by actual evidence, correct the invalid startup language path and
add bounded, credential-free startup error evidence. Preserve the real exception
and stop on fatal initialization failure; do not silently continue in an invalid
state. Include independent review, full regression and Windows verification before
merging to main. Do not publish a Release or delete branches.
